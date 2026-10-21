"""管理员管账号与管邀请码（v4 T1）。

这里刻意**每条写操作都配一条「不允许」的用例**。只测「能做成」是不够的：
「管理员能删号」与「管理员能把自己删掉」用的是同一段代码路径，差别只在一条
前置判断。没有反向用例的话，那条判断被删掉也照样全绿。

同理，每个新端点都配一条「非管理员访问」的用例——`require_admin` 是**逐条**挂的，
漏挂一个端点的代价是那个端点完全裸奔，而它看起来和别的一模一样。
"""

from __future__ import annotations

import pytest
from conftest import register

from backend.core.db import connect
from backend.core.errors import AppError, ErrorCode
from backend.core.migrations import MIGRATIONS, run_migrations
from backend.repositories.generation_job_repo import GenerationJobRepository
from backend.services import admin_service, auth_service

# 与 conftest.register 的默认密码保持一致，避免被强度校验绊倒而掩盖真正的断言
_PASSWORD = "password123"

# 邀请码刻意避开 O/0、I/1 等被字符集排除的字符（见 `admin_service._INVITE_ALLOWED`）
_CODE_A = "ALPHA2"
_CODE_B = "BETA3"


def _user_id(username: str) -> int:
    """按用户名取 id。"""
    row = auth_service.UserRepository.get_by_username(username)
    assert row is not None
    return int(row["id"])


def _as_admin(client, username: str = "root") -> None:
    """注册并任命一个管理员（顺带让 client 处于该账号的登录态）。"""
    register(client, username)
    auth_service.set_admin(username, True)


# ---------------------------------------------------------------------------
# 迁移
# ---------------------------------------------------------------------------


def test_migrations_reach_latest_version():
    """跑完迁移必须到清单末尾。

    断言写成 `len(MIGRATIONS)` 而不是写死数字：写死的话将来每加一步迁移，
    这条与本次改动毫无关系的用例都会红一次。
    """
    assert run_migrations() == len(MIGRATIONS)


def test_migration_adds_token_columns_and_audit_table():
    """v14/v15：Token 列与审计表都要真的存在。

    Token 列若缺失，后台统计要么报错、要么（更糟）悄悄退化成解析 JSON，
    而解析失败时可能返回 0——表现为「平台用量为零」而不是报错，最难发现。
    """
    run_migrations()

    with connect() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(generation_jobs)")}
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    assert {"prompt_tokens", "completion_tokens"} <= columns
    assert "admin_audit_log" in tables


# ---------------------------------------------------------------------------
# 守卫：每个端点都要挂住
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/api/admin/users", "/api/admin/invites", "/api/admin/stats", "/api/admin/audit"],
)
def test_read_endpoints_reject_normal_user(client, path):
    """非管理员读任何管理员端点都得 403。

    参数化而不是逐个写：这张表本身就是一份「该挂守卫的端点清单」，
    新增端点时漏登记，就等于那个端点没人守。
    """
    register(client, "alice")

    response = client.get(path)

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_write_endpoints_reject_normal_user(client):
    """写端点同样要挂守卫，而且**请求体要合法**。

    传空 body 会被 Pydantic 先拦成 422，那样测的是字段校验、不是权限守卫——
    真正的越权者会带上一个完全合法的请求体。
    """
    register(client, "alice")

    assert client.post(
        "/api/admin/users", json={"username": "bob", "password": _PASSWORD}
    ).status_code == 403
    assert client.post("/api/admin/invites", json={"code": _CODE_A}).status_code == 403
    assert client.patch(
        "/api/admin/users/1/active", json={"is_active": False}
    ).status_code == 403
    assert client.post(
        "/api/admin/users/1/password", json={"password": _PASSWORD}
    ).status_code == 403
    assert client.delete("/api/admin/users/1").status_code == 403
    assert client.delete("/api/admin/invites/" + _CODE_A).status_code == 403


def test_admin_endpoints_require_authentication(client):
    """匿名访问由中间件拦成 401——管理员端点不在白名单里。"""
    assert client.get("/api/admin/users").status_code == 401


# ---------------------------------------------------------------------------
# 用户列表与统计
# ---------------------------------------------------------------------------


