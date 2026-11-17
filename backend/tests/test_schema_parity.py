"""结构对拍（v6 / T16.3；#77 起跑在 Postgres 上）：证明「模型」忠实描述「现有库」。

这是整次数据层迁移的**关键门**。模型少一列的后果是「写不进去、读出来是默认值」，
运行时不报错——只能靠这里挡住。

三条判据：

1. `create_all()` 建出来的库，与 **Alembic 基线**建出来的库，**语义快照完全一致**；
2. 每张有自增主键的表在声明里仍带 `sqlite_autoincrement`，且**行为上** id 不复用
   （Postgres 一侧由 `SERIAL` 序列保证：序列只增不回退，删掉最大行也不会复用它的 id）；
3. 没有裸 `str` 字段（SQLModel 会把它映射成 `VARCHAR`，与现有库的 `TEXT` 全表漂移）。

#77 换库改了**手段**：库从「临时 SQLite 文件」换成「两个临时 Postgres 库」，
结构快照从 `schema_snapshot(文件路径)`（PRAGMA 专用）换成 `inspector_snapshot(engine)`
（SQLAlchemy 反射，任何方言通用）。被验的性质一条没变。
"""

from __future__ import annotations

import pytest
import sqlalchemy
from sqlmodel import SQLModel

import dbsupport
from backend.core.database import build_engine
from backend.core.schema_snapshot import (
    BOOLEAN_COLUMNS,
    diff_snapshots,
    inspector_snapshot,
)

# 允许出现在库里的列类型。
#
# v8 之前这里**不含** VARCHAR：那时裸 `str` 字段会生成它，与现有库的 TEXT 在 SQLite
# 里行为相同、类型名不同，会让 alembic 产生幻影迁移。
# v8 之后 VARCHAR 是**故意**的：索引列在 MySQL 上必须有长度，
# 所以判据从「不许 VARCHAR」改成「不许**没有长度**的 String」——那才是裸 str 的特征。
_ALLOWED_TYPES = {"TEXT", "INTEGER", "BOOLEAN", "BLOB", "LARGEBINARY", "STRING"}

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


def _build_models_db(url: str):
    """用模型在一个**空库**上 `create_all`，返回它的 Engine（模拟「空库升级到最新」）。"""
    from backend.core import models  # noqa: F401  导入即注册模型

    engine = build_engine(url)
    try:
        SQLModel.metadata.create_all(engine)
    finally:
        pass
    return engine


def test_create_all_matches_alembic_baseline():
    """`create_all()` 的库 == Alembic 基线建出来的库。

    比较的是语义快照（列/索引/外键），不是建表语句文本——两者的缩进、列序、
    约束渲染位置都不同，文本 diff 会把无关差异报成漂移。

    两个库都是**当场新建的空库**：一个跑基线迁移，一个直接 `create_all`，
    所以这条同时证明「基线迁移没有漏掉模型里的任何东西」。
    """
    from backend.core.schema import prepare_schema

    engine_alembic = None
    engine_models = None
    try:
        with dbsupport.empty_database() as alembic_url:
            engine_alembic = build_engine(alembic_url)
            prepare_schema(engine_alembic)
            alembic_built = inspector_snapshot(engine_alembic)

            with dbsupport.empty_database() as models_url:
                engine_models = _build_models_db(models_url)
                fresh = inspector_snapshot(engine_models)

        problems = diff_snapshots(alembic_built, fresh)
        assert problems == [], "模型与基线库结构不一致：\n  " + "\n  ".join(problems)
    finally:
        for engine in (engine_alembic, engine_models):
            if engine is not None:
                engine.dispose()


def test_every_table_is_modelled():
    """库里的表集合 == 模型声明的表集合（防「模型少一张表」）。"""
    from backend.core import models  # noqa: F401
    from backend.core.schema import prepare_schema

    with dbsupport.empty_database() as url:
        engine = build_engine(url)
        try:
            prepare_schema(engine)
            legacy_tables = set(inspector_snapshot(engine))
        finally:
            engine.dispose()

    model_tables = set(SQLModel.metadata.tables)
    assert model_tables == legacy_tables


def test_no_bare_str_columns():
    """字符串列必须要么有长度（`short_text`），要么是显式 `Text`（长文本）。

    这条守的是「裸 `str` 字段」——SQLModel 会把它映射成**没有长度**的 `VARCHAR`，
    在 SQLite 里与 `TEXT` 亲和性相同、行为一致，但类型名不同 → 结构对拍与
    `alembic check` 会全表报漂移。

    v8 之后多了 `String(n)` 这种**合法**形态（索引列必须有长度），
    所以判据从「不许 VARCHAR」细化成「不许**没有长度**的 String」。
    """
    from backend.core import models  # noqa: F401

    offenders: list[str] = []
    for table in SQLModel.metadata.tables.values():
        for column in table.columns:
            # 剥掉 `with_variant` 的外壳：`long_text()` 在 SQLite 下就是 Text
            impl = getattr(column.type, "impl", column.type)
            type_name = type(impl).__name__.upper()
            if type_name not in _ALLOWED_TYPES:
                offenders.append(f"{table.name}.{column.name} → {type_name}")
            elif type_name == "STRING" and not getattr(impl, "length", None):
                offenders.append(
                    f"{table.name}.{column.name} → String 没有长度（裸 str 的特征）"
                )
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


