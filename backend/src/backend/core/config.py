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

# 供应商 → 密钥环境变量名的映射已移进 `core/providers.py`（那里还管着六个协议
# 与「新增供应商」的预设模板，是同一件事）。这里**反向再导出**，
# 让 `from backend.core.config import API_KEY_ENV_VARS` 这个既有写法、
# 以及 `Settings.api_key_env_var()` 的签名都不变。
# 依赖方向是单向的：`providers` 不 import `config`，不存在循环。
from backend.core.providers import API_KEY_ENV_VARS as API_KEY_ENV_VARS

# config.py -> core/ -> backend/(包) -> src/ -> backend/(项目根目录)
BACKEND_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = BACKEND_ROOT / ".env"

# 供应商 → 密钥所在的环境变量名。
# 错误文案（`require_api_key`）与健康检查的自检（`api/health.py`）共用这一份，
# 避免两处各写一遍之后悄悄漂移——那会变成「报错说缺 A、体检说缺 B」的经典故障。
#
# 真实定义在 `core/providers.py`（模块顶部已再导出），本行只是把「它属于这里」
# 这件事留在原处，方便按老路径读代码的人找到。


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

    # --- 每日额度（每人每天能生成多少） --------------------------------------
    # 为什么需要：邀请码只管「谁能进来」，不管「进来后用多少」。没有这一层，
    # 一个账号跑一百份 PRD 就能把预算烧掉一大截，而事前没有任何拦截手段。
    #
    # 三个默认值全是 0（不限）——这是刻意的：升级后不配置就等于没这回事，
    # 「零变化」是结构保证，不是靠人记得别设值。
    # 单个用户的覆盖值在 `users` 表的三列里（-1 跟随这里，0 不限，N 具体值）。
    quota_daily_task_limit: int = Field(
        default=0,
        ge=0,
        description="每个用户每天最多**发起**多少次生成；0 表示不限。"
        "只统计已收尾与在飞的任务，失败与被取消的不吃额度（系统故障不该罚用户）。",
    )
    quota_daily_token_limit: int = Field(
        default=0,
        ge=0,
        description="每个用户每天最多消耗多少 token（输入 + 输出）；0 表示不限。"
        "按任务收尾时落库的实际用量统计，运行中的消耗尚未计入——"
        "所以它必须与并发上限配合，单靠它拦不住同时开多个大任务。",
    )
    quota_max_running: int = Field(
        default=0,
        ge=0,
        description="每个用户同时最多几个任务在跑；0 表示不限。"
        "这一项防的不是钱而是队列：一个人开满并发会拖慢所有人的生成。",
    )
    quota_enforce: bool = Field(
        default=True,
        description="False 时**只记日志不拦截**：用于刚开放额度、想先看真实用量分布"
        "而不想立刻拒绝用户的场景。日志里的事件名是 `quota.exceeded`。",
    )
    quota_timezone_offset_minutes: int = Field(
        default=480,
        ge=-720,
        le=840,
        description="「每天」按哪个时区重置，单位分钟，默认 480（东八区）。"
        "**必须由服务端决定**：若接受前端传值，用户改一下浏览器时区就能把日界挪走。",
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
    prd_pipeline_concurrency: int = Field(
        default=3,
        ge=1,
        le=8,
        description="PRD 分章节生成时同时在飞的模型调用上限（撰写 / 审核 / 改写共用）。"
        "1 表示退回串行：每章都能带上已定稿的前序章节；大于 1 时后章基于前章草稿，"
        "跨章一致性由最终的审核结论兜底。并发越高越容易撞单供应商限流。",
    )
    prd_section_max_retries: int = Field(
        default=3,
        ge=0,
        le=5,
        description="单个章节在某一阶段失败后的最多重试次数（总尝试 = 1 + 该值）。"
        "只重试该阶段本身：撰写用尽后写占位，审核用尽后保留正文，改写用尽后保留改写前版本。"
        "与 llm_max_retries（单次调用重试）是两层，实际调用次数是两者之积。",
    )

    # --- 文档审核（整篇 / 分章节共用） ----------------------------------------
    # 审核只是「判定 + 批注」，输出量与正文不在一个量级。给它一个远小于生成的上限，
    # 既省时间也省 token；万一被截断，按「审核失败 → 通过」处理（审核是锦上添花，
    # 不能反向拖垮已经生成好的正文）。
    #
    # ⚠️ 这个上限只能靠**换一个模型实例**生效：`VolcanoArkChatModel._astream` 只读
    # `self.max_tokens`，`astream()` 传 kwargs 不生效（见 `services/llm_factory.py`）。
    doc_review_max_tokens: int = Field(
        default=2048,
        ge=256,
        description="整篇文档（接口文档 / 提示词套件）审核这次调用的 max_tokens。",
    )
    prd_section_review_max_tokens: int = Field(
        default=2048,
        ge=256,
        description="PRD 单章节审核这次调用的 max_tokens。"
        "默认值与整篇审核（2048）对齐：此前这里是 1024，但章节审核实际复用了整篇的"
        " `review_llm`（2048），于是这个配置一直是**死配置**——改了没有任何效果。"
        "把默认值对齐到 2048 并让章节审核真正用自己的实例，才不会一改就改变现有行为。",
    )
    review_rewrite_min_severity: Literal["high", "medium", "low"] = Field(
        default="high",
        description="触发改写的最低严重度，两条链路共用。"
        "默认 `high`：medium 里有「空泛表述」「结构不符」这类几乎必然命中的口径，"
        "按它改写会把「审一次 → 改一次」变成常态，时延翻倍而收益极小。"
        "审核结论里的 issues 不受此影响——medium 照样报给用户，只是不自动改。",
    )
    doc_section_rewrite_enabled: bool = Field(
        default=True,
        description="整篇文档改写是否走「分节改写」（只重写受影响的章节）。"
        "关掉即退回整篇重写——这是分节改写出问题时的一键回退开关。",
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

    # --- 日志持久化 ----------------------------------------------------------
    # 没有这一组，日志就只活在 stdout 里：进程一停历史全没，管理员后台也就无从查起。
    # 采集分两路双写：文件永远开（兜底「不丢」），落库可关（检索用，见 `log_db_enabled`）。
    log_dir: str = Field(
        default="",
        description="日志轮转文件所在目录；留空则使用后端项目根目录下的 logs/。",
    )
    log_file_max_bytes: int = Field(
        default=5 * 1024 * 1024,
        ge=1024,
        description="单个日志文件的大小上限（字节），超过即轮转到下一个文件。",
    )
    log_file_backups: int = Field(
        default=5,
        ge=0,
        description="保留的轮转文件份数。超出后最旧的那个被删除。",
    )
    log_db_enabled: bool = Field(
        default=True,
        description="是否把日志额外写进 `app_logs` 表。关掉后仍然写文件——"
        "**落库是可被牺牲的那一半，文件不是**。后台的日志检索依赖它，"
        "默认开启是因为「想看的时候才发现没存」是不可挽回的。",
    )
    log_db_min_level: str = Field(
        default="INFO",
        description="落库的最低级别。默认 `INFO`：DEBUG 的量级不可控，"
        "只在调试期临时下调，日常开会把库迅速撑大。",
    )
    log_retention_days: int = Field(
        default=7,
        ge=1,
        description="日志保留天数，超期的行会被裁剪掉。",
    )
    log_max_rows: int = Field(
        default=50_000,
        ge=1000,
        description="`app_logs` 表的行数上限，超出后按时间淘汰最旧的部分。"
        "它与 `log_retention_days` 是两个独立的闸门，谁先触发谁生效。",
    )
    log_db_batch_size: int = Field(
        default=100,
        ge=1,
        description="攒够多少条日志就写一次库。批量插入是为了不让每条日志都开一次事务。",
    )
    log_db_flush_interval: float = Field(
        default=1.0,
        ge=0.1,
        description="距上次写库超过多少秒就强制写一次（秒）。"
        "它与 `log_db_batch_size` 一起决定落库的延迟上限，"
        "保证低流量时日志也不会长时间攒在内存里。",
    )
    log_db_queue_size: int = Field(
        default=5000,
        ge=100,
        description="待落库队列的容量。**满了就丢弃新日志**（并降级告警），"
        "绝不让日志写不动反过来拖垮生成主链路。",
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
    auth_allow_register: bool = Field(
        default=True,
        description="是否开放自助注册。关闭后注册接口一律拒绝，新账号只能由管理员"
        "或运维脚本创建。已有账号的登录不受影响。",
    )
    login_max_attempts: int = Field(
        default=5,
        ge=0,
        description="同一账号在一个时间窗内允许的失败次数。超过后每次登录都会被"
        "**递增延时**（2/4/8 秒封顶）而不是锁定账号——锁定会被人拿来把别人挡在门外。"
        "填 0 表示完全不限流（不建议）。",
    )
    login_failure_window_minutes: int = Field(
        default=15,
        ge=1,
        description="登录失败次数的统计窗口（分钟）。窗口外的失败不再计入，"
        "也由登录链路顺手清理，表不会无限增长。",
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
        default="http://localhost:5173,http://localhost:5174",
        description="允许跨域访问的来源，多个用英文逗号分隔。"
        "默认含 5173（用户端）与 5174（管理员端 `frontend_admin`）两个开发源。",
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

    @property
    def log_dir_path(self) -> Path:
        """日志轮转文件所在目录的绝对路径。

        与 `db_file` 同样的规则：显式配置优先，否则落在后端项目根目录下的 `logs/`。
        """
        if self.log_dir:
            return Path(self.log_dir).expanduser().resolve()
        return BACKEND_ROOT / "logs"

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
