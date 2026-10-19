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

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

from langchain_core.messages import AIMessageChunk, BaseMessage

from backend.core.config import get_settings
from backend.core.contracts import StreamEvent
from backend.core.errors import AppError, ErrorCode
from backend.services.llm_factory import StreamingChatModel
from backend.services.llm_meter import current_meter
from backend.services.llm_utils import extract_text
from backend.services.stream_guard import OutputTruncatedError, ensure_not_truncated
from backend.services.token_budget import enforce

# 流式生成失败时使用的错误码：分章节与整篇是同一条能力的两条实现路径，
# 对前端来说不该有两种错误分类。
_DEFAULT_ERROR_CODE = ErrorCode.LLM_UPSTREAM_ERROR

# 判定「值不值得整次重来」的字样白名单。这里刻意**不 import httpx**（业务层不碰传输层
# 的库），改按异常类名匹配：httpx 的传输类异常名字本身很稳定——
# ConnectTimeout / ReadTimeout / PoolTimeout、ConnectError / ReadError / WriteError、
# LocalProtocolError / RemoteProtocolError，各家 SDK 的 APITimeoutError /
# APIConnectionError 也都被这些字样覆盖住。
_RETRYABLE_NAME_PARTS = (
    "Timeout",
    "Connection",
    "ConnectError",
    "ReadError",
    "WriteError",
    "ProtocolError",
    "NetworkError",
)


class CallTimeoutError(AppError):
    """单次模型调用超过了墙钟硬顶，本次调用被取消。

    继承 `AppError` 是为了两件事：错误码复归 `LLM_UPSTREAM_ERROR`（8 码体系不膨胀），
    以及让 `_is_retryable` 据此**不再重试**——慢到触发总上限的调用，重来一次只会同样慢，
    让用户多等一遍没有意义。
    """

    def __init__(self, seconds: float) -> None:
        super().__init__(
            ErrorCode.LLM_UPSTREAM_ERROR,
            f"模型响应超过 {seconds:g} 秒仍未完成，已取消本次调用。"
            "请稍后重试；若频繁出现，可调大 LLM_TOTAL_TIMEOUT 或精简输入。",
        )


def _elapsed_ms(started_at: float) -> int:
    """把一次调用的墙钟消耗算成毫秒；时钟回拨得到的负数按 0 处理。"""
    return int(max(0.0, time.monotonic() - started_at) * 1000)


def _is_retryable(exc: Exception) -> bool:
    """这次失败是不是「大概率重来一次就好」的传输层抖动。

    **宁可不重试，也不误重试**：漏掉一次本可自愈的调用，代价是用户手动点一次重生成；
    误判的代价则是重复扣费与重复内容（后者由 `astream_text` 里的 `text_emitted` 兜底）。
    所以这里是白名单，并先排除两类「重试一万次结果也一样」的失败：

    - `AppError`：我们自己判定的失败（预算超顶、调用超过墙钟硬顶 …）；
    - `OutputTruncatedError`：内容性质的失败，重来只会拿到同样的内容。

    Returns:
        是否可以整次重来。
    """
    if isinstance(exc, (AppError, OutputTruncatedError)):
        return False

    if any(part in type(exc).__name__ for part in _RETRYABLE_NAME_PARTS):
        return True

    # 上游明确的服务端故障与请求/锁超时；4xx 一律不重试（参数不合法、认证、限流）
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and (500 <= status < 600 or status in (408, 409)):
        return True
    return False


@asynccontextmanager
async def _total_deadline(seconds: float) -> AsyncIterator[None]:
    """给一次调用套墙钟上限；`seconds <= 0` 表示不设限。

    为什么要多这一层：底层 httpx 的 read timeout 是 **inter-byte** 语义，只约束
    「相邻两次数据之间」的等待。一个每 59 秒吐一个字的模型能把单次调用无限拖下去
    而永不超时，所以总时长必须在这里自己包一层。
    """
    if seconds <= 0:
        yield
        return
    try:
        async with asyncio.timeout(seconds):
            yield
    except TimeoutError as exc:
        # 换成业务异常，前端拿到的是可展示的中文说明而不是一个空字符串的 TimeoutError
        raise CallTimeoutError(seconds) from exc


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


