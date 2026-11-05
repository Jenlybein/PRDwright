"""生成任务的「不可变数据完整投影」（票 #34）。

任务单里的不可变数据——原始请求参数、配置/供应商快照、技能包身份——必须在
**建任务那一刻一次写全**；收尾不再从另一条路径派生供应商四列；运行态仍以内存
运行时为权威、按既有节奏落进度。本文件只跨公开接缝断言：

| 接缝 | 断言的可观察行为 |
| --- | --- |
| `GenerationJobRepository.create` | 建任务后立刻读得到三块不可变数据 |
| `job_service.create_job` | 生产入口把同一份请求/配置/技能包定格进 DB 行 |
| `job_finalize._finalize` | 收尾后供应商四列与建任务时定格的值一致 |
| `_db_snapshot_event` | 运行时未命中时 DB 兜底快照读得到新字段，坏数据不炸 |
| `head_revision` | 迁移链 head 正确、旧库升级后 `alembic_version` 为真源 |
| `ensure_text_within_limit` | 新列的大段文本在 `create` 写入前受 60KB 闸门约束 |

不碰流式事件种类与编码、`[DONE]`、单 worker + 进程内运行时、重连先快照后 live、
失败不覆写、被取代与已取消分开、排空顺序与 30 秒、就绪探针、启动回收孤儿任务、
错误码、前端交互。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import register, users
from dbsupport import connect

from backend.core.engine import session_scope
from backend.core.errors import AppError, ErrorCode
from backend.repositories.generation_job_repo import GenerationJobRepository
from backend.services import job_service


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------


def _alice_id() -> int:
    """取已注册用户 alice 的 id（generation_jobs.owner_id 有外键）。"""
    user = users.get_by_username("alice")
    assert user is not None
    return int(user["id"])


@pytest.fixture
def alice(client):
    """注册 alice 并返回其 id（顺带保证 DB 已初始化）。"""
    register(client)
    return _alice_id()


def _raw(job_id: int) -> dict[str, Any]:
    """按 id 直读整行（绕开 ORM，验证真的落盘了）。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM generation_jobs WHERE id = ?", (job_id,)
        ).fetchone()
    assert row is not None
    return dict(row)


def _seed_provider(*, provider_id: str = "cfg_proj01", name: str = "定格用配置") -> str:
    """落一条供应商配置并设为使用中，返回 id。"""
    from backend.core import runtime_config as rc
    from conftest import model_providers, system_config

    model_providers.create(
        provider_id=provider_id,
        name=name,
        protocol="ark",
        value_token="",
        default_model="doubao-frozen-01",
    )
    system_config.upsert_many(
        {rc.ACTIVE_PROVIDER_KEY: json.dumps(provider_id)}, updated_by=None
    )
    rc.invalidate()
    return provider_id


# ---------------------------------------------------------------------------
# 一、建任务那一刻一次写全
# ---------------------------------------------------------------------------


def test_create_writes_all_immutable_data_at_creation(alice):
    """建任务后立刻能读到原始请求参数、配置/供应商快照与技能包身份。

    三块数据都必须来自 `create` 的入参（而不是收尾时补写、不是另读一次配置）：
    生成跑到一半改后台配置，DB 行仍应回答「这一单当时用的是哪套模型与技能包」。
    """
    request = {"summary": "做个笔记应用", "requirements": "支持离线", "doc_kind": "prd"}
    # 定格形状：`active_provider` 的四格与库里四列同名，避免两处再各映射一遍。
    provider = {
        "provider_protocol": "ark",
        "provider_config_id": "cfg_proj01",
        "provider_name": "定格用配置",
        "provider_model": "doubao-frozen-01",
    }
    config = {"active_provider": provider, "prd_pipeline_concurrency": 1}
    skills = [{"name": "prd-generator", "version": "0.1.0", "fingerprint": "abc123"}]

    with session_scope() as session:
        job = GenerationJobRepository(session).create(
            alice,
            "prd",
            None,
            doc_kind="prd",
            request_params=request,
            config_snapshot=config,
            skill_refs=skills,
        )
        job_id = int(job.id or 0)  # flush() 之后 id 当场可用

    assert job_id > 0
    row = _raw(job_id)
    assert json.loads(row["request_params"]) == request
    assert json.loads(row["config_snapshot"]) == config
    assert json.loads(row["skill_refs"]) == skills
    # 供应商四列在建任务时就从同一份快照定格，不等收尾再派生。
    assert row["provider_protocol"] == "ark"
    assert row["provider_config_id"] == "cfg_proj01"
    assert row["provider_name"] == "定格用配置"
    assert row["provider_model"] == "doubao-frozen-01"
    # 技能包身份另有可索引的短串：运行中管理端不必解析大段 JSON 也能按它定位。
    assert row["skill_manifest"] == "prd-generator|0.1.0|abc123"


