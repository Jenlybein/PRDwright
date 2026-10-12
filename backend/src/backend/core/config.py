"""应用配置。

所有运行时配置都从环境变量读取，并允许被后端项目根目录（backend/.env）下的
本地 .env 文件覆盖。复制 .env.example 为 .env 即可开始使用。

使用方式::

    from backend.core.config import get_settings

    settings = get_settings()
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# config.py -> core/ -> backend/(包) -> src/ -> backend/(项目根目录)
BACKEND_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = BACKEND_ROOT / ".env"


class Settings(BaseSettings):
    """后端服务的运行时配置。"""

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,  # 环境变量名不区分大小写
        extra="ignore",  # 忽略 .env 中出现的多余变量
    )

    # --- LLM 供应商凭据 ------------------------------------------------------
    # 默认为空，这样即使没有密钥服务也能启动；真正调用模型时再通过
    # require_api_key() 惰性获取，缺失时才报错。
    anthropic_api_key: str = Field(
        default="",
        description="Anthropic Claude API 密钥。",
    )
    openai_api_key: str = Field(
        default="",
        description="OpenAI API 密钥（通用 OpenAI 兼容分支使用）。",
    )
    # 自定义 API 地址：用于自建网关 / 中转代理 / 兼容服务，留空则用官方默认地址
    anthropic_api_url: str = Field(
        default="",
        description="Anthropic API 地址；留空则使用官方默认地址。",
    )
    openai_api_url: str = Field(
        default="",
        description="OpenAI API 地址（通用 OpenAI 兼容分支使用）；留空则使用官方默认地址。",
    )

    # 火山方舟（Volcano Ark）：调用豆包等模型走官方 SDK，而不是 OpenAI 兼容伪装
    ark_api_key: str = Field(
        default="",
        description="火山方舟（Volcano Ark）API 密钥，用于调用豆包系列模型。",
    )
    ark_base_url: str = Field(
        default="https://ark.cn-beijing.volces.com/api/v3",
        description="火山方舟 API 地址；一般无需修改。",
    )
    ark_thinking_enabled: bool = Field(
        default=False,
        description="是否开启方舟混合思考模型的深度思考（reasoning）。"
        "默认关闭，模型直接回答，避免先吐一大段思考再出正文导致的「首字极慢 + 正文爆发」观感。"
        "后续若接入自主可控的思考机制，与此开关无关。",
    )
    ark_max_tokens: int = Field(
        default=4096,
        description="火山方舟模型单次回复的最大 token 数。默认 4096 会截断较长文档"
        "（finish_reason=length），生成 PRD 等长文时需调大（如 131072 即 128k）。",
    )
    anthropic_max_tokens: int = Field(
        default=4096,
        description="Anthropic Claude 单次回复的最大 token 数。注意 Anthropic 的"
        "截断标记为 stop_reason=max_tokens。",
    )
    openai_max_tokens: int = Field(
        default=4096,
        description="OpenAI（兼容协议）单次回复的最大 token 数。",
    )

    # --- 默认模型选择 --------------------------------------------------------
    default_llm_provider: Literal["anthropic", "openai", "ark"] = Field(
        default="ark",
        description="默认使用的 LLM 供应商。",
    )
    default_llm_model: str = Field(
        default="doubao-seed-2-1-turbo-260628",
        description="所选供应商下的默认模型名称。",
    )

    # --- PRD 分章节生成 ------------------------------------------------------
    # 模板本身就是「6 章 + 待确认附录」，逐章撰写可以让每章都带上已定稿的前序章节，
    # 从而保证 6.1 引用的功能编号、附录汇总的「待确认」与前文一致；
    # 代价是 LLM 调用次数从 1 次变成「章节数 × (1 + 审核 + 改写)」次。
    prd_section_review_enabled: bool = Field(
        default=True,
        description="生成 PRD 时是否启用「写作 → 审核 → 改写」中的审核环节。"
        "关闭后只逐章撰写，不再调用审核 Agent，可显著降低时延与费用。",
    )
    prd_section_max_rewrites: int = Field(
        default=1,
        ge=0,
        le=3,
        description="单个章节最多改写的轮数。0 表示只审核不改写；"
        "达到上限即保留当前版本，避免审核与改写互相拉扯导致无限循环。",
    )

    # --- 服务配置 ------------------------------------------------------------
    cors_origins: str = Field(
        default="http://localhost:5173",
        description="允许跨域访问的来源，多个用英文逗号分隔。",
    )
    host: str = Field(
        default="0.0.0.0",
        description="服务监听地址。",
    )
    port: int = Field(
        default=8000,
        description="服务监听端口。",
    )

    @field_validator("default_llm_provider", mode="before")
    @classmethod
    def _normalize_provider(cls, value: object) -> object:
        """兼容大小写，例如 ANTHROPIC / OpenAI 都能识别。"""
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @property
    def cors_origins_list(self) -> list[str]:
        """把 cors_origins 按逗号拆分并清洗，返回来源列表。"""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    def require_api_key(self) -> str:
        """返回当前所选供应商对应的 API 密钥。

        Raises:
            RuntimeError: 所选供应商的密钥未配置时抛出。
        """
        if self.default_llm_provider == "anthropic":
            key = self.anthropic_api_key
            env_var = "ANTHROPIC_API_KEY"
        elif self.default_llm_provider == "ark":
            key = self.ark_api_key
            env_var = "ARK_API_KEY"
        else:
            key = self.openai_api_key
            env_var = "OPENAI_API_KEY"
        if not key:
            raise RuntimeError(
                f"尚未配置 {env_var}，但当前请求的供应商是 '{self.default_llm_provider}'。"
                f"请在 {ENV_FILE} 中设置（可参考 .env.example）。"
            )
        return key


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回进程内共享的配置单例（带缓存）。"""
    return Settings()


settings: Settings = get_settings()
