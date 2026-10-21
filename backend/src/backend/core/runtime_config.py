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

三条铁律
--------
1. **只读、不写、不抛**。启动期表还没建、行被写坏、库打不开……一律降级成
   「只有 env 的配置」并记一条 WARN。生成主链路不能因为配置层失败而失败。
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
from backend.core.model_registry import ModelLimits, input_ceiling
from backend.core.providers import ProviderSpec, spec_from_row
from backend.core.secrets import is_present
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
    "llm_context_window",
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

    `settings` 是把覆盖项 `model_copy` 进去之后的 `Settings`——它保留了
    `budget_step_caps` / `require_api_key()` / `api_key_env_var()` 等派生属性，
    所以读取点从 `get_settings()` 换成 `get_runtime_config().settings`
    **不需要改任何取值写法**。
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
        if spec is None or not spec.api_key_env_var:
            return ""
        return "" if is_present(spec.api_key_env_var) else spec.key_env_hint


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


def _synthesize_from_env(settings: Settings) -> ProviderSpec:
    """没有数据库时的供应商 spec（降级路径）。

    这条路径必须能独立工作——表还没建、库损坏、启动早期都会走到这里。
    值全部取自 `Settings`，所以降级时的行为与**升级前完全一致**：
    这是「默认行为零变化」最后一道保证。
    """
    provider = (settings.default_llm_provider or "ark").strip().lower()
    if provider == "anthropic":
        return ProviderSpec(
            id="anthropic",
            name="Anthropic Claude",
            protocol="anthropic",
            base_url=settings.anthropic_api_url or "",
            api_key_env_var="ANTHROPIC_API_KEY",
            model=settings.default_llm_model,
            max_tokens=int(settings.anthropic_max_tokens),
        )
    if provider == "openai":
        return ProviderSpec(
            id="openai",
            name="OpenAI",
            protocol="openai_compatible",
            base_url=settings.openai_api_url or "",
            api_key_env_var="OPENAI_API_KEY",
            model=settings.default_llm_model,
            max_tokens=int(settings.openai_max_tokens),
        )
    return ProviderSpec(
        id="ark",
        name="火山方舟",
        protocol="ark",
        base_url=settings.ark_base_url or "",
        api_key_env_var="ARK_API_KEY",
        model=settings.default_llm_model,
        max_tokens=int(settings.ark_max_tokens),
        thinking=bool(settings.ark_thinking_enabled),
    )


def _resolve_active_provider(
    settings: Settings, override_id: str | None
) -> ProviderSpec | None:
    """定出「当前激活哪一家」。

    优先级：
    1. `system_config.active_provider_id`（后台显式激活的）；
    2. `DEFAULT_LLM_PROVIDER` 对应的内置行（升级后未激活任何一家时的行为）；
    3. env 合成的 spec（表不可用时的降级）。

    第 3 条不能省：它让「配置表丢了」从「生成全挂」降级成「行为回到升级前」。
    """
    rows: list[dict[str, Any]] = []
    try:
        rows = ModelProviderRepository.list_all()
    except Exception:  # noqa: BLE001 - 表可能还不存在，降级即可
        rows = []

    if not rows:
        return _synthesize_from_env(settings)

    by_id = {str(row["id"]): row for row in rows}

    if override_id:
        row = by_id.get(override_id)
        if row is None:
            logger.warning(
                "激活的供应商已不存在，回退到默认供应商",
                extra={"event": "config.active_provider_missing", "provider_id": override_id},
            )
        elif not int(row.get("enabled") or 0):
            logger.warning(
                "激活的供应商已被停用，回退到默认供应商",
                extra={"event": "config.active_provider_disabled", "provider_id": override_id},
            )
        else:
            return spec_from_row({**row, "extra": decode_extra(row.get("extra_json"))})

    # 回退：按 DEFAULT_LLM_PROVIDER 找对应 id 的内置行
    fallback_id = (settings.default_llm_provider or "ark").strip().lower()
    row = by_id.get(fallback_id)
    if row is not None and int(row.get("enabled") or 0):
        return spec_from_row({**row, "extra": decode_extra(row.get("extra_json"))})

    # 连内置行都不在（表被清空过）：用 env 合成，行为回到升级前
    return _synthesize_from_env(settings)


def _load() -> RuntimeConfig:
    """读一次配置并组装快照。**永不抛**。"""
    global _loading
    base = get_settings()
    source = "env"
    overrides: dict[str, Any] = {}
    active_id: str | None = None

    if _loading:
        # 走到这里说明有人在配置的加载路径里又调了本模块（预期外的递归）。
        # 直接返回纯 env 快照，把递归就地掐断。
        return RuntimeConfig(
            settings=base,
            active_provider=_synthesize_from_env(base),
            source="env",
            loaded_at=time.monotonic(),
            revision=_revision_of({}),
        )

    _loading = True
    try:
        try:
            raw = SystemConfigRepository.get_all()
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
        provider = _resolve_active_provider(settings, active_id)

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
    """当前配置下「可用于输入」的 token 上限（含 `llm_context_window` 覆盖）。"""
    limits = resolve_model_limits()
    config = get_runtime_config()
    window = override or config.settings.llm_context_window or limits.context_window
    return max(window - limits.max_output, 1_000)


def config_service_window() -> int:
    """给预算检查用的窗口值（`token_budget.evaluate` 的入口）。"""
    return current_input_ceiling()


def provider_key_present(spec: ProviderSpec | None = None) -> bool:
    """该供应商的密钥是否存在。只回布尔，绝不回值。"""
    target = spec or active_provider()
    if target is None or not target.api_key_env_var:
        return False
    return is_present(target.api_key_env_var)


def lookup_provider_key(spec: ProviderSpec) -> str:
    """取该供应商的密钥值。缺失时抛 `RuntimeError`（文案带变量名）。

    与 `Settings.require_api_key()` 同口径：**在构造 LLM 的前一刻**才失败，
    而不是等到真正发请求。文案里的变量名是刻意的——只说「未配置密钥」
    会让人翻半天 `.env`，而答案就在这句话里。
    """
    from backend.core.secrets import lookup_env

    value = lookup_env(spec.api_key_env_var) if spec.api_key_env_var else ""
    if not value:
        raise RuntimeError(spec.key_env_hint)
    return value


def input_ceiling_for(spec: ProviderSpec) -> int:
    """某个 spec 的输入上限（构造预算检查时用，避免二次取快照）。"""
    from backend.core.model_registry import resolve_limits

    base = resolve_limits(spec.protocol, spec.model)
    window = spec.context_window or base.context_window
    max_output = spec.max_output or base.max_output
    return max(window - max_output, 1_000)
