"""生产任务路由切到「造单 → 登记 → 派发」三步（票 #37 迁移批次一）。

本文件只跨 `POST /api/jobs` 与它的下游公开出口断言：

| 接缝 | 断言的可观察行为 |
| --- | --- |
| `POST /api/jobs` | 只造一次单、登记一次、派发一次；旧 `create_job` / `dispatch` 两个入口不再被生产调用 |
| `GET /api/jobs/{id}/stream` | 任务真的被派发，首帧仍是 snapshot（流式事件形状不变） |
| `generation_jobs` 行 | 输入指纹（仅 prd）、`doc_kind`、请求参数投影都还在 |
| `POST /api/jobs`（chat + tool_result） | 嵌套结构能一路走完登记与轮次落库（深还原的生产路径） |

前置语义（参数校验 / 配额 / 指纹 / `_build_request`）由既有用例覆盖，这里不重复。
所有生成链路走离线假 LLM，零 token。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from conftest import register, users

from backend.core.engine import session_scope
from backend.repositories.chat_turn_repo import ChatTurnRepository
from backend.services import (
    conversation_service,
    document_service,
    job_service,
    job_ticket,
    llm_factory,
    prd_section_writer,
)
from backend.services.job_bus import job_bus
from dbsupport import connect
from fakes import ScriptedLLM


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------


def _alice_id() -> int:
    """取已注册用户 alice 的 id（generation_jobs.owner_id 有外键）。"""
    user = users.get_by_username("alice")
    assert user is not None
    return int(user["id"])


@pytest.fixture(autouse=True)
def clean_job_state():
    """清空进程内运行时、任务集合与总线订阅，避免用例互相污染。"""
    job_service._RUNTIMES.clear()  # noqa: SLF001
    job_service._TASKS.clear()  # noqa: SLF001
    job_bus._subscribers.clear()  # noqa: SLF001
    yield
    job_service._RUNTIMES.clear()  # noqa: SLF001
    job_service._TASKS.clear()  # noqa: SLF001
    job_bus._subscribers.clear()  # noqa: SLF001


@pytest.fixture(autouse=True)
def no_real_llm(monkeypatch):
    """真实 LLM 工厂一被调用就抛，杜绝任何测试触网。"""

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("测试不应触网调用真实 LLM")

    monkeypatch.setattr(llm_factory, "get_llm", _boom)
    monkeypatch.setattr(document_service, "get_llm", _boom)
    monkeypatch.setattr(prd_section_writer, "get_llm", _boom)


def _fake_document_service(monkeypatch, llm: ScriptedLLM) -> None:
    """把模块级文档服务换成注入假 LLM 的实例。"""
    monkeypatch.setattr(
        document_service,
        "get_service",
        lambda: document_service.DocumentService(llm=llm),
    )


def _fake_conversation_service(monkeypatch, pieces: list[str]) -> None:
    """把模块级对话服务换成注入假 LLM 的实例。"""
    monkeypatch.setattr(
        conversation_service,
        "get_service",
        lambda: conversation_service.ConversationService(llm=ScriptedLLM(pieces)),
    )


def _spy_three_steps(monkeypatch) -> dict[str, int]:
    """给三步公开动作装计数器（仍委托原实现），并拦住两个旧入口。"""
    calls = {"build": 0, "register": 0, "start": 0, "legacy": 0}
    original = {
        name: getattr(job_ticket, name)
        for name in ("build_job_ticket", "register_job_ticket", "start_registered_job")
    }

    def build(*args, **kwargs):  # noqa: ANN002, ANN003
        calls["build"] += 1
        return original["build_job_ticket"](*args, **kwargs)

    def register_ticket(*args, **kwargs):  # noqa: ANN002, ANN003
        calls["register"] += 1
        return original["register_job_ticket"](*args, **kwargs)

    def start(*args, **kwargs):  # noqa: ANN002, ANN003
        calls["start"] += 1
        return original["start_registered_job"](*args, **kwargs)

    def legacy(*args, **kwargs):  # noqa: ANN002, ANN003
        calls["legacy"] += 1
        raise AssertionError("生产路由不该再把同一份参数交给旧的两个入口")

    monkeypatch.setattr(job_ticket, "build_job_ticket", build)
    monkeypatch.setattr(job_ticket, "register_job_ticket", register_ticket)
    monkeypatch.setattr(job_ticket, "start_registered_job", start)
    monkeypatch.setattr(job_service, "create_job", legacy)
    monkeypatch.setattr(job_service, "dispatch", legacy)
    return calls


def _row(job_id: int) -> dict[str, Any]:
    """按 id 直读整行（绕开 ORM，验证真的落盘了）。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM generation_jobs WHERE id = ?", (job_id,)
        ).fetchone()
    assert row is not None
    return dict(row)


def _seed_solution(client, title: str = "路由方案") -> int:
    """走既有路由建一份真实方案，返回 id。"""
    created = client.post("/api/solutions", json={"title": title})
    assert created.status_code == 201, created.text
    return int(created.json()["id"])


# ---------------------------------------------------------------------------
# 一、生产路由只经手一次造单
# ---------------------------------------------------------------------------


