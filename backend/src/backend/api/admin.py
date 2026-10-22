"""管理员路由：v4 后台的服务端接口。

本模块有两个「身份」落点：`POST /api/admin/login`（**进后台的唯一入口**）与
`GET /api/admin/me`（身份自检）。后者本身不返回什么有用的数据，它的价值在于
「它是第一个被 `require_admin` 保护起来的接口」——「非管理员进不去后台」因此有了
一个能被用例钉住的具体落点。

两层拦截的分工
--------------
- **未登录**：由 `AuthMiddleware` 拦成 401。`/api/admin/login` 是白名单里唯一的例外
  （登录这件事本身不能要求先登录），其余 `/api/admin/**` 的匿名请求到不了路由层。
- **已登录但不是管理员**：由 `require_admin` 依赖拦成 403。

状态码仍然分开（401 让前端去登录、403 说明「这个身份不行」），但**文案已经统一**：
非管理员看到的是「账号不存在」而不是「权限不足」——后台对外只承认管理员账号存在
（ADR-049）。管理端前端据此把这种情况与「没登录」呈现为同一页（登录页），
不再有「你登录了，但你不是管理员」这种既泄露又说不出解决办法的页面。

本模块只做三件事
----------------
1. 解析请求、调用服务层、渲染响应；
2. 把服务层的业务异常转成正确的状态码；
3. 把「当前是谁」传给服务层——**操作者身份由服务端从会话里取，不由请求体带**，
   否则任何人都能伪造成「是管理员 A 干的」。

业务规则（能不能删、能不能停）一律不写在这里，它们在 `admin_service`。
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, Response, status

from backend.api import schemas
from backend.api.deps import require_admin
from backend.api.login_flow import set_session_cookie, wait_login_delay
from backend.core.errors import AppError
from backend.core.providers import PRESETS
from backend.services import admin_config_service, admin_service, auth_service
from backend.services.auth_service import AuthError, User

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ----------------------------------------------------------------------
# 身份与总览
# ----------------------------------------------------------------------


@router.post("/login", response_model=schemas.AuthUserResponse, summary="管理员登录")
async def login(
    payload: schemas.LoginRequest,
    request: Request,
    response: Response,
) -> schemas.AuthUserResponse:
    """管理后台的唯一登录入口：**只认管理员账号**。

    为什么单开一条而不是复用 `/api/auth/login`
    -----------------------------------------
    复用那条接口意味着：普通账号在后台登录页输对密码后**会真的登进主站**
    （两个前端共用同一份 Cookie），随后才发现自己进不了后台。这既是误导，
    也在用户端与后台之间留下一条「后台顺手帮你登录了另一个产品」的暗路。
    这里改成**先判身份、再决定要不要签发会话**：非管理员一个字节的凭据都不下发，
    也不会顶掉他在主站上的那条会话。

    三处口径与用户端完全共用同一份 `auth_service.login`：密码错/用户不存在 →
    「用户名或密码错误」；账号停用 → 「该账号已被停用」；渐进延时同样在路由层 `await`。
    差异只有一处：**密码正确但不是管理员 → 401「账号不存在」**（ADR-049）。
    """
    await wait_login_delay(payload.username)

    client_ip = request.client.host if request.client else ""
    try:
        user, credential, _ = auth_service.login(
            payload.username, payload.password, ip=client_ip, require_admin=True
        )
    except AuthError as exc:
        # 一律 401：语义是「这次登不进去」，不是「你缺某个功能的权限」
        raise AppError(exc.code, exc.message, status.HTTP_401_UNAUTHORIZED) from exc

    set_session_cookie(response, credential)
    return schemas.AuthUserResponse(id=user.id, username=user.username)


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


@router.get(
    "/stats/daily",
    response_model=schemas.AdminDailyStatsResponse,
    summary="按日用量趋势",
)
async def stats_daily(
    days: int = 7,
    tz_offset_minutes: int = 0,
    user: User = Depends(require_admin),
) -> schemas.AdminDailyStatsResponse:
    """最近 N 天的每日用量（生成数 / 失败率 / Token）。

    `tz_offset_minutes` 由前端按浏览器实际时区传（东八区 480）。**服务端不猜时区**：
    猜错会让整条「按日」错位，而且错得很隐蔽——数字都合理，只是每一天都挪了格。

    没有任务的那天也会返回（各项为 0）：折线图若跳过空白日，会把「那天没人用」
    画成一条平滑斜线，看起来像用量在缓慢变化。
    """
    del user
    data = admin_service.daily_stats(days=days, tz_offset_minutes=tz_offset_minutes)
    summary = data.get("summary") or {}
    return schemas.AdminDailyStatsResponse(
        items=[schemas.AdminDailyStat(**row) for row in data["items"]],
        # 汇总行的 `day` 不是某一天，用空串明确表示「这是整段区间」
        summary=schemas.AdminDailyStat(day="", **summary) if summary else None,
        days=len(data["items"]),
    )


@router.get(
    "/stats/users",
    response_model=schemas.AdminTopUsersResponse,
    summary="用户用量排行",
)
async def stats_users(
    limit: int = 10,
    user: User = Depends(require_admin),
) -> schemas.AdminTopUsersResponse:
    """用量最高的用户。

    口径与 `/users` 列表**同源**（共用仓储里的同一段聚合表达式）：两处各算一遍，
    迟早会给出两个数，而管理员只会更困惑。
    """
    del user
    return schemas.AdminTopUsersResponse(
        users=[schemas.AdminTopUserItem(**row) for row in admin_service.top_users(limit)]
    )


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
    "/users/{user_id}/quota",
    response_model=schemas.AdminUserItem,
    summary="设置用户的每日额度",
)
async def set_user_quota(
    user_id: int,
    payload: schemas.AdminSetQuotaRequest,
    user: User = Depends(require_admin),
) -> schemas.AdminUserItem:
    """设置某个用户的额度覆盖值，**下一次生成即生效**。

    三个字段同语义：`-1` 跟随全局（`/config` 的「每日额度」分组），
    `0` 不限，正整数为具体上限。

    `-1` 必须能显式传：它是「把这个人恢复成跟大家一样」，与「给他不限」
    是两种完全不同的意图，只靠空值表达不出区别。

    返回更新后的**完整用户行**（含生效值与来源），前端可以直接替换列表里那一行，
    并据此复核「我提交的」与「实际生效的」是否一致。
    """
    row = admin_service.set_user_quota(
        user,
        user_id,
        daily_task_limit=payload.daily_task_limit,
        daily_token_limit=payload.daily_token_limit,
        max_running=payload.max_running,
    )
    return schemas.AdminUserItem(**row)


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


@router.post(
    "/users/{user_id}/sessions/revoke",
    response_model=schemas.AdminRevokeSessionsResponse,
    summary="强制下线",
)
async def revoke_sessions(
    user_id: int,
    user: User = Depends(require_admin),
) -> schemas.AdminRevokeSessionsResponse:
    """吊销指定账号的全部会话，等于「立刻踢下线」。

    与删除、停用的区别：只删会话，不改密码、不禁登录——他下次请求 401，
    但只要密码还对，立刻就能重新登录。这是「踢下线」，不是「封号」。

    返回实际吊销条数：单点登录下每人至多一条，`revoked=0` 表示他当时并未登录，
    这不是失败。正在跑的任务不受影响（任务与连接解耦），要停任务请用任务监控页。
    """
    revoked = admin_service.revoke_sessions(user, user_id)
    return schemas.AdminRevokeSessionsResponse(revoked=revoked)


@router.get(
    "/users/{user_id}/deletion-preview",
    response_model=schemas.AdminDeletionPreview,
    summary="删除前预览",
)
async def deletion_preview(
    user_id: int, user: User = Depends(require_admin)
) -> schemas.AdminDeletionPreview:
    """删除账号前先看「会删什么、会留什么」。

    不改任何数据。前端拿它在确认面板里列出条数，并提醒「平台历史统计会随之减少」
    （任务记录是级联删除的，删完之后总量与历史图表都会变小）。
    """
    data = admin_service.deletion_preview(user_id)
    return schemas.AdminDeletionPreview(**data)


@router.get(
    "/users/{user_id}/export",
    response_class=Response,
    summary="导出账号数据（ZIP）",
    responses={200: {"content": {"application/zip": {}}, "description": "账号数据压缩包"}},
)
async def export_user_data(
    user_id: int, user: User = Depends(require_admin)
) -> Response:
    """把该账号的方案、文档正文、对话记录与任务明细打成一个 ZIP 下载。

    **只读**，但会留下一条 `user.export` 审计：它把某人的全部内容搬出了系统，
    事后必须能回答「谁在什么时候导走了谁的数据」。

    `Cache-Control: no-store` 是必须的：响应体是个人数据，任何中间缓存都不该留存。
    文件名用 RFC 5987 的 `filename*` 再带一份纯 ASCII 兜底，避免中文名在旧客户端
    退化成 `download.bin`。
    """
    payload, filename, _ = admin_service.export_user_data(user, user_id)
    return Response(
        content=payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{filename}"; '
                f"filename*=UTF-8''{quote(filename)}"
            ),
            "Content-Length": str(len(payload)),
            "Cache-Control": "no-store",
        },
    )


@router.delete(
    "/users/{user_id}",
    response_model=schemas.AdminOkResponse,
    summary="删除账号",
)
async def delete_user(
    user_id: int, user: User = Depends(require_admin)
) -> schemas.AdminOkResponse:
    """彻底删除账号及其数据（不可逆）。

    方案、文档与版本、对话轮次、任务记录都会级联删除；在飞任务会**先被取消**
    （否则它还会继续烧 token 到跑完才发现没有归属可写）。

    与「停用」「强制下线」的区别：停用只是禁止登录、数据都在；强制下线只是踢掉会话。
    若只是想暂时禁止登录，用停用而不是删除。
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


