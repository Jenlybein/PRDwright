"""管理员路由：v4 后台的地基。

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
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from backend.api import schemas
from backend.api.deps import require_admin
from backend.services.auth_service import User

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/me", response_model=schemas.AdminMeResponse, summary="管理员身份自检")
async def me(user: User = Depends(require_admin)) -> schemas.AdminMeResponse:
    """确认当前登录者**是**管理员。

    前端拿它判定「要不要放行进后台」：200 放行、401 去登录、403 停在无权限页。

    刻意**不**另设一个「返回 `is_admin` 布尔值」的公开接口——那会让
    「我是不是管理员」变成任何人都能问的问题，而这个信息本不该被随意探测。
    想知道答案就去敲管理员接口，让服务端用状态码回答。
    """
    return schemas.AdminMeResponse(id=user.id, username=user.username, is_admin=user.is_admin)
