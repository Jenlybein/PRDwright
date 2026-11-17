"""Postgres 主链路集成（票 #76「Postgres 接入（扩展期）」）。

## 这张票要证明什么

1. **应用能用 Postgres 启动并跑通主链路**：空库 → 迁移到 head → 连接池预热 →
   `/readyz` 可写探针 200 → 提交任务 → 跑到终态 → 正文落到 `document_versions`；
2. **结构校验没有被放宽**：PG 上的 `stamp` 路径（已有结构、没有 `alembic_version`）
   必须逐列逐索引对拍通过；真的漂移时仍然拒绝启动；
3. **按方言的写法**：就绪探针在 PG 上不许发 `BEGIN IMMEDIATE` / `PRAGMA`，
   只读事务里必须如实说「不可写」。

## 为什么单独一个文件

「同一套用例在 PG 上跑通」是 #77 的范围（换测试基座：模板库 / 每用例 `CREATE
DATABASE`）。本文件只挑**主链路**这一条纵向切片，在真库上走一遍；它用的是既有的
公开接缝（HTTP + 迁移编排 + 就绪探针），不构造任何内部运行态。

## 库从哪来（#77 之后没有「跳过」这条路）

连 `PRDWRIGHT_TEST_POSTGRES_URL`（维护库地址、**不带库名**，缺省
`postgresql+psycopg://postgres:postgres@127.0.0.1:5433`）的维护库，**每个用例新建
一个唯一命名的库**，用例结束删掉。不碰共享库里的既有数据，也不依赖上一个用例留下的结构。

⚠️ 以前这里是「没 PG 就整模块跳过」。票 #77 之后全量用例都要在 PG 上跑，
**跳过就是静默绿**——所以跳过策略已删除：基座连不上库时由 `conftest` 的
`isolated_db` 直接失败并给出操作指引（见 `dbsupport.PostgresUnavailable`）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import inspect, text

import dbsupport
from backend.core import database as database_module
from backend.core.schema import (
    SchemaMismatchError,
    current_revision,
    head_revision,
    prepare_schema,
)


# ---------------------------------------------------------------------------
# 夹具：每用例一个独立的 PG 库
# ---------------------------------------------------------------------------


@pytest.fixture
def pg_database():
    """建一个唯一命名的**空**库，用例结束删掉；返回完整 URL。

    空库（而不是从模板派生）：本文件要验的正是「全新库上一次跑通迁移链」。
    """
    with dbsupport.empty_database() as url:
        yield url


@pytest.fixture
def pg_engine(pg_database):
    """按应用同一条路（`build_engine` → `prepare_schema`）把空库带到 head。"""
    engine = database_module.build_engine(pg_database)
    try:
        prepare_schema(engine)
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def pg_client(pg_database, isolated_db, temp_skills_root, monkeypatch):  # noqa: ARG001
    """把整个应用绑到这个 PG 库上的 TestClient（lifespan 会迁移 + 预热）。

    ⚠️ 排序依赖：`DATABASE_URL` 必须在 `get_settings()` 被读之前设进环境。
    `isolated_db` 先跑完（它会 `cache_clear()` 一次，并把 `DATABASE_URL` 指到本用例
    那个派生库），所以我们这里再覆盖成「空库」并清一次。
    """
    from fastapi.testclient import TestClient

    from backend.core import runtime_config
    from backend.core.config import get_settings
    from backend.core.engine import reset_engine

    monkeypatch.setenv("DATABASE_URL", pg_database)
    get_settings.cache_clear()
    runtime_config.invalidate()
    reset_engine()

    from conftest import write_skill_package

    write_skill_package(temp_skills_root, "prd-generator")

    from backend.main import app

    engine = reset_engine()  # noqa: F841 - 真的把上一份引擎收掉；返回值为 None
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        # 必须在 `pg_database` 删库之前把池关干净（夹具销毁顺序：本夹具先于它）
        reset_engine()
        get_settings.cache_clear()
        runtime_config.invalidate()


@pytest.fixture
def pg_engine(pg_database):
    """按应用同一条路（`build_engine` → `prepare_schema`）把空库带到 head。"""
    engine = database_module.build_engine(pg_database)
    try:
        prepare_schema(engine)
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def pg_client(pg_database, isolated_db, temp_skills_root, monkeypatch):  # noqa: ARG001
    """把整个应用绑到这个 PG 库上的 TestClient（lifespan 会迁移 + 预热）。

    ⚠️ 排序依赖：`DATABASE_URL` 必须在 `get_settings()` 被读之前设进环境，且
    `DB_PATH`（`isolated_db` 里的兼容糖）要被压过——优先级契约见 `core/database.py`。
    `isolated_db` 先跑完（它会 `cache_clear()` 一次），所以我们这里再清一次。
    """
    from fastapi.testclient import TestClient

    from backend.core import runtime_config
    from backend.core.config import get_settings
    from backend.core.engine import reset_engine

    monkeypatch.setenv("DATABASE_URL", pg_database)
    get_settings.cache_clear()
    runtime_config.invalidate()
    reset_engine()

    from conftest import write_skill_package

    write_skill_package(temp_skills_root, "prd-generator")

    from backend.main import app

    engine = reset_engine()  # noqa: F841 - 真的把上一份引擎收掉；返回值为 None
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        # 必须在 `pg_database` 删库之前把池关干净（夹具销毁顺序：本夹具先于它）
        reset_engine()
        get_settings.cache_clear()
        runtime_config.invalidate()


# ---------------------------------------------------------------------------
# 一、建库：空库上的迁移链必须一次跑通
# ---------------------------------------------------------------------------


def test_empty_postgres_database_migrates_to_head(pg_database):
    """空库 → `upgrade head`：版本到最新、16 张业务表 + 迁移账本都在。

    这是「迁移能在全新库上一次跑通」那条 AC 的**空库一半**。它同时钉住 #76 的
    第一号阻塞点：`0001` 的布尔列默认值（`DEFAULT 0`/`1`）在 PG 的 `BOOLEAN` 上非法，
    所以这条用例以前在第一条建表语句上就炸。
    """
    engine = database_module.build_engine(pg_database)
    try:
        revision = prepare_schema(engine)
        assert revision == head_revision(), "迁移没有跑到最新"
        assert current_revision(engine) == head_revision()

        tables = set(inspect(engine).get_table_names())
        assert "alembic_version" in tables
        business = {name for name in tables if not name.startswith("pg_")}
        assert len(business) == 17, f"应当是 16 张业务表 + alembic_version，实际 {sorted(business)}"

        # 布尔列的**库级默认值**必须真的可插入：以前这里会撞上
        # 「column is of type boolean but default expression is of type integer」
        with engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (username, password_hash, salt, created_at) "
                    "VALUES (:u, :h, :s, :c)"
                ),
                {"u": "probe", "h": "h", "s": b"", "c": "2026-01-01T00:00:00"},
            )
            row = conn.execute(text("SELECT is_active, is_admin FROM users")).one()
            assert tuple(row) == (True, False), "布尔默认值不是 true/false"
    finally:
        engine.dispose()


def test_readyz_reports_writable_on_postgres(pg_client):
    """`/readyz` 在 PG 上返回 200：可写探针走的是 PG 认得的「真写一次再回滚」。

    以前探针无条件发 `BEGIN IMMEDIATE` + `PRAGMA application_id`——PG 上它们是语法
    错误，探针会把自己判成「库不可写」并一直往编排器吐 503（摘流）。
    """
    response = pg_client.get("/readyz")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ready", body
    assert body["checks"]["database"]["ok"] is True, body


def test_write_probe_reports_read_only_transaction(pg_engine):
    """只读事务里探针必须如实报「不可写」——这是它与 `/health` 分工的前提。

    正面（可写）由上一个用例证明；这里证明它**不是**恒真：把事务设成 READ ONLY
    之后，PG 会在写入那一句上抛错，探针把错误外包给调用方（端点转 503）。
    """
    from backend.core.dialect import write_probe

    with pg_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            with pytest.raises(Exception) as caught:  # noqa: BLE001 - 驱动/方言都可能抛
                write_probe(conn)
            assert "read-only" in str(caught.value).lower() or "readonly" in str(caught.value).lower(), (
                f"报错不像「只读」：{caught.value}"
            )
        finally:
            transaction.rollback()


# ---------------------------------------------------------------------------
# 二、结构校验：PG 上也要逐列逐索引对拍，不放宽
# ---------------------------------------------------------------------------


def test_existing_postgres_schema_is_stamped_without_any_ddl(pg_engine):
    """已有当前结构的 PG 库（没有账本）→ `stamp head`，结构一个字节都不变。

    这条同时把「PG 侧结构快照」逼出来：`_models_snapshot()` 以前只会用 SQLite 临时库
    做期望形状，PG 侧拿不到同语义的快照，`stamp` 根本走不过来。
    """
    from backend.core.schema_snapshot import inspector_snapshot

    with pg_engine.connect() as conn:
        conn.exec_driver_sql("DROP TABLE alembic_version")
        conn.commit()

    before = inspector_snapshot(pg_engine)

    revision = prepare_schema(pg_engine)

    assert revision == head_revision()
    assert inspector_snapshot(pg_engine) == before, "stamp 路径执行了 DDL：结构被改了"


def test_mismatched_postgres_schema_refuses_to_start(pg_engine):
    """PG 上的真实漂移同样拒绝启动，且差异明细指出是哪一列。

    结构校验**不能**因为「换库了」就放宽：`stamp` 只写一行版本，藏起来的差异
    会在某次生成时以「写不进去、读出来是默认值」的形式出现。
    """
    with pg_engine.connect() as conn:
        conn.exec_driver_sql("DROP TABLE alembic_version")
        conn.exec_driver_sql(
            "ALTER TABLE users ADD COLUMN legacy_extra TEXT NOT NULL DEFAULT ''"
        )
        conn.commit()

    with pytest.raises(SchemaMismatchError) as excinfo:
        prepare_schema(pg_engine)

    assert "users.legacy_extra" in str(excinfo.value), "差异明细里必须指出是哪一列"


# ---------------------------------------------------------------------------
# 三、主链路：提交 → 生成 → 已定稿 → 正文落到文档版本
# ---------------------------------------------------------------------------


def _events(response) -> list[dict[str, Any]]:  # noqa: ANN001 - TestClient 响应
    """把 SSE 响应解析成事件 dict 列表（`data:` 行即 `StreamEvent` 的 JSON）。"""
    payloads: list[dict[str, Any]] = []
    for line in response.text.splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[len("data:") :].strip()
        if not raw or raw == "[DONE]":
            continue
        payloads.append(json.loads(raw))
    return payloads


def _fake_document_service(monkeypatch, text_out: str) -> None:  # noqa: ANN001
    """模型边界换注入假 LLM 的实例（既有的离线假环境形态，零 token）。"""
    from fakes import ScriptedLLM

    from backend.services import document_service

    monkeypatch.setattr(
        document_service,
        "get_service",
        lambda: document_service.DocumentService(llm=ScriptedLLM([text_out])),
    )


def _create_solution(client) -> int:  # noqa: ANN001
    response = client.post(
        "/api/solutions",
        json={"title": "Postgres 主链路", "mode": "structured", "stage": "review-prd", "data": "{}"},
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _seed_prd(client, solution_id: int) -> None:  # noqa: ANN001
    """把 PRD 上游放进方案（接口文档的上游由服务端取，见 C-R3）。"""
    saved = client.put(
        f"/api/solutions/{solution_id}/documents/prd",
        json={"content": "# 演示产品\n\n## 1. 功能需求\n\n- 创建订单\n"},
    )
    assert saved.status_code == 200, saved.text


def test_generation_job_reaches_finalized_document_version(pg_client, monkeypatch):
    """`POST /api/jobs` → 订阅到 `run_summary` → 文档当前版与版本详情都是那份定稿正文。

    断言的顺序就是 AC 的顺序：任务终态 `completed`（跑到已定稿）→ 文档当前版本里
    是**定稿正文** → 版本链上确实新增了一条、且它就是当前版本。全部只走公开 HTTP
    接缝，不在用例里构造运行态。
    """
    from conftest import register

    from backend.core.engine import get_engine

    finalized = "## 概览\n\nPostgres 主链路定稿正文\n"

    register(pg_client)
    solution_id = _create_solution(pg_client)
    _seed_prd(pg_client, solution_id)
    _fake_document_service(monkeypatch, finalized)

    created = pg_client.post(
        "/api/jobs", json={"kind": "api_docs", "solution_id": solution_id, "review_enabled": False}
    )
    assert created.status_code == 201, created.text
    job_id = int(created.json()["job_id"])

    stream = pg_client.get(f"/api/jobs/{job_id}/stream")
    assert stream.status_code == 200, stream.text
    events = _events(stream)
    assert events[-1]["type"] == "run_summary", [item["type"] for item in events]
    assert events[-1]["data"]["status"] == "completed"

    # ① 任务跑到终态（已定稿）
    detail = pg_client.get(f"/api/jobs/{job_id}").json()
    assert detail["status"] == "completed"

    # ② 正文落到文档（当前版本）
    documents = pg_client.get(f"/api/solutions/{solution_id}/documents").json()["documents"]
    api_docs = next(item for item in documents if item["kind"] == "api_docs")
    assert "Postgres 主链路定稿正文" in api_docs["content"]

    # ③ 版本链上多了一条，且正文就是它（「正文落文档版本」的字面判据）
    versions = pg_client.get(
        f"/api/solutions/{solution_id}/documents/api_docs/versions"
    ).json()["versions"]
    assert len(versions) == 1
    assert versions[0]["version_no"] == 1
    assert versions[0]["is_current"] is True
    version_detail = pg_client.get(
        f"/api/solutions/{solution_id}/documents/api_docs/versions/{versions[0]['version_id']}"
    ).json()
    assert "Postgres 主链路定稿正文" in version_detail["content"]

    # ④ 这些行真的在 PG 里（不是悄悄回落到某个 SQLite 文件）
    engine = get_engine()
    assert engine.url.get_backend_name() == "postgresql"
    with engine.connect() as conn:
        stored = conn.exec_driver_sql(
            "SELECT v.content FROM document_versions v "
            "JOIN documents d ON d.id = v.document_id WHERE d.kind = 'api_docs'"
        ).scalars().all()
        assert len(stored) == 1, f"api_docs 在文档版本表里有 {len(stored)} 条"
        assert "Postgres 主链路定稿正文" in stored[0]
        assert conn.exec_driver_sql("SELECT status FROM generation_jobs").scalar() == "completed"