# ----------------------------------------------------------------------
# 日志检索（v4 T4）
# ----------------------------------------------------------------------


@router.get("/logs", response_model=schemas.AdminLogListResponse, summary="日志检索")
async def list_logs(
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
    user: User = Depends(require_admin),
) -> schemas.AdminLogListResponse:
    """按条件检索应用日志（分页）。

    与 `/audit` 的分工
    ------------------
    `/audit` 是**人**做了什么（问责，不该随便清）；
    这里是**系统**在说什么（排障，可随时清空重建）。混在一起时，
    排查故障会被大量人为操作淹没，追责时又会漏在日志风暴里。

    为什么所有过滤条件都是查询参数而不是请求体
    ------------------------------------------
    它是**只读**接口，而且最常见的用法是「把链接发给自己或同事」——
    过滤条件进 URL 才能分享，进请求体就只能靠截图。这也让浏览器能直接缓存与回退。

    Args:
        level: 最低级别（选 WARNING 即「警告及以上」，含 ERROR）。
        since / until: 时间范围，UTC ISO 8601。
        logger_name: logger 名前缀，如 `backend.services`。
        event: 事件名精确匹配，如 `job.started`。
        request_id: 请求号精确匹配——**排障主路径**：粘一个号拉出该请求全部日志。
        user_id: 归属用户。
        keyword: 消息关键词。
        exclude_event: 排除某个事件名。**访问日志（`request.completed`）每个请求一条**，
            量级会压过业务事件——排掉它才看得见「刚才那次生成怎么了」。
        page / page_size: 分页，页码从 1 开始。

    Returns:
        一页日志与命中总数。`extra` 已由后端解析成对象。
    """
    del user  # 只读接口，不涉及写操作，也不记审计
    data = admin_service.list_logs(
        level=level,
        since=since,
        until=until,
        logger_name=logger_name,
        event=event,
        request_id=request_id,
        user_id=user_id,
        keyword=keyword,
        exclude_event=exclude_event,
        page=page,
        page_size=page_size,
    )
    return schemas.AdminLogListResponse(
        items=[schemas.AdminLogItem(**row) for row in data["items"]],
        total=data["total"],
        page=data["page"],
        page_size=data["page_size"],
    )


