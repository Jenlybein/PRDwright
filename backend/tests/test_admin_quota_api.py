"""管理员设置用户额度（v4.5 T5-2）。

三条重点：

1. **密钥式的三态不能塌成两态**：`-1`（跟随全局）/ `0`（不限）/ 正数（具体上限）
   必须都能显式表达。少了 `-1`，「恢复成跟大家一样」就做不到；少了 `0`，
   「给这个账号开口子」就做不到。
2. **非管理员必须进不来**：`require_admin` 是逐条挂的，漏挂等于端点裸奔。
3. **审计要记「改前 → 改后」**，否则事后追责时只知道「有人改过额度」，
   不知道改成了多少。
"""

from __future__ import annotations

from conftest import login, register, users

from backend.core.engine import session_scope
from backend.services import admin_service, auth_service
from backend.repositories.quota_repo import QuotaRepository



def _quota_columns(user_id: int) -> dict[str, int]:
    """读三列覆盖值（仓库不再自己开连接，所以这里显式开一个事务）。"""
    with session_scope() as session:
        return QuotaRepository(session).quota_columns(user_id)

def _user_id(username: str) -> int:
    row = users.get_by_username(username)
    assert row is not None
    return int(row["id"])


def _as_admin(client, username: str = "root") -> None:
    register(client, username)
    auth_service.set_admin(username, True)


def test_admin_can_set_quota(client, isolated_db):  # noqa: ARG001
    """设置成功，返回的是**生效值 + 来源**，而不是原始覆盖值。

    前端要靠来源决定编辑框回填 -1 / 0 / 具体值——只给生效数字的话，
    「继承全局的 10」和「单独设的 10」在界面上完全一样，
    管理员点一次保存就把继承关系锁死了。
    """
    _as_admin(client)
    register(client, "alice")
    uid = _user_id("alice")
    login(client, "root")  # register 会把登录态换成 alice，必须切回来

    response = client.patch(
        f"/api/admin/users/{uid}/quota",
        json={"daily_task_limit": 3, "daily_token_limit": 0, "max_running": 1},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["daily_task_limit"] == 3
    assert body["daily_task_source"] == "user"
    # 显式 0 = 不限，来源要能区分出来（不是「继承全局得到 0」）
    assert body["daily_token_limit"] == 0
    assert body["daily_token_source"] == "off"


def test_minus_one_restores_inheritance(client, isolated_db):  # noqa: ARG001
    """把用户改回 `-1` 之后，来源变回 `global`。"""
    _as_admin(client)
    register(client, "bob")
    uid = _user_id("bob")
    login(client, "root")

    client.patch(
        f"/api/admin/users/{uid}/quota",
        json={"daily_task_limit": 5, "daily_token_limit": 5, "max_running": 5},
    )
    response = client.patch(
        f"/api/admin/users/{uid}/quota",
        json={"daily_task_limit": -1, "daily_token_limit": -1, "max_running": -1},
    )
    assert response.status_code == 200
    assert response.json()["daily_task_source"] == "global"
    # 落库的也是 -1，而不是「把当前生效值写死」
    assert _quota_columns(uid)["daily_task_limit"] == -1


def test_out_of_range_is_rejected(client, isolated_db):  # noqa: ARG001
    """小于 -1 的取值被挡在 schema 层（422），不会写进库。"""
    _as_admin(client)
    register(client, "carol")
    uid = _user_id("carol")
    login(client, "root")

    response = client.patch(
        f"/api/admin/users/{uid}/quota",
        json={"daily_task_limit": -2, "daily_token_limit": 0, "max_running": 0},
    )
    assert response.status_code == 422
    # 没被写坏
    assert _quota_columns(uid)["daily_task_limit"] == -1


def test_unknown_user_is_404(client, isolated_db):  # noqa: ARG001
    """对不存在的用户设额度返回 404，而不是静默成功。

    静默成功会让管理员以为设置生效了，而实际上一个人也没被限制。
    """
    _as_admin(client)
    response = client.patch(
        "/api/admin/users/999999/quota",
        json={"daily_task_limit": 1, "daily_token_limit": 0, "max_running": 0},
    )
    assert response.status_code == 404


def test_non_admin_is_forbidden(client, isolated_db):  # noqa: ARG001
    """普通用户进不来——额度是管理面操作。"""
    _as_admin(client)
    register(client, "dave")  # 注册即登录，且不是管理员
    uid = _user_id("dave")

    response = client.patch(
        f"/api/admin/users/{uid}/quota",
        json={"daily_task_limit": 1, "daily_token_limit": 0, "max_running": 0},
    )
    assert response.status_code == 403


def test_user_list_carries_quota_and_today_usage(client, isolated_db):  # noqa: ARG001
    """用户列表要带额度与今日用量——那是「谁快被卡住了」的唯一入口。"""
    _as_admin(client)
    response = client.get("/api/admin/users")
    assert response.status_code == 200
    row = response.json()["users"][0]
    for key in (
        "daily_task_limit",
        "daily_token_limit",
        "max_running",
        "daily_task_source",
        "usage_tasks_today",
        "usage_tokens_today",
    ):
        assert key in row, f"用户列表缺字段 {key}"


def test_audit_records_before_and_after(client, isolated_db):  # noqa: ARG001
    """审计要写清「改前 → 改后」，否则只知道有人改过、不知道改成了多少。"""
    _as_admin(client)
    register(client, "erin")
    uid = _user_id("erin")
    login(client, "root")

    client.patch(
        f"/api/admin/users/{uid}/quota",
        json={"daily_task_limit": 4, "daily_token_limit": -1, "max_running": -1},
    )

    from conftest import audit_log as AuditRepository
    rows = AuditRepository.list_recent(limit=20)
    hits = [row for row in rows if row.get("action") == "user.quota"]
    assert hits, "没有记下 user.quota 审计"
    detail = str(hits[0].get("detail") or "")
    assert "daily_task_limit" in detail
    # 只记有变化的项：另外两项仍是 -1，不该出现在 detail 里
    assert "daily_token_limit" not in detail