async def _astream_chunks(
    llm: StreamingChatModel,
    messages: list[BaseMessage],
) -> AsyncIterator[AIMessageChunk]:
    """核心：流式产出原始 `AIMessageChunk`，并检查预算、超时与截断，**并在此计量**。

    文本产出（`astream_text`）与工具调用捕获（`iter_reply_events_with_tools`）都复用它——
    一处覆盖全部调用点，预算/计量/重试/截断的口径不会分叉。

    **自愈也发生在这里**（ADR-016）：失败时若**还没有向调用方交付过任何增量**（正文文本
    或 `tool_call_chunks`），就把整次调用重来一遍。重来对消费者是透明的——一帧都还没出去，
    重开 `astream` 即可。一旦交付过增量就绝不再来：那些字符/工具分片已经流到了下游撤不回来，
    重来会让消费方收到「半截 + 一份完整」的重复内容（T0 时代并发订阅踩过同款的坑）。

    Args:
        llm: 已构造好的流式模型。
        messages: 已组装好的 LangChain 消息。

    Yields:
        原始 `AIMessageChunk`（含文本增量与 `tool_call_chunks`）。

    Raises:
        BudgetExceededError: 估算的输入超过预算，本次调用未发出。
        CallTimeoutError: 单次调用超过墙钟硬顶被取消。
        OutputTruncatedError: 模型输出因达到 token 上限被截断。由调用方决定
            转成 `error` 事件还是直接上抛。
    """
    _check_budget(messages)
    meter = current_meter()
    settings = get_settings()

    for attempt in range(settings.llm_max_retries + 1):
        started_at = time.monotonic()
        last_chunk: AIMessageChunk | None = None
        delivered = False
        try:
            async with _total_deadline(settings.llm_total_timeout):
                async for chunk in llm.astream(messages):
                    last_chunk = chunk
                    if extract_text(chunk.content) or getattr(chunk, "tool_call_chunks", None):
                        delivered = True
                    yield chunk
        except Exception as exc:  # noqa: BLE001 - 要不要重来由 `_is_retryable` 判，不在这里过滤
            # 失败同样占用墙钟与一次请求，先记下来再决定要不要重来。
            # （`CancelledError` 是 BaseException，不会被这里捕获，取消任务不会误记成失败。）
            if meter is not None:
                meter.record_failed(duration_ms=_elapsed_ms(started_at))
            if (
                delivered  # 内容已交付（文本或工具分片），撤不回来
                or not _is_retryable(exc)
                or attempt >= settings.llm_max_retries
            ):
                raise
            if settings.llm_retry_delay > 0:
                await asyncio.sleep(settings.llm_retry_delay)
            continue

        # 成功路径（含「成功但被截断」）：先计量再查截断——
        # 被截断的这次调用照样烧了 token，不该因为它随后抛错就漏记。
        if meter is not None:
            input_tokens, output_tokens = _chunk_usage(last_chunk)
            meter.record(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                duration_ms=_elapsed_ms(started_at),
            )
        if last_chunk is not None:
            ensure_not_truncated(last_chunk)
        return


async def astream_text(
    llm: StreamingChatModel,
    messages: list[BaseMessage],
) -> AsyncIterator[str]:
    """流式产出一次调用的纯文本增量（薄封装，行为与旧实现逐字节一致）。

    各供应商的分块 `content` 有可能是结构化列表，`extract_text` 统一摊平成字符串。
    分章节流水线的两个包装（`_stream_text` / `_collect`）与 `collect_reply` 都走这条
    路，故文本口径不变。计量/预算/重试/截断在 `_astream_chunks` 里。

    Args:
        llm: 已构造好的流式模型。
        messages: 已组装好的 LangChain 消息。

    Yields:
        非空文本增量。
    """
    async for chunk in _astream_chunks(llm, messages):
        text = extract_text(chunk.content)
        if text:
            yield text


async def astream_chunks(
    llm: StreamingChatModel,
    messages: list[BaseMessage],
) -> AsyncIterator[AIMessageChunk]:
    """流式产出原始 `AIMessageChunk`（供需要捕获 `tool_call_chunks` 的调用方用）。

    与 `astream_text` 共用同一份预算/计量/重试/截断内核，只是把分块原样交出去。
    """
    async for chunk in _astream_chunks(llm, messages):
        yield chunk


