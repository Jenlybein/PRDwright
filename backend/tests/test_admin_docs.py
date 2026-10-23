"""管理员专属的接口文档（ADR-085）。

守的是三件事，每一件错了都**不会报错**，只会静默地把接口清单交出去：

1. **公开的四条路由根本不存在**。`/docs`、`/redoc`、`/openapi.json`、
   `/docs/oauth2-redirect`——最后那条是 Swagger UI 的 OAuth2 重定向页，
   最容易在「我只关了三处」的时候漏掉。
2. **它们不在免登录白名单里**。白名单是「允许匿名访问」的清单，
   而路由是否存在是另一件事：两条防线独立，任何一条单独失效都不该导致泄露。
3. **管理员那三条真的挂了 `require_admin`**。漏挂的代码看起来完全正常，
   而匿名访问会直接拿到完整 schema。

⚠️ 未登录打**任何**非白名单路径都是 401（`AuthMiddleware` 跑在路由之前），
所以「公开文档被关掉」在直连后端时表现为 401 而不是 404。404 只在**已登录**之后
才出现（那时才走到路由表）。这两个断言分开写，就是在钉这条时序。
"""

from __future__ import annotations

from conftest import register

from backend.api.deps import is_public_path
from backend.main import app
from backend.services import auth_service

# 框架默认会注册的四条。第三条（oauth2-redirect）是「只关了三处」的典型漏项。
PUBLIC_DOC_PATHS = ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect")

# 管理员专属的三条。顺序与页面上的入口一致。
ADMIN_DOC_PATHS = ("/api/admin/docs", "/api/admin/redoc", "/api/admin/openapi.json")


def _as_admin(client, username: str = "root") -> None:
    """注册并提权。`register` 会顺带登录，所以之后这个 client 就是管理员。"""
    register(client, username)
    auth_service.set_admin(username, True)


# ---------------------------------------------------------------------------
# 公开的四条：不存在，也不在白名单
# ---------------------------------------------------------------------------


def test_public_doc_routes_are_not_registered():
    """路由表里一条都不该有。

    直接读 `app.routes` 而不是发请求：请求会先经过中间件，看到的只是「被拦了」，
    看不出「路由到底挂没挂」。这条要的是后者。
    """
    paths = {getattr(route, "path", "") for route in app.routes}
    for path in PUBLIC_DOC_PATHS:
        assert path not in paths, f"{path} 仍然注册着，应改为管理员专属入口"


def test_public_doc_paths_are_not_whitelisted():
    """白名单里不能有它们——即使哪天有人把路由加回来，匿名也进不去。"""
    for path in PUBLIC_DOC_PATHS:
        assert is_public_path(path) is False


def test_public_doc_paths_closed_for_anonymous(client):
    """未登录：中间件先拦成 401（默认拒绝），不是 200、也不是 404。"""
    for path in PUBLIC_DOC_PATHS:
        response = client.get(path)
        assert response.status_code == 401, f"{path} 匿名访问应当 401：{response.text}"
        assert response.json()["code"] == "UNAUTHORIZED"


def test_public_doc_paths_404_for_logged_in_user(client):
    """已登录的**普通用户**走到路由层：路由不存在 → 404。

    这条与上一条合起来才说明「两层独立」：中间件放行之后，仍然没有东西可看。
    """
    register(client, "alice")
    for path in PUBLIC_DOC_PATHS:
        assert client.get(path).status_code == 404, f"{path} 对已登录用户应当是 404"


# ---------------------------------------------------------------------------
# 管理员的三条：401 / 403 / 200 三段都要分清楚
# ---------------------------------------------------------------------------


def test_admin_docs_require_login(client):
    for path in ADMIN_DOC_PATHS:
        response = client.get(path)
        assert response.status_code == 401, f"{path} 未登录应当 401"
        assert response.json()["code"] == "UNAUTHORIZED"


def test_admin_docs_reject_normal_user(client):
    """已登录但不是管理员 → 403，文案统一为「账号不存在」（ADR-049）。"""
    register(client, "alice")
    for path in ADMIN_DOC_PATHS:
        response = client.get(path)
        assert response.status_code == 403, f"{path} 普通用户应当 403"
        assert response.json()["detail"] == "账号不存在"


def test_admin_openapi_returns_full_schema(client):
    """管理员拿到的 schema 必须是真的全量：既有业务接口，也不含文档自身。"""
    _as_admin(client)
    response = client.get("/api/admin/openapi.json")

    assert response.status_code == 200
    spec = response.json()
    assert spec["openapi"].startswith("3.")
    assert "/api/jobs" in spec["paths"]
    # 文档自身 `include_in_schema=False`：否则 Swagger UI 里会多出一条
    # 「管理员：OpenAPI 定义」，点开是一个巨大的 JSON，纯噪音。
    assert "/api/admin/openapi.json" not in spec["paths"]


def test_admin_swagger_page_points_at_admin_schema(client):
    """页面里引用的 schema 地址必须是管理员那条，不能落回 `/openapi.json`。"""
    _as_admin(client)
    response = client.get("/api/admin/docs")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "/api/admin/openapi.json" in response.text
    # 落回公开地址的表现是「页面能打开、但一直转圈加载不出接口」——
    # 因为那条路由已经不存在了。这条断言就是为了不让它悄悄发生。
    assert 'url: "/openapi.json"' not in response.text


def test_admin_redoc_page_points_at_admin_schema(client):
    _as_admin(client)
    response = client.get("/api/admin/redoc")

    assert response.status_code == 200
    assert "/api/admin/openapi.json" in response.text