# ----------------------------------------------------------------------
# 任务监控（v4.5 T5-1）
# ----------------------------------------------------------------------


@router.get("/jobs", response_model=schemas.AdminJobListResponse, summary="任务监控列表")
async def list_jobs(
    status: str = "",
    username: str = "",
    kind: str = "",
    limit: int = 100,
    offset: int = 0,
    user: User = Depends(require_admin),
) -> schemas.AdminJobListResponse:
    """在飞 / 最近失败 / 全部任务（可按状态、用户名、类型筛）。

    `status` 为空表示不筛（页面的「全部」标签）；常用的两个值是 `running`
    与 `failed`。running 按「跑得最久优先」排序——卡死任务的特征就是时间长，
    顶到最前面才看得见。

    `is_stuck` 由服务端按「running 且超过 `STUCK_MINUTES` 未更新」判定：
    前端只负责把它画红，不重复实现这条规则。
    """
    del user
    data = admin_service.list_jobs(
        status=status,
        username=username,
        kind=kind,
        limit=limit,
        offset=offset,
    )
    return schemas.AdminJobListResponse(
        items=[schemas.AdminJobItem(**row) for row in data["items"]],
        total=data["total"],
    )


@router.post(
    "/jobs/cancel-stuck",
    response_model=schemas.AdminCancelStuckResponse,
    summary="批量取消卡死任务",
)
async def cancel_stuck_jobs(user: User = Depends(require_admin)) -> schemas.AdminCancelStuckResponse:
    """一次性取消所有判定为卡死的任务，返回实际取消条数。

    只动「已停更超过阈值」的那些：正常在跑的任务一个都不碰——批量误伤会让
    用户的正文断在半截，比卡死更难解释。
    """
    return schemas.AdminCancelStuckResponse(cancelled=admin_service.cancel_stuck_jobs(user))


