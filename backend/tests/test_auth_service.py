"""认证服务层：密码哈希、邀请码核销、启动播种、归属兜底。

这里测的是**规则本身**，不经过 HTTP。接口层的契约测试在 `test_auth_api.py`。
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from backend.api.deps import acting_user
from backend.core.config import get_settings
from backend.core.db import init_schema
from backend.main import register_error_handlers
from backend.repositories.auth_repo import (
    AuthSessionRepository,
    InviteCodeRepository,
    UserRepository,
)
from backend.services import auth_service


# ---------------------------------------------------------------------------
# 密码哈希
# ---------------------------------------------------------------------------


def test_same_password_yields_different_hashes():
    """每个用户独立 salt，因此相同密码的哈希值也不同。

    这条性质决定「彩虹表 / 批量撞库」不能一次命中一片用户。
    """
    hash_a, salt_a = auth_service.hash_password("password123")
    hash_b, salt_b = auth_service.hash_password("password123")

    assert salt_a != salt_b
    assert hash_a != hash_b


def test_verify_password_roundtrip():
    """正确密码通过，错误密码拒绝。"""
    password_hash, salt = auth_service.hash_password("password123")

    assert auth_service.verify_password("password123", password_hash, salt)
    assert not auth_service.verify_password("password124", password_hash, salt)


def test_hash_is_not_plain_text():
    """库里存的是派生结果，明文密码不应出现在哈希里。"""
    password_hash, _ = auth_service.hash_password("password123")
    assert "password123" not in password_hash


# ---------------------------------------------------------------------------
# 注册规则
# ---------------------------------------------------------------------------


def test_register_rejects_short_username():
    with pytest.raises(auth_service.AuthError, match="用户名长度"):
        auth_service.register("ab", "password123", "test-invite")


def test_register_rejects_short_password():
    with pytest.raises(auth_service.AuthError, match="密码至少需要"):
        auth_service.register("alice", "1234567", "test-invite")


def test_register_rejects_blank_invite_code():
    with pytest.raises(auth_service.AuthError, match="邀请码"):
        auth_service.register("alice", "password123", "   ")


def test_invite_code_must_exist():
    """不存在的邀请码不能注册。"""
    with pytest.raises(auth_service.AuthError, match="邀请码无效或已用完"):
        auth_service.register("alice", "password123", "not-a-real-code")


def test_registered_user_can_log_in():
    """注册后能用同一组凭据登录，且拿到的用户一致。"""
    auth_service.ensure_bootstrap()  # 播种 .env 里配置的初始邀请码
    created = auth_service.register("alice", "password123", "test-invite")
    user, credential, _ = auth_service.login("alice", "password123")

    assert user.id == created.id
    assert user.username == "alice"
    assert credential  # 签发了非空凭据


def test_unlimited_invite_code_is_reusable():
    """`max_uses=0` 表示不限次数，可反复用于注册。"""
    InviteCodeRepository.create("unlimited", max_uses=0, expires_at=None, note="不限量")

    assert auth_service.register("alice", "password123", "unlimited").username == "alice"
    assert auth_service.register("bob", "password123", "unlimited").username == "bob"


def test_limited_invite_code_runs_out():
    """限量码用尽后必须拒绝注册。"""
    InviteCodeRepository.create("once-only", max_uses=1, expires_at=None, note="限量码")

    auth_service.register("alice", "password123", "once-only")
    with pytest.raises(auth_service.AuthError, match="邀请码无效或已用完"):
        auth_service.register("bob", "password123", "once-only")


def test_expired_invite_code_is_rejected():
    """过期邀请码不能注册。"""
    InviteCodeRepository.create(
        "stale", max_uses=0, expires_at=datetime(2000, 1, 1), note="已过期"
    )

    with pytest.raises(auth_service.AuthError, match="邀请码无效或已用完"):
        auth_service.register("alice", "password123", "stale")


def test_invite_code_consumption_is_atomic():
    """并发抢同一个限量码时，成功的次数必须等于额度，不能超发。

    这是「用一条 UPDATE 语句 + rowcount 判断」而不是「先 SELECT 再 UPDATE」的原因：
    后者在并发下必然超发。
    """
    InviteCodeRepository.create("hot", max_uses=2, expires_at=None, note="并发抢购")

    results: list[bool] = []
    lock = threading.Lock()

    def try_consume() -> None:
        ok = InviteCodeRepository.consume("hot")
        with lock:
            results.append(ok)

    workers = [threading.Thread(target=try_consume) for _ in range(10)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert results.count(True) == 2, f"限量 2 次却成功核销了 {results.count(True)} 次"


# ---------------------------------------------------------------------------
# 运维通道：直接建号 / 重置密码
# ---------------------------------------------------------------------------


def test_create_user_direct_needs_no_invite_code():
    """运维建号不走邀请码——邀请码表为空时也能建。"""
    assert InviteCodeRepository.count() == 0

    user = auth_service.create_user_direct("alice", "password123")
    assert user.username == "alice"
    assert auth_service.login("alice", "password123")[0].id == user.id


def test_create_user_direct_still_enforces_password_policy():
    """免邀请码不等于免校验，弱密码同样拒绝。

    这条守的是「运维通道不能成为绕过安全策略的后门」。
    """
    with pytest.raises(auth_service.AuthError, match="密码至少需要"):
        auth_service.create_user_direct("alice", "123")
    with pytest.raises(auth_service.AuthError, match="用户名长度"):
        auth_service.create_user_direct("ab", "password123")


def test_create_user_direct_rejects_duplicate():
    auth_service.create_user_direct("alice", "password123")
    with pytest.raises(auth_service.AuthError, match="已被注册"):
        auth_service.create_user_direct("alice", "password123")


def test_reset_password_replaces_old_credential():
    """改密后旧密码失效、新密码可用。"""
    auth_service.create_user_direct("alice", "old-password")
    auth_service.reset_password("alice", "new-password")

    with pytest.raises(auth_service.AuthError):
        auth_service.login("alice", "old-password")
    assert auth_service.login("alice", "new-password")[0].username == "alice"


def test_reset_password_rejects_unknown_user():
    with pytest.raises(auth_service.AuthError, match="该用户不存在"):
        auth_service.reset_password("ghost", "password123")


def test_reset_password_enforces_policy():
    """重置密码同样受强度约束。"""
    auth_service.create_user_direct("alice", "old-password")
    with pytest.raises(auth_service.AuthError, match="密码至少需要"):
        auth_service.reset_password("alice", "123")


def test_list_users_exposes_no_credentials():
    """用户列表不能带出哈希或 salt，否则运维工具就成了泄露面。

    键集合是**精确断言**：多返回一个字段（哪怕是 `is_admin` 这种无害的）也要
    在这里显式确认过，避免「顺手把某个敏感列也查出来」混在改动里溜过去。
    """
    auth_service.create_user_direct("alice", "password123")

    rows = auth_service.list_users()
    assert len(rows) == 1
    assert set(rows[0]) == {"id", "username", "created_at", "is_active", "is_admin"}


def test_delete_user_removes_the_account():
    """删号后该账号不能再登录。"""
    auth_service.create_user_direct("alice", "password123")
    auth_service.delete_user("alice")

    assert UserRepository.get_by_username("alice") is None
    with pytest.raises(auth_service.AuthError):
        auth_service.login("alice", "password123")


def test_delete_user_rejects_unknown_user():
    with pytest.raises(auth_service.AuthError, match="该用户不存在"):
        auth_service.delete_user("ghost")


def test_delete_user_invalidates_existing_sessions():
    """删号必须让该用户已签发的会话立刻失效——等于强制下线。

    否则会出现「账号已经不存在，但拿着旧 Cookie 还能继续用」的窗口。
    这里同时断言会话记录本身被清掉，避免表里留下指向空用户的孤儿行。
    """
    auth_service.create_user_direct("alice", "password123")
    _, credential, _ = auth_service.login("alice", "password123")
    token_hash = auth_service._sha256(credential)  # noqa: SLF001 - 测试里需要定位会话记录
    assert AuthSessionRepository.fetch_valid(token_hash) is not None

    auth_service.delete_user("alice")

    assert auth_service.current_user(credential) is None
    assert AuthSessionRepository.fetch_valid(token_hash) is None


# ---------------------------------------------------------------------------
# 单点登录：新登录吊销旧会话（ADR-024）
# ---------------------------------------------------------------------------


def test_second_login_revokes_the_first_session():
    """二次登录后，第一个会话立即失效——单点登录的核心契约。"""
    auth_service.create_user_direct("alice", "password123")
    _, first, _ = auth_service.login("alice", "password123")
    assert auth_service.current_user(first) is not None

    _, second, _ = auth_service.login("alice", "password123")

    assert second != first
    assert auth_service.current_user(first) is None, "旧会话应当被新登录吊销"
    assert auth_service.current_user(second) is not None


def test_replace_for_user_keeps_exactly_one_session_per_user():
    """换发之后，该用户在库里**只剩一条**会话记录。

    只删「已过期」的那些是不够的：单点登录要的是「新登录让旧的立刻失效」，
    留一条还没过期的旧会话就等于留着一个可用的第二登录态。
    """
    auth_service.create_user_direct("alice", "password123")
    user_id = int(UserRepository.get_by_username("alice")["id"])  # type: ignore[index]
    _, first, _ = auth_service.login("alice", "password123")
    _, second, _ = auth_service.login("alice", "password123")

    rows = AuthSessionRepository.list_by_user(user_id)
    assert len(rows) == 1
    assert rows[0]["token_hash"] == auth_service._sha256(second)  # noqa: SLF001
    assert rows[0]["token_hash"] != auth_service._sha256(first)  # noqa: SLF001


def test_one_users_login_does_not_touch_another():
    """单点登录只清自己的会话——别人的登录态不该受影响。"""
    auth_service.create_user_direct("alice", "password123")
    auth_service.create_user_direct("bob", "password123")
    _, alice_first, _ = auth_service.login("alice", "password123")
    _, bob, _ = auth_service.login("bob", "password123")

    auth_service.login("alice", "password123")

    assert auth_service.current_user(bob) is not None
    assert auth_service.current_user(alice_first) is None


def test_replace_for_user_is_atomic():
    """换发是**一条事务**：插入失败时旧会话必须还在。

    这条守的是「先删后插、中间不留窗口」。若实现分两次提交，插入失败（这里故意让它撞上
    主键）就会留下「旧的已删、新的没立」的库状态——用户刚登录成功却判定未登录。
    """
    auth_service.create_user_direct("alice", "password123")
    auth_service.create_user_direct("bob", "password123")
    _, alice_token, _ = auth_service.login("alice", "password123")
    _, bob_token, _ = auth_service.login("bob", "password123")

    alice_id = int(UserRepository.get_by_username("alice")["id"])  # type: ignore[index]
    # 拿 bob 的哈希去给 alice 换发 → 撞 `auth_sessions` 的主键
    with pytest.raises(sqlite3.IntegrityError):
        AuthSessionRepository.replace_for_user(
            auth_service._sha256(bob_token),  # noqa: SLF001
            alice_id,
            datetime(2099, 1, 1),
        )

    assert auth_service.current_user(alice_token) is not None, "插入失败后旧会话不该被删掉"
    assert AuthSessionRepository.fetch_valid(
        auth_service._sha256(alice_token)  # noqa: SLF001
    ) is not None


# ---------------------------------------------------------------------------
# 启动播种
# ---------------------------------------------------------------------------


def test_bootstrap_seeds_configured_invite_code(monkeypatch):
    """首次启动时自动播种 `.env` 里的邀请码，省去手工插库。"""
    monkeypatch.setenv("INVITE_CODE", "seed-me")
    get_settings.cache_clear()
    init_schema()
    auth_service.ensure_bootstrap()

    assert InviteCodeRepository.get("seed-me") is not None


def test_bootstrap_creates_initial_admin_when_configured(monkeypatch):
    """配置 `INIT_ADMIN_USER`/`INIT_ADMIN_PASSWORD` 时，首个账号自动创建。"""
    monkeypatch.setenv("INIT_ADMIN_USER", "root")
    monkeypatch.setenv("INIT_ADMIN_PASSWORD", "root-password")
    get_settings.cache_clear()
    init_schema()
    auth_service.ensure_bootstrap()

    row = UserRepository.get_by_username("root")
    assert row is not None
    # 自动创建的账号要能直接登录
    user, _, _ = auth_service.login("root", "root-password")
    assert user.username == "root"


def test_bootstrap_does_not_overwrite_existing_data(monkeypatch):
    """已有人注册过就不再创建初始管理员——否则每次重启都会往库里塞账号。

    顺序：先在没有管理员配置的状态下启动一次并注册一个用户，
    再带着管理员配置启动第二次，此时不该凭空多出 root。
    """
    get_settings.cache_clear()
    init_schema()
    auth_service.ensure_bootstrap()

    auth_service.register("alice", "password123", "test-invite")

    monkeypatch.setenv("INIT_ADMIN_USER", "root")
    monkeypatch.setenv("INIT_ADMIN_PASSWORD", "root-password")
    get_settings.cache_clear()
    auth_service.ensure_bootstrap()

    assert UserRepository.get_by_username("root") is None
    assert UserRepository.get_by_username("alice") is not None


# ---------------------------------------------------------------------------
# 会话与归属
# ---------------------------------------------------------------------------


def test_invalid_credential_resolves_to_no_user():
    """伪造 / 空凭据都解析不出用户。"""
    assert auth_service.current_user("") is None
    assert auth_service.current_user("forged-token") is None


def test_dev_user_is_stable_and_not_loginable():
    """开发模式的 dev 用户重复取到的是同一个，且密码随机、无法登录。"""
    first = auth_service.ensure_dev_user()
    second = auth_service.ensure_dev_user()

    assert first.id == second.id
    assert first.username == "dev"
    with pytest.raises(auth_service.AuthError):
        auth_service.login("dev", "anything")


def test_acting_user_falls_back_to_dev_when_auth_disabled(monkeypatch):
    """`AUTH_ENABLED=false` 时业务数据的归属落到 dev 用户，`user_id` 恒非空。"""
    monkeypatch.setenv("AUTH_ENABLED", "false")
    get_settings.cache_clear()
    init_schema()

    probe = FastAPI()

    @probe.get("/who")
    def who(request: Request) -> dict:
        """探针路由：返回 `acting_user` 解析出的归属用户名。"""
        return {"username": acting_user(request).username}

    with TestClient(probe) as client:
        assert client.get("/who").json() == {"username": "dev"}


def test_acting_user_raises_when_auth_enabled_and_anonymous():
    """鉴权开启且没有登录态时，取归属直接 401——不能悄悄放行到 dev 用户。

    这条是 `acting_user` 与「关闭鉴权」之间的边界：dev 兜底只在开关关掉时生效。

    探针应用要装上生产用的错误处理器（`register_error_handlers`）：`acting_user` 抛的
    是 `AppError`，裸 `FastAPI()` 不认识它，不装的话请求会 500 而不是 401。
    """
    probe = FastAPI()
    register_error_handlers(probe)

    @probe.get("/who")
    def who(request: Request) -> dict:
        """探针路由：返回 `acting_user` 解析出的归属用户名。"""
        return {"username": acting_user(request).username}

    with TestClient(probe) as client:
        response = client.get("/who")

    assert response.status_code == 401
    assert response.json()["detail"] == "请先登录后再使用该功能"
