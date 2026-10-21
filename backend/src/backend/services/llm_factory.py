"""LLM 工厂：**所有**供应商适配器与构造逻辑的唯一落点。

为什么把方舟适配器也搬进来
--------------------------
`ark_llm.py` 曾经是一个独立文件（323 行）。现在六个协议各有分支，如果每家都开一个
文件，`tests/test_layering.py` 的「允许直接 import 供应商 SDK」白名单就要从 2 个
文件长到 7 个——而白名单每多一个文件，就多一处「忘了把它加进去」的机会。
收敛到一个文件后，规则变成一句话：**只有 `llm_factory.py` 能碰供应商 SDK**。

代价是这个文件约 550 行，同时装着「工厂分发」和「一个完整的 `BaseChatModel` 子类」。
这个代价是值的：换来的是导入纪律只有一条。

六个协议与各自的参数差异（都是实测过的，别照直觉改）
----------------------------------------------------
| protocol            | 类                        | 输出上限参数        | 超时参数                  |
|---------------------|---------------------------|---------------------|---------------------------|
| `openai_compatible` | `ChatOpenAI`              | `max_tokens`        | `request_timeout`         |
| `anthropic`         | `ChatAnthropic`           | `max_tokens`        | `default_request_timeout` |
| `ark`               | 自研 `VolcanoArkChatModel` | `max_tokens`（实例）| `timeout`                 |
| `google_genai`      | `ChatGoogleGenerativeAI`  | **`max_output_tokens`** | `timeout`             |
| `nvidia`            | `ChatNVIDIA`              | `max_tokens`        | **不支持**                |
| `huggingface`       | `ChatHuggingFace`         | `max_tokens`        | `HuggingFaceEndpoint` 上  |

三家「不支持某参数」的写法必须**不传**而不是传 `None`：pydantic 校验会把
`timeout=None` 当成「显式给了个空值」的可能性，而这里干脆不构造那个键。

SDK 自带重试一律显式关掉（ADR-016）
-----------------------------------
三家 SDK 默认会自己重试 2 次。那层重试在本层**不可见**——废弃掉的尝试无法计量，
回执上的 token 会系统性偏乐观；它还会与本层的重试叠成 `2 × N` 次。
所以除方舟（自研、本来就没有内置重试）外，能关的都关掉，重试统一交给
`stream_reply._astream_chunks`。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Protocol

from arkruntime import AsyncArk
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolCallChunk,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import ConfigDict

from backend.core.providers import ProviderSpec
from backend.core.runtime_config import get_runtime_config, lookup_provider_key

# 默认生成温度：兼顾稳定性与创造力
DEFAULT_TEMPERATURE = 0.7


class StreamingChatModel(Protocol):
    """只需满足异步流式产出 `AIMessageChunk` 的聊天模型协议。

    用于解耦业务层与具体供应商 SDK：LangChain 模型与火山方舟封装
    （`VolcanoArkChatModel`，已子类化 `BaseChatModel`）都实现了 `astream`，可互换使用。
    `bind_tools` 用于澄清选择题等工具调用：绑定后 `astream` 的分块会带上
    `tool_call_chunks`。
    """

    def astream(self, messages: list[BaseMessage], **kwargs: Any) -> AsyncIterator[AIMessageChunk]:
        """异步流式产出回复，每块为 `AIMessageChunk`。"""
        ...

    def bind_tools(
        self,
        tools: list[dict[str, Any]],
        *,
        tool_choice: str | None = None,
        parallel_tool_calls: bool | None = None,
        **kwargs: Any,
    ) -> Any:
        """把工具绑定到模型，返回可 `astream` 的绑定对象。"""
        ...


# ---------------------------------------------------------------------------
# 火山方舟适配器（自研：用量帧与思考开关都是方舟特有的）
# ---------------------------------------------------------------------------

# 方舟接受的消息角色，按 LangChain 消息类型映射
_ROLE_MAP: dict[type[BaseMessage], str] = {
    SystemMessage: "system",
    HumanMessage: "user",
    AIMessage: "assistant",
    ToolMessage: "tool",
}


def _role_of(message: BaseMessage) -> str:
    """把 LangChain 消息映射到方舟角色；未知类型兜底为 user。"""
    for message_type, role in _ROLE_MAP.items():
        if isinstance(message, message_type):
            return role
    return "user"


def _content_of(message: BaseMessage) -> str:
    """提取消息正文文本，兼容字符串与多模态 `list[dict]` 两种格式。"""
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "".join(parts)
    return str(content)


def _ark_usage_metadata(usage: Any) -> dict[str, Any] | None:
    """把方舟的 `CompletionUsage` 映射成 LangChain 的 `usage_metadata`。

    LangChain 的 openai / anthropic 集成统一用 `input_tokens` / `output_tokens` /
    `total_tokens` 这套键名，方舟侧转成同一种形状，上游（`stream_reply`）就只需认一种。
    字段缺失时返回 `None`——「没给用量」与「用量为零」是两回事，前者不该被记成一次零消耗。
    """
    if usage is None:
        return None
    prompt_tokens = getattr(usage, "prompt_tokens", None)
    completion_tokens = getattr(usage, "completion_tokens", None)
    if prompt_tokens is None and completion_tokens is None:
        return None
    input_tokens = int(prompt_tokens or 0)
    output_tokens = int(completion_tokens or 0)
    total_tokens = getattr(usage, "total_tokens", None)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": int(total_tokens) if total_tokens is not None else input_tokens + output_tokens,
    }


def _message_to_ark(message: BaseMessage) -> dict[str, Any]:
    """把 LangChain 消息映射成方舟请求体里的消息 dict。

    除了正文与角色，还序列化两类「工具」字段：
    - `AIMessage.tool_calls` → `tool_calls`（`[{id, type, function:{name, arguments}}]`，
      `arguments` 为 JSON 字符串）——历史里模型上一轮的工具调用；
    - `ToolMessage.tool_call_id` → `tool_call_id`——工具结果回灌时，模型据此对回自己的调用。
    """
    role = _role_of(message)
    out: dict[str, Any] = {"role": role, "content": _content_of(message)}

    tool_calls = getattr(message, "tool_calls", None)
    if role == "assistant" and tool_calls:
        out["tool_calls"] = [
            {
                "id": tc.get("id"),
                "type": "function",
                "function": {
                    "name": tc.get("name"),
                    "arguments": json.dumps(tc.get("args") or {}, ensure_ascii=False),
                },
            }
            for tc in tool_calls
        ]

    if role == "tool":
        tool_call_id = getattr(message, "tool_call_id", None)
        if tool_call_id:
            out["tool_call_id"] = tool_call_id

    return out


def _ark_tool_call_chunks(raw: Any) -> list[ToolCallChunk]:
    """把方舟 `delta.tool_calls` 映射成 LangChain 的 `ToolCallChunk` 列表。

    `args` 是**分片 JSON 字符串**（不是 dict），交由 LangChain 的 `default_tool_chunk_parser`
    在合并后拼成完整 `tool_calls`——**不要**在这里自己拼 JSON。
    """
    if not raw:
        return []
    chunks: list[ToolCallChunk] = []
    for tc in raw:
        fn = getattr(tc, "function", None)
        chunks.append(
            ToolCallChunk(
                name=getattr(fn, "name", None) if fn is not None else None,
                args=getattr(fn, "arguments", None) if fn is not None else None,
                id=getattr(tc, "id", None),
                index=getattr(tc, "index", 0),
            )
        )
    return chunks


class VolcanoArkChatModel(BaseChatModel):
    """基于官方 arkruntime 的 LangChain 异步流式聊天模型。"""

    api_key: str
    model: str
    base_url: str | None = None
    temperature: float = 0.7
    max_tokens: int | None = None
    thinking_enabled: bool = False
    timeout: float | None = None

    model_config = ConfigDict(extra="ignore")

    @property
    def _llm_type(self) -> str:
        return "volcano_ark"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        """同步生成（抽象方法，必须实现）。业务只走 `astream`，此路径仅兜底。

        用独立事件循环收集 `_astream` 的全部分块并合并，避免与调用方的运行中循环冲突。
        """
        merged: AIMessageChunk | None = None

        async def _collect() -> None:
            nonlocal merged
            async for chunk in self._astream(
                messages, stop=stop, run_manager=run_manager, **kwargs
            ):
                merged = chunk.message if merged is None else merged + chunk.message

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_collect())
        finally:
            loop.close()

        message = merged.to_message() if merged is not None else AIMessage(content="")
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(
        self,
        tools: list[dict[str, Any]],
        *,
        tool_choice: str | None = None,
        parallel_tool_calls: bool | None = None,
        **kwargs: Any,
    ) -> Any:
        """把工具绑定到模型（覆写基类占位实现）。

        方舟的工具形状本就是 OpenAI 兼容的 `[{"type":"function","function":{...}}]`，
        直接透传即可（无需 `convert_to_openai_tool`）。绑定的 kwargs 会经
        `BaseChatModel.astream` 透传进 `_astream`（见 chat_models.py）。
        """
        return self.bind(
            tools=tools,
            tool_choice=tool_choice,
            parallel_tool_calls=parallel_tool_calls,
            **kwargs,
        )

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        """异步流式产出模型回复，每块为 `ChatGenerationChunk(message=AIMessageChunk)`。

        `kwargs` 里带 `bind_tools` 塞进来的 `tools` / `tool_choice` /
        `parallel_tool_calls`，有 `tools` 时才下发。
        """
        tools = kwargs.get("tools")
        tool_choice = kwargs.get("tool_choice")
        parallel_tool_calls = kwargs.get("parallel_tool_calls")

        client_kwargs: dict[str, Any] = {"api_key": self.api_key}
        if self.base_url:
            client_kwargs["base_url"] = self.base_url
        # 不配超时时不传这个参数，沿用 SDK 自己的默认值
        if self.timeout is not None:
            client_kwargs["timeout"] = self.timeout
        client = AsyncArk(**client_kwargs)
        try:
            request: dict[str, Any] = {
                "model": self.model,
                "messages": [_message_to_ark(message) for message in messages],
                "stream": True,
                "temperature": self.temperature,
                # 索取用量：不加这一项时，方舟在流式模式下不回吐任何 token 计数
                # （`include_usage` 的定义是「把用量放进最后一个流式分块」）
                "stream_options": {"include_usage": True},
            }
            # 显式指定单次回复的 token 上限，避免长文档被默认上限截断
            if self.max_tokens is not None:
                request["max_tokens"] = self.max_tokens
            # 仅对自带思考的方舟模型下发；默认关闭，直接回答。
            if not self.thinking_enabled:
                request["thinking"] = {"type": "disabled"}
            # 工具调用：只有绑定过工具才下发
            if tools:
                request["tools"] = tools
            if tool_choice is not None:
                request["tool_choice"] = tool_choice
            if parallel_tool_calls is not None:
                request["parallel_tool_calls"] = parallel_tool_calls

            stream = await client.chat.completions.create(**request)
            # 跨块记住结束原因：开启用量后，**携带用量的那一帧 `choices` 为空**，
            # 它自己没有 finish_reason，必须在之前的正文帧上记下来再带到最后，
            # 否则上游的截断检测（读最后一块的 finish_reason）会永远判不出截断。
            last_finish_reason = ""
            # 真末块引用：流结束后要给它打 `chunk_position="last"` 标记（见下方说明）。
            last_message: AIMessageChunk | None = None
            async for chunk in stream:
                # 先取用量、再看 choices：这是与旧实现的**唯一实质差异**。旧写法
                # `if not chunk.choices: continue` 会把用量帧整帧丢掉，表现为
                # 「开了 include_usage 却永远拿不到 usage」。
                usage_metadata = _ark_usage_metadata(getattr(chunk, "usage", None))
                choices = getattr(chunk, "choices", None) or []

                message: AIMessageChunk | None = None
                if choices:
                    choice = choices[0]
                    delta = getattr(choice, "delta", None)
                    text = getattr(delta, "content", None) or ""
                    tool_call_chunks = _ark_tool_call_chunks(getattr(delta, "tool_calls", None))
                    finish_reason = getattr(choice, "finish_reason", None)
                    if finish_reason:
                        last_finish_reason = str(finish_reason)
                    if text or tool_call_chunks or last_finish_reason:
                        message = AIMessageChunk(
                            content=text,
                            tool_call_chunks=tool_call_chunks,
                            response_metadata=(
                                {"finish_reason": last_finish_reason}
                                if last_finish_reason
                                else {}
                            ),
                            usage_metadata=usage_metadata,
                        )
                elif usage_metadata is not None:
                    # 用量帧：没有正文，但要把结束原因一并透出，让最后一块自洽
                    message = AIMessageChunk(
                        content="",
                        response_metadata=(
                            {"finish_reason": last_finish_reason}
                            if last_finish_reason
                            else {}
                        ),
                        usage_metadata=usage_metadata,
                    )
                if message is not None:
                    last_message = message
                    yield ChatGenerationChunk(message=message)

            # 标记真末块（LangChain 1.x 约定）：基类 `astream` 在流结束后若发现最后一块
            # 没带 `chunk_position="last"`，会**再补一个空尾巴块**。那个尾巴块既没有
            # `usage_metadata` 也没有 `finish_reason`，而 `stream_reply` 的计量与截断检测
            # 都读「最后一块」——尾巴块一出，用量被记成零、截断被静默吞掉。
            # 把真末块标上 "last" 即可抑制尾巴块。注意必须在生成器收尾**之前**改：
            # 基类是等 `_astream` 耗尽后才检查这个标记，此时改对象它看得到（同一引用）。
            if last_message is not None:
                last_message.chunk_position = "last"
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# 各协议分支
# ---------------------------------------------------------------------------


def _build_openai_compatible(
    spec: ProviderSpec, *, api_key: str, temperature: float, timeout: float | None
) -> StreamingChatModel:
    """OpenAI 兼容分支：覆盖绝大多数厂商（DeepSeek / Kimi / GLM / 通义 / vLLM …）。

    `base_url` 留空时交给 SDK 用官方地址——这是「不填也能用 OpenAI」的路径。
    """
    from langchain_openai import ChatOpenAI

    extra: dict[str, Any] = {"max_retries": 0}
    if spec.base_url:
        extra["base_url"] = spec.base_url
    if timeout is not None:
        extra["request_timeout"] = timeout
    return ChatOpenAI(
        model=spec.model,
        api_key=api_key,
        temperature=temperature,
        max_tokens=spec.max_tokens,
        **extra,
    )


def _build_anthropic(
    spec: ProviderSpec, *, api_key: str, temperature: float, timeout: float | None
) -> StreamingChatModel:
    """Anthropic 原生分支。

    超时参数名是 `default_request_timeout`（不是 `request_timeout`）——
    写错不会报错，只会静默退回 SDK 默认的 600 秒。
    """
    from langchain_anthropic import ChatAnthropic

    extra: dict[str, Any] = {"max_retries": 0}
    if spec.base_url:
        extra["base_url"] = spec.base_url
    if timeout is not None:
        extra["default_request_timeout"] = timeout
    return ChatAnthropic(
        model=spec.model,
        api_key=api_key,
        temperature=temperature,
        max_tokens=spec.max_tokens,
        **extra,
    )


def _build_ark(
    spec: ProviderSpec, *, api_key: str, temperature: float, timeout: float | None
) -> StreamingChatModel:
    """火山方舟分支：走官方 SDK + 自研适配器（用量帧与思考开关都是它特有的）。"""
    return VolcanoArkChatModel(
        api_key=api_key,
        model=spec.model,
        base_url=spec.base_url or None,
        temperature=temperature,
        max_tokens=spec.max_tokens,
        thinking_enabled=spec.thinking,
        timeout=timeout,
    )


def _build_google_genai(
    spec: ProviderSpec, *, api_key: str, temperature: float, timeout: float | None
) -> StreamingChatModel:
    """Google Gemini 分支。

    ⚠️ 输出上限的参数名是 `max_output_tokens`（不是 `max_tokens`）。传错名字
    pydantic 会忽略它（extra=allow），表现为「配了 8192 却仍按默认上限截断」。
    """
    from langchain_google_genai import ChatGoogleGenerativeAI

    extra: dict[str, Any] = {"max_retries": 0}
    if spec.base_url:
        extra["base_url"] = spec.base_url
    if timeout is not None:
        extra["timeout"] = timeout
    return ChatGoogleGenerativeAI(
        model=spec.model,
        google_api_key=api_key,
        temperature=temperature,
        max_output_tokens=spec.max_tokens,
        **extra,
    )


def _build_nvidia(
    spec: ProviderSpec, *, api_key: str, temperature: float, timeout: float | None
) -> StreamingChatModel:
    """NVIDIA NIM 分支。

    该类**没有 timeout 参数**（也没有 max_retries）：这里刻意不传，
    传了会被 `extra="allow"` 静默吞掉，让人误以为超时被管住了。
    """
    from langchain_nvidia_ai_endpoints import ChatNVIDIA

    extra: dict[str, Any] = {}
    if spec.base_url:
        extra["base_url"] = spec.base_url
    return ChatNVIDIA(
        model=spec.model,
        api_key=api_key,
        temperature=temperature,
        max_tokens=spec.max_tokens,
        **extra,
    )


def _build_huggingface(
    spec: ProviderSpec, *, api_key: str, temperature: float, timeout: float | None
) -> StreamingChatModel:
    """Hugging Face 分支。

    HF 是**两段式**：`HuggingFaceEndpoint`（一个 `LLM`）负责发请求，
    `ChatHuggingFace` 把它包成 chat 模型。地址走 `endpoint_url`，
    token 走 `huggingfacehub_api_token`，输出上限在两处都要给
    （endpoint 用 `max_new_tokens`、chat 包装用 `max_tokens`）。
    """
    from langchain_huggingface import ChatHuggingFace, HuggingFaceEndpoint

    endpoint_kwargs: dict[str, Any] = {
        "model": spec.model,
        "huggingfacehub_api_token": api_key,
        "max_new_tokens": spec.max_tokens,
        "temperature": temperature,
    }
    if spec.base_url:
        endpoint_kwargs["endpoint_url"] = spec.base_url
    if timeout is not None:
        endpoint_kwargs["timeout"] = timeout
    endpoint = HuggingFaceEndpoint(**endpoint_kwargs)
    return ChatHuggingFace(llm=endpoint, max_tokens=spec.max_tokens, temperature=temperature)


_BUILDERS = {
    "openai_compatible": _build_openai_compatible,
    "anthropic": _build_anthropic,
    "ark": _build_ark,
    "google_genai": _build_google_genai,
    "nvidia": _build_nvidia,
    "huggingface": _build_huggingface,
}


# ---------------------------------------------------------------------------
# 工厂入口
# ---------------------------------------------------------------------------


def config_fingerprint(max_tokens: int | None = None) -> tuple[str, str, str, int]:
    """当前配置的指纹：**变了就该重建缓存的 LLM 实例**。

    为什么必须集中在一处
    --------------------
    `document_service` / `prd_section_writer` / `conversation_service` 都缓存了
    LLM 实例。如果三处各写一套「判断配置变没变」的逻辑，就会出现
    「撰写实例换了新模型、审核实例还是旧的」——同一次生成里两个实例参数不一致，
    表现为正文和批注的口径对不上，而且极难归因。

    指纹里放四样东西：
    - `revision`：覆盖项（含所有 `*_max_tokens`、并发度、审核开关）的哈希；
    - 供应商 `id` 与 `model`：切供应商时 `revision` 可能没变（激活指针在
      `system_config` 里，也在 revision 里），但显式带上更直观、也防漏；
    - `max_tokens`：同一个供应商下「撰写用大窗口、审核用小窗口」是两个实例。
    """
    config = get_runtime_config()
    spec = config.active_provider
    return (
        config.revision,
        spec.id if spec else "",
        spec.model if spec else "",
        int(max_tokens or 0),
    )


def get_llm(
    model: str | None = None,
    temperature: float = DEFAULT_TEMPERATURE,
    **kwargs: object,
) -> StreamingChatModel:
    """按**当前生效配置**创建 LLM 实例。

    供应商来自 `runtime_config.active_provider`（后台可切换、不用重启），
    模型默认取该供应商登记的 `default_model`，密钥按其登记的变量名去环境里取。

    每次调用都**新建一个实例、不做缓存**：缓存该由调用方按需做（`document_service`
    按配置指纹缓存），工厂替它缓存反而会让「改了配置要等很久才生效」。

    Args:
        model: 覆盖模型名；不传则用该供应商登记的 `default_model`。
        temperature: 生成温度，默认 0.7。
        **kwargs: 目前只认 `max_tokens`（审核链路要更小的输出窗口）。
            ⚠️ 方舟的 `max_tokens` **只能靠实例生效**（`astream(max_tokens=...)` 不生效），
            所以这里把它折进实例参数，而不是原样传给 `astream`。

    Returns:
        LangChain 聊天模型实例或方舟封装，统一满足 `StreamingChatModel`。

    Raises:
        RuntimeError: 没有可用供应商，或所选供应商的密钥未配置。
        ValueError: 协议没有对应分支（正常被 `providers.Protocol` 拦住）。
    """
    config = get_runtime_config()
    spec = config.active_provider
    if spec is None:
        raise RuntimeError("没有可用的模型供应商，请在后台上传配置并激活一家")

    if model:
        spec = spec.with_model(str(model))

    # 调用方显式给的 max_tokens 优先于供应商登记值（审核要更小的窗口）
    override = kwargs.get("max_tokens")
    if override is not None:
        spec = spec.with_max_tokens(int(override))

    api_key = lookup_provider_key(spec)

    # 超时是「每次调用读」，所以取当前配置而不是快照里固化的实例字段；
    # 0 表示沿用各家 SDK 默认值，此时不传该参数。
    timeout = config.settings.llm_timeout or None

    builder = _BUILDERS.get(spec.protocol)
    if builder is None:
        raise ValueError(f"不支持的 LLM 协议：{spec.protocol}")

    return builder(spec, api_key=api_key, temperature=temperature, timeout=timeout)
