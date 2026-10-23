"""方案持久化的测试。

这里最要紧的一组是**越权**：数据隔离是安全边界，不能靠「路由层记得检查」，
因此不仅要测正常流程，还要逐个验证「别人的方案在我这里等同于不存在」。
"""

from __future__ import annotations

import pytest

from conftest import login, register, users

# 一份最小的方案状态。字段与 `schemas.SolutionStatePayload` 对齐——
# 聊天记录**不在这里**：它由服务端写进 `chat_turns`，只走轮次接口。
STATE = {
    "view_state": "review-prd",
    "entry_mode": "structured",
    "chat_tab": "requirement",
    "summary": {"product_name": "团队周报助手", "product_goal": "让周报自动生成"},
    "quick_product_name": "",
    "quick_requirements": "",
    "draft_prd_text": "",
    "paste_prd_text": "",
    "paste_api_docs_text": "",
    "active_job_id": None,
    "active_job_kind": "",
    "active_job_doc_kind": "",
}


def _payload(
    title: str = "团队周报助手",
    mode: str = "structured",
    stage: str = "review-prd",
    state: dict | None = None,
    content_changed: bool = True,
) -> dict:
    """构造保存请求体。

    `content_changed` 缺省为真：这个参数是「这次保存报了一次内容变更」，绝大多数用例
    模拟的都是一次真实编辑。要测「只翻页」的保存，显式传 `False`。
    """
    return {
        "title": title,
        "mode": mode,
        "stage": stage,
        "state": state if state is not None else STATE,
        "content_changed": content_changed,
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
    """保存后读回，状态与草稿必须逐字一致（含中文）。"""
    register(client)
    created = _create(client)

    assert created["title"] == "团队周报助手"
    assert isinstance(created["id"], int) and created["id"] > 0

    detail = client.get(f"/api/solutions/{created['id']}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["state"]["summary"] == STATE["summary"]
    assert body["state"]["view_state"] == "review-prd"
    # 中文没有被转义成 \uXXXX，也没有被包成字符串来回搬运
    assert "团队周报助手" in str(body["state"]["summary"])
    assert body["created_at"] == body["updated_at"]


def test_update_overwrites_whole_state(client):
    """更新是整份覆盖：没传的字段回到空值，不会残留上一版，updated_at 会前进。"""
    register(client)
    created = _create(client)

    response = client.put(
        f"/api/solutions/{created['id']}",
        json=_payload(
            title="改名后",
            stage="done",
            state={"view_state": "done", "draft_prd_text": "# 改过的标题"},
        ),
    )
    assert response.status_code == 200

    body = client.get(f"/api/solutions/{created['id']}").json()
    assert body["state"]["draft_prd_text"] == "# 改过的标题"
    # 整份覆盖的语义：上一版的问卷不会因为这次没传就保留下来
    assert body["state"]["summary"] is None
    assert body["state"]["entry_mode"] == ""
    assert body["title"] == "改名后"
    assert body["stage"] == "done"
    assert body["updated_at"] >= body["created_at"]


def test_list_returns_summaries_without_state(client):
    """列表只给摘要——响应里不该出现 state，避免把草稿一起拖出来。"""
    register(client)
    _create(client, title="方案甲")
    _create(client, title="方案乙")

    response = client.get("/api/solutions")
    assert response.status_code == 200
    items = response.json()["solutions"]
    assert [item["title"] for item in items] == ["方案乙", "方案甲"]  # 最近更新在前
    for item in items:
        assert "state" not in item, "列表接口不应返回状态与草稿"


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
        json=_payload(title="被篡改", state={"view_state": "done"}),
    )
    assert response.status_code == 404

    as_alice()
    body = client.get(f"/api/solutions/{solution_id}").json()
    assert body["title"] == "alice 的机密方案"
    assert body["state"]["summary"] == STATE["summary"]


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
        ("post", "/api/solutions", {"title": "x"}),
        ("get", "/api/solutions/1", None),
        ("put", "/api/solutions/1", {"title": "x"}),
        ("delete", "/api/solutions/1", None),
    ],
)
def test_all_solution_endpoints_require_login(client, method, path, body):
    """五个接口都必须在未登录时拒绝——它们不在白名单里，由中间件统一拦。"""
    call = getattr(client, method)
    response = call(path, json=body) if body is not None else call(path)
    assert response.status_code == 401
    assert response.json()["detail"] == "请先登录后再使用该功能"


def test_state_is_optional(client):
    """只提交标题 / 阶段也能建——状态全空是合法输入。

    旧契约要求 `data` 非空（「一份没有任何内容的快照没有保存的意义」）。拆成结构化
    字段之后，这个约束就失去了落点：`state` 的每一项都有各自的默认值，而「有内容
    才建方案」的判定在前端（`hasContent`），后端再拦一次只会让两边口径分叉。
    """
    register(client)
    response = client.post("/api/solutions", json={"title": "空方案", "stage": "entry"})
    assert response.status_code == 201

    body = client.get(f"/api/solutions/{response.json()['id']}").json()
    assert body["state"]["view_state"] == ""
    assert body["state"]["summary"] is None


