"""路由层依赖与认证中间件。

**设计要点：默认拒绝，而非默认允许。**

常见的做法是给需要保护的接口逐个挂 `Depends(...)`，但那是「默认放行 + 手动收紧」——
将来新增一个会消耗模型额度的接口，只要忘了加依赖就直接裸奔。漏加是这类事故最常见的来源。

因此这里改成**中间件统一拦截、白名单放行**：新增任何接口都自动受保护，
要公开某个接口必须显式加进白名单，而这个动作会让人停下来想一下它是否真的可以公开。
"""

from __future__ import annotations

import json
import logging
import time
from http.cookies import SimpleCookie
from uuid import uuid4

from fastapi import Depends, Request, status
from starlette.datastructures import MutableHeaders

from backend.core.config import get_settings
from backend.core.errors import AppError, ErrorCode, error_payload
from backend.core.request_context import reset_request_id, set_request_id
from backend.services.auth_service import User, current_user, ensure_dev_user

# 访问日志独立一个 logger 名：将来想单独调它的级别、或整体关掉，
# 不必牵连应用自己的业务日志。
_access_logger = logging.getLogger("backend.api.access")

# 精确匹配的公开路径
_PUBLIC_EXACT: frozenset[str] = frozenset(
    {
        "/",
        "/health",
        # 问卷配置是静态内容，未登录也要能渲染入口页
        "/api/conversation/questions",
        # 管理后台的登录入口：登录这件事本身不能要求先登录。这是 `/api/admin` 下
        # **唯一**的例外，写在这里而不是给整个前缀开口子——前缀放行会让新增的
        # 管理接口默默变成公开接口。
        "/api/admin/login",
        # 交互式文档，方便本地联调；部署到公网时可从这里移除
        "/docs",
        "/redoc",
        "/openapi.json",
    }
)

# 前缀匹配的公开路径。认证接口本身必须放行，否则未登录时连登录都用不了。
_PUBLIC_PREFIXES: tuple[str, ...] = ("/api/auth",)

# 未登录时的统一提示。注意**不区分**「未登录」与「会话过期」，
# 避免泄露额外信息。
_UNAUTHORIZED_DETAIL = "请先登录后再使用该功能"

# 已登录但非管理员的统一提示。**刻意说「账号不存在」而不是「权限不足」**
# （ADR-049）：后台对外只承认管理员账号存在，非管理员账号在这里等同于不存在。
# 这句话不泄露「谁是管理员」，也不给出一条「这个账号确实注册过」的确认。
_NOT_ADMIN_DETAIL = "账号不存在"


def is_public_path(path: str) -> bool:
    """判断路径是否在白名单内（白名单内的接口无需登录）。"""
    if path in _PUBLIC_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in _PUBLIC_PREFIXES)


def _cookie_value(scope: dict, name: str) -> str:
    """从 ASGI 请求头里取出指定 Cookie 的值；不存在或格式异常时返回空串。"""
    raw = ""
    for key, value in scope.get("headers", []):
        if key == b"cookie":
            raw = value.decode("latin-1")
            break
    if not raw:
        return ""

    jar = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:  # Cookie 头格式非法时按「没有登录态」处理，不影响其它接口
        return ""
    morsel = jar.get(name)
    return morsel.value if morsel else ""


