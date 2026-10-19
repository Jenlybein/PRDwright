"""统一流式编排（`services/stream_reply.py`）的契约测试。

背景：合并前「把模型流包装成 SSE 事件」在 `conversation_service`、
`document_service`、`prd_section_writer` 里各有一份实现，两处调用点各自依赖
不同的边角行为（`on_finish` 回写历史、`extra_data` 透传）。本文件把这些行为钉成
不变量——**合并后仍必须成立**（ADR-013）。

覆盖的行为契约：

- 事件序列 `status → chunk* → done`，`done.data` 带完整 `reply` 与 `extra_data`。
- 出错 / 截断 → `error` 事件（带 `code`），且**之后不再发 `done`**。
- `on_finish` 仅在**成功**时调用，且在 `done` 之前。
- 分块内容是结构化列表时正确摊平。
"""

import asyncio
from pathlib import Path
from typing import Any, AsyncIterator

from langchain_core.messages import AIMessageChunk, BaseMessage

from backend.api.schemas import StreamEvent
from backend.core.errors import ErrorCode
from backend.services.stream_reply import (
    _merge_tool_calls,
    astream_text,
    collect_reply,
    error_event,
    iter_reply_events,
)

MESSAGES: list[BaseMessage] = []

# 被扫描的 services 目录：用于「不许再长出第二份编排」的不变量
_SERVICES_DIR = Path(__file__).resolve().parents[1] / "src" / "backend" / "services"