def test_large_draft_round_trips(client):
    """粘贴的正文按原样存取，不做截断——它是用户输入，不是展示用的冗余。

    摘要是唯一留在 JSON 里的一项（问卷结构演进快）；这条用例同时守住
    「嵌套对象过一遍 JSON 列后仍然等值」。
    """
    register(client)
    draft = "# 很长的 PRD\n\n" + "正文段落。\n" * 500
    summary = {"product_name": "长文产品", "target_users": ["运营", "研发"]}
    created = _create(
        client, state={"draft_prd_text": draft, "summary": summary}
    )

    body = client.get(f"/api/solutions/{created['id']}").json()
    assert body["state"]["draft_prd_text"] == draft
    assert body["state"]["summary"] == summary


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
    from backend.core.engine import session_scope
    from backend.repositories.solution_repo import SolutionRepository
    from backend.services import auth_service

    register(client)
    created = _create(client)
    user = users.get_by_username("alice")
    assert user is not None

    auth_service.delete_user("alice")
    # v6：仓储是绑定 Session 的实例方法，直查要自己开一个作用域。
    with session_scope() as session:
        assert SolutionRepository(session).get(user["id"], created["id"]) is None


# ---------------------------------------------------------------------------
# 两根时间轴：`updated_at`（写库）与 `content_updated_at`（内容变更）
#
# 存在的理由：快照里同时住着内容（摘要、对话、粘贴的材料）与相位（`viewState`），
# 而切阶段也会写库。没有这第二根轴，列表里永远是一片「刚刚」，排序也会被「刚点开
# 看过的那份」占据。这里盯的是「哪次保存该推进它、哪次不该」。
# ---------------------------------------------------------------------------


def test_create_stamps_content_time(client):
    """新建即内容非空（方案是「有内容了才建」的），两根时间轴都从此刻起步。"""
    register(client)
    created = _create(client)

    assert created["content_updated_at"] != ""
    assert created["content_updated_at"] == created["updated_at"]


def test_navigation_save_keeps_content_time(client):
    """只切阶段的保存**不**推进内容时间，但仍然刷新写库时间。

    `updated_at` 照旧前进是刻意的：它回答的是「这份记录最近一次被写是什么时候」，
    而这件事确实刚刚发生过。两根轴各答一个问题，不是二选一。
    """
    register(client)
    created = _create(client)
    before = created["content_updated_at"]

    # 只换 stage，内容快照一字未动
    response = client.put(
        f"/api/solutions/{created['id']}",
        json=_payload(stage="done", content_changed=False),
    )
    assert response.status_code == 200
    after = response.json()

    assert after["content_updated_at"] == before
    assert after["updated_at"] >= created["updated_at"]


def test_content_save_advances_content_time(client):
    """`content_changed=True` 才推进内容时间。"""
    register(client)
    created = _create(client)

    changed = {**STATE, "draft_prd_text": "# 新贴进来的一份 PRD"}
    response = client.put(
        f"/api/solutions/{created['id']}",
        json=_payload(state=changed, content_changed=True),
    )
    assert response.status_code == 200

    after = response.json()
    assert after["content_updated_at"] >= created["content_updated_at"]
    # 内容真的落库了（推进时间的那次保存不能是空写）
    detail = client.get(f"/api/solutions/{created['id']}").json()
    assert "新贴进来的一份 PRD" in detail["state"]["draft_prd_text"]


def test_content_changed_defaults_to_false(client):
    """请求体没带这个字段时按「不算内容变更」处理。

    缺省取保守值：老前端（还没有这个字段）发的保存不该把内容时间顶到当下——
    轴一旦被无意义的写入推着走，就再也读不出「作品何时真的变了」。
    """
    register(client)
    created = _create(client)

    payload = _payload(stage="chatting")
    payload.pop("content_changed")
    response = client.put(f"/api/solutions/{created['id']}", json=payload)

    assert response.status_code == 200
    assert response.json()["content_updated_at"] == created["content_updated_at"]


def test_list_orders_by_content_time(client):
    """列表按**内容时间**倒序，而不是按写库时间。

    两条记录的时间由 SQL 直接写成确定值：真实流程里两次保存常落在同一秒内，
    时间戳可能相同、排序就退化成按 id，测不出想测的东西。
    这里让「只是被翻看过的那份」写库时间更新、内容时间更旧——按写库排会在前，
    按内容排必须在后。
    """
    from dbsupport import connect

    register(client)
    viewed = _create(client, title="只是被翻看过")
    edited = _create(client, title="真的改了内容")

    with connect() as conn:
        conn.execute(
            "UPDATE solutions SET content_updated_at = ?, updated_at = ? WHERE id = ?",
            ("2026-01-01T00:00:00+00:00", "2026-09-09T00:00:00+00:00", viewed["id"]),
        )
        conn.execute(
            "UPDATE solutions SET content_updated_at = ?, updated_at = ? WHERE id = ?",
            ("2026-05-05T00:00:00+00:00", "2026-05-05T00:00:00+00:00", edited["id"]),
        )
        conn.commit()

    titles = [item["title"] for item in client.get("/api/solutions").json()["solutions"]]
    assert titles == ["真的改了内容", "只是被翻看过"]