class AuthMiddleware:
    """认证中间件：白名单直接放行，其余一律校验登录态。

    **为什么不用 `BaseHTTPMiddleware`**：它会把下游响应体重新包装成新的
    `StreamingResponse`，对本项目的 SSE 流式接口（`/api/conversation/*-stream`）
    是额外的不确定因素。这里写成纯 ASGI 中间件，只做「放行 / 直接回 401」两件事，
    放行时**完全不触碰响应体**，流式数据原样透传。

    `AUTH_ENABLED=false`（本地开发）时全部放行，并把用户置为 None，
    由业务层在需要落归属时用 dev 用户兜底（见 `acting_user`）。
    """

    def __init__(self, app) -> None:  # noqa: ANN001 - ASGIApp 类型随 starlette 版本而异
        self.app = app

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        settings = get_settings()
        # Request.state 由 scope["state"] 支撑，中间件写进去，路由层就能读到
        scope.setdefault("state", {})
        scope["state"]["user"] = None

        # 预检请求（OPTIONS）按规范不带任何凭据，拦它只会让浏览器的跨域前置请求失败，
        # 进而表现为「接口凭空 401」。真实请求仍会被下面的校验拦住，安全边界没有放松。
        if scope["method"] == "OPTIONS":
            await self.app(scope, receive, send)
            return

        if settings.auth_enabled and not is_public_path(scope["path"]):
            user = current_user(_cookie_value(scope, settings.auth_cookie_name))
            if user is None:
                await self._send_unauthorized(send)
                return
            scope["state"]["user"] = user

        await self.app(scope, receive, send)

    @staticmethod
    async def _send_unauthorized(send) -> None:  # noqa: ANN001
        """直接回一个 401 JSON，带 `UNAUTHORIZED` 错误码。

        走 ASGI 原生消息而不构造 Response 对象：中间件这一层还没进路由，
        构造 Response 要额外承担序列化与生命周期管理，这里只需要「写两帧」。

        响应体由 `error_payload` 构造，与路由层的 `AppError` 保持同一形态——
        中间件拦下的 401 和路由抛出的 401 对前端来说必须是同一种东西。
        """
        # separators 与 JSONResponse 的取值一致，让中间件与路由层产出的错误体逐字节相同
        body = json.dumps(
            error_payload(_UNAUTHORIZED_DETAIL, ErrorCode.UNAUTHORIZED),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status.HTTP_401_UNAUTHORIZED,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


_REQUEST_ID_HEADER = b"x-request-id"

# 入站 request_id 的最大长度。回写响应头之前必须设上限，避免外部塞进超长头。
_MAX_REQUEST_ID_LENGTH = 64


def _incoming_request_id(scope: dict) -> str:
    """取入站 `X-Request-ID`；没有或不可用时返回空串（由调用方另生成）。

    **必须过滤**：这个值会被原样写进响应头，直接采信外部输入等于把响应头的控制权
    交给调用方（换行注入、超长头）。因此只接受「长度合规 + 仅字母数字与 `-` `_`」，
    不合规就当没带，而不是原样透传。
    """
    for key, value in scope.get("headers", []):
        if key.lower() != _REQUEST_ID_HEADER:
            continue
        raw = value.decode("latin-1").strip()
        if 0 < len(raw) <= _MAX_REQUEST_ID_LENGTH and all(
            char.isalnum() or char in "-_" for char in raw
        ):
            return raw
        return ""
    return ""


def _log_request(scope: dict, request_id: str, status_code: int, started_at: float) -> None:
    """记一条访问日志：请求号、状态码、耗时。

    耗时是「从收到请求到**开始**回响应」的时间。对普通请求约等于总耗时；对 SSE 是
    **建立流**的耗时——整段生成多久不在这里，它由 `run_summary` 记录
    （见 `services/stream_reply.py`）。两者刻意不混成一个字段：否则看日志的人
    会以为「一次 PRD 生成只花了 40 毫秒」。

    记在最外层中间件里，所以**被认证拦掉的 401 也有这一条**。排查「用户说打不开」
    时，第一个要确认的问题正是「这个请求到底有没有到过后端」。
    """
    _access_logger.info(
        "请求完成",
        extra={
            "event": "request.completed",
            "request_id": request_id,
            "method": scope["method"],
            "path": scope["path"],
            "status": status_code,
            "duration_ms": round((time.perf_counter() - started_at) * 1000, 1),
        },
    )


class RequestIdMiddleware:
    """给每个请求分配 `request_id`，并回写 `X-Request-ID` 响应头。

    **为什么是纯 ASGI**：与 `AuthMiddleware` 同理——`BaseHTTPMiddleware` 会把下游响应体
    重新包一层，对本项目的 SSE 接口是额外的不确定因素。这里只在 `http.response.start`
    那一帧往响应头里**追加一个字段**，完全不触碰响应体，流式数据原样透传；
    也正因为只在首帧动手，长连接（SSE）不会因此被缓冲。

    **为什么要回写响应头**：`request_id` 的价值在于「用户能把它念给维护者」。
    只写日志、不回给前端，用户报障时给不出这个值，追踪链就断在浏览器这一端。

    **为什么还要写上下文变量**：`run_summary` 由服务层组装，那里没有任何 HTTP 对象；
    上下文变量是中间件与服务层之间唯一的通道（见 `core/request_context.py`）。
    后台任务会复制当前上下文，因此生成任务的 `request_id` 与发起它的那次请求一致。
    """

    def __init__(self, app) -> None:  # noqa: ANN001 - ASGIApp 类型随 starlette 版本而异
        self.app = app

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope) or uuid4().hex
        scope.setdefault("state", {})
        scope["state"]["request_id"] = request_id
        token = set_request_id(request_id)

        started_at = time.perf_counter()
        # 0 表示「还没开始回响应就结束了」（客户端提前断开、或下游直接抛异常）。
        # 留一个能分辨的值，比事后看到 `status=None` 再回头翻代码要省事。
        status_code = 0

        async def send_with_request_id(message) -> None:  # noqa: ANN001
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            # 先记日志再还原上下文：`extra` 里已经显式带了号（不依赖上下文变量），
            # 但让上下文在格式化那一刻仍然有效，就没有「某种格式下才正确」的隐性前提。
            _log_request(scope, request_id, status_code, started_at)
            reset_request_id(token)


