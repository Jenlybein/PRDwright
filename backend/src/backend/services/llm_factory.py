"""LLM 工厂。

统一按配置创建 LangChain 聊天模型实例，业务层不需要关心具体供应商，
只需 `from backend.services.llm_factory import get_llm`。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Protocol

from langchain_core.messages import AIMessageChunk, BaseMessage

from backend.core.config import get_settings

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


def get_llm(
    model: str | None = None,
    temperature: float = DEFAULT_TEMPERATURE,
    **kwargs: object,
) -> StreamingChatModel:
    """按配置创建 LLM 实例。

    供应商由 `DEFAULT_LLM_PROVIDER` 决定，模型默认取 `DEFAULT_LLM_MODEL`，
    API 密钥取对应供应商的环境变量（缺失时直接报错）。

    Args:
        model: 覆盖默认模型名称；不传则用配置里的 `default_llm_model`。
        temperature: 生成温度，默认 0.7。
        **kwargs: 透传给具体模型构造函数的额外参数，如 `max_tokens`、`timeout`、
            `base_url`（显式传入时优先于环境变量里的自定义地址）。

    Returns:
        LangChain 聊天模型实例（`ChatAnthropic` / `ChatOpenAI`）或火山方舟封装
        （`VolcanoArkChatModel`）。

    Raises:
        RuntimeError: 所选供应商的 API 密钥未配置。
        ValueError: 配置了不支持的供应商（正常情况下被配置层的 Literal 拦住）。
    """
    settings = get_settings()
    provider = settings.default_llm_provider
    model = model or settings.default_llm_model
    # 密钥缺失时在此直接失败，避免等到真正调用模型时才发现问题
    api_key = settings.require_api_key()
    # 自定义地址只在配置了时才注入，留空则交给 SDK 使用官方默认地址
    extra: dict[str, object] = dict(kwargs)

    # 超时与重试的统一口径（ADR-016）：
    # - `llm_timeout` 为 0 时表示沿用各家 SDK 自己的默认值，此时不传该参数。
    # - SDK 自带的重试一律显式关掉，交由 `stream_reply` 统一接管。不是因为它不安全
    #   （它的判定发生在「拿到响应」层，边界等同于「只在首字之前」），而是因为它在
    #   本层不可见——废弃掉的尝试无法计量，回执上的 token 会系统性偏乐观。
    #   不关掉还会与本层的重试叠成 `2 × N` 次。
    # 业务层若通过 kwargs 显式指定了同名参数，以它的为准（走 `setdefault`）。
    timeout = settings.llm_timeout or None

    if provider == "ark":
        # 火山方舟走官方 SDK，而不是 OpenAI 兼容伪装
        from backend.services.ark_llm import VolcanoArkChatModel

        return VolcanoArkChatModel(
            api_key=api_key,
            model=model,
            base_url=settings.ark_base_url or None,
            temperature=temperature,
            max_tokens=settings.ark_max_tokens,
            thinking_enabled=settings.ark_thinking_enabled,
            timeout=timeout,
        )

    if provider == "anthropic":
        # 延迟导入：只加载当前用到的供应商依赖
        from langchain_anthropic import ChatAnthropic

        if settings.anthropic_api_url:
            extra.setdefault("base_url", settings.anthropic_api_url)
        # Anthropic 用 max_tokens 控制单次回复上限，避免长文档被截断
        extra.setdefault("max_tokens", settings.anthropic_max_tokens)
        if timeout is not None:
            extra.setdefault("default_request_timeout", timeout)
        # 关掉 SDK 自带重试，让重试只在 stream_reply 这一处发生（见上面的说明）
        extra.setdefault("max_retries", 0)

        return ChatAnthropic(
            model=model,
            api_key=api_key,
            temperature=temperature,
            **extra,
        )

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        if settings.openai_api_url:
            extra.setdefault("base_url", settings.openai_api_url)
        # OpenAI 用 max_tokens 控制单次回复上限（新版本也支持 max_completion_tokens）
        extra.setdefault("max_tokens", settings.openai_max_tokens)
        if timeout is not None:
            extra.setdefault("request_timeout", timeout)
        # 同上：关掉 SDK 自带重试，重试由 stream_reply 统一管
        extra.setdefault("max_retries", 0)

        return ChatOpenAI(
            model=model,
            api_key=api_key,
            temperature=temperature,
            **extra,
        )

    raise ValueError(f"不支持的 LLM 供应商：{provider}")
