"""系统配置与供应商的后台服务。

职责边界
--------
- **规则只在这里**：能不能删、能不能激活、密钥怎么表达、审计记什么，
  全部落在本模块。接口层只做「解析 → 调用 → 渲染」。
- **SQL 不在这里**：读写都经 `repositories/runtime_config_repo`。
- **写完之后必须 `invalidate()`**：否则本进程还会用旧快照最多 5 秒，
  表现为「保存成功、立刻试一下还是老行为」——最容易让人误判的一种。

为什么字段的展示元数据也放在这里
--------------------------------
「这个配置项叫什么、属于哪一组、要不要二次确认、取值范围是多少」——这些是**产品
定义**，不是接口细节。放在这里，前端只负责渲染；将来加一个配置项只需改这一处
（`_FIELD_META`）+ 白名单，不必前后端各改一遍（那正是漂移的来源）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from backend.core import runtime_config as rc
from backend.core.config import Settings, get_settings
from backend.core.errors import AppError, ErrorCode
from backend.core.providers import (
    PRESETS,
    PROTOCOL_LABELS,
    PROTOCOLS,
    ProviderSpec,
    spec_from_row,
)
from backend.core.secrets import is_present
from backend.repositories.runtime_config_repo import (
    ModelProviderRepository,
    SystemConfigRepository,
    decode_extra,
)
from backend.services.admin_service import record_audit
from backend.services.auth_service import User

logger = logging.getLogger(__name__)

# 配置项的分组与展示元数据。`danger=True` 的项前端要弹二次确认——
# 它们的共同点是「改错了不明显，但影响很大」。
_GROUPS: tuple[tuple[str, str, str], ...] = (
    ("prd", "分章节生成", "控制 PRD 逐章撰写的节奏。并发越高越快，也越容易撞供应商限流。"),
    ("review", "审核与改写", "审核是锦上添花：它自己失败不会拖垮正文，但会多花 token。"),
    ("llm", "调用与预算", "单次调用的超时、重试，以及发出请求前的 token 预算检查。"),
    ("quota", "每日额度", "每个用户每天能生成多少。默认全为不限——不配置就等于没这层限制。"),
)

_FIELD_META: dict[str, dict[str, Any]] = {
    "prd_section_review_enabled": {
        "group": "prd",
        "label": "逐章审核",
        "help": "关闭后只逐章撰写，不再调用审核 Agent。省时省钱，但正文不再被自动检查与改写。",
        "danger": True,
    },
    "prd_section_max_rewrites": {
        "group": "prd",
        "label": "单章最多改写轮数",
        "help": "0 表示只审核不改写。达到上限即保留当前版本，避免审核与改写互相拉扯。",
    },
    "prd_pipeline_concurrency": {
        "group": "prd",
        "label": "并发章节数",
        "help": "1 表示退回串行（每章都能带上已定稿的前序章节）；越高越快，也越容易撞限流。",
    },
    "prd_section_max_retries": {
        "group": "prd",
        "label": "单章失败重试次数",
        "help": "只重试该阶段本身。与单次调用重试是两层，实际调用次数是两者之积。",
    },
    "review_rewrite_min_severity": {
        "group": "review",
        "label": "触发改写的最低严重度",
        "help": "默认 high。选 medium 会让「几乎必然命中」的批注也触发改写，时延翻倍而收益很小。",
        "danger": True,
    },
    "doc_section_rewrite_enabled": {
        "group": "review",
        "label": "整篇改写按章节进行",
        "help": "关闭后退回整篇重写：更省调用次数，但改动更容易溢出到无关章节。",
    },
    "doc_review_max_tokens": {
        "group": "review",
        "label": "整篇审核输出上限",
        "help": "审核只吐一段 JSON 批注，不需要生成级的上限。太小会导致 JSON 被截断、审核按通过处理。",
    },
    "prd_section_review_max_tokens": {
        "group": "review",
        "label": "单章审核输出上限",
        "help": "只审一章，可以比整篇更小。太小同样会让 JSON 被截断。",
    },
    "llm_timeout": {
        "group": "llm",
        "label": "帧间隔超时（秒）",
        "help": "相邻两次数据之间最多等多久。0 表示沿用各家 SDK 默认值。",
    },
    "llm_total_timeout": {
        "group": "llm",
        "label": "单次调用墙钟硬顶（秒）",
        "help": "帧间隔超时管不住「一直在吐、只是很慢」的模型，用它兜底。0 表示不设硬顶。",
    },
    "llm_max_retries": {
        "group": "llm",
        "label": "单次调用重试次数",
        "help": "只在「尚未产出任何内容」时重试；已经开始吐字就不再重试（增量撤不回）。",
    },
    "llm_retry_delay": {
        "group": "llm",
        "label": "重试间隔（秒）",
        "help": "固定退避。次数只有一次，用不上指数退避；留一点间隔是为了避开上游刚抖动完的那几个瞬间。",
    },
    "llm_budget_enabled": {
        "group": "llm",
        "label": "启用事前预算检查",
        "help": "关闭后只做事后计量、不做拦截。仅在排查「是不是误拦了正常请求」时临时关闭。",
        "danger": True,
    },
    "llm_budget_default_cap": {
        "group": "llm",
        "label": "默认业务顶（token）",
        "help": "故意小于模型物理窗口：让用户逼近极限前就收束，而不是等上游报错。",
    },
    "llm_budget_caps": {
        "group": "llm",
        "label": "按步骤覆盖业务顶",
        "help": "形如 prd:writing=60000,api_docs=40000。写错的项会被忽略而不是让服务起不来。",
    },
    "llm_budget_warn_ratio": {
        "group": "llm",
        "label": "预算告警比例",
        "help": "估算值达到有效预算的这个比例时记一条告警（随回执返回）。只提示、不拦截。",
    },
    "llm_context_window": {
        "group": "llm",
        "label": "覆盖上下文窗口（token）",
        "help": "0 表示用模型注册表/供应商配置的值。换表里没有的型号时用这一项，不必改代码。",
    },
    "quota_daily_task_limit": {
        "group": "quota",
        "label": "每人每日生成次数",
        "help": "0 表示不限。只统计已收尾与在飞的任务——失败与被取消的不吃额度，"
        "否则一次上游故障就会把所有人的额度清零。",
    },
    "quota_daily_token_limit": {
        "group": "quota",
        "label": "每人每日 Token 上限",
        "help": "0 表示不限，统计输入 + 输出。只算任务收尾时落库的实际用量，"
        "所以在飞任务尚未计入——它必须与并发上限一起用，单靠它拦不住同时开多个大任务。",
        "danger": True,
    },
    "quota_max_running": {
        "group": "quota",
        "label": "每人同时运行任务数",
        "help": "0 表示不限。这一项防的不是钱而是队列：一个人开满并发会拖慢所有人的生成。",
    },
    "quota_enforce": {
        "group": "quota",
        "label": "超限时真的拦截",
        "help": "关闭后只记日志不拒绝（事件名 quota.exceeded），用于先观察真实用量分布。",
        "danger": True,
    },
    "quota_timezone_offset_minutes": {
        "group": "quota",
        "label": "每日重置时区（分钟）",
        "help": "默认 480（东八区，即北京时间 00:00 重置）。必须由服务端定，"
        "否则用户改一下浏览器时区就能把日界挪走、绕开额度。",
    },
}

# 布尔项与枚举项在前端的控件类型
_BOOL_KEYS = frozenset(
    {"prd_section_review_enabled", "doc_section_rewrite_enabled", "llm_budget_enabled"}
)
_ENUM_OPTIONS: dict[str, list[str]] = {
    "review_rewrite_min_severity": ["high", "medium", "low"],
}


def _constraints(key: str) -> dict[str, Any]:
    """从 `Settings` 的字段定义里取出 `ge` / `le` / `gt`，给前端做校验提示。

    **不另写一份**：约束的真源是 `config.py` 的 `Field(...)`，这里只是把它读出来。
    两处各写一套，迟早出现「配置页允许 99，后端 env 只允许 8」。
    """
    info = Settings.model_fields.get(key)
    if info is None:
        return {}
    bounds: dict[str, Any] = {}
    for meta in info.metadata:
        for attr, name in (("ge", "min"), ("le", "max"), ("gt", "min_exclusive")):
            value = getattr(meta, attr, None)
            if value is not None:
                bounds[name] = value
    return bounds


def _item_of(key: str, overrides: dict[str, Any]) -> dict[str, Any]:
    """一个配置项的完整描述（值、默认值、来源、控件类型、约束）。"""
    defaults = get_settings()
    meta = _FIELD_META.get(key, {})
    effective = overrides.get(key, getattr(defaults, key))
    if key in _BOOL_KEYS:
        kind = "bool"
    elif key in _ENUM_OPTIONS:
        kind = "enum"
    elif isinstance(getattr(defaults, key), (int, float)):
        kind = "number"
    else:
        kind = "text"
    return {
        "key": key,
        "value": effective,
        "default": getattr(defaults, key),
        "source": "db" if key in overrides else "env",
        "type": kind,
        "label": meta.get("label", key),
        "help": meta.get("help", ""),
        "danger": bool(meta.get("danger")),
        "options": _ENUM_OPTIONS.get(key),
        **_constraints(key),
    }


def get_config() -> dict[str, Any]:
    """分组返回全部可调项 + 当前激活供应商 + 版本指纹。"""
    config = rc.get_runtime_config()
    overrides = config.overrides

    groups = []
    for key, label, help_text in _GROUPS:
        items = [_item_of(name, overrides) for name in rc.WHITELIST if _FIELD_META.get(name, {}).get("group") == key]
        groups.append({"key": key, "label": label, "help": help_text, "items": items})

    spec = config.active_provider
    return {
        "groups": groups,
        "active_provider_id": spec.id if spec else "",
        "active_provider": _provider_view(spec) if spec else None,
        "config_source": config.source,
        "revision": config.revision,
        "missing_key": config.provider_missing_key(),
    }


def _provider_view(spec: ProviderSpec | None) -> dict[str, Any] | None:
    if spec is None:
        return None
    return {
        "id": spec.id,
        "name": spec.name,
        "protocol": spec.protocol,
        "protocol_label": PROTOCOL_LABELS.get(spec.protocol, spec.protocol),
        "base_url": spec.base_url,
        "default_model": spec.model,
        "max_tokens": spec.max_tokens,
        "context_window": spec.context_window,
        "max_output": spec.max_output,
        "thinking_enabled": spec.thinking,
    }


def _diff_detail(before: dict[str, Any], after: dict[str, Any]) -> str:
    """把变更压成一行 `key: 旧 → 新；…`，用于审计。

    ⚠️ 密钥类字段绝不进这一行。当前白名单里没有密钥，但将来若加了
    `*_api_key` 之类的键，这里的判断能兜住——审计表是给人翻的，不是保险箱。
    """
    parts: list[str] = []
    for key in sorted(after):
        old = before.get(key)
        new = after[key]
        if old == new:
            continue
        if any(marker in key.lower() for marker in ("key", "secret", "token_value")):
            parts.append(f"{key}: ***")
            continue
        parts.append(f"{key}: {old!r} → {new!r}")
    return "；".join(parts)


def update_config(actor: User, updates: dict[str, Any]) -> dict[str, Any]:
    """批量修改配置项。

    **先全量校验再写**：只要有一项非法，整批都不落库。配置是一组语义，
    写一半会留下「并发度改了、审核开关没改」这种撕裂状态，而它下次生成就生效。

    Raises:
        AppError: 键不在白名单或取值非法（400）。
    """
    if not updates:
        return get_config()

    unknown = [key for key in updates if key not in rc.WHITELIST]
    if unknown:
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            f"以下配置项不可在后台修改：{'、'.join(unknown)}",
            status_code=400,
        )

    validated: dict[str, Any] = {}
    for key, raw in updates.items():
        try:
            validated[key] = rc.validate_value(key, raw)
        except ValueError as exc:
            # 单条非法就整批拒绝——而不是跳过它继续写别的：
            # 管理员看到「部分成功」时无法判断到底哪几项生效了。
            raise AppError(ErrorCode.INVALID_REQUEST, str(exc), status_code=400) from exc

    before = rc.get_runtime_config().overrides
    SystemConfigRepository.upsert_many(
        {key: json.dumps(value, ensure_ascii=False) for key, value in validated.items()},
        updated_by=actor.id,
    )
    rc.invalidate()

    record_audit(
        actor,
        "config.update",
        target=",".join(sorted(validated)),
        detail=_diff_detail(before, {**before, **validated}),
    )
    return get_config()


def reset_config(actor: User, keys: list[str] | None = None) -> dict[str, Any]:
    """删除覆盖项（= 恢复 env 默认）。不传 `keys` 则全部恢复。"""
    targets = keys if keys else list(rc.WHITELIST)
    bad = [key for key in targets if key not in rc.WHITELIST]
    if bad:
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            f"以下配置项不可在后台修改：{'、'.join(bad)}",
            status_code=400,
        )

    deleted = SystemConfigRepository.delete_many(targets)
    rc.invalidate()
    record_audit(
        actor,
        "config.reset",
        target=",".join(sorted(targets)) if keys else "全部",
        detail=f"已删除 {deleted} 条覆盖项",
    )
    return get_config()


# ---------------------------------------------------------------------------
# 供应商
# ---------------------------------------------------------------------------


def list_providers() -> dict[str, Any]:
    """全部供应商 + 当前激活项。

    `key_present` 只回布尔：界面要能提示「这家还没配密钥」，
    但绝不能把密钥值本身带到前端。
    """
    config = rc.get_runtime_config()
    active_id = config.active_provider.id if config.active_provider else ""
    providers = []
    for row in ModelProviderRepository.list_all():
        extra = decode_extra(row.get("extra_json"))
        providers.append(
            {
                "id": row["id"],
                "name": row["name"],
                "protocol": row["protocol"],
                "protocol_label": PROTOCOL_LABELS.get(row["protocol"], row["protocol"]),
                "base_url": row["base_url"],
                "api_key_env_var": row["api_key_env_var"],
                "key_present": is_present(row["api_key_env_var"]) if row["api_key_env_var"] else False,
                "default_model": row["default_model"],
                "max_tokens": row["max_tokens"],
                "context_window": row["context_window"],
                "max_output": row["max_output"],
                "thinking_enabled": bool(row["thinking_enabled"]),
                "builtin": bool(row["builtin"]),
                "enabled": bool(row["enabled"]),
                "sort_order": row["sort_order"],
                "active": row["id"] == active_id,
                "extra": extra,
            }
        )
    return {
        "providers": providers,
        "active_provider_id": active_id,
        "presets": PRESETS,
        "protocols": [{"value": value, "label": PROTOCOL_LABELS[value]} for value in PROTOCOLS],
    }


def _provider_or_404(provider_id: str) -> dict[str, Any]:
    row = ModelProviderRepository.get(provider_id)
    if row is None:
        # 沿用既有口径：错误码只有 9 个，「找不到」统一用 SOLUTION_NOT_FOUND（404）
        raise AppError(ErrorCode.SOLUTION_NOT_FOUND, "该供应商不存在")
    return row


def _check_protocol(protocol: str) -> str:
    value = (protocol or "").strip()
    if value not in PROTOCOLS:
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            f"不支持的协议：{value or '(空)'}。可选：{'、'.join(PROTOCOLS)}",
            status_code=400,
        )
    return value


def create_provider(actor: User, payload: dict[str, Any]) -> dict[str, Any]:
    """登记一家新供应商。

    `id` 是身份（会进 `RunMeter.provider` 与回执），必须由管理员指定且不可改；
    冲突时给 400 而不是静默覆盖别人的行。
    """
    provider_id = str(payload.get("id") or "").strip()
    if not provider_id:
        raise AppError(ErrorCode.INVALID_REQUEST, "id 不能为空", status_code=400)
    if ModelProviderRepository.get(provider_id) is not None:
        raise AppError(
            ErrorCode.INVALID_REQUEST, f"供应商 id「{provider_id}」已存在", status_code=400
        )

    protocol = _check_protocol(str(payload.get("protocol") or ""))
    key_var = str(payload.get("api_key_env_var") or "").strip()
    # ⚠️ `isalnum()` 对中文也返回 True（汉字属于字母类），所以必须另加 `isascii()`。
    # 少了这一条，「我的密钥」会被当成合法变量名存进去，然后永远查不到值、
    # 报一句「尚未配置 我的密钥」——错因藏在最不像错的地方。
    bare = key_var.replace("_", "")
    if key_var and not (bare.isascii() and bare.isalnum()):
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            "密钥变量名只能包含 ASCII 字母、数字与下划线",
            status_code=400,
        )

    ModelProviderRepository.create(
        provider_id=provider_id,
        name=str(payload.get("name") or provider_id),
        protocol=protocol,
        base_url=str(payload.get("base_url") or ""),
        api_key_env_var=key_var,
        default_model=str(payload.get("default_model") or ""),
        max_tokens=int(payload.get("max_tokens") or 4096),
        context_window=int(payload.get("context_window") or 0),
        max_output=int(payload.get("max_output") or 0),
        thinking_enabled=bool(payload.get("thinking_enabled")),
        extra_json=json.dumps(payload.get("extra") or {}, ensure_ascii=False),
        sort_order=int(payload.get("sort_order") or 100),
    )
    rc.invalidate()
    record_audit(
        actor,
        "provider.create",
        target=provider_id,
        # 带上协议与密钥变量名：排查「这家为什么一直报缺密钥」时，这两样是唯一线索
        detail=f"{protocol} {payload.get('base_url') or ''}｜密钥变量 {key_var or '(未设)'}".strip(),
    )
    return list_providers()


def update_provider(actor: User, provider_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """编辑一家供应商。`id` 与 `builtin` 不可改（仓储层也会过滤）。"""
    row = _provider_or_404(provider_id)
    fields: dict[str, Any] = {}

    if "protocol" in payload and payload["protocol"]:
        fields["protocol"] = _check_protocol(str(payload["protocol"]))
    for key in ("name", "base_url", "api_key_env_var", "default_model"):
        if key in payload:
            fields[key] = str(payload[key] or "")
    for key in ("max_tokens", "context_window", "max_output", "sort_order"):
        if key in payload and payload[key] is not None:
            fields[key] = int(payload[key])
    for key in ("thinking_enabled", "enabled"):
        if key in payload:
            fields[key] = 1 if payload[key] else 0
    if "extra" in payload:
        fields["extra_json"] = json.dumps(payload["extra"] or {}, ensure_ascii=False)

    if not fields:
        return list_providers()

    before = {key: row.get(key) for key in fields}
    ModelProviderRepository.update(provider_id, fields)
    rc.invalidate()
    record_audit(
        actor,
        "provider.update",
        target=provider_id,
        detail=_diff_detail(before, fields),
    )
    return list_providers()


def delete_provider(actor: User, provider_id: str) -> dict[str, Any]:
    """删除一家供应商。

    两条拦截都写在服务层（UI 与脚本共用）：
    - **内置行不可删**：它是「回到出厂状态」的锚点，删了就再也没法恢复默认配置；
    - **激活中的不可删**：删掉之后下一次生成会因「找不到供应商」而全线失败，
      而那时管理员多半已经离开页面了。让他先激活别的，是唯一安全的顺序。
    """
    row = _provider_or_404(provider_id)
    if int(row.get("builtin") or 0):
        raise AppError(
            ErrorCode.FORBIDDEN, "内置供应商不能删除，可以改为停用", status_code=403
        )

    config = rc.get_runtime_config()
    if config.active_provider is not None and config.active_provider.id == provider_id:
        raise AppError(
            ErrorCode.FORBIDDEN, "这是当前激活的供应商，请先激活另一家", status_code=403
        )

    ModelProviderRepository.delete(provider_id)
    rc.invalidate()
    record_audit(actor, "provider.delete", target=provider_id, detail="已删除")
    return list_providers()


def activate_provider(actor: User, provider_id: str) -> dict[str, Any]:
    """激活一家供应商。

    缺密钥时**允许激活**，但会返回一条 `warning`：拦着不让激活会让「先切过去、
    再去配密钥」这个正常顺序走不通；而一旦真去生成，`get_llm()` 会用带变量名的
    人话报错。这比在保存时拦下来更好用——后者会让人以为服务坏了。
    """
    row = _provider_or_404(provider_id)
    if not int(row.get("enabled") or 0):
        raise AppError(
            ErrorCode.INVALID_REQUEST, "该供应商已停用，请先启用再激活", status_code=400
        )

    SystemConfigRepository.upsert_many(
        {rc.ACTIVE_PROVIDER_KEY: json.dumps(provider_id)}, updated_by=actor.id
    )
    rc.invalidate()

    record_audit(
        actor, "provider.activate", target=provider_id, detail=f"协议 {row['protocol']}"
    )

    key_var = str(row.get("api_key_env_var") or "")
    warning = ""
    if key_var and not is_present(key_var):
        warning = f"已激活，但 {key_var} 还没配：下一次生成会直接报错。"

    data = list_providers()
    data["warning"] = warning
    return data


def test_provider(actor: User, provider_id: str) -> dict[str, Any]:
    """真发一次最小调用，验证这家**到底能不能用**。

    为什么必须真调
    --------------
    「填对了参数」与「真的能生成」是两件事：`base_url` 少了 `/v1`、密钥是别的
    环境的、协议选错了，这些在保存时都看不出来。而这个按钮存在的全部意义就是
    在切换之前把它们试出来。

    代价控制：`max_tokens=16` + 15 秒超时 + 只要一个字的回复。它是唯一会花钱的
    管理接口，所以额度压到最低。

    无论成败都记审计：失败原因本身就是要留档的信息。
    """
    import asyncio

    row = _provider_or_404(provider_id)
    spec = spec_from_row({**row, "extra": decode_extra(row.get("extra_json"))})

    from backend.services.llm_factory import _BUILDERS, lookup_provider_key  # noqa: PLC0415

    try:
        api_key = lookup_provider_key(spec)
    except RuntimeError as exc:
        record_audit(actor, "provider.test", target=provider_id, detail=f"缺密钥：{exc}")
        return {"ok": False, "detail": str(exc), "latency_ms": 0}

    builder = _BUILDERS.get(spec.protocol)
    if builder is None:
        return {"ok": False, "detail": f"不支持的协议：{spec.protocol}", "latency_ms": 0}

    import time

    started = time.monotonic()

    async def _probe() -> str:
        llm = builder(
            spec.with_max_tokens(16), api_key=api_key, temperature=0.0, timeout=15.0
        )
        chunks: list[str] = []
        async for chunk in llm.astream([{"role": "user", "content": "hi"}]):  # type: ignore[arg-type]
            text = getattr(chunk, "content", "")
            if isinstance(text, str) and text:
                chunks.append(text)
            if chunks:
                break
        return "".join(chunks)

    try:
        # 独立事件循环：本函数在同步上下文里被调用（服务层），不能依赖外层循环
        reply = asyncio.run(asyncio.wait_for(_probe(), timeout=20))
        latency = int((time.monotonic() - started) * 1000)
        detail = f"连通正常（{latency} ms）"
        if not reply:
            # 没吐字也算通——有些模型对「hi」只回空 delta，但连接确实是好的
            detail = f"连通正常，但未返回文本（{latency} ms）"
        record_audit(actor, "provider.test", target=provider_id, detail=detail)
        return {"ok": True, "detail": detail, "latency_ms": latency}
    except Exception as exc:  # noqa: BLE001 - 测试连通性本来就要吞掉各种异常
        latency = int((time.monotonic() - started) * 1000)
        detail = f"调用失败：{type(exc).__name__}: {exc}"
        logger.warning(
            "供应商连通性测试失败",
            extra={"event": "provider.test_failed", "provider_id": provider_id},
        )
        record_audit(actor, "provider.test", target=provider_id, detail=detail)
        return {"ok": False, "detail": detail, "latency_ms": latency}
