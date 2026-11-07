"""全链路用例的共享样板：只跨「造单 → 登记 → 派发 → 订阅」主接缝（票 #38 / #39）。

假环境在**造单时**传入，生产真值 / 测试假值的分工因此只有一处：
`build_job_ticket` 的 `event_stream_source`（生成事件流来源）与 `config`
（配置 / 供应商快照）两个入参。测试想造出错、半截、分章乱序、预算超顶、
配置中途不改这些分支时，给这两个入参放假值即可，不再临时替换
`job_service` 的内部零件，也不开测试专用私有缝。

#39 归位后的用例一律跨公开动作断言：
- `scripted_stream` 把「过程字 / 权威正文 / 阶段输出」当假事件流在造单时传入；
- `first_snapshot_event` 读 DB 兜底那一路的首份快照，`run_ticket_snapshot` 读在途
  内存那一路的快照，两者都走 `open_stream`，取代直调内部快照构造函数；
- `run_ticket_events` 让「登记 → 派发 → 订阅到结束」只写一行，
  `run_ticket_in_flight` 在任务仍在跑时插入公开动作（再登记 / 删方案 / 取消）。

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
    resolved_inputs: dict[str, Any] | None = None,
) -> RegisteredJobTicket:
    """造单 → 登记：假环境在造单时传入，返回唯一权威的单②。

    `request` 只放「调用方发了什么」（`POST /api/jobs` 的请求体），`resolved_inputs`
    放**服务端解析出的上游输入**（生产里由路由调 `job_inputs` 得来，票 #54）。
    两者分开传，正是为了让伪造的单与生产同形：请求体里没有上游正文。
    """
    draft = build_job_ticket(
        owner_id=owner_id,
        solution_id=solution_id,
        kind=kind,
        request=request,
        event_stream_source=event_stream_source,
        config=config,
        resolved_inputs=resolved_inputs,
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
    resolved_inputs: dict[str, Any] | None = None,
    before_open: Callable[[int], Any] | None = None,
    on_event: Callable[[StreamEvent], Any] | None = None,
) -> tuple[RegisteredJobTicket, list[StreamEvent]]:
    """造单 → 登记 → 派发 → 一路订阅到结束，返回 (单②, 全部事件)。

    `resolved_inputs`：服务端解析出的上游输入（生产由路由在建任务时给出）。
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
        resolved_inputs=resolved_inputs,
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
    resolved_inputs: dict[str, Any] | None = None,
) -> tuple[int, list[StreamEvent]]:
    """兼容既有 `_run_job` 的返回形状 (job_id, 全部事件)。"""
    registered, events = run_ticket(
        owner_id=owner_id,
        solution_id=solution_id,
        kind=kind,
        request=request,
        event_stream_source=event_stream_source,
        config=config,
        resolved_inputs=resolved_inputs,
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
    resolved_inputs: dict[str, Any] | None = None,
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
        resolved_inputs=resolved_inputs,
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


def scripted_stream(
    *events: StreamEvent, reply: str = "", done_data: dict[str, Any] | None = None
) -> EventStreamSource:
    """造单时传入的假事件流：按序吐出给定事件，末尾补一条 `done`。

    假环境是「生成事件流来源」这一项公开入参，不替换任何内部零件；`done.reply`
    是收尾写文档 / 写轮次用的权威正文，默认空串（不写）。`done_data` 让 `done`
    事件还能带上待确认清单 / 部分完成等既有契约字段。
    """
    data = dict(done_data or {})
    data.setdefault("reply", reply)

    async def source(kind: str, request: Any) -> Any:
        for event in events:
            yield event
        # 调用方已经把 `done` 排进事件序列时不再补第二条：否则后一条空 `done`
        # 会把前一条的 `clarification_asks` / `pending_items` 等字段抹掉。
        if not events or events[-1].type != "done":
            yield StreamEvent(type="done", content="", data=data)

    return source


def run_ticket_in_flight(
    owner_id: int,
    solution_id: int | None,
    kind: str,
    request: dict[str, Any],
    while_running: Callable[[], Any] | None = None,
    *events: StreamEvent,
    reply: str = "",
    config: Any | None = None,
    resolved_inputs: dict[str, Any] | None = None,
) -> tuple[RegisteredJobTicket, list[StreamEvent]]:
    """派发一支在途任务，在它仍在跑时执行 `while_running`，再放开流收尾。

    「单写者」「删方案时取消在途」这类语义要在一支**真的还没结束**的任务上做动作，
    旧写法是把裸运行时塞进内部登记表。归位后：造单时传入会卡住的假事件流，
    派发由公开动作完成，`while_running` 里执行的仍是公开动作（再登记 / 删方案 /
    取消），最后放开闸门订阅到结束。返回单②与全部事件。
    """
    produced = asyncio.Event()
    release = asyncio.Event()

    async def stream(kind: str, request: Any) -> Any:
        for event in events:
            yield event
        produced.set()
        await release.wait()
        yield StreamEvent(type="done", content="", data={"reply": reply})

    async def scenario() -> tuple[RegisteredJobTicket, list[StreamEvent]]:
        return await run_ticket_async(
            owner_id=owner_id,
            solution_id=solution_id,
            kind=kind,
            request=request,
            event_stream_source=stream,
            config=config,
            resolved_inputs=resolved_inputs,
            before_open=act_while_in_flight,
        )

    async def act_while_in_flight(job_id: int) -> None:
        await asyncio.wait_for(produced.wait(), timeout=5)
        if while_running is not None:
            outcome = while_running()
            if inspect.isawaitable(outcome):
                await outcome
        release.set()

    return asyncio.run(scenario())


def run_ticket_events(
    owner_id: int,
    solution_id: int | None,
    kind: str,
    request: dict[str, Any],
    *events: StreamEvent,
    reply: str = "",
    done_data: dict[str, Any] | None = None,
    resolved_inputs: dict[str, Any] | None = None,
) -> tuple[RegisteredJobTicket, list[StreamEvent]]:
    """登记 → 派发 → 订阅到结束：假事件流在造单时传入，返回单②与全部事件。"""
    return run_ticket(
        owner_id=owner_id,
        solution_id=solution_id,
        kind=kind,
        request=request,
        event_stream_source=scripted_stream(*events, reply=reply, done_data=done_data),
        resolved_inputs=resolved_inputs,
    )


def run_ticket_snapshot(
    owner_id: int,
    solution_id: int | None,
    kind: str,
    request: dict[str, Any],
    *events: StreamEvent,
    reply: str = "",
    done_data: dict[str, Any] | None = None,
    resolved_inputs: dict[str, Any] | None = None,
) -> tuple[RegisteredJobTicket, StreamEvent, list[StreamEvent]]:
    """跨主接缝读「任务仍在途时」的内存快照，返回 (单②, 首份快照, 全部事件)。

    假事件流在造单时传入：先吐 `events`（此时还没有订阅者，事件只进运行时，
    正是「退订后重连要补全」的场景），再等一道闸门。派发后订阅，首事件必然是
    内存运行时那一支的 `snapshot`；收到它的瞬间放开闸门，任务继续跑到收尾。
    于是「事件 → 运行时状态」这条内部映射，全部改由公开可观察的快照与库行断言。
    """
    produced = asyncio.Event()
    release = asyncio.Event()
    done = dict(done_data or {})
    done.setdefault("reply", reply)

    async def stream(kind: str, request: Any) -> Any:
        for event in events:
            yield event
        produced.set()
        await release.wait()
        yield StreamEvent(type="done", content="", data=done)

    async def wait_until_produced(job_id: int) -> None:
        await asyncio.wait_for(produced.wait(), timeout=5)

    def unlock_on_first_event(event: StreamEvent) -> None:
        if event.type == "snapshot":
            release.set()

    async def scenario() -> tuple[RegisteredJobTicket, list[StreamEvent]]:
        return await run_ticket_async(
            owner_id=owner_id,
            solution_id=solution_id,
            kind=kind,
            request=request,
            event_stream_source=stream,
            resolved_inputs=resolved_inputs,
            before_open=wait_until_produced,
            on_event=unlock_on_first_event,
        )

    registered, collected = asyncio.run(scenario())
    assert collected and collected[0].type == "snapshot"
    return registered, collected[0], collected


def first_snapshot_event(owner_id: int, job_id: int) -> StreamEvent:
    """订阅一个已登记但未派发的任务，返回 DB 兜底那一路的首份 `snapshot`。

    登记表里没有这支运行时时 `open_stream` 走 DB 兜底；这是进程重启、任务完成过久
    之后重连的真实路径。只断到「首事件」就退订，不把任务推进到终态。
    """

    async def scenario() -> StreamEvent:
        async for event in job_service.open_stream(owner_id, job_id):
            return event
        raise AssertionError("订阅应当至少收到一份快照")

    return asyncio.run(scenario())


__all__ = [
    "build_and_register",
    "chat_request_from_ticket",
    "fake_registered_ticket",
    "first_snapshot_event",
    "run_ticket",
    "run_ticket_async",
    "run_ticket_events",
    "run_ticket_in_flight",
    "run_ticket_snapshot",
    "run_ticket_to_completion",
    "scripted_stream",
]
