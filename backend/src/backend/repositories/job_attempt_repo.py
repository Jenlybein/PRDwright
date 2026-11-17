"""执行账本（`job_attempts`）的数据仓储（票 #80）。

## 这张表是什么

每一次「已结束的执行」一行：第几次（`attempt_no`）、成还是败（`outcome`）、为什么
（`error_code` + 脱敏摘要）、花了多少（两列 token）、跑了多久（`duration_ms`）。
它只增不改，是**审计账**；任务行上的 `error_code` / `error_message` 只保留最近一次，
完整历史必须来这里查。

## 写入路径只有一个

`append` 的调用方**只有状态机收尾**（`services/job_state.transition`）：账本与任务终态在
**同一个事务**里写，任一笔写不进去也不允许把一次真实成功的生成判死（服务层用保存点
隔开，见 `job_state._record_attempt`）。因此本类**不 commit、不 rollback**——事务边界
仍归调用方的 `session_scope()`。

`append` 刻意不带 `owner_id`：写入路径不面向用户，归属在「任务状态的条件更新」那一句
`WHERE owner_id=?` 里已经校验过，这里再查一次只会多一次 SELECT 而不会多一层保护。
读方法仍强制带归属（经 `generation_jobs` join）——越权读在 SQL 里就查不到。唯一的例外
是系统级维护读 `count_for_job`（恢复器计数用，见该方法的说明）。

## 序号怎么分配

`attempt_no` 是**同一任务内**从 1 起的序号，由本层算 `MAX(attempt_no)+1`（与状态更新
同一事务，单写者进程内天然串行）；`ux_job_attempts_job_attempt`（`job_id + attempt_no`
唯一）是并发下的最后一道门——重复记账会在 flush 阶段撞上它，而不是悄悄多一行。
这与 `document_versions` 的 `(document_id, version_no)` 同一套路：先读后写在单进程下
够用，唯一约束兜住例外。

## 返回形状

读方法返回 `JobAttempt` 模型对象，不返回裸 dict（仓储层的既有约定）。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlmodel import Session

from backend.core.clock import utc_now_text
from backend.core.models import GenerationJob, JobAttempt

# 账本里的「结论」取值。三条都是**执行结束**，区别只在于为什么结束：
# `completed` 跑成；`failed` 没跑成（含上游报错 / 预算超顶 / 进程崩溃后被判死）；
# `cancelled` 用户或系统主动停止。**被取代不在其中**——它不是这次执行的结局。
OUTCOME_COMPLETED = "completed"
OUTCOME_FAILED = "failed"
OUTCOME_CANCELLED = "cancelled"


class JobAttemptRepository:
    """`job_attempts` 的写入与按任务读取（绑定一个 `Session`，不碰事务边界）。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def append(
        self,
        job_id: int,
        *,
        outcome: str,
        error_code: str = "",
        error_summary: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        duration_ms: int = 0,
        created_at: str | None = None,
    ) -> JobAttempt:
        """追加一条账本（序号自动取该任务的下一号），`flush` 后返回模型对象。

        不 commit：账本必须和任务终态同生共死，提交时机由调用方（状态机的
        `session_scope()`）决定。`flush` 是为了拿到自增 id 并让唯一约束当场生效
        （等到提交才炸，异常就落在状态更新的事务里了）。

        Args:
            job_id: 归属任务（外键 `ON DELETE CASCADE`）。
            outcome: 三个结论之一（本模块的 `OUTCOME_*`）。
            error_code: 失败 / 取消的分类；成功留空。
            error_summary: **已脱敏**的单行摘要（脱敏发生在服务层，本层不加工文案）。
            prompt_tokens / completion_tokens / duration_ms: 这一轮的用量与耗时。
            created_at: 记账时间；缺省用当前 UTC 文本时钟（测试可注入固定值）。
        """
        attempt = JobAttempt(
            job_id=job_id,
            attempt_no=self._next_attempt_no(job_id),
            outcome=outcome,
            error_code=error_code,
            error_summary=error_summary,
            prompt_tokens=int(prompt_tokens or 0),
            completion_tokens=int(completion_tokens or 0),
            duration_ms=int(duration_ms or 0),
            created_at=created_at or utc_now_text(),
        )
        self.session.add(attempt)
        self.session.flush()
        return attempt

    def list_by_job(self, owner_id: int, job_id: int) -> list[JobAttempt]:
        """某任务的完整账本历史，按序号升序（归属不符时返回空列表）。

        归属条件写在 SQL 里（`generation_jobs.owner_id`），不是读出来再比：
        「查不到」与「不是你的」在这一层是同一个结果。
        """
        statement = (
            select(JobAttempt)
            .join(GenerationJob, JobAttempt.job_id == GenerationJob.id)
            .where(
                GenerationJob.owner_id == owner_id,
                JobAttempt.job_id == job_id,
            )
            .order_by(JobAttempt.attempt_no)
        )
        return list(self.session.execute(statement).scalars().all())

    def _next_attempt_no(self, job_id: int) -> int:
        """该任务的下一账本序号（1 起）：`MAX(attempt_no) + 1`。"""
        current = self.session.execute(
            select(func.max(JobAttempt.attempt_no)).where(JobAttempt.job_id == job_id)
        ).scalar_one()
        return int(current or 0) + 1

    def count_for_job(self, job_id: int) -> int:
        """该任务已经记了几次（**系统级维护读**，没有 `owner_id`）。

        调用点只有恢复器（#83）：一行 `job_attempts` = 这一次执行有结论，所以行数就是
        「已经试过几次」。恢复器据此与允许的最多次数比较，决定超时的行是回排队还是
        判失败——这是在没有 `attempt_count` 列（本票不允许加迁移）时唯一的事实来源，
        与 `append` 的 `MAX+1` 同源，不会出现「账本与计数两说」。

        不做归属校验：恢复器的扫描本来就跨用户，而它随后要做的是条件写——真正能改到
        那一行的判据在 `WHERE owner_id + lease_token + lease_expire_at` 里（见
        `generation_job_repo.requeue_lease` / `fail_expired`）。这里只回答「记了几次」，
        不承担授权。
        """
        total = self.session.execute(
            select(func.count()).select_from(JobAttempt).where(JobAttempt.job_id == job_id)
        ).scalar_one()
        return int(total or 0)
