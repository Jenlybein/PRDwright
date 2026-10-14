"""路由层依赖与认证中间件。

**设计要点：默认拒绝，而非默认允许。**

常见的做法是给需要保护的接口逐个挂 `Depends(...)`，但那是「默认放行 + 手动收紧」——
将来新增一个会消耗模型额度的接口，只要忘了加依赖就直接裸奔。漏加是这类事故最常见的来源。

因此这里改成**中间件统一拦截、白名单放行**：新增任何接口都自动受保护，
要公开某个接口必须显式加进白名单，而这个动作会让人停下来想一下它是否真的可以公开。
"""

from __future__ import annotations

import json
from http.cookies import SimpleCookie
from uuid import uuid4

from fastapi import Request, status
from starlette.datastructures import MutableHeaders

from backend.core.config import get_settings
from backend.core.errors import AppError, ErrorCode, error_payload
from backend.core.request_context import reset_request_id, set_request_id
from backend.services.auth_service import User, current_user, ensure_dev_user

# 精确匹配的公开路径
_PUBLIC_EXACT: frozenset[str] = frozenset(
    {
        "/",
        "/health",
        # 问卷配置是静态内容，未登录也要能渲染入口页
        "/api/conversation/questions",
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

        async def send_with_request_id(message) -> None:  # noqa: ANN001
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
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
