"""管理员身份与权限（v4 地基）：列 → 服务层规则 → 接口层守卫。

这里钉住的是**整条链**，而不是其中一层。只测一层是不够的：例如「列加上了」
却忘了挂 `require_admin`，管理员与非管理员拿到的响应会一模一样，
而这类漏挂在评审时最容易混过去（代码看起来完全正常）。
"""

from __future__ import annotations

import pytest
from conftest import register

from backend.core.config import get_settings
from backend.core.db import connect, init_schema
from backend.core.migrations import MIGRATIONS, run_migrations
from backend.services import auth_service


# ---------------------------------------------------------------------------
# 迁移：列必须真的存在，且不顺手提权
# ---------------------------------------------------------------------------


def test_migration_adds_is_admin_column():
    """v12 给 `users` 加 `is_admin`。

    断言写成 `len(MIGRATIONS)` 而不是写死 12：写死的话，将来每加一步迁移
    这条无关用例都会跟着红一次，而它跟那些迁移毫无关系。
    """
    assert run_migrations() == len(MIGRATIONS)

    with connect() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    assert "is_admin" in columns


def test_migration_does_not_promote_existing_users():
    """升级**不能**把存量账号集体提权——默认值必须是 0。

    这条守的是「悄悄扩大权限面」：若默认值为 1，一次版本升级之后所有老用户
    都能进后台，而且整个过程没有任何报错、没有任何人点过确认。
    """
    run_migrations()
    auth_service.create_user_direct("alice", "password123")

    assert auth_service.login("alice", "password123")[0].is_admin is False


# ---------------------------------------------------------------------------
# 服务层：任命 / 撤销
# ---------------------------------------------------------------------------


def test_set_admin_promotes_a_normal_user():
    auth_service.create_user_direct("alice", "password123")
    assert auth_service.login("alice", "password123")[0].is_admin is False

    auth_service.set_admin("alice", True)

    assert auth_service.login("alice", "password123")[0].is_admin is True


def test_set_admin_rejects_unknown_user():
    with pytest.raises(auth_service.AuthError, match="该用户不存在"):
        auth_service.set_admin("ghost", True)


def test_last_admin_cannot_be_demoted():
    """撤销最后一位管理员必须被拒绝。

    这个状态**无法自助恢复**：任命管理员本身需要管理员权限，一旦归零就只剩
    手工改库一条路。宁可拒绝这次操作并把原因说清楚。
    """
    auth_service.create_user_direct("alice", "password123")
    auth_service.set_admin("alice", True)

    with pytest.raises(auth_service.AuthError, match="最后一位管理员"):
        auth_service.set_admin("alice", False)

    assert auth_service.login("alice", "password123")[0].is_admin is True


def test_demote_is_allowed_when_another_admin_remains():
    """还有别的管理员时，撤销是允许的——不变式是「至少一位」，不是「只能一位」。"""
    auth_service.create_user_direct("alice", "password123")
    auth_service.create_user_direct("bob", "password123")
    auth_service.set_admin("alice", True)
    auth_service.set_admin("bob", True)

    auth_service.set_admin("alice", False)

    assert auth_service.login("alice", "password123")[0].is_admin is False
    assert auth_service.login("bob", "password123")[0].is_admin is True


def test_bootstrap_marks_the_initial_account_as_admin(monkeypatch):
    """全新部署时自举出来的首个账号直接是管理员。

    否则会出现死锁：没有任何人能进后台，而「任命管理员」本身又需要管理员权限。
    """
    monkeypatch.setenv("INIT_ADMIN_USER", "root")
    monkeypatch.setenv("INIT_ADMIN_PASSWORD", "root-password")
    get_settings.cache_clear()
    init_schema()
    auth_service.ensure_bootstrap()

    assert auth_service.login("root", "root-password")[0].is_admin is True


# ---------------------------------------------------------------------------
# 接口层：require_admin（v4 的「门」）
# ---------------------------------------------------------------------------


def test_admin_me_returns_200_for_admin(client):
    """管理员访问管理员接口：放行。"""
    register(client, "alice")
    auth_service.set_admin("alice", True)

    response = client.get("/api/admin/me")

    assert response.status_code == 200
    assert response.json()["username"] == "alice"


def test_admin_me_returns_403_for_normal_user(client):
    """已登录但非管理员：必须是 **403 `FORBIDDEN`**，不能是 401。

    两者合成一种的话，前端只能给同一句文案，普通用户会被反复踢去登录页——
    而他们登录进去之后依然进不来，陷入死循环。
    """
    register(client, "alice")

    response = client.get("/api/admin/me")

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_admin_me_returns_401_when_anonymous(client):
    """未登录由中间件拦成 401：管理员接口不在白名单里，匿名请求到不了路由层。"""
    response = client.get("/api/admin/me")

    assert response.status_code == 401
