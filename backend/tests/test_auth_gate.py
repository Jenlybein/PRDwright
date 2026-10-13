"""认证中间件：默认拒绝 + 白名单放行。

这批测试守的是**安全边界**，所以它比一般的接口测试更「硬」：
除了逐个接口的行为断言，还有一条不变量测试——任何不在白名单里的路由，
未登录访问一律 401。将来新增接口若忘了考虑鉴权，这条测试会直接失败。
"""

from __future__ import annotations

import re

import pytest

from backend.api.deps import is_public_path
from backend.main import app

UNAUTHORIZED_DETAIL = "请先登录后再使用该功能"

# 需要登录的接口清单（用于逐个断言，读起来比不变量测试直观）
PROTECTED_POST_ENDPOINTS = [
    "/api/conversation/start-stream",
    "/api/conversation/continue-stream",
    "/api/conversation/sync-summary-from-conversation",
    "/api/conversation/retrieve-api-docs-rag",
    "/api/conversation/generate-prd-stream",
    "/api/conversation/generate-prd-from-summary-stream",
    "/api/conversation/generate-api-docs-stream",
    "/api/conversation/generate-prompts-stream",
    "/api/conversation/optimize-document-stream",
    # 方案接口：新增时**没有**做任何鉴权相关的工作，它们是被中间件默认拦下的。
    # 这条清单只用于逐个断言，真正兜底的是下面的不变量测试。
    "/api/solutions",
]

_PATH_PARAM = re.compile(r"\{[^}]+\}")


def _iter_operations():
    """遍历 OpenAPI 里声明的全部 (方法, 原始路径, 可请求路径)。

    路径参数（如 `{conversation_id}`）替换成占位串，才能真的发出请求。
    """
    schema = app.openapi()
    for raw_path, operations in schema["paths"].items():
        for method in operations:
            if method not in ("get", "post", "put", "delete", "patch"):
                continue
            yield method, raw_path, _PATH_PARAM.sub("dummy", raw_path)


def test_public_paths_are_reachable_without_login(client):
    """白名单内的接口未登录也能访问。"""
    assert client.get("/").status_code == 200
    assert client.get("/health").status_code == 200
    assert client.get("/api/conversation/questions").status_code == 200


@pytest.mark.parametrize("endpoint", PROTECTED_POST_ENDPOINTS)
def test_llm_endpoints_require_login(client, endpoint):
    """消耗模型额度或涉及内部资产的接口，未登录一律 401。

    断言 `detail` 而不是只断言状态码：统一文案是为了不泄露「未登录」与
    「会话过期」的区别，改动文案时这条测试会提醒你重新想一遍。
    """
    response = client.post(endpoint, json={})
    assert response.status_code == 401
    assert response.json()["detail"] == UNAUTHORIZED_DETAIL


def test_every_non_public_route_is_denied(client):
    """不变量：白名单之外的路由，未登录访问必须 401。

    这条测试的意义在于**覆盖将来新增的接口**——新路由不写测试也会自动被检查，
    正是「默认拒绝」相对「逐个挂 Depends」的价值所在。
    """
    denied = []
    for method, raw_path, request_path in _iter_operations():
        if is_public_path(raw_path):
            continue
        if client.request(method.upper(), request_path, json={}).status_code != 401:
            denied.append(f"{method.upper()} {raw_path}")

    assert not denied, f"以下路由未登录却能被访问：{denied}"


def test_public_route_set_is_explicit(client):  # noqa: ARG001 - 仅借用建好库的夹具
    """白名单必须小而明确，避免有人图省事把业务接口塞进来。

    这里用「白名单里不该出现 conversation 的写接口」来兜底。
    """
    for endpoint in PROTECTED_POST_ENDPOINTS:
        assert not is_public_path(endpoint)


def test_options_preflight_is_not_blocked(client):
    """跨域预检请求必须放行，否则浏览器会把它当成接口 401。

    预检按规范不带凭据，放行它不等于放开安全边界——真实请求仍会被校验。
    """
    response = client.options(
        "/api/conversation/start-stream",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"},
    )
    assert response.status_code != 401


def test_me_endpoint_requires_cookie_even_though_public(client):
    """`/api/auth/me` 在白名单里，但它自己解析 Cookie，没登录态仍返回 401。

    白名单的意思是「不被中间件拦掉」，而不是「谁都能拿到数据」。
    """
    response = client.get("/api/auth/me")
    assert response.status_code == 401
    assert response.json()["detail"] == "当前未登录"


def test_auth_disabled_passes_everything_through(auth_off_client):
    """`AUTH_ENABLED=false` 时全放行，用于本地无登录调试。

    注意 `/api/auth/me` 仍然 401——它没有 Cookie 可解析，与鉴权开关无关。
    """
    assert auth_off_client.get("/health").status_code == 200
    assert auth_off_client.get("/api/conversation/questions").status_code == 200
    assert auth_off_client.get("/api/auth/me").status_code == 401

    for endpoint in PROTECTED_POST_ENDPOINTS:
        assert auth_off_client.post(endpoint, json={}).status_code != 401
