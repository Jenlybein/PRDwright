"""排空职责归位：等待在途任务由生成任务一侧回答（票 #37）。

排空仍是包外使用方，只问三件事：「还有哪些在跑」「要不要等」「超时取消哪几支」。
drain 一个运行时内部字段都不碰——静态守卫扫 `runtime.task` / `runtime.status` 零命中；
行为守卫 drain 时把「在途快照」换成**没有 task / status 属性**的句柄，
若 drain 仍在自己翻字段就会红，而超时取消仍要落终态。

同时守住「一条都不变」的排空语义：
- 摘流 → 停收新活 → 最多等 30 秒 → 超时标 CANCELLED 终态 → flush 日志；
- 就绪探针立刻 503、排空期间新任务被明确拒绝；
- `drain_controller` 仍是「可直接调用、用例毫秒级验证」的进程内单例；
- 流式事件形状（error → run_summary）、`CANCELLED` 错误码与文案不变。

用例全部走公开接缝：`build_job_ticket → register_job_ticket → start_registered_job`
造出真实在途任务，再用假事件流把它卡在「模型调用里」；不替换内部零件。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import register, users

from backend.core.contracts import StreamEvent
from backend.services import job_drain, job_service, job_runtime, job_ticket, job_wait
from backend.services.job_bus import job_bus
from dbsupport import connect


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------


def _alice_id() -> int:
    """取已注册用户 alice 的 id（generation_jobs.owner_id 有外键）。"""
    user = users.get_by_username("alice")
    assert user is not None
    return int(user["id"])


@pytest.fixture
def alice(client):
    """注册 alice 并返回其 id。"""
    register(client)
    return _alice_id()


@pytest.fixture(autouse=True)
def clean_job_state():
    """每个用例前后清空进程内运行时、任务集合、总线与排空标志。"""
    job_runtime._RUNTIMES.clear()  # noqa: SLF001
    job_runtime._TASKS.clear()  # noqa: SLF001
    job_bus._subscribers.clear()  # noqa: SLF001
    job_drain.drain_controller.reset()
    yield
    job_runtime._RUNTIMES.clear()  # noqa: SLF001
    job_runtime._TASKS.clear()  # noqa: SLF001
    job_bus._subscribers.clear()  # noqa: SLF001
    job_drain.drain_controller.reset()


def _start_stuck_job(owner_id: int, never: asyncio.Event) -> int:
    """走公开三步起一个真的卡在模型调用里的任务（假事件流在造单时传入）。"""

    async def stream(kind, request):  # noqa: ANN001, ARG001
        yield StreamEvent(type="chunk", content="第一段", data={})
        await never.wait()
        yield StreamEvent(type="done", content="", data={"reply": "完整正文"})

    draft = job_ticket.build_job_ticket(
        owner_id=owner_id,
        solution_id=None,
        kind="prd",
        request={"requirements": "排空时在途"},
        event_stream_source=stream,
    )
    registered = job_ticket.register_job_ticket(draft)
    job_ticket.start_registered_job(registered)
    return registered.job_id


def _row(job_id: int) -> dict[str, Any]:
    """按 id 直读任务行终态。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT status, error_code, error_message FROM generation_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    assert row is not None
    return dict(row)


def _run(coro):
    """在新事件循环里跑一段协程（排空是异步单元）。"""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 一、静态守卫：drain 不读任何运行时内部字段，也不再经再导出引用 JobRuntime
# ---------------------------------------------------------------------------


def test_drain_source_never_touches_runtime_internals():
    """`job_drain.py` 源码里没有 `runtime.task` / `runtime.status`，也不引用 JobRuntime。

    规格要求「排空一个内部字段都不碰」：这条把「等待在途」彻底留在生成任务一侧，
    内部字段改名或换成别的存储结构都不该让排空跟着改。
    """
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "backend"
        / "services"
        / "job_drain.py"
    ).read_text(encoding="utf-8")
    assert re.search(r"runtime\s*\.\s*(task|status)\b", source) is None
    assert "job_service.JobRuntime" not in source
    assert "list[job_service.JobRuntime]" not in source


# ---------------------------------------------------------------------------
# 二、行为守卫：快照句柄没有 task / status，drain 仍能等到超时取消
# ---------------------------------------------------------------------------


class _OpaqueHandle:
    """生成任务一侧交出的抽象句柄：只读 id，访问 task / status 立即报错。"""

    def __init__(self, job_id: int) -> None:
        self.job_id = job_id

    def __getattr__(self, name: str) -> Any:
        if name in ("task", "status"):
            raise AssertionError(f"排空不该读运行时的内部字段：{name}")
        raise AttributeError(name)


