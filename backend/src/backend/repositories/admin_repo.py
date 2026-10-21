"""管理员后台专用的两块读写：用量聚合与操作审计。

为什么单独一个文件
------------------
这两块都只在后台用到，塞进 `auth_repo`（管身份）或 `generation_job_repo`（管任务）
都会让那两个模块背上与自身职责无关的表。单独放也便于一眼看出「后台碰了哪些数据」。

- `AdminStatsRepository`：**只读**，把「每个用户用了多少」变成一条 SQL。
- `AuditRepository`：**只追加**，管理员每次写操作落一行。

审计为什么不加外键
------------------
`actor_id` 指向 `users(id)` 但**刻意不设外键约束**：管理员账号被删之后，
他做过的操作仍必须看得见。设了外键，那些记录会随删除一起消失——而那恰恰是
最需要保留的部分（「谁删的」往往正是被追责的人）。用户名因此冗余一份。
"""

from __future__ import annotations

from typing import Any

from backend.core.db import connect, utc_now_text

# 审计一次最多返回多少行。它是给人翻的，不是给机器导的：
# 真要做全量导出应该另开口子，而不是把这里的上限调到无穷大。
_AUDIT_MAX_ROWS = 500


class AdminStatsRepository:
    """管理员后台的统计查询。全部只读，不产生任何副作用。"""

    @staticmethod
    def user_usage() -> list[dict[str, Any]]:
        """每个用户的任务数与 Token 合计。

        用 `LEFT JOIN` 而不是先查用户再逐个统计：**没有生成过任务的用户也要出现**
        （一个刚注册的人，任务数就是 0）。若用 `INNER JOIN`，后台会看不见所有新用户，
        表现为「刚注册的账号在用户列表里凭空消失」。

        Token 读的是迁移 v14 加的两列，不是解析 `usage` 的 JSON——
        后者要把每一行取回 Python，量一大就跑不动。
        """
        with connect() as conn:
            rows = conn.execute(
                "SELECT u.id, u.username, u.created_at, u.is_active, u.is_admin, "
                "       COUNT(j.id)                        AS task_count, "
                "       COALESCE(SUM(j.prompt_tokens), 0)     AS prompt_tokens, "
                "       COALESCE(SUM(j.completion_tokens), 0) AS completion_tokens "
                "FROM users u "
                "LEFT JOIN generation_jobs j ON j.owner_id = u.id "
                "GROUP BY u.id "
                "ORDER BY u.id"
            ).fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    def platform_usage() -> dict[str, Any]:
        """平台总量：用户数、任务数、Token 合计。

        单独一条 SQL 而不是把 `user_usage()` 的结果在 Python 里加总——
        列表为了展示可能分页或裁剪，拿它算总量会悄悄算少。
        """
        with connect() as conn:
            row = conn.execute(
                "SELECT "
                "  (SELECT COUNT(*) FROM users)                          AS user_count, "
                "  (SELECT COUNT(*) FROM users WHERE is_admin = 1)       AS admin_count, "
                "  (SELECT COUNT(*) FROM generation_jobs)                AS task_count, "
                "  (SELECT COUNT(*) FROM generation_jobs "
                "     WHERE status = 'completed')                        AS completed_count, "
                "  (SELECT COALESCE(SUM(prompt_tokens), 0) "
                "     FROM generation_jobs)                              AS prompt_tokens, "
                "  (SELECT COALESCE(SUM(completion_tokens), 0) "
                "     FROM generation_jobs)                              AS completion_tokens"
            ).fetchone()
            return dict(row) if row else {}


class AuditRepository:
    """管理员操作审计。只追加，不更新、不删除。"""

    @staticmethod
    def record(
        *,
        actor_id: int | None,
        actor_name: str,
        action: str,
        target: str = "",
        detail: str = "",
        request_id: str | None = None,
    ) -> None:
        """落一条审计。

        Args:
            actor_id: 操作者 id。账号被删后这里会变空，故用户名另存一份。
            actor_name: 操作者用户名。
            action: 动作名，形如 `user.create` / `invite.delete`。
            target: 被操作对象（用户名、邀请码…）。
            detail: 变更内容。**绝不写密码、会话凭据等敏感值**——审计表是给人看的，
                不是保险箱，写进去等于给下一个读到它的人一份明文口令。
            request_id: 与当次请求的日志对得上，便于顺着一条线追到底。

        写失败只记警告、不抛：审计是**旁路**，一次写不动不该让管理员的删除操作
        整笔失败——但也不能静默吞掉，否则表空了还以为没人操作过。
        """
        with connect() as conn:
            conn.execute(
                "INSERT INTO admin_audit_log "
                "(actor_id, actor_name, action, target, detail, request_id, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    actor_id,
                    actor_name,
                    action,
                    target,
                    detail,
                    request_id,
                    utc_now_text(),
                ),
            )
            conn.commit()

    @staticmethod
    def list_recent(limit: int = 100) -> list[dict[str, Any]]:
        """最近的操作，倒序。上限由 `_AUDIT_MAX_ROWS` 兜住。"""
        safe_limit = max(1, min(int(limit), _AUDIT_MAX_ROWS))
        with connect() as conn:
            rows = conn.execute(
                "SELECT id, actor_id, actor_name, action, target, detail, request_id, ts "
                "FROM admin_audit_log ORDER BY id DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
            return [dict(row) for row in rows]
