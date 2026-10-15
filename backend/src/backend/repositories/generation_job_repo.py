"""生成任务（generation_jobs）的数据仓储。

**数据隔离落在这一层，而不是路由层。** 与 `solution_repo` 的口径一致：除系统级维护
方法 `reap_running` 外，每个方法的第一个参数都是 `owner_id` 且**不接受 `None`**——
没有「不过滤归属」的逃生口。所有查询、更新都把它写进 SQL 的 WHERE 条件，
越权访问在数据库层面就查不到这一行。

`draft_content` 走**合并写**：`job_service` 在内存里维护权威正文，按「≤300ms 或增量
>4KB」的节奏调用 `persist_progress` 落一份最新快照。因此本层不实现
`add_completed_section` 那种「读-改-写单段」的方法——那种写法要在单连接内 read-modify-write
才不会并发丢段，而合并写由调用方一次传入完整的 `completed_sections`，天然避免该问题。
"""

from __future__ import annotations

import json
from typing import Any

from backend.core.db import connect, utc_now_text

# 任务状态：running / completed / failed（本层不做枚举约束，取值由服务层保证）
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"


def _dump_sections(sections: list[dict[str, Any]]) -> str:
    """把章节列表序列化成 JSON 文本（中文不转义，便于人工排查）。"""
    return json.dumps(sections or [], ensure_ascii=False)


def _dump_pending_items(items: list[dict[str, Any]] | None) -> str:
    """把待确认清单序列化成 JSON 文本（中文不转义）。`None` 视为空清单。"""
    return json.dumps(items or [], ensure_ascii=False)


