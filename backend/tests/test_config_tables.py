"""配置与模型配置两张表的迁移与仓储（v16 + v19）。

三条最要紧的不变式，每条都对应一种「加完就出事」的失败模式：

1. **表/列必须同时写进基线与迁移**。`tests/conftest.py` 的 `isolated_db` 只调
   `init_schema()`、不跑迁移，所以「只加迁移」会让所有纯服务层用例拿到
   `no such table: system_config` / `no such column: provider_protocol`——
   而这条错在 CI 上与业务代码毫无关系。
2. **出厂空壳只删「没人碰过」的**。v19 是本仓库第一个删除业务数据的迁移，
   判据错一条就会丢管理员的配置（见 `_provider_config_entries` 的 docstring）。
3. **`system_config` 初态为空**。没有覆盖行 = 全部取 env 默认值，这是
   「升级后行为零变化」的结构保证；迁移若顺手写了几行默认值，就等于悄悄
   把 env 里配的东西架空。
"""

from __future__ import annotations

import json

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


def _job_columns() -> set[str]:
    with connect() as conn:
        return {str(row[1]) for row in conn.execute("PRAGMA table_info(generation_jobs)")}


def _run_until(version: int) -> None:
    """把库升级到某一个指定版本（模拟「还没升到最新」的旧库）。"""
    from backend.core import migrations as module

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(module, "MIGRATIONS", MIGRATIONS[:version])
        assert run_migrations() == version
    finally:
        monkeypatch.undo()


def _run_rest() -> None:
    """在旧库上跑完整清单。"""
    assert run_migrations() == len(MIGRATIONS)


# ---------------------------------------------------------------------------
# 迁移
# ---------------------------------------------------------------------------


def test_migrations_reach_latest_version():
    """跑完迁移必须到清单末尾。

    断言写成 `len(MIGRATIONS)` 而不是写死数字：写死的话，将来每加一步迁移，
    这条与本次改动无关的用例都会红一次。
    """
    assert run_migrations() == len(MIGRATIONS)


def test_only_init_schema_also_creates_config_tables():
    """**双写守卫**：只跑 `init_schema()`（不跑迁移）也要有两张表 + 四个快照列。

    这正是 `conftest.isolated_db` 的路径。只写迁移会让所有纯服务层用例
    在 `SELECT * FROM system_config` 上炸掉；v19 的四个列同理。
    """
    init_schema()
    tables = _tables()
    assert "model_providers" in tables
    assert "system_config" in tables
    assert {
        "provider_protocol",
        "provider_config_id",
        "provider_name",
        "provider_model",
    } <= _job_columns()


def test_migration_v16_creates_tables_from_previous_version(isolated_db):  # noqa: ARG001
    """从 v15 升上来时才会真正执行 v16，且执行后两张表出现、六条种子入库。

    先删表 + 截断清单到 15，断言表还不存在；再升到 16，断言表出现且种了六条。
    这条比「跑完全量后表在」更有价值：它证明 v16 是**新加的那一步**在做事，
    而不是别的地方顺手建的表。

    ⚠️ 必须先 `DROP`：`isolated_db` 夹具会调 `init_schema()`，而基线里
    **也**写了这两张表（双写纪律），不删就模拟不出「v15 的旧库」。
    """
    with connect() as conn:
        conn.execute("DROP TABLE IF EXISTS model_providers")
        conn.execute("DROP TABLE IF EXISTS system_config")
        conn.commit()
    assert "model_providers" not in _tables()

    _run_until(15)
    assert "model_providers" not in _tables()

    _run_until(16)
    assert "model_providers" in _tables()
    assert "system_config" in _tables()
    # v16 不只是建表，还要把六家内置播种进去（v19 之后才会清掉，见下一条）
    assert ModelProviderRepository.count() == 6


def test_v19_deletes_only_factory_seeds():
    """v19 只删「v16 播种 + 从没被改过 + 不是使用中」的行。

    夹具里的三行分别代表三种情况：
    - `openai`：管理员改过 `base_url` → 必须保留（否则等于静默打回默认）；
    - `ark`：当前使用中 → 必须保留（否则后台列表变空、只能靠 `env:ark` 兜底，
      在管理员眼里就是「迁移把我的配置弄丢了」）；
    - 其余四条：出厂空壳 → 删掉，否则后台永远摆着四个没人配过的空位。
    """
    _run_until(18)
    assert ModelProviderRepository.count() == 6

    ModelProviderRepository.update("openai", {"base_url": "https://my-proxy.example/v1"})
    SystemConfigRepository.upsert_many({"active_provider_id": json.dumps("ark")}, updated_by=1)

    _run_rest()

    ids = {row["id"] for row in ModelProviderRepository.list_all()}
    assert ids == {"ark", "openai"}
    # 改过的那条地址不能被重置
    assert ModelProviderRepository.get("openai")["base_url"] == "https://my-proxy.example/v1"
    # 激活指针原样保留：v19 不该动一个仍然指得着的指针
    assert json.loads(SystemConfigRepository.get_all()["active_provider_id"]) == "ark"


def test_v19_clears_factory_seeds_on_fresh_db():
    """全新库：没有任何激活指针，六条空壳全删，列表为空。

    这是「配置条目是可选的东西」的结构表达：没配过就是没有，
    运行时走 `env:` 合成（行为与升级前一致），而不是凭空摆六个空卡片。
    """
    run_migrations()
    assert ModelProviderRepository.count() == 0
    assert "provider_protocol" in _job_columns()


