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

from backend.core.errors import AppError, ErrorCode
from backend.core.request_context import current_request_id
from backend.repositories.admin_repo import AdminStatsRepository, AuditRepository
from backend.repositories.auth_repo import InviteCodeRepository, UserRepository
from backend.repositories.log_repo import LogRepository
from backend.services import auth_service
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


def _audit(actor: User, action: str, target: str = "", detail: str = "") -> None:
    """记一条管理员操作。

    `detail` 里**绝不写密码**：审计表是给人翻的，不是保险箱。
    """
    try:
        AuditRepository.record(
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


def list_audit(limit: int = 100) -> list[dict[str, Any]]:
    """最近的管理员操作，倒序。"""
    return AuditRepository.list_recent(limit)


# ----------------------------------------------------------------------
# 规则校验
# ----------------------------------------------------------------------


def _row_or_404(user_id: int) -> dict[str, Any]:
    """按 id 取用户行，不存在抛 404。"""
    row = UserRepository.get_by_id(user_id)
    if row is None:
        raise AppError(ErrorCode.SOLUTION_NOT_FOUND, "该用户不存在")
    return row


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
    if bool(row.get("is_admin")) and UserRepository.count_admins() <= 1:
        raise AppError(
            ErrorCode.FORBIDDEN,
            f"这是最后一位管理员，不能{verb}——至少保留一位，否则无人能进后台",
        )


# ----------------------------------------------------------------------
# 用户管理
# ----------------------------------------------------------------------


def list_users() -> list[dict[str, Any]]:
    """用户列表，附带每个账号的任务数与 Token 合计。

    统计由 SQL 一次聚合出来，不在 Python 里逐个任务累加——后者会把整张任务表
    读进内存，用户一多就拖垮后台首屏。
    """
    return AdminStatsRepository.user_usage()


def platform_stats() -> dict[str, Any]:
    """平台总量：用户数、管理员数、任务数、成功数、Token 合计。"""
    return AdminStatsRepository.platform_usage()


def daily_stats(days: int = 7, tz_offset_minutes: int = 0) -> dict[str, Any]:
    """按本地日期聚合最近 `days` 天的用量，并附上这段区间的汇总。

    时区偏移由前端按浏览器实际时区传（东八区传 480）。默认 0 即 UTC——
    服务端不猜客户端的时区，猜错会让「按日」整体错位，而且错得很隐蔽。

    **区间汇总在这里算，不交给前端**：失败率若让前端把每天的数字再加一遍，
    就等于同一套口径实现了两次。两处一旦有一处改了口径（比如以后把「运行中」
    也算进分母），页面上的日线和汇总行就会互相矛盾，而没人知道该信哪个。
    """
    items = AdminStatsRepository.daily(days=days, tz_offset_minutes=tz_offset_minutes)

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
    return AdminStatsRepository.top_users(limit=limit)


def create_user(actor: User, username: str, password: str, *, is_admin: bool = False) -> User:
    """新建账号。用户名与密码强度复用注册那一套，后台不开后门。"""
    user = auth_service.create_user_direct(username, password)
    if is_admin:
        UserRepository.set_is_admin(user.id, True)
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

    UserRepository.set_active(user_id, is_active)
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


def delete_user(actor: User, user_id: int) -> None:
    """删除账号。级联效果：其登录会话一并失效（外键 ON DELETE CASCADE）。"""
    row = _row_or_404(user_id)
    _reject_locking_out(actor, row, "删除")

    username = str(row["username"])
    UserRepository.delete(user_id)
    _audit(actor, "user.delete", target=username, detail="账号及其数据已删除")


# ----------------------------------------------------------------------
# 邀请码管理
# ----------------------------------------------------------------------


def list_invites() -> list[dict[str, Any]]:
    """全部邀请码及使用情况。"""
    return InviteCodeRepository.list_all()


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
    if InviteCodeRepository.get(cleaned) is not None:
        raise AppError(ErrorCode.INVALID_REQUEST, "该邀请码已存在", status_code=400)

    moment: datetime | None = None
    if expires_at:
        try:
            moment = datetime.fromisoformat(expires_at.strip())
        except ValueError as exc:
            raise AppError(
                ErrorCode.INVALID_REQUEST, "过期时间格式不正确，请用 ISO 8601", status_code=400
            ) from exc

    InviteCodeRepository.create(cleaned, max_uses=max_uses, expires_at=moment, note=note.strip())
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
    if not InviteCodeRepository.delete(cleaned):
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
    rows, total = LogRepository.search(
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

    items: list[dict[str, Any]] = []
    for row in rows:
        extra: Any = {}
        raw_extra = row.get("extra_json") or ""
        if raw_extra:
            try:
                parsed = json.loads(raw_extra)
                # 只接受对象：历史上有过把数组写进去的可能，非对象一律当空，
                # 免得前端拿到一个数组还要判断类型。
                extra = parsed if isinstance(parsed, dict) else {}
            except (ValueError, TypeError):
                extra = {}
        items.append({**row, "extra": extra})

    return {"items": items, "total": total, "page": safe_page, "page_size": safe_size}
