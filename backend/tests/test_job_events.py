"""生成过程事件表 + 游标订阅（票 #86）。

## 接缝

全部断言只跨**公开可观察的边界**（见 `test_no_internal_positioning.py`）：

1. **主接缝**：造单 → 登记 → 派发 → `job_service.open_stream`（`ticket_support`）；
2. **事件表读接缝**：`JobEventRepository.list_after` / `max_id` ——它是「按游标读」的
   唯一读入口，与 `open_stream` 用的是同一个仓储方法（不是测试专用旁路）；
3. **HTTP 接缝**：`GET /api/jobs/{id}/stream?cursor=...`（前端续读真正打的那条路径）。

## 口径（票面五条 AC）

- **AC1 断线重连**：客户端带着上一次收到的游标重订阅，服务端**只发游标之后**的事件；
  两次订阅合起来的事件游标必须**连续**（不丢一条），且每条只出现一次（不重）。
- **AC2 多订阅者**：两个读者各持各的游标，各自把同一任务读完整，互不扣减。
- **AC3 通知只是加速器**：把 `job_bus.publish`（进程内唤醒）整个掐掉之后，订阅仍能
  按轮询推进——数据永远来自表，不来自通知。
- **AC4 冷读者**：不带游标的新读者**先拿快照**，随后只追快照游标之后的事件，
  **不回放**已有历史（否则一次刷新会把整段生成过程再放一遍）。
- **AC5 清理**：终态任务的事件保留 7 天后可清理；清理**只删过程日志**，权威正文
  （`generation_jobs.draft_content` / 文档版本）一个字都不动。
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
from conftest import job_create, job_get, register, users
from dbsupport import connect

from backend.core.clock import utc_now
from backend.core.contracts import StreamEvent
from backend.core.engine import session_scope
from backend.repositories.job_event_repo import JobEventRepository
from backend.services import job_events, job_runtime, job_service
from backend.services.job_bus import job_bus
from backend.services.job_ticket import start_registered_job
from ticket_support import build_and_register, run_ticket_events, scripted_stream

# 一段有「阶段 → 增量 → 定稿」的最小生成过程：三类事件都要进表，
# 而且 chunk 与 part 的正文要能拼回 done.reply（「不缺字」的判据）。
_SCRIPT = (
    StreamEvent(type="stage", content="", data={"stage": "writing"}),
    StreamEvent(type="chunk", content="甲"),
    StreamEvent(type="part", content="", data={"text": "甲"}),
    StreamEvent(type="chunk", content="乙"),
    StreamEvent(type="part", content="", data={"text": "乙"}),
)
_REPLY = "甲乙"


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------


def _alice_id() -> int:
    user = users.get_by_username("alice")
    assert user is not None
    return int(user["id"])


@pytest.fixture
def alice(client):
    """注册 alice 并返回 id（HTTP 接缝与 job_service 接缝共用同一份库）。"""
    register(client)
    return _alice_id()


@pytest.fixture(autouse=True)
def clean_job_state():
    """进程内运行时是单例：用例之间必须清干净（与 `test_generation_job` 同一口径）。"""
    job_runtime._RUNTIMES.clear()  # noqa: SLF001
    job_runtime._TASKS.clear()  # noqa: SLF001
    job_bus._subscribers.clear()  # noqa: SLF001
    yield
    job_runtime._RUNTIMES.clear()  # noqa: SLF001
    job_runtime._TASKS.clear()  # noqa: SLF001
    job_bus._subscribers.clear()  # noqa: SLF001


@pytest.fixture
def per_event_flush(monkeypatch):
    """把合并写窗口调到 0：每个事件都当场落一行。

    真实节奏是「≤300ms 或增量 >4KB」，用例里若照它走，短假流的事件会全部堆到收尾
    那一批，就测不到「流中间断线、按游标接着读」这件事。窗口本身是既有实现，
    这里只是把它调快——写入口一个都没替换。
    """
    monkeypatch.setattr(
        "backend.services.job_runtime_events._FLUSH_INTERVAL_SECONDS", 0.0
    )
    return 0.0


def _cursor_of(event: StreamEvent) -> int:
    raw = event.event_id
    assert isinstance(raw, int) and raw >= 0, f"事件 {event.type} 缺游标：{event.data}"
    return raw


def _event_rows(job_id: int, after: int = 0) -> list[dict]:
    with session_scope() as session:
        return JobEventRepository(session).list_after(job_id, after, limit=1000)


def _append_rows(job_id: int, events: list[StreamEvent]) -> int:
    """按事件形状追加几行（清理用例造历史用；走真实仓储而不是裸 SQL）。"""
    rows = [
        {"kind": event.type, "payload": job_events.payload_of(event)} for event in events
    ]
    with session_scope() as session:
        return JobEventRepository(session).append(job_id, rows)


def _collect(owner_id: int, job_id: int, *, cursor: int | None = None) -> list[StreamEvent]:
    """把一次订阅读到底（只用于「任务已结束」的冷读者场景）。"""

    async def scenario() -> list[StreamEvent]:
        return [event async for event in job_service.open_stream(owner_id, job_id, cursor=cursor)]

    return asyncio.run(scenario())


def _event_types(events: list[StreamEvent]) -> list[str]:
    return [event.type for event in events]


def _text_of(events: list[StreamEvent], kind: str) -> str:
    return "".join(
        (event.data.get("text") or "") if kind == "part" else event.content
        for event in events
        if event.type == kind
    )


# ---------------------------------------------------------------------------
# AC1：断线后按游标重连，不重复、不缺条
# ---------------------------------------------------------------------------


def test_ac1_reconnect_from_cursor_resumes_exactly_after_it(alice, per_event_flush):
    """中途断线 → 带游标重连：只发该游标之后的事件，两次合起来一条不缺、一条不重。

    这是本票的核心行为：游标是客户端手里**已经收到的那一条**的 id，服务端从
    `id > cursor` 继续发。判据用「游标序列连续」——它同时钉住「不重」与「不缺」：
    每加 1 才说明表里每条过程日志都被恰好读了一次。
    """

    async def scenario() -> tuple[list[StreamEvent], int, list[StreamEvent]]:
        registered = build_and_register(
            owner_id=alice,
            solution_id=None,
            kind="prd",
            request={},
            event_stream_source=scripted_stream(*_SCRIPT, reply=_REPLY),
        )
        start_registered_job(registered)

        first: list[StreamEvent] = []
        async for event in job_service.open_stream(alice, registered.job_id):
            first.append(event)
            # 收到第一个增量就「断线」：这正是刷新 / 网络抖动的时刻
            if event.type == "chunk":
                break
        resume_cursor = _cursor_of(first[-1])
        second = [
            event
            async for event in job_service.open_stream(
                alice, registered.job_id, cursor=resume_cursor
            )
        ]
        return first, resume_cursor, second

    first, resume_cursor, second = asyncio.run(scenario())

    assert first[0].type == "snapshot", "新读者（不带游标）必须先拿快照"
    # 重连那一路**不重发快照**：客户端手里已经有游标之前的内容
    assert "snapshot" not in _event_types(second), "带游标续读不该再发快照"

    delivered = first + second
    cursors = [_cursor_of(event) for event in delivered]
    assert cursors == list(range(cursors[0], cursors[0] + len(cursors))), (
        f"两次订阅的事件游标必须连续（不重不缺）：{cursors}"
    )
    # 续读的第一条正是断线那条的下一条
    assert _cursor_of(second[0]) == resume_cursor + 1
    # 正文不缺字：两次订阅拿到的增量拼起来 == 权威正文
    assert _text_of(delivered, "chunk") == _REPLY
    assert _text_of(delivered, "part") == _REPLY
    done = [event for event in delivered if event.type == "done"]
    assert done and done[0].data["reply"] == _REPLY


def test_ac1_cursor_read_replays_only_rows_after_it(alice, per_event_flush):
    """游标语义在仓储层也是逐条成立的：`list_after` 只回 `id > cursor` 的行。"""
    registered, _events = run_ticket_events(
        alice, None, "prd", {}, *_SCRIPT, reply=_REPLY
    )
    rows = _event_rows(registered.job_id)
    assert len(rows) >= 5, "过程日志应当真的落进表里"
    middle = rows[2].id
    after = _event_rows(registered.job_id, after=middle)
    assert [row.id for row in after] == [row.id for row in rows[3:]]
    assert all(row.id > middle for row in after)


# ---------------------------------------------------------------------------
# AC2：两个订阅者同时读同一任务，互不影响
# ---------------------------------------------------------------------------


def test_ac2_two_subscribers_read_the_same_job_independently(alice, per_event_flush):
    """两个读者各持各的游标、同时读同一任务：都读到完整正文，谁也不扣减谁。

    这里刻意让三路并发：一路从游标 0 回放、一路带快照、一路只读两条就退订。
    判据是「完整那两路都拿到了 done 与完整正文」——一个读者的读取进度不是共享状态。
    """
    produced = asyncio.Event()
    release = asyncio.Event()

    async def source(kind, request):  # noqa: ANN001, ANN002 - 与真实事件流同形
        for event in _SCRIPT:
            yield event
        produced.set()
        await release.wait()
        yield StreamEvent(type="done", content="", data={"reply": _REPLY})

    async def scenario() -> tuple[int, list[StreamEvent], list[StreamEvent], list[StreamEvent]]:
        registered = build_and_register(
            owner_id=alice,
            solution_id=None,
            kind="prd",
            request={},
            event_stream_source=source,
        )
        start_registered_job(registered)

        async def collect(**kwargs) -> list[StreamEvent]:
            return [
                event
                async for event in job_service.open_stream(
                    alice, registered.job_id, **kwargs
                )
            ]

        async def collect_two_then_leave() -> list[StreamEvent]:
            events: list[StreamEvent] = []
            async for event in job_service.open_stream(alice, registered.job_id):
                events.append(event)
                if len(events) >= 2:
                    break
            return events

        replay = asyncio.create_task(collect(cursor=0))
        snapshot_reader = asyncio.create_task(collect())
        early_leaver = asyncio.create_task(collect_two_then_leave())

        await asyncio.wait_for(produced.wait(), timeout=5)
        await asyncio.sleep(0.05)  # 让三个读者先各自读走已落盘的那一段
        release.set()
        return (
            registered.job_id,
            await asyncio.wait_for(replay, timeout=5),
            await asyncio.wait_for(snapshot_reader, timeout=5),
            await asyncio.wait_for(early_leaver, timeout=5),
        )

    job_id, replay, snapshot_reader, early_leaver = asyncio.run(scenario())

    # 回放那一路：从表的第一条开始，不发快照
    assert "snapshot" not in _event_types(replay)
    assert _event_types(replay)[0] == "stage"
    # 快照那一路：首事件是快照
    assert _event_types(snapshot_reader)[0] == "snapshot"
    for reader in (replay, snapshot_reader):
        assert "done" in _event_types(reader), "每个订阅者都要能读到自己的结束事件"
        done = next(event for event in reader if event.type == "done")
        assert done.data["reply"] == _REPLY
    # 回放那一路读到了全部过程事件；提前退订那一路只读到自己的两条
    assert _text_of(replay, "chunk") == _REPLY
    assert len(early_leaver) <= 3
    # 三个读者互不扣减：表里每条行只写了一次
    rows = _event_rows(job_id)
    assert len(rows) == len({row.id for row in rows})


# ---------------------------------------------------------------------------
# AC3：通知只是加速器——没有通知也靠轮询推进
# ---------------------------------------------------------------------------


def test_ac3_polling_still_advances_without_any_notification(alice, monkeypatch):
    """把进程内唤醒整个掐掉：订阅仍能在任务结束**之前**读到事件（数据来自表）。

    `job_bus.publish` 是当下的唤醒通道（与 Postgres 的 NOTIFY 同一角色：有上限、
    无监听者即丢、不保证送达）。它被掐掉之后，读者只能靠轮询 `job_events`——
    这条用例钉住「通知不是数据通道」。
    """

    def _no_wake(*args, **kwargs):  # noqa: ANN002, ANN003
        return None

    monkeypatch.setattr(job_bus, "publish", _no_wake)
    monkeypatch.setattr(job_events, "POLL_SECONDS", 0.02)

    produced = asyncio.Event()
    release = asyncio.Event()

    async def source(kind, request):  # noqa: ANN001, ANN002
        for event in _SCRIPT:
            yield event
        produced.set()
        await release.wait()
        yield StreamEvent(type="done", content="", data={"reply": _REPLY})

    async def scenario() -> tuple[list[StreamEvent], bool]:
        registered = build_and_register(
            owner_id=alice,
            solution_id=None,
            kind="prd",
            request={},
            event_stream_source=source,
        )
        start_registered_job(registered)
        saw_chunk_while_running = asyncio.Event()

        async def collect() -> list[StreamEvent]:
            events: list[StreamEvent] = []
            # 从游标 0 回放：这样「已落盘的过程事件」会以事件形式到达（而不是被快照吞掉）
            async for event in job_service.open_stream(alice, registered.job_id, cursor=0):
                events.append(event)
                if event.type == "chunk":
                    saw_chunk_while_running.set()
            return events

        task = asyncio.create_task(collect())
        released = False
        try:
            # 闸门还没放开（任务仍在跑）就收到增量 → 只能是轮询推进的
            await asyncio.wait_for(saw_chunk_while_running.wait(), timeout=5)
        finally:
            release.set()
            released = True
        return (
            await asyncio.wait_for(task, timeout=10),
            released,
        )

    events, released = asyncio.run(scenario())

    assert released
    assert _text_of(events, "chunk") == _REPLY, "轮询要把全部增量读到"
    assert "done" in _event_types(events), "结束事件同样来自表"


# ---------------------------------------------------------------------------
# AC4：新读者先拿快照再追事件，不回放全历史
# ---------------------------------------------------------------------------


def test_ac4_cold_reader_gets_snapshot_and_does_not_replay_history(alice, per_event_flush):
    """任务结束后新来一个读者：首事件是快照，**不回放**过程日志。

    表里明明有一整段历史（stage / chunk / part / done / run_summary），冷读者却只应
    看到「快照 + 结束回执」：正文由快照给出，过程日志不再逐条重放（否则每次刷新
    都会把生成过程重演一遍）。
    """
    registered, _events = run_ticket_events(
        alice, None, "prd", {}, *_SCRIPT, reply=_REPLY
    )
    rows = _event_rows(registered.job_id)
    assert len(rows) >= 5, "历史确实在表里"
    assert {row.kind for row in rows} <= set(job_events.EVENT_KINDS), (
        f"事件 kind 超出既定取值集合：{sorted({row['kind'] for row in rows})}"
    )
    process_kinds = {row.kind for row in rows} - {"run_summary"}
    assert {"chunk", "part", "stage", "done"} <= process_kinds

    cold = _collect(alice, registered.job_id)

    assert _event_types(cold)[0] == "snapshot"
    # 过程事件一条都没被回放
    replayed = set(_event_types(cold)) - {"snapshot", "run_summary", "error"}
    assert replayed == set(), f"冷读者回放了历史事件：{sorted(replayed)}"
    snapshot = cold[0]
    assert snapshot.data["finalized_text"] == _REPLY
    assert isinstance(snapshot.event_id, int)
    # 快照游标 == 该任务最新一行事件的 id：新读者从这里往后追，不回放已含内容
    assert snapshot.event_id == rows[-1].id
    assert _event_types(cold) == ["snapshot", "run_summary"]


def test_ac4_snapshot_cursor_is_the_last_event_covered_by_the_snapshot(alice, per_event_flush):
    """快照携带的游标 == 快照内容覆盖到的最后一条事件（快照与游标同一笔落库）。

    判据从库里独立读：`MAX(id)` 与快照游标必须一致——否则「快照 + 从这里往后追」
    会出现重复（游标落后）或缺口（游标超前）。
    """
    registered, _events = run_ticket_events(
        alice, None, "prd", {}, *_SCRIPT, reply=_REPLY
    )
    cold = _collect(alice, registered.job_id)
    with session_scope() as session:
        max_id = JobEventRepository(session).max_id(registered.job_id)
    assert cold[0].event_id == max_id


# ---------------------------------------------------------------------------
# AC5：过期事件按既定策略清理，权威正文不受影响
# ---------------------------------------------------------------------------


def _age_job(job_id: int, *, updated_at: str, status: str | None = None) -> None:
    """把任务行的 `updated_at` 拨到过去（`status` 给定时一并改状态）。"""
    if status is None:
        sql = "UPDATE generation_jobs SET updated_at = %s WHERE id = %s"
        params = (updated_at, job_id)
    else:
        sql = "UPDATE generation_jobs SET updated_at = %s, status = %s WHERE id = %s"
        params = (updated_at, status, job_id)
    with connect() as conn:
        conn.execute(sql, params)
        conn.commit()


_OLD = "2026-01-01T00:00:00.000000+00:00"


def test_ac5_purge_removes_expired_events_and_keeps_the_authoritative_body(alice):
    """终态任务过期（>7 天）后清理过程日志；任务行与正文一个字都不动。

    清理单元是**可直接调用的函数**（函数自己不起线程、不起定时器）：生产里由调度循环的清理周期驱动（`job_scheduler.purge_events`，约 1 小时一轮），本函数只负责「按保留期删过程日志」这一个动作。
    """
    registered, _events = run_ticket_events(
        alice, None, "prd", {}, *_SCRIPT, reply=_REPLY
    )
    job_id = registered.job_id
    before = job_get(alice, job_id)
    assert before is not None
    _age_job(job_id, updated_at=_OLD)
    assert len(_event_rows(job_id)) >= 5

    # 一条「新终态」与一条「仍旧在跑」的对照：都不该被清理
    fresh_id = job_create(alice, "prd", None, request_params={})
    _append_rows(fresh_id, list(_SCRIPT))
    _age_job(fresh_id, updated_at=utc_now().isoformat(), status="succeeded")
    running_id = job_create(alice, "prd", None, request_params={})
    _append_rows(running_id, list(_SCRIPT))
    _age_job(running_id, updated_at=_OLD)

    deleted = job_events.purge_expired_events(retention_days=7)

    assert deleted >= 5, f"过期任务的过程日志应当被删掉，实际删了 {deleted} 条"
    assert _event_rows(job_id) == [], "过期事件必须清干净"
    assert _event_rows(fresh_id) != [], "7 天内的终态任务不该被清理"
    assert _event_rows(running_id) != [], "仍在执行的任务不该被清理"

    after = job_get(alice, job_id)
    assert after is not None
    assert after["status"] == before["status"], "权威状态不受事件清理影响"
    assert after["draft_content"] == before["draft_content"], "权威正文不受事件清理影响"
    assert after["usage"] == before["usage"], "权威回执不受事件清理影响"


def test_ac5_purge_is_bounded_by_retention_window(alice, per_event_flush):
    """保留期是「终态之后 7 天」：刚好 8 天前的清理，6 天前的不动。"""
    registered, _events = run_ticket_events(
        alice, None, "prd", {}, *_SCRIPT, reply=_REPLY
    )
    now = utc_now()
    inside_id = job_create(alice, "prd", None, request_params={})
    _append_rows(inside_id, list(_SCRIPT))
    _age_job(inside_id, updated_at=(now - timedelta(days=6)).isoformat(), status="succeeded")
    outside_id = job_create(alice, "prd", None, request_params={})
    _append_rows(outside_id, list(_SCRIPT))
    _age_job(outside_id, updated_at=(now - timedelta(days=8)).isoformat(), status="failed")

    deleted = job_events.purge_expired_events(retention_days=7, now=now)

    assert _event_rows(outside_id) == [], "超过保留期的事件应当被清理"
    assert _event_rows(inside_id) != [], "保留期内的事件必须留着"
    # 本次只清这两条赢家的过期行；另一条真任务的事件也在保留期内（刚刚收尾）
    assert _event_rows(registered.job_id) != []
    assert deleted >= len(_SCRIPT)


# ---------------------------------------------------------------------------
# HTTP 接缝：客户端带的游标真的被用上了
# ---------------------------------------------------------------------------


def test_stream_endpoint_honours_cursor_query(client, alice, per_event_flush):
    """`GET /api/jobs/{id}/stream?cursor=N`：从 N 之后继续发，且**不重发快照**。

    `Last-Event-ID` 是 SSE 规范里客户端断线重连自带的头，两条入口等价；
    前端当前走查询参数（游标来自每条事件的 `id:` 行），头是给标准 SSE 客户端留的。
    """
    registered, _events = run_ticket_events(
        alice, None, "prd", {}, *_SCRIPT, reply=_REPLY
    )
    rows = _event_rows(registered.job_id)
    cursor = rows[1].id

    def _frames(response) -> list[tuple[int | None, dict]]:  # noqa: ANN001 - TestClient 响应
        """把 SSE 文本拆成 (id, payload) 列表；`id:` 行是游标的唯一出口。"""
        assert response.status_code == 200, response.text
        frames: list[tuple[int | None, dict]] = []
        current_id: int | None = None
        for line in response.text.splitlines():
            if line.startswith("id:"):
                current_id = int(line[len("id:") :].strip())
            elif line.startswith("data:"):
                raw = line[len("data:") :].strip()
                if raw in ("", "[DONE]"):
                    continue
                frames.append((current_id, json.loads(raw)))
                current_id = None
        return frames

    resumed = _frames(
        client.get(f"/api/jobs/{registered.job_id}/stream", params={"cursor": cursor})
    )
    assert "snapshot" not in [payload["type"] for _, payload in resumed], "带游标不该重发快照"
    resumed_ids = [event_id for event_id, _ in resumed]
    assert all(event_id is not None and event_id > cursor for event_id in resumed_ids), (
        "只发游标之后的"
    )
    assert resumed_ids == [row.id for row in rows if row.id > cursor]

    header_resumed = _frames(
        client.get(
            f"/api/jobs/{registered.job_id}/stream",
            headers={"Last-Event-ID": str(cursor)},
        )
    )
    assert [event_id for event_id, _ in header_resumed] == [
        row.id for row in rows if row.id > cursor
    ]

    fresh = _frames(client.get(f"/api/jobs/{registered.job_id}/stream"))
    assert [payload["type"] for _, payload in fresh] == ["snapshot", "run_summary"]
    # 快照的 `id:` 就是「快照内容覆盖到的最后一条事件」——新读者从它往后追即可
    assert fresh[0][0] == rows[-1].id
    # 事件体本身**不含**游标字段（它只在 `id:` 行里）
    assert "cursor" not in fresh[0][1]["data"]


def test_stream_endpoint_rejects_malformed_cursor(client, alice):
    """非法游标在参数校验层就被挡住（422），绝不会被当成 0 去回放全历史。"""
    response = client.get("/api/jobs/1/stream", params={"cursor": "abc"})
    assert response.status_code == 422, response.text