def test_running_query_reads_model_and_skill_identity(alice):
    """运行中查询（`get_job`）读得到本次用的模型与技能包身份。

    跨的是服务层公开查询接缝：DB 行是不可变数据的完整投影，不需要等到收尾
    或重新打开流才能回答「这次用的是哪套模型与技能包」。
    """
    request = {"summary": "查询时还要看得见"}
    provider = {
        "provider_protocol": "ark",
        "provider_config_id": "cfg_query01",
        "provider_name": "查询配置",
        "provider_model": "model-query",
    }
    skills = [{"name": "s-query", "version": "2.0", "fingerprint": "ff00"}]
    with session_scope() as session:
        job = GenerationJobRepository(session).create(
            alice,
            "prd",
            None,
            request_params=request,
            config_snapshot={"active_provider": provider},
            skill_refs=skills,
        )
        job_id = int(job.id or 0)

    row = job_service.get_job(alice, job_id)
    assert row is not None
    assert json.loads(row["config_snapshot"])["active_provider"]["provider_model"] == "model-query"
    assert json.loads(row["skill_refs"]) == skills
    assert row["skill_manifest"] == "s-query|2.0|ff00"
    # 运行态没被查询动作改写：仍是 running，正文仍为空。
    assert row["status"] == "running"
    assert row["draft_content"] == ""


