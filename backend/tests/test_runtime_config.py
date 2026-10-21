"""运行时配置层：默认行为零变化、白名单、降级、递归防护（v4 T3）。

这一层是全项目最危险的一段代码——它被每一次模型调用读取。所以用例的重心不是
「能读出值」，而是**读不出值时不许坏事**：

1. **默认行为零变化**：一张空表时，每个白名单键都必须等于 `get_settings()` 的值。
   这条一旦破了，上线当天所有生成的参数都会变（用户只会说「结果跟以前不一样了」）。
2. **白名单外的键永不生效**：`db_path` / `log_level` / `auth_enabled` 若能被后台改，
   等于把「改配置」变成「改部署」，而且改错了服务起不来。
3. **降级不抛**：表不存在、行写坏了，都必须退回 env 并继续跑。配置层失败不该
   让生成失败——那是最难归因的一类故障。
4. **不递归**：`connect()` 会读 `get_settings().db_file`，所以配置层**不能**参与
   「连哪个库」的决策。这条在结构上成立（`db_path` 不在白名单），用用例钉一遍
   防止将来有人好心加进去。
"""

from __future__ import annotations

import pytest

from backend.core import runtime_config as rc
from backend.core.config import get_settings
from backend.core.db import connect
from backend.core.migrations import run_migrations
from backend.core.secrets import reset_cache as reset_secret_cache
from backend.repositories.runtime_config_repo import (
    ModelProviderRepository,
    SystemConfigRepository,
)


@pytest.fixture(autouse=True)
def fresh_config(monkeypatch):
    """每个用例都从「没有缓存、没有真实 .env 干扰」开始。

    ⚠️ 屏蔽 `.env` 是必须的：本机若配了 `ARK_API_KEY`，「缺密钥」类断言会
    永远成立（看着绿，其实没验）。这里把路径指到一个不存在的文件。
    """
    from backend.core import secrets as secrets_module

    monkeypatch.setattr(secrets_module, "_env_file_path", lambda: __import__("pathlib").Path("/nonexistent/.env"))
    reset_secret_cache()
    rc.invalidate()
    yield
    reset_secret_cache()
    rc.invalidate()


def _set(**values) -> None:
    """写覆盖项（值走 JSON 编码，与生产同一格式）。"""
    import json

    SystemConfigRepository.upsert_many(
        {key: json.dumps(value) for key, value in values.items()}, updated_by=1
    )


# ---------------------------------------------------------------------------
# 默认行为零变化
# ---------------------------------------------------------------------------


def test_whitelisted_keys_match_env_when_no_overrides():
    """空表时每个白名单键都等于 env 解析出来的值。

    这是「升级后行为零变化」的核心断言。`model_copy(update={})` 看似无害，
    但只要哪个键被错误地补了个默认值，参数就会整体漂移。
    """
    run_migrations()
    config = rc.get_runtime_config()
    base = get_settings()

    assert config.overrides == {}
    assert config.source == "env"
    for key in rc.WHITELIST:
        assert getattr(config.settings, key) == getattr(base, key), f"{key} 与 env 不一致"


def test_override_applies_and_changes_revision():
    """写入覆盖项后立即生效，且指纹变化（前端据此判断「真的换了一份」）。"""
    run_migrations()
    before = rc.get_runtime_config()
    assert before.settings.prd_pipeline_concurrency == 3

    _set(prd_pipeline_concurrency=5)
    rc.invalidate()
    after = rc.get_runtime_config()

    assert after.settings.prd_pipeline_concurrency == 5
    assert after.source == "db"
    assert after.revision != before.revision, "指纹没变，前端会误判成「保存了但没生效」"


def test_delete_override_restores_default():
    """删掉覆盖行 = 恢复 env 默认（「恢复默认」按钮的语义）。"""
    run_migrations()
    _set(llm_max_retries=4)
    rc.invalidate()
    assert rc.get_runtime_config().settings.llm_max_retries == 4

    SystemConfigRepository.delete_many(["llm_max_retries"])
    rc.invalidate()
    assert rc.get_runtime_config().settings.llm_max_retries == get_settings().llm_max_retries


