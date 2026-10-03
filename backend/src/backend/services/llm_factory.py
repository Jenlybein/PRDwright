"""LLM 工厂。

统一按配置创建 LangChain 聊天模型实例，业务层不需要关心具体供应商，
只需 `from backend.services.llm_factory import get_llm`。
"""

from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel

from backend.core.config import get_settings

# 默认生成温度：兼顾稳定性与创造力
DEFAULT_TEMPERATURE = 0.7


def get_llm(
    model: str | None = None,
    temperature: float = DEFAULT_TEMPERATURE,
    **kwargs: object,
) -> BaseChatModel:
    """按配置创建 LLM 实例。

    供应商由 `DEFAULT_LLM_PROVIDER` 决定，模型默认取 `DEFAULT_LLM_MODEL`，
    API 密钥取对应供应商的环境变量（缺失时直接报错）。

    Args:
        model: 覆盖默认模型名称；不传则用配置里的 `default_llm_model`。
        temperature: 生成温度，默认 0.7。
        **kwargs: 透传给具体模型构造函数的额外参数，如 `max_tokens`、`timeout`、
            `base_url`（显式传入时优先于环境变量里的自定义地址）。

    Returns:
        LangChain 聊天模型实例（`ChatAnthropic` 或 `ChatOpenAI`）。

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

    if provider == "anthropic":
        # 延迟导入：只加载当前用到的供应商依赖
        from langchain_anthropic import ChatAnthropic

        if settings.anthropic_api_url:
            extra.setdefault("base_url", settings.anthropic_api_url)

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

        return ChatOpenAI(
            model=model,
            api_key=api_key,
            temperature=temperature,
            **extra,
        )

    raise ValueError(f"不支持的 LLM 供应商：{provider}")
