"""单进程后台调度循环：**三个维护单元的生产调用者**（本票 P0-1）。

## 本模块回答什么问题

`claim_once`（#82）、`recover_once`（#83）、`purge_expired_events`（#86）此前都是
「可直接调用的单元」——写完就被留在原地，只有用例在调。于是运行期有三件事**从来没
发生过**：

1. 退避到期（`next_run_at` 已过）的任务没人领——#88 AC1 在运行期不可达；
2. 拿到租约的执行者崩了、`cancelling` 的执行者卡在模型调用里不肯退出，没人兜底——
   #83 AC3 / #84 AC3 不可达；
3. 过程日志 7 天不清理，`job_events` 只涨不落。

本模块把这三个单元接进 `asyncio` 后台任务：**领 → 收口 → 清理**，各自一个周期。
它只做「补漏」——交互路径不变：新建任务仍是「登记后立刻抢占派发」（
`job_registration.claim_and_dispatch`），延迟不经过任何周期。

## 三个周期（都可配，见 `core/config.py` 的 `job_scheduler_*`）

| 周期 | 调什么 | 默认 | 为什么是这个粒度 |
|---|---|---|---|
| 领取 | `job_claimer.claim_once` + 派发 | 1 秒 | 退避最短 5 秒，1 秒粒度足够；再密只是白扫空表 |
| 收口 | `job_recovery.recover_once` | 30 秒 | 租约 120 秒到期，半分钟内发觉「执行者没了」 |
| 清理 | `job_events.purge_expired_events` | 1 小时 | 保留期是 7 天，没必要更勤 |

## 派发：从**库里的行**重建任务单

被领到的行只有一张旧单的投影（`request_params` / `config_snapshot` / `skill_refs`），
所以派发前要重建一张任务单（`_resume_ticket`）：

- **上游输入重新解析**（`job_inputs.resolve_runtime_inputs`）：`resolved_inputs` 按契约
  不落库（C-R3：它回答的是「那一刻库里是什么」）。重试是一次**新的执行**，按新的
  「那一刻」重新取是正确的口径；取不到就按 `INVALID_REQUEST` 判失败（缺材料重试
  一万次也缺）。
- **配置取当前生效的那一份**（`get_runtime_config()`）：DB 里的 `config_snapshot` 是
  投影，不含量身定做的 `RuntimeConfig` 对象。「每次执行冻结一份快照」这条规则因此
  仍然成立——冻结的是**这一次**执行，而不是上一轮那一份。
- **对话任务判失败**：`chat` 的上下文在 `chat_turns` 里、靠登记时写下的
  `input_turn_id` 定位，这个 id 不落库。拿空历史续跑等于让模型对着半句话作答，
  比中断更糟——如实判「服务重启后无法恢复」。

## 关闭：干净取消，不留悬挂 task

`stop()` 先 `cancel()` 再 `gather()` 等全部退出（`lifespan` 关闭阶段第一步就是它，
先于排空——排空期间不该再有新一轮领取）。周期里抛出的任何异常都只留一条
`scheduler.round_failed` warning 并继续下一轮：一个维护动作写失败不该让整个
循环静默死掉（那正是本模块要消灭的状态）。

## 本模块不做什么

- **不做跨进程协调**：单 worker 是部署硬约束（`_RUNTIMES` / `JobBus` 都在进程内），
  多进程同时跑循环也不会派发同一条（`claim` 是条件写），但运行时登记表会各算各的。
- **不改交互延迟**：登记路径的「立刻抢占派发」原样保留，循环只补漏。

## 事件名为什么是 `scheduler.*` 而不是 `job.*`

`job.*` 那条命名空间是「**某一条任务**的主链路事件」——日志页靠它按任务串起来，
`test_main_chain_logging` 也据此断言每一条都带 `job_id` / `user_id`。循环自己的
起停与「一轮收到了几行」属于**进程级**事实，带上 `job.` 前缀会把那条不变量搅浑。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from backend.core.config import Settings, get_settings
from backend.core.engine import session_scope
from backend.core.errors import AppError, ErrorCode
from backend.core.runtime_config import get_runtime_config
from backend.repositories.generation_job_repo import GenerationJobRepository
from backend.services import job_claimer, job_events, job_inputs, job_recovery, job_state
from backend.services.job_registration import dispatch_registered
from backend.services.job_ticket import (
    RegisteredJobTicket,
    build_job_ticket,
    with_registered_identity,
)

logger = logging.getLogger(__name__)

# 能从库里重建输入的任务种类：`job_inputs.DOCUMENT_JOB_KINDS`（八种文档类任务）。
# `chat` 不在其中，理由见模块 docstring 的「派发」一节。
RESUMABLE_KINDS: frozenset[str] = frozenset(job_inputs.DOCUMENT_JOB_KINDS)

MESSAGE_UNRESUMABLE_KIND = "服务重启后这条任务无法恢复（对话上下文不落库），已中断"
MESSAGE_TICKET_FAILED = "重新组装任务单失败，任务已中断"
MESSAGE_JOB_ROW_GONE = "任务行已不存在，无法派发"


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    """一轮领取里某一条任务的去向（派发成功 / 重建失败后已判失败）。"""

    job_id: int
    owner_id: int
    kind: str
    dispatched: bool
    detail: str = ""


def _load_json(text: str, fallback: Any, *, job_id: int) -> Any:
    """把库里的 JSON 文本解回来；坏值退回 `fallback`（脏数据不该卡住调度）。"""
    try:
        return json.loads(text) if text else fallback
    except ValueError:
        logger.warning(
            "任务行的 JSON 列解不开，本次按空值重建任务单",
            extra={"event": "scheduler.bad_json", "job_id": job_id},
        )
        return fallback


def _resume_ticket(lease: job_claimer.Lease) -> RegisteredJobTicket:
    """按**库里那一行**重建一张任务单（同步：调用方放进线程池执行）。

    重建的是「同一件事的下一次执行」，因此上游输入重新解析、配置取当前生效值
    （理由见模块 docstring）。

    Raises:
        AppError: 行已不存在 / 种类不可续 / 上游材料取不到。调用方据此把这一行判失败。
    """
    with session_scope() as session:
        row = GenerationJobRepository(session).get(lease.owner_id, lease.job_id)
        if row is None:
            raise AppError(ErrorCode.INTERNAL_ERROR, MESSAGE_JOB_ROW_GONE)
        kind = row.kind
        solution_id = row.solution_id
        doc_kind = row.doc_kind or ""
        params = _load_json(row.request_params, {}, job_id=lease.job_id)
        if not isinstance(params, dict):
            params = {}

    if kind not in RESUMABLE_KINDS:
        raise AppError(ErrorCode.INTERNAL_ERROR, MESSAGE_UNRESUMABLE_KIND)

    resolved = job_inputs.resolve_runtime_inputs(
        kind, lease.owner_id, solution_id, doc_kind or None
    )
    draft = build_job_ticket(
        owner_id=lease.owner_id,
        solution_id=solution_id,
        kind=kind,
        request=params,
        config=get_runtime_config(),
        resolved_inputs=resolved,
    )
    return with_registered_identity(draft, job_id=lease.job_id, input_turn_id=0)


def _fail_lease(lease: job_claimer.Lease, *, error_code: str, error_message: str) -> None:
    """把一条已经领到、但**无法派发**的任务收成失败（同步：放进线程池执行）。

    为什么用状态机而不是直接写库：清租约、记账本、留丢弃日志都只有那一处实现
    （`job_state.transition`），走旁路就会漏掉其中一件。载荷给空：这一行还没开始
    执行，没有「已生成的内容」可落。
    """
    job_state.transition(
        job_state.JobEvent.FAIL,
        job_state.job_scope(lease.owner_id, lease.job_id),
        payload=job_state.FinishPayload(),
        error_code=error_code,
        error_message=error_message,
        lease_token=lease.token,
    )


class JobScheduler:
    """三个周期任务的所有者：`start()` 起、`stop()` 停，中间只做补漏。

    用例可以直接调 `run_once()` / `recover_expired()` / `purge_events()` ——
    它们是「一轮」，不依赖周期任务在跑，也不需要等真实的秒数。
    """

    def __init__(
        self,
        *,
        claim_interval: float,
        recovery_interval: float,
        purge_interval: float,
        claim_batch: int = 5,
    ) -> None:
        self._claim_interval = claim_interval
        self._recovery_interval = recovery_interval
        self._purge_interval = purge_interval
        self._claim_batch = claim_batch
        self._tasks: list[asyncio.Task[None]] = []

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        """是否还有周期任务在跑（`stop()` 之后恒为 False）。"""
        return any(not task.done() for task in self._tasks)

    @property
    def tasks(self) -> tuple[asyncio.Task[None], ...]:
        """当前持有的周期任务（用例据此断言「关闭后没有悬挂 task」）。"""
        return tuple(self._tasks)

    def start(self) -> None:
        """在**当前事件循环**里起三个周期任务（幂等：重复 start 会报错而不是起两份）。"""
        if self._tasks:
            raise RuntimeError("调度循环已经在跑：先 stop() 再 start()")
        loop = asyncio.get_running_loop()
        self._tasks = [
            loop.create_task(self._claim_loop(), name="job-scheduler-claim"),
            loop.create_task(self._recovery_loop(), name="job-scheduler-recovery"),
            loop.create_task(self._purge_loop(), name="job-scheduler-purge"),
        ]
        logger.info(
            "调度循环已启动",
            extra={
                "event": "scheduler.started",
                "claim_interval": self._claim_interval,
                "recovery_interval": self._recovery_interval,
                "purge_interval": self._purge_interval,
                "claim_batch": self._claim_batch,
            },
        )

    async def stop(self) -> None:
        """取消并**等**三个周期任务真的退出（不留悬挂 task）。"""
        tasks, self._tasks = self._tasks, []
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        # `return_exceptions=True`：被取消的任务会抛 `CancelledError`，那不是失败。
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info(
            "调度循环已退出",
            extra={"event": "scheduler.stopped", "tasks": len(tasks)},
        )

    # ------------------------------------------------------------------
    # 三个周期
    # ------------------------------------------------------------------

    async def _claim_loop(self) -> None:
        while True:
            await asyncio.sleep(self._claim_interval)
            await self._guarded("claim", self.run_once)

    async def _recovery_loop(self) -> None:
        while True:
            await asyncio.sleep(self._recovery_interval)
            await self._guarded("recovery", self.recover_expired)

    async def _purge_loop(self) -> None:
        while True:
            await asyncio.sleep(self._purge_interval)
            await self._guarded("purge", self.purge_events)

    @staticmethod
    async def _guarded(
        step: str, action: Callable[[], Awaitable[Any]]
    ) -> None:
        """跑一轮；任何异常都只留痕、不打断循环（取消信号照旧向上抛）。"""
        try:
            await action()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 一轮写失败不该让循环静默死掉
            logger.warning(
                "调度循环的一轮失败，下一轮继续",
                exc_info=True,
                extra={"event": "scheduler.round_failed", "step": step},
            )

    # ------------------------------------------------------------------
    # 一轮（用例直接调用这三条，不必等真实周期）
    # ------------------------------------------------------------------

    async def run_once(self) -> list[DispatchOutcome]:
        """领一批**到期**的排队任务并派发，返回各自的去向。

        扫描 / 抢占 / 重建任务单都是同步的数据库工作，放进线程池：它们会借连接、
        可能等锁，压在事件循环上就会拖慢所有在途任务的 SSE。
        """
        leases = await asyncio.to_thread(job_claimer.claim_once, self._claim_batch)
        outcomes = [await self._dispatch(lease) for lease in leases]
        if outcomes:
            logger.info(
                "调度循环领取了一轮任务",
                extra={
                    "event": "scheduler.claimed",
                    "claimed": len(outcomes),
                    "dispatched": sum(1 for item in outcomes if item.dispatched),
                },
            )
        return outcomes

    async def _dispatch(self, lease: job_claimer.Lease) -> DispatchOutcome:
        """把一条领到的任务派发出去；重建任务单失败就地判失败（不静默丢行）。"""
        try:
            ticket = await asyncio.to_thread(_resume_ticket, lease)
        except AppError as exc:
            await asyncio.to_thread(
                _fail_lease, lease, error_code=exc.code.value, error_message=exc.message
            )
            logger.warning(
                "调度循环领到的任务无法派发，已按失败收口",
                extra={
                    "event": "scheduler.dispatch_failed",
                    "job_id": lease.job_id,
                    "user_id": lease.owner_id,
                    "kind": lease.kind,
                    "error_code": exc.code.value,
                },
            )
            return DispatchOutcome(
                job_id=lease.job_id,
                owner_id=lease.owner_id,
                kind=lease.kind,
                dispatched=False,
                detail=exc.message,
            )
        except Exception:  # noqa: BLE001 - 读库 / 组装出意外同样要收口，不能留尸体
            await asyncio.to_thread(
                _fail_lease,
                lease,
                error_code=ErrorCode.INTERNAL_ERROR.value,
                error_message=MESSAGE_TICKET_FAILED,
            )
            logger.warning(
                "调度循环重建任务单出意外，已按失败收口",
                exc_info=True,
                extra={
                    "event": "scheduler.dispatch_failed",
                    "job_id": lease.job_id,
                    "user_id": lease.owner_id,
                    "kind": lease.kind,
                },
            )
            return DispatchOutcome(
                job_id=lease.job_id,
                owner_id=lease.owner_id,
                kind=lease.kind,
                dispatched=False,
                detail=MESSAGE_TICKET_FAILED,
            )

        dispatch_registered(ticket, lease)
        return DispatchOutcome(
            job_id=lease.job_id,
            owner_id=lease.owner_id,
            kind=lease.kind,
            dispatched=True,
        )

    async def recover_expired(self) -> job_recovery.RecoveryReport:
        """收口一批过期租约 / 取消中的任务（一轮）。"""
        return await asyncio.to_thread(job_recovery.recover_once)

    async def purge_events(self) -> int:
        """清理一批超过保留期的过程日志，返回删除行数（一轮）。"""
        return await asyncio.to_thread(job_events.purge_expired_events)


# 当前进程里在跑的那个循环。它是**观测句柄**：用例据此断言「生命周期结束后
# 循环确实退出了」，运维接口将来也用它回答「循环在不在」。为 None 表示没起。
_active: JobScheduler | None = None


def active() -> JobScheduler | None:
    """当前在跑的调度循环；没有起（配置关闭 / 已停止）时返回 None。"""
    return _active


def start(settings: Settings | None = None) -> JobScheduler | None:
    """按配置起循环；`job_scheduler_enabled` 为 false 时返回 None。

    必须在**运行中的事件循环**里调用（`start()` 内部要 `create_task`）。
    """
    global _active
    config = settings or get_settings()
    if not config.job_scheduler_enabled:
        logger.info(
            "调度循环按配置未启用",
            extra={"event": "scheduler.disabled"},
        )
        return None
    if _active is not None:
        raise RuntimeError("本进程已经有一个调度循环在跑")
    scheduler = JobScheduler(
        claim_interval=config.job_scheduler_claim_interval,
        recovery_interval=config.job_scheduler_recovery_interval,
        purge_interval=config.job_scheduler_purge_interval,
        claim_batch=config.job_scheduler_claim_batch,
    )
    scheduler.start()
    _active = scheduler
    return scheduler


async def stop(scheduler: JobScheduler | None = None) -> None:
    """停掉给定的（或当前进程里的那个）循环，并清空观测句柄。"""
    global _active
    target = scheduler if scheduler is not None else _active
    if target is not None:
        await target.stop()
    if _active is target:
        _active = None