@router.post(
    "/jobs/{job_id}/cancel",
    response_model=schemas.AdminCancelJobResponse,
    summary="取消一次任务",
)
async def cancel_job(
    job_id: int,
    user: User = Depends(require_admin),
) -> schemas.AdminCancelJobResponse:
    """手动取消一次任务（管理员可取消任意用户的任务）。

    `cancelled=false` 不是错误：它表示「你点的时候任务刚好结束了」。
    这种情况若报错，管理员会以为操作失败而反复点击；返回 false 让他
    刷新后看到它已不在运行列表里，反而更清楚。

    任务是**协作式**停止：在途协程在下一个 await 点跳出。卡在纯同步阻塞上的
    任务不会立刻退出，但库里已是终态，重启后不会再显示「生成中」。
    """
    return schemas.AdminCancelJobResponse(cancelled=admin_service.cancel_job_as_admin(user, job_id))


# ----------------------------------------------------------------------
# 系统配置（v4 T3）
# ----------------------------------------------------------------------


@router.get("/config", response_model=schemas.AdminConfigResponse, summary="读取可调配置")
async def get_config(user: User = Depends(require_admin)) -> schemas.AdminConfigResponse:
    """分组返回全部可调项 + 当前激活供应商 + 配置指纹。

    每个配置项都带 `value`（生效值）与 `default`（env/代码默认值）。两者不同说明
    后台覆盖过，`source` 会标 `db` —— 这正是排查「改了没生效」时要看的第一列。

    **启动类配置不在响应里**（日志级别、落库开关、db 路径等）：它们改了必须重启，
    放进来只会制造「显示已保存、实际没变」的困惑。这是刻意的省略，不是遗漏。
    """
    del user
    return schemas.AdminConfigResponse(**admin_config_service.get_config())


