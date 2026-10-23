"""端到端回归：**生成途中通过 HTTP 删掉方案**。

线上形状：用户在大模型调用进行中删除当前方案 → 调用结束后列表里冒出一份
「未命名方案 · MM-DD HH:mm」，刷新仍在（库里真多了一行）。

后端这一半的责任是「方案没了，就别再往 documents 里写正文，并把在途任务收掉」。
本文件不做任何 HTTP 打桩，走的是真实接口序列：

    建方案 → POST /api/jobs（后台任务真的跑起来）→ DELETE /api/solutions/{id}
    → 放行被闸住的模型调用 → 等任务收尾 → 查库

与 `test_generation_job.py` 里那几条的区别：那几条分别只验「仓储置终态」
「收尾不写文档」「事件循环看到标记就 break」；这里验的是**它们接在一起**
——路由里的 `cancel_for_solution` 确实与内存运行时、收尾逻辑串上了。

⚠️ 断言里刻意包含「一个事件都没被消费」（`draft_content` 为空）：只看「文档没写」
是不够的——`_finalize` 里那道「方案还在不在」的兜底也能让文档不写。真正要钉住的是
**任务在途中就被中止**（否则 token 照烧、只在收尾时判个失败）。

`generation_jobs` 的行**刻意不删**（用量审计要靠它），因此断言里专门钉住它还在。
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, AsyncIterator

from conftest import register, users

from conftest import job_get
from backend.services import auth_service, document_service, document_store
from fakes import ScriptedLLM

# 等待任务收尾的上限：正常在毫秒级完成，留足余量以免慢机器上偶发失败
_SETTLE_TIMEOUT_SECONDS = 10.0


class _GatedLLM(ScriptedLLM):
    """首块产出前先等一个**跨线程**闸门。

    用闸门而不是 `sleep`：测试线程先发 DELETE、再放行模型，因此「删除发生在生成途中」
    是确定的，不靠时序碰运气（`sleep` 在慢机器上会变成 flaky）。
    闸门落在 `super().astream` **之前**，所以闸住期间一次调用都没发生
    （`calls` 为空）——「取消后事件不再被消费」因此有了确定性。
    """

    def __init__(self, gate: threading.Event, pieces: list[str] | None = None) -> None:
        super().__init__(pieces)
        self._gate = gate

    async def astream(self, messages: list[Any]) -> AsyncIterator[Any]:
        # `to_thread` 把等待放到线程里，事件循环照常转：DELETE 请求才进得来
        await asyncio.to_thread(self._gate.wait, _SETTLE_TIMEOUT_SECONDS)
        async for chunk in super().astream(messages):
            yield chunk


def _install_doc_service(monkeypatch, gate: threading.Event) -> _GatedLLM:
    """把文档服务换成「先等闸门、再吐正文」的假实现，返回假 LLM 供断言调用次数。"""
    llm = _GatedLLM(gate, ["章节正文"])
    monkeypatch.setattr(
        document_service,
        "get_service",
        lambda: document_service.DocumentService(llm=llm),
    )
    return llm


def _owner_id() -> int:
    user = users.get_by_username("alice")
    assert user is not None
    return int(user["id"])


def _create_solution(client) -> int:
    response = client.post(
        "/api/solutions",
        json={"title": "演示方案", "mode": "structured", "stage": "form", "data": "{}"},
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _start_prd_job(client, solution_id: int) -> int:
    response = client.post(
        "/api/jobs",
        json={"kind": "prd", "solution_id": solution_id, "requirements": "做一个演示产品"},
    )
    assert response.status_code == 201, response.text
    return int(response.json()["job_id"])


def _wait_settled(owner_id: int, job_id: int) -> dict[str, Any]:
    """轮询任务终态；超时就让用例失败（把最后一行带出去便于排查）。"""
    deadline = time.monotonic() + _SETTLE_TIMEOUT_SECONDS
    row = job_get(owner_id, job_id)
    while row is not None and row["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.02)
        row = job_get(owner_id, job_id)
    assert row is not None, "任务行被删掉了：用量审计要靠它，必须保留"
    assert row["status"] != "running", f"任务没有收尾：{row}"
    return row


def _wait_document(owner_id: int, solution_id: int) -> list[dict[str, Any]]:
    """等文档落库。

    收尾里「任务置终态」与「写文档」是两步：状态先落库，正文随后写。只等任务状态就断言
    文档，会周期性撞上中间那几毫秒——那是一条 flaky 用例，不是被测代码的问题。
    """
    deadline = time.monotonic() + _SETTLE_TIMEOUT_SECONDS
    rows = document_store.list_documents(owner_id, solution_id)
    while not rows and time.monotonic() < deadline:
        time.sleep(0.02)
        rows = document_store.list_documents(owner_id, solution_id)
    return rows


def test_delete_solution_mid_generation_leaves_no_document(client, monkeypatch):
    """删除方案会取消在途任务：不写 documents、任务落 failed/CANCELLED、任务行仍存在。

    同时用「模型调用次数」证明任务**在途中**就被中止了，而不只是收尾时判了个失败：
    `_finalize` 里那道「方案还在不在」的兜底也能让文档不写，若只用「文档没写」做断言，
    路由漏调 `cancel_for_solution` 这个回归就测不出来。
    """
    register(client)
    owner_id = _owner_id()

    # —— 基准：同一套假模型，不删除，跑完整篇要调几次 ——
    full_gate = threading.Event()
    full_gate.set()  # 闸门一开始就开着，等于没有闸门
    full_llm = _install_doc_service(monkeypatch, full_gate)
    full_job = _start_prd_job(client, _create_solution(client))
    assert _wait_settled(owner_id, full_job)["status"] == "completed"
    full_calls = len(full_llm.calls)
    assert full_calls >= 2, "整篇生成至少要有多次模型调用，否则这条对比没有意义"

    # —— 被测：生成途中删掉方案 ——
    gate = threading.Event()
    llm = _install_doc_service(monkeypatch, gate)
    solution_id = _create_solution(client)
    job_id = _start_prd_job(client, solution_id)

    # 任务此刻真的在跑（模型调用被闸住），并且一次都还没产出
    running = job_get(owner_id, job_id)
    assert running is not None
    assert running["status"] == "running"
    assert llm.calls == []

    # 用户点删除，然后放行模型：迟到的正文不能换来一条文档
    assert client.delete(f"/api/solutions/{solution_id}").status_code == 200
    gate.set()

    row = _wait_settled(owner_id, job_id)

    # 1. 任务**在途中**被中止：模型调用次数少于跑完整篇
    assert len(llm.calls) < full_calls
    # 2. 文档侧干净：方案都删了，documents 里不该有任何一行
    assert document_store.list_documents(owner_id, solution_id) == []
    # 3. 任务终态：failed + CANCELLED（刻意不新增 cancelled 状态）
    assert row["status"] == "failed"
    assert row["error_code"] == "CANCELLED"
    # 4. 任务行仍在：用完量审计要能查到「这次烧了 token 但没产出」
    assert job_get(owner_id, job_id) is not None


def test_delete_solution_mid_generation_does_not_touch_other_solutions(client, monkeypatch):
    """取消只作用于被删的那份方案：同一 owner 的另一份方案不受影响。"""
    gate = threading.Event()
    _install_doc_service(monkeypatch, gate)
    register(client)
    owner_id = _owner_id()
    doomed = _create_solution(client)
    kept = _create_solution(client)

    doomed_job = _start_prd_job(client, doomed)
    # 另起一个任务挂在**另一份**方案上（同样被闸住，删除时它还 running）
    kept_job = _start_prd_job(client, kept)

    assert client.delete(f"/api/solutions/{doomed}").status_code == 200
    gate.set()

    doomed_row = _wait_settled(owner_id, doomed_job)
    kept_row = _wait_settled(owner_id, kept_job)

    assert doomed_row["error_code"] == "CANCELLED"
    # 另一份方案的任务照常成功，文档也该写进去
    assert kept_row["status"] == "completed"
    assert _wait_document(owner_id, kept) != []
