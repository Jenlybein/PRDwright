"""对话流把「结构化选择题」剥离并交出去。

盯四件事：

1. 用户**看到**的增量里永远没有哨兵与块体（抑制器在流式阶段就拦住）；
2. `done` 事件里既有**干净正文**、又有解析好的 `clarification_asks`；
3. 写回会话历史的也是干净正文——否则下一轮会把块当上下文喂回模型；
4. 没产块时一切照旧（空数组 + 原文），这是常态而不是异常。
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage

from backend.core.contracts import StreamEvent
from backend.services.clarification_ask import ASK_BEGIN_SENTINEL, ASK_END_SENTINEL
from backend.services.conversation_service import ConversationService


class _ScriptedLLM:
    """按脚本吐字的假模型。

    `chunk_size` 默认取一个**小于哨兵长度**的值（哨兵 20 字符），这样几乎每一轮都会把哨兵
    切在 chunk 边界上——抑制器最容易失效的场景就在那里。
    """

    def __init__(self, reply: str, chunk_size: int = 5) -> None:
        self._chunks = [reply[i : i + chunk_size] for i in range(0, len(reply), chunk_size)]
        self.calls: list[list[BaseMessage]] = []

    async def astream(self, messages: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
        self.calls.append(list(messages))
        for chunk in self._chunks:
            yield AIMessageChunk(content=chunk)


def _drain(events: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    """同步跑完一条事件流并收集全部事件。"""

    async def run() -> list[StreamEvent]:
        return [event async for event in events]

    return asyncio.run(run())


def _block(payload: object) -> str:
    return f"{ASK_BEGIN_SENTINEL}\n{json.dumps(payload, ensure_ascii=False)}\n{ASK_END_SENTINEL}"


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


def test_start_stream_strips_the_block_from_every_chunk() -> None:
    """流式阶段就拦住哨兵与块体——这是抑制器存在的全部意义。"""
    llm = _ScriptedLLM(f"{_PROSE}\n\n{_block(_ASKS)}")
    service = ConversationService(llm=llm)

    events = _drain(
        service.start_conversation_stream(form_data={"product_name": "周报助手"})
    )
    visible = _visible_text(events)

    assert ASK_BEGIN_SENTINEL not in visible
    assert ASK_END_SENTINEL not in visible
    assert "离线同步" not in visible
    # 正文一字不丢
    assert "你更倾向哪种做法？" in visible


def test_start_stream_done_carries_clean_reply_and_asks() -> None:
    """`done` 同时给出干净正文与解析好的题目；选项标号由后端分配。"""
    llm = _ScriptedLLM(f"{_PROSE}\n\n{_block(_ASKS)}")
    service = ConversationService(llm=llm)

    done = _done(
        _drain(service.start_conversation_stream(form_data={"product_name": "周报助手"}))
    )

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


def test_continue_stream_also_strips_and_carries() -> None:
    """接续对话走的是同一条路——两条入口共用 `_stream_with_asks`，不能只修一条。"""
    llm = _ScriptedLLM(f"{_PROSE}\n\n{_block(_ASKS)}")
    service = ConversationService(llm=llm)

    events = _drain(
        service.continue_conversation_stream(
            user_message="第一版要不要离线？",
            history=[{"role": "assistant", "content": "先问一个最关键的。"}],
            profile_key="clarification",
        )
    )

    assert ASK_BEGIN_SENTINEL not in _visible_text(events)
    done = _done(events)
    assert done.data["reply"] == _PROSE
    assert len(done.data["clarification_asks"]) == 1


def test_without_block_nothing_changes() -> None:
    """没产块是常态：空数组 + 原文照旧，绝不能因此报错或吞掉正文。"""
    llm = _ScriptedLLM("信息已经足够，可以点击生成 PRD")
    service = ConversationService(llm=llm)

    events = _drain(
        service.start_conversation_stream(form_data={"product_name": "周报助手"})
    )
    done = _done(events)

    assert done.data["reply"] == "信息已经足够，可以点击生成 PRD"
    assert done.data["clarification_asks"] == []
    assert "信息已经足够" in _visible_text(events)


def test_invalid_block_is_dropped_but_prose_survives() -> None:
    """块坏了只丢块：正文照常给用户，题目为空——生成不因此失败。"""
    reply = f"{_PROSE}\n\n{ASK_BEGIN_SENTINEL}\n[{{这不是合法 JSON\n{ASK_END_SENTINEL}"
    llm = _ScriptedLLM(reply)
    service = ConversationService(llm=llm)

    events = _drain(
        service.start_conversation_stream(form_data={"product_name": "周报助手"})
    )

    assert ASK_BEGIN_SENTINEL not in _visible_text(events)
    done = _done(events)
    assert done.data["clarification_asks"] == []
    assert done.data["reply"] == _PROSE


def test_persisted_history_never_contains_the_block() -> None:
    """写回会话历史的是**干净正文**：带块的原文一旦回流，下一轮会被当作上下文喂给模型。

    这条用「第二轮请求实际发出去的 messages」来断言——只检查 `record` 的内部字段
    证不了「模型真的没看到块」，而这里证的正是后者。
    """
    llm = _ScriptedLLM(f"{_PROSE}\n\n{_block(_ASKS)}")
    service = ConversationService(llm=llm)

    started = _drain(
        service.start_conversation_stream(form_data={"product_name": "周报助手"})
    )
    conversation_id = str(started[0].data["conversation_id"])

    _drain(
        service.continue_conversation_stream(
            user_message="先做在线吧。",
            conversation_id=conversation_id,
        )
    )

    # 只看**历史**那部分（助手回过的内容）。系统提示词里本来就带哨兵示例——那是给模型的
    # 指令，不算污染；要守的是「模型产出的块别回流成上下文」。
    sent = [message for message in llm.calls[1] if isinstance(message, AIMessage)]
    assert sent, "第二轮请求里应当带着上一轮的助手回复"
    history_text = "".join(str(message.content) for message in sent)

    assert ASK_BEGIN_SENTINEL not in history_text
    assert ASK_END_SENTINEL not in history_text
    assert "第一版就做离线同步" not in history_text
    assert "你更倾向哪种做法？" in history_text