def test_drain_waits_and_cancels_without_reading_runtime_fields(alice, monkeypatch):
    """在途任务超时：drain 只调生成任务一侧的等待能力，运行时对象一个字段都不碰。

    把 `in_flight()` 换成不含 task/status 的抽象句柄；若 drain 仍在自己翻字段就红。
    等满窗口后仍要落 `failed + CANCELLED` 终态——「要不要等 / 超时取消哪几支」
    由生成任务一侧按句柄回答。
    """
    never = asyncio.Event()

    async def scenario() -> tuple[int, job_drain.DrainReport]:
        job_id = _start_stuck_job(alice, never)
        for _ in range(200):
            if job_wait.in_flight():
                break
            await asyncio.sleep(0.005)
        # 排空拿到的不再是运行时对象，只是生成任务一侧给出的抽象句柄。
        monkeypatch.setattr(
            job_drain.drain_controller, "in_flight", lambda: [_OpaqueHandle(job_id)]
        )
        return job_id, await job_drain.drain_controller.drain(timeout=0.1)

    job_id, report = _run(scenario())

    assert report.in_flight == 1
    assert report.cancelled == 1
    assert _row(job_id)["error_code"] == "CANCELLED"
    never.set()


def test_drain_cancels_a_stuck_job_and_keeps_the_event_contract(alice):
    """真实在途任务超时：CANCELLED 终态、error → run_summary、报告数字自洽。

    与既有的排空用例同一口径，但任务由**新三步**造出，证明迁移后排水语义不变。
    """

    async def scenario() -> tuple[int, job_drain.DrainReport, list]:
        job_id = _start_stuck_job(alice, asyncio.Event())
        events: list = []

        async def consume() -> None:
            async for event in job_service.open_stream(alice, job_id):
                events.append(event)

        watcher = asyncio.create_task(consume())
        for _ in range(200):
            if any(event.type == "chunk" for event in events):
                break
            await asyncio.sleep(0.005)

        report = await job_drain.drain_controller.drain(timeout=0.1)
        await asyncio.wait_for(watcher, timeout=5)
        return job_id, report, events

    job_id, report, events = _run(scenario())

    assert report.in_flight == 1
    assert report.cancelled == 1
    assert report.finished == 0
    assert report.hit_deadline is True
    assert report.waited_seconds >= 0.08, "至少要等满窗口，不能提前取消"
    assert job_drain.drain_controller.is_draining() is True

    row = _row(job_id)
    assert row["status"] == "failed"
    assert row["error_code"] == "CANCELLED"
    assert "重启" in row["error_message"]

    error = next((event for event in events if event.type == "error"), None)
    assert error is not None
    assert error.code == "CANCELLED"
    assert "重启" in error.content
    summary = [event for event in events if event.type == "run_summary"]
    assert summary and summary[-1].data["status"] == "failed"


def test_drain_returns_at_once_when_nothing_is_in_flight(alice):
    """没有在途任务时立刻返回：等待是上限不是必等（毫秒级验证）。"""

    async def scenario() -> tuple[job_drain.DrainReport, float]:
        started = time.monotonic()
        report = await job_drain.drain_controller.drain(timeout=30)
        return report, time.monotonic() - started

    report, elapsed = _run(scenario())
    assert (report.in_flight, report.cancelled, report.hit_deadline) == (0, 0, False)
    assert elapsed < 1.0, f"没有在途任务时不该等满窗口（实测 {elapsed:.2f} 秒）"


def test_readyz_and_new_job_refusal_stay_unchanged(client):
    """摘流与停收新活仍是既有口径：/readyz 503、创建任务 503、/healthz 仍 200。"""
    register(client)
    assert client.get("/readyz").status_code == 200

    assert _run(job_drain.drain_controller.drain(timeout=0)) is not None

    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["draining"]["ok"] is False
    assert client.get("/healthz").status_code == 200

    refused = client.post("/api/jobs", json={"kind": "prd", "requirements": "排空中"})
    assert refused.status_code == 503
    assert refused.json()["code"] == "INTERNAL_ERROR"
    assert "重启" in refused.json()["detail"]


def test_shutdown_flushes_logs_after_draining(alice, caplog):
    """顺序不变：排空汇总日志先写，shutdown 再 flush，退出码仍是 0。"""
    never = asyncio.Event()

    async def scenario() -> int:
        _start_stuck_job(alice, never)
        for _ in range(200):
            if job_wait.in_flight():
                break
            await asyncio.sleep(0.005)
        with caplog.at_level(logging.WARNING, logger="backend.services.job_drain"):
            return await job_drain.drain_controller.shutdown(timeout=0.05)

    code = _run(scenario())
    assert code == 0
    finished = [r for r in caplog.records if getattr(r, "event", "") == "service.drain_finished"]
    assert finished and finished[-1].cancelled_jobs == 1
    assert "取消 1 个" in finished[-1].getMessage()
    never.set()