def test_production_route_builds_exactly_one_ticket(client, monkeypatch):
    """POST /api/jobs 只造一张单①、登记一次、派发一次；两个旧入口一次都不碰。

    旧形状把同一份摊平参数同时交给 `create_job` 与 `dispatch`（后者还会被
    chat 路径原地改写）。切到三步之后，参数只在造单时交出一次，回执形状不变。
    """
    register(client)
    _fake_document_service(monkeypatch, ScriptedLLM(["章节正文"]))
    calls = _spy_three_steps(monkeypatch)

    response = client.post(
        "/api/jobs",
        json={"kind": "prd", "requirements": "做一个演示产品", "product_name": "演示"},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "running"
    assert body["job_id"] > 0
    assert calls == {"build": 1, "register": 1, "start": 1, "legacy": 0}

    # 任务真的被派发：订阅流首帧仍是 snapshot（流式事件形状不变）。
    stream = client.get(f"/api/jobs/{body['job_id']}/stream")
    assert stream.status_code == 200
    payloads = [
        line[len("data:") :].strip()
        for line in stream.text.splitlines()
        if line.startswith("data:")
    ]
    first = next(payload for payload in payloads if payload and payload != "[DONE]")
    assert json.loads(first)["type"] == "snapshot"

    # 前置语义不变：仅 prd 计算输入指纹；请求参数仍是完整投影。
    row = _row(body["job_id"])
    assert row["input_fingerprint"]
    assert json.loads(row["request_params"])["requirements"] == "做一个演示产品"
    assert row["doc_kind"] == "prd"


def test_production_route_keeps_doc_kind_from_request(client, monkeypatch):
    """`doc_kind` 由请求体推导并落列：优化接口文档不能被写成 PRD。"""
    register(client)
    _fake_document_service(monkeypatch, ScriptedLLM(["优化结果"]))
    solution_id = _seed_solution(client)

    created = client.post(
        "/api/jobs",
        json={
            "kind": "optimize",
            "solution_id": solution_id,
            "document": "# 旧接口文档",
            "instruction": "改一下",
            "doc_kind": "api_docs",
        },
    )

    assert created.status_code == 201, created.text
    job_id = int(created.json()["job_id"])
    assert _row(job_id)["doc_kind"] == "api_docs"
    # 非 prd 不算指纹（与旧入口同一口径）
    assert not _row(job_id)["input_fingerprint"]
    status = client.get(f"/api/jobs/{job_id}")
    assert status.json()["doc_kind"] == "api_docs"


def test_production_chat_route_accepts_nested_tool_result(client, monkeypatch):
    """生产路由带 `tool_result.answers=[{...}]` 的对话：登记不抛，轮次按原结构落库。

    这是深还原修复在生产路径上的回归点：单里的数据是深冻结的，写 chat 输入轮次
    时若没还原成普通容器，`json.dumps(mappingproxy)` 会让这一轮直接 500。
    """
    register(client)
    _fake_conversation_service(monkeypatch, ["收到，按你选的来。"])
    solution_id = _seed_solution(client)
    answers = [{"question": "目标用户", "texts": ["运营"]}]

    created = client.post(
        "/api/jobs",
        json={
            "kind": "chat",
            "solution_id": solution_id,
            "stage": "requirement",
            "chat_mode": "continue",
            "content": "第 1 题：A",
            "tool_result": {"tool_call_id": "call-1", "answers": answers},
        },
    )

    assert created.status_code == 201, created.text
    job_id = int(created.json()["job_id"])
    # 读一次流让后台任务收尾（写入 assistant 轮次）
    assert client.get(f"/api/jobs/{job_id}/stream").status_code == 200

    row = _row(job_id)
    assert json.loads(row["request_params"])["tool_result"]["answers"] == answers

    with session_scope() as session:
        turns = ChatTurnRepository(session).list_thread(
            _alice_id(), solution_id, "requirement"
        )
    stored_tool = next(turn for turn in turns if turn.role == "tool")
    assert json.loads(stored_tool.tool_result)["answers"] == answers


# ---------------------------------------------------------------------------
# 二、排空 / 校验 / 配额的前置语义不变（本票新入口下的再确认）
# ---------------------------------------------------------------------------


def test_production_route_still_rejects_invalid_payload_before_building(client, monkeypatch):
    """参数校验仍在造单之前：不合法请求既不该造单，也不该落任务行。"""
    register(client)
    calls = _spy_three_steps(monkeypatch)

    response = client.post("/api/jobs", json={"kind": "prd"})

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_REQUEST"
    assert calls == {"build": 0, "register": 0, "start": 0, "legacy": 0}


def test_production_auto_clarify_route_keeps_pending_sections_in_the_prompt(
    client, monkeypatch
):
    """生产路由带 `pending_items[].sections` 的 auto_clarify：提示词带「影响章节」。

    深冻结若没在对外边界还原成 list，`format_pending_items` 会静默丢掉这一格；
    这是深还原修复在生产路径上的另一条回归点。
    """
    register(client)
    llm = ScriptedLLM(["# PRD\n\n改写后的正文"])
    _fake_document_service(monkeypatch, llm)

    created = client.post(
        "/api/jobs",
        json={
            "kind": "auto_clarify",
            "document": "# PRD\n\n原文",
            "doc_kind": "prd",
            "pending_items": [
                {
                    "question": "分页上限取多少？",
                    "sections": ["4.3", "5.1"],
                    "importance": "P0",
                }
            ],
        },
    )

    assert created.status_code == 201, created.text
    job_id = int(created.json()["job_id"])
    assert client.get(f"/api/jobs/{job_id}/stream").status_code == 200
    prompt = "\n".join(
        str(getattr(message, "content", "")) for call in llm.calls for message in call
    )
    assert "影响章节：4.3、5.1" in prompt
