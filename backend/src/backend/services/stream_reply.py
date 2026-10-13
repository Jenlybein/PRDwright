"""统一的流式回复编排。

「把模型的一次流式输出包装成 SSE 事件序列」这件事，历史上在 `conversation_service`、
`document_service`、`prd_section_writer` 里各有一份实现：两个 `_stream_reply` 几乎
逐行相同，`prd_section_writer._error_event` 还是第三份「异常 → error 事件」的转换
（它的注释当时写着「与 `document_service._stream_reply` 的口径一致」——靠注释保证
一致，改一处漏一处只是时间问题）。

本模块是全项目唯一的编排实现，三个 service 都改为调用它（ADR-013）。

职责边界：

- 本模块只做**事件编排**（状态 → 增量 → 收尾 / 错误）与「纯文本增量的产出」。
- 「分块是否被 `max_tokens` 截断」由 `stream_guard` 判断，这里不重复实现，只负责
  把 `OutputTruncatedError` 转成 `error` 事件。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Callable

from langchain_core.messages import BaseMessage

from backend.api.schemas import StreamEvent
from backend.core.errors import ErrorCode
from backend.services.llm_factory import StreamingChatModel
from backend.services.llm_utils import extract_text
from backend.services.stream_guard import ensure_not_truncated

# 流式生成失败时使用的错误码：分章节与整篇是同一条能力的两条实现路径，
# 对前端来说不该有两种错误分类。
_DEFAULT_ERROR_CODE = ErrorCode.LLM_UPSTREAM_ERROR


async def astream_text(
    llm: StreamingChatModel,
    messages: list[BaseMessage],
) -> AsyncIterator[str]:
    """流式产出一次调用的纯文本增量，流结束时检查是否被截断。

    各供应商的分块 `content` 有可能是结构化列表，`extract_text` 统一摊平成字符串。

    Args:
        llm: 已构造好的流式模型。
        messages: 已组装好的 LangChain 消息。

    Yields:
        非空文本增量。

    Raises:
        OutputTruncatedError: 模型输出因达到 token 上限被截断。由调用方决定
            转成 `error` 事件还是直接上抛。
    """
    last_chunk = None
    async for chunk in llm.astream(messages):
        text = extract_text(chunk.content)
        last_chunk = chunk
        if text:
            yield text
    if last_chunk is not None:
        ensure_not_truncated(last_chunk)


def error_event(
    exc: Exception,
    code: ErrorCode = _DEFAULT_ERROR_CODE,
) -> StreamEvent:
    """把异常转成 `error` 事件（全项目唯一实现）。

    Args:
        exc: 生成过程中抛出的异常。
        code: 错误码，默认 `LLM_UPSTREAM_ERROR`。

    Returns:
        可直接 yield 给前端的 `error` 事件。
    """
    return StreamEvent(
        type="error",
        content=f"生成失败：{exc}",
        data={"error_type": type(exc).__name__},
        code=code.value,
    )


async def iter_reply_events(
    llm: StreamingChatModel,
    messages: list[BaseMessage],
    *,
    status_text: str | None = None,
    on_finish: Callable[[str], Any] | None = None,
    extra_data: dict[str, Any] | None = None,
    code: ErrorCode = _DEFAULT_ERROR_CODE,
) -> AsyncIterator[StreamEvent]:
    """通用的流式回复流程：状态提示 → 逐字增量 → 收尾事件。

    Args:
        llm: 已构造好的流式模型。
        messages: 已组装好的 LangChain 消息。
        status_text: 首个状态事件的提示语；为 `None` 时不发状态事件
            （调用方已经发过时用）。
        on_finish: 生成**成功**后的回调，接收完整回复文本（如回写会话历史）。
            出错或截断时不调用。
        extra_data: 附加到 `done` 事件的数据，如会话 ID、文档类型。
        code: 出错时 `error` 事件携带的错误码。

    Yields:
        `StreamEvent`，依次为 `status` / 若干 `chunk` / `done`；
        出错时为 `error`，且**之后不再发 `done`**。
    """
    if status_text:
        yield StreamEvent(type="status", content=status_text)

    chunks: list[str] = []
    try:
        async for text in astream_text(llm, messages):
            chunks.append(text)
            yield StreamEvent(type="chunk", content=text)
    except Exception as exc:  # noqa: BLE001 - 流式接口需要把异常转成事件而不是抛出
        yield error_event(exc, code)
        return

    reply = "".join(chunks).strip()
    if on_finish is not None:
        on_finish(reply)

    # done 事件带上完整回复，便于前端在断流或丢包时做一次对齐
    data: dict[str, Any] = {**(extra_data or {}), "reply": reply}
    yield StreamEvent(type="done", content="", data=data)


async def collect_reply(
    llm: StreamingChatModel,
    messages: list[BaseMessage],
) -> str:
    """把流式回复收拢成完整文本。

    用于「必须拿到整段内容才能处理」的场景（如审核需要整体 JSON 才能解析），
    与 `iter_reply_events` 共用同一份文本增量产出逻辑。

    Raises:
        OutputTruncatedError: 模型输出被截断（此处不转事件，由调用方处理）。
    """
    chunks: list[str] = []
    async for text in astream_text(llm, messages):
        chunks.append(text)
    return "".join(chunks).strip()
