"""登录失败限流：渐进延时（v4.5 T5-3）。

为什么钉这些行为
----------------
防爆破最容易做错的两头，这个文件各钉一条：

- **做成硬锁**（失败 N 次禁止登录）会被人反过来利用：知道用户名就能故意输错，
  把别人挡在门外。所以这里断言的是「递增延时」，而不是「拒绝」。
- **只给真实账号计数**会制造新的枚举信号：有延时的账号就是存在的账号，
  于是「防账号枚举」那两条（统一文案、常量时间）全白做。所以不存在的用户名
  也必须计数、也必须延时。

延时的计算在服务层（`auth_service.login_delay`），真正的等待在路由层的
`api/login_flow.py::_sleep`——因为 `login()` 是同步函数，在里面 sleep 会卡死
整个事件循环。两者分开测：数值由服务层断言，接线由路由层断言。

⚠️ 这个模块（而不是某个具体路由）才是**唯一**的等待落点：用户端与管理端后台的
登录都经它，所以这里换掉假函数，两条入口同时被覆盖。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from conftest import login, register

from backend.api import login_flow
from backend.core.config import get_settings
from backend.core.db import connect, utc_now
from backend.repositories.auth_repo import AuthSessionRepository, LoginFailureRepository, UserRepository
from backend.services import auth_service


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    """把路由层的真实等待换成记录器。

    真实的 8 秒延时会让每个用例慢到无法接受，而我们要验证的只是「该等多久」。
    """
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(login_flow, "_sleep", fake_sleep)
    return recorded


def _user_id(username: str) -> int:
    row = UserRepository.get_by_username(username)
    assert row is not None
    return int(row["id"])


def _fail(client, times: int = 1, username: str = "alice", password: str = "wrong-pass-1") -> None:
    """用错密码登录若干次，每次都应当 401。"""
    for _ in range(times):
        assert login(client, username, password).status_code == 401


def test_no_delay_below_threshold(client, sleeps):
    """阈值以内不延时：真打错一两次密码的人不该感觉到任何变化。"""
    register(client, "alice")
    limit = get_settings().login_max_attempts

    _fail(client, limit - 1)

    assert auth_service.login_delay("alice") == 0.0
    assert sleeps == []


def test_delay_grows_geometrically_and_caps(client):
    """超过阈值后翻倍递增，封顶 8 秒。

    封顶很重要：不封顶的话，攻击者只要多失败几次就能把延时推到几分钟，
    而「久到进不去」和「锁号」对合法用户来说没有区别。
    """
    register(client, "alice")
    limit = get_settings().login_max_attempts

    _fail(client, limit)
    assert auth_service.login_delay("alice") == 2.0

    _fail(client, 1)
    assert auth_service.login_delay("alice") == 4.0

    _fail(client, 1)
    assert auth_service.login_delay("alice") == 8.0

    _fail(client, 1)
    assert auth_service.login_delay("alice") == 8.0, "应当封顶，不能无限增长"


def test_route_awaits_delay_before_verifying(client, sleeps):
    """路由层确实 await 了延时，且在校验**之前**。

    放在校验之后的话，「响应快」就等于「这次密码对了」，等于白送一个提示。
    """
    register(client, "alice")
    limit = get_settings().login_max_attempts
    _fail(client, limit)

    sleeps.clear()
    assert login(client, "alice", "wrong-password").status_code == 401
    assert sleeps == [2.0]

    # 刚才那一次失败又把计数推到 6，于是下一次的延时涨到 4 秒——而**正确密码也要先等**，
    # 这正是「延时与猜对没猜对无关」的证据。
    sleeps.clear()
    assert login(client, "alice").status_code == 200
    assert sleeps == [4.0]


def test_unknown_username_counts_too(client):
    """不存在的用户名也要计数、也要延时。

    否则「有没有延时」本身就是账号是否存在的信号，防枚举等于没做。
    """
    limit = get_settings().login_max_attempts
    _fail(client, limit, username="ghost-user")

    assert auth_service.login_delay("ghost-user") == 2.0


def test_case_variants_share_one_counter(client):
    """大小写变体共用同一个计数器。

    `users.username` 精确匹配，所以 `ALICE` 与 `alice` 是两个查询键。
    计数若按原样存，攻击者在两者间轮换就永远触发不了阈值。
    """
    register(client, "alice")
    limit = get_settings().login_max_attempts

    _fail(client, limit - 1)
    _fail(client, 1, username="ALICE")

    assert auth_service.login_delay("Alice") == 2.0


def test_successful_login_clears_failures(client):
    """登录成功即清账。

    不清的话「昨天错了 5 次」会让今天第一次登录白等 2 秒，而用户完全无从理解。
    """
    register(client, "alice")
    _fail(client, 3)

    assert login(client, "alice").status_code == 200
    assert auth_service.login_delay("alice") == 0.0
    assert LoginFailureRepository.count_since("alice", "1970-01-01T00:00:00") == 0


def test_disabled_account_with_right_password_is_not_counted(client):
    """密码正确但账号已停用，不算一次「猜错」。"""
    register(client, "alice")
    UserRepository.set_active(_user_id("alice"), False)

    assert login(client, "alice").status_code == 401
    assert auth_service.login_delay("alice") == 0.0


def test_failures_outside_window_are_ignored(client, isolated_db):  # noqa: ARG001
    """窗口外的失败不再计入——否则「上周错了一次」会永久拖慢这个账号。"""
    register(client, "alice")
    old = (utc_now() - timedelta(minutes=get_settings().login_failure_window_minutes + 5)).isoformat()
    with connect() as conn:
        conn.execute(
            "INSERT INTO login_failures (username, ip, created_at) VALUES (?, ?, ?)",
            ("alice", "", old),
        )
        conn.commit()

    assert auth_service.login_delay("alice") == 0.0


def test_zero_threshold_disables_limiting(client, monkeypatch):
    """`login_max_attempts=0` 关闭限流（给内网部署留的逃生口）。"""
    monkeypatch.setenv("LOGIN_MAX_ATTEMPTS", "0")
    get_settings.cache_clear()

    register(client, "alice")
    _fail(client, 10)

    assert auth_service.login_delay("alice") == 0.0


def test_login_purges_expired_sessions_of_other_users(client, isolated_db):  # noqa: ARG001
    """登录成功时顺手清掉过期会话。

    这条钉的是一个**早已存在的死代码**：`AuthSessionRepository.delete_expired`
    从 v1 起就定义好了，但从来没有被调用，于是 `auth_sessions` 只增不减。
    挂在登录成功这条必经且低频的路径上，不需要额外的定时任务。

    观察方式是「别人的过期会话」：自己登录时 `replace_for_user` 会删掉自己的全部会话，
    不先造一条别人的就分不清「是被清掉的」还是「本来就没有」。
    """
    register(client, "alice")
    register(client, "bob")
    bob_id = _user_id("bob")
    AuthSessionRepository.create("expired-bob-token", bob_id, utc_now() - timedelta(hours=1))
    assert len(AuthSessionRepository.list_by_user(bob_id)) == 2

    assert login(client, "alice").status_code == 200

    assert len(AuthSessionRepository.list_by_user(bob_id)) == 1, "过期会话应当被清掉"


def test_ttl_change_applies_to_new_sessions(client, monkeypatch):
    """后台改「会话有效期」后，**新签发**的会话立刻用新时长。

    这里钉的是一个具体的坑：签发器是模块级单例，若它在 `__init__` 里把 TTL
    转成 timedelta 存下来，改配置就永远不生效——配置读了、界面变了，
    签出来的会话还是旧时长。
    """
    register(client, "alice")
    monkeypatch.setenv("AUTH_SESSION_TTL_HOURS", "1")
    get_settings.cache_clear()

    assert login(client, "alice").status_code == 200

    rows = AuthSessionRepository.list_by_user(_user_id("alice"))
    assert len(rows) == 1
    remaining = datetime.fromisoformat(rows[0]["expires_at"]) - utc_now()
    assert remaining < timedelta(hours=2), f"新会话应当只用 1 小时有效期，实际剩余 {remaining}"


def test_register_can_be_closed(client, monkeypatch):
    """关闭自助注册后，注册接口 403 并说清原因。

    用 `FORBIDDEN` 而不是 `AUTH_FAILED`：前者是「这个动作不被允许」，
    后者是「凭据不对」。前端据此决定是否引导去登录。
    """
    register(client, "alice")  # 先有一个账号，验证「关注册不牵连登录」
    monkeypatch.setenv("AUTH_ALLOW_REGISTER", "false")
    get_settings.cache_clear()

    response = register(client, "bob")

    assert response.status_code == 403
    assert "不开放" in response.json()["detail"]
    # 关闭注册不影响已有账号登录
    assert login(client, "alice").status_code == 200


# ---------------------------------------------------------------------------
# 后台改配置必须立刻生效
#
# 这一段是端到端验证时发现真 bug 之后补的：所有读取点最初写的是 `get_settings()`，
# 而它**只反映环境变量**——后台改的值存在 `system_config`，要通过
# `get_runtime_config().settings` 才看得见。症状极具欺骗性：配置页显示「已关闭」、
# 保存也成功，接口却照旧放行。只测环境变量永远发现不了。
# ---------------------------------------------------------------------------


def _as_admin(client, username: str = "root") -> None:
    """建一个管理员并保持其登录态。"""
    register(client, username)
    auth_service.set_admin(username, True)
    assert client.get("/api/admin/me").status_code == 200


def _patch_config(client, updates: dict) -> None:
    response = client.patch("/api/admin/config", json={"updates": updates})
    assert response.status_code == 200, response.text


def test_register_switch_from_admin_config_takes_effect(client):
    """后台关闭注册后，注册接口立刻 403。"""
    _as_admin(client)
    _patch_config(client, {"auth_allow_register": False})

    response = register(client, "bob")

    assert response.status_code == 403, "配置存进去了却没生效——读取点漏了热配置"


def test_threshold_from_admin_config_takes_effect(client):
    """把阈值改成 1 之后，一次失败就该开始延时。"""
    _as_admin(client)
    _patch_config(client, {"login_max_attempts": 1})
    register(client, "alice")

    _fail(client, 1)

    assert auth_service.login_delay("alice") == 2.0


def test_ttl_from_admin_config_takes_effect(client):
    """后台改会话有效期后，新签发的会话用新时长。"""
    _as_admin(client)
    _patch_config(client, {"auth_session_ttl_hours": 1})

    assert login(client, "root").status_code == 200

    rows = AuthSessionRepository.list_by_user(_user_id("root"))
    assert len(rows) == 1
    remaining = datetime.fromisoformat(rows[0]["expires_at"]) - utc_now()
    assert remaining < timedelta(hours=2), f"应当只剩 1 小时，实际 {remaining}"


# ---------------------------------------------------------------------------
# 后台改配置必须立刻生效
#
# 这一段是端到端验证时发现真 bug 之后补的：所有读取点最初写的是 `get_settings()`，
# 而它**只反映环境变量**——后台改的值存在 `system_config`，要通过
# `get_runtime_config().settings` 才看得见。症状极具欺骗性：配置页显示「已关闭」、
# 保存也成功，接口却照旧放行。只测环境变量永远发现不了。
# ---------------------------------------------------------------------------


def _as_admin(client, username: str = "root") -> None:
    """建一个管理员并保持其登录态。"""
    register(client, username)
    auth_service.set_admin(username, True)
    assert client.get("/api/admin/me").status_code == 200


def _patch_config(client, updates: dict) -> None:
    response = client.patch("/api/admin/config", json={"updates": updates})
    assert response.status_code == 200, response.text


def test_register_switch_from_admin_config_takes_effect(client):
    """后台关闭注册后，注册接口立刻 403。"""
    _as_admin(client)
    _patch_config(client, {"auth_allow_register": False})

    response = register(client, "bob")

    assert response.status_code == 403, "配置存进去了却没生效——读取点漏了热配置"


def test_threshold_from_admin_config_takes_effect(client):
    """把阈值改成 1 之后，一次失败就该开始延时。"""
    _as_admin(client)
    _patch_config(client, {"login_max_attempts": 1})
    register(client, "alice")

    _fail(client, 1)

    assert auth_service.login_delay("alice") == 2.0


def test_ttl_from_admin_config_takes_effect(client):
    """后台改会话有效期后，新签发的会话用新时长。"""
    _as_admin(client)
    _patch_config(client, {"auth_session_ttl_hours": 1})

    assert login(client, "root").status_code == 200

    rows = AuthSessionRepository.list_by_user(_user_id("root"))
    assert len(rows) == 1
    remaining = datetime.fromisoformat(rows[0]["expires_at"]) - utc_now()
    assert remaining < timedelta(hours=2), f"应当只剩 1 小时，实际 {remaining}"