def test_user_list_includes_usage_aggregated_by_sql(client):
    """列表里的任务数与 Token 必须来自 SQL 聚合，且**失败任务也算进去**。

    失败任务不算的话，「这个用户烧了多少」会系统性偏低——而重试恰恰是最烧钱的部分。
    """
    _as_admin(client, "alice")
    owner_id = _user_id("alice")

    job_a = GenerationJobRepository.create(owner_id, "prd", None)
    GenerationJobRepository.finish(
        owner_id,
        job_a,
        draft_content="",
        completed_sections=[],
        prompt_tokens=100,
        completion_tokens=20,
    )
    job_b = GenerationJobRepository.create(owner_id, "prd", None)
    GenerationJobRepository.fail(
        owner_id,
        job_b,
        error_code="INTERNAL_ERROR",
        error_message="炸了",
        draft_content="",
        completed_sections=[],
        prompt_tokens=50,
        completion_tokens=5,
    )

    rows = client.get("/api/admin/users").json()["users"]

    alice = next(row for row in rows if row["username"] == "alice")
    assert alice["task_count"] == 2
    assert alice["prompt_tokens"] == 150
    assert alice["completion_tokens"] == 25


def test_user_list_includes_users_without_any_job(client):
    """一个任务都没有的用户也必须出现在列表里。

    用 `INNER JOIN` 就会漏掉他们，表现为「刚注册的账号在后台凭空消失」——
    而那正是管理员最需要看见的一批人。
    """
    _as_admin(client, "alice")
    auth_service.create_user_direct("newcomer", _PASSWORD)

    usernames = {row["username"] for row in client.get("/api/admin/users").json()["users"]}

    assert "newcomer" in usernames


def test_platform_stats_counts_everyone(client):
    """平台总量是**独立一条** SQL，不是把用户列表加总。

    列表以后可能分页或裁剪，拿它算总量会悄悄算少——这条用例就是那个陷阱的护栏。
    """
    _as_admin(client, "alice")
    owner_id = _user_id("alice")
    job_id = GenerationJobRepository.create(owner_id, "prd", None)
    GenerationJobRepository.finish(
        owner_id,
        job_id,
        draft_content="",
        completed_sections=[],
        prompt_tokens=7,
        completion_tokens=3,
    )

    stats = client.get("/api/admin/stats").json()

    assert stats["user_count"] == 1
    assert stats["admin_count"] == 1
    assert stats["task_count"] == 1
    assert stats["completed_count"] == 1
    assert stats["prompt_tokens"] == 7


# ---------------------------------------------------------------------------
# 建号
# ---------------------------------------------------------------------------


def test_create_user_as_admin(client):
    _as_admin(client)

    response = client.post(
        "/api/admin/users", json={"username": "bob", "password": _PASSWORD, "is_admin": True}
    )

    assert response.status_code == 201
    assert response.json()["is_admin"] is True
    assert auth_service.login("bob", _PASSWORD)[0].is_admin is True


def test_create_user_rejects_weak_password():
    """后台建号不能绕过密码强度——否则后台就成了弱密码后门。

    在服务层断言而不是走 HTTP：请求体的 `min_length` 会在路由前把弱密码拦成 422，
    那条路径测的是 Pydantic、不是服务层的规则。真正要守的是「即使绕开 schema，
    服务层依然不放宽」。
    """
    root = auth_service.create_user_direct("root", _PASSWORD)
    auth_service.set_admin("root", True)

    with pytest.raises(auth_service.AuthError, match="密码"):
        admin_service.create_user(root, "bob", "123")


def test_create_user_rejects_duplicate(client):
    _as_admin(client)
    auth_service.create_user_direct("bob", _PASSWORD)

    response = client.post("/api/admin/users", json={"username": "bob", "password": _PASSWORD})

    assert response.status_code == 400


# ---------------------------------------------------------------------------
# 启停
# ---------------------------------------------------------------------------


def test_deactivated_user_cannot_login(client):
    _as_admin(client)
    auth_service.create_user_direct("bob", _PASSWORD)
    bob_id = _user_id("bob")

    assert client.patch(
        f"/api/admin/users/{bob_id}/active", json={"is_active": False}
    ).status_code == 200

    with pytest.raises(auth_service.AuthError):
        auth_service.login("bob", _PASSWORD)


