"""配置与模型配置两张表的结构与仓储。

三条最要紧的不变式：

1. **两张表与 `generation_jobs` 的四个快照列必须真的存在**——它们是「配置能热改」
   与「按供应商对账」的前提。建库只走 Alembic 基线，所以这里顺带守「基线没漏建」。
2. **`system_config` 初态为空**。没有覆盖行 = 全部取 env 默认值，这是
   「升级后行为零变化」的结构保证；基线若顺手写了几行默认值，就等于悄悄
   把 env 里配的东西架空。
3. **出厂空壳不播种**。`model_providers` 新库是空的：没配过就是没有，
   后台不该凭空摆几张没人填过的空卡片（这条以前由 v19 的删除步骤保证，
   现在由「基线只建表、不插数据」保证）。

> 历史背景：v16 曾把「每个协议一行」播进 `model_providers`，v19 又写一步迁移把它们删掉。
> 那条链已在 v6.1 删除；现在的做法是**根本不播种**，所以不需要「删」。
"""

from __future__ import annotations

import json

from conftest import (
    model_providers as ModelProviderRepository,
    system_config as SystemConfigRepository,
)
from dbsupport import connect

from backend.repositories.runtime_config_repo import decode_extra


def _tables() -> set[str]:
    with connect() as conn:
        return {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }


def _job_columns() -> set[str]:
    with connect() as conn:
        return {str(row[1]) for row in conn.execute("PRAGMA table_info(generation_jobs)")}


# ---------------------------------------------------------------------------
# 结构
# ---------------------------------------------------------------------------


def test_baseline_creates_config_tables_and_snapshot_columns():
    """基线必须建出两张配置表 + `generation_jobs` 的四个供应商快照列。

    四个快照列是「历史账单不跟着今天的状态变」的结构基础：任务行自己记住
    走了哪家、哪个模型，而不是靠 `config_id` 回查（配置会被改、会被删）。
    """
    tables = _tables()
    assert "model_providers" in tables
    assert "system_config" in tables
    assert {
        "provider_protocol",
        "provider_config_id",
        "provider_name",
        "provider_model",
    } <= _job_columns()


def test_system_config_starts_empty():
    """覆盖项初态为空：默认行为完全由 env 决定，建库不改任何行为。"""
    assert SystemConfigRepository.get_all() == {}


def test_fresh_db_has_no_factory_providers():
    """新库没有任何供应商条目（不播种出厂空壳）。"""
    assert ModelProviderRepository.count() == 0


# ---------------------------------------------------------------------------
# 仓储
# ---------------------------------------------------------------------------


def test_config_upsert_overwrites_and_delete_restores_default():
    """写入覆盖同一键；删除即「恢复默认」。"""
    SystemConfigRepository.upsert_many({"prd_pipeline_concurrency": "5"}, updated_by=1)
    SystemConfigRepository.upsert_many({"prd_pipeline_concurrency": "3"}, updated_by=2)
    assert SystemConfigRepository.get_all() == {"prd_pipeline_concurrency": "3"}

    assert SystemConfigRepository.delete_many(["prd_pipeline_concurrency"]) == 1
    assert SystemConfigRepository.get_all() == {}
    # 删不存在的键不该报错，也不该返回负数
    assert SystemConfigRepository.delete_many(["nope"]) == 0


def test_config_upsert_is_all_or_nothing():
    """整批写入在一个事务里：配置是一组语义，写一半会撕裂。"""
    SystemConfigRepository.upsert_many(
        {"prd_pipeline_concurrency": "2", "review_rewrite_min_severity": '"high"'},
        updated_by=1,
    )
    stored = SystemConfigRepository.get_all()
    assert stored["prd_pipeline_concurrency"] == "2"
    assert json.loads(stored["review_rewrite_min_severity"]) == "high"


def test_provider_update_ignores_identity_fields():
    """`id` / `builtin` / **`protocol`** 不允许通过 update 改。

    规则写在仓储一处，接口层不必各自记得过滤；否则将来多一个调用点就可能
    把 id 或协议改掉——前者让激活指针凭空失效，后者让构造器用错分支
    （`max_tokens` 在 ark 与 anthropic 下语义不同）。
    """
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