def _merge_tool_calls(tool_call_chunks: list[Any]) -> list[dict[str, Any]]:
    """把流式累积的 `ToolCallChunk` 分片合并成完整的 `tool_calls`（`[{name,args,id}]`）。

    ⚠️ **必须逐片相加**（`AIMessageChunk.__add__`），不能把全部分片塞进一个 chunk 再读
    `.tool_calls`——后者会**按分片逐个独立解析**：首片（`args=""`）解析成空对象、后续
    残片各自解析失败，一次完整的工具调用就被拆成「一条空调用 + 若干条废调用」。

    这个坑的真实代价：模型明明调了工具，题目却一个都取不到，选项卡**一张都不弹**。
    而「一整片发完整工具调用」的假模型测不出来——只有在真实的分片流下才暴露。

    逐片相加才是 LangChain 拼接流式工具调用的正道：它按 `index` 归类、**拼接 `args`
    字符串**、并把后到的 `name` / `id` 补齐到同一条上，最后统一解析一次 JSON
    （`default_tool_chunk_parser`）。**不自己拼 JSON**，避免与 LangChain 的口径漂移。
    """
    if not tool_call_chunks:
        return []
    merged: AIMessageChunk | None = None
    for piece in tool_call_chunks:
        chunk = AIMessageChunk(content="", tool_call_chunks=[piece])
        # 逐片相加：LangChain 在这里按 index 归类并拼接 args 字符串
        merged = chunk if merged is None else merged + chunk
    return list(merged.tool_calls) if merged is not None else []


async def iter_reply_events_with_tools(
    llm: StreamingChatModel,
    messages: list[BaseMessage],
    *,
    status_text: str | None = None,
    on_finish: Callable[[str, list[dict[str, Any]]], Any] | None = None,
    extra_data: dict[str, Any] | None = None,
    code: ErrorCode = _DEFAULT_ERROR_CODE,
) -> AsyncIterator[StreamEvent]:
    """带**工具调用捕获**的流式回复流程：状态 → 逐字增量 → 收尾（正文 + tool_calls）。

    与 `iter_reply_events` 的区别：除了文本增量，还累积 `tool_call_chunks`，在 `done`
    里以 `tool_calls`（`[{name,args,id}]`）一并下发，供澄清选择题提取结构化题目。
    `on_finish` 多收一个 `tool_calls` 参数。

    Args:
        llm: 已构造好的流式模型（通常已 `bind_tools`）。
        messages: 已组装好的 LangChain 消息。
        status_text: 首个状态事件的提示语；为 `None` 时不发状态事件。
        on_finish: 成功回调，接收 `(完整回复文本, tool_calls)`。
        extra_data: 附加到 `done` 事件的数据。
        code: 出错时 `error` 事件携带的错误码。

    Yields:
        `StreamEvent`，依次为 `status` / 若干 `chunk` / `done`（`done.data` 含 `reply`
        与 `tool_calls`）；出错时为 `error`，且**之后不再发 `done`**。
    """
    if status_text:
        yield StreamEvent(type="status", content=status_text)

    chunks: list[str] = []
    tool_call_chunks: list[Any] = []
    try:
        async for chunk in astream_chunks(llm, messages):
            text = extract_text(chunk.content)
            if text:
                chunks.append(text)
                yield StreamEvent(type="chunk", content=text)
            tool_call_chunks.extend(getattr(chunk, "tool_call_chunks", None) or [])
    except Exception as exc:  # noqa: BLE001 - 流式接口需要把异常转成事件而不是抛出
        yield error_event(exc, code)
        return

    reply = "".join(chunks).strip()
    tool_calls = _merge_tool_calls(tool_call_chunks)
    if on_finish is not None:
        on_finish(reply, tool_calls)

    # done 事件带上完整回复与工具调用，便于前端在断流或丢包时做一次对齐
    data: dict[str, Any] = {
        **(extra_data or {}),
        "reply": reply,
        "tool_calls": tool_calls,
    }
    yield StreamEvent(type="done", content="", data=data)


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
