"""管理员后台的业务规则：管账号、管邀请码，并给每次写操作留痕。

为什么单独一层
--------------
这些动作看似只是「调一下 `auth_service`」，但它们各自带着**只有后台才有的规则**：

- 管理员不能把自己删掉或停用（否则当场把自己锁在门外）；
- 不能让系统里最后一位管理员消失（删号、停用、撤销任一路径都要挡）；
- 每一步都要写审计。

把这些散在路由里，漏一条就是一条静默越权或一次无法追责的删除。集中在一处，
规则才有唯一落点。

**认证（`auth_service`）与授权（本模块）的分工**：前者回答「这个人是谁」，
后者回答「这次操作能不能做」。所以本模块可以复用 `create_user_direct` 建号——
建号本身没有错，错的是「不该由这个人、在这种情形下建」。

审计是旁路
----------
写审计失败只记警告、不抛：审计挂了不该让管理员的删除操作整笔失败。
但也不能静默吞掉——表空了还以为没人操作过，比报错更危险。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from backend.core.clock import utc_now
from backend.core.engine import session_scope
from backend.core.errors import AppError, ErrorCode
from backend.core.request_context import current_request_id
from backend.repositories.account_repo import AccountDataRepository
from backend.repositories.admin_repo import AdminStatsRepository, AuditRepository
from backend.repositories.auth_repo import (
    AuthSessionRepository,
    InviteCodeRepository,
    UserRepository,
)
from backend.repositories.generation_job_repo import STATUS_RUNNING, GenerationJobRepository
from backend.repositories.log_repo import LogRepository
from backend.repositories.quota_repo import QuotaRepository
from backend.services import auth_service, export_service, job_service, quota_service
from backend.services.auth_service import User

logger = logging.getLogger(__name__)

# 邀请码的字符集与长度限制。收紧字符集不是洁癖：码是要被人手抄、贴到聊天窗口里的，
# 一旦允许 `O/0`、`l/1/I` 这类易混字符，注册失败会变成「码明明是对的」的扯皮。
_INVITE_ALLOWED = set("ABCDEFGHJKLMNPQRSTUVWXYZ23456789")
_INVITE_MIN = 4
_INVITE_MAX = 32


# ----------------------------------------------------------------------
# 审计
# ----------------------------------------------------------------------


def record_audit(actor: User, action: str, target: str = "", detail: str = "") -> None:
    """记一条管理员操作。

    `detail` 里**绝不写密码**：审计表是给人翻的，不是保险箱。
    """
    try:
        with session_scope() as session:
            AuditRepository(session).record(
                actor_id=actor.id,
                actor_name=actor.username,
                action=action,
                target=target,
                detail=detail,
                request_id=current_request_id() or None,
            )
    except Exception:  # noqa: BLE001 - 审计失败不能连累业务操作
        logger.warning(
            "管理员操作审计写入失败",
            extra={"event": "admin.audit_failed", "action": action, "target": target},
            exc_info=True,
        )


# 内部沿用旧名：本文件有 7 处调用都写作 `_audit(...)`，改名会引入无谓的 diff。
# 新模块（如 `admin_config_service`）请直接用 `record_audit`。
_audit = record_audit


def list_audit(limit: int = 100) -> list[dict[str, Any]]:
    """最近的管理员操作，倒序。"""
    with session_scope() as session:
        return [row.model_dump() for row in AuditRepository(session).list_recent(limit)]


# ----------------------------------------------------------------------
# 规则校验
# ----------------------------------------------------------------------


def _row_or_404(user_id: int) -> dict[str, Any]:
    """按 id 取用户行（**不含凭据字段**），不存在抛 404。

    投影在仓储层就已经是「除 `password_hash` / `salt` 之外的全部列」——
    这一行会流到导出包里（`export_service.build_zip` 要读额度三列），
    所以既不能少字段、也绝不能把哈希带出去。
    """
    with session_scope() as session:
        user = UserRepository(session).get_by_id(user_id)
        if user is None:
            raise AppError(ErrorCode.SOLUTION_NOT_FOUND, "该用户不存在")
        return {
            "id": int(user.id or 0),
            "username": user.username,
            "created_at": user.created_at,
            "is_active": bool(user.is_active),
            "is_admin": bool(user.is_admin),
            "daily_task_limit": user.daily_task_limit,
            "daily_token_limit": user.daily_token_limit,
            "max_running": user.max_running,
        }


def _reject_locking_out(actor: User, row: dict[str, Any], verb: str) -> None:
    """不许让这次操作把后台变成「无人能进」。

    两个分支守的是同一件事的两面：

    1. **操作者对自己下手**：管理员删掉或停用自己，结果是当场失去后台，
       而恢复只能靠另一个管理员（或者改库）。
    2. **系统里最后一位管理员消失**：撤销、删号、停用三条路都能把管理员清零，
       清零之后没有任何自助恢复手段——任命管理员本身需要管理员权限。

    为什么合在一个函数里：只要操作者是管理员（路由层 `require_admin` 保证），
    分支 1 必然先命中——因为「最后一位」只可能是操作者自己。拆成两个函数会让人
    以为有两条独立规则，其实是一条。保留分支 2 是纵深防御：将来若出现不经过
    HTTP 守卫的调用路径（脚本、后台任务），它才是唯一还在挡的东西。
    """
    if int(row["id"]) == actor.id:
        raise AppError(ErrorCode.FORBIDDEN, f"不能{verb}自己的账号")
    with session_scope() as session:
        admin_count = UserRepository(session).count_admins()
    if bool(row.get("is_admin")) and admin_count <= 1:
        raise AppError(
            ErrorCode.FORBIDDEN,
            f"这是最后一位管理员，不能{verb}——至少保留一位，否则无人能进后台",
        )


# ----------------------------------------------------------------------
# 用户管理
# ----------------------------------------------------------------------


def list_users() -> list[dict[str, Any]]:
    """用户列表，附带每个账号的任务数、Token 合计与**今日额度使用情况**。

    统计由 SQL 一次聚合出来，不在 Python 里逐个任务累加——后者会把整张任务表
    读进内存，用户一多就拖垮后台首屏。

    「今日已用」单独查一次（`usage_today_by_users`），不复用上面那条 JOIN：
    两个聚合的维度不同（累计 vs 今日），塞进同一条 SQL 会互相放大计数
    （多表 JOIN 后聚合相乘，是这类统计最经典的错法）。
    """
    with session_scope() as session:
        rows = [row.as_dict() for row in AdminStatsRepository(session).user_usage()]
    if not rows:
        return []

    # 当日用量：一次 GROUP BY 拿全，而不是每行一次查询
    with session_scope() as session:
        today = QuotaRepository(session).usage_today_by_users(
            [int(row["id"]) for row in rows], quota_service.day_start_utc()
        )

    merged: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        # 原始覆盖值先取出来，后面要换成立生效值 + 来源
        raw = {
            "daily_task_limit": int(item.pop("daily_task_limit", -1)),
            "daily_token_limit": int(item.pop("daily_token_limit", -1)),
            "max_running": int(item.pop("max_running", -1)),
        }
        # 生效上限由 `quota_service` 换算：三态规则只在那一处实现，
        # 这里若自己判 `-1` 就是在写第二份规则。
        limits = quota_service.resolve_from(raw)
        used = today.get(int(item["id"]), {"tasks": 0, "tokens": 0})
        item.update(
            {
                "daily_task_limit": limits.daily_tasks,
                "daily_token_limit": limits.daily_tokens,
                "max_running": limits.max_running,
                # 来源一并回传：前端编辑时需要区分「跟随全局」与「显式设为不限」，
                # 这两种情况生效值可能相同（都等于全局值/0），只看数字无法回填。
                "daily_task_source": quota_service.source_of(raw["daily_task_limit"]),
                "daily_token_source": quota_service.source_of(raw["daily_token_limit"]),
                "max_running_source": quota_service.source_of(raw["max_running"]),
                "usage_tasks_today": used["tasks"],
                "usage_tokens_today": used["tokens"],
            }
        )
        merged.append(item)
    return merged


def set_user_quota(
    actor: User,
    user_id: int,
    *,
    daily_task_limit: int,
    daily_token_limit: int,
    max_running: int,
) -> dict[str, Any]:
    """设置某个用户的额度覆盖值，返回该用户的最新一行。

    `-1` 表示跟随全局，`0` 表示不限，正数表示具体上限——三态的语义与
    `users` 表默认值一致，由 `quota_service` 负责解释，这里只做校验与落库。

    Raises:
        AppError: 用户不存在（404）或取值非法（400）。
    """
    for label, value in (
        ("每日生成次数", daily_task_limit),
        ("每日 Token 上限", daily_token_limit),
        ("同时运行任务数", max_running),
    ):
        # 下界是 -1（跟随全局），上界给一个明显不合理的量级兜底：
        # 真正防的是「多打一个 0」，而不是业务上限。
        if value < -1 or value > 10_000_000:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                f"{label}取值非法：{value}。应为 -1（跟随全局）、0（不限）或正整数。",
                status_code=400,
            )

    before = quota_service.describe(user_id)
    with session_scope() as session:
        hit = QuotaRepository(session).set_quota(
            user_id,
            daily_task_limit=daily_task_limit,
            daily_token_limit=daily_token_limit,
            max_running=max_running,
        )
    if not hit:
        raise AppError(ErrorCode.SOLUTION_NOT_FOUND, "用户不存在", status_code=404)

    # 审计写「改前 → 改后」，只记**有变化的**项：三项全记会让日志里塞满
    # 「10 → 10」这种噪音，而审计的价值恰恰在于能一眼看出改了什么。
    after = quota_service.describe(user_id)
    changes = [
        f"{key} {before[key]} → {after[key]}"
        for key in ("daily_task_limit", "daily_token_limit", "max_running")
        if before[key] != after[key]
    ]
    _audit(
        actor,
        "user.quota",
        target=str(user_id),
        detail="; ".join(changes) or "无变化",
    )
    return next(
        (row for row in list_users() if int(row["id"]) == user_id),
        {"id": user_id},
    )


def platform_stats() -> dict[str, Any]:
    """平台总量：用户数、管理员数、任务数、成功数、Token 合计。"""
    with session_scope() as session:
        return AdminStatsRepository(session).platform_usage().as_dict()


def daily_stats(days: int = 7, tz_offset_minutes: int = 0) -> dict[str, Any]:
    """按本地日期聚合最近 `days` 天的用量，并附上这段区间的汇总。

    时区偏移由前端按浏览器实际时区传（东八区传 480）。默认 0 即 UTC——
    服务端不猜客户端的时区，猜错会让「按日」整体错位，而且错得很隐蔽。

    **区间汇总在这里算，不交给前端**：失败率若让前端把每天的数字再加一遍，
    就等于同一套口径实现了两次。两处一旦有一处改了口径（比如以后把「运行中」
    也算进分母），页面上的日线和汇总行就会互相矛盾，而没人知道该信哪个。
    """
    with session_scope() as session:
        items = [
            row.as_dict()
            for row in AdminStatsRepository(session).daily(
                days=days, tz_offset_minutes=tz_offset_minutes
            )
        ]

    def _sum(key: str) -> int:
        return sum(int(row[key]) for row in items)

    completed = _sum("completed_count")
    failed = _sum("failed_count")
    finished = completed + failed
    summary = {
        "task_count": _sum("task_count"),
        "completed_count": completed,
        "failed_count": failed,
        "cancelled_count": _sum("cancelled_count"),
        "running_count": _sum("running_count"),
        "prompt_tokens": _sum("prompt_tokens"),
        "completion_tokens": _sum("completion_tokens"),
        # 区间失败率用**加权**算法（区间失败 / 区间已收尾），不是「每天率的平均」：
        # 后者会让任务量少的那天与任务量大的那天等权，把真实的失败率算偏。
        "failure_rate": round(failed / finished, 4) if finished else None,
    }
    return {"items": items, "summary": summary}


def top_users(limit: int = 10) -> list[dict[str, Any]]:
    """用量最高的用户（只含真的生成过任务的人）。"""
    with session_scope() as session:
        return [row.as_dict() for row in AdminStatsRepository(session).top_users(limit=limit)]


def create_user(actor: User, username: str, password: str, *, is_admin: bool = False) -> User:
    """新建账号。用户名与密码强度复用注册那一套，后台不开后门。"""
    user = auth_service.create_user_direct(username, password)
    if is_admin:
        with session_scope() as session:
            UserRepository(session).set_is_admin(user.id, True)
        user = auth_service.get_user(user.id) or user

    _audit(
        actor,
        "user.create",
        target=user.username,
        detail="管理员" if is_admin else "普通用户",
    )
    return user


def set_active(actor: User, user_id: int, is_active: bool) -> User:
    """停用 / 启用账号。

    停用**不等于**删号：数据全留着，只是登不进来，随时能重新启用。
    """
    row = _row_or_404(user_id)
    if not is_active:
        _reject_locking_out(actor, row, "停用")

    with session_scope() as session:
        UserRepository(session).set_active(user_id, is_active)
    user = auth_service.get_user(user_id) or User(
        id=user_id,
        username=str(row["username"]),
        is_active=is_active,
        is_admin=bool(row.get("is_admin")),
        created_at=str(row.get("created_at", "")),
    )
    _audit(actor, "user.active", target=user.username, detail="启用" if is_active else "停用")
    return user


def reset_password(actor: User, user_id: int, password: str) -> None:
    """重置密码。强度校验走注册那一套，后台不放宽。"""
    row = _row_or_404(user_id)
    username = str(row["username"])
    auth_service.reset_password(username, password)
    # 只记「改过了」，不记改成了什么
    _audit(actor, "user.password", target=username, detail="已重置")


def revoke_sessions(actor: User, user_id: int) -> int:
    """强制下线：吊销该用户的全部会话，返回实际吊销条数。

    为什么是「吊销会话」而不是「改密码」或「停用」
    --------------------------------------------
    改密码会顺带把密码换掉，停用会连新登录一起禁掉——两者都超出了「把这个人现在
    的登录踢掉」这个诉求。只删会话，他下次请求就 401，但拿着正确密码仍能立刻登录回来，
    这是「踢下线」而不是「封号」，两者在后台是分开的按钮。

    ⚠️ 返回值可能是 0
    -----------------
    登录是单点语义（`replace_for_user` 会先删旧会话），所以正常情况每人**至多一条**。
    返回 0 表示"他本来就没登录"，不是失败——把条数返回给前端，管理员才分得清
    「踢了 1 条」和「他压根没在线」。若这里只回 `ok`，两种情况在界面上无法区分。

    ⚠️ 正在跑的任务不会被终止
    ------------------------
    任务与连接本就解耦（刷新页面不会中断生成），所以吊销会话只影响下一个 HTTP 请求。
    真想停掉在飞任务，用任务监控页的「取消」。

    Args:
        actor: 执行操作的管理员（写进审计）。
        user_id: 目标用户。

    Raises:
        AppError: 用户不存在（404）。
    """
    row = _row_or_404(user_id)
    username = str(row["username"])
    with session_scope() as session:
        revoked = AuthSessionRepository(session).delete_by_user(user_id)
    _audit(actor, "user.revoke_sessions", target=username, detail=f"吊销 {revoked} 条会话")
    return revoked


def set_admin(actor: User, user_id: int, is_admin: bool) -> User:
    """授予 / 撤销管理员身份。

    **业务规则只有一条**，由 `auth_service.set_admin` 独家实现：
    不能撤销最后一位管理员。后台与运维脚本（`auth_cli.py demote`）走的是
    **同一个函数**——两条路各写一遍校验，迟早有一处漏改，后台就成了
    绕过不变式的后门。所以这里只做委派，不重复实现任何校验。

    「撤销自己」**不在**这里拦：只要还有别的管理员，撤销自己就是一次
    正常的「卸任」，而且可逆（另一位管理员能重新任命）。拦它的是那条
    不变式本身——如果自己就是最后一位，撤销自己等于清空管理员。
    """
    row = _row_or_404(user_id)
    username = str(row["username"])

    try:
        user = auth_service.set_admin(username, is_admin)
    except auth_service.AuthError as exc:
        # 「最后一位管理员」是权限层面的拒绝，与删号/停用最后一位管理员同一口径，
        # 所以给 FORBIDDEN(403) 而不是 AUTH_FAILED(401)——401 在前端意味着
        # 「该去登录了」，而这里登录也没用。
        raise AppError(ErrorCode.FORBIDDEN, exc.message) from exc

    _audit(
        actor,
        "user.admin",
        target=username,
        detail="设为管理员" if is_admin else "撤销管理员身份",
    )
    return user


def deletion_preview(user_id: int) -> dict[str, Any]:
    """删除前先算清楚「会删掉什么、会留下什么」，供确认面板展示。

    为什么要有这一步
    ----------------
    删除是**不可逆**的，而原先界面上只有一句「确认删除」——管理员并不知道自己点掉的
    是 1 份方案还是 30 份。更要紧的是「删了但没删干净」（审计与日志按设计会保留），
    如果不提前说明，事后看到日志里还有这个人的记录会以为是没删干净。

    ⚠️ 删账号会让平台历史统计变小
    ---------------------------
    `generation_jobs` 是级联删除的，所以删完之后「总任务数 / 总 Token」以及过去某天的
    图表都会一起减少。这是「彻底删除」的必然结果（合规要求就是「当我没来过」），
    但必须让管理员在点之前就知道，否则会以为统计坏了。前端确认面板里写了这句。

    Raises:
        AppError: 用户不存在（404）。
    """
    row = _row_or_404(user_id)
    with session_scope() as session:
        counts = AccountDataRepository(session).counts(user_id)
    return {"user_id": int(row["id"]), "username": str(row["username"]), **counts}


def export_user_data(actor: User, user_id: int) -> tuple[bytes, str, int]:
    """把一个账号的数据打成一个 ZIP 包，返回 `(内容, 文件名, 包内文件数)`。

    读别人的数据必须留痕
    --------------------
    导出是只读操作，但它**把某人的全部内容搬出了系统**。审计里记一条 `user.export`
    （含条数与体积），事后才能回答「谁在什么时候导走了谁的数据」。
    这也是为什么本函数要 `actor` 而不是只留 `user_id`。

    Raises:
        AppError: 用户不存在（404）或数据超过导出上限（413）。
    """
    row = _row_or_404(user_id)
    payload, filename, file_count = export_service.build_zip(row, exported_by=actor.username)
    _audit(
        actor,
        "user.export",
        target=str(row["username"]),
        detail=f"导出 {file_count} 个文件，{len(payload) // 1024} KB",
    )
    return payload, filename, file_count


def delete_user(actor: User, user_id: int) -> None:
    """彻底删除账号：先停任务，再删数据，最后清掉不随账号走的那点残留。

    顺序是有讲究的
    --------------
    1. **先取消在飞任务**。任务与连接解耦，删号不会让在途协程自己停下；不停的话它会
       继续调上游烧 token，直到跑完才发现没有归属可写。
    2. **再删 `users` 行**。会话、方案、文档与版本、对话轮次、任务记录都靠外键
       `ON DELETE CASCADE` 一起走——级联必须在库里，不能指望调用方逐个记得。
    3. **最后清 `login_failures`**。这张表按**用户名**记，没有外键、不参与级联；
       不清的话，删掉一个失败过 N 次的 `alice`、再注册一个同名 `alice`，
       新账号一上来就被延时——它继承了一笔没欠过的账。

    **不删**的两处（有意保留）：`admin_audit_log`（账号没了更要有据可查）、
    `app_logs`（运维记录，不是用户业务数据）。

    Raises:
        AppError: 用户不存在（404）、操作者删自己或最后一位管理员（403）。
    """
    row = _row_or_404(user_id)
    _reject_locking_out(actor, row, "删除")

    username = str(row["username"])
    cancelled = job_service.cancel_for_user(user_id)
    with session_scope() as session:
        UserRepository(session).delete(user_id)
    cleared = auth_service.clear_login_failures(username)
    _audit(
        actor,
        "user.delete",
        target=username,
        detail=f"账号及其数据已删除；取消在飞任务 {cancelled} 个，清理登录失败记录 {cleared} 条",
    )


# ----------------------------------------------------------------------
# 邀请码管理
# ----------------------------------------------------------------------


def list_invites() -> list[dict[str, Any]]:
    """全部邀请码及使用情况。"""
    with session_scope() as session:
        return [row.model_dump() for row in InviteCodeRepository(session).list_all()]


def _normalize_code(code: str) -> str:
    """清洗邀请码并校验字符集。

    刻意**不转大写**：已发出的码是什么样就存什么样，中途改大小写会让
    「用户复制了旧邮件里的码」突然失效，而这种失效没有任何提示。
    """
    cleaned = code.strip()
    if not (_INVITE_MIN <= len(cleaned) <= _INVITE_MAX):
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            f"邀请码长度需在 {_INVITE_MIN}-{_INVITE_MAX} 个字符之间",
            status_code=400,
        )
    if not set(cleaned) <= _INVITE_ALLOWED:
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            "邀请码只能包含大写字母与数字（已排除 O/0、I/1 等易混字符）",
            status_code=400,
        )
    return cleaned


def create_invite(
    actor: User,
    code: str,
    *,
    max_uses: int = 0,
    expires_at: str | None = None,
    note: str = "",
) -> str:
    """新建邀请码。

    `max_uses=0` 表示不限次数，`expires_at=None` 表示永不过期——沿用
    `InviteCodeRepository.create` 的既有语义，不另造一套。
    """
    cleaned = _normalize_code(code)
    if max_uses < 0:
        raise AppError(ErrorCode.INVALID_REQUEST, "使用次数不能为负数", status_code=400)

    moment: datetime | None = None
    if expires_at:
        try:
            moment = datetime.fromisoformat(expires_at.strip())
        except ValueError as exc:
            raise AppError(
                ErrorCode.INVALID_REQUEST, "过期时间格式不正确，请用 ISO 8601", status_code=400
            ) from exc

    # 「查重 + 写入」一个事务：拆开的话，两个管理员同时建同一个码，
    # 都能查到「不存在」，于是第二个请求会撞在唯一约束上变成 500，
    # 而不是得到那句本该给的「该邀请码已存在」。
    with session_scope() as session:
        invites = InviteCodeRepository(session)
        if invites.get(cleaned) is not None:
            raise AppError(ErrorCode.INVALID_REQUEST, "该邀请码已存在", status_code=400)
        invites.create(cleaned, max_uses=max_uses, expires_at=moment, note=note.strip())

    _audit(
        actor,
        "invite.create",
        target=cleaned,
        detail=f"上限 {max_uses or '不限'} 次，{'过期 ' + expires_at if moment else '长期有效'}",
    )
    return cleaned


def delete_invite(actor: User, code: str) -> None:
    """删除邀请码。

    只删码本身，不影响已注册的用户：那些账号是靠码进来的，但进来之后就与码无关了。
    """
    cleaned = code.strip()
    with session_scope() as session:
        deleted = InviteCodeRepository(session).delete(cleaned)
    if not deleted:
        raise AppError(ErrorCode.SOLUTION_NOT_FOUND, "该邀请码不存在")
    _audit(actor, "invite.delete", target=cleaned, detail="已删除")


# ----------------------------------------------------------------------
# 日志检索（v4 T4）
# ----------------------------------------------------------------------


def list_logs(
    *,
    level: str = "",
    since: str = "",
    until: str = "",
    logger_name: str = "",
    event: str = "",
    request_id: str = "",
    user_id: int | None = None,
    keyword: str = "",
    exclude_event: str = "",
    page: int = 1,
    page_size: int = 100,
) -> dict[str, Any]:
    """检索 `app_logs`，返回 `{items, total, page, page_size}`。

    为什么分页在服务端算
    --------------------
    日志是**只增不减**的表，量级远超用户或任务。前端拉全量再切片，等于每次翻页
    都把整张表搬过网络——第一次打开日志页就会卡住，而那时恰恰是最需要它的时候。

    `extra_json` 在这里**解析成对象**再返回，而不是把那段字符串原样丢给前端：
    后端知道它是什么（`json.dumps` 写的），前端不该为了看一个 `job_id`
    再写一遍 JSON 解析与容错。解析失败给 `{}` 而不是抛错——一条格式异常的旧日志
    不该让整个列表 500。

    Args:
        level / since / until / logger_name / event / request_id / user_id / keyword:
            过滤条件，全部可选（空串视为不筛）。
        exclude_event: 要排除的事件名（如 `request.completed`）。访问日志每个请求一条，
            量级压过业务事件，默认排掉它，日志页才看得见「生成发生了什么」。
        page: 第几页，从 1 开始。
        page_size: 每页条数。

    Returns:
        `{"items": [...], "total": int, "page": int, "page_size": int}`
    """
    safe_page = max(1, int(page))
    safe_size = max(1, min(int(page_size), 500))
    with session_scope() as session:
        rows, total = LogRepository(session).search(
            level=level or None,
            since=since or None,
            until=until or None,
            logger_name=logger_name or None,
            event=event or None,
            request_id=request_id or None,
            user_id=user_id,
            keyword=keyword or None,
            exclude_event=exclude_event or None,
            limit=safe_size,
            offset=(safe_page - 1) * safe_size,
        )
        # 出了 scope 再读属性也是安全的（`expire_on_commit=False`），但 `extra` 的
        # 解析要逐行做，放在块内可以少一次「对象过期」的意外。
        items: list[dict[str, Any]] = []
        for row in rows:
            extra: Any = {}
            raw_extra = row.extra_json or ""
            if raw_extra:
                try:
                    parsed = json.loads(raw_extra)
                    # 只接受对象：历史上有过把数组写进去的可能，非对象一律当空，
                    # 免得前端拿到一个数组还要判断类型。
                    extra = parsed if isinstance(parsed, dict) else {}
                except (ValueError, TypeError):
                    extra = {}
            items.append({**row.model_dump(), "extra": extra})

    return {"items": items, "total": total, "page": safe_page, "page_size": safe_size}


# ----------------------------------------------------------------------
# 任务监控（v4.5 T5-1）
# ----------------------------------------------------------------------

# 卡死判定阈值：running 任务超过这么久没有更新，就认为它「卡住了」。
#
# 为什么看 `updated_at` 而不是 `created_at`：正文在流式写入时 `updated_at`
# 会持续刷新，它停住不动恰恰说明「不再产出」。半小时足以覆盖长文档里最慢的
# 单章生成；再短会把正常的长任务误报成卡死，管理员就会对红色标记脱敏。
STUCK_MINUTES = 30


def _parse_ts(value: Any) -> datetime | None:
    """解析库里的 UTC ISO 串（`utc_now_text()` 写的，不带时区标记）。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _job_view(row: dict[str, Any]) -> dict[str, Any]:
    """把一行任务补上「耗时 / 是否卡死」，供前端直接展示。

    耗时与卡死**在这里算一次**。前端再算一遍就会出现两套口径：浏览器与服务器
    的时钟差几分钟，卡死边界就会对不上，而管理员只会看到「列表说卡死、点了
    却说还在跑」这种互相矛盾的状态。
    """
    created = _parse_ts(row.get("created_at"))
    updated = _parse_ts(row.get("updated_at"))
    now = utc_now()

    duration = int((now - created).total_seconds()) if created else 0
    is_stuck = False
    stuck_minutes = 0
    if row.get("status") == STATUS_RUNNING and updated is not None:
        idle = int((now - updated).total_seconds())
        if idle >= STUCK_MINUTES * 60:
            is_stuck = True
            stuck_minutes = idle // 60

    return {
        "id": int(row["id"]),
        "owner_id": int(row["owner_id"]),
        "owner_name": str(row.get("owner_name") or ""),
        "kind": str(row.get("kind") or ""),
        "status": str(row.get("status") or ""),
        "phase": str(row.get("phase") or ""),
        "created_at": str(row.get("created_at") or ""),
        "updated_at": str(row.get("updated_at") or ""),
        "duration_seconds": max(0, duration),
        "is_stuck": is_stuck,
        "stuck_minutes": stuck_minutes,
        "prompt_tokens": int(row.get("prompt_tokens") or 0),
        "completion_tokens": int(row.get("completion_tokens") or 0),
        "error_code": str(row.get("error_code") or ""),
        "error_message": str(row.get("error_message") or ""),
    }


