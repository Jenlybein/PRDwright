"""结构对拍（v6 / T16.3）：证明「模型」忠实描述「现有库」。

这是整次数据层迁移的**关键门**。模型少一列的后果是「写不进去、读出来是默认值」，
运行时不报错——只能靠这里挡住。

三条判据：

1. `create_all()` 建出来的库，与 **Alembic 基线**建出来的库，**语义快照完全一致**；
2. 每张有自增主键的表真的带 `AUTOINCREMENT`（id 不复用，翻页游标才不会看见已删除的行）；
3. 没有裸 `str` 字段（SQLModel 会把它映射成 `VARCHAR`，与现有库的 `TEXT` 全表漂移）。
"""

from __future__ import annotations

import sqlite3

import pytest
import sqlalchemy
from sqlalchemy import create_engine
from sqlmodel import SQLModel

from backend.core.schema_snapshot import (
    BOOLEAN_COLUMNS,
    diff_snapshots,
    schema_snapshot,
)

# 允许出现在库里的列类型。**不含 VARCHAR**：那是裸 `str` 字段的产物，
# 与现有库的 TEXT 在 SQLite 里行为相同、类型名不同，会让 alembic 产生幻影迁移。
_ALLOWED_TYPES = {"TEXT", "INTEGER", "BOOLEAN", "BLOB", "LARGEBINARY"}

# 有自增主键的表（`AUTOINCREMENT`）。其余表的主键是 TEXT，不需要
_AUTOINCREMENT_TABLES = {
    "users",
    "login_failures",
    "solutions",
    "documents",
    "document_versions",
    "generation_jobs",
    "chat_sessions",
    "chat_turns",
    "app_logs",
    "admin_audit_log",
}


def _build_models_db(path) -> None:
    """用模型建一个全新的库（模拟「空库升级到最新」）。"""
    from backend.core import models  # noqa: F401  导入即注册模型

    engine = create_engine(f"sqlite+pysqlite:///{path}")
    try:
        SQLModel.metadata.create_all(engine)
    finally:
        engine.dispose()


def test_create_all_matches_alembic_baseline(isolated_db, tmp_path):  # noqa: ARG001
    """`create_all()` 的库 == Alembic 基线建出来的库。

    比较的是语义快照（列/索引/外键），不是建表语句文本——两者的缩进、列序、
    约束渲染位置都不同，文本 diff 会把无关差异报成漂移。

    conftest 的那个库就是 `prepare_schema`（`upgrade head`）建的，所以直接取它：
    这条同时证明「基线迁移没有漏掉模型里的任何东西」。
    """
    alembic_built = schema_snapshot(tmp_path / "test.db")

    # 对照路径：用模型直接建库
    models_db = tmp_path / "models.db"
    _build_models_db(models_db)
    fresh = schema_snapshot(models_db)

    problems = diff_snapshots(alembic_built, fresh)
    assert problems == [], "模型与基线库结构不一致：\n  " + "\n  ".join(problems)


def test_every_table_is_modelled(isolated_db, tmp_path):
    """库里的表集合 == 模型声明的表集合（防「模型少一张表」）。"""
    from backend.core import models  # noqa: F401

    legacy_tables = set(schema_snapshot(tmp_path / "test.db"))
    model_tables = set(SQLModel.metadata.tables)
    assert model_tables == legacy_tables


def test_no_bare_str_columns():
    """没有 VARCHAR 列：所有字符串都必须显式 `Column(Text)`。

    裸 `str` 字段会生成 `VARCHAR`。它在 SQLite 里与 `TEXT` 亲和性相同、行为一致，
    但类型名不同 → 结构对拍与 `alembic check` 会全表报漂移。
    """
    from backend.core import models  # noqa: F401

    offenders: list[str] = []
    for table in SQLModel.metadata.tables.values():
        for column in table.columns:
            type_name = type(column.type).__name__.upper()
            if type_name not in _ALLOWED_TYPES:
                offenders.append(f"{table.name}.{column.name} → {type_name}")
    assert offenders == [], "这些列的类型不在允许集合内：\n  " + "\n  ".join(offenders)


def test_boolean_columns_match_whitelist():
    """布尔列集合 == 白名单（守卫 C7）。

    布尔列在库里声明为 `BOOLEAN`、在旧库里是 `INTEGER`，靠两份白名单归一才不产生
    幻影迁移。新增布尔列必须同时登记 `schema_snapshot.BOOLEAN_COLUMNS` 与
    `migrations/env.py`，这条用例负责让「忘了登记」变成红灯。
    """
    from backend.core import models  # noqa: F401

    declared = {
        (table.name, column.name)
        for table in SQLModel.metadata.tables.values()
        for column in table.columns
        if type(column.type).__name__.upper() == "BOOLEAN"
    }
    assert declared == set(BOOLEAN_COLUMNS)


@pytest.mark.parametrize("table", sorted(_AUTOINCREMENT_TABLES))
def test_autoincrement_is_declared(table):
    """每张自增主键表都带 `sqlite_autoincrement`。

    少了它的后果是 SQLite 复用已删除的最大 rowid。`app_logs` / `admin_audit_log`
    的 id 被当作翻页游标用，复用会让「下一页」看见上一次已删除的行。
    """
    from backend.core import models  # noqa: F401

    table_obj = SQLModel.metadata.tables[table]
    assert table_obj.dialect_options["sqlite"]["autoincrement"] is True


def test_deleted_max_id_is_not_reused(isolated_db, tmp_path):
    """行为验证：删掉最大 id 的行之后，新插入的 id **不复用**。

    这条比「声明里有没有 AUTOINCREMENT」更硬：它验证的是 SQLite 的**实际行为**，
    而不是我们对 DDL 的阅读。
    """
    from backend.core import models
    from dbsupport import connect

    models.User  # 确保模型已注册（表由 conftest 的 prepare_schema 建好，这里只写入）
    with connect() as conn:
        now = "2026-01-01T00:00:00"
        for name in ("u1", "u2", "u3"):
            conn.execute(
                "INSERT INTO users (username, password_hash, salt, created_at) VALUES (?,?,?,?)",
                (name, "h", b"s", now),
            )
        conn.commit()
        assert conn.execute("SELECT MAX(id) FROM users").fetchone()[0] == 3
        conn.execute("DELETE FROM users WHERE id = 3")
        conn.execute(
            "INSERT INTO users (username, password_hash, salt, created_at) VALUES (?,?,?,?)",
            ("u4", "h", b"s", now),
        )
        conn.commit()
        new_id = conn.execute("SELECT id FROM users WHERE username = 'u4'").fetchone()[0]

    assert new_id == 4, "id 被复用了：表上没有 AUTOINCREMENT"


def test_metadata_column_is_bound_to_reserved_name():
    """`DocumentVersion.meta` 绑到列名 `metadata`，且没有遮蔽 `SQLModel.metadata`。

    直接写 `metadata: str` 不会报错，但会让 `SQLModel.metadata` 变成一个列对象，
    之后 `create_all()` 找不到任何表（症状是「建库成功但一张表都没有」）。
    """
    from backend.core.models import DocumentVersion

    column_names = {column.name for column in DocumentVersion.__table__.columns}
    assert "metadata" in column_names
    assert "meta" not in column_names

    # 基类的 metadata 必须仍然是一个可用的 MetaData（有 tables 映射且非空）
    assert isinstance(SQLModel.metadata, sqlalchemy.MetaData)
    assert len(SQLModel.metadata.tables) >= 14
