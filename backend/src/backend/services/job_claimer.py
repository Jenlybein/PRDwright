"""抢占 + 租约 + 动作点续租（票 #82）：执行权是数据库里一条**会到期**的租约。

## 本模块回答什么问题

「谁在执行这一行」以前是进程内存里的一个标志：多开一个进程，两份内存各说各话；
进程崩了，那一行仍是 `running`，没人知道它其实没人在跑。现在这件事搬进数据库，
并且**会到期**——三件事一起才构成执行权：

| 字段 | 作用 | 参与判权 |
|---|---|---|
| `lease_owner` | 谁领的（`主机/进程`） | **不参与**（只用来回答「出问题找谁」） |
| `lease_token` | 本轮凭证，逐轮随机 | 参与 |
| `lease_expire_at` | 到期时间 | 参与 |

判权必须看凭证、不能看归属：用归属判权时，同一个进程的上一轮会被认成这一轮，
「旧执行者的晚到结果」就写得进去——那正是本票要挡的东西。

## 三件事与它们各自的口径

1. **抢占**（`claim` / `claim_once`）：判断与修改压进**同一条 UPDATE**（条件写在
   `GenerationJobRepository.claim`）。改到 1 行 = 领到了；改到 0 行 = 别人先领了
   或者租约还没到期。它经 `job_state.transition(CLAIM, ...)` 走，是「排队中 → 执行中」
   的转换，不另写一套改状态的路径。
2. **续租**（`renew`）：把到期时间往后推。**失败本身就是信号**——凭证对不上说明这一轮
   已经被取消或被接手，翻成 `LeaseLostError` 让链路立刻停（不重试、不写结果）。
   续租**不检查「还没到期」**：一次比租约时长更久的模型调用不该让执行者把自己判死。
3. **动作点续租**（`renew_before_action`）：每次模型调用前调一次（挂在
   `stream_reply._astream_chunks` 这个唯一入口上，覆盖全部调用点）。上一次续租到下一次
   之间最多隔「一次模型调用」，所以 120 秒的租约足以撑住十几分钟的任务。

## 「排队中」就是 `pending`（票 #84）

七态之后，「排队中」有自己的取值（`pending`），不再是「`running` + 没有租约」：
登记建行即 `pending`，`claim` 是「排队中 → 执行中」的转换（写状态 + 写租约），
恢复器回排队也把行写回 `pending`。于是抢占条件只有一条 `WHERE status='pending'`，
「谁在跑」只看 `running`——同一个问题不会有两个表示。

## 取消：续租失败要先分清「被取消」还是「被接手」

续租改到 0 行有两种原因，处置**相反**（票 #84 的 AC2）：

| 原因 | 信号 | 处置 |
|---|---|---|
| 行处于 `cancelling` | `CancelRequestedError` | 由这一位执行者确认取消（落 `cancelled`，补一次载荷） |
| 凭证不是本轮 / 行已被恢复器回排队 | `LeaseLostError` | 就地停止，**什么都不写**（结果已失去资格） |

把两者混成一个信号，就会出现「用户按了停止、执行者却以为被人抢走而不落终态」
（任务卡在取消中直到恢复器兜底）或者反过来「被接手的旧执行者去确认取消」。

## 本票不做什么

- **失败重试**：本模块永不自己制造第二次尝试——失去执行权只是「停」，
  要不要再跑是恢复器与退避的事。

## 谁在调 `claim_once`

生产里只有一个调用者：`services/job_scheduler.py` 的领取周期（约 1 秒一次）。
它只**补漏**——新建任务仍然在登记之后立刻 `claim` + 派发（交互延迟不经过任何周期）；
`claim_once` 负责的是「退避到期了、但上一轮没人派发」的那些行。
"""

from __future__ import annotations

import logging
import os
import secrets
import socket
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Iterator

from backend.core.clock import utc_now
from backend.core.engine import session_scope
from backend.repositories.generation_job_repo import (
    STATUS_CANCELLING,
    GenerationJobRepository,
)
from backend.services import job_state

