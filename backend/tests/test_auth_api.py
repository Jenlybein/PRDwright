"""认证接口：注册 / 登录 / 登出 / 当前用户。

关注点是**对外契约**：状态码、错误文案、Cookie 属性、会话生命周期。
密码怎么算、邀请码怎么核销属于服务层，放在 `test_auth_service.py`。
"""

from __future__ import annotations

import pytest
from conftest import login, register

from backend.services import auth_service


def test_register_success_and_auto_login(client):
    """注册成功即下发会话，用户不必再登录一次。"""
    response = register(client, username="alice")

    assert response.status_code == 200
    body = response.json()
    assert body["username"] == "alice"
    assert isinstance(body["id"], int) and body["id"] > 0

    # 注册完直接就是已登录状态
    assert client.get("/api/auth/me").status_code == 200


def test_register_response_leaks_no_credentials(client):
    """响应体里绝不能出现密码哈希或 salt。"""
    body = register(client).json()
    assert set(body) == {"id", "username"}
    assert "password" not in body
    assert "salt" not in body


def test_register_sets_hardened_cookie(client):
    """会话 Cookie 必须带 HttpOnly / SameSite / Path。

    HttpOnly 让 XSS 拿不走登录态；SameSite=lax 挡住大部分跨站请求伪造。
    """
    set_cookie = register(client).headers.get("set-cookie", "")

    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Path=/" in set_cookie
    # 本地 http 调试下不能强制 secure，否则浏览器根本不会回传
    assert "Secure" not in set_cookie


def test_cookie_becomes_secure_when_the_switch_is_on(client):
    """`auth_cookie_secure=true` 时 `Set-Cookie` 必须带 `Secure`。

    上面那条只守了「默认关着」的一半——而真正会在生产出事的是另一半：
    开关打开了却没被读到（典型成因是签发时读了启动期的配置快照），
    表现恰好是「本地全绿、线上把会话 token 明文发在 HTTP 上」。

    因此这里走**热配置**（管理员在后台打开），既钉住「开关真的被读」，
    也钉住它「下一次签发即生效」——`set_session_cookie` 必须在签发那一刻现读配置，
    不能在启动时缓存。
    """
    # 起点是「关着」：本机 http 调试场景
    assert "Secure" not in register(client, username="root").headers.get("set-cookie", "")

    auth_service.set_admin("root", True)
    patched = client.patch("/api/admin/config", json={"updates": {"auth_cookie_secure": True}})
    assert patched.status_code == 200, patched.text

    # 不需要重启，下一次登录签发的 Cookie 就得带上 Secure
    set_cookie = login(client, "root").headers.get("set-cookie", "")
    assert "Secure" in set_cookie
    # 打开 secure 不能把另外三项保护弄丢
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Path=/" in set_cookie


@pytest.mark.parametrize(
    ("username", "password", "invite_code", "expected"),
    [
        ("alice", "password123", "wrong-invite", 400),  # 邀请码无效
        ("ab", "password123", "test-invite", 422),  # 用户名过短
        ("alice", "short", "test-invite", 422),  # 密码过短
    ],
)
def test_register_rejects_invalid_input(client, username, password, invite_code, expected):
    """非法输入按类型分开：业务规则 400，字段格式 422。"""
    response = register(client, username=username, password=password, invite_code=invite_code)
    assert response.status_code == expected


def test_register_rejects_duplicate_username(client):
    """重名注册失败。"""
    assert register(client, username="alice").status_code == 200
    response = register(client, username="alice")
    assert response.status_code == 400
    assert "已被注册" in response.json()["detail"]


def test_register_does_not_consume_invite_code_on_duplicate(client):
    """重名时不能白白核销掉一次邀请额度。

    顺序必须是「先查重、再核销」；反过来实现的话，用户会因为打错一个字
    而损失一次邀请机会。用一个限量码来暴露这个顺序问题。
    """
    from conftest import invite_codes as InviteCodeRepository
    # 先用一个不受限的码建好 alice，再拿限量码去撞重名——
    # 这样撞的确实是一次「重名」而不是「额度用尽」，才能验证先后顺序
    assert register(client, username="alice").status_code == 200
    InviteCodeRepository.create("once-only", max_uses=1, expires_at=None, note="限量码")

    assert register(client, username="alice", invite_code="once-only").status_code == 400
    # 若上面的重名判定发生在核销之后，这一条就会被限额挡住
    assert register(client, username="bob", invite_code="once-only").status_code == 200


