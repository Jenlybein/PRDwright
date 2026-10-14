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
- **用量计量也只落在这里**（`astream_text` 末尾）：所有流式调用都经由本模块，
  把累加写在这一处，就不会出现「某个生成入口忘了统计」。计量器本身在 `llm_meter`。
- **事前预算的检查也只在这里**（`astream_text` 开头）：与计量同理，一次覆盖全部
  调用点；判定逻辑在 `token_budget`。估算超顶时直接抛，请求**根本不会发出**。
"""

from __future__ import annotations

import time
from typing import Any, AsyncIterator, Callable

from langchain_core.messages import AIMessageChunk, BaseMessage

from backend.core.config import get_settings
from backend.core.contracts import StreamEvent
from backend.core.errors import AppError, ErrorCode
from backend.services.llm_factory import StreamingChatModel
from backend.services.llm_meter import current_meter
from backend.services.llm_utils import extract_text
from backend.services.stream_guard import ensure_not_truncated
from backend.services.token_budget import enforce

# 流式生成失败时使用的错误码：分章节与整篇是同一条能力的两条实现路径，
# 对前端来说不该有两种错误分类。
_DEFAULT_ERROR_CODE = ErrorCode.LLM_UPSTREAM_ERROR


def _chunk_usage(chunk: AIMessageChunk | None) -> tuple[int, int]:
    """从最后一块里取 token 用量，返回 `(输入, 输出)`；没带用量时返回 `(0, 0)`。

    `usage_metadata` 是三家供应商的统一字段（方舟封装在 `ark_llm` 里做过映射，
    openai / anthropic 的 LangChain 集成末帧自带）。取不到不算异常：部分流式集成
    默认不回吐用量，此时这次调用仍会按「零 token」计入次数与耗时。
    """
    meta = getattr(chunk, "usage_metadata", None) or {}
    return int(meta.get("input_tokens") or 0), int(meta.get("output_tokens") or 0)


def _check_budget(messages: list[BaseMessage]) -> None:
    """调用**之前**做一次事前预算检查；超顶直接抛，这次调用不发出。

    与事后计量共用同一套「我在哪一步」的线索：计量器在就用它的供应商/型号/步骤名
    （同一条生成链路里两者必须同源，否则「按什么上限判的」和「记到哪一步」会打架）；
    没有计量器时（澄清对话、脚本、单测直接调服务）退回配置的默认值，步骤名为空，
    按默认业务顶判定——照样受保护，只是不再区分步骤。

    Args:
        messages: 待发送的消息。

    Raises:
        BudgetExceededError: 估算值超过有效预算。
    """
    meter = current_meter()
    settings = get_settings()
    if not settings.llm_budget_enabled:
        return

    if meter is not None:
        provider, model, step = meter.provider, meter.model, meter.current_step()
    else:
        provider, model, step = settings.default_llm_provider, settings.default_llm_model, ""

    decision = enforce(messages, provider=provider, model=model, step=step)
    if meter is not None and decision.warned:
        meter.note_warning(decision.step, decision.ratio, decision.warning_message())


async def astream_text(
    llm: StreamingChatModel,
    messages: list[BaseMessage],
) -> AsyncIterator[str]:
    """流式产出一次调用的纯文本增量，流开始时查预算、流结束时检查截断**并计量**。

    各供应商的分块 `content` 有可能是结构化列表，`extract_text` 统一摊平成字符串。

    **这里是全项目唯一的用量累加点**：`iter_reply_events` 与 `collect_reply` 都经由本函数，
    分章节流水线的两个包装（`_stream_text` / `_collect`）也一样——一处覆盖全部调用点，
    新增生成入口只要走这条路就自动被计量，不会漏。事前预算同理。

    计量所需的「这是哪一次生成、第几步」来自环境值（见 `llm_meter`），因此**没有新增
    任何函数参数**，业务层的调用方式一字未改。

    Args:
        llm: 已构造好的流式模型。
        messages: 已组装好的 LangChain 消息。

    Yields:
        非空文本增量。

    Raises:
        BudgetExceededError: 估算的输入超过预算，本次调用未发出。
        OutputTruncatedError: 模型输出因达到 token 上限被截断。由调用方决定
            转成 `error` 事件还是直接上抛。
    """
    _check_budget(messages)
    meter = current_meter()
    started_at = time.monotonic()
    last_chunk: AIMessageChunk | None = None
    async for chunk in llm.astream(messages):
        text = extract_text(chunk.content)
        last_chunk = chunk
        if text:
            yield text

    if meter is not None:
        # 先计量再查截断：被截断的这次调用**照样烧了 token**，不该因为它抛错就漏记。
        # （截断检查在下面，抛异常时本函数的 yield 已结束，但上面的累加已经完成。）
        input_tokens, output_tokens = _chunk_usage(last_chunk)
        meter.record(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=int(max(0.0, time.monotonic() - started_at) * 1000),
        )

    if last_chunk is not None:
        ensure_not_truncated(last_chunk)


def error_event(
    exc: Exception,
    code: ErrorCode = _DEFAULT_ERROR_CODE,
) -> StreamEvent:
    """把异常转成 `error` 事件（全项目唯一实现）。

    `AppError` 自带错误码（如预算超顶是 `INVALID_REQUEST`），这里**以异常自带的为准**：
    抛出方比兜底方更清楚这次失败属于哪一类，否则「超预算」会被笼统标成
    「上游故障」，前端就会去做无意义的重试。

    Args:
        exc: 生成过程中抛出的异常。
        code: 错误码，默认 `LLM_UPSTREAM_ERROR`；`exc` 是 `AppError` 时被它自带
            的码覆盖。

    Returns:
        可直接 yield 给前端的 `error` 事件。
    """
    if isinstance(exc, AppError):
        code = exc.code
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