@router.patch("/config", response_model=schemas.AdminConfigResponse, summary="修改配置")
async def update_config(
    payload: schemas.AdminUpdateConfigRequest,
    user: User = Depends(require_admin),
) -> schemas.AdminConfigResponse:
    """批量修改配置项，**下一次生成即生效**。

    先全量校验再写：只要有一项非法，整批都不落库。配置是一组语义，写一半会留下
    「并发度改了、审核开关没改」这种撕裂状态，而它下次生成就真的生效了。

    返回值是**改完之后重新读出来的**配置，不是「你提交的那份」：
    前端据此比对「我提交的」与「实际生效的」，从而发现「保存成功但没生效」。
    """
    return schemas.AdminConfigResponse(
        **admin_config_service.update_config(user, payload.updates)
    )


@router.post("/config/reset", response_model=schemas.AdminConfigResponse, summary="恢复默认")
async def reset_config(
    payload: schemas.AdminResetConfigRequest,
    user: User = Depends(require_admin),
) -> schemas.AdminConfigResponse:
    """删除覆盖项，回到 env / 代码默认值。

    「恢复默认」在数据层就是**删掉那一行**——这也是「只存覆盖项」这个设计的收益：
    不需要另一张表记默认值，删了就是默认。
    """
    return schemas.AdminConfigResponse(
        **admin_config_service.reset_config(user, payload.keys)
    )


@router.get(
    "/config/effective",
    response_model=schemas.AdminConfigResponse,
    summary="当前生效配置（复核用）",
)
async def config_effective(
    user: User = Depends(require_admin),
) -> schemas.AdminConfigResponse:
    """与 `GET /config` 同一份数据，但**语义不同**：它是给前端「保存后复核」用的。

    为什么需要它
    ------------
    `PATCH` 的响应体也可能是缓存里的回声（比如多 worker 里写的那台与读的那台不是同一个
    进程）。让前端**再独立请求一次**，两次一致才算真的生效——这是唯一能排除
    「响应体看着对、实际没写进去」的办法。
    """
    del user
    return schemas.AdminConfigResponse(**admin_config_service.get_config())


# ----------------------------------------------------------------------
# 模型供应商（v4 T3）
# ----------------------------------------------------------------------


@router.get(
    "/providers",
    response_model=schemas.AdminProviderListResponse,
    summary="配置条目列表",
)
async def list_providers(
    user: User = Depends(require_admin),
) -> schemas.AdminProviderListResponse:
    """全部配置条目 + 当前使用中的那条 + 新增时的供应商模板。

    **响应里没有密钥**，只有 `key_present`（布尔）。`key_present=false` 表示
    「这条还没配可用密钥」，界面据此提示——密钥值本身永远不出后端（ADR-053）。
    要看回明文只能调 `POST /providers/{id}/reveal-key`（会写审计）。
    """
    del user
    return schemas.AdminProviderListResponse(**admin_config_service.list_providers())


@router.get(
    "/providers/presets",
    response_model=list[schemas.AdminProviderPreset],
    summary="新增配置的供应商模板",
)
async def provider_presets(
    user: User = Depends(require_admin),
) -> list[schemas.AdminProviderPreset]:
    """常见厂商的地址与变量名模板（**不入库**，只是表单默认值）。

    ⚠️ 路由必须注册在 `/{provider_id}` **之前**，否则 `presets` 会被当成一个
    配置 id 去查（然后 404）。
    """
    del user
    return [schemas.AdminProviderPreset(**item) for item in PRESETS]


@router.post(
    "/providers",
    response_model=schemas.AdminProviderListResponse,
    status_code=status.HTTP_201_CREATED,
    summary="新增配置",
)
async def create_provider(
    payload: schemas.AdminCreateProviderRequest,
    user: User = Depends(require_admin),
) -> schemas.AdminProviderListResponse:
    """新增一条配置。

    **没有 `id`**：由后端生成（`cfg_xxx`）。同一协议可以有多条配置，
    手填 id 只会撞名。`name` 必填但允许重名——它只是给人看的标签。

    `api_key` 是**密钥明文**，只用于本次加密入库，响应里绝不会再出现它。
    """
    return schemas.AdminProviderListResponse(
        **admin_config_service.create_provider(user, payload.model_dump())
    )


