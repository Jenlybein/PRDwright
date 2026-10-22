"""管理后台登录入口（ADR-049）：**只允许管理员账号登入**。

这份用例钉的是三件容易混淆、而混淆后都很难看出来的事：

1. **非管理员登不进去**：普通账号拿正确密码来后台登录，必须 401「账号不存在」。
2. **失败不能有副作用**：不签发会话、不顶掉他在主站上的那条会话。只判状态码会
   漏掉后者——「登录被拒，但人已经被登出了」是比「登录被拒」更难解释的故障。
3. **不许在密码之前泄露管理员名单**：先查 `is_admin` 再验密码的实现，会让
   「猜一个用户名是不是管理员」变成一个不需要密码就能问出来的问题。

`require_admin` 那一层（已登录的非管理员访问 `/api/admin/**`）在
`test_admin_access.py` 里，这里只补文案断言。
"""

from __future__ import annotations

import pytest
from conftest import register

from backend.api import login_flow
from backend.repositories.auth_repo import LoginFailureRepository, UserRepository
from backend.services import auth_service

NOT_ADMIN_DETAIL = "账号不存在"
BAD_CREDENTIALS_DETAIL = "用户名或密码错误"


def _admin_login(client, username="root", password="password123"):
    """辅助函数：走管理后台的登录入口。"""
    return client.post(
        "/api/admin/login", json={"username": username, "password": password}
    )


def _make_admin(client, username="root", password="password123"):
    """建一个管理员账号，并把客户端退回未登录状态。"""
    assert register(client, username, password).status_code == 200
    auth_service.set_admin(username, True)
    client.post("/api/auth/logout")


# ---------------------------------------------------------------------------
# 正路
# ---------------------------------------------------------------------------


def test_admin_can_log_in_from_the_console(client):
    """管理员登录：200 + 下发会话，随后能进后台。"""
    _make_admin(client)

    response = _admin_login(client)

    assert response.status_code == 200
    assert response.json()["username"] == "root"
    assert "set-cookie" in response.headers
    assert client.get("/api/admin/me").status_code == 200


def test_admin_login_is_reachable_without_a_session(client):
    """登录接口本身必须在白名单里。

    白名单漏了它的症状很隐蔽：未登录时它先被中间件拦成
    「请先登录后再使用该功能」——一个**永远登不进去**的登录接口。
    """
    response = _admin_login(client)

    assert response.status_code == 401
    # 走到路由层才会给出这句；中间件那句是「请先登录后再使用该功能」
    assert response.json()["detail"] == BAD_CREDENTIALS_DETAIL


# ---------------------------------------------------------------------------
# 非管理员：账号不存在
# ---------------------------------------------------------------------------


def test_normal_user_cannot_log_in_to_the_console(client):
    """普通账号（密码正确）登录后台：401「账号不存在」。

    文案刻意不是「无权限」：后台对外只承认管理员账号存在（ADR-049）。
    """
    register(client, "alice", "password123")
    client.post("/api/auth/logout")

    response = _admin_login(client, "alice", "password123")

    assert response.status_code == 401
    assert response.json()["detail"] == NOT_ADMIN_DETAIL


def test_rejected_login_does_not_hand_out_a_session(client):
    """被拒的登录不能留下任何登录态。

    断言 `set-cookie` 不存在，而不是「本次请求没带 Cookie」——后者在客户端已有
    Cookie 时永远为真，看起来像断言、实际什么也没验。
    """
    register(client, "alice", "password123")
    client.post("/api/auth/logout")

    response = _admin_login(client, "alice", "password123")

    assert "set-cookie" not in response.headers
    assert client.get("/api/admin/me").status_code == 401


def test_rejected_login_does_not_kick_the_user_off_the_main_app(client):
    """后台登录被拒时，他在主站上的会话必须完好。

    实现上很容易踩到：先调 `auth_service.login` 再判 `is_admin`——那时会话已经
    被 `replace()` 换掉了，人会「因为去后台登了一次而被踢出主站」。
    """
    register(client, "alice", "password123")
    main_app_token = client.cookies.get("prdwright_session")

    response = _admin_login(client, "alice", "password123")

    assert response.status_code == 401
    assert client.cookies.get("prdwright_session") == main_app_token
    assert client.get("/api/auth/me").json()["username"] == "alice"


def test_wrong_password_says_credentials_wrong_for_admins_too(client):
    """密码错就是密码错：不能因为「这个账号是管理员」而换一句提示。

    这条守的是顺序——**先验密码、再判身份**。反过来的实现在不知道密码的情况下
    就能区分「管理员」与「普通账号」，等于送出一份管理员名单。
    """
    _make_admin(client, "root", "password123")
    register(client, "alice", "password123")
    client.post("/api/auth/logout")

    as_admin = _admin_login(client, "root", "wrong-password")
    as_user = _admin_login(client, "alice", "wrong-password")

    assert as_admin.status_code == as_user.status_code == 401
    assert as_admin.json()["detail"] == as_user.json()["detail"] == BAD_CREDENTIALS_DETAIL


def test_inactive_admin_cannot_log_in(client):
    """停用的管理员同样进不来，且提示是「已停用」而不是「账号不存在」。

    这不是泄露：能走到这句的人已经证明了密码正确，此时告诉他「账号被停用」
    才是可行动的信息。
    """
    register(client, "root", "password123")
    auth_service.set_admin("root", True)
    client.post("/api/auth/logout")
    row = UserRepository.get_by_username("root")
    assert row is not None
    UserRepository.set_active(int(row["id"]), False)

    response = _admin_login(client, "root", "password123")

    assert response.status_code == 401
    assert response.json()["detail"] == "该账号已被停用"


# ---------------------------------------------------------------------------
# 限流接线：管理端入口也必须先等
# ---------------------------------------------------------------------------


def test_admin_login_waits_before_checking(client, monkeypatch):
    """管理端登录同样要付渐进延时的账。

    这条防的是「新开一条登录入口但忘了接延时」——那等于给爆破开了一条无成本通道，
    而它看起来完全正常：登录成功、登录失败、文案都对。
    """
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(login_flow, "_sleep", fake_sleep)
    register(client, "alice", "password123")
    client.post("/api/auth/logout")
    # 阈值（默认 5）刚好用满：下一次尝试应当等 2 秒
    for _ in range(5):
        LoginFailureRepository.record("alice", "127.0.0.1")

    _admin_login(client, "alice", "password123")

    assert recorded == [pytest.approx(2.0)]


# ---------------------------------------------------------------------------
# 已登录的非管理员：文案与状态码
# ---------------------------------------------------------------------------


def test_normal_user_gets_account_not_exist_from_admin_api(client):
    """已登录的非管理员直接敲管理接口：403 +「账号不存在」。

    状态码保留 403（不是 401）：前端靠它区分「去登录」与「这个身份不行」。
    变的只是文案——原来那句「需要管理员权限」等于确认了「你这个账号是存在的」。
    """
    register(client, "alice", "password123")

    response = client.get("/api/admin/me")

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
    assert response.json()["detail"] == NOT_ADMIN_DETAIL
