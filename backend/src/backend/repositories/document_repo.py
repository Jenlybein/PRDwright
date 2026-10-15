"""文档（documents）的数据仓储。

与 `solution_repo.py` 同一套约定：**每个用户面方法的首参都是 `owner_id`，且不接受
`None`**——没有「不过滤归属」的逃生口。

与 `solutions` 不同的地方在于：`documents` 自己没有 `owner_id` 列。归属的唯一来源是
它所属的 `solutions.user_id`，于是这里用 `JOIN solutions` 把越权行在 SQL 层面滤掉，
路由层只需把「查不到 / 没写进去」翻译成 404。冗余一个 `owner_id` 会让归属出现第二个
真源，两边迟早不一致。

写入用 `INSERT ... SELECT ... ON CONFLICT DO UPDATE`：`SELECT` 部分带上归属条件，
因此**归属不通过时一行都插不进去**（`rowcount` 为 0），不需要先查一次再写。
"""

from __future__ import annotations

from typing import Any

from backend.core.db import connect, utc_now_text


class DocumentRepository:
    """`documents` 表的读写。所有方法强制带 `owner_id`。"""

    @staticmethod
    def list_by_solution(owner_id: int, solution_id: int) -> list[dict[str, Any]]:
        """列出某方案下的全部文档（至多三份），按写入先后排序。"""
        with connect() as conn:
            rows = conn.execute(
                "SELECT d.* FROM documents d "
                "JOIN solutions s ON s.id = d.solution_id "
                "WHERE s.user_id = ? AND d.solution_id = ? "
                "ORDER BY d.id",
                (owner_id, solution_id),
            ).fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    def get(owner_id: int, solution_id: int, kind: str) -> dict[str, Any] | None:
        """读取一份文档；不存在、或方案不属于该用户时返回 None。"""
        with connect() as conn:
            row = conn.execute(
                "SELECT d.* FROM documents d "
                "JOIN solutions s ON s.id = d.solution_id "
                "WHERE s.user_id = ? AND d.solution_id = ? AND d.kind = ?",
                (owner_id, solution_id, kind),
            ).fetchone()
            return dict(row) if row else None

    @staticmethod
    def save_content(
        owner_id: int,
        solution_id: int,
        kind: str,
        content: str,
        content_sha: str,
    ) -> bool:
        """写入用户手改后的正文，成功返回 True。

        **只覆盖 `content` 与 `content_sha`**：`pending_items`（模型产出的待确认清单）
        与 `last_run_summary`（用量回执）保持原样。用户改几个字不该把清单清空——
        那是两类完全不同的数据，生命周期也不同。
        """
        now = utc_now_text()
        with connect() as conn:
            cur = conn.execute(
                "INSERT INTO documents "
                "(solution_id, kind, content, content_sha, pending_items, "
                " last_run_summary, created_at, updated_at) "
                "SELECT s.id, ?, ?, ?, '[]', NULL, ?, ? "
                "FROM solutions s WHERE s.id = ? AND s.user_id = ? "
                "ON CONFLICT(solution_id, kind) DO UPDATE SET "
                "  content = excluded.content, "
                "  content_sha = excluded.content_sha, "
                "  updated_at = excluded.updated_at",
                (kind, content, content_sha, now, now, solution_id, owner_id),
            )
            conn.commit()
            return cur.rowcount == 1

    @staticmethod
    def commit_generated(
        owner_id: int,
        solution_id: int,
        kind: str,
        content: str,
        content_sha: str,
        pending_items: str,
        last_run_summary: str | None,
        invalidate: tuple[str, ...] = (),
    ) -> bool:
        """一次生成（或改写）收尾时落稿，成功返回 True。

        与 `save_content` 的关键差别：正文、待确认清单、用量回执**同批替换**。
        这三者产自同一次模型调用栈，分开写就会各自漂移——那正是「清单与正文对不上」
        这类问题的来源。

        `invalidate` 列出「被本次生成取代、应当删除」的产物种类（下游产物从上游正文
        推导而来，上游重写后它们就失效了）。删除与写入**在同一个事务里**完成：
        若分成两次提交，中间那一瞬就是「新正文已写、旧下游还在」，刷新页面会看到
        一份自相矛盾的方案。删不掉（归属不符）不影响本次写入，`rowcount` 仍按
        写入结果返回。
        """
        now = utc_now_text()
        with connect() as conn:
            for stale_kind in invalidate:
                conn.execute(
                    "DELETE FROM documents WHERE id IN ("
                    "  SELECT d.id FROM documents d "
                    "  JOIN solutions s ON s.id = d.solution_id "
                    "  WHERE s.user_id = ? AND d.solution_id = ? AND d.kind = ?"
                    ")",
                    (owner_id, solution_id, stale_kind),
                )
            cur = conn.execute(
                "INSERT INTO documents "
                "(solution_id, kind, content, content_sha, pending_items, "
                " last_run_summary, created_at, updated_at) "
                "SELECT s.id, ?, ?, ?, ?, ?, ?, ? "
                "FROM solutions s WHERE s.id = ? AND s.user_id = ? "
                "ON CONFLICT(solution_id, kind) DO UPDATE SET "
                "  content = excluded.content, "
                "  content_sha = excluded.content_sha, "
                "  pending_items = excluded.pending_items, "
                "  last_run_summary = excluded.last_run_summary, "
                "  updated_at = excluded.updated_at",
                (
                    kind,
                    content,
                    content_sha,
                    pending_items,
                    last_run_summary,
                    now,
                    now,
                    solution_id,
                    owner_id,
                ),
            )
            conn.commit()
            return cur.rowcount == 1
