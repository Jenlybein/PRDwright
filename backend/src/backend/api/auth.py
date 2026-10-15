"""认证路由：注册 / 登录 / 登出 / 当前用户。

本模块位于中间件白名单内（见 `api/deps.py` 的 `is_public_path`），因此它**自己解析
Cookie**，不依赖中间件注入的用户——否则未登录时连「查询我自己登录态」的接口都进不去。

本层只做参数校验与响应封装，全部业务判断都在 `services/auth_service.py`。
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from backend.api import schemas
from backend.core.config import get_settings
from backend.core.errors import AppError, ErrorCode
from backend.services import auth_service

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _set_session_cookie(response: Response, credential: str) -> None:
    """把会话凭据写入 Cookie。

    三项安全设置一个都不能少：

    - `httponly=True`：JS 读不到，XSS 拿不走登录态；
    - `samesite="lax"`：常规导航会带上，同时挡住大部分跨站请求伪造；
    - `secure`：由配置决定——本地 http 调试为 false，HTTPS 部署时必须为 true。
    """
    settings = get_settings()
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=credential,
        max_age=settings.auth_session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.auth_cookie_secure,
        path="/",
    )


@router.post("/register", response_model=schemas.AuthUserResponse, summary="注册（需邀请码）")
async def register(
    payload: schemas.RegisterRequest,
    response: Response,
) -> schemas.AuthUserResponse:
    """注册新账号。

    注册成功后**直接签发会话**（等同于自动登录），省去「注册完还要再登录一次」这一步。
    走 `replace()` 与登录同一口径：新会话立起来的同时清掉该用户此前的会话。新账号本来
    没有旧会话，这一步是空操作——但它保证「凡是签发登录态的地方都只有一个入口」，
    不给「某个入口忘了吊销旧会话」留缝。
    """
    try:
        user = auth_service.register(payload.username, payload.password, payload.invite_code)
    except auth_service.AuthError as exc:
        # 错误码由服务层给定，这里原样透传；状态码才是本层该决定的事
        raise AppError(exc.code, exc.message, status.HTTP_400_BAD_REQUEST) from exc

    credential, _ = auth_service.get_token_issuer().replace(user.id)
    _set_session_cookie(response, credential)
    return schemas.AuthUserResponse(id=user.id, username=user.username)


@router.post("/login", response_model=schemas.AuthUserResponse, summary="登录")
async def login(
    payload: schemas.LoginRequest,
    response: Response,
) -> schemas.AuthUserResponse:
    """校验账号密码，成功则下发会话 Cookie。"""
    try:
        user, credential, _ = auth_service.login(payload.username, payload.password)
    except auth_service.AuthError as exc:
        # 统一返回 401：不区分「用户不存在」与「密码错误」，防止账号枚举
        raise AppError(exc.code, exc.message, status.HTTP_401_UNAUTHORIZED) from exc

    _set_session_cookie(response, credential)
    return schemas.AuthUserResponse(id=user.id, username=user.username)


@router.post("/logout", response_model=schemas.LogoutResponse, summary="登出")
async def logout(request: Request, response: Response) -> schemas.LogoutResponse:
    """登出：吊销服务端会话并清除 Cookie。

    两步都做——只清 Cookie 而服务端不吊销的话，那个 token 捡回来还能继续用。
    """
    credential = request.cookies.get(get_settings().auth_cookie_name, "")
    auth_service.logout(credential)
    response.delete_cookie(key=get_settings().auth_cookie_name, path="/")
    return schemas.LogoutResponse(ok=True)


@router.get("/me", response_model=schemas.AuthUserResponse, summary="当前登录用户")
async def me(request: Request) -> schemas.AuthUserResponse:
    """返回当前登录用户；未登录或会话过期返回 401。

    前端用它来探测登录态（顶栏显示「登录」还是昵称）。
    """
    credential = request.cookies.get(get_settings().auth_cookie_name, "")
    user = auth_service.current_user(credential)
    if user is None:
        raise AppError(ErrorCode.UNAUTHORIZED, "当前未登录")
    return schemas.AuthUserResponse(id=user.id, username=user.username)
