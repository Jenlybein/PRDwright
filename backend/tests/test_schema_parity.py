"""结构对拍（v6 / T16.3；#77 起跑在 Postgres 上）：证明「模型」忠实描述「现有库」。

这是整次数据层迁移的**关键门**。模型少一列的后果是「写不进去、读出来是默认值」，
运行时不报错——只能靠这里挡住。

三条判据：

1. `create_all()` 建出来的库，与 **Alembic 基线**建出来的库，**语义快照完全一致**；
2. 自增主键的 id **行为上**不复用（Postgres 由 `SERIAL` 序列保证：序列只增不回退，
   删掉最大行也不会复用它的 id）；
3. 没有裸 `str` 字段（SQLModel 会把它映射成**没有长度**的 `VARCHAR`，与结构对拍的类型口径漂移）；
4. 布尔列的默认值写成布尔字面量（`true`/`false`），不是 `0`/`1`。

#78 收缩到单一后端后，这里删掉了两条只为 SQLite 服务的断言：`sqlite_autoincrement`
的**声明**检查（Postgres 由序列提供自增，那个表选项在 PG 上是被静默忽略的死配置），
以及布尔默认值 `0/1` 与 `true/false` 的归一（那是为了兼容 v6 之前的旧 SQLite 库；
旧库在 v9 已被拒绝启动）。行为断言（id 不复用）保留。
"""

from __future__ import annotations

import pytest
import sqlalchemy
from sqlmodel import SQLModel

import dbsupport
from backend.core.database import build_engine
from backend.core.models import BOOLEAN_COLUMNS
from backend.core.schema_snapshot import diff_snapshots, inspector_snapshot

# 允许出现在库里的列类型。
#
# VARCHAR 是**故意**的：索引列必须有长度（`short_text`），
# 所以判据是「不许**没有长度**的 String」——那才是裸 str 的特征。
#
# BIGINTEGER（`BIGINT`）是 #86 登记进来的：`job_events.id` 是**全局自增游标**，
# 事件表是增长最快的一张表，`INTEGER`（21 亿）不该成为它的天花板。它是一个标准整型，
# 与其余整型列同族，不影响本用例要防的两件事（裸 str / 未登记的奇特类型）。
_ALLOWED_TYPES = {"TEXT", "INTEGER", "BOOLEAN", "BLOB", "LARGEBINARY", "STRING", "BIGINTEGER"}


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
    与「索引列必须有界、大文本必须显式」的口径分不开，结构对拍会全表报漂移。

    `String(n)` 是**合法**形态（索引列必须有长度），
    所以判据是「不许**没有长度**的 String」。
    """
    from backend.core import models  # noqa: F401

    offenders: list[str] = []
    for table in SQLModel.metadata.tables.values():
        for column in table.columns:
            # `long_text()` 现在是纯 Text；这里仍剥 `impl` 外壳，防将来又加 variant
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

    `BOOLEAN_COLUMNS` 是布尔列的登记清单：默认值必须写 `true`/`false`（下一条用例
    检查），新增布尔列必须在这里登记。这条用例负责让「忘了登记」变成红灯。
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

    Postgres 的 `BOOLEAN` 不接受整数默认值：空库跑基线的第一条建表语句就会报
    「column is of type boolean but default expression is of type integer」。

    判据落在「字面量本身」：`false` / `true` 才是合法布尔字面量。
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


def test_partial_index_declares_postgres_predicate():
    """`ux_chat_turns_job_role` 的 WHERE 必须在 Postgres 方言上声明。

    这条索引是**部分唯一索引**（只约束 `job_id` 非空的行）。`Index` 的方言选项是
    各自独立的——只写 `sqlite_where` 的话，Postgres 上会被**静默忽略**，建成一条
    全量唯一索引：语义少了 WHERE 那一层，结构快照也对不上。单后端之后只留
    `postgresql_where`，这条用例负责让「忘了声明」变成红灯。
    """
    from backend.core import models  # noqa: F401

    table = SQLModel.metadata.tables["chat_turns"]
    index = next(item for item in table.indexes if item.name == "ux_chat_turns_job_role")
    where = index.dialect_options["postgresql"]["where"]
    assert where is not None, "`ux_chat_turns_job_role` 没有声明 postgresql_where"
    assert index.dialect_options["sqlite"]["where"] is None, (
        "单后端之后不该再声明 sqlite_where（那是已删除的 SQLite 分支的写法）"
    )


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
