"""生成任务一侧：回答排空的三个问题（票 #37 排空职责归位）。

规格把「等待在途任务」交回生成任务一侧；排空是包外使用方，只认
`InFlightHandle` 这种只带 `job_id` 的抽象句柄，一个运行时内部字段都不碰。

三个问题与本模块的公开动作一一对应：

1. **还有哪些在跑** → `in_flight()`：返回抽象句柄的稳定快照；
2. **要不要等** → `wait_for_in_flight()`：只等本事件循环上的任务，超时正常返回；
3. **超时取消哪几支** → 同上：从「排空开始那一刻的快照」里挑还在跑的，交
   `job_service.cancel_in_flight()` 落 `CANCELLED` 终态（原因文案由调用方给）。

运行时对象、`asyncio.Task` 与 `status` 字段只在**本模块**内部读，不越过包边界。
`job_service.running_runtimes()` / `cancel_in_flight()` 仍是既有公开动作，
这里没有新增业务能力，只是把「谁负责等」从排空挪回生成任务一侧。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Sequence

from backend.repositories.generation_job_repo import STATUS_RUNNING
from backend.services import job_service

__all__ = ["InFlightHandle", "WaitOutcome", "in_flight", "wait_for_in_flight"]


@dataclass(frozen=True, slots=True)
class InFlightHandle:
    """在途任务的抽象句柄：排空只认 `job_id`。

    运行时对象、任务对象与状态字段是生成任务一侧的内部实现，不递出包边界；
    内部字段改名或换存储结构时，改动面只有本模块。
    """

    job_id: int


@dataclass(frozen=True, slots=True)
class WaitOutcome:
    """一次等待的结论（排空据此组装自己的汇总，数字口径由这里保证）。"""

    in_flight: int
    cancelled: int
    hit_deadline: bool

    @property
    def finished(self) -> int:
        """窗口内自然结束的任务数。`cancelled ≤ in_flight` 由构造保证。"""
        return self.in_flight - self.cancelled


def in_flight() -> list[InFlightHandle]:
    """还有哪些在跑：返回抽象句柄的稳定快照，不把运行时对象递出包边界。"""
    return [InFlightHandle(runtime.job_id) for runtime in job_service.running_runtimes()]


def _runtimes_for(snapshot: Sequence[InFlightHandle]) -> list[Any]:
    """按句柄从登记表还原运行时对象（只在生成任务一侧做）。

    只按 `job_id` 认人：排空交进来的句柄可以是它自己保存的抽象令牌，
    这里不需要、也不允许它携带任何运行时字段。
    """
    wanted = {handle.job_id for handle in snapshot}
    return [
        runtime
        for runtime in job_service.running_runtimes()
        if runtime.job_id in wanted
    ]


async def wait_for_in_flight(
    snapshot: Sequence[InFlightHandle], *, timeout: float, reason: str
) -> WaitOutcome:
    """要不要等 / 超时取消哪几支：一个动作回答完，返回汇总数字。

    顺序与既有排空语义逐项不变：只等**本事件循环**上的任务（跨循环任务永远不会被
    本循环调度，等它们只是白等满窗口），`asyncio.wait` 超时后正常返回；超时者从
    「排空开始那一刻在途的任务」里挑，由 `cancel_in_flight` 先发错误事件、再收尾
    落终态、最后给订阅者放哨兵。
    """
    pending = _runtimes_for(snapshot)
    loop = asyncio.get_running_loop()
    tasks = {
        runtime.task
        for runtime in pending
        if runtime.task is not None
        and not runtime.task.done()
        and runtime.task.get_loop() is loop
    }
    if tasks:
        # `asyncio.wait` 只等这批任务，且超时后正常返回——超时那一半不会被当成
        # 异常（`gather` 会抛，而我们要的恰恰是「没跑完的先记下来」）。
        await asyncio.wait(tasks, timeout=timeout)

    # 只从「排空开始那一刻在途的任务」里挑超时的：这样 `cancelled ≤ in_flight`
    # 由构造保证，汇总里的三个数字不可能互相对不上。
    victims = [runtime for runtime in pending if runtime.status == STATUS_RUNNING]
    cancelled = job_service.cancel_in_flight(reason, runtimes=victims)
    return WaitOutcome(
        in_flight=len(snapshot),
        cancelled=cancelled,
        hit_deadline=bool(victims),
    )
