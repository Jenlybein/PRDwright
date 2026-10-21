"""管理员路由：v4 后台的服务端接口。

本模块当前只有一个「身份自检」接口。它的意义不在返回什么，而在**它是第一个被
`require_admin` 保护起来的接口**——有了它，「非管理员进不去后台」才有一个能被
用例钉住的具体落点，后面的用户管理 / 系统配置 / 日志接口都照此办理。

两层拦截的分工
--------------
- **未登录**：由 `AuthMiddleware` 拦成 401。`/api/admin` 不在白名单里，所以
  匿名请求到不了路由层。
- **已登录但非管理员**：由 `require_admin` 依赖拦成 403。

两者刻意分开：401 让前端跳登录，403 让前端停在无权限页。合成一种的话，
普通用户会被反复踢去登录却永远登不进去。

本模块只做三件事
----------------
1. 解析请求、调用 `admin_service`、渲染响应；
2. 把服务层的业务异常转成正确的状态码；
3. 把「当前是谁」传给服务层——**操作者身份由服务端从会话里取，不由请求体带**，
   否则任何人都能伪造成「是管理员 A 干的」。

业务规则（能不能删、能不能停）一律不写在这里，它们在 `admin_service`。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from backend.api import schemas
from backend.api.deps import require_admin
from backend.core.errors import AppError
from backend.services import admin_service
from backend.services.auth_service import AuthError, User

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ----------------------------------------------------------------------
# 身份与总览
# ----------------------------------------------------------------------


@router.get("/me", response_model=schemas.AdminMeResponse, summary="管理员身份自检")
async def me(user: User = Depends(require_admin)) -> schemas.AdminMeResponse:
    """确认当前登录者**是**管理员。

    前端拿它判定「要不要放行进后台」：200 放行、401 去登录、403 停在无权限页。

    刻意**不**另设一个「返回 `is_admin` 布尔值」的公开接口——那会让
    「我是不是管理员」变成任何人都能问的问题，而这个信息本不该被随意探测。
    想知道答案就去敲管理员接口，让服务端用状态码回答。
    """
    return schemas.AdminMeResponse(id=user.id, username=user.username, is_admin=user.is_admin)


@router.get("/stats", response_model=schemas.AdminStatsResponse, summary="平台总量统计")
async def stats(user: User = Depends(require_admin)) -> schemas.AdminStatsResponse:
    """平台维度的总量：用户数、任务数、Token 合计。

    单独成一条 SQL 而不是让前端把用户列表加总——列表以后可能分页或裁剪，
    拿它算总量会悄悄算少。
    """
    del user  # 无需操作者身份：统计不涉及写操作，也因此不记审计
    return schemas.AdminStatsResponse(**admin_service.platform_stats())


# ----------------------------------------------------------------------
# 审计
# ----------------------------------------------------------------------


@router.get("/audit", response_model=schemas.AdminAuditListResponse, summary="管理员操作记录")
async def audit(limit: int = 100, user: User = Depends(require_admin)) -> schemas.AdminAuditListResponse:
    """最近的管理员操作，倒序。

    它是**问责**用的，与排障用的 `/api/admin/logs`（T4）分列：混在一起时，
    排查线上故障会被大量人为操作淹没，而追责时又会漏在日志风暴里。
    """
    del user
    return schemas.AdminAuditListResponse(items=[schemas.AdminAuditItem(**row) for row in admin_service.list_audit(limit)])


# ----------------------------------------------------------------------
# 用户管理
# ----------------------------------------------------------------------


@router.get("/users", response_model=schemas.AdminUserListResponse, summary="用户列表（含用量）")
async def list_users(user: User = Depends(require_admin)) -> schemas.AdminUserListResponse:
    """列出全部账号及其任务数、Token 合计。

    没有生成过任务的用户也会出现（任务数为 0）——用的是 `LEFT JOIN`，
    否则刚注册的人在列表里会凭空消失。
    """
    del user
    rows = admin_service.list_users()
    return schemas.AdminUserListResponse(users=[schemas.AdminUserItem(**row) for row in rows])


@router.post(
    "/users",
    response_model=schemas.AdminUserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="新建账号",
)
async def create_user(
    payload: schemas.AdminCreateUserRequest,
    user: User = Depends(require_admin),
) -> schemas.AdminUserResponse:
    """后台建号，可一步任命为管理员。

    用户名与密码强度走的是与注册**同一套**校验：后台不该成为弱密码的后门。
    """
    try:
        created = admin_service.create_user(
            user, payload.username, payload.password, is_admin=payload.is_admin
        )
    except AuthError as exc:
        # 用户名已存在、长度不足——都是「请求本身不合规」，400 而非 500
        raise AppError(exc.code, exc.message, status.HTTP_400_BAD_REQUEST) from exc
    return schemas.AdminUserResponse(
        id=created.id,
        username=created.username,
        is_active=created.is_active,
        is_admin=created.is_admin,
    )


@router.patch(
    "/users/{user_id}/active",
    response_model=schemas.AdminUserResponse,
    summary="启停账号",
)
async def set_active(
    user_id: int,
    payload: schemas.AdminSetActiveRequest,
    user: User = Depends(require_admin),
) -> schemas.AdminUserResponse:
    """停用 / 启用账号。

    停用**不等于**删号：数据全留着，只是登不进来，随时可以再启用。
    不能停用自己，也不能让最后一位管理员消失——这两条规则在 `admin_service`。
    """
    target = admin_service.set_active(user, user_id, payload.is_active)
    return schemas.AdminUserResponse(
        id=target.id,
        username=target.username,
        is_active=target.is_active,
        is_admin=target.is_admin,
    )


@router.patch(
    "/users/{user_id}/admin",
    response_model=schemas.AdminUserResponse,
    summary="授予 / 撤销管理员",
)
async def set_admin(
    user_id: int,
    payload: schemas.AdminSetAdminRequest,
    user: User = Depends(require_admin),
) -> schemas.AdminUserResponse:
    """授予或撤销某个账号的管理员身份。

    业务规则只有一条但很关键：**不能撤销最后一位管理员**。
    否则系统里再没有一个人进得了后台，而这个状态无法自助恢复——
    任命管理员本身需要管理员权限，只能靠运维直接改库。

    这条规则由 `auth_service.set_admin` 统一把关，与运维脚本共用同一份实现。
    """
    target = admin_service.set_admin(user, user_id, payload.is_admin)
    return schemas.AdminUserResponse(
        id=target.id,
        username=target.username,
        is_active=target.is_active,
        is_admin=target.is_admin,
    )


@router.post(
    "/users/{user_id}/password",
    response_model=schemas.AdminOkResponse,
    summary="重置密码",
)
async def reset_password(
    user_id: int,
    payload: schemas.AdminResetPasswordRequest,
    user: User = Depends(require_admin),
) -> schemas.AdminOkResponse:
    """重置指定账号的密码，强度要求与注册一致。

    新密码**不写进审计**（只记「已重置」）——审计表是给人翻的，不是保险箱。
    """
    admin_service.reset_password(user, user_id, payload.password)
    return schemas.AdminOkResponse()


@router.delete(
    "/users/{user_id}",
    response_model=schemas.AdminOkResponse,
    summary="删除账号",
)
async def delete_user(user_id: int, user: User = Depends(require_admin)) -> schemas.AdminOkResponse:
    """删除账号，其登录会话一并失效（外键级联），等于强制下线。

    这是**不可逆**的：方案、文档、任务都跟着走。若只是想暂时禁止登录，
    应该用「停用」而不是删除。
    """
    admin_service.delete_user(user, user_id)
    return schemas.AdminOkResponse()


# ----------------------------------------------------------------------
# 邀请码管理
# ----------------------------------------------------------------------


@router.get("/invites", response_model=schemas.AdminInviteListResponse, summary="邀请码列表")
async def list_invites(user: User = Depends(require_admin)) -> schemas.AdminInviteListResponse:
    """全部邀请码及使用情况。表本身就支持多码、次数上限与过期时间。"""
    del user
    return schemas.AdminInviteListResponse(
        invites=[schemas.AdminInviteItem(**row) for row in admin_service.list_invites()]
    )


@router.post(
    "/invites",
    response_model=schemas.AdminInviteResponse,
    status_code=status.HTTP_201_CREATED,
    summary="新建邀请码",
)
async def create_invite(
    payload: schemas.AdminCreateInviteRequest,
    user: User = Depends(require_admin),
) -> schemas.AdminInviteResponse:
    """新建一个邀请码。`max_uses=0` 不限次数，不填过期时间则长期有效。"""
    code = admin_service.create_invite(
        user,
        payload.code,
        max_uses=payload.max_uses,
        expires_at=payload.expires_at,
        note=payload.note,
    )
    return schemas.AdminInviteResponse(code=code)


@router.delete(
    "/invites/{code}",
    response_model=schemas.AdminOkResponse,
    summary="删除邀请码",
)
async def delete_invite(code: str, user: User = Depends(require_admin)) -> schemas.AdminOkResponse:
    """删除邀请码。

    只影响「后面还能不能用它注册」，**不影响已经注册的用户**——那些账号是靠码
    进来的，进来之后就与码无关了。
    """
    admin_service.delete_invite(user, code)
    return schemas.AdminOkResponse()
