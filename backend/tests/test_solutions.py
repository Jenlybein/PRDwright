"""方案持久化的测试。

这里最要紧的一组是**越权**：数据隔离是安全边界，不能靠「路由层记得检查」，
因此不仅要测正常流程，还要逐个验证「别人的方案在我这里等同于不存在」。
"""

from __future__ import annotations

import json

import pytest

from conftest import login, register

# 一份最小可用的会话快照，字段与前端 `SessionDataV2` 对齐
SNAPSHOT = {
    "viewState": "review-prd",
    "entryMode": "structured",
    "summary": {"product_name": "团队周报助手", "product_goal": "让周报自动生成"},
    "messages": [{"role": "user", "content": "帮我写个周报工具"}],
    "conversationId": "conv-1",
    "quickInput": {"productName": "", "requirements": ""},
    "debugPrdText": "",
    "prdContent": "# 团队周报助手\n\n正文……",
    "apiDocsContent": "",
    "promptsContent": "",
}


def _payload(
    title: str = "团队周报助手",
    mode: str = "structured",
    stage: str = "review-prd",
    data: dict | None = None,
) -> dict:
    """构造保存请求体。`data` 走 json.dumps，模拟前端把快照序列化后提交。"""
    return {
        "title": title,
        "mode": mode,
        "stage": stage,
        "data": json.dumps(data if data is not None else SNAPSHOT, ensure_ascii=False),
    }


def _create(client, **kwargs) -> dict:
    """建一份方案并返回其摘要，顺带断言状态码，避免后续断言被 4xx 掩盖。"""
    response = client.post("/api/solutions", json=_payload(**kwargs))
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# 基本流程
# ---------------------------------------------------------------------------


def test_create_and_read_round_trip(client):
    """保存后读回，快照内容必须逐字一致（含中文）。"""
    register(client)
    created = _create(client)

    assert created["title"] == "团队周报助手"
    assert isinstance(created["id"], int) and created["id"] > 0

    detail = client.get(f"/api/solutions/{created['id']}")
    assert detail.status_code == 200
    body = detail.json()
    assert json.loads(body["data"]) == SNAPSHOT
    # 中文没有被转义成 \uXXXX
    assert "团队周报助手" in body["data"]
    assert body["created_at"] == body["updated_at"]


def test_update_overwrites_whole_snapshot(client):
    """更新是整份覆盖：旧字段不会残留，updated_at 会前进。"""
    register(client)
    created = _create(client)

    changed = dict(SNAPSHOT)
    changed["prdContent"] = "# 改过的标题"
    changed["messages"] = []
    response = client.put(
        f"/api/solutions/{created['id']}",
        json=_payload(title="改名后", stage="done", data=changed),
    )
    assert response.status_code == 200

    body = client.get(f"/api/solutions/{created['id']}").json()
    restored = json.loads(body["data"])
    assert restored["prdContent"] == "# 改过的标题"
    # 整份覆盖的语义：上一版的消息不会因为这次没传就保留下来
    assert restored["messages"] == []
    assert body["title"] == "改名后"
    assert body["stage"] == "done"
    assert body["updated_at"] >= body["created_at"]


def test_list_returns_summaries_without_data(client):
    """列表只给摘要——响应里不该出现 data 字段，避免把大 JSON 拖出来。"""
    register(client)
    _create(client, title="方案甲")
    _create(client, title="方案乙")

    response = client.get("/api/solutions")
    assert response.status_code == 200
    items = response.json()["solutions"]
    assert [item["title"] for item in items] == ["方案乙", "方案甲"]  # 最近更新在前
    for item in items:
        assert "data" not in item, "列表接口不应返回快照正文"


def test_list_is_empty_for_new_user(client):
    """新用户没有任何方案时返回空列表，而不是 404。"""
    register(client)
    assert client.get("/api/solutions").json()["solutions"] == []


def test_delete_then_gone(client):
    """删除后列表里没有，再读返回 404。"""
    register(client)
    created = _create(client)

    assert client.delete(f"/api/solutions/{created['id']}").json()["ok"] is True
    assert client.get("/api/solutions").json()["solutions"] == []
    assert client.get(f"/api/solutions/{created['id']}").status_code == 404


def test_title_is_trimmed_and_truncated(client):
    """标题是展示用的冗余列：去掉首尾空白并截断，防止撑坏侧边栏。"""
    register(client)
    created = _create(client, title="  空格包围的标题  ")
    assert created["title"] == "空格包围的标题"

    long_title = "长" * 500
    created = _create(client, title=long_title)
    assert len(created["title"]) == 200


def test_existing_solution_id_is_scoped_to_owner(client):
    """两个用户各自的方案 id 互不干扰，列表只含自己的。"""
    register(client, username="alice")
    alice = _create(client, title="alice 的方案")

    client.post("/api/auth/logout")
    register(client, username="bob")
    bob = _create(client, title="bob 的方案")

    assert alice["id"] != bob["id"]
    titles = [item["title"] for item in client.get("/api/solutions").json()["solutions"]]
    assert titles == ["bob 的方案"]