def test_deactivate_is_reversible(client):
    """停用不等于删号：数据还在，重新启用后照常登录。"""
    _as_admin(client)
    auth_service.create_user_direct("bob", _PASSWORD)
    bob_id = _user_id("bob")

    client.patch(f"/api/admin/users/{bob_id}/active", json={"is_active": False})
    client.patch(f"/api/admin/users/{bob_id}/active", json={"is_active": True})

    assert auth_service.login("bob", _PASSWORD)[0].is_active is True


def test_admin_cannot_deactivate_self(client):
    """管理员停用自己 = 当场把自己锁在门外，且无法自助恢复。

    这条守的是「后台不能变得无人能进」。只要操作者是管理员，唯一的管理员
    就只能是自己——所以这是该规则**唯一可达**的表现形式。
    """
    _as_admin(client)
    root_id = _user_id("root")

    response = client.patch(f"/api/admin/users/{root_id}/active", json={"is_active": False})

    assert response.status_code == 403
    assert "自己" in response.json()["detail"]
    assert auth_service.login("root", _PASSWORD)[0].is_active is True


def test_second_admin_can_be_deactivated(client):
    """有另一名管理员在场时，停用其中一个是允许的。

    不变式是「至少留一位」，不是「只能有一位」——别把护栏做成枷锁。
    """
    _as_admin(client)
    auth_service.create_user_direct("deputy", _PASSWORD)
    auth_service.set_admin("deputy", True)
    deputy_id = _user_id("deputy")

    assert client.patch(
        f"/api/admin/users/{deputy_id}/active", json={"is_active": False}
    ).status_code == 200

    # 停用**不等于**撤销管理员：他仍是管理员，只是登不进来了。
    # 两者必须分开，否则后台想「暂时禁止登录」就只能先撤权限，恢复时容易漏。
    with pytest.raises(auth_service.AuthError):
        auth_service.login("deputy", _PASSWORD)
    assert auth_service.UserRepository.count_admins() == 2


# ---------------------------------------------------------------------------
# 重置密码
# ---------------------------------------------------------------------------


def test_reset_password_takes_effect(client):
    _as_admin(client)
    auth_service.create_user_direct("bob", _PASSWORD)
    bob_id = _user_id("bob")

    response = client.post(f"/api/admin/users/{bob_id}/password", json={"password": "new-password"})

    assert response.status_code == 200
    assert auth_service.login("bob", "new-password")[0].username == "bob"


def test_reset_password_never_written_to_audit(client):
    """审计里只能看到「改过了」，不能看到改成了什么。

    审计表是给人翻的，不是保险箱；把密码写进去等于给下一个读到它的人一份明文口令。
    """
    _as_admin(client)
    auth_service.create_user_direct("bob", _PASSWORD)
    bob_id = _user_id("bob")

    client.post(f"/api/admin/users/{bob_id}/password", json={"password": "brand-new-secret"})

    assert "brand-new-secret" not in str(admin_service.list_audit())


# ---------------------------------------------------------------------------
# 删号
# ---------------------------------------------------------------------------


def test_delete_user_invalidates_its_sessions(client):
    """删号后该用户的会话立即失效——靠外键级联，等于强制下线。"""
    _as_admin(client)
    auth_service.create_user_direct("bob", _PASSWORD)
    _, credential, _ = auth_service.login("bob", _PASSWORD)
    bob_id = _user_id("bob")

    assert client.delete(f"/api/admin/users/{bob_id}").status_code == 200

    assert auth_service.current_user(credential) is None


def test_admin_cannot_delete_self(client):
    _as_admin(client)
    root_id = _user_id("root")

    assert client.delete(f"/api/admin/users/{root_id}").status_code == 403


def test_last_admin_cannot_be_deleted(client):
    """只有一位管理员时，他就是「最后一位」——删掉他就没人能进后台了。"""
    _as_admin(client)
    root_id = _user_id("root")

    assert client.delete(f"/api/admin/users/{root_id}").status_code == 403


def test_delete_unknown_user_returns_404(client):
    _as_admin(client)

    assert client.delete("/api/admin/users/99999").status_code == 404


# ---------------------------------------------------------------------------
# 邀请码
# ---------------------------------------------------------------------------


