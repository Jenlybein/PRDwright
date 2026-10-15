"""迁移地基的用例。

迁移是「只跑一次、跑错了很难回头」的动作，所以这里盯三件事：
清单本身合法、跑过的步骤不会重跑、跑挂的步骤不留下半个结构。

用例不碰 `MIGRATIONS` 的真实内容——那属于各阶段自己的测试。这里只验证**机制**，
因此每次都用 `monkeypatch` 换上一份临时的迁移清单。
"""

from __future__ import annotations

import sqlite3

import pytest

from backend.core.db import connect
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

    # 跑真实迁移清单（含 v3），最终版本应为 3
    assert run_migrations() == 3

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
