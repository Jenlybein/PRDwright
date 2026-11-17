"""结构快照：把库的 schema 归一成**可比较的语义结构**。

两个使用者：
- `core/schema.py::prepare_schema()`：对已有库 `stamp` 之前，校验它与基线是否一致
  （不一致就拒绝启动——直接 stamp 会把差异永久藏起来）；
- `tests/test_schema_parity.py`：证明「模型建出来的库」与「Alembic 基线建出来的库」一致。

## 两份快照，同一份语义

快照的「读法」有两种，取决于库的类型（#76）：

- **SQLite**（`schema_snapshot(path)`）：读 `PRAGMA table_info` / `index_list` /
  `foreign_key_list`，部分索引的 WHERE 只能从 `sqlite_master.sql` 里抠；
- **其它方言**（`inspector_snapshot(engine)`）：走 SQLAlchemy 的 inspector，
  与库的类型无关（Postgres 上就是 `pg_catalog` 的反射）。

「期望形状」也有两种：

- SQLite：用模型在**临时 SQLite 库**上 `create_all` 再读一遍（`core/schema.py::
  _models_snapshot`）；
- 非 SQLite：直接**从模型元数据**生成同一份语义快照（`metadata_snapshot(metadata,
  dialect)`），类型按**目标方言**编译。这样比较的两边都是「目标库的语言」，
  不需要跨方言猜 `BLOB ↔ BYTEA` 之类的对应关系。

## 为什么不能直接比较建表语句文本

同一个 schema 由不同方式建出来，`sqlite_master.sql` 会不一样：

- 空白与换行（手写 DDL 有缩进，ORM 生成时是单行）；
- 列的定义顺序（手写是业务顺序，ORM 是模型字段顺序——两者被要求一致，但不该由文本兜）；
- `NOT NULL` / `DEFAULT` 的渲染位置与引号风格（`'{}'` vs `'{}'`、`DEFAULT 0` vs `DEFAULT '0'`）；
- `AUTOINCREMENT` 是否出现（SQLModel 默认**不**生成，必须显式开 `sqlite_autoincrement`）。

文本 diff 会把这些无关差异报成「漂移」，真正的漂移（少一列、类型变了、索引丢了）反而
淹没在噪音里。所以这里比较的是语义快照：
列（名/类型/非空/默认值/主键位）、索引（名/唯一/列/部分索引条件）、外键（来源列/目标表列/级联规则）。

## 布尔列的类型归一

现有库把布尔存成 `INTEGER`，模型里用 `bool` 会生成 `BOOLEAN`。两者在 SQLite 里行为一致
（都存 0/1，读回来都是 Python bool），但 `PRAGMA table_info` 报的类型名不同。
不归一就会把「同一列」判成漂移，进而让 `alembic check` 产生幻影迁移。
白名单定义在 `core/models.py::BOOLEAN_COLUMNS`（与 `migrations/env.py` 的
`compare_type` 共用同一份），这里只导入、不再复制一份。
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from sqlalchemy import UniqueConstraint, inspect
from sqlalchemy.engine import Engine

# 布尔列白名单与 alembic 的 `compare_type`（`migrations/env.py`）**共用同一份**：
# 定义在 `core/models.py`，因为它描述的是「模型的事实」。
# 两处若各自维护一份，迟早会一边改了另一边没改，症状是 `alembic check` 时红时绿。
from backend.core.models import BOOLEAN_COLUMNS

# 类型名归一：SQLite 的类型亲和性（type affinity）里 BOOLEAN 与 INTEGER 都可存整数，
# 语义等价，只在声明名上不同。
_TYPE_ALIASES = {"BOOLEAN": "INTEGER"}

# 布尔默认值的两种等价写法：旧库（v6 之前的 DDL）里是 `DEFAULT 0/1`，
# `0001` 起模型与迁移统一成 `false/true`（Postgres 的 BOOLEAN 只收后者）。
_BOOLEAN_DEFAULT_ALIASES = {"0": "false", "1": "true"}

# 这些表不属于业务结构，不参与比较：
# - `sqlite_sequence`：`AUTOINCREMENT` 的副产物，有没有取决于「是否插入过行」，不是结构；
# - `alembic_version`：迁移工具自己的账本。
_IGNORED_TABLES = frozenset({"sqlite_sequence", "alembic_version"})

# 非 SQLite 的 inspector 会把这些系统表也反射出来，同样不参与比较。
_SYSTEM_TABLE_PREFIXES = ("pg_", "sql_")


def _norm_type(table: str, column: str, declared: str | None) -> str:
    """归一列类型名。只对白名单里的布尔列做映射，其余原样（大写）。"""
    kind = (declared or "").strip().upper()
    if (table, column) in BOOLEAN_COLUMNS:
        return _TYPE_ALIASES.get(kind, kind)
    return kind


def _norm_default(
    declared: str | None, table: str | None = None, column: str | None = None
) -> str | None:
    """归一默认值。`''` 与 `'{}'` 这类默认值在两种建表方式下引号风格会不同。

    非 SQLite 的 inspector 还会多出两类「不是字面量本身」的东西，一并归一：

    - **类型转换后缀**：Postgres 把 `DEFAULT ''` 反射成 `''::text`、
      `DEFAULT -1` 反射成 `'-1'::integer`——去掉 `::type` 才是那个值；
    - **自增序列**：自增主键的默认值反射成 `nextval('…'::regclass)`。这不是列默认值
      （SQLite 侧对应 `AUTOINCREMENT`，PRAGMA 根本不报），两边都按「无默认值」记。

    布尔列的默认值额外**按值归一**（`0`/`1` ↔ `false`/`true`，#76）：白名单里的列
    （`BOOLEAN_COLUMNS`）在 SQLite 上两种写法等价，而 `0001` 起模型与迁移统一用
    `false`/`true`。不归一的话，v6 之前建的旧库（DDL 里是 `DEFAULT 0`）在
    `stamp` 路径上会被误判成「结构漂移」而拒绝启动。
    """
    if declared is None:
        return None
    text = str(declared).strip()
    if not text:
        return None
    # 自增序列的默认值：语义上等价于「没有显式默认值」
    if re.match(r"^nextval\(", text, re.IGNORECASE):
        return None
    # 去掉 PG 的类型转换后缀（`::text` / `::character varying` / `::integer[]` …）
    text = re.sub(r"::[a-zA-Z_][a-zA-Z_ ]*(?:\[\])?", "", text).strip()
    # 去外层括号：手写迁移写 `('')`、`(-1)`，反射/ORM 写出 `''`、`-1`
    while len(text) >= 2 and text[0] == "(" and text[-1] == ")":
        text = text[1:-1].strip()
    # `DEFAULT ''` 与 `DEFAULT ""` 等价；统一成不带外层引号的裸形式再比
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1]
    # 布尔列的 `0`/`1` 与 `false`/`true` 在库里是同义的两种写法
    if (table, column) in BOOLEAN_COLUMNS:
        aliased = _BOOLEAN_DEFAULT_ALIASES.get(text.lower())
        if aliased is not None:
            return aliased
    return text


def _norm_where(condition: Any) -> str:
    """归一索引的部分条件（WHERE）：去外层括号、压空白、去尾；空条件返回空串。

    SQLite 从 `sqlite_master.sql` 里抠出来的已经是裸条件（`job_id IS NOT NULL`），
    Postgres 反射出来会带一层括号（`(job_id IS NOT NULL)`）。两边都归一成裸条件。

    入参可能是模型侧给 `TextClause`（`postgresql_where=text("…")`）也可能是 inspector
    给字符串，所以先统一 `str()`——`TextClause` 不能直接做真值判断。
    """
    if condition is None:
        return ""
    text = re.sub(r"\s+", " ", str(condition)).strip()
    if not text:
        return ""
    while len(text) >= 2 and text[0] == "(" and text[-1] == ")":
        text = text[1:-1].strip()
    return text


def _norm_sql(sql: str | None) -> str:
    """把建表/建索引语句归一成「只保留结构关键字」的形式。

    只用于**部分索引的 WHERE 条件**（`PRAGMA` 读不到它，只能从 `sqlite_master.sql` 里取）。
    """
    if not sql:
        return ""
    return re.sub(r"\s+", " ", sql).strip()


def _partial_where(conn: sqlite3.Connection, index_name: str) -> str:
    """取部分索引的 WHERE 条件（普通索引返回空串）。"""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (index_name,)
    ).fetchone()
    sql = _norm_sql(row[0] if row else None)
    match = re.search(r"\bWHERE\b(.*)$", sql, re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows if r[0] not in _IGNORED_TABLES and not r[0].startswith("sqlite_")]


def _columns(conn: sqlite3.Connection, table: str) -> list[tuple[Any, ...]]:
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    # cid, name, type, notnull, dflt_value, pk
    #
    # ⚠️ 主键列的 notnull 要做归一：手写 DDL 是 `id INTEGER PRIMARY KEY AUTOINCREMENT`
    # （SQLite 允许 `INSERT` 时不写这一列，所以 PRAGMA 报 notnull=0），
    # 而 ORM 生成的是 `id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT`（报 notnull=1）。
    # 主键在语义上恒非空，两者的差别只是声明风格 → 统一按 1 记，避免全表假阳性。
    return [
        (
            r[1],
            _norm_type(table, r[1], r[2]),
            1 if (r[3] or r[5]) else 0,
            _norm_default(r[4], table, r[1]),
            r[5],
        )
        for r in rows
    ]


def _indexes(conn: sqlite3.Connection, table: str) -> list[tuple[Any, ...]]:
    out: list[tuple[Any, ...]] = []
    # index_list: seq, name, unique, origin, partial
    for _seq, name, unique, origin, partial in conn.execute(
        f'PRAGMA index_list("{table}")'
    ).fetchall():
        # `origin == 'u'` 是表级 UNIQUE 约束自动建的索引，名字是 sqlite_autoindex_*，
        # 不同建表方式下编号可能不同 → 用列集合代替名字，避免假阳性。
        cols = tuple(
            r[2] for r in conn.execute(f'PRAGMA index_info("{name}")').fetchall()
        )
        key = ("<unique-constraint>", cols) if origin == "u" else (name, cols)
        out.append((key, 1 if unique else 0, _partial_where(conn, name) if partial else ""))
    return sorted(out, key=lambda item: str(item[0]))


def _foreign_keys(conn: sqlite3.Connection, table: str) -> list[tuple[Any, ...]]:
    # id, seq, table, from, to, on_update, on_delete, match
    rows = conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
    return sorted(
        ((r[3], r[2], r[4], r[6]) for r in rows),
        key=lambda item: str(item),
    )


def schema_snapshot(db: Path | str) -> dict[str, Any]:
    """读出一个 SQLite 文件的语义结构快照。

    Args:
        db: 数据库文件路径。

    Returns:
        `{表名: {"columns": [...], "indexes": [...], "fks": [...]}}`，键与列表均已排序，
        可直接用 `==` 比较。
    """
    with closing(sqlite3.connect(str(db))) as conn:
        return {
            table: {
                "columns": _columns(conn, table),
                "indexes": _indexes(conn, table),
                "fks": _foreign_keys(conn, table),
            }
            for table in _table_names(conn)
        }


# ---------------------------------------------------------------------------
# 非 SQLite 方言：走 SQLAlchemy inspector（Postgres 等）
#
# 快照的**形状与 SQLite 那份完全一致**（列/索引/外键三元组、同样的归一规则），
# 这样 `diff_snapshots()` 不需要为「换了个库」多写一份比较逻辑。
# ---------------------------------------------------------------------------


def _type_text(type_: Any, dialect: Any) -> str:
    """把列类型按**目标方言**编译成声明文本（`VARCHAR(32)` / `INTEGER` / `TEXT` …）。

    编译而不是取反射对象的类名：两侧都按同一个方言表达，`String(32)` 与反射出来的
    `VARCHAR(32)` 才会是同一个字符串，不必手写 `BLOB ↔ BYTEA` 这类跨方言映射。
    """
    try:
        return str(type_.compile(dialect=dialect))
    except Exception:  # noqa: BLE001 - 编不出来就退回类名，至少可比
        return str(type_)


def _inspector_columns(insp: Any, dialect: Any, table: str) -> list[tuple[Any, ...]]:
    columns = insp.get_columns(table)
    primary_key = insp.get_pk_constraint(table).get("constrained_columns") or []
    out: list[tuple[Any, ...]] = []
    for column in columns:
        name = column["name"]
        pk_position = primary_key.index(name) + 1 if name in primary_key else 0
        out.append(
            (
                name,
                _norm_type(table, name, _type_text(column["type"], dialect)),
                # 主键在语义上恒非空（与 SQLite 那份同一归一）
                1 if (not column["nullable"] or pk_position) else 0,
                _norm_default(column.get("default"), table, name),
                pk_position,
            )
        )
    return out


def _inspector_indexes(insp: Any, table: str) -> list[tuple[Any, ...]]:
    out: list[tuple[Any, ...]] = []
    for constraint in insp.get_unique_constraints(table):
        # 表级 / 列级 UNIQUE 约束：名字在不同库上不同（PG 是 `xxx_key`、SQLite 是
        # `sqlite_autoindex_*`），所以与 SQLite 那份一样用列集合代替名字。
        out.append(
            (("<unique-constraint>", tuple(constraint["column_names"])), 1, "")
        )
    for index in insp.get_indexes(table):
        # 唯一约束背后的索引已经按「约束」记过一遍，别再记第二遍
        if index.get("duplicates_constraint"):
            continue
        options = index.get("dialect_options") or {}
        where = ""
        for key, value in options.items():
            if key.endswith("_where") and value:
                where = _norm_where(value)
        out.append(
            (
                (index["name"], tuple(index["column_names"])),
                1 if index.get("unique") else 0,
                where,
            )
        )
    return sorted(out, key=lambda item: str(item[0]))


def _inspector_foreign_keys(insp: Any, table: str) -> list[tuple[Any, ...]]:
    out: list[tuple[Any, ...]] = []
    for fk in insp.get_foreign_keys(table):
        # 本项目没有复合外键（与 SQLite 那份的假设一致）
        source = (fk.get("constrained_columns") or [""])[0]
        target_table = fk.get("referred_table") or ""
        target = (fk.get("referred_columns") or [""])[0]
        on_delete = (fk.get("options") or {}).get("ondelete") or ""
        out.append((source, target_table, target, on_delete))
    return sorted(out, key=lambda item: str(item))


def inspector_snapshot(engine: Engine) -> dict[str, Any]:
    """用 SQLAlchemy inspector 读出**任意方言**库的语义结构快照。

    与 `schema_snapshot(path)` 的形状完全一致；SQLite 上也能用，但这里给非 SQLite
    用（SQLite 走 PRAGMA 那份，它能读出部分索引的 WHERE）。

    Args:
        engine: 已建好的 Engine（会借一条连接做反射）。

    Returns:
        同 `schema_snapshot`：`{表名: {"columns": [...], "indexes": [...], "fks": [...]}}`。
    """
    insp = inspect(engine)
    dialect = engine.dialect
    tables = [
        name
        for name in insp.get_table_names()
        if name not in _IGNORED_TABLES and not name.startswith(_SYSTEM_TABLE_PREFIXES)
    ]
    return {
        table: {
            "columns": _inspector_columns(insp, dialect, table),
            "indexes": _inspector_indexes(insp, table),
            "fks": _inspector_foreign_keys(insp, table),
        }
        for table in tables
    }


def metadata_snapshot(metadata: Any, dialect: Any) -> dict[str, Any]:
    """把**模型元数据**按目标方言编译成同一份语义快照（「期望形状」）。

    与 `_models_snapshot()`（在临时 SQLite 库上 `create_all` 再读）的产出等价，只是
    不建库：非 SQLite 方言上没法用「临时 SQLite 库」当期望值——类型名与默认值
    都会按各家的写法出现，两边根本对不上。

    Args:
        metadata: `SQLModel.metadata`（或任意 `MetaData`）。
        dialect: 目标方言（`engine.dialect`）。

    Returns:
        同 `schema_snapshot` 的形状，可直接与 `inspector_snapshot(engine)` 比较。
    """
    dialect_key = dialect.name
    out: dict[str, Any] = {}
    for table in metadata.tables.values():
        if table.name in _IGNORED_TABLES:
            continue
        columns: list[tuple[Any, ...]] = []
        for column in table.columns:
            pk_position = (
                list(table.primary_key.columns).index(column) + 1
                if column.primary_key
                else 0
            )
            default = getattr(getattr(column.server_default, "arg", None), "text", None)
            columns.append(
                (
                    column.name,
                    _norm_type(table.name, column.name, _type_text(column.type, dialect)),
                    1 if (not column.nullable or pk_position) else 0,
                    _norm_default(default, table.name, column.name),
                    pk_position,
                )
            )

        indexes: list[tuple[Any, ...]] = []
        for constraint in table.constraints:
            if not isinstance(constraint, UniqueConstraint):
                continue
            # 表级 UNIQUE 约束在不同库上名字不同（PG 是 `xxx_key`、SQLite 是
            # `sqlite_autoindex_*`），所以与 SQLite 那份一样用列集合代替名字；
            # 列**顺序**仍按声明顺序——PRAGMA 与 PG 反射出来都是声明顺序。
            indexes.append(
                (("<unique-constraint>", tuple(column.name for column in constraint.columns)),
                1,
                "",
            ))
        for index in table.indexes:
            options = index.dialect_options.get(dialect_key) or {}
            where = _norm_where(options.get("where"))
            indexes.append(
                (
                    (index.name, tuple(column.name for column in index.columns)),
                    1 if index.unique else 0,
                    where,
                )
            )
        indexes.sort(key=lambda item: str(item[0]))

        fks: list[tuple[Any, ...]] = []
        for fk in table.foreign_keys:
            fks.append(
                (fk.parent.name, fk.column.table.name, fk.column.name, fk.ondelete or "")
            )
        fks.sort(key=lambda item: str(item))

        out[table.name] = {"columns": columns, "indexes": indexes, "fks": fks}
    return out


def diff_snapshots(
    expected: dict[str, Any], actual: dict[str, Any]
) -> list[str]:
    """逐表逐项比较两份快照，返回人类可读的差异清单（空列表 = 完全一致）。

    不直接 `assert a == b`：那样失败时只会打印两个巨大的 dict，看不出差在哪一列。
    """
    problems: list[str] = []
    for table in sorted(set(expected) | set(actual)):
        if table not in actual:
            problems.append(f"缺表：{table}")
            continue
        if table not in expected:
            problems.append(f"多表：{table}")
            continue
        for section in ("columns", "indexes", "fks"):
            want = expected[table][section]
            got = actual[table][section]
            if section == "columns":
                want_map = {item[0]: item[1:] for item in want}
                got_map = {item[0]: item[1:] for item in got}
                for col in sorted(set(want_map) | set(got_map)):
                    if col not in got_map:
                        problems.append(f"{table}.{col}：模型缺这一列")
                    elif col not in want_map:
                        problems.append(f"{table}.{col}：模型多出这一列")
                    elif want_map[col] != got_map[col]:
                        problems.append(
                            f"{table}.{col}：期望 {want_map[col]}，实际 {got_map[col]}"
                        )
            else:
                if want != got:
                    problems.append(f"{table}.{section}：期望 {want}，实际 {got}")
    return problems
