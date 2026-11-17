"""取消语义的三条收口（P0-2 / P0-3 / P0-4）。

三条各自守一句**用户能看见的话**，而且都曾是「说法与实现不一致」的地方：

1. **取消不是审核失败**（P0-2）：用户按停止之后，审阅页不该显示「审核失败」。
   判据是 `document_versions.review_failed_at` 一个字都不写。
2. **「取消中」不会被新任务顶替**（P0-3）：用户已经表达了「停」，那一行不该被改写成
   `superseded`——被取代（系统让位）与已取消（用户意图）含义相反，统计上必须分开。
3. **删方案 / 删账号是单步收口**（P0-4）：方案或账号已经不在了，没人会来确认退出，
   所以任意非终态一步落 `cancelled` + 清租约（而不是留一行 `cancelling` 等恢复器）。

接缝：都跨公开动作（`job_service.*` / 造单 → 登记 → 派发 → 订阅 / 仓储读），
不直调 `_finalize` 之类的内部函数；读库一律经 `job_get` / `document_store`。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncIterator

import pytest
from conftest import job_create, job_get, job_claim, register, users

from backend.core.contracts import StreamEvent
from backend.services import document_store, job_runtime, job_service
from backend.services.job_bus import job_bus
from ticket_support import build_and_register, start_registered_job

_TERMINAL = ("succeeded", "failed", "superseded", "cancelled")
_SETTLE_TIMEOUT_SECONDS = 10.0


@pytest.fixture(autouse=True)
def clean_job_state():
    """每个用例前后清空进程内运行时与总线订阅（走公开的重置口）。"""
    job_runtime.reset_for_tests()
    job_bus.reset_for_tests()
    yield
    job_runtime.reset_for_tests()
    job_bus.reset_for_tests()


def _alice_id() -> int:
    user = users.get_by_username("alice")
    assert user is not None
    return int(user["id"])


def _create_solution(client) -> int:  # noqa: ANN001 - TestClient
    response = client.post(
        "/api/solutions",
        json={
            "title": "取消语义用例",
            "mode": "structured",
            "stage": "review-prd",
            "state": {"entry_mode": "prd-quick", "quick_requirements": "做一个演示产品"},
        },
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


async def _wait_settled(owner_id: int, job_id: int) -> dict[str, Any]:
    """轮询到任务**终态**（异步版：这条路径上有后台任务在同一个循环里跑）。"""
    deadline = time.monotonic() + _SETTLE_TIMEOUT_SECONDS
    while True:
        row = job_get(owner_id, job_id)
        assert row is not None, "任务行被删掉了：用量审计要靠它，必须保留"
        if row["status"] in _TERMINAL:
            return row
        assert time.monotonic() < deadline, f"任务没有收尾：{row}"
        await asyncio.sleep(0.02)


def _gated_stream(text: str, gate: asyncio.Event, produced: list[str]) -> Any:
    """吐一段正文后卡在闸门上：用例在「任务仍在跑」的窗口里插入动作。"""

    async def stream(kind: str, request: Any) -> AsyncIterator[StreamEvent]:
        produced.append("第一段")
        yield StreamEvent(type="chunk", content=text, data={})
        await gate.wait()
        produced.append("done")
        yield StreamEvent(type="done", content="", data={"reply": "完整的回复"})

    return stream


def test_cancelled_review_does_not_mark_review_failed(client) -> None:
    """P0-2：用户停止重审 → 任务 `cancelled`，但 `review_failed_at` 一个字都不写。

    取消（用户意图）与审核失败（这次真没审成）是两件事：把前者写成后者，用户会在
    审阅页看到「审核失败」，而真相是「我自己按了停止」。
    """
    register(client)
    owner_id = _alice_id()
    solution_id = _create_solution(client)
    # 先有一版正文：审核的对象必须是真实存在的当前版本
    document_store.write_document(
        owner_id, solution_id, "prd", "# 演示 PRD\n\n正文", mode="append", source="prd"
    )

    async def scenario() -> tuple[int, dict[str, Any]]:
        gate = asyncio.Event()
        produced: list[str] = []
        registered = build_and_register(
            owner_id=owner_id,
            solution_id=solution_id,
            kind="review",
            request={"doc_kind": "prd"},
            event_stream_source=_gated_stream("审核中…", gate, produced),
        )
        start_registered_job(registered)
        while not produced:
            await asyncio.sleep(0)
        # 用户按「停止生成」：单条任务走两阶段（先转 cancelling，执行者确认后落终态）
        assert job_service.cancel_job(owner_id, registered.job_id) is True
        assert job_get(owner_id, registered.job_id)["status"] == "cancelling"
        gate.set()
        return registered.job_id, await _wait_settled(owner_id, registered.job_id)

    job_id, row = asyncio.run(scenario())

    assert row["status"] == "cancelled", f"用户停止应落已取消：{row}"
    assert row["error_code"] == ""
    doc = document_store.get_document(owner_id, solution_id, "prd")
    assert doc is not None
    assert doc["review_failed_at"] in (None, ""), (
        "取消不是审核失败：停止之后不该在审阅页显示「审核失败」"
    )
    assert job_id  # 行仍在（用量审计要靠它）


def test_new_registration_keeps_a_cancelling_job_out_of_superseded(client) -> None:
    """P0-3：同方案同产物再登记一支新任务，**取消中的那一行仍是 cancelling**。

    被取代是「系统让位」，已取消是「用户意图」；把正在取消的行改写成 `superseded`，
    统计上就再也分不清「用户不要了」还是「被新任务挤掉了」。新的任务照常建行、照常派发。
    """
    register(client)
    owner_id = _alice_id()

    async def scenario() -> tuple[int, int, dict[str, Any]]:
        gate = asyncio.Event()
        produced: list[str] = []
        first = build_and_register(
            owner_id=owner_id,
            solution_id=None,
            kind="prd",
            request={},
            event_stream_source=_gated_stream("第一段正文", gate, produced),
        )
        start_registered_job(first)
        while not produced:
            await asyncio.sleep(0)
        assert job_service.cancel_job(owner_id, first.job_id) is True
        assert job_get(owner_id, first.job_id)["status"] == "cancelling"

        # 同一 owner + 无方案 + 同产物，再登记一支：旧的取消中任务不该被顶替
        second = build_and_register(
            owner_id=owner_id,
            solution_id=None,
            kind="prd",
            request={},
        )
        mid = job_get(owner_id, first.job_id)
        assert mid is not None
        assert mid["status"] == "cancelling", (
            f"取消中的行被新任务改写成 {mid['status']}：用户意图被盖成了系统让位"
        )
        assert job_get(owner_id, second.job_id)["status"] == "pending"

        gate.set()
        return first.job_id, second.job_id, await _wait_settled(owner_id, first.job_id)

    first_id, second_id, row = asyncio.run(scenario())

    assert row["status"] == "cancelled", "执行者确认之后仍按取消收口（不是 superseded）"
    assert second_id != first_id
    # 新任务没有被执行者的收尾波及：它仍是排队中（本用例没有派发它）
    assert job_get(_alice_id(), second_id)["status"] == "pending"


def test_delete_solution_settles_a_running_job_in_one_step(client) -> None:
    """P0-4：删方案 → 执行中的任务**一步**落已取消并清租约，同时记一条取消账。

    转换表里这一行是「任意非终态 → 已取消 + 清租约」：方案已经不存在，不会再有
    「执行者来确认退出」这件事，留一行 `cancelling` 只是把它交给恢复器、等租约到期。
    """
    from backend.repositories.job_attempt_repo import OUTCOME_CANCELLED, JobAttemptRepository
    from backend.core.engine import session_scope

    register(client)
    owner_id = _alice_id()
    solution_id = _create_solution(client)
    job_id = job_create(owner_id, "prd", solution_id)
    lease = job_claim(owner_id, job_id)

    assert job_service.delete_solution_and_cancel_jobs(owner_id, solution_id) is True

    row = job_get(owner_id, job_id)
    assert row is not None
    assert row["status"] == "cancelled", f"应当一步收口成已取消，而不是取消中等确认：{row}"
    assert row["error_code"] == "", "已取消不是一个错误码，是一个状态"
    assert row["error_message"] == "方案已删除，任务已取消"
    assert (row["lease_owner"], row["lease_token"], row["lease_expire_at"]) == ("", "", ""), (
        "单步收口必须同时清租约：这一行已经没有执行者了"
    )
    assert lease.token != ""

    with session_scope() as session:
        attempts = JobAttemptRepository(session).list_by_job(owner_id, job_id)
    assert [item.outcome for item in attempts] == [OUTCOME_CANCELLED], (
        "单步收口此刻就进终态，账本也要当场记一行（不会再有确认方）"
    )