# ---------------------------------------------------------------------------
# 越权：别人的方案等同于不存在
# ---------------------------------------------------------------------------


@pytest.fixture
def two_users(client):
    """准备两个登录态互相切换的用户，alice 名下有方案，bob 名下没有。

    返回 (alice_solution_id, login_as_alice, login_as_bob)：登录态是 Cookie，
    切换用户必须重新登录，因此这里把两个动作都包成可调用的函数。
    """
    register(client, username="alice", invite_code="test-invite")
    solution = _create(client, title="alice 的机密方案")
    client.post("/api/auth/logout")

    register(client, username="bob", invite_code="test-invite")
    client.post("/api/auth/logout")

    def as_alice():
        assert login(client, username="alice").status_code == 200

    def as_bob():
        assert login(client, username="bob").status_code == 200

    return solution["id"], as_alice, as_bob


def test_other_users_solution_is_not_readable(client, two_users):
    """越权读取返回 404 而非 403——403 会泄露「这个 id 确实存在」。"""
    solution_id, _, as_bob = two_users
    as_bob()

    response = client.get(f"/api/solutions/{solution_id}")
    assert response.status_code == 404
    assert response.json()["detail"] == "方案不存在"


def test_other_users_solution_is_not_updatable(client, two_users):
    """越权更新返回 404，且**原方案内容不受影响**。"""
    solution_id, as_alice, as_bob = two_users
    as_bob()
    response = client.put(
        f"/api/solutions/{solution_id}",
        json=_payload(title="被篡改", data={"hacked": True}),
    )
    assert response.status_code == 404

    as_alice()
    body = client.get(f"/api/solutions/{solution_id}").json()
    assert body["title"] == "alice 的机密方案"
    assert json.loads(body["data"]) == SNAPSHOT


def test_other_users_solution_is_not_deletable(client, two_users):
    """越权删除返回 404，且方案仍在。"""
    solution_id, as_alice, as_bob = two_users
    as_bob()

    assert client.delete(f"/api/solutions/{solution_id}").status_code == 404

    as_alice()
    assert client.get(f"/api/solutions/{solution_id}").status_code == 200


def test_other_users_solution_absent_from_list(client, two_users):
    """越权不仅拦直连，列表里也不该出现别人的方案。"""
    _, _, as_bob = two_users
    as_bob()
    assert client.get("/api/solutions").json()["solutions"] == []


def test_nonexistent_solution_returns_404(client):
    """不存在的 id 与越权返回同样的 404，两者不可区分。"""
    register(client)
    assert client.get("/api/solutions/999999").status_code == 404
    assert client.put("/api/solutions/999999", json=_payload()).status_code == 404
    assert client.delete("/api/solutions/999999").status_code == 404


# ---------------------------------------------------------------------------
# 鉴权与参数校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("get", "/api/solutions", None),
        ("post", "/api/solutions", {"title": "x", "data": "{}"}),
        ("get", "/api/solutions/1", None),
        ("put", "/api/solutions/1", {"title": "x", "data": "{}"}),
        ("delete", "/api/solutions/1", None),
    ],
)
def test_all_solution_endpoints_require_login(client, method, path, body):
    """五个接口都必须在未登录时拒绝——它们不在白名单里，由中间件统一拦。"""
    call = getattr(client, method)
    response = call(path, json=body) if body is not None else call(path)
    assert response.status_code == 401
    assert response.json()["detail"] == "请先登录后再使用该功能"


def test_empty_data_is_rejected(client):
    """`data` 不能为空：一份没有任何内容的快照没有保存的意义。"""
    register(client)
    response = client.post(
        "/api/solutions",
        json={"title": "空方案", "mode": "", "stage": "", "data": ""},
    )
    assert response.status_code == 422


def test_dev_mode_falls_back_to_dev_user(auth_off_client):
    """开发模式（AUTH_ENABLED=false）下不登录也能用，归属落到内置 dev 用户。"""
    created = auth_off_client.post("/api/solutions", json=_payload(title="开发模式方案"))
    assert created.status_code == 201

    listed = auth_off_client.get("/api/solutions").json()["solutions"]
    assert [item["title"] for item in listed] == ["开发模式方案"]


def test_deleting_user_removes_their_solutions(client):
    """删号时其方案随外键级联清掉，不留孤儿数据。

    直接走服务层验证——没有「删号」的公开接口，那是运维 CLI 的能力。
    """
    from backend.repositories.solution_repo import SolutionRepository
    from backend.services import auth_service

    register(client)
    created = _create(client)
    user = auth_service.UserRepository.get_by_username("alice")
    assert user is not None

    auth_service.delete_user("alice")
    assert SolutionRepository.get(user["id"], created["id"]) is None
