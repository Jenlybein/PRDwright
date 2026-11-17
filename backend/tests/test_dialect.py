"""方言适配层的分支正确性（v9，单一后端）。

这里**不连数据库**，只把 helper 生成的语句按目标方言编译一遍，断言它落在哪条分支上。
理由：行为正确性已经被仓储契约用例覆盖（全量跑通即证明），但「分支有没有写错」
靠行为测不出来——PG 上的正确行为不会告诉你「别的方言悄悄走了另一条分支」。

三条断言各自守一个具体的坑（#78 收缩到 Postgres 后仍然成立）：

1. Postgres 的 upsert 必须带**冲突目标**（`ON CONFLICT (cols)`）。不带目标的
   `DO NOTHING` 语义更宽，会把别的唯一约束错误一起吞掉；
2. `INSERT ... SELECT` 形状必须保留——`document_repo._ensure_document` 把归属校验
   写进 SELECT 的 WHERE，靠它做到「不属于该用户就一行都插不进去」；
3. 未适配的方言必须**当场报错**，不能回落到某个「看起来差不多」的实现——那样生成的
   是一条在目标库上语法错误的 SQL，报错会指向数据库而不是这里。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import literal, select
from sqlalchemy.dialects import postgresql

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
# PostgreSQL：唯一分支
# ---------------------------------------------------------------------------


def test_postgresql_update_carries_conflict_target():
    """冲突目标必须在 SQL 里，不能是裸的 `ON CONFLICT`。

    不带目标的 `DO NOTHING` 语义比调用方以为的宽，会把别的唯一约束错误一起吞掉。
    """
    sql = _compile("postgresql", postgresql.dialect())
    assert "ON CONFLICT (key) DO UPDATE" in sql
    assert "excluded" in sql


def test_postgresql_do_nothing_carries_conflict_target():
    """DO NOTHING 同样要带目标——否则会吞掉非目标约束的冲突。"""
    sql = _compile_do_nothing("postgresql", postgresql.dialect())
    assert "ON CONFLICT (key) DO NOTHING" in sql


def test_unknown_dialect_is_rejected():
    """未适配的方言不许静默回落。

    这是「单一后端」的另一半：不是「PG 能用」就够了，而是「别的库发不出语句」。
    回落的症状是生成一条在目标库上语法错误的 SQL，报错指向数据库而不是这里，
    下一个人会以为「库坏了」。
    """
    with pytest.raises(NotImplementedError) as caught:
        _compile("sqlite", SimpleNamespace())
    assert "sqlite" in str(caught.value)


# ---------------------------------------------------------------------------
# INSERT ... SELECT 形态
# ---------------------------------------------------------------------------


def test_upsert_select_keeps_insert_select_shape():
    """归属校验写在 SELECT 的 WHERE 里，这个形状不能被打散。"""
    session = _FakeSession("postgresql")
    upsert_select(
        session,  # type: ignore[arg-type]
        Document,
        ["solution_id", "kind", "created_at", "updated_at"],
        select(Solution.id, literal("prd"), literal("t"), literal("t")).where(
            Solution.id == 1
        ),
        conflict_cols=["solution_id", "kind"],
    )
    sql = str(session.statements[0].compile(dialect=postgresql.dialect()))
    assert "INSERT INTO documents" in sql
    assert "SELECT" in sql
    assert "WHERE solutions.id =" in sql
    assert "ON CONFLICT (solution_id, kind) DO NOTHING" in sql


# ---------------------------------------------------------------------------
# 连接级写探针（/readyz 用）：真写一次的语法
#
# 就绪探针要把「库可写」这条判成真的，就必须真写一次再回滚。Postgres 的 DDL 可回滚，
# 所以建一张临时表就是「真写一次」：只读实例 / 只读事务会当场报错。
# ---------------------------------------------------------------------------


class _FakeProbeResult:
    """只够读一次标量（探针不会真读，留一个可调用的形状）。"""

    def __init__(self, value: Any = 0) -> None:
        self._value = value

    def scalar(self) -> Any:
        return self._value


class _FakeProbeConnection:
    """假连接：记下探针发了哪些语句，不连库。"""

    def __init__(self, name: str) -> None:
        self.dialect = SimpleNamespace(name=name)
        self.statements: list[str] = []

    def exec_driver_sql(self, statement: str, parameters: Any = None) -> _FakeProbeResult:
        self.statements.append(statement)
        return _FakeProbeResult(0)


def test_write_probe_on_postgres_does_a_real_write():
    """写探针必须是**该方言真能执行**的那一条。

    判据是独立事实，不是「跟实现一样」：Postgres 没有 `PRAGMA` / `BEGIN IMMEDIATE`，
    所以它们出现在 PG 探针里就是错的，不管实现怎么组织。
    """
    from backend.core.dialect import write_probe

    connection = _FakeProbeConnection("postgresql")
    write_probe(connection)
    joined = "\n".join(connection.statements)
    assert "create temp table" in joined.lower()
    for needle in ("BEGIN IMMEDIATE", "PRAGMA", "sql_mode"):
        assert needle.lower() not in joined.lower(), f"探针混进了 {needle}：{connection.statements}"


@pytest.mark.parametrize("backend", ["sqlite", "mysql", "mariadb"])
def test_write_probe_rejects_unadapted_dialects(backend):
    """未适配方言的探针必须报错，而不是发一句在目标库上语法不通的 SQL。

    后者会把「没适配」伪装成「库坏了」：/readyz 一直 503，看起来像数据库故障。
    """
    from backend.core.dialect import write_probe

    with pytest.raises(NotImplementedError) as caught:
        write_probe(_FakeProbeConnection(backend))
    assert backend in str(caught.value)
