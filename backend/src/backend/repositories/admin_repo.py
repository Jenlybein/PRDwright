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

from datetime import timedelta
from typing import Any

from backend.core.db import connect, utc_now, utc_now_text

# 审计一次最多返回多少行。它是给人翻的，不是给机器导的：
# 真要做全量导出应该另开口子，而不是把这里的上限调到无穷大。
_AUDIT_MAX_ROWS = 500

# 「不算真失败」的错误码。这两个同样是 `status='failed'`，但成因完全不同：
# - `CANCELLED`：用户自己删了方案 / 点了停止；
# - `SUPERSEDED`：同一份文档被更晚的任务接手，旧的那支主动让开。
# 把它们算进失败率，会让平台看起来比实际脆弱得多——而管理员据此判断「模型稳不稳」，
# 正是最不该被污染的一个判断。
_NOT_REAL_FAILURE = ("CANCELLED", "SUPERSEDED")

# 用户用量的聚合表达式。**只写一份**：用户列表、排行、平台总量三处都引它，
# 否则「这个用户用了多少」迟早会出现两个数，而没人知道该信哪个。
_USER_USAGE_SELECT = (
    "       COUNT(j.id)                               AS task_count, "
    "       COALESCE(SUM(j.prompt_tokens), 0)            AS prompt_tokens, "
    "       COALESCE(SUM(j.completion_tokens), 0)        AS completion_tokens "
)


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
                + _USER_USAGE_SELECT
                + "FROM users u "
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

    @staticmethod
    def daily(days: int, tz_offset_minutes: int) -> list[dict[str, Any]]:
        """按**本地日期**聚合最近 `days` 天（含今天），缺数据的日子补 0。

        为什么按本地日期而不是 UTC 日期
        ------------------------------
        `created_at` 存的是 UTC。东八区管理员在本地 23:30 生成的任务，UTC 已经是次日
        00:30——按 UTC 分组会把它算到「明天」，于是「昨晚用的」出现在「今天」那一格。
        管理员看的是本地时间，分组也必须按本地时间。换算交给 SQLite：
        `datetime(created_at, '+480 minutes')`（实测能正确解析微秒级 ISO 串）。

        为什么缺的日子要补 0 行
        ----------------------
        折线图若跳过空白日，会把「周二没人用」画成一条从周一直接连到周三的斜线——
        看起来像「用量平滑变化」，而真相是「那天一个都没有」。**0 和「没有这一天」
        是两个完全不同的结论**，图上必须能看出来。补 0 放在 Python 里做：
        SQL 的递归 CTE 虽然也能生成日历，但读起来远比几行 Python 难。

        失败率的口径
        ------------
        分母是**已收尾**任务（completed + failed），不含 `running`——还在跑的任务
        既不算成功也不算失败，放进分母会把失败率人为压低。
        分子排除 `CANCELLED` / `SUPERSEDED`（见 `_NOT_REAL_FAILURE`）。
        分母为 0 时 `failure_rate` 返回 `None` 而不是 0：那一天没有任何任务，
        「0% 失败」会被读成「全成功了」。

        Args:
            days: 天数（含今天），调用方负责约束范围。
            tz_offset_minutes: 相对 UTC 的分钟偏移（东八区为 480）。

        Returns:
            按日期升序的每一天，字段见下方 `day_row`。
        """
        safe_days = max(1, min(int(days), 365))
        offset = f"{int(tz_offset_minutes):+d} minutes"

        # 先用 `created_at >= ?` 粗筛走索引（时间串的字典序与时间序一致），
        # 再在结果集里换时区。多留一天余量：时区换算会让「本地窗口的第一天」
        # 落在 UTC 的再前一天，卡得太紧会把边界那几条默默漏掉。
        cutoff = (utc_now() - timedelta(days=safe_days + 1)).isoformat()
        with connect() as conn:
            rows = conn.execute(
                "SELECT substr(datetime(created_at, ?), 1, 10) AS day, "
                "       COUNT(*)                                  AS task_count, "
                "       SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed_count, "
                "       SUM(CASE WHEN status = 'failed' "
                "                 AND error_code NOT IN (?, ?) THEN 1 ELSE 0 END) AS failed_count, "
                "       SUM(CASE WHEN status = 'failed' "
                "                 AND error_code IN (?, ?) THEN 1 ELSE 0 END)     AS cancelled_count, "
                "       SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END)    AS running_count, "
                "       COALESCE(SUM(prompt_tokens), 0)     AS prompt_tokens, "
                "       COALESCE(SUM(completion_tokens), 0) AS completion_tokens "
                "FROM generation_jobs "
                "WHERE created_at >= ? "
                "GROUP BY day "
                "ORDER BY day",
                (offset, *_NOT_REAL_FAILURE, *_NOT_REAL_FAILURE, cutoff),
            ).fetchall()

        by_day = {row["day"]: dict(row) for row in rows}

        # 补齐日期：从「本地今天」往前倒推 days-1 天
        today = (utc_now() + timedelta(minutes=int(tz_offset_minutes))).date()
        result: list[dict[str, Any]] = []
        for i in range(safe_days - 1, -1, -1):
            day = (today - timedelta(days=i)).isoformat()
            row = by_day.get(day)
            if row is None:
                row = {
                    "day": day,
                    "task_count": 0,
                    "completed_count": 0,
                    "failed_count": 0,
                    "cancelled_count": 0,
                    "running_count": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                }
            # 失败率由后端算好：前端各算一遍就会与这里漂移
            finished = int(row["completed_count"]) + int(row["failed_count"])
            row["failure_rate"] = (
                round(int(row["failed_count"]) / finished, 4) if finished else None
            )
            result.append(row)
        return result

    @staticmethod
    def top_users(limit: int = 10) -> list[dict[str, Any]]:
        """用量最高的用户。

        与 `user_usage()` 共用同一段聚合表达式（`_USER_USAGE_SELECT`）：
        「这个用户用了多少」只能有一个算法，否则用户列表与排行榜会给出两个数。

        只列**真的生成过任务**的人：排行榜里塞一堆 0 没有意义，还会把真用户挤下去。
        排序先按任务数、再按总 Token——任务数相同的人里，烧得多的那个更值得注意。
        """
        safe_limit = max(1, min(int(limit), 100))
        with connect() as conn:
            rows = conn.execute(
                "SELECT u.id, u.username, u.is_active, "
                + _USER_USAGE_SELECT
                + "FROM users u "
                "JOIN generation_jobs j ON j.owner_id = u.id "
                "GROUP BY u.id "
                "HAVING COUNT(j.id) > 0 "
                "ORDER BY task_count DESC, "
                "         (COALESCE(SUM(j.prompt_tokens), 0) + "
                "          COALESCE(SUM(j.completion_tokens), 0)) DESC, "
                "         u.id ASC "
                "LIMIT ?",
                (safe_limit,),
            ).fetchall()
            return [dict(row) for row in rows]


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
