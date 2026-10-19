"""作答回灌（tool result）——把选择题答案作为 **ToolMessage** 对回发起提问的工具调用。

盯四件事：

1. **成对相邻**：`AIMessage(tool_calls=...)` 之后紧跟它的 `ToolMessage`，
   中间隔着格式重申之类的系统消息也要插对位置（上游对相邻成对有硬要求）；
2. **作答轮不重复投喂人话**：带 `tool_result` 时不再追加 `HumanMessage`，
   同一份作答不能在历史里出现两次；
3. **自由文本兜住悬空调用**：用户没点卡片、直接打字时，那段文本包成工具应答，
   不能留下「调了工具却没人应答」的结构；
4. **配不上就降级**：坏 `tool_call_id` 一律降级成普通用户消息，
   绝不发一条没有对应 `tool_call` 的 `ToolMessage`。
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolCallChunk,
    ToolMessage,
)

from backend.api.schemas import (
    ClarificationAnswerItem,
    ConversationContinueRequest,
    MessageItem,
    ToolResultPayload,
)
from backend.core.contracts import StreamEvent
from backend.services.clarification_ask import ASK_TOOL_NAME
from backend.services.conversation_service import _build_lc_messages, ConversationService


# ---------------------------------------------------------------------------
# 测试装配
# ---------------------------------------------------------------------------


_SYSTEM = "你是一个需求澄清助手。"


def _ask_call(call_id: str = "call-1") -> dict[str, Any]:
    """一条最小的提问工具调用（落进历史的 assistant.tool_calls 形状）。"""
    return {
        "id": call_id,
        "name": ASK_TOOL_NAME,
        "args": {
            "questions": [
                {
                    "question": "第一版要不要做离线同步？",
                    "options": [{"text": "第一版就做离线同步"}, {"text": "第一版只做在线"}],
                }
            ]
        },
    }


def _answer_payload(call_id: str = "call-1") -> dict[str, Any]:
    """一份结构化作答（`ToolResultPayload.model_dump()` 的形状）。"""
    return {
        "tool_call_id": call_id,
        "answers": [
            {
                "ask_id": "ask-1",
                "labels": ["A"],
                "question": "第一版要不要做离线同步？",
                "texts": ["第一版就做离线同步"],
            }
        ],
    }


def _tool_chunks(questions: object) -> list[ToolCallChunk]:
    """构造一条 ask 工具调用的流式分片。"""
    import json

    return [
        ToolCallChunk(
            name=ASK_TOOL_NAME,
            args=json.dumps({"questions": questions}, ensure_ascii=False),
            id="call-1",
            index=0,
        )
    ]


_ASKS = [
    {
        "question": "第一版要不要做离线同步？",
        "background": "你提到用户常在弱网环境使用。",
        "options": [{"text": "第一版就做离线同步"}, {"text": "第一版只做在线"}],
    }
]

_PROSE = "先说我对这个产品的理解：一个给团队用的周报工具。\n\n你更倾向哪种做法？"


class _TurnLLM:
    """按「第几次调用」逐轮放不同脚本的假模型。

    提问轮吐正文 + tool_call 分片，作答轮吐纯正文——正好复现
    「先提问、后收工具应答」的两轮节奏。
    """

    def __init__(self, *turns: tuple[str, list[ToolCallChunk] | None]) -> None:
        self._turns = turns
        self.calls: list[list[BaseMessage]] = []

    def bind_tools(self, tools, *, tool_choice=None, parallel_tool_calls=None, **kwargs):  # noqa: ANN001, ANN003
        return self

    async def astream(self, messages: list[BaseMessage], **kwargs) -> AsyncIterator[AIMessageChunk]:  # noqa: ANN003
        self.calls.append(list(messages))
        reply, chunks = self._turns[len(self.calls) - 1]
        if reply:
            yield AIMessageChunk(content=reply)
        if chunks:
            yield AIMessageChunk(content="", tool_call_chunks=chunks)


def _drain(events: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    """同步跑完一条事件流并收集全部事件。"""

    async def run() -> list[StreamEvent]:
        return [event async for event in events]

    return asyncio.run(run())


def _done(events: list[StreamEvent]) -> StreamEvent:
    """取收尾事件，顺带断言它确实发出去了。"""
    last = events[-1]
    assert last.type == "done", f"最后一个事件应当是 done，实际是 {last.type}"
    return last


# ---------------------------------------------------------------------------
# _build_lc_messages：工具链组装
# ---------------------------------------------------------------------------


def test_tool_result_attaches_next_to_the_tool_call() -> None:
    """作答轮：ToolMessage 紧跟发起调用的 AIMessage，且带自解释的结构化答案。"""
    messages = _build_lc_messages(
        _SYSTEM,
        history=[
            {"role": "user", "content": "做一个周报工具"},
            {"role": "assistant", "content": _PROSE, "tool_calls": [_ask_call()]},
        ],
        user_message="1. 第一版要不要做离线同步？\n   答：A. 第一版就做离线同步",
        tool_result=_answer_payload(),
    )

    ai_at = next(i for i, m in enumerate(messages) if isinstance(m, AIMessage) and m.tool_calls)
    tool = messages[ai_at + 1]
    assert isinstance(tool, ToolMessage)
    assert tool.tool_call_id == "call-1"
    # 自解释：答案正文里能直接看懂选了什么，不用回头翻工具调用参数
    body = str(tool.content)
    assert "ask-1" in body
    assert "labels" in body
    assert "第一版就做离线同步" in body


def test_tool_result_does_not_duplicate_human_message() -> None:
    """作答轮不再投喂人话——展示文本只进 ToolMessage，不能又来一条 HumanMessage。"""
    display = "1. 第一版要不要做离线同步？\n   答：A. 第一版就做离线同步"
    messages = _build_lc_messages(
        _SYSTEM,
        history=[
            {"role": "assistant", "content": _PROSE, "tool_calls": [_ask_call()]},
        ],
        user_message=display,
        tool_result=_answer_payload(),
    )

    assert not any(isinstance(m, HumanMessage) and m.content == display for m in messages)
    assert any(isinstance(m, ToolMessage) for m in messages)


def test_tool_answer_stays_adjacent_despite_trailing_system() -> None:
    """格式重申排在悬空调用后面时，应答要插进成对位置、而不是追加到列表尾。"""
    messages = _build_lc_messages(
        _SYSTEM,
        history=[
            {"role": "assistant", "content": "", "tool_calls": [_ask_call()]},
            {"role": "system", "content": "请继续用工具提问。"},
        ],
        user_message="答：A",
        tool_result=_answer_payload(),
    )

    ai_at = next(i for i, m in enumerate(messages) if isinstance(m, AIMessage) and m.tool_calls)
    assert isinstance(messages[ai_at + 1], ToolMessage)
    # 系统消息仍然留在末尾，没有被工具应答顶掉
    assert isinstance(messages[-1], SystemMessage)
    assert messages[-1].content == "请继续用工具提问。"


def test_history_tool_entry_round_trips_into_tool_message() -> None:
    """历史里的 `tool` 条目（落库形状）下一轮原样转回 ToolMessage。"""
    messages = _build_lc_messages(
        _SYSTEM,
        history=[
            {"role": "assistant", "content": _PROSE, "tool_calls": [_ask_call()]},
            {
                "role": "tool",
                "content": "1. 答：A. 第一版就做离线同步",
                "tool_call_id": "call-1",
                "tool_result": _answer_payload(),
            },
            {"role": "assistant", "content": "好，那就先做在线。"},
        ],
        user_message="下一题呢？",
    )

    kinds = [type(m) for m in messages]
    assert kinds == [SystemMessage, AIMessage, ToolMessage, AIMessage, HumanMessage]
    tool = messages[2]
    assert isinstance(tool, ToolMessage)
    assert tool.tool_call_id == "call-1"


def test_free_text_answers_pending_tool_call() -> None:
    """没点卡片、直接打字：那段文本包成工具应答，补上调用之后的空缺。"""
    messages = _build_lc_messages(
        _SYSTEM,
        history=[
            {"role": "assistant", "content": _PROSE, "tool_calls": [_ask_call()]},
        ],
        user_message="先做在线吧。",
    )

    ai_at = next(i for i, m in enumerate(messages) if isinstance(m, AIMessage) and m.tool_calls)
    tool = messages[ai_at + 1]
    assert isinstance(tool, ToolMessage)
    assert tool.content == "先做在线吧。"
    # 同一段文本不能再以 HumanMessage 出现一次
    assert not any(isinstance(m, HumanMessage) and m.content == "先做在线吧。" for m in messages)


def test_free_text_in_history_wraps_pending_calls() -> None:
    """历史里的自由文本作答（旧前端 / 降级路径）同样要包成工具应答。"""
    messages = _build_lc_messages(
        _SYSTEM,
        history=[
            {"role": "assistant", "content": "", "tool_calls": [_ask_call()]},
            {"role": "user", "content": "先做在线吧。"},
            {"role": "assistant", "content": "明白。"},
        ],
        user_message="继续",
    )

    tools = [m for m in messages if isinstance(m, ToolMessage)]
    assert len(tools) == 1
    assert tools[0].content == "先做在线吧。"
    ai_at = next(i for i, m in enumerate(messages) if isinstance(m, AIMessage) and m.tool_calls)
    assert messages[ai_at + 1] is tools[0]


def test_orphan_tool_entry_degrades_to_human_message() -> None:
    """配不上调用的工具应答降级成普通用户文本，绝不发孤 ToolMessage。"""
    messages = _build_lc_messages(
        _SYSTEM,
        history=[
            {"role": "tool", "content": "孤儿应答", "tool_call_id": "ghost"},
        ],
        user_message="继续",
    )

    assert not any(isinstance(m, ToolMessage) for m in messages)
    assert any(isinstance(m, HumanMessage) and m.content == "孤儿应答" for m in messages)


def test_unmatched_tool_result_degrades_to_human_message() -> None:
    """本轮 tool_result 对不上任何悬空调用（旧快照没存 tool_calls）时降级成人话。"""
    messages = _build_lc_messages(
        _SYSTEM,
        history=[{"role": "user", "content": "做一个周报工具"}],
        user_message="答：A",
        tool_result={"tool_call_id": "ghost", "answers": []},
    )

    assert not any(isinstance(m, ToolMessage) for m in messages)
    assert isinstance(messages[-1], HumanMessage)
    assert messages[-1].content == "答：A"


def test_plain_turns_are_unchanged() -> None:
    """不带工具链的普通对话原样组装——别把好路走坏。"""
    messages = _build_lc_messages(
        _SYSTEM,
        history=[
            {"role": "user", "content": "做一个周报工具"},
            {"role": "assistant", "content": "好的，先问几个关键问题。"},
        ],
        user_message="继续",
    )

    assert [type(m) for m in messages] == [SystemMessage, HumanMessage, AIMessage, HumanMessage]


# ---------------------------------------------------------------------------
# 服务级：两轮往返（提问 → 作答）
# ---------------------------------------------------------------------------


def test_done_carries_tool_call_id_for_answer_pairing() -> None:
    """`done` 顶层带 tool_call_id，题目上也带——作答侧靠它定位该回给哪次调用。"""
    llm = _TurnLLM((_PROSE, _tool_chunks(_ASKS)), ("好。", None))
    service = ConversationService(llm=llm)

    done = _done(_drain(service.start_conversation_stream(form_data={"product_name": "周报助手"})))

    assert done.data["tool_call_id"] == "call-1"
    assert done.data["clarification_asks"][0]["tool_call_id"] == "call-1"


def test_answer_turn_feeds_tool_message_not_human_message() -> None:
    """完整往返：第二轮请求里 ToolMessage 紧跟提问调用，且不重复投喂人话。"""
    llm = _TurnLLM((_PROSE, _tool_chunks(_ASKS)), ("好，第一版就做离线同步。", None))
    service = ConversationService(llm=llm)

    started = _drain(service.start_conversation_stream(form_data={"product_name": "周报助手"}))
    conversation_id = str(started[0].data["conversation_id"])
    display = "1. 第一版要不要做离线同步？\n   答：A. 第一版就做离线同步"

    _drain(
        service.continue_conversation_stream(
            user_message=display,
            conversation_id=conversation_id,
            tool_result=_answer_payload(),
        )
    )

    sent = llm.calls[1]
    ai_at = next(i for i, m in enumerate(sent) if isinstance(m, AIMessage) and m.tool_calls)
    tool = sent[ai_at + 1]
    assert isinstance(tool, ToolMessage)
    assert tool.tool_call_id == "call-1"
    assert "第一版就做离线同步" in str(tool.content)
    assert not any(isinstance(m, HumanMessage) and m.content == display for m in sent)


def test_answer_turn_persists_tool_entry_without_duplicate_user() -> None:
    """落库形状：tool 条目插在提问调用之后，不再另记一条 user 消息。"""
    llm = _TurnLLM((_PROSE, _tool_chunks(_ASKS)), ("好。", None))
    service = ConversationService(llm=llm)

    started = _drain(service.start_conversation_stream(form_data={"product_name": "周报助手"}))
    conversation_id = str(started[0].data["conversation_id"])
    display = "1. 答：A. 第一版就做离线同步"

    _drain(
        service.continue_conversation_stream(
            user_message=display,
            conversation_id=conversation_id,
            tool_result=_answer_payload(),
        )
    )

    record = service._conversations[conversation_id]
    roles = [item["role"] for item in record.messages]
    # user(开场) → assistant(提问) → tool(作答) → assistant(收答)
    assert roles == ["user", "assistant", "tool", "assistant"]
    entry = record.messages[2]
    assert entry["tool_call_id"] == "call-1"
    assert entry["tool_result"] == _answer_payload()
    # 提问调用本体也落了库——作答要靠它配对
    assert record.messages[1]["tool_calls"][0]["id"] == "call-1"
    assert not any(item.get("content") == display and item["role"] == "user" for item in record.messages)


def test_free_text_answer_still_round_trips() -> None:
    """自由文本作答：第二轮照样把文本包成工具应答，不能留悬空调用。"""
    llm = _TurnLLM((_PROSE, _tool_chunks(_ASKS)), ("明白，按在线做。", None))
    service = ConversationService(llm=llm)

    started = _drain(service.start_conversation_stream(form_data={"product_name": "周报助手"}))
    conversation_id = str(started[0].data["conversation_id"])

    _drain(
        service.continue_conversation_stream(
            user_message="先做在线吧。",
            conversation_id=conversation_id,
        )
    )

    sent = llm.calls[1]
    ai_at = next(i for i, m in enumerate(sent) if isinstance(m, AIMessage) and m.tool_calls)
    tool = sent[ai_at + 1]
    assert isinstance(tool, ToolMessage)
    assert tool.content == "先做在线吧。"
    assert not any(isinstance(m, HumanMessage) and m.content == "先做在线吧。" for m in sent)


# ---------------------------------------------------------------------------
# 接口契约：tool 链字段的 schema
# ---------------------------------------------------------------------------


def test_message_item_carries_tool_chain_fields() -> None:
    """`MessageItem` 收得下整条工具链；普通消息的可选字段默认为空。"""
    assistant = MessageItem(role="assistant", content="", tool_calls=[_ask_call()])
    assert assistant.tool_calls[0]["id"] == "call-1"

    tool = MessageItem(
        role="tool",
        content="1. 答：A",
        tool_call_id="call-1",
        tool_result=ToolResultPayload(
            tool_call_id="call-1",
            answers=[
                ClarificationAnswerItem(
                    ask_id="ask-1",
                    labels=["A"],
                    question="第一版要不要做离线同步？",
                    texts=["第一版就做离线同步"],
                )
            ],
        ),
    )
    assert tool.tool_result is not None
    assert tool.tool_result.answers[0].labels == ["A"]

    plain = MessageItem(role="user", content="继续")
    assert plain.tool_calls == []
    assert plain.tool_call_id == ""
    assert plain.tool_result is None


def test_continue_request_accepts_tool_result() -> None:
    """`tool_result` 可带可缺：带上是结构化作答回灌，缺省就是普通续聊。"""
    payload = ConversationContinueRequest(content="答：A", tool_result=ToolResultPayload(**_answer_payload()))
    assert payload.tool_result is not None
    assert payload.tool_result.tool_call_id == "call-1"
    assert payload.tool_result.answers[0].texts == ["第一版就做离线同步"]

    plain = ConversationContinueRequest(content="继续")
    assert plain.tool_result is None


def test_route_history_mapping_keeps_tool_chain_fields() -> None:
    """路由的历史映射不能裁掉工具链字段——裁了就只剩悬空的 tool_call。"""
    from backend.api.conversation import _message_to_history_item

    assistant = _message_to_history_item(MessageItem(role="assistant", content=_PROSE, tool_calls=[_ask_call()]))
    assert assistant["tool_calls"][0]["id"] == "call-1"

    tool = _message_to_history_item(
        MessageItem(
            role="tool",
            content="1. 答：A",
            tool_call_id="call-1",
            tool_result=ToolResultPayload(**_answer_payload()),
        )
    )
    assert tool["tool_call_id"] == "call-1"
    assert tool["tool_result"] == _answer_payload()

    # 普通消息不带空壳键：可选字段只在真有值时出现
    plain = _message_to_history_item(MessageItem(role="user", content="继续"))
    assert plain == {"role": "user", "content": "继续"}