def list_jobs(
    *,
    status: str = "",
    username: str = "",
    kind: str = "",
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """任务监控列表：在飞 / 最近失败 / 全部（可按用户名、类型过滤）。

    Returns:
        `{"items": [...], "total": int}`。`total` 是同口径命中总数，
        前端据此判断「还有多少没显示」。
    """
    safe_limit = max(1, min(int(limit), 500))
    safe_offset = max(0, int(offset))
    clean_username = username.strip() or None
    with session_scope() as session:
        rows = GenerationJobRepository(session).admin_list(
            status=status or None,
            username=clean_username,
            kind=kind or None,
            limit=safe_limit,
            offset=safe_offset,
        )
        total = GenerationJobRepository(session).admin_count(
            status=status or None,
            username=clean_username,
            kind=kind or None,
        )
        items = [_job_view(row.as_dict()) for row in rows]
    return {"items": items, "total": total}


def cancel_job_as_admin(actor: User, job_id: int) -> bool:
    """管理员手动取消一次任务，返回是否确实取消了仍在运行的那一次。

    三种结果要分清：

    - **任务不存在** → 404。静默成功会让管理员以为「已经掐掉了」；
    - **任务已结束** → 返回 False（「你点的时候它刚好说完」不是失败）；
    - **真正取消** → 走 `job_service.cancel_job`，它同时标记内存运行时
      （在途协程下一个 await 点跳出）并落库终态。

    ⚠️ 取消是**协作式**的，不是强杀：卡在纯同步阻塞调用上的任务不会立刻退出。
    但库里已是终态，重启后不会再显示「生成中」——卡死最多是临时的。
    """
    with session_scope() as session:
        admin_job = GenerationJobRepository(session).get_for_admin(job_id)
        row = admin_job.as_dict() if admin_job else None
    if row is None:
        raise AppError(ErrorCode.SOLUTION_NOT_FOUND, "该任务不存在", status_code=404)

    owner_id = int(row["owner_id"])
    # 与用户点「停止」共用同一条链路；只换掉 error_message 的措辞，
    # 让运维在任务详情里能区分「用户放弃」与「管理员清理」。
    changed = job_service.cancel_job(owner_id, job_id, message="管理员手动停止了任务")
    if changed:
        _audit(
            actor,
            "job.cancel",
            target=str(job_id),
            detail=f"取消 {row.get('owner_name') or owner_id} 的 {row.get('kind') or ''} 任务",
        )
    return changed


def cancel_stuck_jobs(actor: User) -> int:
    """取消所有「running 且已停更超过阈值」的任务，返回实际取消条数。

    只动**判定为卡死**的那些：正常在跑的任务一个都不碰——批量取消一旦误伤，
    用户的正文会直接断在半截，比卡死更难解释。
    """
    # running 按 created_at 升序，卡死（最久）的必然排在最前；500 条足够覆盖
    # 单机在飞任务的实际量级。
    with session_scope() as session:
        rows = GenerationJobRepository(session).admin_list(status=STATUS_RUNNING, limit=500)
        views = [_job_view(row.as_dict()) for row in rows]
    stuck = [view for view in views if view["is_stuck"]]
    if not stuck:
        return 0

    cancelled = 0
    for item in stuck:
        if job_service.cancel_job(
            int(item["owner_id"]), int(item["id"]), message="管理员清理卡死任务"
        ):
            cancelled += 1

    if cancelled:
        _audit(actor, "job.cancel_stuck", detail=f"清理卡死任务 {cancelled} 个")
    return cancelled
