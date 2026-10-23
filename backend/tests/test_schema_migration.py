"""迁移编排守卫（v6 / T16.4 / ADR-076）。

四条用例，守的是**升级安全性**，不是功能正确性：

| 用例 | 不做会怎样 |
| --- | --- |
| 空库 → 基线 | 全新部署起不来 |
| 已有结构但无 `alembic_version` → stamp（零 DDL） | 已有库被重建 / 数据丢失 |
| 结构不符 → 拒绝启动 | 差异被永久藏起来，之后某个列「消失」 |
| 基线库 == 模型库（语义快照） | 模型改了却忘了写迁移，只在某些部署上炸 |

> **旧版本库（v20 之前）不再支持自动升级。** `PRAGMA user_version` 的 20 步链已在
> v6.1 删除；那种库现在会撞上第三条用例（拒绝启动），并在错误信息里被告知该怎么做。
> 这是有意的：宁可起不来，也不要「悄悄 stamp 成最新、把少掉的结构永久藏起来」。
"""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine

from backend.core.schema import (
    SchemaMismatchError,
    current_revision,
    prepare_schema,
)
from backend.core.schema_snapshot import schema_snapshot


def _db_path() -> str:
    """当前用例的库文件路径（conftest 已把 DB_PATH 指到 tmp_path）。"""
    from backend.core.paths import default_db_file

    return str(default_db_file())


def _connect() -> sqlite3.Connection:
    """直接用 DBAPI 连库（避免与被测代码争 Engine 池里的连接）。"""
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _forget_alembic_adoption() -> None:
    """把库退回「结构已是最新、但没登记过 alembic 版本」的状态。

    这正是「从 v6 之前的部署直接过来」的样子：结构是对的，只差一行版本登记。
    删 `alembic_version` 比另建一个库更贴近现实，也不受 conftest 已建库的影响。
    """
    conn = _connect()
    try:
        conn.execute("DROP TABLE IF EXISTS alembic_version")
        conn.commit()
    finally:
        conn.close()


def test_empty_db_gets_baseline(tmp_path):
    """空库 → `upgrade head` → 14 张表 + `alembic_version = 0001`。"""
    path = tmp_path / "empty.db"
    engine = create_engine(f"sqlite+pysqlite:///{path}")
    try:
        revision = prepare_schema(engine)
        assert revision == "0001"
        assert current_revision(engine) == "0001"

        tables = {
            row[0]
            for row in sqlite3.connect(path)
            .execute("SELECT name FROM sqlite_master WHERE type='table'")
            .fetchall()
        }
        assert "alembic_version" in tables
        assert len([t for t in tables if not t.startswith("sqlite_")]) == 15  # 14 + alembic
    finally:
        engine.dispose()


def test_existing_current_db_is_stamped_without_any_ddl(isolated_db):  # noqa: ARG001
    """已有当前结构的库 → `stamp head`：**结构一个字节都不变**，数据一行不少。"""
    _forget_alembic_adoption()
    before = schema_snapshot(_db_path())

    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, salt, created_at) VALUES (?,?,?,?)",
            ("keep-me", "h", b"s", "2026-01-01T00:00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    from backend.core.engine import get_engine

    revision = prepare_schema(get_engine())

    assert revision == "0001"
    assert schema_snapshot(_db_path()) == before, "stamp 路径执行了 DDL：结构被改了"

    conn = _connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM users WHERE username='keep-me'").fetchone()[0] == 1
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0001"
    finally:
        conn.close()


def test_mismatched_db_refuses_to_start(isolated_db):  # noqa: ARG001
    """结构与模型不符 → 抛 `SchemaMismatchError`，**不 stamp、不继续**。

    这也是「更早版本的库」现在的归宿：不再有自动升级，只有一条说清原因的拒绝。
    """
    _forget_alembic_adoption()
    conn = _connect()
    try:
        # 造一个真实存在的漂移：多一个模型里没有的列
        conn.execute("ALTER TABLE users ADD COLUMN legacy_extra TEXT NOT NULL DEFAULT ''")
        conn.commit()
    finally:
        conn.close()

    from backend.core.engine import get_engine

    with pytest.raises(SchemaMismatchError) as excinfo:
        prepare_schema(get_engine())

    assert "users.legacy_extra" in str(excinfo.value), "差异明细里必须指出是哪一列"
    assert "v20" in str(excinfo.value), "必须告诉人这是旧库、以及该怎么做"


def test_baseline_produces_exactly_the_model_schema(tmp_path):
    """基线迁移建出来的库 == 模型建出来的库（守卫 C6 的实际实现）。

    ## 为什么不用 `alembic check`

    实测（SQLAlchemy 2.1.6 + SQLite 方言）有两个**反射缺陷**，会让 `alembic check` 恒红：

    1. `PRAGMA foreign_key_list` 里的 `on_delete=...` 没有被反射进 `options`
       → 每个带 `ON DELETE` 的外键都被判成 `remove_fk` + `add_fk`；
    2. 主键列反射出来 `nullable=True`（`INTEGER PRIMARY KEY` 的声明里没有 `NOT NULL`）
       → 每个主键都被判成 `modify_nullable`。

    两者都不是真实漂移。与其在测试里过滤幻影差异（过滤条件一多，真差异也会被吞掉），
    不如换一个**反射无关**的判据：直接比较「基线建出来的库」与「模型建出来的库」的语义快照。

    这条判据同时覆盖「模型改了却忘了写迁移」：模型一变，两边快照就不再相等。
    """
    from backend.core.schema import _models_snapshot

    path = tmp_path / "baseline.db"
    engine = create_engine(f"sqlite+pysqlite:///{path}")
    try:
        prepare_schema(engine)
        assert schema_snapshot(path) == _models_snapshot()
    finally:
        engine.dispose()


def test_models_declare_no_relationship():
    """模型不声明 `Relationship()`（ADR-080）。

    声明了就会在「Session 已关闭」时触发懒加载 → `DetachedInstanceError`，
    而这类错误只在特定分支偶发。跨表查询一律显式 join。
    """
    from sqlalchemy import inspect as sa_inspect

    from backend.core import models

    offenders: list[str] = []
    for name in models.__all__:
        if name == "BOOLEAN_COLUMNS":
            continue
        model = getattr(models, name)
        relationships = sa_inspect(model).relationships
        if relationships:
            offenders.append(f"{name}: {[r.key for r in relationships]}")
    assert offenders == []