# ---------------------------------------------------------------------------
# 白名单与校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["db_path", "log_level", "auth_enabled", "cors_origins", "host"])
def test_rejects_keys_outside_whitelist(key):
    """启动类配置一律不许在后台改。

    它们改了必须重启，放进白名单只会制造「显示已保存、实际没变」的困惑；
    更糟的是 `db_path` 会让配置层与 `connect()` 形成递归。
    """
    with pytest.raises(ValueError, match="不允许"):
        rc.validate_value(key, "x")


def test_unknown_rows_in_table_are_ignored():
    """表里出现白名单外的行时：不生效，也不抛。"""
    run_migrations()
    SystemConfigRepository.upsert_many({"db_path": '"/tmp/hacked.db"', "log_level": '"DEBUG"'})
    rc.invalidate()
    config = rc.get_runtime_config()

    assert "db_path" not in config.overrides
    assert config.settings.log_level == get_settings().log_level


def test_out_of_range_override_falls_back_to_env():
    """越界的值跳过该键，其余键照常生效（单个坏值不拖垮整份配置）。"""
    run_migrations()
    SystemConfigRepository.upsert_many(
        {"prd_pipeline_concurrency": "99", "llm_max_retries": "3"}
    )
    rc.invalidate()
    config = rc.get_runtime_config()

    assert "prd_pipeline_concurrency" not in config.overrides
    assert config.settings.prd_pipeline_concurrency == get_settings().prd_pipeline_concurrency
    # 好的那个仍然生效
    assert config.settings.llm_max_retries == 3


def test_validation_reuses_settings_constraints():
    """约束复用 `Settings`，不另写一套。

    `prd_pipeline_concurrency` 是 `ge=1, le=8`，`llm_budget_warn_ratio` 是 `gt=0, le=1`。
    两处各写一套迟早漂移（配置页允许 99，env 那边只允许 8）。
    """
    assert rc.validate_value("prd_pipeline_concurrency", 1) == 1
    assert rc.validate_value("prd_pipeline_concurrency", 8) == 8
    with pytest.raises(ValueError):
        rc.validate_value("prd_pipeline_concurrency", 9)
    with pytest.raises(ValueError):
        rc.validate_value("prd_pipeline_concurrency", 0)

    assert rc.validate_value("llm_budget_warn_ratio", 0.5) == 0.5
    with pytest.raises(ValueError):
        rc.validate_value("llm_budget_warn_ratio", 1.5)

    assert rc.validate_value("review_rewrite_min_severity", "high") == "high"
    with pytest.raises(ValueError):
        rc.validate_value("review_rewrite_min_severity", "urgent")


# ---------------------------------------------------------------------------
# 降级
# ---------------------------------------------------------------------------


def test_degraded_when_table_missing():
    """表不存在时降级成 env 配置，**不抛**。"""
    with connect() as conn:
        conn.execute("DROP TABLE IF EXISTS system_config")
        conn.commit()
    rc.invalidate()

    config = rc.get_runtime_config()
    assert config.source == "degraded"
    assert config.overrides == {}
    # 降级后每个键仍是 env 值——生成继续能跑
    assert config.settings.prd_pipeline_concurrency == get_settings().prd_pipeline_concurrency


def test_degraded_provider_falls_back_to_env():
    """供应商表也不可用时，激活供应商由 env 合成。

    这条保证「配置表丢了」从「生成全挂」降级成「行为回到升级前」。
    """
    with connect() as conn:
        conn.execute("DROP TABLE IF EXISTS model_providers")
        conn.commit()
        conn.execute("DROP TABLE IF EXISTS system_config")
        conn.commit()
    rc.invalidate()

    config = rc.get_runtime_config()
    spec = config.active_provider
    assert spec is not None
    base = get_settings()
    assert spec.protocol == base.default_llm_provider
    assert spec.model == base.default_llm_model


def test_input_ceiling_never_recurses():
    """在 `connect()` 上下文里取配置不许递归。

    `connect()` 每次都会读 `get_settings().db_file`；若配置层也参与这条路径，
    就会「连库取配置 → 配置决定连哪个库」。这里在已经打开的连接里取配置，
    能过就说明没有递归。
    """
    run_migrations()
    with connect() as conn:
        conn.execute("SELECT 1").fetchone()
        config = rc.get_runtime_config()
        assert config.settings is not None


# ---------------------------------------------------------------------------
# 供应商解析
# ---------------------------------------------------------------------------


