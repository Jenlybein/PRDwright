"""登录链路的两个共用动作：渐进延时与下发会话 Cookie。

为什么单独成一个模块
--------------------
用户端（`/api/auth/login`）与管理端（`/api/admin/login`）的登录接口做的是
**同一件事**：先按失败次数等一会儿、再校验、成功后下发同一个 Cookie。这段逻辑
写两遍的代价不是多几行代码，而是**两边迟早不一致**——一处改了 Cookie 属性
（`secure` / `samesite` / `max_age`），另一处忘了改，症状是「用户端能登录、
管理端登录后立刻掉线」，而排查时会先怀疑会话表。

放进 `api/` 而不是 `services/`：这两个动作都长在 HTTP 上（`Response` 对象、
`asyncio.sleep`），服务层不该知道 Cookie 长什么样。业务判断仍在
`services/auth_service.py`。
"""

from __future__ import annotations

import asyncio

from fastapi import Response

from backend.core.runtime_config import get_runtime_config
from backend.services import auth_service


async def _sleep(seconds: float) -> None:
    """等待指定秒数（登录限流的渐进延时）。

    ⚠️ 单独抽成模块级函数，只为**可测**：留成内联 `await asyncio.sleep(...)` 的话，
    任何触发延时的用例都要真等 2~8 秒，整套用例会慢到没人愿意跑。
    测试里把它换成只记录参数的假函数，就能断言「该等多久」而不真的等。

    因此**不要在调用点绕过它**直接 `await asyncio.sleep`——绕过去的地方，用例里
    就变成了真的在睡觉（失败时表现为「用例超时」，而不是「断言不对」）。
    """
    await asyncio.sleep(seconds)


async def wait_login_delay(username: str) -> None:
    """按失败次数先等一会儿再校验（渐进延时）。

    ⚠️ 「等多久」由服务层算（`auth_service.login_delay`），「真的等」只能在这里做：
    `auth_service.login` 是同步函数、跑在事件循环线程上，在里面 `time.sleep` 会把
    整个进程停住（不只是这条请求，所有在途的 SSE 流都会跟着卡）。

    延时放在校验**之前**：放在失败之后，攻击者用响应快慢就能判断这次猜对了没有。
    """
    delay = auth_service.login_delay(username)
    if delay > 0:
        await _sleep(delay)


def set_session_cookie(response: Response, credential: str) -> None:
    """把会话凭据写入 Cookie。

    三项安全设置一个都不能少：

    - `httponly=True`：JS 读不到，XSS 拿不走登录态；
    - `samesite="lax"`：常规导航会带上，同时挡住大部分跨站请求伪造；
    - `secure`：由配置决定——本地 http 调试为 false，HTTPS 部署时必须为 true。

    ⚠️ 读的是**热配置**（`get_runtime_config().settings`）而不是 `get_settings()`：
    `max_age` 必须与库里那条会话的过期时间一致，而后者是按热配置签发的。
    两处读不同的源，会出现「Cookie 30 天后过期、服务端 7 天就判失效」的错位。
    """
    settings = get_runtime_config().settings
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=credential,
        max_age=settings.auth_session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.auth_cookie_secure,
        path="/",
    )
