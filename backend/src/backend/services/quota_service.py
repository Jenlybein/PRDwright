"""每日额度：解析上限、统计用量、超限拦截。

三条口径（写在最前面，因为它们是这一层唯一容易出错的地方）
------------------------------------------------------------
1. **次数只算「已收尾 + 在飞」**，失败与被取消的不吃额度。
   否则一次上游故障就会把所有人当天的额度清零，而用户完全无从申诉。
2. **token 算全部状态**的实际消耗。跑了就跑掉了，失败的任务同样花了钱。
   这是与第 1 条刻意的差别，别顺手统一成同一个条件。
3. **「每天」由服务端定时区**（`quota_timezone_offset_minutes`，默认东八区）。
   绝不接受前端传值——否则改一下浏览器时区就能把日界挪走、绕开额度。

为什么拦截点在建任务之前
------------------------
放在 `job_service.create_job` 之后的话，被拒的请求会先在 `generation_jobs` 里留下
一行 `failed`。那行会污染 T2 的失败率、也会出现在用户的任务列表里——「我什么都没
生成，却多了一条失败记录」。所以检查必须发生在任何写入之前。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from backend.core.clock import utc_now
from backend.core.engine import session_scope
from backend.core.errors import AppError, ErrorCode
from backend.core.runtime_config import get_runtime_config
from backend.repositories.quota_repo import QuotaRepository
from backend.services.auth_service import User

logger = logging.getLogger(__name__)

# 「跟随全局」的哨兵值，与 `users` 表三列的默认值一致。
FOLLOW_GLOBAL = -1
# 「不限」。
UNLIMITED = 0


@dataclass(frozen=True)
class QuotaLimits:
    """某个用户当前生效的三个上限（0 表示不限）。"""

    daily_tasks: int
    daily_tokens: int
    max_running: int


@dataclass(frozen=True)
class QuotaUsage:
    """某用户今天的用量快照。"""

    tasks: int
    tokens: int
    running: int


def _pick(user_value: int, global_value: int) -> int:
    """三态解析：`-1` 跟随全局，其余（含 `0`）直接用用户自己的值。

    `0` 必须能覆盖全局，否则「这个账号不限」就无法表达——那正是给管理员与可信
    客户开口子的唯一手段。
    """
    return global_value if user_value == FOLLOW_GLOBAL else user_value


def resolve_from(raw: dict[str, int]) -> QuotaLimits:
    """把「用户覆盖值 + 全局默认」换算成生效上限。

    单独拆出来是为了**批量场景**：管理端用户列表已经从库里一次取回了三列覆盖值，
    若每行再调一次 `resolve_limits`（各查一次库），用户一多就是 N 次往返。
    拆出来之后，列表页对已有数据调用本函数，规则仍然只有这一份实现。
    """
    settings = get_runtime_config().settings
    return QuotaLimits(
        daily_tasks=_pick(raw["daily_task_limit"], int(settings.quota_daily_task_limit)),
        daily_tokens=_pick(raw["daily_token_limit"], int(settings.quota_daily_token_limit)),
        max_running=_pick(raw["max_running"], int(settings.quota_max_running)),
    )


def resolve_limits(user_id: int) -> QuotaLimits:
    """算出该用户生效的三个上限：用户覆盖 > 全局默认。"""
    return resolve_from(_quota_columns(user_id))


def _quota_columns(user_id: int) -> dict[str, int]:
    """读该用户的三列覆盖值（短事务：只包住这一次读）。"""
    with session_scope() as session:
        return QuotaRepository(session).quota_columns(user_id)


def day_start_utc() -> str:
    """当前「自然日」起点对应的 UTC 时间串（与 `created_at` 同格式）。

    做法是先把「现在」平移到配置时区，取那里当天 00:00，再平移回 UTC。
    比在 SQL 里对每行做 `datetime(created_at, '+480 minutes')` 快得多：
    那样会让 `created_at` 上的索引用不上（函数包裹后无法走范围扫描）。

    微秒位：`isoformat()` 在微秒为 0 时会省略小数部分，而库里存的是 6 位微秒。
    这没关系——`"…16:00:00" <= "…16:00:00.000001"` 按字典序成立，
    日界当刻与之后的记录都算「今天」，之前的不算。
    """
    offset = int(get_runtime_config().settings.quota_timezone_offset_minutes)
    now_utc = utc_now()
    local_day_start = (now_utc + timedelta(minutes=offset)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return (local_day_start - timedelta(minutes=offset)).isoformat()


def usage_today(user_id: int) -> QuotaUsage:
    """该用户今天的用量。"""
    with session_scope() as session:
        raw = QuotaRepository(session).usage_since(user_id, day_start_utc())
    return QuotaUsage(tasks=raw["tasks"], tokens=raw["tokens"], running=raw["running"])


def _reset_hint() -> str:
    """「额度什么时候恢复」这半句话。

    故意不写死「北京时间」：时区是可配置的，写死之后换个部署环境就在骗人。
    只说「每天 00:00」，配合管理端显示的时区偏移。
    """
    return "额度每天 00:00 重置。"


def _rejection_message(limits: QuotaLimits, usage: QuotaUsage) -> str:
    """挑一条**最相关**的超限原因说，而不是把三个上限一起倒出来。

    同时超了多项时，说「并发满了」比说「你还超了 token」有用得多——
    前者有明确的下一步动作（等一个跑完），后者只能干等到明天。
    """
    if limits.max_running > 0 and usage.running >= limits.max_running:
        return (
            f"同时运行的任务已达上限（{usage.running}/{limits.max_running}），"
            f"请等其中一个完成后再试。"
        )
    if limits.daily_tasks > 0 and usage.tasks >= limits.daily_tasks:
        return (
            f"今日生成次数已用完（{usage.tasks}/{limits.daily_tasks}）。{_reset_hint()}"
        )
    return (
        f"今日 Token 用量已达上限（已用 {usage.tokens}，上限 {limits.daily_tokens}）。"
        f"{_reset_hint()}"
    )


def check(user: User, kind: str) -> None:
    """在创建任务之前校验额度；超限抛 `AppError`（HTTP 429）。

    Args:
        user: 发起人。
        kind: 任务类型。**`chat` 不受配额管**——见下。

    为什么对话不限
    --------------
    对话（澄清、追问、改需求）是生成的**前置步骤**，一次几百 token。把它计入
    每日次数会让「额度用完」变成「连话都不能说」，用户连问「为什么被拒」都做不到。
    重活是文档生成本身，拦那里就够了。

    竞态说明
    --------
    检查与建任务都是同步代码、中间没有 `await`，所以在单进程里它们是原子的。
    多 worker 部署下存在极小窗口（两个请求都读到「还没满」），可能超额 1–2 个任务。
    为这点误差引入跨进程锁不划算：真正防的是「一个人开一百个任务」，不是精确到个位。

    Raises:
        AppError: 超限且 `quota_enforce` 为真。状态码 429。
            错误码刻意复用 `INVALID_REQUEST`——`ErrorCode` 是对外契约，不新增成员；
            前端本来就只展示字符串 `detail`，无需靠码分支。
    """
    if kind == "chat":
        return

    limits = resolve_limits(user.id)
    # 三项都是 0 时（默认）直接返回：这是绝大多数部署的常态，也是「升级后零变化」
    # 的落点。顺带省掉一次不必要的用量查询。
    if limits.daily_tasks <= 0 and limits.daily_tokens <= 0 and limits.max_running <= 0:
        return

    usage = usage_today(user.id)
    reasons: list[str] = []
    if limits.max_running > 0 and usage.running >= limits.max_running:
        reasons.append("running")
    if limits.daily_tasks > 0 and usage.tasks >= limits.daily_tasks:
        reasons.append("tasks")
    if limits.daily_tokens > 0 and usage.tokens >= limits.daily_tokens:
        reasons.append("tokens")
    if not reasons:
        return

    message = _rejection_message(limits, usage)
    # 先记日志再决定拦不拦：`quota_enforce=False` 时这条是唯一的观察手段，
    # 而那时用户侧完全无感（请求照常通过），事后只能靠它复盘用量分布。
    logger.warning(
        "用户 %s 触及每日额度：%s",
        user.username,
        message,
        extra={
            "event": "quota.exceeded",
            "user_id": user.id,
            "kind": kind,
            "reasons": ",".join(reasons),
            "enforced": bool(get_runtime_config().settings.quota_enforce),
            "usage_tasks": usage.tasks,
            "usage_tokens": usage.tokens,
            "usage_running": usage.running,
            "limit_tasks": limits.daily_tasks,
            "limit_tokens": limits.daily_tokens,
            "limit_running": limits.max_running,
        },
    )

    if not get_runtime_config().settings.quota_enforce:
        # 只观察不拦截。日志已经留下，调用方照常继续。
        return

    raise AppError(ErrorCode.INVALID_REQUEST, message, status_code=429)


def source_of(user_value: int) -> str:
    """某个上限的「来源」：`global` 继承全局 / `user` 单独设置 / `off` 显式不限。

    单独暴露出来是为了批量场景（管理端用户列表一次要标几十行）。
    它也是前端编辑回填的依据：`global` → -1，`off` → 0，其余取生效值。
    """
    if user_value == FOLLOW_GLOBAL:
        return "global"
    return "off" if user_value == UNLIMITED else "user"


def describe(user_id: int) -> dict[str, object]:
    """给管理端展示：生效上限、今日用量、每个值来自哪里。

    把「来源」一并回给前端，是为了回答「这个数字是全局默认还是单独给他设的」——
    没有这一列，管理员看到「10」不知道是自己设的（要回去翻）还是继承来的（要算）。
    """
    limits = resolve_limits(user_id)
    own = _quota_columns(user_id)
    usage = usage_today(user_id)
    return {
        "daily_task_limit": limits.daily_tasks,
        "daily_token_limit": limits.daily_tokens,
        "max_running": limits.max_running,
        "daily_task_source": source_of(own["daily_task_limit"]),
        "daily_token_source": source_of(own["daily_token_limit"]),
        "max_running_source": source_of(own["max_running"]),
        "usage_tasks": usage.tasks,
        "usage_tokens": usage.tokens,
        "usage_running": usage.running,
    }
