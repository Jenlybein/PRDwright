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
from dbsupport import connect
from backend.core.secrets import reset_cache as reset_secret_cache
from conftest import model_providers as ModelProviderRepository, system_config as SystemConfigRepository
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
    config = rc.get_runtime_config()
    base = get_settings()

    assert config.overrides == {}
    assert config.source == "env"
    for key in rc.WHITELIST:
        assert getattr(config.settings, key) == getattr(base, key), f"{key} 与 env 不一致"


def test_override_applies_and_changes_revision():
    """写入覆盖项后立即生效，且指纹变化（前端据此判断「真的换了一份」）。"""
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
    SystemConfigRepository.upsert_many({"db_path": '"/tmp/hacked.db"', "log_level": '"DEBUG"'})
    rc.invalidate()
    config = rc.get_runtime_config()

    assert "db_path" not in config.overrides
    assert config.settings.log_level == get_settings().log_level


def test_out_of_range_override_falls_back_to_env():
    """越界的值跳过该键，其余键照常生效（单个坏值不拖垮整份配置）。"""
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


def test_tables_missing_means_no_provider():
    """表都不可用时 `active_provider` 是 `None`：不崩，也不从 env 编一个出来。

    这是 ADR-051 的取舍。`.env` 里已经没有第二份模型配置，所以「配置层失败」
    只能表现为「没有可用配置」，由调用方给出人话报错（「请先在后台新增一条」）。
    悄悄用一个界面上看不见、也改不了的 spec 继续跑，会把排查方向从
    「后台还没配」误导到「为什么这次请求失败」。
    """
    with connect() as conn:
        conn.execute("DROP TABLE IF EXISTS model_providers")
        conn.commit()
        conn.execute("DROP TABLE IF EXISTS system_config")
        conn.commit()
    rc.invalidate()

    config = rc.get_runtime_config()
    assert config.active_provider is None
    assert config.source == "degraded", "降级读配置本身仍不许抛"


def test_input_ceiling_never_recurses():
    """在 `connect()` 上下文里取配置不许递归。

    `connect()` 每次都会读 `get_settings().db_file`；若配置层也参与这条路径，
    就会「连库取配置 → 配置决定连哪个库」。这里在已经打开的连接里取配置，
    能过就说明没有递归。
    """
    with connect() as conn:
        conn.execute("SELECT 1").fetchone()
        config = rc.get_runtime_config()
        assert config.settings is not None


# ---------------------------------------------------------------------------
# 供应商解析
# ---------------------------------------------------------------------------


def test_unactivated_config_uses_first_enabled_entry():
    """没设「使用中」时取**排序第一条启用中的**配置。

    ⚠️ 不能按 id 猜协议，也不再有 `DEFAULT_LLM_PROVIDER` 兜底（env 里已经没有它）：
    只有一条可用时，「哪条在用」本来就没有歧义，让用户再点一次「设为使用中」
    只是仪式。多条时按 `sort_order` 取最早建的那条，结果稳定可预期。
    """
    ModelProviderRepository.create(
        provider_id="cfg_first001",
        name="先建的那条",
        protocol="openai_compatible",
        default_model="m-first",
    )
    ModelProviderRepository.create(
        provider_id="cfg_second01",
        name="后建的那条",
        protocol="ark",
        default_model="m-second",
    )
    rc.invalidate()

    spec = rc.get_runtime_config().active_provider
    assert spec is not None
    assert spec.id == "cfg_first001"
    assert spec.model == "m-first"


def test_no_config_at_all_means_none():
    """库里一条配置都没有 → `None`。

    这是「彻底迁入后台面板」的必然结果：装完就是空的，生成必须明确报
    「还没有任何模型配置」，而不是拿 env 里某个默认值把它撑起来。
    """
    rc.invalidate()

    assert rc.get_runtime_config().active_provider is None


def test_activate_switches_to_a_config_entry():
    """写 `active_provider_id`（配置条目 id）后生效。"""
    ModelProviderRepository.create(
        provider_id="cfg_claude01",
        name="Claude",
        protocol="anthropic",
        default_model="claude-test",
    )
    _set(active_provider_id="cfg_claude01")
    rc.invalidate()

    spec = rc.get_runtime_config().active_provider
    assert spec is not None
    assert spec.id == "cfg_claude01"
    assert spec.protocol == "anthropic"