class GenerationJobRepository:
    """`generation_jobs` 表的读写。所有用户面方法强制带 `owner_id`。"""

    @staticmethod
    def create(
        owner_id: int,
        kind: str,
        solution_id: int | None,
        input_fingerprint: str | None = None,
    ) -> int:
        """新建一条 running 任务，返回其 id。

        Args:
            owner_id: 归属用户，恒非空。
            kind: 任务类型，`prd` / `api_docs` / `prompts` / `optimize`。
            solution_id: 关联方案；快捷入口等场景可为 `None`。
            input_fingerprint: 输入指纹（本期仅 prd 计算），可为空。

        Returns:
            新任务的 id。
        """
        now = utc_now_text()
        with connect() as conn:
            cur = conn.execute(
                "INSERT INTO generation_jobs "
                "(owner_id, solution_id, kind, status, phase, draft_content, "
                " completed_sections, input_fingerprint, usage, max_provider_switches, "
                " error_code, error_message, created_at, updated_at) "
                "VALUES (?, ?, ?, 'running', '', '', '[]', ?, NULL, 2, '', '', ?, ?)",
                (owner_id, solution_id, kind, input_fingerprint, now, now),
            )
            conn.commit()
            return int(cur.lastrowid or 0)

    @staticmethod
    def get(owner_id: int, job_id: int) -> dict[str, Any] | None:
        """读取单条任务；不存在或不属于该用户时返回 None。"""
        with connect() as conn:
            row = conn.execute(
                "SELECT * FROM generation_jobs WHERE id = ? AND owner_id = ?",
                (job_id, owner_id),
            ).fetchone()
            return dict(row) if row else None

    @staticmethod
    def find_active(
        owner_id: int,
        *,
        solution_id: int | None = None,
        kind: str | None = None,
    ) -> dict[str, Any] | None:
        """查该 owner 下最近的 running 任务（`updated_at DESC`）。

        `solution_id` / `kind` 为 `None` 时不参与过滤。
        """
        clauses = ["owner_id = ?", "status = 'running'"]
        params: list[Any] = [owner_id]
        if solution_id is not None:
            clauses.append("solution_id = ?")
            params.append(solution_id)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        with connect() as conn:
            row = conn.execute(
                f"SELECT * FROM generation_jobs WHERE {' AND '.join(clauses)} "
                "ORDER BY updated_at DESC, id DESC LIMIT 1",
                params,
            ).fetchone()
            return dict(row) if row else None

    @staticmethod
    def list_by_owner(
        owner_id: int,
        *,
        status: str | None = None,
        solution_id: int | None = None,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        """列出该 owner 的任务，最近创建在前；可选按 status / solution_id / kind 过滤。"""
        clauses = ["owner_id = ?"]
        params: list[Any] = [owner_id]
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if solution_id is not None:
            clauses.append("solution_id = ?")
            params.append(solution_id)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        with connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM generation_jobs WHERE {' AND '.join(clauses)} "
                "ORDER BY created_at DESC, id DESC",
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    def persist_progress(
        owner_id: int,
        job_id: int,
        *,
        draft_content: str,
        completed_sections: list[dict[str, Any]],
        phase: str,
        pending_items: list[dict[str, Any]] | None = None,
    ) -> None:
        """合并写一次进度（正文 / 章节 / 阶段 / 待确认清单）。WHERE 含 owner_id，改不到别人的任务。"""
        with connect() as conn:
            conn.execute(
                "UPDATE generation_jobs "
                "SET draft_content = ?, completed_sections = ?, phase = ?, "
                "    pending_items = ?, updated_at = ? "
                "WHERE id = ? AND owner_id = ?",
                (
                    draft_content,
                    _dump_sections(completed_sections),
                    phase,
                    _dump_pending_items(pending_items),
                    utc_now_text(),
                    job_id,
                    owner_id,
                ),
            )
            conn.commit()

    @staticmethod
    def finish(
        owner_id: int,
        job_id: int,
        *,
        draft_content: str,
        completed_sections: list[dict[str, Any]],
        usage: str | None = None,
        pending_items: list[dict[str, Any]] | None = None,
    ) -> None:
        """标记任务完成，落最终正文与章节。清空错误字段。"""
        with connect() as conn:
            conn.execute(
                "UPDATE generation_jobs "
                "SET status = 'completed', draft_content = ?, completed_sections = ?, "
                "    usage = ?, error_code = '', error_message = '', pending_items = ?, "
                "    updated_at = ? "
                "WHERE id = ? AND owner_id = ?",
                (
                    draft_content,
                    _dump_sections(completed_sections),
                    usage,
                    _dump_pending_items(pending_items),
                    utc_now_text(),
                    job_id,
                    owner_id,
                ),
            )
            conn.commit()

    @staticmethod
    def fail(
        owner_id: int,
        job_id: int,
        *,
        error_code: str,
        error_message: str,
        draft_content: str,
        completed_sections: list[dict[str, Any]],
        usage: str | None = None,
        pending_items: list[dict[str, Any]] | None = None,
    ) -> None:
        """标记任务失败，保留已产出的半截正文、章节与已发生的用量。

        `usage` 在失败路径同样要落：失败的生成**照样消耗了 token**，
        只记成功那次等于把最贵的失败藏起来。
        """
        with connect() as conn:
            conn.execute(
                "UPDATE generation_jobs "
                "SET status = 'failed', error_code = ?, error_message = ?, "
                "    draft_content = ?, completed_sections = ?, usage = ?, "
                "    pending_items = ?, updated_at = ? "
                "WHERE id = ? AND owner_id = ?",
                (
                    error_code,
                    error_message,
                    draft_content,
                    _dump_sections(completed_sections),
                    usage,
                    _dump_pending_items(pending_items),
                    utc_now_text(),
                    job_id,
                    owner_id,
                ),
            )
            conn.commit()

    @staticmethod
    def supersede_active(owner_id: int, *, solution_id: int | None, kind: str) -> int:
        """把同 owner + solution + kind 下旧的 running 置为 failed，返回被取代的条数。

        「同一 owner + solution + kind 至多一个 running」是一条业务不变量，在新建任务前
        先把旧的中止掉，避免两个任务同时往同一处写正文。
        `solution_id` 可能为 `None`，SQL 需用 `IS NULL` 而不是 `= NULL`。
        """
        clauses = ["owner_id = ?", "kind = ?", "status = 'running'"]
        where_params: list[Any] = [owner_id, kind]
        if solution_id is None:
            clauses.append("solution_id IS NULL")
        else:
            clauses.append("solution_id = ?")
            where_params.append(solution_id)
        set_params: list[Any] = ["INTERNAL_ERROR", "被新任务取代", utc_now_text()]
        with connect() as conn:
            cur = conn.execute(
                "UPDATE generation_jobs "
                "SET status = 'failed', error_code = ?, error_message = ?, updated_at = ? "
                f"WHERE {' AND '.join(clauses)}",
                (*set_params, *where_params),
            )
            conn.commit()
            return int(cur.rowcount or 0)

    @staticmethod
    def reap_running() -> int:
        """系统级维护：把所有残留的 running 任务置为 failed，返回条数。

        **这是本仓库唯一没有 `owner_id` 的方法**，属于启动运维（进程重启后，内存里
        的运行时已丢失，DB 里不可能还有真正在跑的任务），**不是用户面接口**。
        调用点只有 `job_service.reap_orphans()`，即在应用 `lifespan` 启动时回收上次
        崩溃 / 重启留下的悬挂任务。
        """
        with connect() as conn:
            cur = conn.execute(
                "UPDATE generation_jobs "
                "SET status = 'failed', error_code = 'INTERNAL_ERROR', "
                "    error_message = '服务重启，任务已中断', updated_at = ? "
                "WHERE status = 'running'",
                (utc_now_text(),),
            )
            conn.commit()
            return int(cur.rowcount or 0)