def test_active_provider_follows_default_when_not_activated():
    """没激活任何一家时，跟随 `DEFAULT_LLM_PROVIDER`（升级后的行为）。"""
    run_migrations()
    rc.invalidate()
    config = rc.get_runtime_config()
    base = get_settings()

    assert config.active_provider is not None
    assert config.active_provider.protocol == base.default_llm_provider
    assert config.active_provider.model == base.default_llm_model


def test_activate_switches_provider():
    """写 `active_provider_id` 后生效。"""
    run_migrations()
    _set(active_provider_id="anthropic")
    rc.invalidate()

    spec = rc.get_runtime_config().active_provider
    assert spec is not None
    assert spec.id == "anthropic"
    assert spec.protocol == "anthropic"


def test_activated_provider_missing_falls_back():
    """激活指针指向一个已删除的供应商时，回退到默认而不是崩。"""
    run_migrations()
    _set(active_provider_id="ghost")
    rc.invalidate()

    config = rc.get_runtime_config()
    assert config.active_provider is not None
    # 回退到 DEFAULT_LLM_PROVIDER 对应的那家，而不是返回 None
    assert config.active_provider.id != "ghost"


def test_disabled_provider_falls_back():
    """激活的那家被停用后回退。"""
    run_migrations()
    _set(active_provider_id="openai")
    ModelProviderRepository.update("openai", {"enabled": 0})
    rc.invalidate()

    config = rc.get_runtime_config()
    assert config.active_provider is not None
    assert config.active_provider.id != "openai"


def test_provider_missing_key_reports_variable_name():
    """缺密钥的提示必须带变量名——只说「未配置」会让人翻半天 .env。"""
    run_migrations()
    rc.invalidate()
    config = rc.get_runtime_config()
    hint = config.provider_missing_key()
    assert config.active_provider is not None
    assert config.active_provider.api_key_env_var in hint


def test_model_limits_use_provider_row_when_filled():
    """供应商行里填了窗口就用它，没填才退回注册表。

    自定义供应商（OpenAI 兼容 + 自己的模型名）在注册表里查不到，会落到 32k
    兜底——对 128k 的模型等于把正常请求误判成超顶。所以这一列必须有优先级。
    """
    run_migrations()
    ModelProviderRepository.update("ark", {"context_window": 64_000, "max_output": 8_000})
    rc.invalidate()

    limits = rc.resolve_model_limits()
    assert limits.context_window == 64_000
    assert limits.max_output == 8_000

    ModelProviderRepository.update("ark", {"context_window": 0, "max_output": 0})
    rc.invalidate()
    from backend.core.model_registry import resolve_limits

    spec = rc.get_runtime_config().active_provider
    assert spec is not None
    assert rc.resolve_model_limits().context_window == resolve_limits(spec.protocol, spec.model).context_window


# ---------------------------------------------------------------------------
# 冻结
# ---------------------------------------------------------------------------


def test_use_config_freezes_snapshot_for_task():
    """任务内冻结：`use_config` 期间改配置不影响已冻结的那一份。

    这正是「改了下次生成生效、跑到一半不漂移」的实现。少了它，
    一次 20 分钟的生成中途 TTL 到点就会换参数，表现为章节乱序或正文重复。
    """
    run_migrations()
    rc.invalidate()
    frozen = rc.get_runtime_config()
    assert frozen.settings.prd_pipeline_concurrency == 3

    with rc.use_config(frozen):
        _set(prd_pipeline_concurrency=7)
        rc.invalidate()
        inside = rc.get_runtime_config()
        assert inside is frozen
        assert inside.settings.prd_pipeline_concurrency == 3

    # 出了上下文就拿到新配置
    assert rc.get_runtime_config().settings.prd_pipeline_concurrency == 7


def test_invalidate_drops_cache():
    """`invalidate()` 之后必须重新读库。

    写配置后若不失效，本进程还会用旧快照最多 5 秒——「保存成功、立刻试还是老行为」
    是最容易让人误判的一种。
    """
    run_migrations()
    rc.invalidate()
    assert rc.get_runtime_config().settings.llm_max_retries == get_settings().llm_max_retries

    _set(llm_max_retries=5)
    rc.invalidate()
    assert rc.get_runtime_config().settings.llm_max_retries == 5
