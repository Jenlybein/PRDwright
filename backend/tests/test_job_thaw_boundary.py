"""任务单「深冻结」跨出对外边界时的深还原（票 #37 生产切换前置修复）。

单① / 单② 本身必须保持 frozen + 深冻结（规格要求：不可变数据不产生第二份真相）。
但把单里的数据递给「外部 API」时，必须递归还原成普通 dict / list：

| 对外边界 | 收到的数据 | 浅还原的后果 |
| --- | --- | --- |
| `as_event_request` → 事件流 | `pending_items[].sections` | tuple 让 `isinstance(sections, list)` 为假，提示词静默丢「影响章节」 |
| 登记写 DB 投影 | `request_params` | `json.dumps(default=str)` 把 mappingproxy 存成坏字符串 |
| 登记写 chat 输入轮次 | `chat_tool_result.answers` | `json.dumps` 直接抛 `TypeError`，整笔登记失败 |

本文件只跨「造单 → 登记 → 派发 → 订阅」公开主接缝断言外部可观察结果
（提示词里有没有那一段、库行能不能按原结构读回、任务能不能收尾），不探内部状态。
所有生成链路走离线假 LLM，零 token。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from conftest import register, users
from dbsupport import connect

from backend.core.engine import session_scope
from backend.repositories.chat_turn_repo import ChatTurnRepository
from backend.services import (
    conversation_service,
    document_service,
    job_service,
    job_runtime,
    llm_factory,
    prd_section_writer,
)
from backend.services.job_bus import job_bus
from fakes import ScriptedLLM


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
    """注册 alice 并返回其 id（顺带保证 DB 已初始化）。"""
    register(client)
    return _alice_id()


@pytest.fixture(autouse=True)
def clean_job_state():
    """清空进程内运行时、任务集合与总线订阅，避免用例互相污染。"""
    job_runtime._RUNTIMES.clear()  # noqa: SLF001
    job_runtime._TASKS.clear()  # noqa: SLF001
    job_bus._subscribers.clear()  # noqa: SLF001
    yield
    job_runtime._RUNTIMES.clear()  # noqa: SLF001
    job_runtime._TASKS.clear()  # noqa: SLF001
    job_bus._subscribers.clear()  # noqa: SLF001


@pytest.fixture(autouse=True)
def no_real_llm(monkeypatch):
    """真实 LLM 工厂一被调用就抛，杜绝任何测试触网。"""

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("测试不应触网调用真实 LLM")

    monkeypatch.setattr(llm_factory, "get_llm", _boom)
    monkeypatch.setattr(document_service, "get_llm", _boom)
    monkeypatch.setattr(prd_section_writer, "get_llm", _boom)


def _seed_solution(client, title: str = "深还原方案") -> int:
    """走既有路由建一份真实方案，返回 id。"""
    created = client.post("/api/solutions", json={"title": title})
    assert created.status_code == 201, created.text
    return int(created.json()["id"])


def _use_document_llm(monkeypatch, llm: ScriptedLLM) -> None:
    """把模块级文档服务换成注入假 LLM 的实例。"""
    monkeypatch.setattr(
        document_service, "get_service", lambda: document_service.DocumentService(llm=llm)
    )


def _use_chat_llm(monkeypatch, pieces: list[str]) -> None:
    """把模块级对话服务换成注入假 LLM 的实例。"""
    monkeypatch.setattr(
        conversation_service,
        "get_service",
        lambda: conversation_service.ConversationService(llm=ScriptedLLM(pieces)),
    )


def _three_steps(owner_id: int, request: dict[str, Any], **kwargs: Any):
    """走公开三步：造单① → 登记单② → 派发 → 订阅到结束。"""
    from backend.services import job_ticket

    async def scenario():
        draft = job_ticket.build_job_ticket(
            owner_id=owner_id,
            solution_id=kwargs.get("solution_id"),
            kind=kwargs.get("kind", "prd"),
            request=request,
            **kwargs.get("build_options", {}),
        )
        registered = job_ticket.register_job_ticket(draft)
        job_ticket.start_registered_job(registered)
        events = [event async for event in job_service.open_stream(owner_id, registered.job_id)]
        return draft, registered, events

    return asyncio.run(scenario())


def _projection(job_id: int) -> dict[str, Any]:
    """按 id 直读 DB 行的 `request_params`（不可变数据的完整投影）。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT request_params FROM generation_jobs WHERE id = ?", (job_id,)
        ).fetchone()
    assert row is not None
    return json.loads(row["request_params"])