class ScriptedLLM:
    """按预设分块作答；`finish_reason` 可注入以模拟截断。"""

    def __init__(self, pieces: list[str], finish_reason: str | None = None) -> None:
        self._pieces = list(pieces)
        self._finish_reason = finish_reason

    async def astream(self, messages: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
        for index, piece in enumerate(self._pieces):
            is_last = index == len(self._pieces) - 1
            metadata = {"finish_reason": self._finish_reason} if (is_last and self._finish_reason) else {}
            yield AIMessageChunk(content=piece, response_metadata=metadata)


class FailLLM:
    """先吐一块再抛异常，模拟生成中途失败。"""

    async def astream(self, messages: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(content="已生成的一半")
        raise RuntimeError("上游连接中断")


class StructuredLLM:
    """分块内容是结构化列表（Anthropic 风格）。"""

    async def astream(self, messages: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(content=[{"type": "text", "text": "结构化"}])
        yield AIMessageChunk(content=[{"type": "text", "text": "分块"}])


def _drain(agen: Any) -> list[StreamEvent]:
    """同步跑完一条事件流，收集全部事件。"""

    async def run() -> list[StreamEvent]:
        return [event async for event in agen]

    return asyncio.run(run())


def test_event_sequence_is_status_then_chunks_then_done() -> None:
    """不变量：事件序列为 `status → chunk* → done`，且 `reply` 是完整拼接结果。"""
    events = _drain(
        iter_reply_events(
            ScriptedLLM(["第一段", "第二段"]),
            MESSAGES,
            status_text="正在生成…",
            extra_data={"doc_type": "prd"},
        )
    )

    assert [event.type for event in events] == ["status", "chunk", "chunk", "done"]
    assert events[0].content == "正在生成…"
    assert [event.content for event in events[1:3]] == ["第一段", "第二段"]
    assert events[-1].data == {"doc_type": "prd", "reply": "第一段第二段"}


def test_no_status_event_when_status_text_is_omitted() -> None:
    """不变量：`status_text` 为空时不发 `status` 事件（调用方已发过）。"""
    events = _drain(iter_reply_events(ScriptedLLM(["正文"]), MESSAGES))

    assert [event.type for event in events] == ["chunk", "done"]


def test_failure_becomes_error_event_without_done() -> None:
    """不变量：异常转成 `error` 事件并携带 `code`，且**不再发 `done`**。"""
    events = _drain(iter_reply_events(FailLLM(), MESSAGES, extra_data={"doc_type": "prd"}))

    assert [event.type for event in events] == ["chunk", "error"]
    error = events[-1]
    assert error.code == ErrorCode.LLM_UPSTREAM_ERROR.value
    assert "上游连接中断" in error.content
    assert error.data is not None and error.data["error_type"] == "RuntimeError"


def test_truncation_becomes_error_event() -> None:
    """不变量：被 `max_tokens` 截断时同样转 `error` 事件（两家供应商标记都认）。"""
    for reason in ("length", "max_tokens"):
        events = _drain(iter_reply_events(ScriptedLLM(["半截"], finish_reason=reason), MESSAGES))

        assert [event.type for event in events] == ["chunk", "error"], reason
        assert events[-1].code == ErrorCode.LLM_UPSTREAM_ERROR.value


def test_on_finish_runs_only_on_success_and_before_done() -> None:
    """不变量：`on_finish` 在 `done` 之前调用，且出错/截断时**不**调用。"""
    calls: list[str] = []

    def on_finish(reply: str) -> None:
        calls.append(reply)

    order: list[str] = []

    async def run_success() -> None:
        async for event in iter_reply_events(
            ScriptedLLM(["甲", "乙"]), MESSAGES, on_finish=on_finish
        ):
            order.append(event.type)

    asyncio.run(run_success())
    assert calls == ["甲乙"]
    assert order[-1] == "done"
    assert order.index("done") == len(order) - 1

    # 出错与截断都不应触发回调
    calls.clear()
    _drain(iter_reply_events(FailLLM(), MESSAGES, on_finish=on_finish))
    _drain(iter_reply_events(ScriptedLLM(["半截"], finish_reason="length"), MESSAGES, on_finish=on_finish))
    assert calls == []


def test_structured_chunk_content_is_flattened() -> None:
    """不变量：结构化分块（`[{"type":"text",...}]`）被摊平成纯文本。"""
    events = _drain(iter_reply_events(StructuredLLM(), MESSAGES))

    assert [event.content for event in events if event.type == "chunk"] == ["结构化", "分块"]
    assert events[-1].data is not None
    assert events[-1].data["reply"] == "结构化分块"


def test_collect_reply_returns_whole_text() -> None:
    """不变量：`collect_reply` 复用同一份增量逻辑，返回完整文本。"""
    text = asyncio.run(collect_reply(ScriptedLLM(["甲", "乙", "  "]), MESSAGES))

    assert text == "甲乙"


def test_astream_text_raises_on_truncation() -> None:
    """不变量：`astream_text` 遇到截断抛 `OutputTruncatedError`（此处不转事件）。"""
    from backend.services.stream_guard import OutputTruncatedError

    async def run() -> str:
        return "".join([text async for text in astream_text(ScriptedLLM(["半截"], finish_reason="length"), MESSAGES)])

    try:
        asyncio.run(run())
    except OutputTruncatedError:
        return
    raise AssertionError("截断时应抛出 OutputTruncatedError")


def test_error_event_defaults_to_llm_upstream_code() -> None:
    """不变量：默认错误码是 `LLM_UPSTREAM_ERROR`，与整篇生成同分类。"""
    event = error_event(ValueError("坏了"))

    assert event.type == "error"
    assert event.code == ErrorCode.LLM_UPSTREAM_ERROR.value
    assert event.data is not None and event.data["error_type"] == "ValueError"


# ---------------------------------------------------------------------------
# _merge_tool_calls：把流式工具调用分片拼回完整调用
#
# ⚠️ 这一组守的是**真实模型**的分片形态（一次调用被切成很多片送来）。
# 只造「一整片发完」的假数据测不出问题——线上就是这么漏过去的：拼装方式错了，
# 模型明明调了工具，题目却一个都取不到，选项卡一张都不弹。
# ---------------------------------------------------------------------------


def test_split_tool_call_chunks_merge_into_one_complete_call() -> None:
    """一次工具调用被切成多片：参数必须按 index 拼回一条完整调用。

    这是线上「选项卡一张都不弹」的根因：若按分片逐个解析，首片（`args=""`）
    解析成空对象、后续残片各自解析失败，一次调用就变成「一条空调用 + 若干条废调用」。
    """
    shards = [
        {"name": "ask_clarification_questions", "args": "", "id": "call-abc", "index": 0},
        {"name": None, "args": '{"questions": [{"question": "要不要离线", "opti', "id": None, "index": 0},
        {"name": None, "args": 'ons": [{"text": "做"}, {"text": "不做"}]}]}', "id": None, "index": 0},
    ]

    merged = _merge_tool_calls(shards)

    assert len(merged) == 1, f"分片应当拼成一条调用，实际是 {merged}"
    assert merged[0]["name"] == "ask_clarification_questions"
    assert merged[0]["id"] == "call-abc"
    assert merged[0]["args"] == {
        "questions": [
            {"question": "要不要离线", "options": [{"text": "做"}, {"text": "不做"}]}
        ]
    }


def test_two_tool_calls_stay_separate() -> None:
    """两次调用（index 0 / 1）各拼各的，不能被并成一条。"""
    shards = [
        {"name": "ask_clarification_questions", "args": '{"questions": [{"question": "第一题",', "id": "c1", "index": 0},
        {"name": None, "args": ' "options": [{"text": "A"}, {"text": "B"}]}]}', "id": None, "index": 0},
        {"name": "ask_clarification_questions", "args": '{"questions": [{"question": "第二题", "options": [{"text": "C"}, {"text": "D"}]}]}', "id": "c2", "index": 1},
    ]

    merged = _merge_tool_calls(shards)

    assert [call["id"] for call in merged] == ["c1", "c2"]
    assert merged[0]["args"]["questions"][0]["question"] == "第一题"
    assert merged[1]["args"]["questions"][0]["question"] == "第二题"


def test_merge_without_chunks_is_empty() -> None:
    """没有工具调用分片就是空列表——没调工具是常态，不是异常。"""
    assert _merge_tool_calls([]) == []


def test_services_hold_no_duplicate_stream_implementation() -> None:
    """不变量：services 层不许再长出第二份流式编排或文本提取实现。

    合并前这里曾有 `_stream_reply`（两份）与 `_extract_text`（`llm_utils` 的副本）。
    这条扫描保证它们不会悄悄复活。
    """
    forbidden = ("async def _stream_reply", "def _extract_text", "def _astream_text")

    offenders: list[str] = []
    for path in sorted(_SERVICES_DIR.glob("*.py")):
        if path.name == "stream_reply.py":
            continue  # 唯一允许持有编排实现的模块
        source = path.read_text(encoding="utf-8")
        for pattern in forbidden:
            if pattern in source:
                offenders.append(f"{path.name}: {pattern}")

    assert offenders == [], f"发现重复的流式实现：{offenders}"
