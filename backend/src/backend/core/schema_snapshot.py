"""结构快照：把库的 schema 归一成**可比较的语义结构**（v9 / 单后端）。

两个使用者：
- `core/schema.py::prepare_schema()`：对已有库 `stamp` 之前，校验它与基线是否一致
  （不一致就拒绝启动——直接 stamp 会把差异永久藏起来）；
- `tests/test_schema_parity.py`：证明「模型建出来的库」与「Alembic 基线建出来的库」一致。

## 一份快照、一种读法

v9 起只有 Postgres，所以这里只保留 **SQLAlchemy inspector 反射**这一条读法
（`inspector_snapshot(engine)`，Postgres 上就是 `pg_catalog` 的反射）。「期望形状」由
`metadata_snapshot(metadata, dialect)` 按**目标方言编译**模型元数据得到。
这样比较的两边都是「目标库的语言」，不需要跨方言猜 `BLOB ↔ BYTEA` 之类的对应关系。

⚠️ #78 删掉了 SQLite 专用实现（`sqlite_master` / `PRAGMA table_info` / `index_list` /
`foreign_key_list`，以及只能从 `sqlite_master.sql` 抠出来的部分索引 WHERE）。那份实现
的存在理由是「SQLite 读结构要绕开反射」，随 SQLite 后端一起退场；快照的形状没变，
`diff_snapshots()` 不需要跟着改。

## 为什么不能直接比较建表语句文本

同一个 schema 由不同方式建出来，DDL 文本会不一样：

- 空白与换行（手写 DDL 有缩进，ORM 生成时是单行）；
- 列的定义顺序（手写是业务顺序，ORM 是模型字段顺序——两者被要求一致，但不该由文本兜）；
- `NOT NULL` / `DEFAULT` 的渲染位置与引号风格。

文本 diff 会把这些无关差异报成「漂移」，真正的漂移（少一列、类型变了、索引丢了）反而
淹没在噪音里。所以这里比较的是语义快照：
列（名/类型/非空/默认值/主键位）、索引（名/唯一/列/部分索引条件）、外键（来源列/目标表列/级联规则）。

## 归一规则只服务于「同一结构的不同渲染」

Postgres 反射出来的声明与模型按同一方言编译出来的声明，在下面几处会差一层壳，
这里统一剥掉：`::type` 转换后缀、自增序列默认值（`nextval(...)`）、外层括号、
字面量外层引号、部分索引条件的括号与空白。这些都**不改变语义**；不归一就会把
「同一列」判成漂移，进而让 `alembic check` 产生幻影迁移。
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import UniqueConstraint, inspect
from sqlalchemy.engine import Engine

# 这些表不属于业务结构，不参与比较：`alembic_version` 是迁移工具自己的账本。
_IGNORED_TABLES = frozenset({"alembic_version"})

# inspector 偶尔会把系统表也反射出来，同样不参与比较。
_SYSTEM_TABLE_PREFIXES = ("pg_", "sql_")


def _norm_type(declared: str | None) -> str:
    """归一列类型名：只做大小写与空白归一，不做跨方言映射。"""
    return (declared or "").strip().upper()


def _norm_default(declared: str | None) -> str | None:
    """归一默认值，剥掉「不是字面量本身」的渲染外壳。

    Postgres 的 inspector 会把：

    - **类型转换后缀**加回去：`DEFAULT ''` 反射成 `''::text`、
      `DEFAULT -1` 反射成 `'-1'::integer`——去掉 `::type` 才是那个值；
    - **自增序列**写成默认值：自增主键反射成 `nextval('…'::regclass)`。这不是列默认值
      （模型侧对应 `SERIAL`，反射方按「无默认值」记），两边都归一成「没有默认值」。
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
    return text


def _norm_where(condition: Any) -> str:
    """归一索引的部分条件（WHERE）：去外层括号、压空白、去尾；空条件返回空串。

    Postgres 反射出来会带一层括号（`(job_id IS NOT NULL)`），模型侧给的是
    `TextClause`。两边都归一成裸条件。

    入参可能是 `TextClause`（`postgresql_where=text("…")`）也可能是 inspector
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


def _type_text(type_: Any, dialect: Any) -> str:
    """把列类型按**目标方言**编译成声明文本（`VARCHAR(32)` / `INTEGER` / `TEXT` …）。

    编译而不是取反射对象的类名：两侧都按同一个方言表达，`String(32)` 与反射出来的
    `VARCHAR(32)` 才会是同一个字符串，不必手写 `BYTEA ↔ LargeBinary` 这类跨方言映射。
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
                _norm_type(_type_text(column["type"], dialect)),
                # 主键在语义上恒非空（部分实现反射出来 nullable=True）
                1 if (not column["nullable"] or pk_position) else 0,
                _norm_default(column.get("default")),
                pk_position,
            )
        )
    return out


def _inspector_indexes(insp: Any, table: str) -> list[tuple[Any, ...]]:
    out: list[tuple[Any, ...]] = []
    for constraint in insp.get_unique_constraints(table):
        # 表级 / 列级 UNIQUE 约束：名字在不同实现上不同（PG 是 `xxx_key`），
        # 所以用列集合代替名字，避免假阳性。
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
        # 本项目没有复合外键
        source = (fk.get("constrained_columns") or [""])[0]
        target_table = fk.get("referred_table") or ""
        target = (fk.get("referred_columns") or [""])[0]
        on_delete = (fk.get("options") or {}).get("ondelete") or ""
        out.append((source, target_table, target, on_delete))
    return sorted(out, key=lambda item: str(item))


def inspector_snapshot(engine: Engine) -> dict[str, Any]:
    """用 SQLAlchemy inspector 读出库的语义结构快照。

    Args:
        engine: 已建好的 Engine（会借一条连接做反射）。

    Returns:
        `{表名: {"columns": [...], "indexes": [...], "fks": [...]}}`，键与列表均已排序，
        可直接用 `==` 比较。
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

    不建临时库：类型名与默认值都要按目标方言表达，这样比较的两边是同一个语言。

    Args:
        metadata: `SQLModel.metadata`（或任意 `MetaData`）。
        dialect: 目标方言（`engine.dialect`）。

    Returns:
        同 `inspector_snapshot` 的形状，可直接与它比较。
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
                    _norm_type(_type_text(column.type, dialect)),
                    1 if (not column.nullable or pk_position) else 0,
                    _norm_default(default),
                    pk_position,
                )
            )

        indexes: list[tuple[Any, ...]] = []
        for constraint in table.constraints:
            if not isinstance(constraint, UniqueConstraint):
                continue
            # 表级 UNIQUE 约束在不同实现上名字不同（PG 是 `xxx_key`），
            # 所以与反射那份一样用列集合代替名字；列**顺序**仍按声明顺序。
            indexes.append(
                (("<unique-constraint>", tuple(column.name for column in constraint.columns)),
                 1,
                 "",
                )
            )
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
