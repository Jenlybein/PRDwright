"""生成过程事件表（`job_events`）的数据仓储（票 #86）。

## 这张表是什么

生成过程的**日志**：一条事件一行，主键 `id` 是**全局自增游标**。读侧只回答一个问题：
「游标之后还有什么」。因此本层的读方法固定是 `WHERE job_id = ? AND id > ?`（走
`idx_job_events_job_cursor`），写方法只做追加。

## 写入路径与事务边界

`append` 的调用方只有服务层的事件写入点（`services/job_events`），后者总是与
「进度列合并写」或「收尾终态写」在**同一个事务**里做——事件与它描述的状态必须同生
共死。因此本类**不 commit、不 rollback**：事务边界仍归调用方的 `session_scope()`。

## 为什么按「全局游标」而不是「每任务序号」

- 谁的游标都只是一个整数：客户端把它原样带回来，服务端 `id > ?` 一句就能继续；
- 同一任务内 id 天然有序，不需要再存第二份「顺序」；
- 不存 `owner_id`：归属经任务行 join 确认，事件表不制造第二份归属真源。

## 为什么保留策略不在这里写

清理是**运维动作**（按终态时间删过期行），不是数据写入；它住在服务层
（`services/job_events.purge_expired_events`），本层只提供 `prune(cutoff)` 这一句
条件删除——仓储层天天拿来判断业务规则，规则就会分叉。

## 返回形状

读方法返回 `JobEvent` 模型对象（仓储层既有约定，不返回裸 dict）。
"""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlmodel import Session

from backend.core.clock import utc_now_text
from backend.core.models import GenerationJob, JobEvent
from backend.repositories.generation_job_repo import STATUS_RUNNING

# 一次读最多取多少行：订阅是流，不是导出。上限存在的意义是「一页别把内存打爆」，
# 不是分页语义——读到满页就说明还有，下一轮接着读。
DEFAULT_PAGE = 200


class JobEventRepository:
    """`job_events` 的追加与按游标读取（绑定一个 `Session`，不碰事务边界）。"""

    def __init__(self, session: Session) -> None:
        """绑定一个 `Session`；事务边界由调用方的 `session_scope()` 决定。"""
        self.session = session

    def append(self, job_id: int, rows: list[dict[str, str]]) -> int:
        """批量追加事件行，返回**最后一行**的游标（`rows` 为空时返回 0）。

        一次批量只 `flush` 一次：`add_all` + 单次 `flush` 让同一个事务里的那一段事件
        一起进库（订阅者因此不会看到「半批已提交」的中间态）。id 由序列分配，
        返回最大那一条的 id 即「这批写到哪」。

        Args:
            job_id: 归属任务（外键 `ON DELETE CASCADE`）。
            rows: 每项含 `kind` 与 `payload`（JSON 文本）；可带 `created_at` 供对拍注入。

        Returns:
            本批最后一行的 id；空批返回 0。
        """
        if not rows:
            return 0
        created = utc_now_text()
        objects = [
            JobEvent(
                job_id=job_id,
                kind=str(row["kind"]),
                payload=str(row["payload"]),
                created_at=str(row.get("created_at") or created),
            )
            for row in rows
        ]
        self.session.add_all(objects)
        self.session.flush()
        return max(int(item.id or 0) for item in objects)

    def list_after(self, job_id: int, after_id: int, *, limit: int = DEFAULT_PAGE) -> list[JobEvent]:
        """该任务里**游标之后**的事件，按 id 升序（不含 `after_id` 那一条）。

        条件里只有任务与游标：调用方（订阅循环）在进入本方法之前已经从任务行确认了
        归属，这里再 join 一次只会多读一张表，不会多一层保护。
        """
        statement = (
            select(JobEvent)
            .where(JobEvent.job_id == job_id, JobEvent.id > after_id)
            .order_by(JobEvent.id)
            .limit(limit)
        )
        return list(self.session.execute(statement).scalars().all())

    def max_id(self, job_id: int) -> int:
        """该任务最新一行的游标；没有任何事件时为 0。

        它是**快照游标**的来源：任务行的进度列与事件行在同一个事务里写，所以
        「此刻读到的进度」恰好覆盖到 `max_id` 为止的所有事件——冷读者拿它往后追，
        既不会重复也不会留下缺口。
        """
        newest = self.session.execute(
            select(func.max(JobEvent.id)).where(JobEvent.job_id == job_id)
        ).scalar_one()
        return int(newest or 0)

    def prune(self, *, cutoff: str) -> int:
        """删掉「终态且收尾时间早于 `cutoff`」的任务的全部事件行，返回删除行数。

        `cutoff` 是 ISO 8601 文本（字典序 = 时间序，与全库口径一致）；比较对象是
        **任务行的 `updated_at`**——终态就是在那次更新里落下的，所以它等于「收尾时间」。
        `running` 的任务无论如何都不清理：它的过程日志正是活着的那条订阅要读的。
        """
        terminal_jobs = select(GenerationJob.id).where(
            GenerationJob.status != STATUS_RUNNING,
            GenerationJob.updated_at < cutoff,
        )
        result = self.session.execute(
            delete(JobEvent).where(JobEvent.job_id.in_(terminal_jobs))
        )
        return int(result.rowcount or 0)
