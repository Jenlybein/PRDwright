"""对话流把「结构化选择题」从**工具调用**里解析出来并交出去。

盯四件事：

1. 用户**看到**的增量只有正文（工具调用走独立通道，不进正文）；
2. `done` 事件里既有**干净正文**、又有从 tool_calls 解析好的 `clarification_asks`；
3. 写回会话历史的也是干净正文——正文不该混进工具参数；
4. 没调工具 / 参数坏了时一切照旧（空数组 + 原文），这是常态而不是异常。
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    ToolCallChunk,
)

from backend.core.contracts import StreamEvent
from backend.services.clarification_ask import ASK_TOOL_NAME
from backend.services.conversation_service import ConversationService


class _ScriptedLLM:
    """按脚本吐字 + 可选吐 tool_call 分片的假模型。

    `bind_tools` 直接返回自己（脚本模型不在乎绑了什么工具），`astream` 先吐正文分片、
    末尾再吐 `tool_call_chunks`，模拟「模型写完正文后调用工具」。
    """

    def __init__(
        self,
        reply: str,
        tool_call_chunks: list[ToolCallChunk] | None = None,
        chunk_size: int = 5,
    ) -> None:
        self._chunks = [reply[i : i + chunk_size] for i in range(0, len(reply), chunk_size)]
        self._tool_call_chunks = tool_call_chunks or []
        self.calls: list[list[BaseMessage]] = []

    def bind_tools(self, tools, *, tool_choice=None, parallel_tool_calls=None, **kwargs):  # noqa: ANN001, ANN003
        return self

    async def astream(self, messages: list[BaseMessage], **kwargs) -> AsyncIterator[AIMessageChunk]:  # noqa: ANN003
        self.calls.append(list(messages))
        for chunk in self._chunks:
            yield AIMessageChunk(content=chunk)
        if self._tool_call_chunks:
            yield AIMessageChunk(content="", tool_call_chunks=self._tool_call_chunks)


def _drain(events: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    """同步跑完一条事件流并收集全部事件。"""

    async def run() -> list[StreamEvent]:
        return [event async for event in events]

    return asyncio.run(run())


def _tool_chunks(questions: object, *, as_json: bool = False) -> list[ToolCallChunk]:
    """构造一条 ask 工具调用的 `ToolCallChunk`（`args` 可选 JSON 串形态）。"""
    args: object = {"questions": questions}
    if as_json:
        args = json.dumps(args, ensure_ascii=False)
    return [
        ToolCallChunk(name=ASK_TOOL_NAME, args=args if isinstance(args, str) else json.dumps(args, ensure_ascii=False), id="call-1", index=0)
    ]


_ASKS = [
    {
        "question": "第一版要不要做离线同步？",
        "background": "你提到用户常在弱网环境使用。",
        "options": [{"text": "第一版就做离线同步"}, {"text": "第一版只做在线"}],
    }
]

_PROSE = "先说我对这个产品的理解：一个给团队用的周报工具。\n\n你更倾向哪种做法？"


def _visible_text(events: list[StreamEvent]) -> str:
    """用户实际会看到的正文（只有 chunk 事件承载正文）。"""
    return "".join(event.content for event in events if event.type == "chunk")


def _done(events: list[StreamEvent]) -> StreamEvent:
    """取收尾事件，顺带断言它确实发出去了。"""
    last = events[-1]
    assert last.type == "done", f"最后一个事件应当是 done，实际是 {last.type}"
    return last


def test_start_stream_extracts_asks_from_tool_call() -> None:
    """`done` 从 tool_calls 解析出题目；正文是干净的、不含工具参数。"""
    llm = _ScriptedLLM(_PROSE, _tool_chunks(_ASKS))
    service = ConversationService(llm=llm)

    events = _drain(service.start_conversation_stream(form_data={"product_name": "周报助手"}))
    done = _done(events)

    assert done.data["reply"] == _PROSE
    asks = done.data["clarification_asks"]
    assert len(asks) == 1
    assert asks[0]["question"] == "第一版要不要做离线同步？"
    assert asks[0]["background"] == "你提到用户常在弱网环境使用。"
    assert asks[0]["multiple"] is False
    assert [(item["label"], item["text"]) for item in asks[0]["options"]] == [
        ("A", "第一版就做离线同步"),
        ("B", "第一版只做在线"),
    ]


def test_start_stream_visible_text_is_prose_only() -> None:
    """正文一字不丢、也不混进工具参数——工具调用走独立通道。"""
    llm = _ScriptedLLM(_PROSE, _tool_chunks(_ASKS))
    service = ConversationService(llm=llm)

    events = _drain(service.start_conversation_stream(form_data={"product_name": "周报助手"}))
    visible = _visible_text(events)

    assert "你更倾向哪种做法？" in visible
    assert ASK_TOOL_NAME not in visible
    # 选项短标题只在工具参数里、不在正文里（正文只写解释）
    assert "第一版就做离线同步" not in visible


def test_continue_stream_also_extracts() -> None:
    """接续对话走的是同一条路——两条入口共用 `_stream_with_asks`，不能只修一条。"""
    llm = _ScriptedLLM(_PROSE, _tool_chunks(_ASKS))
    service = ConversationService(llm=llm)

    events = _drain(
        service.continue_conversation_stream(
            user_message="第一版要不要离线？",
            history=[{"role": "assistant", "content": "先问一个最关键的。"}],
            profile_key="clarification",
        )
    )

    done = _done(events)
    assert done.data["reply"] == _PROSE
    assert len(done.data["clarification_asks"]) == 1


def test_without_tool_call_nothing_changes() -> None:
    """没调工具是常态：空数组 + 原文照旧，绝不能因此报错或吞掉正文。"""
    llm = _ScriptedLLM("信息已经足够，可以点击生成 PRD")
    service = ConversationService(llm=llm)

    events = _drain(service.start_conversation_stream(form_data={"product_name": "周报助手"}))
    done = _done(events)

    assert done.data["reply"] == "信息已经足够，可以点击生成 PRD"
    assert done.data["clarification_asks"] == []
    assert "信息已经足够" in _visible_text(events)


def test_bad_tool_args_degrade_to_empty_asks() -> None:
    """工具参数坏了只丢单：正文照常给用户，题目为空——生成不因此失败。"""
    bad = [ToolCallChunk(name=ASK_TOOL_NAME, args="{这不是合法 JSON", id="call-1", index=0)]
    llm = _ScriptedLLM(_PROSE, bad)
    service = ConversationService(llm=llm)

    events = _drain(service.start_conversation_stream(form_data={"product_name": "周报助手"}))

    done = _done(events)
    assert done.data["clarification_asks"] == []
    assert done.data["reply"] == _PROSE


class _ShardedLLM:
    """把**一次**工具调用切成多片吐出来的假模型。

    真实上游就是这么送的：首片带 `name` / `id` / 空 `args`，随后每片只带一小段
    JSON 字符串。`_ScriptedLLM` 那种「一整片发完」的写法掩盖过一个真 bug
    （分片没被拼接 → 题目全丢 → 选项卡一张不弹），所以这里另开一个假模型。
    """

    def __init__(self, reply: str, shards: list[ToolCallChunk]) -> None:
        self._reply = reply
        self._shards = shards
        self.calls: list[list[BaseMessage]] = []

    def bind_tools(self, tools, *, tool_choice=None, parallel_tool_calls=None, **kwargs):  # noqa: ANN001, ANN003
        return self

    async def astream(self, messages: list[BaseMessage], **kwargs) -> AsyncIterator[AIMessageChunk]:  # noqa: ANN003
        self.calls.append(list(messages))
        if self._reply:
            yield AIMessageChunk(content=self._reply)
        for shard in self._shards:
            yield AIMessageChunk(content="", tool_call_chunks=[shard])


def test_split_tool_call_stream_still_yields_asks() -> None:
    """端到端：工具调用被切成多片送来时，题目照样解析得出来、选项卡照样弹。

    这条守的是「模型分明调了工具，选项卡却一张都不弹」——只有**分片**形态能复现，
    「一整片发完」的假数据测不出来。
    """
    import json

    payload = {
        "questions": [
            {
                "question": "第一版要不要做离线同步？",
                "background": "你提到用户常在弱网环境使用。",
                "options": [{"text": "第一版就做离线同步"}, {"text": "第一版只做在线"}],
            }
        ]
    }
    raw = json.dumps(payload, ensure_ascii=False)
    cut = len(raw) // 2  # 从中间切开，模拟真实的分片
    shards = [
        ToolCallChunk(name=ASK_TOOL_NAME, args="", id="call-1", index=0),
        ToolCallChunk(name=None, args=raw[:cut], id=None, index=0),
        ToolCallChunk(name=None, args=raw[cut:], id=None, index=0),
    ]
    llm = _ShardedLLM(_PROSE, shards)
    service = ConversationService(llm=llm)

    events = _drain(service.start_conversation_stream(form_data={"product_name": "周报助手"}))
    done = _done(events)

    asks = done.data["clarification_asks"]
    assert len(asks) == 1, f"分片流应当解析出一道题，实际是 {asks}"
    assert asks[0]["question"] == "第一版要不要做离线同步？"
    assert [(item["label"], item["text"]) for item in asks[0]["options"]] == [
        ("A", "第一版就做离线同步"),
        ("B", "第一版只做在线"),
    ]
    # 工具调用 id 也要保住——作答回灌要靠它配对
    assert asks[0]["tool_call_id"] == "call-1"
    assert done.data["tool_call_id"] == "call-1"
    # 正文仍然干净：工具参数不混进正文
    assert _visible_text(events) == _PROSE


def test_persisted_history_is_clean_prose() -> None:
    """写回会话历史的是**干净正文**：工具参数不混进正文，正文也不该丢。

    这条用「第二轮请求实际发出去的 messages」来断言——只检查 `record` 的内部字段
    证不了「模型真的看到的是干净正文」，而这里证的正是后者。
    """
    llm = _ScriptedLLM(_PROSE, _tool_chunks(_ASKS))
    service = ConversationService(llm=llm)

    started = _drain(service.start_conversation_stream(form_data={"product_name": "周报助手"}))
    conversation_id = str(started[0].data["conversation_id"])

    _drain(
        service.continue_conversation_stream(
            user_message="先做在线吧。",
            conversation_id=conversation_id,
        )
    )

    # 只看**历史**那部分（助手回过的内容）。系统提示词里本来就会讲工具用法——那是给模型的
    # 指令，不算污染；要守的是「模型产出的正文别混进工具参数」。
    sent = [message for message in llm.calls[1] if isinstance(message, AIMessage)]
    assert sent, "第二轮请求里应当带着上一轮的助手回复"
    history_text = "".join(str(message.content) for message in sent)

    assert ASK_TOOL_NAME not in history_text
    assert "你更倾向哪种做法？" in history_text
