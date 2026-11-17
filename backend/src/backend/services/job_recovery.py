"""恢复器（票 #83）：过期租约的收口——接手、回排队、判失败或**兜底落已取消**。

## 本模块回答什么问题

执行权在 #82 之后是数据库里一条**会到期**的租约。到期这条出路让「执行者还在跑」
有了一个确定的上限，但也留下一个必须收口的新空档：**执行者的进程崩了，租约到期，
没人接手**。这一行如果没人动，它会永远停在 `running`——用户看到进度条一直转，
运维看到一条说不清的尸体行。

票 #84 之后，同一个空档还有一个变种：**用户已经请求取消（行处于 `cancelling`），
执行者却卡在模型调用里不肯退出**。租约到期之后同样由恢复器接手，结局是 `cancelled`
（用户的明确意图），而不是一次失败（AC3）。

恢复器是**可直接调用的单元**：`recover_once(limit)` 扫一批、逐条收口、返回报告。
不启动线程、不起定时器、不 sleep——接入调度循环（多久扫一次、扫到之后怎么派发）
是 #89 的事。

## 档位与去向

| 档位 | 候选集 | 去向 |
|---|---|---|
| 周期（`startup=False`，默认） | `running` 且租约已过期；`cancelling` 且租约已过期（或没有租约） | `running`：按剩余次数回排队（→ `pending`）或判失败；`cancelling`：**兜底落 `cancelled`** |
| 启动（`startup=True`，`reap_orphans` 用） | 库里所有**非终态**行（`pending` / `running` / `cancelling`） | 一律**落终态**：`cancelling` → `cancelled`；`running` 有租约 → `failed + LEASE_EXPIRED`；`running` 没租约 / `pending` → `failed + INTERNAL_ERROR`（服务重启中断） |

为什么启动档不看「剩余次数」：**启动那一刻没有任何执行者会来领这一行**。单 worker
是部署硬约束（`_RUNTIMES` 在进程内），进程刚起来时库里不可能有真正在跑的任务；
而本票不引入调度循环（那是 #89），把行清成「排队中」等于让它永远排队——AC4
「重启后不存在永远进行中」当场就红。

## 判权：fencing 用扫描时读到的旧凭证

扫描只负责**挑候选**，真正的「改不改得到」由状态机的条件写回答。条件是**扫描时
读到的那一份旧租约**（`job_state.LeaseFence`：凭证 + 到期时间），不是「刚才查到的
行」。于是两种竞争都改到 0 行：凭证被换掉（别人已经接手）、到期时间被推后（执行者
其实还在跑，刚续了租）。改到 0 行 = 这一行已经不是我扫描时看到的那一份死租约了：
**跳过、不报错、不记账**（丢弃日志由状态机统一留）。

## 去向判定规则

按账本（`job_attempts`）已经记了几次来判——账本行数 = 已结束的执行次数。本票
**不新增迁移的列**，`job_attempts` 的行数就是唯一的事实来源，与 `append` 的
`MAX(attempt_no)+1` 同源。

    attempts + 1 >= DEFAULT_MAX_ATTEMPTS  → 判失败（EXPIRE）
    否则                                  → 回排队（REQUEUE）

即「共允许 3 次执行机会」：首次 + 两次恢复接手。第 3 次崩溃之后落终态，不再重试。

**取消中的行不走这套次数规则**：用户已经说要停，再回排队等于把用户叫停的任务重新
跑起来——直接落 `cancelled`（一次确认，没有「下一轮」）。

## 账本与状态同事务

每收口一行都记一条超时账本（`error_code=LEASE_EXPIRED`，摘要写明超时原因），
`cancelling` 那一支记 `outcome=cancelled`，与状态更新在**同一个事务**里（#80 的口径）。
执行者不在场，所以用量与耗时是**未知**而不是 0 成本（记 0）。

## 本票不做什么

- 调度循环 / 定时器 / 派发（#89）：本模块只提供 `recover_once` 这个入口。
- 审批 / 人工介入：超时收口是机械规则，不在这里排队等人。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime

from backend.core.clock import utc_now
from backend.core.engine import session_scope
from backend.repositories.generation_job_repo import (
    STATUS_CANCELLING,
    STATUS_PENDING,
    GenerationJobRepository,
)
from backend.repositories.job_attempt_repo import JobAttemptRepository
from backend.services import job_state

logger = logging.getLogger(__name__)

# 一条任务共允许几次执行机会（首次 + 恢复接手）。判据取账本行数。
DEFAULT_MAX_ATTEMPTS = 3

# 单次扫描的上限：恢复是**逐条收口**的维护动作，一次收太多会把一批跨用户的写压在
# 一个事务周期里（每行各自一个事务，但仍是同一轮里的连续写）。
DEFAULT_RECOVERY_LIMIT = 20

# 账本 / 报告里的去向。`skipped` 不是失败：它是「这一行已经不是我扫描时看到的那一份
# 死租约了」（凭证被换 / 续了租 / 已经终态），属于正常竞争，不报错。
ACTION_REQUEUED = "requeued"
ACTION_FAILED = "failed"
ACTION_CANCELLED = "cancelled"
ACTION_SKIPPED = "skipped"

# 超时的原因文案。写进账本摘要与任务行 `error_message`。
MESSAGE_LEASE_EXPIRED = "执行者进程已消失，租约到期未续，任务已中断"
# 从没被领过的残留行：租约一次都没发过，说成「过期」会把排查方向带偏。
MESSAGE_RESTART_INTERRUPTED = "服务重启，任务已中断"
# 卡住的取消中：执行者没来确认，恢复器替它收口。
MESSAGE_CANCEL_CONFIRMED = "执行者未确认取消，租约到期后已收为已取消"

# 不是 `ErrorCode` 成员的启动回收码（`reap_orphans` 的「服务重启中断」失败收尾）。
CODE_RESTART_INTERRUPTED = "INTERNAL_ERROR"


@dataclass(frozen=True, slots=True)
class RecoveryTarget:
    """一条候选任务：扫描时读到的、足以完成收口的最小事实。

    `status` 是扫描那一刻的取值（七态）：它决定这一条的去向完全不同——
    `cancelling` 收成已取消，`running` 才按账本次数回排队 / 判失败。
    所有字段都是扫描那一刻的值，之后不再回查——回查会悄悄换上新的凭证，于是 fencing 失效。
    """

    owner_id: int
    job_id: int
    status: str = ""
    kind: str = ""
    solution_id: int | None = None
    lease_owner: str = ""
    lease_token: str = ""
    lease_expire_at: str = ""

    @property
    def fence(self) -> job_state.LeaseFence:
        """这次的 fencing 判据：扫描时读到的旧凭证 + 旧到期时间。"""
        return job_state.LeaseFence(token=self.lease_token, expire_at=self.lease_expire_at)


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    """一条候选的去向与判据（**行数是唯一判据**，与状态机同一口径）。"""

    target: RecoveryTarget
    action: str
    rows: int
    attempts: int = 0
    error_code: str = ""

    @property
    def applied(self) -> bool:
        """是否真的改到了那一行。"""
        return self.rows > 0


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """一轮恢复的报告：扫到几条、各自去了哪里。

    计数是**从 outcomes 派生**的，不是另一个计数器：报告与实际发生的收口只有一份
    事实，避免「报告说收了 3 条、账本只有 2 条」这种两说。
    """

    started_at: str
    startup: bool = False
    outcomes: tuple[RecoveryOutcome, ...] = ()

    @property
    def scanned(self) -> int:
        return len(self.outcomes)

    @property
    def requeued(self) -> int:
        return sum(1 for item in self.outcomes if item.action == ACTION_REQUEUED)

    @property
    def failed(self) -> int:
        return sum(1 for item in self.outcomes if item.action == ACTION_FAILED)

    @property
    def cancelled(self) -> int:
        return sum(1 for item in self.outcomes if item.action == ACTION_CANCELLED)

    @property
    def skipped(self) -> int:
        return sum(1 for item in self.outcomes if item.action == ACTION_SKIPPED)


def scan(
    limit: int = DEFAULT_RECOVERY_LIMIT,
    *,
    now: datetime | None = None,
    startup: bool = False,
) -> list[RecoveryTarget]:
    """扫一批该接手的任务（只挑候选，判权在 `recover_one` 的条件写里）。

    条件见模块 docstring 的档位表；排序 `lease_expire_at, id`：过期最久的最先收。

    Args:
        limit: 本次最多扫几条。
        now: 判据用的「现在」，留空取 UTC 当前时刻（注入只为用例能演示时间跨度）。
        startup: 启动档（候选集不看到期时间，见模块 docstring）。
    """
    moment = now or utc_now()
    with session_scope() as session:
        rows = GenerationJobRepository(session).scan_recoverable(
            now_text=moment.isoformat(), limit=limit, startup=startup
        )
        # 在会话内把模型折成不可变的候选：模型在 `session_scope()` 提交之后会过期，
        # 拿出去的会是「要回库刷新的对象」，而不是扫描那一刻的事实。
        return [
            RecoveryTarget(
                owner_id=int(row.owner_id),
                job_id=int(row.id or 0),
                status=row.status,
                kind=row.kind,
                solution_id=row.solution_id,
                lease_owner=row.lease_owner,
                lease_token=row.lease_token,
                lease_expire_at=row.lease_expire_at,
            )
            for row in rows
        ]


def _attempts_used(job_id: int) -> int:
    """这一条任务已经记了几次账（= 已结束的执行次数）。"""
    with session_scope() as session:
        return JobAttemptRepository(session).count_for_job(job_id)


def _decide(
    target: RecoveryTarget, *, attempts: int, startup: bool
) -> tuple[job_state.JobEvent, str, str]:
    """判这一条的去向：返回（事件、错误码、原因文案）。

    优先级按「用户意图 > 机械超时」排：

    1. `cancelling`：**任何档位**都收成已取消——用户已经说要停，再跑或判失败都读错了
       他的意思（AC3）。
    2. 没有租约（`pending`，或启动档里从没被领过的 `running`）：原因是「服务重启中断」，
       不是「租约过期」——租约一次都没发过。
    3. 启动档的 `running`：一律按租约超时判失败（那一刻没有执行者会来领）。
    4. 周期档的 `running`：次数用完判失败，否则回排队。
    """
    if target.status == STATUS_CANCELLING:
        return (
            job_state.JobEvent.EXPIRE_CANCELLING,
            "",
            MESSAGE_CANCEL_CONFIRMED,
        )
    if target.status == STATUS_PENDING:
        # 启动档才扫得到排队中：登记完还没派发就崩了，本票没有调度循环，留着就是永远排队中。
        return (
            job_state.JobEvent.EXPIRE_PENDING,
            CODE_RESTART_INTERRUPTED,
            MESSAGE_RESTART_INTERRUPTED,
        )
    if not target.lease_token:
        return (
            job_state.JobEvent.FAIL,
            CODE_RESTART_INTERRUPTED,
            MESSAGE_RESTART_INTERRUPTED,
        )
    if startup:
        return (
            job_state.JobEvent.EXPIRE,
            job_state.CODE_LEASE_EXPIRED,
            MESSAGE_LEASE_EXPIRED,
        )
    if attempts + 1 >= DEFAULT_MAX_ATTEMPTS:
        return (
            job_state.JobEvent.EXPIRE,
            job_state.CODE_LEASE_EXPIRED,
            MESSAGE_LEASE_EXPIRED,
        )
    return (
        job_state.JobEvent.REQUEUE,
        job_state.CODE_LEASE_EXPIRED,
        MESSAGE_LEASE_EXPIRED,
    )


def _progress_payload(session, owner_id: int, job_id: int) -> job_state.FinishPayload:
    """按行上**已有的**进度造一份收尾载荷，用于「服务重启中断」那条失败收尾。

    为什么不给空载荷：`fail` 是整段载荷写入（正文 / 章节 / 用量 / 两列 token 一起
    覆盖）。给空载荷等于把这一行可能已有的半截进度清掉——按现值回填更诚实。
    """

    def _load(text: str, default):
        try:
            return json.loads(text) if text else default
        except json.JSONDecodeError:  # 脏数据不该挡住收口，退回空值
            return default

    job = GenerationJobRepository(session).get(owner_id, job_id)
    if job is None:
        return job_state.FinishPayload()
    return job_state.FinishPayload(
        draft_content=job.draft_content or "",
        completed_sections=_load(job.completed_sections, []) or [],
        usage=job.usage,
        pending_items=_load(job.pending_items, None),
        prompt_tokens=int(job.prompt_tokens or 0),
        completion_tokens=int(job.completion_tokens or 0),
    )


def recover_one(
    target: RecoveryTarget, *, now: datetime | None = None, startup: bool = False
) -> RecoveryOutcome:
    """接手一条候选：按去向判定走状态机的条件写，返回它实际去了哪里。

    整条路径只有**一次状态转换**（`job_state.transition`）：状态机内部完成条件写、
    账本、丢弃日志与内存结算，本模块不自己写库。
    """
    moment = now or utc_now()
    now_text = moment.isoformat()
    attempts = _attempts_used(target.job_id)
    event, code, message = _decide(target, attempts=attempts, startup=startup)

    if event is job_state.JobEvent.FAIL or event is job_state.JobEvent.EXPIRE_PENDING:
        with session_scope() as session:
            payload = _progress_payload(session, target.owner_id, target.job_id)
        result = job_state.transition(
            event,
            job_state.job_scope(target.owner_id, target.job_id),
            payload=payload,
            error_code=code,
            error_message=message,
            now_text=now_text,
        )
        action = ACTION_FAILED
    else:
        result = job_state.transition(
            event,
            job_state.job_scope(target.owner_id, target.job_id),
            error_code=code,
            error_message=message,
            lease_fence=target.fence,
            now_text=now_text,
        )
        if event is job_state.JobEvent.REQUEUE:
            action = ACTION_REQUEUED
        elif event is job_state.JobEvent.EXPIRE_CANCELLING:
            action = ACTION_CANCELLED
        else:
            action = ACTION_FAILED

    if not result.applied:
        # 改到 0 行：凭证被换掉 / 刚续过租 / 行已终态。跳过、不报错、不记账——
        # 丢弃日志由状态机留（`job.transition_discarded`），与其它转换同一口径。
        action = ACTION_SKIPPED
    logger.info(
        "恢复器接手了一条过期租约",
        extra={
            "event": "job.recovered",
            "action": action,
            "job_id": target.job_id,
            "user_id": target.owner_id,
            "kind": target.kind,
            "solution_id": target.solution_id,
            "lease_owner": target.lease_owner,
            "status": target.status,
            "attempts": attempts,
            "rows": result.rows,
            "startup": startup,
        },
    )
    return RecoveryOutcome(
        target=target,
        action=action,
        rows=result.rows,
        attempts=attempts,
        error_code=code if result.applied else "",
    )


def recover_once(
    limit: int = DEFAULT_RECOVERY_LIMIT,
    *,
    now: datetime | None = None,
    startup: bool = False,
) -> RecoveryReport:
    """**本票的入口**：扫一批、逐条收口、返回报告（不起线程 / 不起定时器）。

    每条候选各自一个事务（`recover_one` 里的状态转换），所以一条写失败不会连带
    别的候选：#89 的调度循环直接调用它即可。
    """
    moment = now or utc_now()
    targets = scan(limit, now=moment, startup=startup)
    outcomes = tuple(recover_one(target, now=moment, startup=startup) for target in targets)
    report = RecoveryReport(started_at=moment.isoformat(), startup=startup, outcomes=outcomes)
    if report.outcomes:
        logger.info(
            "本轮恢复完成",
            extra={
                "event": "job.recovery_report",
                "scanned": report.scanned,
                "requeued": report.requeued,
                "failed": report.failed,
                "cancelled": report.cancelled,
                "skipped": report.skipped,
                "startup": startup,
            },
        )
    return report


__all__ = [
    "ACTION_CANCELLED",
    "ACTION_FAILED",
    "ACTION_REQUEUED",
    "ACTION_SKIPPED",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_RECOVERY_LIMIT",
    "MESSAGE_CANCEL_CONFIRMED",
    "MESSAGE_LEASE_EXPIRED",
    "MESSAGE_RESTART_INTERRUPTED",
    "RecoveryOutcome",
    "RecoveryReport",
    "RecoveryTarget",
    "recover_once",
    "recover_one",
    "scan",
]