if TYPE_CHECKING:
    from backend.services.job_runtime import JobRuntime

logger = logging.getLogger(__name__)

# 租约时长（秒）。120 秒是「一次模型调用」的量级：动作点续租把到期时间一次次往后推，
# 于是十几分钟的长任务只要还在动，就永远不会中途失效（AC5）。
DEFAULT_LEASE_SECONDS = 120.0

# 默认的「谁领的」：主机名 + 进程号。**仅供排查**——判权看凭证（见模块 docstring）。
LEASE_OWNER = f"{socket.gethostname()}/{os.getpid()}"


class LeaseLostError(RuntimeError):
    """本轮执行权已失效（被接手 / 凭证对不上）。

    它是一个**明确的中止信号**，不是「这次生成失败了」：调用方（`run_job`）必须就地
    停下、不写任何结果、也不广播失败——那一行可能正被新的执行者跑着，把「我失去资格」
    说成「任务失败」会让用户看到一次并不存在的失败。
    """

    def __init__(self, job_id: int, reason: str = "") -> None:
        self.job_id = job_id
        self.reason = reason or "本轮执行权已失效"
        super().__init__(f"任务 {job_id}：{self.reason}")


class CancelRequestedError(RuntimeError):
    """**用户已经请求取消**（行处于 `cancelling`），不是「执行权丢了」。

    两者的处置相反：失去执行权要什么都不写（别人接手了）；取消请求则要由这一位
    执行者把行确认成已取消——它是唯一还有资格写这一行的人，半截正文与已经烧掉的
    用量也要由它补一次（票 #84 的 AC2）。
    """

    def __init__(self, job_id: int, reason: str = "") -> None:
        self.job_id = job_id
        self.reason = reason or "任务已被请求取消"
        super().__init__(f"任务 {job_id}：{self.reason}")


@dataclass(frozen=True, slots=True)
class Lease:
    """一次执行权（也即「不透明的任务单」）：归属 + 凭证 + 到期时间。

    派发侧只从它取「我该用哪个凭证写结果」；谁领的、到几点失效跟着它走，不另行保存。
    `kind` / `solution_id` 是给恢复器（#83）重建可派发任务单用的最小归属信息，
    本票的派发路径不依赖它们。
    """

    job_id: int
    owner_id: int
    kind: str = ""
    solution_id: int | None = None
    lease_owner: str = ""
    token: str = ""
    expire_at: str = ""


@dataclass
class LeaseGuard:
    """当前执行者的租约 + 「已经失去 / 被请求取消」的标记（跨协程共享同一个对象）。

    为什么要这两个标记：续租失败会抛异常，但异常可能被流水线内部的 `except` 吞掉
    （分章链路会把单章失败记成 partial 继续跑）。标记让 `run_job` 在下一个事件到达时
    也能看见「别再写了」或「该收成已取消了」，而不只依赖异常穿透。
    """

    lease: Lease
    lost: bool = False
    cancel_requested: bool = False


# 当前上下午持有的执行权；没有任务在跑时为 None（续租自动变为无操作）。
_current_guard: ContextVar[LeaseGuard | None] = ContextVar("job_lease_guard", default=None)


def _expire_text(started: datetime, ttl: float) -> str:
    """到期时间文本：与 `core/clock.utc_now_text()` 同一格式（字典序 = 时间序）。"""
    return (started + timedelta(seconds=ttl)).isoformat()


def _new_token() -> str:
    """本轮凭证：32 位十六进制。逐轮随机——凭证相同就等于「同一轮」，判权会失效。"""
    return secrets.token_hex(16)


