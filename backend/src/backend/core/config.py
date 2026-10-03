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
        description="OpenAI API 密钥。",
    )
    # 自定义 API 地址：用于自建网关 / 中转代理 / 兼容服务，留空则用官方默认地址
    anthropic_api_url: str = Field(
        default="",
        description="Anthropic API 地址；留空则使用官方默认地址。",
    )
    openai_api_url: str = Field(
        default="",
        description="OpenAI API 地址；留空则使用官方默认地址。",
    )

    # --- 默认模型选择 --------------------------------------------------------
    default_llm_provider: Literal["anthropic", "openai"] = Field(
        default="anthropic",
        description="默认使用的 LLM 供应商。",
    )
    default_llm_model: str = Field(
        default="claude-sonnet-4-20250514",
        description="所选供应商下的默认模型名称。",
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
        key = (
            self.anthropic_api_key
            if self.default_llm_provider == "anthropic"
            else self.openai_api_key
        )
        if not key:
            env_var = (
                "ANTHROPIC_API_KEY"
                if self.default_llm_provider == "anthropic"
                else "OPENAI_API_KEY"
            )
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
