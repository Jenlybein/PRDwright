"""迁移地基的用例。

迁移是「只跑一次、跑错了很难回头」的动作，所以这里盯三件事：
清单本身合法、跑过的步骤不会重跑、跑挂的步骤不留下半个结构。

用例不碰 `MIGRATIONS` 的真实内容——那属于各阶段自己的测试。这里只验证**机制**，
因此每次都用 `monkeypatch` 换上一份临时的迁移清单。
"""

from __future__ import annotations

import sqlite3

import pytest

from backend.core.db import connect, utc_now_text
from backend.core.migrations import Migration, current_version, run_migrations


def _table_exists(name: str) -> bool:
    """库里是否存在名为 `name` 的表。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
    return row is not None


def _version() -> int:
    """直接读库里的 schema 版本。"""
    with connect() as conn:
        return current_version(conn)


def _step(
    version: int,
    *,
    calls: list[int],
    creates: str | None = None,
    boom: bool = False,
) -> Migration:
    """造一步迁移：记录自己被调用、可选建一张表、可选抛异常。"""

    def apply(conn: sqlite3.Connection) -> None:
        calls.append(version)
        if creates:
            conn.execute(f"CREATE TABLE {creates} (id INTEGER PRIMARY KEY)")
        if boom:
            raise RuntimeError("这一步故意炸了")

    return Migration(version=version, name=f"step-{version}", apply=apply)


def test_fresh_db_applies_every_step(monkeypatch) -> None:
    """空库（版本 0）从第 1 步一路跑到最后，版本号与结构都到位。"""
    calls: list[int] = []
    monkeypatch.setattr(
        "backend.core.migrations.MIGRATIONS",
        [
            _step(1, calls=calls, creates="alpha"),
            _step(2, calls=calls, creates="beta"),
        ],
    )

    assert _version() == 0
    assert run_migrations() == 2

    assert calls == [1, 2]
    assert _version() == 2
    assert _table_exists("alpha")
    assert _table_exists("beta")


def test_second_run_does_not_repeat_steps(monkeypatch) -> None:
    """重启再跑一次，已应用的步骤不会重跑（幂等）。"""
    calls: list[int] = []
    monkeypatch.setattr(
        "backend.core.migrations.MIGRATIONS",
        [
            _step(1, calls=calls, creates="alpha"),
            _step(2, calls=calls, creates="beta"),
        ],
    )

    run_migrations()
    assert run_migrations() == 2

    # 两次调用加起来，每一步仍然只被执行过一次
    assert calls == [1, 2]


def test_only_runs_the_missing_tail(monkeypatch) -> None:
    """库停在第 1 步时，只补第 2 步。"""
    calls: list[int] = []
    monkeypatch.setattr(
        "backend.core.migrations.MIGRATIONS",
        [
            _step(1, calls=calls, creates="alpha"),
            _step(2, calls=calls, creates="beta"),
            _step(3, calls=calls, creates="gamma"),
        ],
    )

    # 先把库推到第 1 步
    monkeypatch.setattr(
        "backend.core.migrations.MIGRATIONS",
        [_step(1, calls=calls, creates="alpha")],
    )
    run_migrations()
    assert calls == [1]

    # 换回完整清单：应当只跑 2、3
    calls.clear()
    monkeypatch.setattr(
        "backend.core.migrations.MIGRATIONS",
        [
            _step(1, calls=calls, creates="alpha"),
            _step(2, calls=calls, creates="beta"),
            _step(3, calls=calls, creates="gamma"),
        ],
    )
    assert run_migrations() == 3
    assert calls == [2, 3]


def test_failed_step_leaves_no_partial_structure(monkeypatch) -> None:
    """某一步失败：版本停在上一档，且它建了一半的东西被回滚掉。

    这条是关键——如果失败步骤的 DDL 留在库里，重跑会撞上「表已存在」，
    把一个能自愈的失败变成长久打不开的死结。
    """
    calls: list[int] = []
    monkeypatch.setattr(
        "backend.core.migrations.MIGRATIONS",
        [
            _step(1, calls=calls, creates="alpha"),
            # 先建表再抛异常，模拟「跑到一半失败」
            _step(2, calls=calls, creates="beta", boom=True),
        ],
    )

    with pytest.raises(RuntimeError, match="故意炸了"):
        run_migrations()

    assert _version() == 1
    assert _table_exists("alpha")
    assert not _table_exists("beta")


def test_resumes_from_breakpoint_after_fix(monkeypatch) -> None:
    """修好失败的那一步后重跑，从断点继续，且不重复已成功的步骤。"""
    calls: list[int] = []
    broken = [
        _step(1, calls=calls, creates="alpha"),
        _step(2, calls=calls, creates="beta", boom=True),
    ]
    monkeypatch.setattr("backend.core.migrations.MIGRATIONS", broken)
    with pytest.raises(RuntimeError):
        run_migrations()

    calls.clear()
    fixed = [
        _step(1, calls=calls, creates="alpha"),
        _step(2, calls=calls, creates="beta"),
    ]
    monkeypatch.setattr("backend.core.migrations.MIGRATIONS", fixed)

    assert run_migrations() == 2
    assert calls == [2]  # 第 1 步不再重跑
    assert _table_exists("beta")


def test_rejects_non_consecutive_plan(monkeypatch) -> None:
    """版本号必须从 1 开始连续——不连续是写错了，要在启动时立刻炸出来。"""
    calls: list[int] = []
    monkeypatch.setattr(
        "backend.core.migrations.MIGRATIONS",
        [_step(1, calls=calls), _step(3, calls=calls)],
    )

    with pytest.raises(ValueError, match="连续递增"):
        run_migrations()
    # 校验失败时一步都不该执行
    assert calls == []


def test_newer_db_is_left_untouched(monkeypatch) -> None:
    """库版本高于本代码已知的迁移数时，不做任何迁移（不擅自降级）。"""
    calls: list[int] = []
    monkeypatch.setattr(
        "backend.core.migrations.MIGRATIONS", [_step(1, calls=calls, creates="alpha")]
    )
    run_migrations()

    with connect() as conn:
        conn.execute("PRAGMA user_version = 99")
        conn.commit()

    calls.clear()
    assert run_migrations() == 99
    assert calls == []


def test_empty_plan_keeps_baseline(monkeypatch) -> None:
    """还没有任何迁移时，跑一次是无害的空操作。"""
    monkeypatch.setattr("backend.core.migrations.MIGRATIONS", [])
    assert run_migrations() == 0
    assert _version() == 0


def _table_info(name: str) -> list[dict[str, Any]]:
    """读取某张表的列信息（`PRAGMA table_info`）。"""
    with connect() as conn:
        rows = conn.execute(f"PRAGMA table_info({name})").fetchall()
    return [dict(row) for row in rows]


def test_v3_migration_adds_pending_items_column_to_jobs() -> None:
    """真实 v3 迁移给 `generation_jobs` 加 `pending_items` 列（NOT NULL，默认 '[]'）。

    这是 P3-b 的全部落点依赖：刷新重连时待确认清单要从任务记录里一并恢复，没有这一列
    `persist_progress` / `finish` / `fail` 的写入会直接报「no such column」。这里卡的是
    「结构真的加进去了、且约束正确」。

    基线用的是 `init_schema` 建出的表——`pending_items` 这一列只走迁移、不进 `init_schema`
    的 `SCHEMA_SQL`（P0 约定：新列一律进迁移，避免新旧库两处各建一次结构对不上），所以
    跑迁移前它不存在。
    """
    from backend.core.migrations import run_migrations

    # 基线：只有 init_schema 的表，尚无该列
    assert "pending_items" not in {row["name"] for row in _table_info("generation_jobs")}

    # 跑真实迁移清单（含 v3），最终版本应为 7（v4~v7 与本步无关）
    assert run_migrations() == 8

    cols = {row["name"]: row for row in _table_info("generation_jobs")}
    assert "pending_items" in cols
    # NOT NULL：零值语义是「空清单」而不是 NULL，读取侧据此免于空判断
    assert cols["pending_items"]["notnull"] == 1

    # 默认值真的生效：关掉外键插一行，不显式给 pending_items 也应当是 '[]'
    with connect() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "INSERT INTO generation_jobs "
            "(owner_id, kind, solution_id, status, phase, draft_content, "
            " completed_sections, created_at, updated_at) "
            "VALUES (1, 'prd', NULL, 'running', '', '', '[]', 't', 't')"
        )
        raw = conn.execute(
            "SELECT pending_items FROM generation_jobs WHERE owner_id = 1"
        ).fetchone()["pending_items"]
    assert raw == "[]"


# ---------------------------------------------------------------------------
# v4~v6：文档版本链（documents 退化为「身份 + 当前版本指针」，正文搬进版本表）
# ---------------------------------------------------------------------------


def _seed_solution_with_document(
    kind: str = "prd",
    content: str = "# 旧正文",
    *,
    content_sha: str = "sha000000000",
    last_run_summary: str | None = '{"kind": "prd"}',
) -> int:
    """在「documents 仍带正文列」的旧库形态下，造一个用户 + 方案 + 一行文档。

    前提：调用前库里已跑过 v1~v3（`documents` 表已存在且带正文列）。返回 `solution_id`。
    """
    now = utc_now_text()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, salt, created_at) VALUES (?, ?, ?, ?)",
            ("legacy", "hash", b"salt", now),
        )
        user_id = int(cur.lastrowid or 0)
        cur = conn.execute(
            "INSERT INTO solutions (user_id, title, mode, stage, data, created_at, updated_at) "
            "VALUES (?, '', '', '', '{}', ?, ?)",
            (user_id, now, now),
        )
        solution_id = int(cur.lastrowid or 0)
        conn.execute(
            "INSERT INTO documents "
            "(solution_id, kind, content, content_sha, pending_items, "
            " last_run_summary, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, '[]', ?, ?, ?)",
            (solution_id, kind, content, content_sha, last_run_summary, now, now),
        )
        conn.commit()
    return solution_id


def _columns(name: str) -> set[str]:
    """某张表的列名集合。"""
    return {row["name"] for row in _table_info(name)}


def test_v4_v6_build_version_chain_and_backfill(monkeypatch) -> None:
    """真实 v4~v6：建版本表、旧文档行回填成版本 1、原表四列被删、指针已接。

    这是「正文全搬进版本表」这条单向门的核心——跑完之后 `documents` 不再有正文列，
    正文只能从版本表读；旧行必须原样搬成 `version_no=1, source='migrated'`。
    """
    from backend.core.migrations import MIGRATIONS

    # 先把库推到 v3：documents 仍是「带正文列」的旧形态
    monkeypatch.setattr("backend.core.migrations.MIGRATIONS", MIGRATIONS[:3])
    assert run_migrations() == 3
    solution_id = _seed_solution_with_document()

    # 再跑完整清单：v4（建表 + 加指针）、v5（回填）、v6（删列）
    monkeypatch.setattr("backend.core.migrations.MIGRATIONS", MIGRATIONS)
    assert run_migrations() == 8

    assert _table_exists("document_versions")
    cols = _columns("documents")
    assert {"content", "content_sha", "pending_items", "last_run_summary"}.isdisjoint(cols)
    assert "current_version_id" in cols

    with connect() as conn:
        row = conn.execute(
            "SELECT d.current_version_id, v.version_no, v.content, v.content_sha, "
            "       v.source, v.last_run_summary "
            "FROM documents d JOIN document_versions v ON v.id = d.current_version_id "
            "WHERE d.solution_id = ?",
            (solution_id,),
        ).fetchone()
    assert row is not None
    assert row["version_no"] == 1
    assert row["content"] == "# 旧正文"
    # 原样搬运：指纹与回执都不重算、不改形状
    assert row["content_sha"] == "sha000000000"
    assert row["last_run_summary"] == '{"kind": "prd"}'
    assert row["source"] == "migrated"


def test_v4_v6_is_idempotent(monkeypatch) -> None:
    """版本链迁移跑第二次是空操作：不重复建版本、不报错。"""
    from backend.core.migrations import MIGRATIONS

    monkeypatch.setattr("backend.core.migrations.MIGRATIONS", MIGRATIONS[:3])
    run_migrations()
    solution_id = _seed_solution_with_document()

    monkeypatch.setattr("backend.core.migrations.MIGRATIONS", MIGRATIONS)
    assert run_migrations() == 8
    assert run_migrations() == 8

    with connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM document_versions v "
            "JOIN documents d ON d.id = v.document_id WHERE d.solution_id = ?",
            (solution_id,),
        ).fetchone()[0]
    assert count == 1


def test_v4_v6_cascade_clears_versions(monkeypatch) -> None:
    """删方案要连带清掉 documents 与该文档的全部版本，不留孤儿、不报错。

    环形外键（documents ↔ document_versions）在这里最容易出问题，必须钉死。
    """
    from backend.core.migrations import MIGRATIONS

    monkeypatch.setattr("backend.core.migrations.MIGRATIONS", MIGRATIONS[:3])
    run_migrations()
    solution_id = _seed_solution_with_document()

    monkeypatch.setattr("backend.core.migrations.MIGRATIONS", MIGRATIONS)
    run_migrations()

    with connect() as conn:
        conn.execute("DELETE FROM solutions WHERE id = ?", (solution_id,))
        conn.commit()
        docs = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE solution_id = ?", (solution_id,)
        ).fetchone()[0]
        versions = conn.execute("SELECT COUNT(*) FROM document_versions").fetchone()[0]
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    assert docs == 0
    assert versions == 0
    assert violations == []


# ---------------------------------------------------------------------------
# v7：solutions 加 `content_updated_at`（把「内容变更」与「写库」分成两根时间轴）
# ---------------------------------------------------------------------------


def _seed_legacy_solution(username: str, now: str) -> int:
    """在「还没有 `content_updated_at` 列」的旧库形态下造一份方案，返回其 id。

    前提：调用前库里已跑过 v1~v6（`solutions` 表存在、且**没有**新列）。
    """
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, salt, created_at) VALUES (?, ?, ?, ?)",
            (username, "hash", b"salt", now),
        )
        user_id = int(cur.lastrowid or 0)
        cur = conn.execute(
            "INSERT INTO solutions (user_id, title, mode, stage, data, created_at, updated_at) "
            "VALUES (?, '旧方案', 'structured', 'review-prd', '{}', ?, ?)",
            (user_id, now, now),
        )
        solution_id = int(cur.lastrowid or 0)
        conn.commit()
    return solution_id


def test_v7_adds_content_updated_at_and_backfills(monkeypatch) -> None:
    """真实 v7：加列、旧行回填成 `updated_at`、并建好排序用的复合索引。

    回填口径必须是 `updated_at` 而**不是**「迁移时刻」：后者会把所有旧方案并列成同一个
    时间，列表的相对顺序在一夜之间被重排（用户会以为所有方案的进度都重置了）。
    这里用两条时间不同的旧行把这件事钉死。
    """
    from backend.core.migrations import MIGRATIONS

    monkeypatch.setattr("backend.core.migrations.MIGRATIONS", MIGRATIONS[:6])
    assert run_migrations() == 6
    # 迁移前这一列不该存在（新列一律走迁移，不进 init_schema 的基线 SQL）
    assert "content_updated_at" not in _columns("solutions")

    older = _seed_legacy_solution("legacy-old", "2026-01-01T00:00:00+00:00")
    newer = _seed_legacy_solution("legacy-new", "2026-02-02T00:00:00+00:00")

    monkeypatch.setattr("backend.core.migrations.MIGRATIONS", MIGRATIONS)
    assert run_migrations() == 8

    cols = {row["name"]: row for row in _table_info("solutions")}
    assert "content_updated_at" in cols
    # NOT NULL：读取侧与排序都不必再写 `COALESCE(..., updated_at)` 那种兜底
    assert cols["content_updated_at"]["notnull"] == 1

    with connect() as conn:
        stamps = {
            int(row["id"]): row["content_updated_at"]
            for row in conn.execute("SELECT id, content_updated_at FROM solutions").fetchall()
        }
        indexes = {row["name"] for row in conn.execute("PRAGMA index_list(solutions)")}
    assert stamps[older] == "2026-01-01T00:00:00+00:00"
    assert stamps[newer] == "2026-02-02T00:00:00+00:00"
    assert "idx_solutions_owner_content" in indexes


def test_v7_is_idempotent(monkeypatch) -> None:
    """v7 跑第二次是空操作——`ALTER TABLE ADD COLUMN` 没有 `IF NOT EXISTS`，
    全靠版本守卫挡住重跑；这条盯着那道守卫还在。"""
    from backend.core.migrations import MIGRATIONS

    monkeypatch.setattr("backend.core.migrations.MIGRATIONS", MIGRATIONS)
    assert run_migrations() == 8
    assert run_migrations() == 8


def test_v8_adds_doc_kind_to_jobs(monkeypatch) -> None:
    """v8：给 `generation_jobs` 加 `doc_kind`，旧行是空串（未知），不是 prd。

    「未知就不猜」是这一列的核心口径：空串被读取侧退化成 `None`，前端据此走
    「按 viewState 反推」的旧路，而不是拿 prd 去覆盖一份接口文档。
    """
    from backend.core.migrations import MIGRATIONS

    monkeypatch.setattr("backend.core.migrations.MIGRATIONS", MIGRATIONS[:7])
    assert run_migrations() == 7
    assert "doc_kind" not in _columns("generation_jobs")

    monkeypatch.setattr("backend.core.migrations.MIGRATIONS", MIGRATIONS)
    assert run_migrations() == 8

    cols = {row["name"]: row for row in _table_info("generation_jobs")}
    assert "doc_kind" in cols
    assert cols["doc_kind"]["notnull"] == 1

    with connect() as conn:
        # 旧行没有依据可考：默认值是空串，不得被回填成任何具体产物。
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, salt, created_at) VALUES (?, ?, ?, ?)",
            ("legacy-optimize", "hash", b"salt", "2026-01-01T00:00:00+00:00"),
        )
        user_id = int(cur.lastrowid or 0)
        conn.execute(
            "INSERT INTO generation_jobs (owner_id, solution_id, kind, status, created_at, updated_at)"
            " VALUES (?, NULL, 'optimize', 'running', '2026-01-01T00:00:00+00:00',"
            " '2026-01-01T00:00:00+00:00')",
            (user_id,),
        )
        conn.commit()
        row = conn.execute("SELECT doc_kind FROM generation_jobs").fetchone()
    assert row["doc_kind"] == ""