def test_dangling_pointer_falls_back_to_an_existing_entry():
    """「使用中」指向一条已删除的配置时，回退到第一条可用的，而不是崩。

    指针悬空是删配置的正常残留（也见过手工改库）。此时**不能**返回 None 了事：
    库里明明有一条能用的配置，却对用户说「还没有任何配置」，比直接用它更糟。
    """
    ModelProviderRepository.create(
        provider_id="cfg_alive001", name="还活着的", protocol="ark", default_model="m1"
    )
    _set(active_provider_id="cfg_ghost")
    rc.invalidate()

    config = rc.get_runtime_config()
    assert config.active_provider is not None
    assert config.active_provider.id == "cfg_alive001"


def test_disabled_config_falls_back_to_another_entry():
    """使用中的那条被停用后回退到另一条启用中的配置。"""
    ModelProviderRepository.create(
        provider_id="cfg_used0001", name="在用的", protocol="openai_compatible", default_model="m1"
    )
    ModelProviderRepository.create(
        provider_id="cfg_spare001", name="备用的", protocol="openai_compatible", default_model="m2"
    )
    _set(active_provider_id="cfg_used0001")
    ModelProviderRepository.update("cfg_used0001", {"enabled": 0})
    rc.invalidate()

    config = rc.get_runtime_config()
    assert config.active_provider is not None
    assert config.active_provider.id == "cfg_spare001"


def test_provider_missing_key_points_to_admin_panel():
    """缺密钥的提示必须告诉人去哪儿配——现在密钥只在管理后台录入。"""
    ModelProviderRepository.create(
        provider_id="cfg_nokey001",
        name="没配密钥的",
        protocol="ark",
        value_token="",
        default_model="m1",
    )
    rc.invalidate()
    config = rc.get_runtime_config()
    assert config.active_provider is not None
    assert "管理后台" in config.active_provider.key_hint


def test_env_context_window_is_gone(monkeypatch):
    """`LLM_CONTEXT_WINDOW` 写了也不再影响任何东西：窗口只看配置条目。

    升级用户的 `.env` 里往往还留着这一行（`extra="ignore"` 会静默吃掉它）。
    留着它的后果不是报错，而是**悄悄把后台填的窗口盖回去**：后台给某条填了 128k、
    env 里留着 32k，预算就按 32k 判——症状是「换了大窗口还老报超顶」，
    而看代码的人以为配置生效了。ADR-051 把这根线彻底拔掉。
    """
    monkeypatch.setenv("LLM_CONTEXT_WINDOW", "32000")
    get_settings.cache_clear()

    assert not hasattr(get_settings(), "llm_context_window")

    ModelProviderRepository.create(
        provider_id="cfg_bigwin01", name="大窗口", protocol="ark", default_model="m"
    )
    _set(active_provider_id="cfg_bigwin01")
    ModelProviderRepository.update("cfg_bigwin01", {"context_window": 200_000, "max_output": 8_000})
    rc.invalidate()

    assert rc.resolve_model_limits().context_window == 200_000


def test_model_limits_use_provider_row_when_filled():
    """配置条目里填了窗口就用它，没填才退回注册表。

    自定义供应商（OpenAI 兼容 + 自己的模型名）在注册表里查不到，会落到 32k
    兜底——对 128k 的模型等于把正常请求误判成超顶。所以这一列必须有优先级。
    """
    ModelProviderRepository.create(
        provider_id="cfg_window01", name="本地大窗口", protocol="ark", default_model="local-m"
    )
    _set(active_provider_id="cfg_window01")
    ModelProviderRepository.update("cfg_window01", {"context_window": 64_000, "max_output": 8_000})
    rc.invalidate()

    limits = rc.resolve_model_limits()
    assert limits.context_window == 64_000
    assert limits.max_output == 8_000


