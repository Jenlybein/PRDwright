"""全链路用例的共享样板：只跨「造单 → 登记 → 派发 → 订阅」主接缝（票 #38）。

假环境在**造单时**传入，生产真值 / 测试假值的分工因此只有一处：
`build_job_ticket` 的 `event_stream_source`（生成事件流来源）与 `config`
（配置 / 供应商快照）两个入参。测试想造出错、半截、分章乱序、预算超顶、
配置中途不改这些分支时，给这两个入参放假值即可，不再临时替换
`job_service` 的内部零件，也不开测试专用私有缝。

离线零 token 沿用仓库既有的 `tests/fakes.py::ScriptedLLM`：模型边界的换实例
是既有假环境形态，不属于「替换内部零件」。
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Callable

from backend.core.contracts import StreamEvent
from backend.services import job_service
from backend.services.job_ticket import (
    RegisteredJobTicket,
    build_job_ticket,
    register_job_ticket,
    start_registered_job,
    with_registered_identity,
)

# 造单时传入的假环境类型：事件流来源（异步生成器函数）与配置快照。
EventStreamSource = Callable[..., Any]


def build_and_register(
    *,
    owner_id: int,
    solution_id: int | None,
    kind: str,
    request: dict[str, Any],
    event_stream_source: EventStreamSource | None = None,
    config: Any | None = None,
) -> RegisteredJobTicket:
    """造单 → 登记：假环境在造单时传入，返回唯一权威的单②。"""
    draft = build_job_ticket(
        owner_id=owner_id,
        solution_id=solution_id,
        kind=kind,
        request=request,
        event_stream_source=event_stream_source,
        config=config,
    )
    return register_job_ticket(draft)


async def _maybe_await(outcome: Any) -> None:
    """钩子既可以是同步函数也可以是协程函数，两种都支持。"""
    if inspect.isawaitable(outcome):
        await outcome


async def run_ticket_async(
    *,
    owner_id: int,
    solution_id: int | None,
    kind: str,
    request: dict[str, Any],
    event_stream_source: EventStreamSource | None = None,
    config: Any | None = None,
    before_open: Callable[[int], Any] | None = None,
    on_event: Callable[[StreamEvent], Any] | None = None,
) -> tuple[RegisteredJobTicket, list[StreamEvent]]:
    """造单 → 登记 → 派发 → 一路订阅到结束，返回 (单②, 全部事件)。

    `before_open`：派发之后、订阅之前插入的动作（如「等第一段落库」「进 request
    上下文」），拿到的是公开可观察的 `job_id`。
    `on_event`：每收到一个事件回调一次（如「收到第一段增量就按停止」）。
    两个钩子都只跨公开接缝，不接触内部摆位。
    """
    registered = build_and_register(
        owner_id=owner_id,
        solution_id=solution_id,
        kind=kind,
        request=request,
        event_stream_source=event_stream_source,
        config=config,
    )
    start_registered_job(registered)
    if before_open is not None:
        await _maybe_await(before_open(registered.job_id))
    events: list[StreamEvent] = []
    async for event in job_service.open_stream(owner_id, registered.job_id):
        events.append(event)
        if on_event is not None:
            await _maybe_await(on_event(event))
    return registered, events


def run_ticket(**kwargs: Any) -> tuple[RegisteredJobTicket, list[StreamEvent]]:
    """同步形态：在新事件循环里跑完主接缝（大多数用例用这个）。"""
    return asyncio.run(run_ticket_async(**kwargs))


def run_ticket_to_completion(
    owner_id: int,
    solution_id: int | None,
    kind: str,
    request: dict[str, Any],
    *,
    event_stream_source: EventStreamSource | None = None,
    config: Any | None = None,
) -> tuple[int, list[StreamEvent]]:
    """兼容既有 `_run_job` 的返回形状 (job_id, 全部事件)。"""
    registered, events = run_ticket(
        owner_id=owner_id,
        solution_id=solution_id,
        kind=kind,
        request=request,
        event_stream_source=event_stream_source,
        config=config,
    )
    return registered.job_id, events


def fake_registered_ticket(
    *,
    owner_id: int,
    solution_id: int | None,
    kind: str,
    request: dict[str, Any],
    job_id: int = 999,
    event_stream_source: EventStreamSource | None = None,
    config: Any | None = None,
) -> RegisteredJobTicket:
    """走公开正门造假单②：造单不登记，用 `with_registered_identity` 补登记身份。

    只派发不登记的用例（例如假 id 999）归到「造单 + 派发」：造单本来就是
    公开正门、允许造假单，无需往内部登记表里塞任何东西。
    """
    draft = build_job_ticket(
        owner_id=owner_id,
        solution_id=solution_id,
        kind=kind,
        request=request,
        event_stream_source=event_stream_source,
        config=config,
    )
    return with_registered_identity(draft, job_id=job_id, input_turn_id=0)


def chat_request_from_ticket(ticket: RegisteredJobTicket) -> dict[str, Any]:
    """把单②明写的四个 chat 派生字段读成 dict，供既有断言形状使用。

    这些字段是单②上的不可变数据，不是从运行时内部状态里翻出来的。
    """
    return {
        "input_turn_id": ticket.input_turn_id,
        "job_id": ticket.job_id,
        "owner_id": ticket.owner_id,
        "solution_id": ticket.solution_id,
    }


__all__ = [
    "build_and_register",
    "chat_request_from_ticket",
    "fake_registered_ticket",
    "run_ticket",
    "run_ticket_async",
    "run_ticket_to_completion",
]
