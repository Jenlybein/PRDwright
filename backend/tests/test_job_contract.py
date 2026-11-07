"""任务请求体的**收口契约**（spec #51 / 票 #54）。

本文件只跨 HTTP 主链路断言：

| 接缝 | 断言的可观察行为 |
| --- | --- |
| `POST /api/jobs` | 文档类任务必须带方案；缺方案 / 缺材料是**明确的 400**，且不留下一个空跑的任务 |
| `POST /api/jobs`（旧前端） | 请求体里多带上游正文与需求正文**被忽略**，既不是 422、也不参与生成 |
| `POST /api/conversation/generate-api-docs-stream` / `...-prompts-stream` / `...-prd-stream` / `...-prd-from-summary-stream` | 四条旧的非任务通道一律改收 `solution_id`，上游与需求输入由服务端取 |
| `POST /api/jobs`（clarify） | 澄清对话由服务端按线程取；请求体里不再有 `history` |

「不空跑一次生成」用 `GET /api/jobs` 判：400 之后任务列表仍是空的——被拒的请求
不该在用户的任务列表里留一条失败记录。

所有用例离线零 token：真实 LLM 工厂一调用即抛，生成一律注入 `ScriptedLLM`。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import register, users

from backend.core.engine import session_scope
from backend.repositories.chat_turn_repo import ChatTurnRepository
from backend.services import (
    document_service,
    job_runtime,
    llm_factory,
    prd_section_writer,
)
from backend.services.job_bus import job_bus
from fakes import ScriptedLLM

# ---------------------------------------------------------------------------
# 夹具与工具（与 `test_job_server_inputs.py` 同款）
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_real_llm(monkeypatch):
    """真实 LLM 工厂一被调用就抛，杜绝任何测试触网。"""

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("测试不应触网调用真实 LLM")

    monkeypatch.setattr(llm_factory, "get_llm", _boom)
    monkeypatch.setattr(document_service, "get_llm", _boom)
    monkeypatch.setattr(prd_section_writer, "get_llm", _boom)
    yield


@pytest.fixture(autouse=True)
def clean_job_state():
    """清空进程内运行时与任务集合，避免用例互相污染。"""
    job_runtime._RUNTIMES.clear()  # noqa: SLF001
    job_runtime._TASKS.clear()  # noqa: SLF001
    job_bus._subscribers.clear()  # noqa: SLF001
    yield
    job_runtime._RUNTIMES.clear()  # noqa: SLF001
    job_runtime._TASKS.clear()  # noqa: SLF001
    job_bus._subscribers.clear()  # noqa: SLF001


def _fake_service(monkeypatch, llm: ScriptedLLM) -> None:
    """把模块级文档服务的实例换成注入了假 LLM 的实例。"""
    monkeypatch.setattr(
        document_service,
        "get_service",
        lambda: document_service.DocumentService(llm=llm),
    )


def _create_solution(client, state: dict[str, Any] | None = None) -> int:
    """建一份真实方案（可带状态列），返回 id。"""
    created = client.post(
        "/api/solutions",
        json={
            "title": "演示方案",
            "mode": "structured",
            "stage": "review-prd",
            "state": state or {},
        },
    )
    assert created.status_code == 201, created.text
    return int(created.json()["id"])


def _put_document(client, solution_id: int, kind: str, content: str) -> None:
    """把一份正文写进方案（走文档路由；没有文档时它会顺势建第一版）。"""
    response = client.put(
        f"/api/solutions/{solution_id}/documents/{kind}", json={"content": content}
    )
    assert response.status_code == 200, response.text


def _documents(client, solution_id: int) -> dict[str, dict[str, Any]]:
    """读回方案下的三份产物（按 kind 索引）。"""
    response = client.get(f"/api/solutions/{solution_id}/documents")
    assert response.status_code == 200, response.text
    return {item["kind"]: item for item in response.json()["documents"]}


def _seed_thread(solution_id: int, stage: str, turns: list[tuple[str, str]]) -> None:
    """在指定澄清线程里落若干轮次（准备材料，不参与断言）。

    生产里这些轮次由对话任务写入同一张表；用例里注册的用户都是 alice。
    """
    user = users.get_by_username("alice")
    assert user is not None
    with session_scope() as session:
        repo = ChatTurnRepository(session)
        for role, content in turns:
            repo.create(int(user["id"]), solution_id, stage=stage, role=role, content=content)


def _create_job(client, payload: dict[str, Any]) -> int:
    """建一个任务并返回 id（201 之外的响应直接判失败）。"""
    created = client.post("/api/jobs", json=payload)
    assert created.status_code == 201, created.text
    return int(created.json()["job_id"])


def _drain(client, job_id: int) -> list[dict[str, Any]]:
    """订阅事件流直到任务结束，返回解码后的事件列表。"""
    response = client.get(f"/api/jobs/{job_id}/stream")
    assert response.status_code == 200, response.text
    events: list[dict[str, Any]] = []
    for line in response.text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload or payload == "[DONE]":
            continue
        events.append(json.loads(payload))
    assert events, "订阅应当至少收到一份快照"
    return events


def _run_to_completion(client, payload: dict[str, Any]) -> None:
    """建任务 → 订阅到结束；终态不是 completed 就当场判失败。"""
    job_id = _create_job(client, payload)
    events = _drain(client, job_id)
    summary = next(event for event in events if event["type"] == "run_summary")
    assert summary["data"]["status"] == "completed", summary


def _generation_prompt(llm: ScriptedLLM) -> str:
    """取出**生成那一次**调用收到的提示词正文。"""
    assert llm.calls, "生成没有调用模型"
    return "\n".join(str(getattr(message, "content", "")) for message in llm.calls[0])


def _all_prompts(llm: ScriptedLLM) -> str:
    """把所有调用收到的正文拼成一段（PRD 分章节链路一次生成会调很多次）。"""
    assert llm.calls, "生成没有调用模型"
    return "\n".join(
        str(getattr(message, "content", ""))
        for call in llm.calls
        for message in call
    )


def _sse_payloads(response) -> list[dict[str, Any]]:
    """把一次 SSE 响应拆成事件列表（旧的非任务通道用）。"""
    events: list[dict[str, Any]] = []
    for line in response.text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload or payload == "[DONE]":
            continue
        events.append(json.loads(payload))
    return events


def _assert_rejected(client, payload: dict[str, Any], *fragments: str) -> str:
    """断言请求被 400 拒掉、文案说清了缺什么，且**没有**留下一个空跑的任务。"""
    response = client.post("/api/jobs", json=payload)
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["code"] == "INVALID_REQUEST"
    detail = str(body["detail"])
    for fragment in fragments:
        assert fragment in detail, detail
    listed = client.get("/api/jobs")
    assert listed.status_code == 200
    assert listed.json()["jobs"] == [], "被拒的请求不该在任务列表里留下一条记录"
    return detail


# ---------------------------------------------------------------------------
# 一、请求体：旧字段被忽略，而不是 422
# ---------------------------------------------------------------------------

# 旧前端会照发的那一串：上游正文 + 需求正文。
_LEGACY_FIELDS: dict[str, Any] = {
    "prd_content": "# 请求体里的 PRD",
    "api_docs_content": "# 请求体里的接口文档",
    "summary": {"product_name": "请求体里的产品", "problem": "请求体里的问题"},
    "requirements": "请求体里的需求",
    "product_name": "请求体里的产品",
    "history": [{"role": "user", "content": "请求体里的对话"}],
}


def test_legacy_request_fields_are_ignored_by_the_prd_job(client, monkeypatch):
    """旧前端多带六个字段：**照收不误**，且一个都不参与生成。

    「忽略而不是 422」是这次收口的兼容承诺：拆字段不能把还在跑旧版的客户端打断，
    但旧字段也不能再当上游——否则双真源就又复活了。
    """
    register(client)
    llm = ScriptedLLM(["章节正文"])
    _fake_service(monkeypatch, llm)
    solution_id = _create_solution(
        client,
        {
            "summary": {"product_name": "库里的产品", "problem": "库里记下的问题"},
            "quick_product_name": "库里的产品",
        },
    )

    _run_to_completion(
        client,
        {"kind": "prd", "solution_id": solution_id, **_LEGACY_FIELDS},
    )

    prompts = _all_prompts(llm)
    assert "库里记下的问题" in prompts
    assert "请求体里的需求" not in prompts
    assert "请求体里的问题" not in prompts


def test_legacy_request_fields_are_ignored_by_the_api_docs_job(client, monkeypatch):
    """接口文档同理：旧前端仍可带 `prd_content`，但上游是库里的那一份。"""
    register(client)
    llm = ScriptedLLM(["# 接口文档"])
    _fake_service(monkeypatch, llm)
    solution_id = _create_solution(client)
    _put_document(client, solution_id, "prd", "# 库里的 PRD")

    _run_to_completion(
        client,
        {"kind": "api_docs", "solution_id": solution_id, **_LEGACY_FIELDS},
    )

    prompt = _generation_prompt(llm)
    assert "# 库里的 PRD" in prompt
    assert "请求体里的 PRD" not in prompt


# ---------------------------------------------------------------------------
# 二、文档类任务必须带方案
# ---------------------------------------------------------------------------


def test_document_jobs_without_a_solution_are_rejected(client):
    """六种文档类任务缺 `solution_id` → 400，提示说明缺方案。

    方案是「输入从哪儿取」的前提：没有它就没有服务端上游可查。从前这类任务能靠
    请求体里的正文跑起来（`solution_id=None` 的逃生口），那条路随本票一起关掉。
    """
    register(client)

    payloads = (
        {"kind": "prd"},
        {"kind": "api_docs"},
        {"kind": "prompts"},
        {"kind": "optimize", "document": "# 正文", "instruction": "改一下", "doc_kind": "prd"},
        {"kind": "clarify", "document": "# 正文", "doc_kind": "prd"},
        {"kind": "auto_clarify", "document": "# 正文", "doc_kind": "prd", "pending_items": []},
    )
    for payload in payloads:
        _assert_rejected(client, payload, "方案")


# ---------------------------------------------------------------------------
# 三、材料缺失：说清缺什么，不空跑一次生成
# ---------------------------------------------------------------------------


def test_api_docs_job_without_prd_material_is_rejected(client):
    """方案里既没有 PRD 当前版本、也没有保存的录入文本 → 400。

    「导入入口的材料还没落库」在这里表现为同一个判据：方案行上的录入列为空。
    提示要能直接指导下一步——先去生成 PRD，或先在导入页保存材料。
    """
    register(client)
    solution_id = _create_solution(client, {"entry_mode": "prompt-debug"})

    _assert_rejected(
        client,
        {"kind": "api_docs", "solution_id": solution_id},
        "PRD",
        "请先保存材料",
    )


def test_import_entry_prompts_job_without_saved_material_is_rejected(client):
    """导入入口（我有 PRD）没保存材料就点生成 → 400，提示「请先保存材料」。

    旧前端会把贴入的正文随请求体递上来，那条路已经没有兜底：材料必须先在方案上。
    """
    register(client)
    solution_id = _create_solution(client, {"entry_mode": "prompt-debug"})

    _assert_rejected(
        client,
        {"kind": "prompts", "solution_id": solution_id, "prd_content": "# 贴入的 PRD"},
        "PRD",
        "请先保存材料",
    )


def test_prd_job_without_requirement_input_is_rejected(client):
    """方案上既没有结构化摘要、也没有自由文本需求 → 400，提示说清缺需求输入。"""
    register(client)
    solution_id = _create_solution(client, {"entry_mode": "structured"})

    _assert_rejected(client, {"kind": "prd", "solution_id": solution_id}, "需求")


def test_prompts_job_only_needs_the_prd(client, monkeypatch):
    """接口文档是**可选**材料：只有 PRD 也能生成提示词（与从前口径一致）。"""
    register(client)
    llm = ScriptedLLM(["# 提示词套件"])
    _fake_service(monkeypatch, llm)
    solution_id = _create_solution(
        client,
        {"entry_mode": "prd-and-api-paste", "paste_prd_text": "# 贴入的 PRD"},
    )

    _run_to_completion(client, {"kind": "prompts", "solution_id": solution_id})

    assert "# 贴入的 PRD" in _generation_prompt(llm)
    assert _documents(client, solution_id)["prompts"]["content"] == "# 提示词套件"


# ---------------------------------------------------------------------------
# 四、旧的非任务生成通道：改收方案
# ---------------------------------------------------------------------------


def test_legacy_api_docs_channel_takes_a_solution(client, monkeypatch):
    """`/generate-api-docs-stream` 只收 `solution_id`，PRD 由服务端取。"""
    register(client)
    llm = ScriptedLLM(["# 接口文档"])
    _fake_service(monkeypatch, llm)
    solution_id = _create_solution(client)
    _put_document(client, solution_id, "prd", "# 库里的 PRD")

    response = client.post(
        "/api/conversation/generate-api-docs-stream",
        json={"solution_id": solution_id},
    )
    assert response.status_code == 200, response.text
    events = _sse_payloads(response)
    assert any(event["type"] == "done" for event in events), events
    assert "# 库里的 PRD" in _generation_prompt(llm)


def test_legacy_api_docs_channel_no_longer_accepts_plain_text(client):
    """旧通道不再收上游正文：只给 `prd_content` 的请求连门都进不去（422）。"""
    register(client)

    response = client.post(
        "/api/conversation/generate-api-docs-stream",
        json={"prd_content": "# 前端手里的 PRD"},
    )
    assert response.status_code == 422, response.text


def test_legacy_prd_channel_takes_a_solution(client, monkeypatch):
    """`/generate-prd-from-summary-stream` 也只收方案：需求输入由服务端按方案取。

    它与 `/generate-prd-stream` 是同一族的两条旧通道（一条吃摘要 / 自由文本，一条吃
    拼好的需求文本），此前都收需求正文。两条一起改收方案之后，「请求体里不再有上游
    与需求正文」这条契约才真正没有例外——否则同一份需求有两份真源，只是换了一条路由。
    """
    register(client)
    llm = ScriptedLLM(["# 库里的需求写出来的 PRD"])
    _fake_service(monkeypatch, llm)
    solution_id = _create_solution(client, {"quick_requirements": "做一个演示产品"})

    response = client.post(
        "/api/conversation/generate-prd-from-summary-stream",
        json={"solution_id": solution_id},
    )
    assert response.status_code == 200, response.text
    events = _sse_payloads(response)
    assert any(event["type"] == "done" for event in events), events
    # 需求文本取自方案（库里那一份），不是请求体
    assert "做一个演示产品" in _all_prompts(llm)


def test_legacy_prd_channel_no_longer_accepts_requirement_text(client):
    """旧字段不再被接收：只给 `summary` / `requirements` 的请求连门都进不去（422）。"""
    register(client)

    response = client.post(
        "/api/conversation/generate-prd-from-summary-stream",
        json={"summary": {"product_name": "演示产品"}, "requirements": "前端手里的那段话"},
    )
    assert response.status_code == 422, response.text


def test_legacy_prd_stream_channel_takes_a_solution(client, monkeypatch):
    """`/generate-prd-stream` 同样是「只点名方案」：需求文本由服务端按方案取。

    这条通道比 `/generate-prd-from-summary-stream` 更老：它从前收的是**已经拼好的
    需求文本**（`requirements` + `history`）。与后者一起改收方案之后，四条非任务通道
    才真正同形——C-R3「生成输入只来自服务端」也就不再有一条能绕过库的路。
    """
    register(client)
    llm = ScriptedLLM(["# 库里的需求写出来的 PRD"])
    _fake_service(monkeypatch, llm)
    solution_id = _create_solution(client, {"quick_requirements": "做一个演示产品"})

    response = client.post(
        "/api/conversation/generate-prd-stream",
        json={"solution_id": solution_id},
    )
    assert response.status_code == 200, response.text
    events = _sse_payloads(response)
    assert any(event["type"] == "done" for event in events), events
    # 需求文本取自方案（库里那一份），不是请求体
    assert "做一个演示产品" in _all_prompts(llm)


def test_legacy_prd_stream_channel_no_longer_accepts_requirement_text(client):
    """`requirements` 不再是这条通道的入参：只给它的请求连门都进不去（422）。"""
    register(client)

    response = client.post(
        "/api/conversation/generate-prd-stream",
        json={"requirements": "前端手里的那段话"},
    )
    assert response.status_code == 422, response.text


def test_legacy_prompts_channel_takes_a_solution(client, monkeypatch):
    """`/generate-prompts-stream` 同样改收方案，两份上游都由服务端取。"""
    register(client)
    llm = ScriptedLLM(["# 提示词套件"])
    _fake_service(monkeypatch, llm)
    solution_id = _create_solution(client)
    _put_document(client, solution_id, "prd", "# 库里的 PRD")
    _put_document(client, solution_id, "api_docs", "# 库里的接口文档")

    response = client.post(
        "/api/conversation/generate-prompts-stream",
        json={"solution_id": solution_id},
    )
    assert response.status_code == 200, response.text
    events = _sse_payloads(response)
    assert any(event["type"] == "done" for event in events), events
    prompt = _generation_prompt(llm)
    assert "# 库里的 PRD" in prompt
    assert "# 库里的接口文档" in prompt


# ---------------------------------------------------------------------------
# 五、按澄清结果改写：对话由服务端按线程取
# ---------------------------------------------------------------------------


def test_clarify_takes_its_conversation_from_the_saved_thread(client, monkeypatch):
    """`clarify` 不再收 `history`：对话从库里的那条文档线程读。

    请求体里照旧带一份 `history` 也不会被采纳——旧前端不至于 422，但它说的不算。
    """
    register(client)
    llm = ScriptedLLM(["# 改写后的 PRD"])
    _fake_service(monkeypatch, llm)
    solution_id = _create_solution(client)
    _seed_thread(
        solution_id,
        "prd-clarify",
        [
            ("assistant", "分页上限取多少？"),
            ("user", "就定 50 条。"),
        ],
    )

    _run_to_completion(
        client,
        {
            "kind": "clarify",
            "solution_id": solution_id,
            "document": "# 演示产品 PRD\n\n分页上限：待确认",
            "doc_kind": "prd",
            "history": [{"role": "user", "content": "请求体里的对话"}],
        },
    )

    prompt = _generation_prompt(llm)
    assert "就定 50 条。" in prompt
    assert "请求体里的对话" not in prompt


def test_clarify_without_a_conversation_is_rejected(client):
    """那条文档线程一条轮次都没有 → 400：没有「用户答了什么」，无从改写。"""
    register(client)
    solution_id = _create_solution(client)

    _assert_rejected(
        client,
        {
            "kind": "clarify",
            "solution_id": solution_id,
            "document": "# 演示产品 PRD",
            "doc_kind": "prd",
        },
        "澄清",
    )