def test_login_success(client):
    """登录成功后能拿到登录态，且用户正确。"""
    register(client, username="alice", password="password123")
    client.post("/api/auth/logout")

    response = login(client, username="alice", password="password123")
    assert response.status_code == 200
    assert response.json()["username"] == "alice"
    assert client.get("/api/auth/me").json()["username"] == "alice"


def test_login_failure_messages_are_identical(client):
    """「用户不存在」与「密码错误」必须返回同一句话，否则等于提供了账号枚举接口。"""
    register(client, username="alice", password="password123")
    client.post("/api/auth/logout")

    wrong_password = login(client, username="alice", password="wrong-password")
    no_such_user = login(client, username="ghost", password="password123")

    assert wrong_password.status_code == 401
    assert no_such_user.status_code == 401
    assert wrong_password.json()["detail"] == no_such_user.json()["detail"]


def test_logout_revokes_session_on_server_side(client):
    """登出必须让服务端会话失效——只删 Cookie 的话，token 捡回来还能用。"""
    register(client)
    assert client.get("/api/auth/me").status_code == 200

    stolen_token = client.cookies.get("prdwright_session")
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401

    # 手动把旧 token 塞回 Cookie，服务端仍应拒绝
    client.cookies.set("prdwright_session", stolen_token)
    assert client.get("/api/auth/me").status_code == 401


def test_logout_is_idempotent(client):
    """没有登录态时登出也返回成功，前端无需区分处理。"""
    assert client.post("/api/auth/logout").status_code == 200


def test_second_login_revokes_the_first_cookie(client):
    """二次登录后旧 Cookie 立即 401——这是**单点登录**，不是「多端并存」。

    这条契约在 ADR-024 里明确接受了一个代价：同一账号在另一台设备登录，这边会被踢下线。
    换来的是「不需要再维护会话版本号 + 乐观锁」——那份状态本身才是真正会出错的地方。

    旧用例曾断言「多次登录签发多个互不影响的会话」（多端并存），那是改动前的行为，
    这里连同测试一起改口径。
    """
    register(client, username="alice", password="password123")
    first_token = client.cookies.get("prdwright_session")
    assert client.get("/api/auth/me").status_code == 200

    login(client, username="alice", password="password123")
    second_token = client.cookies.get("prdwright_session")
    assert first_token != second_token, "每次登录都应签发新的会话凭据"

    # 新 Cookie 可用
    assert client.get("/api/auth/me").status_code == 200

    # 换回旧 Cookie：已被新登录吊销
    client.cookies.set("prdwright_session", first_token)
    assert client.get("/api/auth/me").status_code == 401


def test_first_login_does_not_affect_a_different_account(client):
    """别人的登录态不受影响——单点登录只在同一个账号内生效。"""
    register(client, username="alice", password="password123")
    alice_token = client.cookies.get("prdwright_session")

    register(client, username="bob", password="password123")
    bob_token = client.cookies.get("prdwright_session")
    assert bob_token != alice_token

    login(client, username="alice", password="password123")

    # bob 刚拿到的 Cookie 仍然有效
    client.cookies.set("prdwright_session", bob_token)
    assert client.get("/api/auth/me").status_code == 200


def test_expired_session_is_rejected(client):
    """会话过期后必须失效。

    这里不改动数据库，而是直接换一个「签发即过期」的签发器，
    让过期判断走真实的代码路径（`fetch_valid` 的 SQL 里带 expires_at 条件）。
    """
    auth_service._issuer = auth_service.OpaqueSessionIssuer(-1 / 3600)  # noqa: SLF001 - 测试用负 TTL

    assert register(client).status_code == 200
    assert client.get("/api/auth/me").status_code == 401
