"""管理员后台专用的两块读写：用量聚合与操作审计。

为什么单独一个文件
------------------
这两块都只在后台用到，塞进 `auth_repo`（管身份）或 `generation_job_repo`（管任务）
都会让那两个模块背上与自身职责无关的表。单独放也便于一眼看出「后台碰了哪些数据」。

- `AdminStatsRepository`：**只读**，把「每个用户用了多少」变成一条 SQL。
- `AuditRepository`：**只追加**，管理员每次写操作落一行。

为什么聚合结果用 `@dataclass` 而不是裸 `dict`
-------------------------------------------
`GROUP BY` 的结果不是任何一张表的行，没有对应的模型类；但裸 dict 会把「有哪些字段」
变成运行期才知道的事，调用方写错一个键要到用户点开后台才报 500。这里用**不可变投影**
（frozen dataclass）把形状钉死：少一个字段是导入期就能发现的 `TypeError`，
而不是线上的一条 KeyError。

审计为什么不加外键
------------------
`actor_id` 指向 `users(id)` 但**刻意不设外键约束**：管理员账号被删之后，
他做过的操作仍必须看得见。设了外键，那些记录会随删除一起消失——而那恰恰是
最需要保留的部分（「谁删的」往往正是被追责的人）。用户名因此冗余一份。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, case, func, select
from sqlmodel import Session

from backend.core.clock import utc_now, utc_now_text
from backend.core.models import AdminAuditLog, GenerationJob, User

# 审计一次最多返回多少行。它是给人翻的，不是给机器导的：
# 真要做全量导出应该另开口子，而不是把这里的上限调到无穷大。
_AUDIT_MAX_ROWS = 500

# 「不算真失败」的错误码。这两个同样是 `status='failed'`，但成因完全不同：
# - `CANCELLED`：用户自己删了方案 / 点了停止；
# - `SUPERSEDED`：同一份文档被更晚的任务接手，旧的那支主动让开。
# 把它们算进失败率，会让平台看起来比实际脆弱得多——而管理员据此判断「模型稳不稳」，
# 正是最不该被污染的一个判断。
_NOT_REAL_FAILURE = ("CANCELLED", "SUPERSEDED")


@dataclass(frozen=True, slots=True)
class UserUsage:
    """用户列表的一行：账号基本信息 + 累计用量 + 额度覆盖值。"""

    id: int
    username: str
    created_at: str
    is_active: bool
    is_admin: bool
    # 额度覆盖原值（v17）。生效值要靠这三列与全局默认换算——换算规则只在
    # `quota_service.resolve_from` 一处实现，这里只负责把原值带出来。
    daily_task_limit: int
    daily_token_limit: int
    max_running: int
    task_count: int
    prompt_tokens: int
    completion_tokens: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "created_at": self.created_at,
            "is_active": self.is_active,
            "is_admin": self.is_admin,
            "daily_task_limit": self.daily_task_limit,
            "daily_token_limit": self.daily_token_limit,
            "max_running": self.max_running,
            "task_count": self.task_count,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


@dataclass(frozen=True, slots=True)
class TopUser:
    """排行榜的一行（只列真的生成过任务的人）。"""

    id: int
    username: str
    is_active: bool
    task_count: int
    prompt_tokens: int
    completion_tokens: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "is_active": self.is_active,
            "task_count": self.task_count,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


@dataclass(frozen=True, slots=True)
class PlatformUsage:
    """平台总量。"""

    user_count: int
    admin_count: int
    task_count: int
    completed_count: int
    prompt_tokens: int
    completion_tokens: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_count": self.user_count,
            "admin_count": self.admin_count,
            "task_count": self.task_count,
            "completed_count": self.completed_count,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


@dataclass(frozen=True, slots=True)
class DailyUsage:
    """按本地日期聚合的一天。`failure_rate` 为 None 表示那天没有已收尾任务。"""

    day: str
    task_count: int
    completed_count: int
    failed_count: int
    cancelled_count: int
    running_count: int
    prompt_tokens: int
    completion_tokens: int
    failure_rate: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "task_count": self.task_count,
            "completed_count": self.completed_count,
            "failed_count": self.failed_count,
            "cancelled_count": self.cancelled_count,
            "running_count": self.running_count,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "failure_rate": self.failure_rate,
        }


class AdminStatsRepository:
    """管理员后台的统计查询。全部只读，不产生任何副作用。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def user_usage(self) -> list[UserUsage]:
        """每个用户的任务数与 Token 合计。

        用 `LEFT JOIN` 而不是先查用户再逐个统计：**没有生成过任务的用户也要出现**
        （一个刚注册的人，任务数就是 0）。若用 `INNER JOIN`，后台会看不见所有新用户，
        表现为「刚注册的账号在用户列表里凭空消失」。

        Token 读的是迁移 v14 加的两列，不是解析 `usage` 的 JSON——
        后者要把每一行取回 Python，量一大就跑不动。
        """
        stmt = (
            select(
                User.id,
                User.username,
                User.created_at,
                User.is_active,
                User.is_admin,
                User.daily_task_limit,
                User.daily_token_limit,
                User.max_running,
                func.count(GenerationJob.id).label("task_count"),
                func.coalesce(func.sum(GenerationJob.prompt_tokens), 0).label("prompt_tokens"),
                func.coalesce(func.sum(GenerationJob.completion_tokens), 0).label(
                    "completion_tokens"
                ),
            )
            .select_from(User)
            .join(GenerationJob, GenerationJob.owner_id == User.id, isouter=True)
            .group_by(User.id)
            .order_by(User.id)
        )
        return [
            UserUsage(
                id=int(row[0] or 0),
                username=str(row[1]),
                created_at=str(row[2] or ""),
                is_active=bool(row[3]),
                is_admin=bool(row[4]),
                daily_task_limit=int(row[5]),
                daily_token_limit=int(row[6]),
                max_running=int(row[7]),
                task_count=int(row[8]),
                prompt_tokens=int(row[9]),
                completion_tokens=int(row[10]),
            )
            for row in self.session.execute(stmt).all()
        ]

    def platform_usage(self) -> PlatformUsage:
        """平台总量：用户数、任务数、Token 合计。

        单独一条查询而不是把 `user_usage()` 的结果在 Python 里加总——
        列表为了展示可能分页或裁剪，拿它算总量会悄悄算少。
        """

        def _count(from_clause: Any, *where: Any) -> Any:
            stmt = select(func.count()).select_from(from_clause)
            for clause in where:
                stmt = stmt.where(clause)
            return stmt.scalar_subquery()

        def _sum(column: Any) -> Any:
            return select(func.coalesce(func.sum(column), 0)).scalar_subquery()

        row = self.session.execute(
            select(
                _count(User).label("user_count"),
                _count(User, User.is_admin.is_(True)).label("admin_count"),
                _count(GenerationJob).label("task_count"),
                _count(GenerationJob, GenerationJob.status == "completed").label(
                    "completed_count"
                ),
                _sum(GenerationJob.prompt_tokens).label("prompt_tokens"),
                _sum(GenerationJob.completion_tokens).label("completion_tokens"),
            )
        ).one()
        return PlatformUsage(
            user_count=int(row[0] or 0),
            admin_count=int(row[1] or 0),
            task_count=int(row[2] or 0),
            completed_count=int(row[3] or 0),
            prompt_tokens=int(row[4] or 0),
            completion_tokens=int(row[5] or 0),
        )

    def daily(self, days: int, tz_offset_minutes: int) -> list[DailyUsage]:
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
            按日期升序的每一天。
        """
        safe_days = max(1, min(int(days), 365))
        offset = f"{int(tz_offset_minutes):+d} minutes"

        # 先用 `created_at >= ?` 粗筛走索引（时间串的字典序与时间序一致），
        # 再在结果集里换时区。多留一天余量：时区换算会让「本地窗口的第一天」
        # 落在 UTC 的再前一天，卡得太紧会把边界那几条默默漏掉。
        cutoff = (utc_now() - timedelta(days=safe_days + 1)).isoformat()

        day_of = func.substr(func.datetime(GenerationJob.created_at, offset), 1, 10)
        status_is = lambda value: GenerationJob.status == value  # noqa: E731
        real_failure = and_(
            status_is("failed"), GenerationJob.error_code.notin_(_NOT_REAL_FAILURE)
        )
        not_real_failure = and_(
            status_is("failed"), GenerationJob.error_code.in_(_NOT_REAL_FAILURE)
        )

        stmt = (
            select(
                day_of.label("day"),
                func.count().label("task_count"),
                func.sum(case((status_is("completed"), 1), else_=0)).label("completed_count"),
                func.sum(case((real_failure, 1), else_=0)).label("failed_count"),
                func.sum(case((not_real_failure, 1), else_=0)).label("cancelled_count"),
                func.sum(case((status_is("running"), 1), else_=0)).label("running_count"),
                func.coalesce(func.sum(GenerationJob.prompt_tokens), 0).label("prompt_tokens"),
                func.coalesce(func.sum(GenerationJob.completion_tokens), 0).label(
                    "completion_tokens"
                ),
            )
            .where(GenerationJob.created_at >= cutoff)
            .group_by(day_of)
            .order_by(day_of)
        )

        by_day: dict[str, dict[str, Any]] = {
            str(row[0]): {
                "task_count": int(row[1] or 0),
                "completed_count": int(row[2] or 0),
                "failed_count": int(row[3] or 0),
                "cancelled_count": int(row[4] or 0),
                "running_count": int(row[5] or 0),
                "prompt_tokens": int(row[6] or 0),
                "completion_tokens": int(row[7] or 0),
            }
            for row in self.session.execute(stmt).all()
        }

        # 补齐日期：从「本地今天」往前倒推 days-1 天
        today = (utc_now() + timedelta(minutes=int(tz_offset_minutes))).date()
        result: list[DailyUsage] = []
        for i in range(safe_days - 1, -1, -1):
            day = (today - timedelta(days=i)).isoformat()
            row = by_day.get(day) or {
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
            result.append(
                DailyUsage(
                    day=day,
                    task_count=int(row["task_count"]),
                    completed_count=int(row["completed_count"]),
                    failed_count=int(row["failed_count"]),
                    cancelled_count=int(row["cancelled_count"]),
                    running_count=int(row["running_count"]),
                    prompt_tokens=int(row["prompt_tokens"]),
                    completion_tokens=int(row["completion_tokens"]),
                    failure_rate=(
                        round(int(row["failed_count"]) / finished, 4) if finished else None
                    ),
                )
            )
        return result

    def top_users(self, limit: int = 10) -> list[TopUser]:
        """用量最高的用户。

        「这个用户用了多少」只能有一个算法，否则用户列表与排行榜会给出两个数
        ——两边都按同一组聚合列来数。

        只列**真的生成过任务**的人：排行榜里塞一堆 0 没有意义，还会把真用户挤下去。
        排序先按任务数、再按总 Token——任务数相同的人里，烧得多的那个更值得注意。
        """
        safe_limit = max(1, min(int(limit), 100))
        total_tokens = func.coalesce(func.sum(GenerationJob.prompt_tokens), 0) + func.coalesce(
            func.sum(GenerationJob.completion_tokens), 0
        )
        stmt = (
            select(
                User.id,
                User.username,
                User.is_active,
                func.count(GenerationJob.id).label("task_count"),
                func.coalesce(func.sum(GenerationJob.prompt_tokens), 0).label("prompt_tokens"),
                func.coalesce(func.sum(GenerationJob.completion_tokens), 0).label(
                    "completion_tokens"
                ),
            )
            .select_from(User)
            .join(GenerationJob, GenerationJob.owner_id == User.id)
            .group_by(User.id)
            .having(func.count(GenerationJob.id) > 0)
            .order_by(
                func.count(GenerationJob.id).desc(),
                total_tokens.desc(),
                User.id.asc(),
            )
            .limit(safe_limit)
        )
        return [
            TopUser(
                id=int(row[0] or 0),
                username=str(row[1]),
                is_active=bool(row[2]),
                task_count=int(row[3]),
                prompt_tokens=int(row[4]),
                completion_tokens=int(row[5]),
            )
            for row in self.session.execute(stmt).all()
        ]


class AuditRepository:
    """管理员操作审计。只追加，不更新、不删除。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self,
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

        写失败由调用方（`admin_service.record_audit`）记警告、不抛：审计是**旁路**，
        一次写不动不该让管理员的删除操作整笔失败——但也不能静默吞掉，
        否则表空了还以为没人操作过。
        """
        self.session.add(
            AdminAuditLog(
                actor_id=actor_id,
                actor_name=actor_name,
                action=action,
                target=target,
                detail=detail,
                request_id=request_id,
                ts=utc_now_text(),
            )
        )

    def list_recent(self, limit: int = 100) -> list[AdminAuditLog]:
        """最近的操作，倒序。上限由 `_AUDIT_MAX_ROWS` 兜住。"""
        safe_limit = max(1, min(int(limit), _AUDIT_MAX_ROWS))
        return list(self.session.execute(select(AdminAuditLog).order_by(AdminAuditLog.id.desc()).limit(safe_limit)).scalars().all())
