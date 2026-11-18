"""对话里「只提问、不说话」那一轮的落库契约。

2026-11-18 线上事故：AI 追问一轮时模型只发了一个 `ask_clarification_questions`
工具调用、正文为空。用户当场看到题目卡片；刷新后卡片消失——因为收尾时
`_write_chat_turn` 用「正文为空就跳过」当判据，把**这一轮的全部可见内容**
（题目）一起丢了。

这条用例把判据钉死：这一轮有没有**用户可见内容**（正文 / 题目 / 工具调用），
而不是「正文是不是空串」。零 token：对话服务换成注入了脚本模型的实例。
"""

from __future__ import annotations

import json

import pytest
from conftest import register, users

from backend.core.engine import session_scope
from backend.repositories.chat_turn_repo import ChatTurnRepository
from backend.services import conversation_service, job_runtime
from backend.services.clarification_ask import ASK_TOOL_NAME
from fakes import ScriptedLLM
from ticket_support import run_ticket

_ASKS = [
    {
        "question": "表单里哪些字段是必填的？",
        "background": "必填规则直接影响表单能不能提交。",
        "options": [{"text": "基础必填、高级选填"}, {"text": "全部必填"}],
    }
]


def _tool_chunks(questions: object) -> list:
    """构造一条 ask 工具调用分片。"""
    from langchain_core.messages.tool import ToolCallChunk

    args = json.dumps({"questions": questions}, ensure_ascii=False)
    return [ToolCallChunk(name=ASK_TOOL_NAME, args=args, id="call-1", index=0)]


@pytest.fixture(autouse=True)
def clean_job_state():
    job_runtime._RUNTIMES.clear()  # noqa: SLF001
    job_runtime._TASKS.clear()  # noqa: SLF001
    yield
    job_runtime._RUNTIMES.clear()  # noqa: SLF001
    job_runtime._TASKS.clear()  # noqa: SLF001


@pytest.fixture
def alice(client):
    register(client)
    user = users.get_by_username("alice")
    assert user is not None
    created = client.post("/api/solutions", json={"title": "只提问的一轮"})
    assert created.status_code == 201, created.text
    return int(user["id"]), int(created.json()["id"])


def test_tool_only_round_is_persisted_as_a_turn(client, alice, monkeypatch):
    """模型只提问、不写正文：这一轮仍要落成一条 assistant 轮次（带题目）。"""
    owner_id, solution_id = alice
    monkeypatch.setattr(
        conversation_service,
        "get_service",
        lambda: conversation_service.ConversationService(
            llm=ScriptedLLM([], tool_call_chunks=_tool_chunks(_ASKS))
        ),
    )

    run_ticket(
        owner_id=owner_id,
        solution_id=solution_id,
        kind="chat",
        request={"stage": "requirement", "chat_mode": "start", "form_data": {}},
    )

    with session_scope() as session:
        turns = ChatTurnRepository(session).list_thread(owner_id, solution_id, "requirement")

    assistant = [turn for turn in turns if turn.role == "assistant"]
    assert assistant, f"只提问的一轮没有落库，刷新后题目就消失了（实际轮次：{[t.role for t in turns]}）"
    # 存的是存储形状（`asks` 为 JSON 文本），展示形状要经 `to_display_turn` 投影——
    # 前端拿到的就是它，所以断言直接盯用户能看见的那一份。
    from backend.repositories.chat_turn_repo import to_display_turn

    display = to_display_turn(assistant[-1])
    assert display.get("asks"), "轮次落了但题目丢了：刷新后卡片是空的"
    assert display["asks"][0]["question"] == "表单里哪些字段是必填的？"