def test_config_id_never_reaches_provider_defaults(monkeypatch):
    """查限值时第一个实参必须是**协议名**，永远不能是配置条目 id。

    为什么值得单独立一条：`model_registry` 查不到就静默落到 `_FALLBACK`（32k），
    不报错。于是「配了个 128k 的模型却老被拦超顶」这种问题，从错误现场完全看不出
    原因，只能靠这条断言把它钉死在改动的那一刻。
    """
    from backend.core import model_registry

    seen: list[str] = []
    original = model_registry.resolve_limits

    def spy(provider: str, model: str):
        seen.append(provider)
        return original(provider, model)

    ModelProviderRepository.create(
        provider_id="cfg_window02", name="方舟", protocol="ark", default_model="doubao"
    )
    _set(active_provider_id="cfg_window02")
    rc.invalidate()

    monkeypatch.setattr(model_registry, "resolve_limits", spy)
    rc.resolve_model_limits()

    assert seen == ["ark"], f"限值查表拿到了非协议值：{seen}"

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
    rc.invalidate()
    assert rc.get_runtime_config().settings.llm_max_retries == get_settings().llm_max_retries

    _set(llm_max_retries=5)
    rc.invalidate()
    assert rc.get_runtime_config().settings.llm_max_retries == 5


def test_placeholder_key_for_keyless_endpoints():
    """本地 vLLM / Ollama 这类不要密钥的接入点，填占位值即可。

    空 `value_token` 现在表示「未配置」，所以这类接入点必须显式给一个占位值
    （如 `not-needed`）——OpenAI 兼容客户端要求 api_key 非空。
    """
    from backend.core import crypto
    from backend.core.providers import ProviderSpec

    spec = ProviderSpec(
        id="cfg_local",
        name="本地 vLLM",
        protocol="openai_compatible",
        base_url="http://127.0.0.1:8000/v1",
        value_token=crypto.encrypt_value("not-needed"),
        model="qwen2.5",
        max_tokens=4096,
    )
    assert rc.lookup_provider_key(spec) == "not-needed"


def test_missing_key_fails_loudly_with_admin_hint():
    """没有密钥 → 必须报错，且提示指向管理后台，而不是含糊的「未配置」。

    静默用占位值会让它变成一次 401，而 401 的文案不会告诉你去哪儿填。
    """
    from backend.core.providers import ProviderSpec

    spec = ProviderSpec(
        id="cfg_ark",
        name="方舟-生产",
        protocol="ark",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        value_token="",
        model="doubao-seed",
        max_tokens=4096,
    )
    with pytest.raises(RuntimeError) as excinfo:
        rc.lookup_provider_key(spec)
    assert "管理后台" in str(excinfo.value)


def test_get_llm_without_any_config_says_build_one_first():
    """一条配置都没有时，`get_llm()` 的报错必须说清「去哪儿建」。

    ADR-051 之后这里没有 env 兜底了，所以「装完直接用」会撞到这条。
    报错若是「没有可用的模型供应商」这类含糊话，用户第一反应是去翻 `.env`——
    而答案恰恰是「`.env` 里没有了，去后台建一条」。文案本身就是修复指引。
    """
    from backend.services.llm_factory import get_llm

    rc.invalidate()

    with pytest.raises(RuntimeError) as excinfo:
        get_llm()

    message = str(excinfo.value)
    assert "还没有任何模型配置" in message
    assert "管理后台" in message


def test_env_example_has_no_effective_lines():
    """`.env.example` 里**一行生效配置都没有**——它是说明，不是配置。

    三层理由叠在一起：

    1. **可热改项不能出现**（ADR-052）：模板是所有人第一次接触配置的地方，
       列着 `LLM_TIMEOUT` / `PRD_*` / `LLM_BUDGET_*` 就会让人以为「这里才是入口」，
       改完不生效（后台覆盖在它前面）。
    2. **启动项也不写成生效行**：全都有可用默认值，而且默认值通常比手写更对
       （`CORS_ORIGINS` 的默认含后台端口 5174，模板里手写的那版反而漏了）。
    3. **密钥改成注释占位**：`ARK_API_KEY=` 这种空值行本来就等于没写，
       留着只会让人以为「这里是一个需要打勾的配置项」。真正的问题是部署者
       不知道要准备哪几把钥匙，注释照样答得出来。

    断言强到「一个生效行都不许有」：下一次有人「顺手补个默认值」时它就红，
    比在文档里写一句「请勿添加」可靠得多。
    """
    from backend.core.config import BACKEND_ROOT

    text = (BACKEND_ROOT / ".env.example").read_text(encoding="utf-8")
    keys: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        keys.add(line.split("=", 1)[0].strip().lower())

    assert keys == set(), f"模板里不该有任何生效配置行，实际有：{sorted(keys)}"
    # 冗余但留着当文档：可热改项永远不该回到模板里
    assert not (keys & set(rc.WHITELIST))
