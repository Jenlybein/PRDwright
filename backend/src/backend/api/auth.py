"""认证路由：注册 / 登录 / 登出 / 当前用户。

本模块位于中间件白名单内（见 `api/deps.py` 的 `is_public_path`），因此它**自己解析
Cookie**，不依赖中间件注入的用户——否则未登录时连「查询我自己登录态」的接口都进不去。

本层只做参数校验与响应封装，全部业务判断都在 `services/auth_service.py`。
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from backend.api import schemas
from backend.api.login_flow import set_session_cookie, wait_login_delay
from backend.core.config import get_settings
from backend.core.errors import AppError, ErrorCode
from backend.core.runtime_config import get_runtime_config
from backend.services import auth_service

router = APIRouter(prefix="/api/auth", tags=["auth"])


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

    管理员可以在后台关闭自助注册（`auth_allow_register`）。关闭后一律 403——用
    `FORBIDDEN` 而不是 `AUTH_FAILED`：前者表示「你这次请求本身不被允许」，
    后者是「凭据不对」，前端据此展示的提示不同。
    """
    if not get_runtime_config().settings.auth_allow_register:
        raise AppError(
            ErrorCode.FORBIDDEN,
            "本站暂不开放自助注册，请联系管理员开通账号",
            status.HTTP_403_FORBIDDEN,
        )

    try:
        user = auth_service.register(payload.username, payload.password, payload.invite_code)
    except auth_service.AuthError as exc:
        # 错误码由服务层给定，这里原样透传；状态码才是本层该决定的事
        raise AppError(exc.code, exc.message, status.HTTP_400_BAD_REQUEST) from exc

    credential, _ = auth_service.get_token_issuer().replace(user.id)
    set_session_cookie(response, credential)
    return schemas.AuthUserResponse(id=user.id, username=user.username)


@router.post("/login", response_model=schemas.AuthUserResponse, summary="登录")
async def login(
    payload: schemas.LoginRequest,
    request: Request,
    response: Response,
) -> schemas.AuthUserResponse:
    """校验账号密码，成功则下发会话 Cookie。

    渐进延时**必须由本层 `await`**：`auth_service.login` 是同步函数、跑在事件循环
    线程上，在里面 `time.sleep` 会把整个进程停住（不只是这条请求，所有在途的 SSE
    流都会跟着卡）。所以「等多久」由 service 算、「真的等」由 `wait_login_delay` 做。

    延时放在校验**之前**：若放在失败之后，攻击者用「响应快慢」就能判断这次猜对了
    没有，等于白送一个提示；放在之前则每次尝试都要付这个时间，而正确密码也只是慢一点。
    """
    await wait_login_delay(payload.username)

    client_ip = request.client.host if request.client else ""
    try:
        user, credential, _ = auth_service.login(
            payload.username, payload.password, ip=client_ip
        )
    except auth_service.AuthError as exc:
        # 统一返回 401：不区分「用户不存在」与「密码错误」，防止账号枚举
        raise AppError(exc.code, exc.message, status.HTTP_401_UNAUTHORIZED) from exc

    set_session_cookie(response, credential)
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
