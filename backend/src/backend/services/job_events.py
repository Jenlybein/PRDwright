"""生成过程事件日志：写入、按游标读取、过期清理（票 #86）。

## 它解决什么

浏览器断线 / 刷新之后，「上次读到哪里」原先没有落点：进程内的 `JobBus` 只把事件推给
**当刻连着**的订阅者，晚一步就没有了。于是重连只能靠「快照 + 继续 live」，而快照与
live 之间那段只能靠运气。本模块把过程日志落成 `job_events` 里的行，订阅改按**游标**
读表：

    SELECT ... FROM job_events WHERE job_id = ? AND id > ? ORDER BY id

游标是**读者**手里的状态（客户端收到的那一条的 id），不是任务的。因此：

- 断线重连带自己的游标回来 → 只读它之后的行，**不重不漏**；
- 多个订阅者各持各的游标 → 各读各的，互不扣减；
- 进程内唤醒（`job_bus`）或数据库通知（NOTIFY）只负责「叫醒」，**数据永远来自表**；
  没有通知就靠 `POLL_SECONDS` 轮询兜底。

## 与「权威正文」的分工（重要）

- `generation_jobs.draft_content` / `completed_sections` / `phase` 是**快照**：
  冷读者一次拿走，回答「现在长什么样」；
- 本模块写的事件是**过程日志**：回答「一路走来发生了什么」，且**只增不改**；
- 两者在**同一个事务**里写（见 `job_runtime_events._maybe_flush` 与
  `job_finalize._closeout_once`），所以「快照游标 = 快照内容覆盖到的最后一条事件」
  是一条结构保证，而不是靠时序运气。冷读者因此**不回放**历史。

## 为什么需要「先记内存、再合并落盘」

事件是逐条到达的（模型增量可能一秒几百条），但每次来一条就写一次库既没必要、
又与既有的「≤300ms 或增量 >4KB 合并写」节奏打架。所以先记进 `JobRuntime.pending_events`
（纯内存、零 IO），到合并写窗口时与进度列**同一笔**落盘。`mark_flushed` 只在事务
提交之后调用——回滚了就不能把缓冲清掉，否则那一批事件会永久丢失。

## 事件种类

`EVENT_KINDS` 就是对外 SSE 的事件种类（+ 收尾的 `run_summary`）：`snapshot` 不在其中，
它是由快照列**现算**出来的，不是过程日志。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlmodel import Session

from backend.core.clock import utc_now
from backend.core.contracts import StreamEvent
from backend.core.engine import session_scope
from backend.repositories.job_event_repo import DEFAULT_PAGE, JobEventRepository

if TYPE_CHECKING:
    from backend.core.models import JobEvent
    from backend.services.job_runtime import JobRuntime

logger = logging.getLogger(__name__)

# 过程日志允许的 `kind` 取值 == 对外 SSE 的事件种类。
#
# `snapshot` 刻意不在其中：它不是「发生过的一件事」，而是由任务行当前列**现算**出来的
# 视图；把它也存一份，就等于给权威正文造了第二份会漂移的副本。
EVENT_KINDS: frozenset[str] = frozenset(
    {
        "status",
        "chunk",
        "part",
        "stage",
        "stage_chunk",
        "review",
        "error",
        "done",
        "run_summary",
    }
)

# 终态之后过程日志的保留期（天）。清理单元见 `purge_expired_events`。
RETENTION_DAYS = 7

# 订阅的轮询节拍（秒）：没有唤醒时靠它兜底推进。
#
# 0.25 是取舍：比合并写窗口（0.3s）略小，所以「事件刚落盘就有人来读」的最坏延迟
# 仍在一个合并窗口之内；再密一点只是白烧 CPU 与连接。
POLL_SECONDS = 0.25


def payload_of(event: StreamEvent) -> str:
    """把一个事件序列化成 `job_events.payload`（与 `StreamEvent` 三字段一一对应）。

    `snapshot` 不进表（见 `EVENT_KINDS`），调用方只应传过程事件。
    """
    return json.dumps(
        {"content": event.content, "data": event.data, "code": event.code},
        ensure_ascii=False,
    )


def event_of(row: JobEvent) -> StreamEvent | None:
    """把一行事件还原成 `StreamEvent`；坏行（未知 kind / 非法 JSON）返回 `None`。

    容错是刻意的：读侧是一个**长连接循环**，一行坏数据不该把整条订阅打断。
    跳过它并在日志里留一句，排查时仍能看见「这里丢了一条」。
    """
    kind = str(row.kind)
    if kind not in EVENT_KINDS:
        logger.warning(
            "跳过未知种类的事件行",
            extra={"event": "job_events.unknown_kind", "job_id": row.job_id, "kind": kind},
        )
        return None
    try:
        body = json.loads(row.payload or "{}")
    except (json.JSONDecodeError, TypeError):
        logger.warning(
            "跳过坏事件行",
            extra={"event": "job_events.bad_payload", "job_id": row.job_id, "id": row.id},
        )
        return None
    if not isinstance(body, dict):
        return None
    data = body.get("data")
    code = body.get("code")
    return StreamEvent(
        type=kind,  # type: ignore[arg-type] - 已由 EVENT_KINDS 收敛到 Literal 取值
        content=str(body.get("content") or ""),
        data=data if isinstance(data, dict) else {},
        code=code if isinstance(code, str) and code else None,
        # 游标走 SSE 的 `id:` 行（模型字段，`exclude=True` 不进 JSON 体）：
        # 客户端据此知道「读到哪了」，下次断线重连原样带回来。
        event_id=int(row.id or 0),
    )


def record(runtime: JobRuntime, event: StreamEvent) -> None:
    """把一条过程事件记进运行时的待写缓冲（纯内存，不碰库、不 await）。

    非过程事件（`snapshot`）直接丢弃：它不是「发生过的事」，进表只会成为第二份正文。
    """
    if event.type not in EVENT_KINDS:
        return
    runtime.pending_events.append(event)


def flush_pending(
    runtime: JobRuntime, session: Session, *, extra: tuple[StreamEvent, ...] = ()
) -> int:
    """在**调用方的事务**里把待写事件 + `extra` 落盘，返回本批最后的游标。

    `extra` 给收尾那一笔用：`run_summary` 事件在收尾时才算出来，它必须与终态写
    **同一个事务**提交，否则订阅者会看到「任务已结束、回执还没来」的窗口。

    Args:
        runtime: 待写缓冲的来源。
        session: 调用方已打开的事务（本函数不 commit）。
        extra: 追加在缓冲之后一起落盘的事件（通常只有 `run_summary`）。

    Returns:
        本批最后一行的 id；这批为空时返回 `runtime.last_event_id`（即「没有新事件」）。
    """
    events = list(runtime.pending_events) + list(extra)
    if not events:
        return runtime.last_event_id
    rows = [{"kind": event.type, "payload": payload_of(event)} for event in events]
    return JobEventRepository(session).append(runtime.job_id, rows)


def mark_flushed(runtime: JobRuntime, cursor: int) -> None:
    """事务**提交之后**清空待写缓冲，并把游标推进到 `cursor`。

    只能在提交之后调：回滚时缓冲必须还在，否则那一批事件永久丢失（而客户端只会看到
    「断线重连少了一段」这种极难归因的现象）。
    """
    runtime.pending_events.clear()
    if cursor > runtime.last_event_id:
        runtime.last_event_id = cursor


def read_after(
    job_id: int, cursor: int, *, limit: int = DEFAULT_PAGE
) -> tuple[list[StreamEvent], int]:
    """读一批「游标之后」的事件，返回 ``(事件, 本页最后一条的 id)``。

    这是订阅循环唯一的取数入口：一次一页、读空就说明「此刻没有更新的」，
    下一轮等唤醒或轮询再来。每次调用一个短事务（读完即关，不持连接）。
    坏行跳过但游标仍越过整页——否则会反复读同一页、卡在那里。
    """
    with session_scope() as session:
        rows = JobEventRepository(session).list_after(job_id, cursor, limit=limit)
        page_last = int(rows[-1].id or 0) if rows else cursor
        events = [event_of(row) for row in rows]
    return [event for event in events if event is not None], page_last


def cursor_of(job_id: int) -> int:
    """该任务最新一行事件的游标（0 = 还没有任何事件）。冷读者的快照游标就是它。"""
    with session_scope() as session:
        return JobEventRepository(session).max_id(job_id)


def purge_expired_events(
    *, retention_days: int = RETENTION_DAYS, now: datetime | None = None
) -> int:
    """清理超过保留期的过程日志，返回删除行数（**可直接调用的清理单元**）。

    策略：任务已经是**终态**、且收尾时间（任务行的 `updated_at`）早于
    `now - retention_days` 时，删掉它的全部事件行。仍在执行的任务一条都不删
    （那是活着的那条订阅要读的日志）。

    **权威正文不受影响**：正文在 `generation_jobs.draft_content` 与
    `document_versions.content` 上，本动作只 `DELETE FROM job_events`。

    这里刻意**不**起后台定时器、不接调度循环：调度是 #89 / 清理票的事，本票只交付
    「按保留期删过程日志」这一个可以直接被调用的动作。
    """
    moment = now or utc_now()
    if moment.tzinfo is not None:
        # 库里一律 naive UTC 文本（见 `core/clock.py`）：带时区的入参先归一，
        # 否则 `WHERE updated_at < ?` 的字典序会在时区后缀上悄悄错位。
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    cutoff = moment - timedelta(days=retention_days)
    with session_scope() as session:
        deleted = JobEventRepository(session).prune(cutoff=cutoff.isoformat())
    logger.info(
        "清理过期过程日志",
        extra={
            "event": "job_events.purged",
            "deleted": deleted,
            "retention_days": retention_days,
            "cutoff": cutoff.isoformat(),
        },
    )
    return deleted