def test_db_projection_and_run_summary_share_one_frozen_provider(alice, monkeypatch):
    """跨主接缝：造单定格的供应商四格，与收尾回执 `run_summary` 是同一份值。

    走「造单 → 登记 → 派发 → 订阅」真实链路（离线假 LLM），只断言两个公开
    出口一致：DB 行的不可变投影，以及订阅者收到的回执；收尾也不覆写四列。
    """
    from backend.services import document_service, prd_section_writer
    from backend.services.job_bus import job_bus
    from fakes import ScriptedLLM
    from ticket_support import run_ticket

    _seed_provider()
    # 全链路离线：模块级文档服务换成注入假 LLM 的实例，真实工厂一调用即抛。
    monkeypatch.setattr(
        document_service,
        "get_service",
        lambda: document_service.DocumentService(llm=ScriptedLLM(["章节正文"])),
    )

    def _no_real_llm(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("测试不应触网调用真实 LLM")

    monkeypatch.setattr(document_service, "get_llm", _no_real_llm)
    monkeypatch.setattr(prd_section_writer, "get_llm", _no_real_llm)
    job_service._RUNTIMES.clear()  # noqa: SLF001 - 清掉别的用例留下的运行时
    job_bus._subscribers.clear()  # noqa: SLF001

    registered, events = run_ticket(
        owner_id=alice,
        solution_id=None,
        kind="prd",
        request={"summary": "定格"},
    )
    frozen = json.loads(_raw(registered.job_id)["config_snapshot"])["active_provider"]
    summary = events[-1].data
    assert events[-1].type == "run_summary"
    assert summary["protocol"] == frozen["provider_protocol"] != ""
    assert summary["provider_config_id"] == frozen["provider_config_id"] != ""
    assert summary["provider_name"] == frozen["provider_name"]
    assert summary["model"] == frozen["provider_model"] != ""
    # 收尾没覆写四列：DB 行仍是建任务时定格的那一份。
    row = _raw(registered.job_id)
    assert row["provider_config_id"] == frozen["provider_config_id"]
    assert row["provider_model"] == frozen["provider_model"]


def test_registered_ticket_freezes_the_same_snapshot(
    alice, monkeypatch, temp_skills_root
):
    """造单 → 登记把同一份请求/配置/技能包身份定格进 DB 行。

    配置来自造单那一刻的 `get_runtime_config()`，技能包身份由注册表在造单时
    算好作为数据随单走；两者都不在收尾时重读。假环境用 `temp_skills_root`
    里的技能包（不替换 `describe_kind` 内部零件）。
    """
    import backend.core.runtime_config as rc
    from conftest import write_skill_package
    from ticket_support import build_and_register

    _seed_provider()
    write_skill_package(temp_skills_root, "prd-generator", version="9.9.9")

    payload = {"summary": "原始需求", "requirements": "别被改写", "doc_kind": "prd"}
    registered = build_and_register(
        owner_id=alice,
        solution_id=None,
        kind="prd",
        request=payload,
    )
    row = _raw(registered.job_id)

    assert json.loads(row["request_params"])["summary"] == "原始需求"
    snapshot = json.loads(row["config_snapshot"])
    assert snapshot["active_provider"]["provider_config_id"] == "cfg_proj01"
    assert snapshot["active_provider"]["provider_model"] == "doubao-frozen-01"
    skills = json.loads(row["skill_refs"])
    assert skills[0]["name"] == "prd-generator"
    assert skills[0]["version"] == "9.9.9"
    assert len(skills[0]["fingerprint"]) == 12
    assert row["provider_config_id"] == "cfg_proj01"
    assert row["provider_model"] == "doubao-frozen-01"
    # 不再原地改写调用方传入的字典：四个 chat 派生字段明写在单②上。
    assert payload == {"summary": "原始需求", "requirements": "别被改写", "doc_kind": "prd"}
    # 配置对象仍是「那一刻」的快照，不是每次现算的新对象。
    assert rc.get_runtime_config().active_provider is not None


# ---------------------------------------------------------------------------
# 二、收尾不再从另一条路径派生供应商四列
# ---------------------------------------------------------------------------


def test_finalize_keeps_the_provider_columns_frozen_at_creation(alice, monkeypatch):
    """收尾不另算供应商四列：跑完一整单，DB 四列仍是登记那一刻定格的值。

    `usage` JSON、token 两列、错误码、终态语义都不改。归位后跨「造单 → 登记 →
    派发 → 订阅」：造单时传入一份假的「原始配置」快照，派发后即使后台配置被切成
    另一套，收尾落库的四列与订阅者收到的回执都只能来自同一份定格值。
    """
    import backend.core.runtime_config as rc
    from backend.core.providers import ProviderSpec
    from backend.services import document_service, prd_section_writer
    from backend.services.job_bus import job_bus
    from fakes import ScriptedLLM
    from ticket_support import run_ticket_in_flight

    original = ProviderSpec(
        id="cfg_original",
        name="原始配置",
        protocol="ark",
        base_url="",
        value_token="",
        model="model-original",
        max_tokens=0,
    )
    fake_config = rc.RuntimeConfig(
        settings=rc.get_settings(),
        active_provider=original,
        source="fake",
        revision="fake-revision",
    )
    # 全链路离线：模块级文档服务换成注入假 LLM 的实例。
    monkeypatch.setattr(
        document_service,
        "get_service",
        lambda: document_service.DocumentService(llm=ScriptedLLM(["章节正文"])),
    )

    def _no_real_llm(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("测试不应触网调用真实 LLM")

    monkeypatch.setattr(document_service, "get_llm", _no_real_llm)
    monkeypatch.setattr(prd_section_writer, "get_llm", _no_real_llm)
    job_bus._subscribers.clear()  # noqa: SLF001

    def switch_provider_while_running() -> None:
        # 生成中途改后台配置：收尾若重读配置就会把四列写成新值。
        _seed_provider(provider_id="cfg_changed", name="改名后的配置")
        changed = rc.get_runtime_config().active_provider
        assert changed is not None and changed.model != "model-original"

    registered, events = run_ticket_in_flight(
        alice,
        None,
        "prd",
        {"summary": "定格"},
        switch_provider_while_running,
        config=fake_config,
    )

    assert events[-1].type == "run_summary"
    assert events[-1].data["status"] == "completed"
    row = _raw(registered.job_id)
    assert row["status"] == "completed"
    # 收尾没有覆写四列：仍是建任务时定格的那一份。
    assert row["provider_protocol"] == "ark"
    assert row["provider_config_id"] == "cfg_original"
    assert row["provider_name"] == "原始配置"
    assert row["provider_model"] == "model-original"
    # usage JSON 与回执同源，且都是定格值（不因为改了后台配置而漂移）。
    assert json.loads(row["usage"])["model"] == "model-original"
    assert events[-1].data["model"] == "model-original"
    assert row["error_code"] == ""


# ---------------------------------------------------------------------------
# 三、DB 兜底快照读得到新字段，坏数据容错不变
# ---------------------------------------------------------------------------


def test_db_snapshot_event_reads_back_immutable_fields(alice):
    """运行时未命中时，DB 兜底快照能读回请求参数、配置快照与技能包身份。

    归位后走「登记（不派发）→ `open_stream`」：登记表里没有这支运行时，
    订阅读的是 DB 兜底路径，正是进程重启后重连的真实场景。
    """
    from ticket_support import build_and_register, first_snapshot_event

    registered = build_and_register(
        owner_id=alice,
        solution_id=None,
        kind="prd",
        request={"summary": "重连时还要看得见"},
    )
    # 三块不可变数据由登记落库；这里把它们替换成一条已知的自定义投影，
    # 证明 DB 兜底快照读的是**库里那一份**，不是运行时内存。
    with connect() as conn:
        conn.execute(
            "UPDATE generation_jobs SET request_params = ?, config_snapshot = ?, "
            "skill_refs = ? WHERE id = ?",
            (
                json.dumps({"summary": "重连时还要看得见"}),
                json.dumps({"active_provider": {"model": "m1"}}),
                json.dumps([{"name": "s1"}]),
                registered.job_id,
            ),
        )
        conn.commit()

    data = first_snapshot_event(alice, registered.job_id).data
    assert data["request_params"] == {"summary": "重连时还要看得见"}
    assert data["config_snapshot"] == {"active_provider": {"model": "m1"}}
    assert data["skill_refs"] == [{"name": "s1"}]
    assert data["status"] == "running"


def test_db_snapshot_tolerates_corrupt_immutable_json(alice):
    """新字段里是坏 JSON / 旧行缺字段时，快照逐字段退化为空值，不整条炸掉。"""
    from ticket_support import build_and_register, first_snapshot_event

    broken = build_and_register(
        owner_id=alice, solution_id=None, kind="prd", request={}
    )
    with connect() as conn:
        conn.execute(
            "UPDATE generation_jobs SET status = 'failed', "
            "request_params = '{坏掉的 JSON', config_snapshot = '[1,2,3]', "
            "skill_refs = '{坏掉的 JSON', usage = '{坏掉的 JSON', "
            "completed_sections = '{坏掉的 JSON', pending_items = '{坏掉的 JSON' "
            "WHERE id = ?",
            (broken.job_id,),
        )
        conn.commit()

    event = first_snapshot_event(alice, broken.job_id)
    assert event.data["request_params"] == {}
    assert event.data["config_snapshot"] == {}
    assert event.data["skill_refs"] == []
    assert event.data["status"] == "failed"

    # 旧行三块新列是空串时同样退化为空容器（登记落库后回填空串复现旧行）。
    legacy = build_and_register(
        owner_id=alice, solution_id=None, kind="prd", request={}
    )
    with connect() as conn:
        conn.execute(
            "UPDATE generation_jobs SET status = 'completed', "
            "request_params = '', config_snapshot = '', skill_refs = '' WHERE id = ?",
            (legacy.job_id,),
        )
        conn.commit()

    old = first_snapshot_event(alice, legacy.job_id)
    assert old.data["request_params"] == {}
    assert old.data["config_snapshot"] == {}
    assert old.data["skill_refs"] == []


# ---------------------------------------------------------------------------
# 四、新列的大段文本受既有 60KB 口径约束
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field", ["request_params", "config_snapshot", "skill_refs"]
)
def test_create_rejects_oversized_immutable_text(alice, field):
    """`create` 写入前按既有 60KB 口径拦住三块 JSON；事务整体回滚不留半截行。

    不塞进 `_ensure_progress_texts`（那是渐进正文专用）：闸门只管建任务时的
    不可变数据，且错误码复用 `INVALID_REQUEST`，不新增错误码。
    """
    oversized = "中" * 20_481  # 61,443 字节，超 3 字节
    kwargs: dict[str, Any] = {
        "request_params": {"summary": "ok"},
        "config_snapshot": {"active_provider": {}},
        "skill_refs": [],
    }
    if field == "skill_refs":
        kwargs[field] = [{"fingerprint": oversized}]
    else:
        kwargs[field] = {"big": oversized}

    with pytest.raises(AppError) as caught:
        with session_scope() as session:
            GenerationJobRepository(session).create(
                alice, "prd", None, doc_kind="prd", **kwargs
            )
    assert caught.value.code is ErrorCode.INVALID_REQUEST
    assert "60KB" in caught.value.message

    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM generation_jobs").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# 五、迁移链 head 正确、旧库升级后的真源是 alembic_version
# ---------------------------------------------------------------------------


def test_migration_head_and_old_db_upgrade(isolated_db, tmp_path):  # noqa: ARG001
    """手写迁移是唯一 head；旧库升上来后 `alembic_version` 是真源、结构对拍通过。

    不写死「第几步」：上一版从迁移链现取，断言的是「库的版本 == 代码里的 head」。
    """
    from alembic import command
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine

    from backend.core.schema import (
        _BACKEND_ROOT,
        _alembic_config,
        _models_snapshot,
        current_revision,
        head_revision,
        prepare_schema,
    )
    from backend.core.schema_snapshot import diff_snapshots, schema_snapshot

    head = head_revision()
    assert head is not None
    # 从链上现取「head 的上一版」，不写死编号。
    scripts = ScriptDirectory.from_config(_script_config(_BACKEND_ROOT))
    previous = scripts.get_revision(head).down_revision
    assert previous is not None

    path = tmp_path / "old.db"
    engine = create_engine(f"sqlite+pysqlite:///{path}")
    conn = None
    try:
        cfg = _alembic_config(f"sqlite+pysqlite:///{path}")
        command.upgrade(cfg, previous)
        conn = engine.connect()
        before_cols = {
            row[1] for row in conn.exec_driver_sql(
                "PRAGMA table_info(generation_jobs)"
            ).fetchall()
        }
        assert "request_params" not in before_cols
        conn.close()
        conn = None

        revision = prepare_schema(engine)
        assert revision == head
        assert current_revision(engine) == head
        conn = engine.connect()
        after_cols = {
            row[1] for row in conn.exec_driver_sql(
                "PRAGMA table_info(generation_jobs)"
            ).fetchall()
        }
        conn.close()
        conn = None
        assert {"request_params", "config_snapshot", "skill_refs", "skill_manifest"} <= after_cols
        # 迁移前后结构对拍：升上来的库 == 模型直接建出来的库。
        assert diff_snapshots(_models_snapshot(), schema_snapshot(path)) == []
    finally:
        if conn is not None:
            conn.close()
        engine.dispose()


def _script_config(backend_root):
    """造一份只用于读迁移链的 Alembic Config（不连库）。"""
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(backend_root / "migrations"))
    return cfg