def _prompt_text(llm: ScriptedLLM) -> str:
    """把假 LLM 收到的所有消息正文拼成一段文本。"""
    return "\n".join(
        str(getattr(message, "content", "")) for call in llm.calls for message in call
    )


# ---------------------------------------------------------------------------
# 一、auto_clarify：待确认清单里的嵌套列表不能变成 tuple
# ---------------------------------------------------------------------------


def test_nested_pending_items_reach_the_prompt_and_the_db_projection(alice, monkeypatch):
    """`pending_items[].sections` 必须是 list：提示词带「影响章节」，DB 投影可原样读回。

    深冻结把 list 变 tuple 之后，`format_pending_items` 的 `isinstance(sections, list)`
    会静默返回假——不报错，只是模型再也看不到「这条问题影响哪几章」。
    """
    llm = ScriptedLLM(["# PRD\n\n改写后的正文"])
    _use_document_llm(monkeypatch, llm)
    request = {
        "document": "# PRD\n\n原文",
        "doc_kind": "prd",
        "pending_items": [
            {
                "question": "分页上限取多少？",
                "sections": ["4.3", "5.1"],
                "decider": "产品负责人",
                "importance": "P0",
            }
        ],
    }

    draft, registered, events = _three_steps(alice, request, kind="auto_clarify")

    # 单① 仍是深冻结的不可变对象：修复只发生在「对外交付」那一层。
    assert draft is not registered
    assert "影响章节：4.3、5.1" in _prompt_text(llm), _prompt_text(llm)
    assert events[-1].type == "run_summary"
    assert _projection(registered.job_id)["pending_items"][0]["sections"] == ["4.3", "5.1"]


# ---------------------------------------------------------------------------
# 二、chat：tool_result 里嵌 answers，登记写轮次不许炸
# ---------------------------------------------------------------------------


def test_chat_tool_result_with_nested_answers_survives_three_steps(client, alice, monkeypatch):
    """`tool_result.answers=[{...}]` 必须能写进 chat 轮次，整笔登记不能抛 TypeError。

    `ChatTurnRepository._dumps` 用 `json.dumps`（没有 `default=`）：深冻结留下来的
    mappingproxy 会当场抛 `TypeError: Object of type mappingproxy is not JSON
    serializable`，结果是「用户点了结构化作答、服务端 500、任务根本没有」。
    """
    solution_id = _seed_solution(client)
    _use_chat_llm(monkeypatch, ["收到，按你选的来。"])
    answers = [{"question": "目标用户", "texts": ["运营"]}]
    request = {
        "stage": "requirement",
        "chat_mode": "continue",
        "content": "第 1 题：A",
        "tool_result": {
            "tool_call_id": "call-1",
            "answers": answers,
        },
    }

    draft, registered, events = _three_steps(
        alice, request, kind="chat", solution_id=solution_id
    )

    assert draft is not registered
    assert registered.input_turn_id > 0
    assert events[-1].type == "run_summary"
    assert _projection(registered.job_id)["tool_result"]["answers"] == answers

    with session_scope() as session:
        turns = ChatTurnRepository(session).list_thread(alice, solution_id, "requirement")
    stored_tool = next(turn for turn in turns if turn.role == "tool")
    assert json.loads(stored_tool.tool_result)["answers"] == answers