def test_invite_lifecycle(client):
    """建 → 列 → 删。表本就支持多码，这里验证的是接口确实串得起来。"""
    _as_admin(client)

    created = client.post(
        "/api/admin/invites", json={"code": _CODE_A, "max_uses": 5, "note": "内测"}
    )
    assert created.status_code == 201

    invites = client.get("/api/admin/invites").json()["invites"]
    alpha = next(item for item in invites if item["code"] == _CODE_A)
    assert alpha["max_uses"] == 5
    assert alpha["used_count"] == 0
    assert alpha["expires_at"] is None

    assert client.delete(f"/api/admin/invites/{_CODE_A}").status_code == 200
    # 断言「这个码没了」而不是「列表空了」：测试配置里还播种着一个默认邀请码，
    # 写死空列表会把那条与本用例无关的数据也算进来。
    assert _CODE_A not in {i["code"] for i in client.get("/api/admin/invites").json()["invites"]}


def test_create_invite_rejects_duplicate(client):
    _as_admin(client)
    client.post("/api/admin/invites", json={"code": _CODE_A})

    response = client.post("/api/admin/invites", json={"code": _CODE_A})

    assert response.status_code == 400


def test_create_invite_rejects_ambiguous_characters(client):
    """含 `0/O`、`1/I` 这类易混字符的码一律拒收。

    码是要被人手抄、贴进聊天窗口的，允许易混字符会把注册失败变成
    「码明明是对的」的扯皮，而这种扯皮在后台没有任何日志可查。
    """
    _as_admin(client)

    response = client.post("/api/admin/invites", json={"code": "WELC0ME"})

    assert response.status_code == 400


def test_create_invite_accepts_expiry(client):
    """过期时间是可选的，填了就原样落库。"""
    _as_admin(client)

    response = client.post(
        "/api/admin/invites", json={"code": _CODE_B, "expires_at": "2026-12-31T00:00:00"}
    )

    assert response.status_code == 201
    item = next(
        i for i in client.get("/api/admin/invites").json()["invites"] if i["code"] == _CODE_B
    )
    assert item["expires_at"].startswith("2026-12-31")


def test_delete_unknown_invite_returns_404(client):
    _as_admin(client)

    assert client.delete("/api/admin/invites/NOSUCH").status_code == 404


def test_deleting_invite_does_not_remove_registered_users(client):
    """删码只影响后续注册，已经注册的用户不受牵连。

    他们**靠码进来**，但进来之后就与码无关了。若删码连带删人，
    后台一次清理邀请码就会误伤一批真实用户。

    这里用服务层注册而不是 `register(client, ...)`：后者会把 client 的会话
    换成 bob，后续删码就变成普通用户在操作，测的就不是这回事了。
    """
    _as_admin(client)
    client.post("/api/admin/invites", json={"code": _CODE_A})
    auth_service.register("bob", _PASSWORD, _CODE_A)

    client.delete(f"/api/admin/invites/{_CODE_A}")

    assert auth_service.login("bob", _PASSWORD)[0].username == "bob"


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------


def test_every_write_operation_is_audited(client):
    """每一次写操作都要留痕，且记的是**谁**干的。

    这里同时验证「操作者取自会话」而不是请求体：请求里没有 actor 字段，
    审计里却必须有 root——说明身份来自服务端。
    """
    _as_admin(client)
    client.post("/api/admin/invites", json={"code": _CODE_A})
    client.post("/api/admin/users", json={"username": "bob", "password": _PASSWORD})

    items = client.get("/api/admin/audit").json()["items"]

    actions = {item["action"] for item in items}
    assert {"invite.create", "user.create"} <= actions
    assert all(item["actor_name"] == "root" for item in items)


def test_audit_is_newest_first(client):
    _as_admin(client)
    client.post("/api/admin/invites", json={"code": _CODE_A})
    client.post("/api/admin/invites", json={"code": _CODE_B})

    items = client.get("/api/admin/audit").json()["items"]

    assert items[0]["target"] == _CODE_B


def test_audit_survives_actor_deletion():
    """操作者账号被删之后，他做过的操作仍要看得见。

    这正是审计表**不加外键**的理由：加了外键，这些记录会随账号一起消失，
    而「谁删的」往往正是最需要追的人。
    """
    actor = auth_service.create_user_direct("root", _PASSWORD)
    auth_service.set_admin("root", True)
    admin_service.create_invite(actor, "KEEP2")
    auth_service.delete_user("root")

    items = admin_service.list_audit()

    assert any(item["target"] == "KEEP2" and item["actor_name"] == "root" for item in items)


