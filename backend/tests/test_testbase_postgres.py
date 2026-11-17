"""测试基座自身的门禁（票 #77「测试基座切到 Postgres」）。

## 这张票要证明什么

1. **全量用例跑在 Postgres 上**：`isolated_db` 把 `DATABASE_URL` 指到一个 Postgres
   库，`get_engine()` 拿到的必须是它——不是某个 SQLite 文件；
2. **用例之间数据互不可见**：每个用例一个**新建的库**（从本 worker 的模板库派生），
   模板库只有结构、没有数据，所以「上一个用例写的行」不可能出现在下一个用例里；
3. **不再靠拷库文件隔离**：测试基座里不许再有 `shutil.copyfile` / `DB_PATH`；
4. **没有 Postgres 时明确失败，不静默绿**：全量用例不允许 `skip`；连不上库时给出的
   文案必须能照着办（设 `PRDWRIGHT_TEST_POSTGRES_URL` 或起集成库）；
5. **连接泄漏守卫还在**：`isolated_db` 收尾仍以 `engine_stats()` 断「没有连接没归还」。

## 维护库地址的约定（⚠️ 别拼错）

`PRDWRIGHT_TEST_POSTGRES_URL` 给的是**维护库地址、不带库名**：

```
postgresql+psycopg://postgres:postgres@127.0.0.1:5433
```

测试自己往后面拼 `/<用例库名>` 去 `CREATE DATABASE` / `DROP DATABASE`。
**别把库名写进这个变量**：写进去之后每个用例都连同一个库，隔离就没了，而且
`CREATE DATABASE` 也不允许在事务块里跑。
"""

from __future__ import annotations

import pathlib
import re

import pytest

_TESTS_DIR = pathlib.Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# 一、全量用例都在 Postgres 上跑
# ---------------------------------------------------------------------------


def test_every_case_runs_on_postgres(isolated_db):  # noqa: ARG001 - 依赖 autouse 夹具定库
    """`get_engine()` 指向的必须是 Postgres，且是**本用例专属**的那个库。"""
    from backend.core.engine import get_engine
    from backend.core.schema import current_revision, head_revision

    engine = get_engine()
    backend = engine.url.get_backend_name()
    assert backend == "postgresql", (
        f"全量用例应当跑在 Postgres 上，实际连的是 {backend!r}"
        f"（{engine.url.render_as_string(hide_password=True)}）。"
        "若这是本地没起集成库，请设 PRDWRIGHT_TEST_POSTGRES_URL。"
    )
    # 库名带前缀且**每用例一个**（隔离的落地方式就是「换库」，不是换文件）。
    assert engine.url.database is not None
    assert engine.url.database.startswith("prdwright_test_"), (
        f"用例库应当是每用例新建的 prdwright_test_* 库，实际是 {engine.url.database!r}"
    )
    # 派生自模板库 → 结构已是 head（不是空壳，不需要本用例再迁一次）
    assert current_revision(engine) == head_revision()


def test_case_database_starts_empty(isolated_db):  # noqa: ARG001
    """用例从「只有结构的模板库」派生：开始跑时业务表一行都没有。"""
    from dbsupport import connect

    with connect() as conn:
        for table in (
            "users",
            "solutions",
            "documents",
            "document_versions",
            "generation_jobs",
            "chat_sessions",
        ):
            count = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
            assert count == 0, f"{table} 在用例开始时就有 {count} 行：上一个用例的数据渗过来了"


def test_template_database_has_structure_but_no_rows():
    """隔离的**根**：每 worker 的模板库只含结构，不含数据。

    模板库一旦带上数据（例如某个用例把行写进模板），所有从它派生的库都会看到同一批行。
    这条比「每个用例自己检查自己是空的」更接近机制本身。
    """
    from sqlalchemy import create_engine

    from backend.core.schema import current_revision, head_revision
    from dbsupport import template_database, maintenance_url

    name = template_database()
    engine = create_engine(f"{maintenance_url()}/{name}")
    try:
        assert current_revision(engine) == head_revision()
        with engine.connect() as conn:
            for table in ("users", "solutions", "generation_jobs"):
                count = conn.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar()
                assert count == 0, f"模板库 {table} 有 {count} 行：隔离的根被污染了"
    finally:
        engine.dispose()


def test_rows_written_in_one_case_database_are_invisible_in_a_fresh_one():
    """跨用例隔离的行为验证：往本用例库写一行，再派生一个新库，那边一行都没有。

    这是 AC「用例之间数据互不可见」的可执行判据——它复用的正是 `isolated_db`
    每用例建库的同一个机制（`dbsupport.database()`）。
    """
    from sqlalchemy import create_engine

    from dbsupport import connect, insert_job

    with connect() as conn:
        owner_id = conn.execute(
            "INSERT INTO users (username, password_hash, salt, created_at) "
            "VALUES (%s, 'h', %s, '2026-01-01T00:00:00') RETURNING id",
            ("neighbour-proof", b"s"),
        ).fetchone()["id"]
        conn.commit()

    insert_job(owner_id)

    from dbsupport import database

    with database() as fresh_url:
        fresh = create_engine(fresh_url)
        try:
            with fresh.connect() as conn:
                rows = conn.exec_driver_sql("SELECT count(*) FROM generation_jobs").scalar()
                assert rows == 0, "新库看得见别的用例写的行：隔离没生效"
        finally:
            fresh.dispose()


def test_case_database_names_are_unique():
    """每用例的库名必须唯一：并发跑时同名库会互相踩，而且 `CREATE DATABASE` 会报重复。"""
    from dbsupport import database_name

    names = {database_name() for _ in range(50)}
    assert len(names) == 50, "库名生成了重复值"


