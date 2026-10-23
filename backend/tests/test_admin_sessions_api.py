"""管理员强制下线（v4.5 T5-6）。

三个要点
--------
1. **只吊销会话，不改密码、不禁登录**——这是「踢下线」而不是「封号」。
   所以吊销之后，用原密码必须还能立刻登回来。
2. **`revoked=0` 是正常结果**：单点登录下每人至多一条会话，0 表示「他当时没登录」。
   若这里报错，管理员会以为操作失败而反复点击。
3. **必须留痕**：踢人是干预他人账号的动作，事后要能查出是谁、什么时候、踢了几条。
"""

from __future__ import annotations

from conftest import login, register, users

from backend.core.config import get_settings
from backend.services import admin_service, auth_service
from conftest import auth_sessions as AuthSessionRepository
def _user_id(username: str) -> int:
    row = users.get_by_username(username)
    assert row is not None
    return int(row["id"])


def _as_admin(client, username: str = "root") -> None:
    """注册并提升为管理员，然后**保持管理员身份登录**。

    ⚠️ 注册别的用户会把客户端的 Cookie 换成那个用户，后续请求就不是管理员了
    （症状是 403）。所以凡是「先用管理员动作、中途又注册了别人」的用例，
    都必须再 `login(client, "root")` 回到管理员会话——下面几处都是这个原因。
    """
    register(client, username)
    auth_service.set_admin(username, True)
    assert client.get("/api/admin/me").status_code == 200


def _as_admin_again(client, username: str = "root") -> None:
    """在以别人的身份注册之后，重新切回管理员会话。"""
    assert login(client, username).status_code == 200


def test_admin_can_revoke_and_user_is_logged_out(client):
    """吊销后该用户的凭据立刻失效。

    这里**不能**用 `client.get("/api/auth/me")` 来验证：吊销动作必须由管理员发起，
    而客户端同一时刻只持有一份 Cookie——切到管理员身份后，那个请求验的是管理员的会话。
    所以直接拿 alice 自己的凭据去问服务层「这个 token 还认不认」，才是真的在测吊销。
    """
    _as_admin(client)
    register(client, "alice")  # 注册即登录，alice 有一条会话
    alice_cookie = client.cookies.get(get_settings().auth_cookie_name)
    assert alice_cookie, "注册后应当下发会话 Cookie"
    alice_id = _user_id("alice")
    assert auth_service.current_user(alice_cookie) is not None

    _as_admin_again(client)  # 切回管理员会话
    response = client.post(f"/api/admin/users/{alice_id}/sessions/revoke")

    assert response.status_code == 200, response.text
    assert response.json()["revoked"] == 1
    assert auth_service.current_user(alice_cookie) is None, "被吊销的凭据不该还能用"


def test_revoke_does_not_change_password(client):
    """踢下线 ≠ 封号：原密码仍然能立刻登回来。"""
    _as_admin(client)
    register(client, "alice")
    _as_admin_again(client)
    client.post(f"/api/admin/users/{_user_id('alice')}/sessions/revoke")

    assert login(client, "alice").status_code == 200
    assert client.get("/api/auth/me").json()["username"] == "alice"


def test_revoke_without_session_returns_zero(client):
    """没登录过的人也能点，返回 0 而不是报错。

    单点登录下每人至多一条会话，所以 0 是常见情况。若这里 4xx，
    管理员会以为「踢失败」而反复点击，实际上对方根本没在线。
    """
    _as_admin(client)
    register(client, "bob")
    bob_id = _user_id("bob")
    # 把 bob 的会话清掉，模拟「他早就不在线了」
    assert AuthSessionRepository.delete_by_user(bob_id) == 1
    _as_admin_again(client)

    response = client.post(f"/api/admin/users/{bob_id}/sessions/revoke")

    assert response.status_code == 200
    assert response.json()["revoked"] == 0


def test_unknown_user_returns_404(client):
    """任务不存在才报错——静默成功会让人以为「已经踢掉了」。"""
    _as_admin(client)

    response = client.post("/api/admin/users/999999/sessions/revoke")

    assert response.status_code == 404


def test_non_admin_is_forbidden(client):
    """`require_admin` 必须挡住普通用户：这条路径能踢掉任何人。"""
    register(client, "alice")
    alice_id = _user_id("alice")

    response = client.post(f"/api/admin/users/{alice_id}/sessions/revoke")

    assert response.status_code == 403
    # 而且确实没生效
    assert len(AuthSessionRepository.list_by_user(alice_id)) == 1


def test_revoke_is_audited(client):
    """每次强制下线都要留痕：踢了谁、踢了几条。"""
    _as_admin(client)
    register(client, "alice")
    _as_admin_again(client)

    client.post(f"/api/admin/users/{_user_id('alice')}/sessions/revoke")

    records = admin_service.list_audit(limit=10)
    matched = [r for r in records if r["action"] == "user.revoke_sessions"]
    assert matched, f"应当记一条 user.revoke_sessions，实际：{[r['action'] for r in records]}"
    assert matched[0]["target"] == "alice"
    assert "1 条" in matched[0]["detail"]
