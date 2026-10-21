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
        doc_kind: str = "",
        chat_stage: str = "",
    ) -> int:
        """新建一条 running 任务，返回其 id。

        Args:
            owner_id: 归属用户，恒非空。
            kind: 任务类型，`prd` / `api_docs` / `prompts` / `optimize` / `chat`。
            solution_id: 关联方案；快捷入口等场景可为 `None`。
            input_fingerprint: 输入指纹（本期仅 prd 计算），可为空。
            doc_kind: 这次任务**写哪一份产物**；未知时传空串。
            chat_stage: 对话任务属于哪条线程（仅 `chat`），其余传空串。

        `doc_kind` 与 `kind` 分开：`kind` 是「做了什么」，`doc_kind` 是「写到哪」。
        `optimize` / `clarify` 这类动作凭 `kind` 定不了产物，只有请求体知道；
        创建时就把结论落列，刷新 / 进程重启后仍能回答「这个任务改的是哪一份」。

        `chat_stage` 同理：`kind='chat'` 定不了「哪条线程」，而并发判重与重连都要它。
        """
        now = utc_now_text()
        with connect() as conn:
            cur = conn.execute(
                "INSERT INTO generation_jobs "
                "(owner_id, solution_id, kind, doc_kind, chat_stage, status, phase, "
                " draft_content, completed_sections, input_fingerprint, usage, "
                " max_provider_switches, error_code, error_message, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'running', '', '', '[]', ?, NULL, 2, '', '', ?, ?)",
                (owner_id, solution_id, kind, doc_kind, chat_stage, input_fingerprint, now, now),
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
        chat_stage: str | None = None,
    ) -> dict[str, Any] | None:
        """查该 owner 下最近的 running 任务（`updated_at DESC`）。

        `solution_id` / `kind` / `chat_stage` 为 `None` 时不参与过滤。
        """
        clauses = ["owner_id = ?", "status = 'running'"]
        params: list[Any] = [owner_id]
        if solution_id is not None:
            clauses.append("solution_id = ?")
            params.append(solution_id)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if chat_stage is not None:
            clauses.append("chat_stage = ?")
            params.append(chat_stage)
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

        `error_code` 用 `SUPERSEDED` 而不是笼统的 `INTERNAL_ERROR`：这次失败有明确原因
        （被别人接手了，不是出错），运维看这一行时不该去翻日志找异常。
        调用方还要把内存里的旧运行时一并标记——只改库的话，那个运行时收尾时仍会写文档。
        """
        clauses = ["owner_id = ?", "kind = ?", "status = 'running'"]
        where_params: list[Any] = [owner_id, kind]
        if solution_id is None:
            clauses.append("solution_id IS NULL")
        else:
            clauses.append("solution_id = ?")
            where_params.append(solution_id)
        set_params: list[Any] = ["SUPERSEDED", "被新任务取代", utc_now_text()]
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
    def cancel_one(owner_id: int, job_id: int, *, message: str) -> bool:
        """把某一条 running 任务置为 failed + CANCELLED，返回是否确实改到了。

        与 `cancel_running`（按方案批量）分开：用户按「停止生成」针对的是**这一次**
        任务，而同一方案下别的线程可能也在跑，批量取消会连带掐掉它们。

        任务已经自然结束（`rowcount = 0`）时返回 False：调用方据此回答
        「没取消到东西」，而不是报错——「你点的时候它刚好说完了」不是失败。
        """
        with connect() as conn:
            cur = conn.execute(
                "UPDATE generation_jobs "
                "SET status = 'failed', error_code = 'CANCELLED', "
                "    error_message = ?, updated_at = ? "
                "WHERE owner_id = ? AND id = ? AND status = 'running'",
                (message, utc_now_text(), owner_id, job_id),
            )
            conn.commit()
            return int(cur.rowcount or 0) == 1

    @staticmethod
    def cancel_running(owner_id: int, *, solution_id: int) -> int:
        """把该 owner + 方案下所有 running 任务置为 failed，返回受影响条数。

        用在「方案被删除」：方案没了，挂在它上面的生成任务再跑完也没地方落正文，
        继续跑只会白烧 token，并在收尾时写一次注定被丢弃的文档。

        ⚠️ **刻意复用 `failed` 而不是新造一个 `cancelled` 状态**：任务状态是前后端共享
        的契约（前端所有 status 分支都按 running / completed / failed 三态判），
        多一个取值意味着每一处分支都要多一条分支，而漏掉任何一处都会让界面停在
        「生成中」。语义上被取消确实「没有成功」，复用 `failed` 是代价最小的选择；
        具体的取消原因放在 `error_code='CANCELLED'` / `error_message` 里，需要区分时读它们。

        `solution_id` 用 `= ?` 而不是 `IS NULL`：本方法只服务于「删掉一份具体方案」，
        没有方案的任务（快捷入口逃生口）不会被这个动作波及。
        """
        with connect() as conn:
            cur = conn.execute(
                "UPDATE generation_jobs "
                "SET status = 'failed', error_code = 'CANCELLED', "
                "    error_message = '方案已删除，任务已取消', updated_at = ? "
                "WHERE owner_id = ? AND solution_id = ? AND status = 'running'",
                (utc_now_text(), owner_id, solution_id),
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