# ---------------------------------------------------------------------------
# 二、`dbsupport.connect()` 给出的是 psycopg 连接（签名不变，实现换掉）
# ---------------------------------------------------------------------------


def test_connect_yields_postgres_connection_with_named_and_positional_rows(isolated_db):  # noqa: ARG001
    """`connect()` 仍返回「底层 DBAPI 连接」，只是这次是 psycopg。

    两个取值形态都必须可用，因为既有用例两种都在用：
    - 按列名（`row["status"]` / `dict(row)`）；
    - 按位置（`fetchone()[0]`，数行、取第一列那种写法）。
    """
    from dbsupport import connect

    with connect() as conn:
        assert type(conn).__module__.startswith("psycopg"), (
            f"connect() 应当给出 psycopg 的底层连接，实际是 {type(conn)}"
        )
        row = conn.execute("SELECT 42 AS answer").fetchone()
        assert row["answer"] == 42, "按列名取值失效"
        assert row[0] == 42, "按位置取值失效（既有 `fetchone()[0]` 写法会全红）"
        assert dict(row) == {"answer": 42}, "`dict(row)` 失效"


def test_insert_job_writes_a_row_on_postgres(isolated_db):  # noqa: ARG001
    """`insert_job` 的列清单与占位符在 psycopg 上同样成立（唯一调用点是 `test_quota`）。"""
    from dbsupport import connect, insert_job

    with connect() as conn:
        owner_id = conn.execute(
            "INSERT INTO users (username, password_hash, salt, created_at) "
            "VALUES (%s, 'h', %s, '2026-01-01T00:00:00') RETURNING id",
            ("insert-job-owner", b"s"),
        ).fetchone()["id"]
        conn.commit()

    insert_job(owner_id, prompt_tokens=7, completion_tokens=3)

    with connect() as conn:
        row = conn.execute(
            "SELECT status, prompt_tokens, completion_tokens FROM generation_jobs "
            "WHERE owner_id = %s",
            (owner_id,),
        ).fetchone()
    assert row is not None
    assert row["status"] == "completed"
    assert (row["prompt_tokens"], row["completion_tokens"]) == (7, 3)


# ---------------------------------------------------------------------------
# 三、没有 Postgres 时明确失败，不静默绿
# ---------------------------------------------------------------------------


def test_missing_postgres_fails_loudly_with_actionable_message(monkeypatch):
    """连不上库时抛**带操作指引**的异常，而不是 `pytest.skip`。

    票面原话：没有 PG 时要「明确失败并给出可操作提示，绝不静默绿」。
    指向一个没人监听的端口来造「连不上」的现场。
    """
    from dbsupport import PostgresUnavailable, probe_postgres

    monkeypatch.setenv(
        "PRDWRIGHT_TEST_POSTGRES_URL",
        "postgresql+psycopg://postgres:postgres@127.0.0.1:1",
    )

    with pytest.raises(PostgresUnavailable) as caught:
        probe_postgres()

    message = str(caught.value)
    assert "PRDWRIGHT_TEST_POSTGRES_URL" in message, message
    assert "不带库名" in message, "必须说清「维护库地址、不带库名」这条约定"
    assert "5433" in message, "必须给出本地集成库的默认地址，否则读了也不知道该起在哪"


def test_suite_forbids_silent_skip():
    """全量用例不许 `skip`——静默绿比红更难发现。

    唯一的例外是「前端源码目录不存在」那 4 处跨端守卫：它们守的是另一侧的仓库
    （后端 job 里没有前端源码时才跳过），与数据库基座无关。
    """
    allowed_reason = "前端源码目录不存在"
    offenders: list[str] = []
    for path in sorted(_TESTS_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r"pytest\.skip\(|pytest\.mark\.skipif\(|pytest\.mark\.skip\(", source):
            line_no = source[: match.start()].count("\n") + 1
            line = source.splitlines()[line_no - 1]
            if allowed_reason in line or allowed_reason in _context(source, line_no, allowed_reason):
                continue
            offenders.append(f"{path.name}:{line_no}: {line.strip()}")
    assert offenders == [], "这些地方静默跳过了用例（本票之后全量用例必须全部执行）：\n  " + "\n  ".join(
        offenders
    )


def _context(source: str, line_no: int, needle: str, radius: int = 6) -> str:
    lines = source.splitlines()
    start = max(0, line_no - 1 - radius)
    return "\n".join(lines[start : line_no + radius])


# ---------------------------------------------------------------------------
# 四、基座里不再有「拷库文件」与连接泄漏守卫的倒退
# ---------------------------------------------------------------------------


def test_test_base_no_longer_copies_database_files():
    """AC：测试代码里不再出现「拷一份库文件」的隔离写法。"""
    conftest_source = (_TESTS_DIR / "conftest.py").read_text(encoding="utf-8")
    assert "shutil.copyfile" not in conftest_source, "conftest 还在拷库文件"
    assert 'setenv("DB_PATH"' not in conftest_source, "conftest 还在用 DB_PATH 造隔离"

    dbsupport_source = (_TESTS_DIR / "dbsupport.py").read_text(encoding="utf-8")
    assert "import sqlite3" not in dbsupport_source, "dbsupport 还在依赖 sqlite3"
    assert "sqlite3.Row" not in dbsupport_source, "row_factory 还指着 SQLite 的行"


def test_connection_leak_guard_is_still_asserted():
    """AC：连接泄漏守卫仍然生效。

    守卫写在 `isolated_db` 的收尾：它必须仍然读 `engine_stats()` 并断言
    `in_use == 0`。这条守的是「有人重写夹具时把守卫一起删掉」。
    """
    source = (_TESTS_DIR / "conftest.py").read_text(encoding="utf-8")
    assert "engine_stats()" in source, "收尾不再检查连接池状态"
    assert "in_use" in source, "收尾不再断言 in_use == 0"
