"""结构快照：把 SQLite 库的 schema 归一成**可比较的语义结构**。

两个使用者：
- `core/schema.py::prepare_schema()`：对已有库 `stamp` 之前，校验它与基线是否一致
  （不一致就拒绝启动——直接 stamp 会把差异永久藏起来）；
- `tests/test_schema_parity.py`：证明「模型建出来的库」与「Alembic 基线建出来的库」一致。

## 为什么不能直接比较建表语句文本

同一个 schema 由不同方式建出来，`sqlite_master.sql` 会不一样：

- 空白与换行（手写 DDL 有缩进，ORM 生成时是单行）；
- 列的定义顺序（手写是业务顺序，ORM 是模型字段顺序——两者被要求一致，但不该由文本兜）；
- `NOT NULL` / `DEFAULT` 的渲染位置与引号风格（`'{}'` vs `'{}'`、`DEFAULT 0` vs `DEFAULT '0'`）；
- `AUTOINCREMENT` 是否出现（SQLModel 默认**不**生成，必须显式开 `sqlite_autoincrement`）。

文本 diff 会把这些无关差异报成「漂移」，真正的漂移（少一列、类型变了、索引丢了）反而
淹没在噪音里。所以这里比较的是 `PRAGMA` 读出来的**语义快照**：
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

# 布尔列白名单与 alembic 的 `compare_type`（`migrations/env.py`）**共用同一份**：
# 定义在 `core/models.py`，因为它描述的是「模型的事实」。
# 两处若各自维护一份，迟早会一边改了另一边没改，症状是 `alembic check` 时红时绿。
from backend.core.models import BOOLEAN_COLUMNS

# 类型名归一：SQLite 的类型亲和性（type affinity）里 BOOLEAN 与 INTEGER 都可存整数，
# 语义等价，只在声明名上不同。
_TYPE_ALIASES = {"BOOLEAN": "INTEGER"}

# 这些表不属于业务结构，不参与比较：
# - `sqlite_sequence`：`AUTOINCREMENT` 的副产物，有没有取决于「是否插入过行」，不是结构；
# - `alembic_version`：迁移工具自己的账本。
_IGNORED_TABLES = frozenset({"sqlite_sequence", "alembic_version"})


def _norm_type(table: str, column: str, declared: str | None) -> str:
    """归一列类型名。只对白名单里的布尔列做映射，其余原样（大写）。"""
    kind = (declared or "").strip().upper()
    if (table, column) in BOOLEAN_COLUMNS:
        return _TYPE_ALIASES.get(kind, kind)
    return kind


def _norm_default(declared: str | None) -> str | None:
    """归一默认值。`''` 与 `'{}'` 这类空串默认值在两种建表方式下引号风格会不同。"""
    if declared is None:
        return None
    text = str(declared).strip()
    # `DEFAULT ''` 与 `DEFAULT ""` 等价；统一成不带外层引号的裸形式再比
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1]
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
            _norm_default(r[4]),
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
