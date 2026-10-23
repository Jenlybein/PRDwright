"""每日额度的读写：用量聚合 + 用户覆盖值。

为什么单独一个仓储
------------------
额度这一件事同时要读**两张表**：额度上限在 `users`（三列覆盖值），
实际用量在 `generation_jobs`（按 owner 聚合）。放进任何一个既有仓储都会让它
变成「顺便管着另一个领域」；而数据访问又必须留在 `repositories/`（分层纪律）。
所以按**关注点**而不是按表来切：这个文件里的查询全部只服务于额度判断。

为什么不在内存里计数
--------------------
进程内计数器在多 worker 下各记各的，重启即归零，而额度恰恰是「越界就拒人」的
判断——用自己的缓存去判，会出现「这个 worker 认为你还有额度、那个认为没有」。
直接聚合库里的任务表，是唯一一份真源。

## 形态（v6 / T16.6）

从前是模块级函数，现在收进 `QuotaRepository`（绑定 `Session` 的实例方法）：
额度判断与「创建任务」必须在同一个事务里看到彼此，模块级函数各自开连接做不到。
**本类不 commit / rollback / close**。

这里的方法返回的是**聚合结果**（字典）而不是模型对象：它们没有对应的行，
返回一个 `GenerationJob` 反而是在编造。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import case, func, select
from sqlmodel import Session

from backend.core.models import GenerationJob, User

# 计入「今日次数」的状态。
#
# ⚠️ **不含 `failed`**：被取消（`CANCELLED`）、被新任务接管（`SUPERSEDED`）
# 以及上游故障导致失败的任务，都不该吃掉用户的额度——那等于「系统抖动一次，
# 所有人今天少一次机会」，用户完全无从申诉。防滥用靠并发上限与日志，不靠罚失败。
#
# ⚠️ **含 `running`**：在飞任务已经占用了名额。不计的话，连发 N 个请求都会通过
# 检查（每个都还没落终态），而这正是限额要防的事。
_COUNTED_STATUSES = ("completed", "running")


def _task_count_expression():
    """「算作已用次数」的聚合表达式。"""
    return func.sum(case((GenerationJob.status.in_(_COUNTED_STATUSES), 1), else_=0))


def _token_sum_expression():
    """消耗的 token 合计（无行时为 0）。"""
    return func.coalesce(
        func.sum(GenerationJob.prompt_tokens + GenerationJob.completion_tokens), 0
    )


class QuotaRepository:
    """额度相关的读写（绑定一个 `Session`）。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def usage_since(self, user_id: int, since: str) -> dict[str, Any]:
        """统计某用户自 `since`（UTC ISO 串）以来的用量，以及当前在飞任务数。

        Args:
            user_id: 归属用户。
            since: UTC 时间下界的 ISO 字符串（与 `created_at` 同格式，字典序即时间序）。

        Returns:
            `{"tasks": 已占用次数, "tokens": 已消耗 token, "running": 在飞任务数}`。

        ⚠️ `running` **不受 `since` 限制**：一个任务可能昨天发起、现在仍在跑
        （长文档生成确实会跨日）。若也跟着日界过滤，跨日任务会从并发上限里消失，
        于是同一个人能同时开「昨天那批 + 今天这批」。
        """
        # token 用**无条件求和**：跑了就跑掉了，失败或被取消的任务同样花钱。
        # 这是「次数」与「token」两条口径的关键差别，别顺手统一。
        row = self.session.execute(
            select(
                _task_count_expression().label("tasks"),
                _token_sum_expression().label("tokens"),
            ).where(
                GenerationJob.owner_id == user_id,
                GenerationJob.created_at >= since,
            )
        ).one()

        live = self.session.execute(
            select(func.count())
            .select_from(GenerationJob)
            .where(
                GenerationJob.owner_id == user_id,
                GenerationJob.status == "running",
            )
        ).one()

        return {
            "tasks": int(row.tasks or 0),
            "tokens": int(row.tokens or 0),
            "running": int(live[0] or 0),
        }

    def usage_today_by_users(
        self, user_ids: list[int], since: str
    ) -> dict[int, dict[str, int]]:
        """批量版 `usage_since`：一次查出多个用户的当日次数与 token。

        管理端用户列表要给每一行显示「今天用了多少」，逐个查就是 N 次往返；
        用户一多首屏会明显变慢。这里一次 `GROUP BY` 拿全。

        ⚠️ 刻意**不含 `running`**：列表页只关心「今天用掉多少额度」，
        而在飞数量与列表里已有 `task_count` 是不同的口径（一个是今天、一个是累计），
        混在一起最容易让人把两个数读成矛盾。
        """
        if not user_ids:
            return {}
        rows = self.session.execute(
            select(
                GenerationJob.owner_id.label("owner_id"),
                _task_count_expression().label("tasks"),
                _token_sum_expression().label("tokens"),
            )
            .where(
                GenerationJob.owner_id.in_(user_ids),
                GenerationJob.created_at >= since,
            )
            .group_by(GenerationJob.owner_id)
        ).all()
        return {
            int(row.owner_id): {"tasks": int(row.tasks or 0), "tokens": int(row.tokens or 0)}
            for row in rows
        }

    def quota_columns(self, user_id: int) -> dict[str, int]:
        """读用户的三列覆盖值；用户不存在或列缺失时一律给 `-1`（跟随全局）。

        失败时给 `-1` 而不是抛错：这是**读额度**，读不到就走全局默认，
        比让整个创建任务接口 500 更合理。真要出问题，`-1` 会退化成全局值，
        而全局默认是 0（不限）——方向是「放行」，与「判断出错时拒绝」相反，
        但这里的风险是可接受的：拿不到覆盖值最坏只是没享受到限制，而不是误伤用户。
        """
        user = self.session.get(User, user_id)
        if user is None:
            return {"daily_task_limit": -1, "daily_token_limit": -1, "max_running": -1}
        return {
            "daily_task_limit": _int_or_default(user.daily_task_limit),
            "daily_token_limit": _int_or_default(user.daily_token_limit),
            "max_running": _int_or_default(user.max_running),
        }

    def set_quota(
        self,
        user_id: int,
        *,
        daily_task_limit: int,
        daily_token_limit: int,
        max_running: int,
    ) -> bool:
        """写入用户的三列覆盖值；返回是否命中了一行（用户是否存在）。"""
        user = self.session.get(User, user_id)
        if user is None:
            return False
        user.daily_task_limit = int(daily_task_limit)
        user.daily_token_limit = int(daily_token_limit)
        user.max_running = int(max_running)
        self.session.add(user)
        # 不 commit：提交归一调用方的 session_scope()。
        # 但必须 flush 一次再判「有没有命中」，否则下面的返回值和「写入已经生效」
        # 之间会隔着一个没人知道的时机。
        self.session.flush()
        return True


def _int_or_default(value: Any) -> int:
    """把列值折成 int；读不出来时给 `-1`（跟随全局）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1