def get_current_user_optional(request: Request) -> User | None:
    """取当前用户；未登录返回 `None`，不抛异常。

    优先读中间件已解析好的结果，取不到时（例如该路径在白名单里）再自行解析 Cookie。
    """
    user = getattr(request.state, "user", None)
    if user is not None:
        return user
    credential = request.cookies.get(get_settings().auth_cookie_name, "")
    return current_user(credential)


def get_current_user(request: Request) -> User:
    """取当前用户；未登录抛 401（带 `UNAUTHORIZED` 错误码）。"""
    user = get_current_user_optional(request)
    if user is None:
        raise AppError(ErrorCode.UNAUTHORIZED, _UNAUTHORIZED_DETAIL)
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    """取当前用户**且**要求是管理员；不是管理员抛 403（带 `FORBIDDEN` 错误码）。

    这是所有 `/api/admin/**` 路由的**唯一入口依赖**（`/login` 除外，它在白名单里且
    自己判身份）。刻意做成依赖，而不是让各路由自己写 `if not user.is_admin`：
    漏写一次判断就是一次越权，且它在代码里长得跟正常分支没什么两样；而依赖**漏挂**
    会直接体现为「这个函数签名里没有它」，评审时一眼能看到。

    注意它只管「权限」，不管「登录」——后者由 `AuthMiddleware` 统一拦
    （`/api/admin` 不在白名单里），所以走到这里时用户必然已登录。

    ⚠️ 文案是「账号不存在」而不是「权限不足」（`_NOT_ADMIN_DETAIL`）：登录入口已经在
    源头挡住了非管理员，这个人还能走到这里，只可能是「会话建立之后被撤销了管理员」或
    「绕过前端直接敲接口」。两种情况都不该告诉对方「你存在、只是不够格」。
    """
    if not user.is_admin:
        raise AppError(ErrorCode.FORBIDDEN, _NOT_ADMIN_DETAIL)
    return user


def acting_user(request: Request) -> User:
    """取**实际操作者**，用于给业务数据打归属（owner_id）。

    与 `get_current_user` 的区别是：`AUTH_ENABLED=false` 的开发模式下这里不抛异常，
    而是回落到内置的 dev 用户。这样业务代码拿到的 `user_id` 恒非空，
    数据模型不必为「开发模式」开一个 NULL 分支，将来打开鉴权也零迁移。

    注意：本函数只在**需要落归属**的地方调用（例如将来的方案保存）。
    判断「这个接口要不要登录」是中间件的事，不要用它来放行接口。
    """
    user = get_current_user_optional(request)
    if user is not None:
        return user

    if not get_settings().auth_enabled:
        return ensure_dev_user()

    raise AppError(ErrorCode.UNAUTHORIZED, _UNAUTHORIZED_DETAIL)
