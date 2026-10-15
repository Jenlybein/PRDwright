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

# 供应商 → 密钥所在的环境变量名。
# 错误文案（`require_api_key`）与健康检查的自检（`api/health.py`）共用这一份，
# 避免两处各写一遍之后悄悄漂移——那会变成「报错说缺 A、体检说缺 B」的经典故障。
API_KEY_ENV_VARS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "ark": "ARK_API_KEY",
    "openai": "OPENAI_API_KEY",
}


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

    # --- Token 预算（事前） --------------------------------------------------
    # 事后计量（llm_meter）只能告诉你「刚才花了多少」；这几个开关负责在调用
    # **发出之前**拦住明显失控的请求（如章节上下文滚雪球）。详见 services/token_budget.py。
    llm_budget_enabled: bool = Field(
        default=True,
        description="是否在调用模型前做 token 预算检查。关闭后只做事后计量，"
        "不做任何拦截——仅在排查「是不是误拦了正常请求」时临时关闭。",
    )
    llm_budget_default_cap: int = Field(
        default=120_000,
        ge=1,
        description="业务顶的默认值（token）。故意小于模型的物理窗口：让用户在逼近"
        "模型极限之前就先收束，而不是等到上游返回一句厂商自己的报错。",
    )
    llm_budget_caps: str = Field(
        default="",
        description="按 step 覆盖业务顶，形如 `prd:writing=60000,api_docs=40000`。"
        "step 是「任务类型:阶段」（如 prd:writing / prd:reviewing）；只写任务类型"
        "（如 `prd=60000`）则对该类型下所有阶段生效。未列出的 step 用 "
        "llm_budget_default_cap；格式写错的项会被忽略而不是让服务起不来。",
    )
    llm_budget_warn_ratio: float = Field(
        default=0.8,
        gt=0,
        le=1,
        description="估算值达到有效预算的这个比例时记一条告警（随 run_summary 回传）。"
        "只提示、不拦截。",
    )
    llm_context_window: int = Field(
        default=0,
        ge=0,
        description="覆盖模型注册表里的上下文窗口（token）；0 表示用注册表的值。"
        "换了表里没有的型号、或厂商调整了窗口时用这一个变量，不必改代码。",
    )

    # --- 超时与重试（单次调用自愈） ------------------------------------------
    # 背景：三家 SDK 各自带着「默认重试 2 次 + 默认超时 600 秒」在跑，而项目从未显式
    # 配置过它们——不知道重试在发生，比没有重试更糟。下面四个开关把这件事摆到明面上，
    # 并把重试收归 `stream_reply` 自己管（ADR-016），让废弃的尝试也能被计量。
    llm_timeout: float = Field(
        default=60.0,
        ge=0,
        description="单次调用的**帧间隔**超时（秒）：相邻两次数据之间最多等多久。"
        "注意它不是总时长——httpx 的 read timeout 是 inter-byte 语义，"
        "一个每 59 秒才吐一个字的模型会把这次调用无限拖下去（要约束总时长请配 "
        "llm_total_timeout）。0 表示沿用各家 SDK 自己的默认值（实测三家均为 600 秒）。",
    )
    llm_total_timeout: float = Field(
        default=1800.0,
        ge=0,
        description="单次调用的墙钟硬顶（秒），超时即取消这次调用。"
        "由上一条推导而来：帧间隔超时管不住「一直在吐、只是很慢」的情况。"
        "0 表示不设硬顶。此范围要留足余量——触发时机是按章节调用，不是整份 PRD。",
    )
    llm_max_retries: int = Field(
        default=1,
        ge=0,
        description="单次调用在**尚未产出任何内容之前**失败时，最多额外重试几次。"
        "一旦已经向前端交付过增量就不再重试（增量撤不回，重来会产生重复内容）。"
        "各适配器的 `max_retries` 会显式设为 0，避免 SDK 内部再重试一层。",
    )
    llm_retry_delay: float = Field(
        default=1.5,
        ge=0,
        description="两次尝试之间的等待秒数（固定退避）。次数只有一次，"
        "用不上指数退避；留一点间隔是为了避开上游刚抖动完的那几个瞬间。",
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

    # --- 技能包（PRD 生成的提示词资产） --------------------------------------
    skills_strict: bool = Field(
        default=False,
        description="技能资源缺失时是否中止启动（fail-fast）。默认 false 以便本地调试："
        "技能包不在位时服务照常起，只是本次生成退回内置提示词（会在回执上显式标出）。"
        "**部署环境应设为 true**——漏打包 skills/ 目录应在起服务时就炸出来，"
        "而不是等某次生成的 PRD 风格悄悄变了才被发现。",
    )

    # --- 可观测性（日志） ----------------------------------------------------
    log_level: str = Field(
        default="INFO",
        description="日志级别（DEBUG / INFO / WARNING / ERROR）。认不出的名字退回 INFO，"
        "不让一个拼错的配置把服务拦在启动之外。",
    )
    log_format: Literal["text", "json"] = Field(
        default="text",
        description="日志格式。`text` 给人看（本地开发默认）；`json` 一行一条 JSON，"
        "给日志采集用（部署建议设为 json）。两者带**同一组字段**，"
        "所以切换格式不会丢信息——尤其是每条日志里的 `request_id`。",
    )

    # --- 认证（登录 / 注册） --------------------------------------------------
    auth_enabled: bool = Field(
        default=True,
        description="是否启用登录鉴权。开启时除白名单接口外一律要求登录；"
        "关闭时所有接口放行，并以内置的 dev 用户身份运行，便于本地功能调试。"
        "**生产环境必须保持开启**。",
    )
    auth_session_ttl_hours: int = Field(
        default=168,
        ge=1,
        description="登录会话的有效时长（小时），默认 7 天。过期后需要重新登录。",
    )
    auth_cookie_name: str = Field(
        default="prdwright_session",
        description="存放会话 token 的 Cookie 名称。",
    )
    auth_cookie_secure: bool = Field(
        default=False,
        description="会话 Cookie 是否只允许通过 HTTPS 传输。"
        "本地 http 调试必须为 false，部署到 HTTPS 环境后应改为 true。",
    )
    invite_code: str = Field(
        default="123456",
        description="初始邀请码。首次启动且 invite_codes 表为空时自动播种这一条"
        "（不限使用次数、永不过期）；注册时必须填写有效的邀请码。",
    )
    init_admin_user: str = Field(
        default="",
        description="初始管理员用户名，仅在 users 表为空时自动创建，用于初始化首个账号。"
        "留空则不自动创建。",
    )
    init_admin_password: str = Field(
        default="",
        description="初始管理员密码，配合 init_admin_user 使用。",
    )

    # --- 数据库 --------------------------------------------------------------
    db_path: str = Field(
        default="",
        description="SQLite 数据库文件路径；留空则使用后端项目根目录下的 prdwright.db。",
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
    def budget_step_caps(self) -> dict[str, int]:
        """把 `llm_budget_caps` 解析成 `{step: cap}`。

        解析不了的项**静默丢弃**：这是配置项，写错一个不该让整个服务起不来；
        未配置任何项时返回空字典，由调用方回落到 `llm_budget_default_cap`。
        """
        caps: dict[str, int] = {}
        for item in self.llm_budget_caps.split(","):
            name, sep, raw = item.partition("=")
            name = name.strip()
            if not sep or not name:
                continue
            try:
                value = int(raw.strip())
            except ValueError:
                continue
            if value > 0:
                caps[name] = value
        return caps

    @property
    def cors_origins_list(self) -> list[str]:
        """把 cors_origins 按逗号拆分并清洗，返回来源列表。"""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def db_file(self) -> Path:
        """SQLite 数据库文件的绝对路径。

        未显式配置 `db_path` 时，放在后端项目根目录下的 `prdwright.db`。
        """
        if self.db_path:
            return Path(self.db_path).expanduser().resolve()
        return BACKEND_ROOT / "prdwright.db"

    def api_key_env_var(self) -> str:
        """当前所选供应商对应的 API 密钥环境变量名；供应商未知时为空串。

        给两处用：`require_api_key` 的报错文案、以及健康检查里「密钥没配」的自检。
        **只有这一处知道「哪个供应商看哪个变量」**，加供应商时不会漏掉某一边。
        """
        return API_KEY_ENV_VARS.get(self.default_llm_provider, "")

    def require_api_key(self) -> str:
        """返回当前所选供应商对应的 API 密钥。

        Raises:
            RuntimeError: 所选供应商的密钥未配置时抛出。
        """
        if self.default_llm_provider == "anthropic":
            key = self.anthropic_api_key
        elif self.default_llm_provider == "ark":
            key = self.ark_api_key
        else:
            key = self.openai_api_key
        if not key:
            raise RuntimeError(
                f"尚未配置 {self.api_key_env_var()}，但当前请求的供应商是"
                f" '{self.default_llm_provider}'。"
                f"请在 {ENV_FILE} 中设置（可参考 .env.example）。"
            )
        return key


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回进程内共享的配置单例（带缓存）。"""
    return Settings()


settings: Settings = get_settings()
