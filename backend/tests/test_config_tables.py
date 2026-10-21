"""配置与供应商两张表的迁移与仓储（v4 T3 / 迁移 v16）。

三条最要紧的不变式，每条都对应一种「加完就出事」的失败模式：

1. **表必须同时写进基线与迁移**。`tests/conftest.py` 的 `isolated_db` 只调
   `init_schema()`、不跑迁移，所以「只加迁移」会让所有纯服务层用例拿到
   `no such table: system_config`——而这条错在 CI 上与业务代码毫无关系。
2. **播种只插不覆盖**。迁移可能被重跑（旧库补升），`INSERT OR IGNORE` 保证
   管理员改过的地址/密钥变量名不会被一次迁移重置回默认值。
3. **`system_config` 初态为空**。没有覆盖行 = 全部取 env 默认值，这是
   「升级后行为零变化」的结构保证；迁移若顺手写了几行默认值，就等于悄悄
   把 env 里配的东西架空。
"""

from __future__ import annotations

import pytest

from backend.core.db import connect, init_schema
from backend.core.migrations import MIGRATIONS, run_migrations
from backend.repositories.runtime_config_repo import (
    ModelProviderRepository,
    SystemConfigRepository,
    decode_extra,
)


def _tables() -> set[str]:
    with connect() as conn:
        return {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }


# ---------------------------------------------------------------------------
# 迁移
# ---------------------------------------------------------------------------


def test_migrations_reach_latest_version():
    """断言写成 `len(MIGRATIONS)` 而不是写死数字。

    写死的话，将来每加一步迁移，这条与本次改动无关的用例都会红一次。
    """
    assert run_migrations() == len(MIGRATIONS)


def test_only_init_schema_also_creates_config_tables():
    """**双写守卫**：只跑 `init_schema()`（不跑迁移）也要有两张表。

    这正是 `conftest.isolated_db` 的路径。只写迁移会让所有纯服务层用例
    在 `SELECT * FROM system_config` 上炸掉。
    """
    init_schema()
    tables = _tables()
    assert "model_providers" in tables
    assert "system_config" in tables


def test_migration_v16_creates_tables_from_previous_version(isolated_db):  # noqa: ARG001
    """从 v15 升上来时才会真正执行 v16，且执行后两张表出现。

    先删表 + 截断清单到 15，断言表还不存在；再跑全量清单，断言表出现。
    这条比「跑完全量后表在」更有价值：它证明 v16 是**新加的那一步**在做事，
    而不是别的地方顺手建的表。

    ⚠️ 必须先 `DROP`：`isolated_db` 夹具会调 `init_schema()`，而基线里
    **也**写了这两张表（双写纪律），不删就模拟不出「v15 的旧库」。
    """
    from backend.core import migrations as module

    with connect() as conn:
        conn.execute("DROP TABLE IF EXISTS model_providers")
        conn.execute("DROP TABLE IF EXISTS system_config")
        conn.commit()
    assert "model_providers" not in _tables()

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(module, "MIGRATIONS", MIGRATIONS[:15])
        assert run_migrations() == 15
        assert "model_providers" not in _tables()

        monkeypatch.setattr(module, "MIGRATIONS", MIGRATIONS)
        assert run_migrations() == len(MIGRATIONS)
        assert "model_providers" in _tables()
        assert "system_config" in _tables()
        # v16 不只是建表，还要把六家内置播种进去
        assert ModelProviderRepository.count() == 6
    finally:
        monkeypatch.undo()


def test_builtin_providers_are_seeded():
    """播种六家内置供应商，覆盖全部六个协议。"""
    run_migrations()
    rows = ModelProviderRepository.list_all()
    by_id = {row["id"]: row for row in rows}

    assert set(by_id) == {"ark", "anthropic", "openai", "gemini", "nvidia", "huggingface"}
    # 协议枚举必须与 runtime_config 的取值域一致
    assert {row["protocol"] for row in rows} == {
        "ark",
        "anthropic",
        "openai_compatible",
        "google_genai",
        "nvidia",
        "huggingface",
    }
    # 六家全是内置（不可删），且只存密钥变量名、绝不存值
    assert all(row["builtin"] == 1 for row in rows)
    assert all(row["api_key_env_var"] for row in rows)
    assert not any("sk-" in str(row.values()) for row in rows)


def test_seed_does_not_overwrite_admin_edits():
    """迁移重跑不覆盖管理员改过的值。

    `INSERT OR IGNORE` 的意义就在这里：迁移是「补结构」，不是「重置配置」。
    若写成 `INSERT OR REPLACE`，一次补升就会把后台配好的地址悄悄打回默认。
    """
    run_migrations()
    ModelProviderRepository.update("openai", {"base_url": "https://my-proxy.example/v1"})

    # 模拟「迁移被重跑」：把版本退回 15 再升
    with connect() as conn:
        conn.execute("PRAGMA user_version = 15")
        conn.commit()
    run_migrations()

    assert ModelProviderRepository.get("openai")["base_url"] == "https://my-proxy.example/v1"


def test_system_config_starts_empty():
    """覆盖项初态为空：默认行为完全由 env 决定，升级不改任何行为。"""
    run_migrations()
    assert SystemConfigRepository.get_all() == {}


# ---------------------------------------------------------------------------
# 仓储
# ---------------------------------------------------------------------------


def test_config_upsert_overwrites_and_delete_restores_default():
    """写入覆盖同一键；删除即「恢复默认」。"""
    run_migrations()
    SystemConfigRepository.upsert_many({"prd_pipeline_concurrency": "5"}, updated_by=1)
    SystemConfigRepository.upsert_many({"prd_pipeline_concurrency": "3"}, updated_by=2)
    assert SystemConfigRepository.get_all() == {"prd_pipeline_concurrency": "3"}

    assert SystemConfigRepository.delete_many(["prd_pipeline_concurrency"]) == 1
    assert SystemConfigRepository.get_all() == {}
    # 删不存在的键不该报错，也不该返回负数
    assert SystemConfigRepository.delete_many(["nope"]) == 0


def test_provider_update_ignores_identity_fields():
    """`id` 与 `builtin` 不允许通过 update 改。

    规则写在仓储一处，接口层不必各自记得过滤；否则将来多一个调用点
    就可能把 id 改掉，而那等于让激活指针凭空失效。
    """
    run_migrations()
    assert ModelProviderRepository.update(
        "openai", {"base_url": "https://x/v1", "id": "hacked", "builtin": 0}
    )
    row = ModelProviderRepository.get("openai")
    assert row["id"] == "openai"
    assert row["builtin"] == 1
    assert row["base_url"] == "https://x/v1"


def test_provider_delete_and_count():
    run_migrations()
    before = ModelProviderRepository.count()
    ModelProviderRepository.create(
        provider_id="deepseek",
        name="DeepSeek",
        protocol="openai_compatible",
        base_url="https://api.deepseek.com/v1",
        api_key_env_var="DEEPSEEK_API_KEY",
        default_model="deepseek-chat",
    )
    assert ModelProviderRepository.count() == before + 1
    assert ModelProviderRepository.get("deepseek")["builtin"] == 0

    assert ModelProviderRepository.delete("deepseek")
    assert ModelProviderRepository.get("deepseek") is None
    assert not ModelProviderRepository.delete("deepseek")


def test_decode_extra_tolerates_bad_values():
    """坏 `extra_json` 给 `{}`：一个坏字段不该让整份配置失效。"""
    assert decode_extra('{"stream_usage": true}') == {"stream_usage": True}
    assert decode_extra("{bad") == {}
    assert decode_extra("") == {}
    assert decode_extra("[1,2]") == {}