def test_v19_clears_dangling_active_pointer():
    """指针指向不存在的行（手工改库的产物）→ 迁移清掉该键。

    不清的后果不是报错，而是每次读配置都刷一条 WARN，且后台「当前供应商」
    与列表永远对不上——那种问题最难被当成 bug 报上来。
    """
    _run_until(18)
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO system_config (key, value, updated_at, updated_by) "
            "VALUES ('active_provider_id', '\"ghost\"', '2026-01-01T00:00:00', 1)"
        )
        conn.commit()

    _run_rest()

    assert "active_provider_id" not in SystemConfigRepository.get_all()


def test_v19_backfills_job_snapshot_columns_and_survives_bad_usage():
    """历史 `usage` 回填成四列；坏 JSON 不能让整条迁移抛错。

    `json_extract` 遇到非法 JSON 会直接抛，而迁移失败会拦住服务启动——
    「一条脏日志把整个库升不上去」是最不该出现的失败模式，所以守卫写在 WHERE 里。
    """
    _run_until(18)
    good = json.dumps({"provider": "ark", "model": "doubao-seed-2-1-turbo"}, ensure_ascii=False)
    with connect() as conn:
        # generation_jobs.owner_id 有外键，先造一个属主；本用例只关心列里回填了什么
        conn.execute(
            "INSERT INTO users (username, password_hash, salt, created_at) "
            "VALUES ('owner', 'x', x'00', '2026-01-01T00:00:00')"
        )
        owner_id = int(
            conn.execute("SELECT id FROM users WHERE username = 'owner'").fetchone()[0]
        )
        for payload in [good, "not json", "[]"]:
            conn.execute(
                "INSERT INTO generation_jobs "
                "(owner_id, kind, status, usage, created_at, updated_at) "
                "VALUES (?, 'prd', 'completed', ?, '2026-01-01T00:00:00', '2026-01-01T00:00:00')",
                (owner_id, payload),
            )
        conn.commit()

    _run_rest()

    with connect() as conn:
        rows = [
            dict(found)
            for found in conn.execute(
                "SELECT usage, provider_protocol, provider_model FROM generation_jobs"
            )
        ]
    by_usage = {row["usage"]: row for row in rows}
    assert by_usage[good]["provider_protocol"] == "ark"
    assert by_usage[good]["provider_model"] == "doubao-seed-2-1-turbo"
    assert by_usage["not json"]["provider_protocol"] == ""
    assert by_usage["[]"]["provider_protocol"] == ""


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
    """`id` / `builtin` / **`protocol`** 不允许通过 update 改。

    规则写在仓储一处，接口层不必各自记得过滤；否则将来多一个调用点就可能
    把 id 或协议改掉——前者让激活指针凭空失效，后者让构造器用错分支
    （`max_tokens` 在 ark 与 anthropic 下语义不同）。
    """
    run_migrations()
    ModelProviderRepository.create(
        provider_id="cfg_test0001",
        name="方舟-测试",
        protocol="ark",
        base_url="https://ark.example/api/v3",
    )
    assert ModelProviderRepository.update(
        "cfg_test0001",
        {"base_url": "https://x/api/v3", "id": "hacked", "builtin": 1, "protocol": "anthropic"},
    )
    row = ModelProviderRepository.get("cfg_test0001")
    assert row["id"] == "cfg_test0001"
    assert row["protocol"] == "ark"
    assert row["base_url"] == "https://x/api/v3"


def test_provider_delete_and_count():
    run_migrations()
    before = ModelProviderRepository.count()
    ModelProviderRepository.create(
        provider_id="cfg_test0002",
        name="DeepSeek",
        protocol="openai_compatible",
        base_url="https://api.deepseek.com/v1",
        value_token="",
        default_model="deepseek-chat",
    )
    assert ModelProviderRepository.count() == before + 1
    # builtin 已从投影里移除：这条捷径没了，代码就不再有「内置/自定义」之分
    assert "builtin" not in ModelProviderRepository.get("cfg_test0002")

    assert ModelProviderRepository.delete("cfg_test0002")
    assert ModelProviderRepository.get("cfg_test0002") is None
    assert not ModelProviderRepository.delete("cfg_test0002")


def test_provider_sort_order_appends_to_the_end():
    """不传 `sort_order` 时排到末尾，而不是全填 100 靠 id 排序。

    id 现在是随机后缀，按它排等于随机顺序；管理员会看到列表顺序莫名跳动。
    """
    run_migrations()
    ModelProviderRepository.create(provider_id="cfg_a", name="A", protocol="ark")
    ModelProviderRepository.create(provider_id="cfg_b", name="B", protocol="ark")
    orders = [row["sort_order"] for row in ModelProviderRepository.list_all()]
    assert orders == sorted(orders)
    assert orders[0] < orders[1]


def test_decode_extra_tolerates_bad_values():
    """坏 `extra_json` 给 `{}`：一个坏字段不该让整份配置失效。"""
    assert decode_extra('{"stream_usage": true}') == {"stream_usage": True}
    assert decode_extra("{bad") == {}
    assert decode_extra("") == {}
    assert decode_extra("[1,2]") == {}