@router.patch(
    "/providers/{provider_id}",
    response_model=schemas.AdminProviderListResponse,
    summary="编辑配置",
)
async def update_provider(
    provider_id: str,
    payload: schemas.AdminUpdateProviderRequest,
    user: User = Depends(require_admin),
) -> schemas.AdminProviderListResponse:
    """只改传入的字段。

    `id` / `builtin` 由仓储层过滤；**`protocol` 改了会 400**——换协议等于换一家
    厂商，参数语义全变，只能新建一条。
    """
    return schemas.AdminProviderListResponse(
        **admin_config_service.update_provider(
            user, provider_id, payload.model_dump(exclude_unset=True)
        )
    )


@router.delete(
    "/providers/{provider_id}",
    response_model=schemas.AdminProviderListResponse,
    summary="删除配置",
)
async def delete_provider(
    provider_id: str,
    user: User = Depends(require_admin),
) -> schemas.AdminProviderListResponse:
    """删除一条配置。

    唯一会被拒的情形（403）：**它是当前使用中的那条**——删完下次生成会全线失败，
    而那时管理员多半已经离开了。先切到另一条再删，是唯一安全的顺序。

    「使用中」按 `system_config` 里的**原始指针**判断，不看解析后的激活项：
    后者在被停用时会回退到别条，于是会放行并留下悬空指针。
    """
    return schemas.AdminProviderListResponse(
        **admin_config_service.delete_provider(user, provider_id)
    )


@router.post(
    "/providers/{provider_id}/activate",
    response_model=schemas.AdminProviderListResponse,
    summary="设为使用中",
)
async def activate_provider(
    provider_id: str,
    user: User = Depends(require_admin),
) -> schemas.AdminProviderListResponse:
    """把这条配置设为「使用中」，**下一次生成即生效**（正在跑的任务不受影响）。

    同一时刻只有一条：它在 `system_config.active_provider_id` 里。

    缺密钥时**允许切换**，但响应里的 `warning` 会说明「下一次生成会报错」。
    拦着不让切会让「先切过去、再去配密钥」这个正常顺序走不通。
    """
    return schemas.AdminProviderListResponse(
        **admin_config_service.activate_provider(user, provider_id)
    )


@router.post(
    "/providers/{provider_id}/reveal-key",
    response_model=schemas.AdminProviderKeyResponse,
    summary="查看密钥明文（一次性，写审计）",
)
async def reveal_provider_key(
    provider_id: str,
    user: User = Depends(require_admin),
) -> schemas.AdminProviderKeyResponse:
    """解密并返回该配置的密钥明文。

    这是后台唯一能把密钥看回明文的地方，所以刻意做成**独立动作**并写审计：
    列表是随手可看的，密钥不该随手可见；单独一次「查看」能留下
    「谁、什么时候、看了哪条」的痕迹。
    """
    return schemas.AdminProviderKeyResponse(
        **admin_config_service.reveal_provider_key(user, provider_id)
    )


@router.post(
    "/providers/{provider_id}/test",
    response_model=schemas.AdminProviderTestResponse,
    summary="测试配置连通性",
)
async def test_provider(
    provider_id: str,
    user: User = Depends(require_admin),
) -> schemas.AdminProviderTestResponse:
    """**真的发一次最小调用**（`max_tokens=16`、15 秒超时）。

    为什么必须真调：「参数填对了」与「真的能生成」是两件事——`base_url` 少了
    `/v1`、密钥是别的环境的、协议选错了，这些在保存时都看不出来。这个按钮存在的
    全部意义就是在切换之前把它们试出来。

    它是本项目唯一会花钱的管理接口，所以额度压到最低；无论成败都记审计。
    """
    return schemas.AdminProviderTestResponse(
        **admin_config_service.test_provider(user, provider_id)
    )
