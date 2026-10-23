"""管理员专属的接口文档（ADR-085）。

## 为什么不直接在 `/docs` 上加鉴权

FastAPI 默认注册**四条**公开路由（0.142.2 实测）：

```text
['/openapi.json', '/docs', '/docs/oauth2-redirect', '/redoc']
```

而本项目的鉴权是「默认拒绝 + 白名单放行」（`api/deps.py`）。这三条一旦躺在白名单里，
任何来访者都能拿到完整接口清单——包括全部管理员接口的路径与参数结构。

三条路都比过，最后选的是「另起一套」：

| 方案 | 为什么不够 |
| --- | --- |
| 只在 Nginx 挡 | 绕过反向代理的路径（本机 curl、SSH 隧道、换机器忘配）仍然可见；安全边界不该只落在代理上 |
| 从白名单删掉、保留注册 | 「要登录」不等于「要管理员」——任何普通用户登录后都能看全部接口形状 |
| 给 `/docs` 挂依赖 | 这四条是框架自动注册的路由，挂不上 `Depends`；要自定义就得自己重写页面，那还不如一次做对 |

于是：**公开四条一律不注册**（`main.py` 传三个 `None`），改在 `/api/admin/` 下重建三条，
逐条挂 `require_admin`。中间件那一层也自动生效——`/api/admin/**` 不在白名单，
匿名请求到不了路由。

## 页面能打开，是因为浏览器会带 Cookie

`/api/admin/docs` 是后端直接渲染的 HTML，浏览器用**新标签页导航**打开它，同源导航会带上
会话 Cookie（`SameSite=Lax` 允许顶层 GET 导航）。进页面后 Swagger UI 用 `fetch` 取
`/api/admin/openapi.json`，同源请求同样带 Cookie。

⚠️ 因此 `require_admin` 在这里是**靠 Cookie 生效**的，不是 Bearer Token。
若将来前端改成把 token 放 `Authorization` 头，这两个页面会一起失效——那时需要另想办法
（例如把会话同时写进可被同源读取的 Cookie，或给文档页签名临时地址）。

## Swagger UI / ReDoc 的静态资源走 CDN

浏览器（不是服务器）需要能访问下面两个域名。内网或 CDN 不可达时，把 `swagger-ui-dist`
与 `redoc` 的产物放到本机静态目录，再把三个常量改成同源路径即可——**只改这三个常量**，
不要改函数体。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse

from backend.api.deps import require_admin
from backend.services.auth_service import User

router = APIRouter(prefix="/api/admin", tags=["admin"])

# 这份 schema 的**唯一**地址。三个页面都引用它，写死成常量是为了不让它们各写一份字面量。
ADMIN_OPENAPI_URL = "/api/admin/openapi.json"

# ⚠️ 这三个地址是给**浏览器**用的，与服务器出网无关。
SWAGGER_JS_URL = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"
SWAGGER_CSS_URL = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css"
REDOC_JS_URL = "https://cdn.jsdelivr.net/npm/redoc@2/bundles/redoc.standalone.js"


@router.get(
    "/openapi.json",
    include_in_schema=False,
    summary="管理员：OpenAPI 定义",
)
def admin_openapi(request: Request, _: User = Depends(require_admin)) -> JSONResponse:
    """返回完整的 OpenAPI 定义（JSON）。

    用 `request.app.openapi()` 而不是在模块级 import `app`：后者会成环
    （`main` → `admin_docs` → `main`）。FastAPI 自己缓存了这份 schema，
    重复调用不会重新生成。

    `include_in_schema=False`：文档自身不进文档。否则 Swagger UI 里会出现
    「管理员：OpenAPI 定义」这条接口，点开是一个巨大的 JSON 结构——噪音。
    """
    return JSONResponse(request.app.openapi())


@router.get(
    "/docs",
    include_in_schema=False,
    summary="管理员：交互式接口文档（Swagger UI）",
)
def admin_swagger(_: User = Depends(require_admin)) -> HTMLResponse:
    """Swagger UI：可展开、可在线发请求。

    ⚠️ 在线试调会**真的**调用接口，包括写操作。这是它相对 ReDoc 的全部价值，
    也是它的风险——所以这一页只在管理员手里。
    """
    return get_swagger_ui_html(
        openapi_url=ADMIN_OPENAPI_URL,
        title="PRDwright API · 管理员",
        swagger_js_url=SWAGGER_JS_URL,
        swagger_css_url=SWAGGER_CSS_URL,
    )


@router.get(
    "/redoc",
    include_in_schema=False,
    summary="管理员：只读接口文档（ReDoc）",
)
def admin_redoc(_: User = Depends(require_admin)) -> HTMLResponse:
    """ReDoc：只读三栏文档，适合通读；没有试调按钮，渲染纯静态。"""
    return get_redoc_html(
        openapi_url=ADMIN_OPENAPI_URL,
        title="PRDwright API · 管理员",
        redoc_js_url=REDOC_JS_URL,
        # 默认会从 Google Fonts 拉字体：国内网络下这一跳常常卡住（页面能渲染，
        # 但要等浏览器超时）。ReDoc 的排版用系统字体已经够看，直接关掉。
        with_google_fonts=False,
    )
