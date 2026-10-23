"""运行时可改配置：**读**这一侧的唯一入口。

它解决什么问题
--------------
在此之前所有配置都在 `.env`：改一个值要改文件、重启服务。后台因此无法调参数，
「换模型供应商」更是要改代码里的 `Literal` 与分支。

现在解析顺序是 **DB 覆盖项 > env > 代码默认值**：

- `system_config` 里**只存覆盖项**。没有某一行 = 用 env 的值。
  所以「升级后行为零变化」是结构保证，而不是靠人记得别写默认值。
- 白名单之外的键**永不查库**（连 `SELECT` 都不需要）。`db_path` / 日志 / 认证
  这类「改了必须重启」的项刻意不在白名单里——后台也不显示它们。
- ⚠️ **模型接入不在这条链上**（ADR-051）：接哪家、走哪个协议、地址、型号、
  输出上限、思考开关只住在 `model_providers` 表里，`.env` 里连字段都没有。
  所以本模块除了「白名单参数」还负责一件事：算出**当前使用中那一条配置**
  （`active_provider`）。一条都没有时它是 `None`——生成会明确报错，
  而不是退回某个界面上看不见、也改不了的默认值。

三条铁律
--------
1. **只读、不写、不抛**。启动期表还没建、行被写坏、库打不开……一律降级成
   「只有 env 的配置」并记一条 WARN。这**不等于**「生成一定成功」：
   没有可用模型配置时 `active_provider` 是 `None`，由调用方给出人话报错。
2. **一次生成冻结一份快照**（`use_config`）。这同时满足两个相反的需求：
   「改了下次生成生效」与「跑到一半不会参数漂移」。
3. **白名单是硬编码常量**。不在表里、不在配置里，改白名单要改代码——
   它是安全边界（不能让后台改到 `db_path` 或 `auth_enabled`），
   能通过配置扩展的白名单等于没有白名单。

递归风险在哪（以及为什么这里没有）
----------------------------------
`core/db.py` 每次 `connect()` 都会读 `get_settings().db_file`。如果配置层也参与
「连哪个库」的决策，就会变成「连库取配置 → 配置决定连哪个库」。
本模块的防线是**结构性的**：`db_path` 永不在白名单里，所以这条路径根本不存在。
`_loading` 守卫只是第二道保险，防的是将来有人在 `Settings` 的校验器里调本模块。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Annotated, Iterator

from pydantic import TypeAdapter, ValidationError

from backend.core.config import Settings, get_settings
from backend.core.engine import session_scope
from backend.core.model_registry import ModelLimits, input_ceiling
from backend.core.providers import ProviderSpec, spec_from_row
from backend.repositories.runtime_config_repo import (
    ModelProviderRepository,
    SystemConfigRepository,
    decode_extra,
)

logger = logging.getLogger(__name__)

# 覆盖项的键名（存进 `system_config`）。它不是一个 `Settings` 字段，
# 所以单独列出来——把它混进白名单会让「字段校验」那段代码要特判。
ACTIVE_PROVIDER_KEY = "active_provider_id"

# 白名单：**只有这些键可以覆盖**。其余键即使被写进表里也不认（并会记 WARN）。
# 选择标准是「改完之后下一次生成就生效」，凡是「改了必须重启」的一律不收：
# 日志级别/落盘、db 路径、认证开关、CORS、host/port、技能目录。
WHITELIST: tuple[str, ...] = (
    # 分章节生成
    "prd_section_review_enabled",
    "prd_section_max_rewrites",
    "prd_pipeline_concurrency",
    "prd_section_max_retries",
    # 审核与改写
    "review_rewrite_min_severity",
    "doc_section_rewrite_enabled",
    "doc_review_max_tokens",
    "prd_section_review_max_tokens",
    # 调用与预算
    "llm_timeout",
    "llm_total_timeout",
    "llm_max_retries",
    "llm_retry_delay",
    "llm_budget_enabled",
    "llm_budget_default_cap",
    "llm_budget_caps",
    "llm_budget_warn_ratio",
    # ⚠️ `llm_context_window` 不在这里，也不再存在于 `Settings`（ADR-051）：
    # 窗口属于「这条配置」，由 `model_providers.context_window` 决定。
    # 每日额度（v4.5 T5-2）。这五项都是「每次检查时现读」，所以改完下一次生成即生效。
    # 注意 `quota_timezone_offset_minutes` 也在这里：它改的是「日界怎么算」，
    # 不是「进程启动时算一次」——否则跨时区部署就没法在不重启的前提下纠正。
    "quota_daily_task_limit",
    "quota_daily_token_limit",
    "quota_max_running",
    "quota_enforce",
    "quota_timezone_offset_minutes",
    # 安全（v4.5 T5-3）。这五项都必须在**使用的那一刻**现读，不能启动时缓存：
    # `auth_session_ttl_hours` 若被 `OpaqueSessionIssuer` 在导入时抓走，
    # 后台改完看起来没反应（见 services/auth_service.get_token_issuer）；
    # `auth_cookie_secure` 只在签发 Cookie 时读，改完下一次登录生效。
    "auth_allow_register",
    "auth_session_ttl_hours",
    "auth_cookie_secure",
    "login_max_attempts",
    "login_failure_window_minutes",
)

# 成功快照的 TTL。多 worker 部署时它决定「改了配置多久在所有进程生效」的上界。
_TTL_OK = 5.0
# 降级快照的 TTL 更短：表可能刚建好（启动期），要尽快重试而不是死等 5 秒。
_TTL_DEGRADED = 1.0

# 第二道递归保险（见模块 docstring 最后一节）
_loading = False


@dataclass(frozen=True)
class RuntimeConfig:
    """某一刻的生效配置快照。

    `settings` 是把覆盖项 `model_copy` 进去之后的 `Settings`——派生属性
    （如 `budget_step_caps`）照旧可用，所以读取点从 `get_settings()` 换成
    `get_runtime_config().settings` **不需要改任何取值写法**。

    ⚠️ 它**不含**模型接入（供应商 / 协议 / 地址 / 型号）：那些在这份快照的
    `active_provider` 里，而且可能为 `None`（一条配置都没有），见 ADR-051。
    """

    settings: Settings
    active_provider: ProviderSpec | None
    overrides: dict[str, Any] = field(default_factory=dict)
    source: str = "env"
    loaded_at: float = 0.0
    revision: str = ""

    def provider_missing_key(self) -> str:
        """激活供应商缺密钥时的提示文案；不缺则返回空串。"""
        spec = self.active_provider
        if spec is None or not spec.value_token:
            return ""
        from backend.core.crypto import is_configured

        return "" if is_configured(spec.value_token) else spec.key_hint


# 任务级冻结：`job_service.dispatch` 取快照后 `use_config` 把它挂到上下文，
# 整条生成链路（含子任务）都读同一份，直到任务结束。
_frozen: ContextVar[RuntimeConfig | None] = ContextVar("runtime_config_frozen", default=None)

# 进程级缓存
_snapshot: RuntimeConfig | None = None
# 快照是基于哪一个 `Settings` 对象算出来的（**持有引用**，防 id 复用）。
# 只比 TTL 是不够的：`get_settings.cache_clear()` 之后会有一个全新的 Settings，
# 但旧快照还在 TTL 内——表现为「改了环境变量，配置层还要迟钝 5 秒」。
# 绑定对象身份后，清缓存即等于让快照失效。
_snapshot_base: Settings | None = None


def _revision_of(overrides: dict[str, Any]) -> str:
    """配置指纹。前端用它判断「保存后真的换了一份配置」而不是拿到回声。"""
    payload = json.dumps(overrides, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()[:12]


def _validator_for(key: str) -> TypeAdapter:
    """按 `Settings` 里的字段定义造一个单字段校验器。

    **复用而不是重写**：`ge` / `le` / `gt` / `Literal` 只定义在 `config.py` 一处，
    两处各写一套迟早会漂移（配置页允许 `concurrency=99`，而 env 那边是 1..8）。

    ⚠️ `Literal[...]` 这类字段的约束在 **annotation 本身**里、`metadata` 为空，
    此时不能再套 `Annotated`（它要求至少两个参数）。分两种情况处理，
    否则「枚举项能校验、数值项不能校验」这类半残会很难发现。
    """
    info = Settings.model_fields[key]
    if not info.metadata:
        return TypeAdapter(info.annotation)
    annotated = Annotated.__class_getitem__((info.annotation, *info.metadata))
    return TypeAdapter(annotated)


def validate_value(key: str, value: Any) -> Any:
    """校验并归一一个覆盖值；非法时抛 `ValueError`（带人话）。

    Raises:
        ValueError: 键不在白名单、或值不符合该字段的约束。
    """
    if key not in WHITELIST:
        raise ValueError(f"配置项 {key} 不允许在后台修改")
    try:
        return _validator_for(key).validate_python(value)
    except ValidationError as exc:
        # 只取第一条错误的说明：pydantic 的完整错误对管理员太吵，
        # 而第一条通常就是真正的错因。
        detail = exc.errors()[0].get("msg", "取值不合法")
        raise ValueError(f"配置项 {key} 的取值不合法：{detail}") from exc


def _apply_overrides(base: Settings, decoded: dict[str, Any]) -> Settings:
    """把已校验的覆盖项补进 `Settings`。

    用 `model_copy(update=...)` 而不是逐字段赋值：`Settings` 没开
    `validate_assignment`，直接赋值**不校验**；而这里传进来的值已经在
    `validate_value` 里校验过，走 `model_copy` 能保持「整份对象一次性换掉」的语义，
    不会出现「改了一半」的中间态被别的协程读到。
    """
    if not decoded:
        return base
    return base.model_copy(update=decoded)


def _resolve_active_provider(override_id: str | None) -> ProviderSpec | None:
    """定出「当前使用中哪一条配置」；**一条都没有时返回 `None`**。

    优先级：
    1. `system_config.active_provider_id`（后台显式选定的）；
    2. 没有指针 / 指针悬空 / 指针指向已停用的那条 → 按 `sort_order` 取**第一条可用的**
       （仓库层已按 `sort_order, created_at` 排序，所以结果稳定可预期）。

    ⚠️ **不再有第 3 条「env 合成」**（ADR-051）。模型接入已整体迁入后台面板，
    `.env` 里没有第二份默认供应商可回退——写进去也不会被读。列表为空时
    `None` 就是**正确答案**：调用方据此报一句「请先在后台新增一条」，
    而不是拿着一个界面上看不见、也改不了的 spec 继续跑。

    ⚠️ 第 2 条**不能按 id 查**：`model_providers.id` 现在是 `cfg_xxx`，
    而厂商名（`ark` / `openai`）是**协议**的近似值。按 id 找必然落空，
    于是「管理员明明配好了那一条，运行时却根本不用它」——而且不报错。
    """
    rows: list[dict[str, Any]] = []
    try:
        # 每次读配置开一个**短**作用域，不把 session 存进配置快照里：
        # 快照是进程级单例（TTL 5 秒），让它持有一条池连接等于长期占用池子，
        # 并发一上来就是「借不到连接 → 接口全超时」。
        with session_scope() as session:
            rows = [
                row.model_dump()
                for row in ModelProviderRepository(session).list_all()
            ]
    except Exception:  # noqa: BLE001 - 表可能还不存在
        rows = []

    if not rows:
        return None

    by_id = {str(row["id"]): row for row in rows}

    if override_id:
        row = by_id.get(override_id)
        if row is None:
            logger.warning(
                "使用中的配置已不存在，回退到第一条可用配置",
                extra={"event": "config.active_provider_missing", "provider_id": override_id},
            )
        elif not int(row.get("enabled") or 0):
            logger.warning(
                "使用中的配置已被停用，回退到第一条可用配置",
                extra={"event": "config.active_provider_disabled", "provider_id": override_id},
            )
        else:
            return spec_from_row({**row, "extra": decode_extra(row.get("extra_json"))})

    # 没有明确指针：取第一条**启用中**的。此时「哪条在用」这件事本身没有歧义——
    # 只有一条可用就没有选择的余地，让用户多点一次「设为使用中」只是仪式。
    for row in rows:
        if not int(row.get("enabled") or 0):
            continue
        return spec_from_row({**row, "extra": decode_extra(row.get("extra_json"))})

    # 全被停用：这也是合法状态（管理员在收摊），生成前会明确报错。
    logger.warning(
        "所有模型配置都已停用，生成将无法进行",
        extra={"event": "config.no_enabled_provider"},
    )
    return None


def _load() -> RuntimeConfig:
    """读一次配置并组装快照。**永不抛**。"""
    global _loading
    base = get_settings()
    source = "env"
    overrides: dict[str, Any] = {}
    active_id: str | None = None

    if _loading:
        # 走到这里说明有人在配置的加载路径里又调了本模块（预期外的递归）。
        # 直接返回只有 env 的快照，把递归就地掐断。
        # ⚠️ 这里**不解析模型配置**：解析本身要查库，那正是递归的来源。
        # 返回 `None` 是安全的——调用方本来就按「没有可用配置」处理。
        return RuntimeConfig(
            settings=base,
            active_provider=None,
            source="env",
            loaded_at=time.monotonic(),
            revision=_revision_of({}),
        )

    _loading = True
    try:
        try:
            with session_scope() as session:
                raw = SystemConfigRepository(session).get_all()
        except Exception:  # noqa: BLE001 - 表不存在 / 库不可用：降级而不是抛
            logger.warning(
                "读取运行时配置失败，本次使用环境变量配置",
                exc_info=True,
                extra={"event": "config.load_degraded"},
            )
            raw = {}
            source = "degraded"

        for key, text in raw.items():
            if key == ACTIVE_PROVIDER_KEY:
                try:
                    active_id = str(json.loads(text))
                except (ValueError, TypeError):
                    logger.warning(
                        "激活供应商的值无法解析，忽略",
                        extra={"event": "config.bad_override", "key": key},
                    )
                continue
            if key not in WHITELIST:
                # 白名单外的行可能是历史遗留或手工写进去的。不认，但要留痕——
                # 静默忽略会让人以为「我明明改了却没生效」。
                logger.warning(
                    "配置项不在白名单内，已忽略",
                    extra={"event": "config.unknown_key", "key": key},
                )
                continue
            try:
                overrides[key] = validate_value(key, json.loads(text))
            except (ValueError, TypeError) as exc:
                # 单个坏值不拖垮整份配置：跳过它，该键回落到 env 值。
                logger.warning(
                    "配置项取值非法，已回落到环境变量：%s",
                    exc,
                    extra={"event": "config.bad_override", "key": key},
                )

        if overrides:
            source = "db"

        settings = _apply_overrides(base, overrides)
        provider = _resolve_active_provider(active_id)

        return RuntimeConfig(
            settings=settings,
            active_provider=provider,
            overrides=overrides,
            source=source,
            loaded_at=time.monotonic(),
            revision=_revision_of(overrides),
        )
    finally:
        _loading = False


def get_runtime_config() -> RuntimeConfig:
    """取当前生效配置（带进程内缓存，默认 5 秒）。

    这是热路径：`get_llm`、`stream_reply`、`token_budget` 每次调用都会来问一次。
    所以必须缓存——每次查库会让「一次生成几十次模型调用」变成几十次 SQL。

    缓存的有效期由两个条件共同决定：**TTL 未过** 且 **底层 `Settings` 还是同一个对象**。
    后者让 `get_settings.cache_clear()`（改环境变量后的标准动作）能立刻让快照失效，
    而不是等满 5 秒——否则「改了 env 却不生效」这种错觉会反复出现。
    """
    frozen = _frozen.get()
    if frozen is not None:
        return frozen

    global _snapshot, _snapshot_base
    base = get_settings()
    now = time.monotonic()
    if _snapshot is not None and _snapshot_base is base:
        ttl = _TTL_DEGRADED if _snapshot.source == "degraded" else _TTL_OK
        if now - _snapshot.loaded_at < ttl:
            return _snapshot
    _snapshot = _load()
    _snapshot_base = get_settings()
    return _snapshot


def invalidate() -> None:
    """丢掉缓存，下一次访问重新读库。

    写配置之后**必须**调用：否则本进程还会用旧快照最多 5 秒，
    表现为「保存成功、但立刻试一下还是老行为」——最容易让人误判的一种。
    """
    global _snapshot, _snapshot_base
    _snapshot = None
    _snapshot_base = None


@contextmanager
def use_config(config: RuntimeConfig | None) -> Iterator[None]:
    """把一份快照冻在上下文里（任务级）。

    由 `job_service.dispatch` 与直连 SSE 的路由使用。任务内所有读取点都会
    读到它，于是「任务跑到一半有人改了配置」不会让同一次生成前后参数不一致
    ——那会让章节乱序、正文重复，且极难归因。

    `None` 表示「不冻结」（没有可用快照时），此时什么都不做而不是抛错：
    配置冻结是优化，不是正确性的前置条件。
    """
    if config is None:
        yield
        return
    token = _frozen.set(config)
    try:
        yield
    finally:
        try:
            _frozen.reset(token)
        except ValueError:
            # 生成器可能在**另一个 task** 里被关闭（客户端断开时 GC 触发），
            # 那个 Context 与 set 时的不是同一个，token 无法 reset。
            # 这没关系：该 Context 随即被丢弃，不会污染别的请求。
            # 不处理的话，客户端一断开就在日志里刷 ValueError，把真正的
            # 错误淹掉——而这恰恰发生在「用户说生成断了」的时候。
            pass


def active_provider() -> ProviderSpec | None:
    """当前激活的供应商。"""
    return get_runtime_config().active_provider


def resolve_model_limits() -> ModelLimits:
    """当前供应商 + 模型的物理上限。

    先看数据库里该供应商填的 `context_window` / `max_output`（自定义供应商的
    窗口 `model_registry` 不认识，不填就会落到 32k 兜底，把正常请求误判超顶），
    没填再退回注册表。
    """
    config = get_runtime_config()
    spec = config.active_provider
    if spec is None:
        return ModelLimits(context_window=32_000, max_output=4_096)

    from backend.core.model_registry import resolve_limits

    base = resolve_limits(spec.protocol, spec.model)
    window = spec.context_window or base.context_window
    max_output = spec.max_output or base.max_output
    return ModelLimits(context_window=window, max_output=max_output)


def current_input_ceiling(override: int = 0) -> int:
    """当前配置下「可用于输入」的 token 上限。

    窗口只有一个来源：**当前使用中那条配置**填的 `context_window`，
    没填才退回 `model_registry`。不再有全局的 `LLM_CONTEXT_WINDOW` 覆盖项
    （ADR-051）——它会静默盖掉条目里的值，表现为「换了大窗口还老报超顶」。
    """
    limits = resolve_model_limits()
    window = override or limits.context_window
    return max(window - limits.max_output, 1_000)


def config_service_window() -> int:
    """给预算检查用的窗口值（`token_budget.evaluate` 的入口）。"""
    return current_input_ceiling()


def provider_key_present(spec: ProviderSpec | None = None) -> bool:
    """该供应商的密钥是否**可用**（非空且能被当前主密钥解开）。只回布尔，绝不回值。"""
    target = spec or active_provider()
    if target is None or not target.value_token:
        return False
    from backend.core.crypto import is_configured

    return is_configured(target.value_token)


def lookup_provider_key(spec: ProviderSpec) -> str:
    """取该供应商的密钥明文。缺失或解不开时抛 `RuntimeError`。

    与体检（`/health` 的 llm 项）同一口径：**在构造 LLM 的前一刻**才失败，
    而不是等到真正发请求。

    ⚠️ 本地 vLLM / Ollama / 内网网关这类不要密钥的接入点，请把密钥填成一个
    占位值（如 `not-needed`）——OpenAI 兼容客户端要求 `api_key` 非空，传空串
    会在构造时抛「api_key must be set」。占位值服务端不会校验。
    """
    from backend.core.crypto import decrypt_value

    if not spec.value_token:
        raise RuntimeError(spec.key_hint)
    value = decrypt_value(spec.value_token)
    if not value:
        raise RuntimeError(spec.key_hint)
    return value


def input_ceiling_for(spec: ProviderSpec) -> int:
    """某个 spec 的输入上限（构造预算检查时用，避免二次取快照）。"""
    from backend.core.model_registry import resolve_limits

    base = resolve_limits(spec.protocol, spec.model)
    window = spec.context_window or base.context_window
    max_output = spec.max_output or base.max_output
    return max(window - max_output, 1_000)
