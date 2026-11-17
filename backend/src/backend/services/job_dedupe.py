"""创建去重：同一 owner + 方案 + 产物 + 输入指纹，10 秒内的重复提交复用既有任务（票 #87）。

## 判据（显式，便于逐条对票）

- **键**：`owner_id` + `solution_id`（可空，按 `IS NULL` 比）+ `kind` + `doc_kind`
  + `input_fingerprint`。`kind` 与 `doc_kind` 都参与：它们回答「这次做的是什么、写到哪
  一份」，产物不同就不是同一次提交（例如「优化接口文档」与「生成 PRD」即使指纹相同
  也不该被合并）。今天指纹只由 PRD 计算，所以这条键同时挡住了「不同动作、相同指纹」。
- **时间窗**：10 秒。下界 = `core/clock.utc_now()` 减 10 秒，交给 SQL 与
  `created_at`（`timestamp_text()`，ISO 文本、字典序即时间序）做文本比较。
  时钟只从 `core/clock.py` 取——这个仓库所有时间文本的唯一入口，不在别处散 `datetime.now()`。
- **复用的既有任务必须仍在 `running`**。终态不复用：生成已经结束，用户再点一次是
  「重新生成」，照常新建（也才能再走一次顶替语义）。所以「第二次提交」永远不会拿到
  上一次已经产出的结果充当本次结果。
- **没有 `input_fingerprint` 的任务不参与去重**（今天只有 PRD 落指纹）：其它种类的
  建单语义一个字都不变——顶替、必填校验、方案存在检查、额度口径都照旧。

## 为什么本票不加唯一索引、不加迁移

判据里有**滚动的 10 秒窗**，唯一索引表达不了它；而对
`(owner, solution, kind, fingerprint)` 建普通唯一索引会把「10 秒之后合法的新建」
误伤成冲突。所以本票不加索引（`generation_jobs.input_fingerprint` 列已存在，未迁移），
索引留给后续统一加索引的票。

跨请求的原子性由 `job_registration` 的**进程内互斥锁**兜住：查 → 建 → 提交收进一段
临界区。单 worker 是本项目的部署硬约束（`_RUNTIMES` / `JobBus` / 运行时快照都在进程内，
见约定 §11），因此进程内互斥就足够；残留竞态只在「多 worker / 多进程」这种不支持的
部署形态下出现（两个进程会各建一条）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlmodel import Session

from backend.core.clock import utc_now
from backend.core.models import GenerationJob
from backend.repositories.generation_job_repo import GenerationJobRepository
from backend.services.job_ticket import JobTicket

logger = logging.getLogger(__name__)

# 时间窗：既有任务 `created_at` 距今不超过这么多秒才可复用（票面口径）。
REUSE_WINDOW_SECONDS = 10


def window_start(now: datetime | None = None) -> str:
    """窗口下界的时间文本（`now - REUSE_WINDOW_SECONDS`）。

    单独成函数是为了让窗口口径只有一个出处，也让「超窗仍正常新建」可以用**挪时钟**
    来测（不必 sleep 11 秒）：用例把 `job_dedupe.utc_now` 换成「真时间 + 11 秒」即可。
    """
    moment = now if now is not None else utc_now()
    return (moment - timedelta(seconds=REUSE_WINDOW_SECONDS)).isoformat()


def find_reusable_job(session: Session, ticket: JobTicket) -> GenerationJob | None:
    """在登记事务里查「这次提交可以复用哪一条既有任务」；没有就返回 `None`。

    只认**仍在 running、且在 10 秒窗内**的同键任务。读用登记事务同一个 Session，
    避免「查完到落库之间」另开一条连接看到别的快照。
    """
    fingerprint = str(ticket.input_fingerprint or "").strip()
    if not fingerprint:
        # 没有指纹就没有去重键（今天只有 PRD 会算指纹）：建单语义完全不变。
        return None

    job = GenerationJobRepository(session).find_dedupe_target(
        ticket.owner_id,
        solution_id=ticket.solution_id,
        kind=ticket.kind,
        doc_kind=ticket.doc_kind,
        input_fingerprint=fingerprint,
        created_since=window_start(),
    )
    if job is None:
        return None
    logger.info(
        "重复提交复用既有任务",
        extra={
            "event": "job.dedupe_reused",
            "job_id": int(job.id or 0),
            "user_id": ticket.owner_id,
            "solution_id": ticket.solution_id,
            "kind": ticket.kind,
            "doc_kind": ticket.doc_kind,
        },
    )
    return job


__all__ = ["REUSE_WINDOW_SECONDS", "find_reusable_job", "window_start"]