def test_boolean_server_defaults_are_boolean_literals():
    """布尔列的 `server_default` 必须是 `true` / `false`，不能是 `0` / `1`（#76）。

    ```sql
    ALTER TABLE ... ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT 0   -- ✗ Postgres
    ```

    SQLite 把 `BOOLEAN` 当整数、`DEFAULT 0` 毫无问题，所以这个坑在 SQLite 上
    **完全看不出来**：接上 Postgres 之后，空库跑 `0001` 的第一条建表语句就会报
    「column is of type boolean but default expression is of type integer」。

    判据落在「字面量本身」：`false` / `true` 在 SQLite（3.23 起）与 Postgres 上
    都是合法布尔字面量，两边语同一个写法——不需要在迁移里按方言分支。
    """
    from backend.core import models  # noqa: F401

    offenders: list[str] = []
    for table_name, column_name in sorted(BOOLEAN_COLUMNS):
        column = SQLModel.metadata.tables[table_name].columns[column_name]
        default = column.server_default
        literal = getattr(getattr(default, "arg", None), "text", None)
        if literal is None or literal.strip().lower() not in {"true", "false"}:
            offenders.append(f"{table_name}.{column_name} → {literal!r}")
    assert offenders == [], (
        "布尔列的默认值必须是 true/false 字面量（0/1 在 Postgres 的 BOOLEAN 上非法）：\n  "
        + "\n  ".join(offenders)
    )


def test_partial_index_declares_predicate_for_both_dialects():
    """`ux_chat_turns_job_role` 的 WHERE 必须 SQLite / Postgres 都声明（#76）。

    这条索引是**部分唯一索引**（只约束 `job_id` 非空的行）。`sqlite_where` 在
    Postgres 上会被忽略——不是报错，而是静默建成全量唯一索引：SQLite 那份语义
    还在，PG 那份悄悄丢了 WHERE，两边结构快照也对不上。
    """
    from backend.core import models  # noqa: F401

    table = SQLModel.metadata.tables["chat_turns"]
    index = next(item for item in table.indexes if item.name == "ux_chat_turns_job_role")
    for dialect in ("sqlite", "postgresql"):
        where = index.dialect_options[dialect]["where"]
        assert where is not None, f"`ux_chat_turns_job_role` 没有声明 {dialect}_where"


def test_boolean_default_normalization_accepts_legacy_zero_one():
    """布尔列的默认值 `0/1` 与 `false/true` 视为同一个（#76 的兼容面）。

    `0001` 起模型与迁移统一写 `false`/`true`（Postgres 的 BOOLEAN 只收后者）；
    v6 之前的老 DDL 里是 `DEFAULT 0`/`1`。两者在库里同义，所以结构对拍必须归一——
    否则「已有结构、没有 `alembic_version`」的老 SQLite 库会在 `stamp` 路径上被
    误判成漂移、拒绝启动。
    """
    from backend.core.schema_snapshot import _norm_default

    assert _norm_default("0", "users", "is_active") == _norm_default(
        "false", "users", "is_active"
    )
    assert _norm_default("1", "users", "is_admin") == _norm_default(
        "true", "users", "is_admin"
    )
    # 非布尔列不参与这条归一：整数 `0` 就是 `0`
    assert _norm_default("0", "invite_codes", "max_uses") == "0"


@pytest.mark.parametrize("table", sorted(_AUTOINCREMENT_TABLES))

def test_autoincrement_is_declared(table):
    """每张自增主键表在**声明**里仍带 `sqlite_autoincrement`。

    这是 SQLite 专属表选项（非 SQLite 方言上会被静默忽略）。留着它有两个意义：

    - 本仓库的模型是「一份声明，多方言」的，SQLite 那个后端在 #78 收缩之前仍要能建库；
    - PG 一侧的自增来自 `SERIAL`（SQLAlchemy 对整数主键自动生成），**不需要**这个声明。

    真正有价值的是下一条**行为**验证：删掉最大 id 之后新 id 不复用。声明只是它的一半证据。
    """
    from backend.core import models  # noqa: F401

    table_obj = SQLModel.metadata.tables[table]
    assert table_obj.dialect_options["sqlite"]["autoincrement"] is True


def test_deleted_max_id_is_not_reused(isolated_db):  # noqa: ARG001
    """行为验证：删掉最大 id 的行之后，新插入的 id **不复用**。

    这条比「声明里有没有 AUTOINCREMENT」更硬：它验证的是库的**实际行为**。
    Postgres 一侧由 `SERIAL` 序列保证——序列只增不回退，删行不会让下一个 id 落回来。
    """
    from backend.core import models
    from dbsupport import connect

    models.User  # 确保模型已注册（表由 conftest 的模板库建好，这里只写入）
    with connect() as conn:
        now = "2026-01-01T00:00:00"
        for name in ("u1", "u2", "u3"):
            conn.execute(
                "INSERT INTO users (username, password_hash, salt, created_at) "
                "VALUES (%s,%s,%s,%s)",
                (name, "h", b"s", now),
            )
        conn.commit()
        assert conn.execute("SELECT MAX(id) FROM users").fetchone()[0] == 3
        conn.execute("DELETE FROM users WHERE id = 3")
        conn.execute(
            "INSERT INTO users (username, password_hash, salt, created_at) "
            "VALUES (%s,%s,%s,%s)",
            ("u4", "h", b"s", now),
        )
        conn.commit()
        new_id = conn.execute("SELECT id FROM users WHERE username = 'u4'").fetchone()[0]

    assert new_id == 4, "id 被复用了：自增序列退回了（Postgres 上是 SERIAL 出问题）"


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
