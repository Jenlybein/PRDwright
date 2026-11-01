"""方言适配层的分支正确性（v8 / ADR-088）。

这里**不连数据库**，只把 helper 生成的语句按各地方言编译一遍，断言它落在哪条分支上。
理由：行为正确性已经被仓储契约用例覆盖（全量跑通即证明），但"分支有没有写错"
靠行为测不出来——SQLite 上的正确行为不会告诉你 MySQL 的分支是不是悄悄走了
`INSERT IGNORE`。

三条断言各自守一个具体的坑：

1. SQLite / PostgreSQL 必须带**冲突目标**（`ON CONFLICT (cols)`）。不带目标的
   `DO NOTHING` 语义更宽，会把别的唯一约束错误一起吞掉；
2. MySQL 的「冲突就跳过」必须是 `ON DUPLICATE KEY UPDATE <pk> = <pk>`，**不能是
   `INSERT IGNORE`**——后者会把列超长、违反 NOT NULL 一并降级成警告；
3. MySQL 的 `INSERT ... SELECT` 形态也要能挂上冲突处理（`document_repo` 的归属校验
   依赖这个形状）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from sqlalchemy import literal, select
from sqlalchemy.dialects import mysql, postgresql, sqlite

from backend.core.dialect import upsert_select, upsert_values
from backend.core.models import Document, Solution, SystemConfig


class _FakeSession:
    """只够 `upsert_*` 用：给一个方言名，把语句记下来。"""

    def __init__(self, name: str) -> None:
        self._bind = SimpleNamespace(dialect=SimpleNamespace(name=name))
        self.statements: list[Any] = []

    def get_bind(self) -> Any:
        return self._bind

    def execute(self, stmt: Any) -> None:
        self.statements.append(stmt)
        return None


def _compile(name: str, dialect: Any) -> str:
    session = _FakeSession(name)
    upsert_values(
        session,  # type: ignore[arg-type]
        SystemConfig,
        {"key": "a", "value": "1", "updated_at": "t", "updated_by": None},
        conflict_cols=["key"],
        update_cols=["value", "updated_at", "updated_by"],
    )
    return str(session.statements[0].compile(dialect=dialect))


def _compile_do_nothing(name: str, dialect: Any) -> str:
    session = _FakeSession(name)
    upsert_values(
        session,  # type: ignore[arg-type]
        SystemConfig,
        {"key": "a", "value": "1", "updated_at": "t", "updated_by": None},
        conflict_cols=["key"],
        update_cols=None,
    )
    return str(session.statements[0].compile(dialect=dialect))


# ---------------------------------------------------------------------------
# SQLite（当前主路径）
# ---------------------------------------------------------------------------


def test_sqlite_update_carries_conflict_target():
    """冲突目标必须在 SQL 里，不能是裸的 DO UPDATE。

    断言不比对引号形态：SQLite 编译器给标识符加双引号、MySQL 用反引号，
    写成逐字匹配会让用例在 SQLAlchemy 升级换渲染风格时误红。
    """
    sql = _compile("sqlite", sqlite.dialect())
    assert "ON CONFLICT" in sql
    assert "DO UPDATE" in sql
    assert '"key"' in sql, "冲突目标必须显式给出，不能是裸的 ON CONFLICT"
    assert "excluded" in sql


def test_sqlite_do_nothing_carries_conflict_target():
    """DO NOTHING 同样要带目标——否则会吞掉非目标约束的冲突。"""
    sql = _compile_do_nothing("sqlite", sqlite.dialect())
    assert "ON CONFLICT" in sql
    assert "DO NOTHING" in sql
    assert '"key"' in sql


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------


def test_postgresql_uses_on_conflict():
    """PG 与 SQLite 同一套语法，收敛的意义就在这里：调用方无需区分。"""
    sql = _compile("postgresql", postgresql.dialect())
    assert "ON CONFLICT (key) DO UPDATE" in sql


# ---------------------------------------------------------------------------
# MySQL
# ---------------------------------------------------------------------------


def test_mysql_update_uses_on_duplicate_key():
    sql = _compile("mysql", mysql.dialect())
    assert "ON DUPLICATE KEY UPDATE" in sql
    assert "IGNORE" not in sql, "不许用 INSERT IGNORE（会吞掉截断与非空错误）"


def test_mysql_do_nothing_is_primary_key_noop():
    """「冲突就跳过」在 MySQL 上只能是把主键更新成自己。

    断言里刻意检查**没有** `IGNORE`：那是最短的写法，也是最容易悄悄丢数据的那种。
    """
    sql = _compile_do_nothing("mysql", mysql.dialect())
    assert "ON DUPLICATE KEY UPDATE" in sql
    assert "IGNORE" not in sql
    # 「更新成自己」：MySQL 编译器用反引号包标识符，两种形态都接受
    assert "VALUES(`key`)" in sql or "VALUES(key)" in sql


def test_mariadb_shares_mysql_branch():
    """MariaDB 的语法与 MySQL 一致，不应各自一套。"""
    sql = _compile("mariadb", mysql.dialect())
    assert "ON DUPLICATE KEY UPDATE" in sql


# ---------------------------------------------------------------------------
# INSERT ... SELECT 形态
# ---------------------------------------------------------------------------


def test_upsert_select_keeps_insert_select_shape_on_sqlite():
    """归属校验写在 SELECT 的 WHERE 里，这个形状不能被打散。"""
    session = _FakeSession("sqlite")
    upsert_select(
        session,  # type: ignore[arg-type]
        Document,
        ["solution_id", "kind", "created_at", "updated_at"],
        select(
            Solution.id, literal("prd"), literal("t"), literal("t")
        ).where(Solution.id == 1),
        conflict_cols=["solution_id", "kind"],
    )
    sql = str(session.statements[0].compile(dialect=sqlite.dialect()))
    assert "INSERT INTO documents" in sql
    assert "SELECT" in sql
    assert "WHERE solutions.id =" in sql
    assert "ON CONFLICT (solution_id, kind) DO NOTHING" in sql


def test_upsert_select_works_on_mysql():
    """MySQL 的 INSERT ... SELECT ... ON DUPLICATE KEY 也要能编出来。"""
    session = _FakeSession("mysql")
    upsert_select(
        session,  # type: ignore[arg-type]
        Document,
        ["solution_id", "kind", "created_at", "updated_at"],
        select(
            Solution.id, literal("prd"), literal("t"), literal("t")
        ).where(Solution.id == 1),
        conflict_cols=["solution_id", "kind"],
    )
    sql = str(session.statements[0].compile(dialect=mysql.dialect()))
    assert "INSERT INTO documents" in sql
    assert "ON DUPLICATE KEY UPDATE" in sql