# ---------------------------------------------------------------------------
# 服务层规则的直接断言（不走 HTTP）
# ---------------------------------------------------------------------------


def test_service_layer_enforces_rules_without_http():
    """规则必须在服务层：绕过 HTTP 直接调服务，一样要被挡。

    若规则只写在路由里，将来任何一条内部调用路径（脚本、后台任务）都能绕过去。
    """
    root = auth_service.create_user_direct("root", _PASSWORD)
    auth_service.set_admin("root", True)
    bob = auth_service.create_user_direct("bob", _PASSWORD)

    # 不能删自己
    with pytest.raises(AppError) as exc:
        admin_service.delete_user(root, root.id)
    assert exc.value.code == ErrorCode.FORBIDDEN

    # 正路仍然通
    admin_service.delete_user(root, bob.id)
    assert auth_service.UserRepository.get_by_username("bob") is None


# ---------------------------------------------------------------------------
# 授予 / 撤销管理员
# ---------------------------------------------------------------------------


def test_admin_can_promote_another_user(client):
    """后台能把普通账号提为管理员——不必绕到运维脚本。"""
    _as_admin(client, "alice")
    auth_service.create_user_direct("bob", _PASSWORD)
    bob_id = _user_id("bob")

    response = client.patch(f"/api/admin/users/{bob_id}/admin", json={"is_admin": True})

    assert response.status_code == 200
    assert response.json()["is_admin"] is True
    assert auth_service.UserRepository.get_by_username("bob")["is_admin"] == 1


def test_admin_can_demote_another_admin(client):
    """有第二位管理员时，撤销第一位是允许的。"""
    _as_admin(client, "alice")
    auth_service.create_user_direct("bob", _PASSWORD)
    bob_id = _user_id("bob")
    alice_id = _user_id("alice")
    client.patch(f"/api/admin/users/{bob_id}/admin", json={"is_admin": True})

    response = client.patch(f"/api/admin/users/{alice_id}/admin", json={"is_admin": False})

    assert response.status_code == 200
    assert response.json()["is_admin"] is False


def test_cannot_demote_last_admin(client):
    """最后一位管理员不能被撤销——否则无人能进后台，且无法自助恢复。

    这条规则必须与运维脚本（`auth_cli.py demote`）**共用同一份实现**：
    两条路各自写一遍，迟早有一处漏改，后台就成了绕过不变式的后门。
    """
    _as_admin(client, "alice")
    alice_id = _user_id("alice")

    response = client.patch(f"/api/admin/users/{alice_id}/admin", json={"is_admin": False})

    assert response.status_code == 403
    assert "最后一位管理员" in response.json()["detail"]
    assert auth_service.UserRepository.get_by_username("alice")["is_admin"] == 1


def test_can_demote_self_when_another_admin_remains(client):
    """只要还有别的管理员，撤销自己的管理员身份是允许的——这是正常的「卸任」。

    刻意**不**把它当成违规：管理员交接、临时让渡权限都是真实场景。
    而且它是可逆的——另一位管理员随时能重新任命。

    真正要拦的只有一种：撤销之后系统里一个管理员都不剩。
    """
    _as_admin(client, "alice")
    auth_service.create_user_direct("bob", _PASSWORD)
    bob_id = _user_id("bob")
    client.patch(f"/api/admin/users/{bob_id}/admin", json={"is_admin": True})
    alice_id = _user_id("alice")

    response = client.patch(f"/api/admin/users/{alice_id}/admin", json={"is_admin": False})

    assert response.status_code == 200
    assert response.json()["is_admin"] is False
    # bob 接任，系统里仍有管理员
    assert auth_service.UserRepository.count_admins() == 1


def test_promote_endpoint_requires_admin(client):
    """提名管理员这个接口本身也要受守卫——否则普通用户能给自己提权。"""
    register(client, "bob")

    response = client.patch(f"/api/admin/users/{_user_id('bob')}/admin", json={"is_admin": True})

    assert response.status_code == 403
    assert auth_service.UserRepository.get_by_username("bob")["is_admin"] == 0