def claim(
    owner_id: int,
    job_id: int,
    *,
    lease_owner: str = "",
    now: datetime | None = None,
    ttl: float = DEFAULT_LEASE_SECONDS,
) -> Lease | None:
    """抢占一条**排队中**任务：领到返回 `Lease`，没领到（改到 0 行）返回 `None`。

    改到 0 行有两种情况，对调用方是同一件事——**这一行现在不归你**：别人先领走了
    （已变成执行中），或者它已经不是排队中（被取消 / 已经终态）。

    Args:
        owner_id: 任务归属。用户面动作必带；恢复器（#83）从扫描结果里取。
        job_id: 要领的那一条。
        lease_owner: 「谁领的」，留空取 `LEASE_OWNER`（主机/进程）。仅供排查。
        now: 判据用的「现在」，留空取 UTC 当前时刻（注入只为用例能演示时间跨度）。
        ttl: 租约时长（秒）。
    """
    started = now or utc_now()
    token = _new_token()
    expire_at = _expire_text(started, ttl)
    grant = job_state.LeaseGrant(
        owner=lease_owner or LEASE_OWNER,
        token=token,
        expire_at=expire_at,
        now_text=started.isoformat(),
    )
    with session_scope() as session:
        result = job_state.transition(
            job_state.JobEvent.CLAIM,
            job_state.job_scope(owner_id, job_id),
            lease_grant=grant,
            session=session,
        )
        if not result.applied:
            return None
        # 归属信息在同一事务里读回：恢复器（#83）要靠它把行还原成可派发的任务单。
        job = GenerationJobRepository(session).get(owner_id, job_id)
        kind = job.kind if job is not None else ""
        solution_id = job.solution_id if job is not None else None
    logger.info(
        "领到任务执行权",
        extra={
            "event": "job.claimed",
            "job_id": job_id,
            "user_id": owner_id,
            "kind": kind,
            "lease_owner": lease_owner or LEASE_OWNER,
        },
    )
    return Lease(
        job_id=job_id,
        owner_id=owner_id,
        kind=kind,
        solution_id=solution_id,
        lease_owner=lease_owner or LEASE_OWNER,
        token=token,
        expire_at=expire_at,
    )


def claim_once(
    limit: int = 1,
    *,
    lease_owner: str = "",
    now: datetime | None = None,
    ttl: float = DEFAULT_LEASE_SECONDS,
) -> list[Lease]:
    """扫一批「还没有有效租约」的任务并逐条抢占，返回真正领到的那些（不透明任务单）。

    扫描只负责**挑候选**，判权仍由 `claim` 那条条件写完成：候选在两个执行者之间同时
    被看到是常态，改到 0 行的那些直接跳过——不会出现两个人领同一条。

    调用点：生产里是 `services/job_scheduler.py` 的领取周期（只补漏，约 1 秒一轮）；
    用例也直接调它来证明「并发抢占恰好一个赢」。扫描 → 抢占 → 派发的整条路径在
    `job_scheduler.run_once` 一处，本模块不做任何调度。
    """
    started = now or utc_now()
    with session_scope() as session:
        candidates = GenerationJobRepository(session).claimable(
            now_text=started.isoformat(), limit=max(1, int(limit))
        )
    claimed: list[Lease] = []
    for owner_id, job_id in candidates:
        lease = claim(owner_id, job_id, lease_owner=lease_owner, now=started, ttl=ttl)
        if lease is not None:
            claimed.append(lease)
    return claimed


