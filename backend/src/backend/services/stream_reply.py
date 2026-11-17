"""统一的流式回复编排。

「把模型的一次流式输出包装成 SSE 事件序列」这件事，历史上在 `conversation_service`、
`document_service` 里曾有两份实现：两个 `_stream_reply` 几乎
逐行相同，另一份 `_error_event` 还是第三份「异常 → error 事件」的转换
（它的注释当时写着「与 `document_service._stream_reply` 的口径一致」——靠注释保证
一致，改一处漏一处只是时间问题）。

本模块是全项目唯一的编排实现，三个 service 都改为调用它。

职责边界：

- 本模块只做**事件编排**（状态 → 增量 → 收尾 / 错误）与「纯文本增量的产出」。
- 「分块是否被 `max_tokens` 截断」由 `stream_guard` 判断，这里不重复实现，只负责
  把 `OutputTruncatedError` 转成 `error` 事件。
- **用量计量也只落在这里**（`astream_text` 末尾）：所有流式调用都经由本模块，
  把累加写在这一处，就不会出现「某个生成入口忘了统计」。计量器本身在 `llm_meter`。
- **事前预算的检查也只在这里**（`astream_text` 开头）：与计量同理，一次覆盖全部
  调用点；判定逻辑在 `token_budget`。估算超顶时直接抛，请求**根本不会发出**。
- **分批续写也只在这里**（`iter_continuation_events`，票 #93）：撞上模型输出上限时
  接着写下一批，直到模型自己收尾。判据是**模型返回的结束原因**（`stream_guard`），
  不是字数阈值；批次之间的接缝由 `_SeamGuard` 逐字去重。三个整篇生成入口都走它，
  因此「续一篇文档」这件事只有一份实现。
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

from langchain_core.messages import AIMessageChunk, BaseMessage

from backend.core.contracts import StreamEvent
from backend.core.errors import AppError, ErrorCode
from backend.core.runtime_config import get_runtime_config
from backend.core.request_context import current_request_id
from backend.services import job_claimer
from backend.services.llm_factory import StreamingChatModel
from backend.services.llm_meter import current_meter
from backend.services.llm_utils import extract_text

logger = logging.getLogger(__name__)
from backend.services.stream_guard import (
    OutputTruncatedError,
    ensure_not_truncated,
    extract_finish_reason,
    is_truncated,
)
from backend.services.token_budget import enforce

# 流式生成失败时使用的错误码：分章节与整篇是同一条能力的两条实现路径，
# 对前端来说不该有两种错误分类。
_DEFAULT_ERROR_CODE = ErrorCode.LLM_UPSTREAM_ERROR

# 一份文档最多写几批（含首批）。这是「防跑飞」的护栏，不是正常路径的预期：
# 一份两万字上下的文档在常见输出窗口下也就四五批。
MAX_CONTINUATION_BATCHES = 6

# 单批失败最多试几次（含首次）。**批次级重试**与任务级退避（#88）不是一回事：
# 后者把整条任务退回排队、等 `next_run_at` 再被领走（落库、跨进程）；
# 这里不碰任务状态、不等待，只是「这一批没写成，从断点再要一次」。
MAX_BATCH_ATTEMPTS = 3

# 接缝比对只看已写正文的最后这么多字符。更长的重合已经不属于「接缝上的重复」，
# 而是模型整篇重写——那要靠识别而不是靠去重。
SEAM_TAIL_CHARS = 2000

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
    config = get_runtime_config()
    if not config.settings.llm_budget_enabled:
        return

    if meter is not None:
        # ⚠️ 传的是**协议**（`meter.protocol`），不是配置条目 id。限值注册表的键
        # 是协议名，传 id 不会报错，只会静默落到 32k 兜底——表现为「换了个 128k
        # 模型却老报超顶」。
        provider, model, step = meter.protocol, meter.model, meter.current_step()
    else:
        # 没有计量器时（脚本 / 单测直接调）只能看「当前使用中那条配置」。
        # 空值交给 `evaluate` 自己再兜一次，这里不猜默认供应商——env 里已经没有这个配置了。
        spec = config.active_provider
        provider = spec.protocol if spec else ""
        model = spec.model if spec else ""
        step = ""

    decision = enforce(messages, provider=provider, model=model, step=step)
    if meter is not None and decision.warned:
        meter.note_warning(decision.step, decision.ratio, decision.warning_message())


async def _astream_chunks(
    llm: StreamingChatModel,
    messages: list[BaseMessage],
    *,
    on_finish_reason: Callable[[str], None] | None = None,
) -> AsyncIterator[AIMessageChunk]:
    """核心：流式产出原始 `AIMessageChunk`，并检查预算、超时与截断，**并在此计量**。

    文本产出（`astream_text`）与工具调用捕获（`iter_reply_events_with_tools`）都复用它——
    一处覆盖全部调用点，预算/计量/重试/截断的口径不会分叉。

    **自愈也发生在这里**：失败时若**还没有向调用方交付过任何增量**（正文文本
    或 `tool_call_chunks`），就把整次调用重来一遍。重来对消费者是透明的——一帧都还没出去，
    重开 `astream` 即可。一旦交付过增量就绝不再来：那些字符/工具分片已经流到了下游撤不回来，
    重来会让消费方收到「半截 + 一份完整」的重复内容（T0 时代并发订阅踩过同款的坑）。

    **动作点续租（#82）**：每次模型调用前在这里续一次租约（`_check_budget` 之前）。
    这里是**唯一入口**——文本产出与工具调用捕获都经由它，所以「每次模型调用续一次」
    不会漏掉任何一个调用点。续租失败会抛 `LeaseLostError`，且**调用根本不发出去**：
    已经失去执行权的进程多调一次模型只是白烧钱。

    Args:
        llm: 已构造好的流式模型。
        messages: 已组装好的 LangChain 消息。
        on_finish_reason: 给了它就**不再因截断抛异常**，而是把模型返回的结束原因
            （`finish_reason` / `stop_reason`，没有则空串）交回调用方决定后续动作——
            分批续写（#93）要的正是这个事实。不给时保持原有口径：截断即抛
            `OutputTruncatedError`（审核 / 改写 / 对话这些「必须整段才能处理」的链路）。

    Yields:
        原始 `AIMessageChunk`（含文本增量与 `tool_call_chunks`）。

    Raises:
        LeaseLostError: 这一轮的执行权已失效（被取消 / 被接手），本次调用未发出。
        BudgetExceededError: 估算的输入超过预算，本次调用未发出。
        CallTimeoutError: 单次调用超过墙钟硬顶被取消。
        OutputTruncatedError: 模型输出因达到 token 上限被截断（**仅在未给
            `on_finish_reason` 时**）。
    """
    job_claimer.renew_before_action()
    _check_budget(messages)
    meter = current_meter()
    settings = get_runtime_config().settings

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
                # 彻底失败前留一条 WARNING：这是排障时「这次生成为什么挂了」的唯一直达路径。
                # 重试静默是 T4a 之前的历史现状——失败只体现在 run_summary 的 failed_calls 里，
                # 但「哪一次调用、什么原因」完全无迹可循。
                logger.warning(
                    "模型调用失败（不再重试）：%s",
                    exc,
                    exc_info=True,
                    extra={
                        "event": "llm.call_failed",
                        "attempt": attempt + 1,
                        "step": meter.current_step() if meter is not None else "",
                        "request_id": current_request_id(),
                    },
                )
                raise
            # 中间失败：重试前留一条 INFO，把「第几次重试、什么原因」从静默变成可查。
            logger.info(
                "模型调用失败，将重试（第 %d 次）",
                attempt + 1,
                extra={
                    "event": "llm.call_retry",
                    "attempt": attempt + 1,
                    "step": meter.current_step() if meter is not None else "",
                    "request_id": current_request_id(),
                },
            )
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
            if on_finish_reason is None:
                ensure_not_truncated(last_chunk)
            else:
                # 把结束原因交回调用方：分批续写按它决定「要不要接着写」，
                # 而不是拿字数阈值去猜。
                on_finish_reason(extract_finish_reason(last_chunk))
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


class BatchOutcome:
    """一次模型调用的结束原因（分批续写的判据）。

    为什么需要一个出参对象：`astream_text` 只把文本增量交给调用方，结束原因在
    `_astream_chunks` 里被 `ensure_not_truncated` 直接转成异常吃掉了。续写要的正是
    「这一批是不是被截断」这个**事实**，所以这条路上把结束原因交回来，
    异常口径留给不需要续写的调用点（审核 / 改写 / 对话）。
    """

    __slots__ = ("finish_reason",)

    def __init__(self) -> None:
        self.finish_reason = ""

    @property
    def truncated(self) -> bool:
        """这一批是不是撞上了输出上限（`length` / `max_tokens`）。"""
        return is_truncated(self.finish_reason)


def _seam_overlap(tail: str, head: str) -> int:
    """`head` 开头有多少字符是 `tail` 结尾的**逐字重复**（最长匹配，没有则 0）。

    丢掉的只有「已经紧挨在正文末尾、逐字存在」的那一段，因此这一步不丢信息：
    被切掉的字符在紧邻的前文里原样存在。这是「不漏字」的构造性保证——
    除了这段可证明的重复，正文一个字节都不动。
    """
    limit = min(len(tail), len(head))
    for length in range(limit, 0, -1):
        if tail.endswith(head[:length]):
            return length
    return 0


class _SeamGuard:
    """续写批次与已写正文之间的接缝去重。

    模型有时会「重起个头」：把上一批的结尾再写一遍，然后才往下写。直接拼就是重复。
    做法是**先扣住**批次开头的若干字符：只要它还有可能是在复述上一批的结尾就继续扣；
    一旦确认不可能（写到了前文里没有的地方）或整段结尾被复述完，就结算一次——丢掉重合
    那段，其余原样吐出。此后这批的剩余增量**直通**，不再扣。

    为什么判断条件是「这段字在前文里出现过」而不是「它是前文的结尾」：模型复述的是
    上一批的**某一段结尾**，随着它一个字一个字往下写，中间状态必然是「前文的子串」
    而不是「前文的后缀」（例：前文是 `甲。`，续写从 `甲。乙。` 起头，写到 `甲` 时它
    出现在前文开头、并不在后缀上）。等它写到前文里没有的地方，才谈得上「复述到哪里为止」。

    为什么不写成「字数到了就认为写完」：这只有逐字比对答得上来；阈值既会拦下正常续写，
    也会放过真正的重复。扣住的是**待判定的字符**，不是丢弃的字符：批次结束时
    `flush` 一定把它们交出去（失败路径同样），所以扣住本身不丢字。
    """

    __slots__ = ("_tail", "_held", "_settled")

    def __init__(self, tail: str) -> None:
        self._tail = tail[-SEAM_TAIL_CHARS:]
        self._held = ""
        self._settled = False

    def feed(self, text: str) -> str:
        """喂一片增量，返回**可以立刻发出去**的部分（可能为空串，表示继续扣着）。"""
        if self._settled:
            return text
        self._held += text
        if len(self._held) <= len(self._tail) and self._held in self._tail:
            # 到目前为止仍能在前文里逐字找到：还不能判断它是「复述」还是「新内容」
            return ""
        return self._settle()

    def flush(self) -> str:
        """批次结束（正常收尾或失败）：把扣住的部分结算后交出来。"""
        return "" if self._settled else self._settle()

    def _settle(self) -> str:
        self._settled = True
        return self._held[_seam_overlap(self._tail, self._held) :]


async def _astream_batch_text(
    llm: StreamingChatModel,
    messages: list[BaseMessage],
    outcome: BatchOutcome,
) -> AsyncIterator[str]:
    """一次模型调用的文本增量，并把结束原因记进 `outcome`（**不**因截断抛异常）。

    与 `astream_text` 共用 `_astream_chunks` 的内核：预算、计量、动作点续租、
    首字前的自愈重试全部照旧，只有「截断」这一件事换成了回报而不是抛出。
    """

    def _record(reason: str) -> None:
        outcome.finish_reason = reason

    async for chunk in _astream_chunks(llm, messages, on_finish_reason=_record):
        text = extract_text(chunk.content)
        if text:
            yield text


def _resume_messages(
    base: list[BaseMessage],
    continuation_messages: Callable[[str], list[BaseMessage]],
    written: str,
) -> list[BaseMessage]:
    """断批重来 / 续写下一批时的消息：写过东西就从正文接着写，否则从头再来。"""
    return continuation_messages(written) if written else base


async def iter_continuation_events(
    llm: StreamingChatModel,
    messages: list[BaseMessage],
    *,
    continuation_messages: Callable[[str], list[BaseMessage]],
    status_text: str | None = None,
    extra_data: dict[str, Any] | None = None,
    code: ErrorCode = _DEFAULT_ERROR_CODE,
) -> AsyncIterator[StreamEvent]:
    """整篇生成的**分批续写**流程：状态 → 增量*（→ 续写批次）→ done。

    与 `iter_reply_events` 的区别只有一件事：模型因为输出上限被截断时**接着写**，
    而不是把整篇判成失败。事件形状不变（`chunk` 照旧逐片下发），额外在每批续写开始时
    补一条 `stage(writing)` + `data.continuation`（第几批），于是：

    - 用户看到的是**正文继续长**，不是多出一块（`chunk` 只有一条缓冲）；
    - 进度条**不回到起点**：续写仍然报 `writing`，`outlining` 不会再发；
    - 批次之间不重复：接缝由 `_SeamGuard` 逐字去重（见它的说明）。

    批次内失败（断流等可重试的传输层失败）**在同一批里重试**，从「已经写到哪」继续；
    重试用尽则整篇失败（`error`，不发 `done`），已生成的部分留在流里——收尾把它落成一版
    「部分完成」是 #85 的能力，本函数只保证一个字都不撤。

    Args:
        llm: 已构造好的流式模型。
        messages: 首批的完整消息（各产物自己的提示词）。
        continuation_messages: 由「已写正文」构造续写请求的函数。三份产物共用同一个
            构造器（`document_service._continuation_messages`），因此续写口径不会分叉。
        status_text: 首个状态事件的提示语；为 `None` 时不发状态事件。
        extra_data: 附加到 `done`（与续写阶段事件）的数据，如 `doc_type`。
        code: 出错时 `error` 事件携带的错误码。

    Yields:
        `StreamEvent`：`status?` → 若干 `chunk` →（`stage` + 若干 `chunk`）* → `done`；
        失败时为 `error`，**之后不再发 `done`**。
    """
    if status_text:
        yield StreamEvent(type="status", content=status_text)

    written: list[str] = []
    batch_messages = messages
    batch_no = 1
    while True:
        outcome = BatchOutcome()
        attempt = 1
        while True:
            written_text = "".join(written)
            guard = _SeamGuard(written_text) if written_text else None
            try:
                async for text in _astream_batch_text(llm, batch_messages, outcome):
                    if guard is not None:
                        text = guard.feed(text)
                        if not text:
                            continue
                    written.append(text)
                    yield StreamEvent(type="chunk", content=text)
                if guard is not None:
                    rest = guard.flush()
                    if rest:
                        written.append(rest)
                        yield StreamEvent(type="chunk", content=rest)
            except Exception as exc:  # noqa: BLE001 - 要不要重试由 `_is_retryable` 判
                # 这一批已经流出去的字撤不回来：它们**留在正文里**，重试从断点继续。
                if guard is not None:
                    rest = guard.flush()
                    if rest:
                        written.append(rest)
                        yield StreamEvent(type="chunk", content=rest)
                if attempt >= MAX_BATCH_ATTEMPTS or not _is_retryable(exc):
                    yield error_event(exc, code)
                    return
                logger.info(
                    "续写批次失败，将在批次内重试（第 %d 次）：%s",
                    attempt,
                    exc,
                    extra={"event": "doc.batch_retry", "batch": batch_no, "attempt": attempt},
                )
                attempt += 1
                batch_messages = _resume_messages(
                    messages, continuation_messages, "".join(written)
                )
                continue
            break

        if not outcome.truncated:
            break
        if batch_no >= MAX_CONTINUATION_BATCHES:
            # 批数用尽还在报「被截断」：如实失败，别把一份确定没写完的稿子当成品交出去。
            yield error_event(OutputTruncatedError(outcome.finish_reason), code)
            return
        batch_no += 1
        batch_messages = _resume_messages(messages, continuation_messages, "".join(written))
        yield StreamEvent(
            type="stage",
            content=f"正在续写第 {batch_no} 段…",
            data={**(extra_data or {}), "stage": "writing", "continuation": batch_no},
        )

    reply = "".join(written).strip()
    yield StreamEvent(type="done", content="", data={**(extra_data or {}), "reply": reply})


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