def renew(
    lease: Lease,
    *,
    now: datetime | None = None,
    ttl: float = DEFAULT_LEASE_SECONDS,
) -> Lease:
    """续租：把到期时间往后推，返回**新的** `Lease`（凭证不变）。

    Raises:
        CancelRequestedError: 行处于 `cancelling`——用户已经说要停，这一位执行者要
            在下一个动作点退出并确认取消（不是「失去执行权」）。
        LeaseLostError: 改到 0 行且不是因为取消——状态已不是执行中、或凭证不是这一轮
            的了。调用方据此立刻停下（不重试、不写任何结果）。
    """
    started = now or utc_now()
    expire_at = _expire_text(started, ttl)
    with session_scope() as session:
        repo = GenerationJobRepository(session)
        rows = repo.renew_lease(
            lease.owner_id,
            lease.job_id,
            lease_token=lease.token,
            expire_at=expire_at,
        )
        # 0 行时在同一事务里读回那一行：判断「被取消」还是「被接手」不能靠猜。
        observed = repo.get(lease.owner_id, lease.job_id) if rows == 0 else None
    if rows == 0:
        if observed is not None and observed.status == STATUS_CANCELLING:
            logger.info(
                "续租看到取消标记：本轮执行者应当在下一个动作点确认取消",
                extra={
                    "event": "job.cancel_requested",
                    "job_id": lease.job_id,
                    "user_id": lease.owner_id,
                    "kind": lease.kind,
                    "lease_owner": lease.lease_owner,
                },
            )
            raise CancelRequestedError(lease.job_id, "任务已进入取消中")
        logger.warning(
            "续租未改到任何行：本轮执行权已失效，执行者应当立刻停止",
            extra={
                "event": "job.lease_lost",
                "job_id": lease.job_id,
                "user_id": lease.owner_id,
                "kind": lease.kind,
                "lease_owner": lease.lease_owner,
            },
        )
        raise LeaseLostError(lease.job_id, "续租改到 0 行：本轮凭证已失效")
    return replace(lease, expire_at=expire_at)


@contextmanager
def use_lease(guard: LeaseGuard | None) -> Iterator[LeaseGuard | None]:
    """把当前执行权挂到上下文，退出时还原。

    由 `run_job` 包住整条事件流的消费过程（与 `use_meter` / `use_config` 同一套写法）：
    流水线深处的模型调用点靠它找到「这一轮的凭证」。`finally` 必须还原，否则同一个
    事件循环上先后跑的任务会读到别人的租约。
    """
    token = _current_guard.set(guard)
    try:
        yield guard
    finally:
        _current_guard.reset(token)


def current_lease() -> Lease | None:
    """取当前上下文的执行权；没有任务在跑时返回 `None`。"""
    guard = _current_guard.get()
    return guard.lease if guard is not None else None


def guard_for(runtime: JobRuntime) -> LeaseGuard | None:
    """按运行时的租约三列造一个守卫；这一轮没有租约（不该发生）时返回 `None`。"""
    if not runtime.lease_token:
        return None
    return LeaseGuard(
        Lease(
            job_id=runtime.job_id,
            owner_id=runtime.owner_id,
            kind=runtime.kind,
            solution_id=runtime.solution_id,
            lease_owner=runtime.lease_owner,
            token=runtime.lease_token,
            expire_at=runtime.lease_expire_at,
        )
    )


def renew_before_action(*, now: datetime | None = None, ttl: float = DEFAULT_LEASE_SECONDS) -> None:
    """**动作点续租**：每次模型调用前调一次。

    没有执行权上下文时是**无操作**（对话 / 单测这类不经任务运行时的调用不该被拦）。

    Raises:
        CancelRequestedError: 这一轮已被用户请求取消。抛出去让链路停；守卫标成
            `cancel_requested`，`run_job` 据此在下一个事件到达时确认取消并落终态。
        LeaseLostError: 这一轮的执行权已经失效（被接手）。抛出去让链路停；守卫标成
            `lost`，异常即使被流水线内部吞掉，`run_job` 也能在下一个事件到达时看见。
            **不重试**：失去资格的进程重试一次也不会重新获得资格，只会白烧 token 与额度。
    """
    guard = _current_guard.get()
    if guard is None:
        return
    if guard.lost:
        raise LeaseLostError(guard.lease.job_id, "本轮执行权已失效")
    if guard.cancel_requested:
        raise CancelRequestedError(guard.lease.job_id, "任务已处于取消中")
    try:
        guard.lease = renew(guard.lease, now=now, ttl=ttl)
    except CancelRequestedError:
        guard.cancel_requested = True
        raise
    except LeaseLostError:
        guard.lost = True
        raise


__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "LEASE_OWNER",
    "CancelRequestedError",
    "Lease",
    "LeaseGuard",
    "LeaseLostError",
    "claim",
    "claim_once",
    "current_lease",
    "guard_for",
    "renew",
    "renew_before_action",
    "use_lease",
]
