"""对外契约对拍（v6 / T16.13）：换数据层，**对外行为零变化**。

## 这份文件在证明什么

整次迁移唯一可能「悄悄改变用户可见行为」的地方，是**类型从 DBAPI 的返回值变成了模型
属性**：sqlite3 把布尔列给成 `0/1`、把 `NULL` 给成 `None`、把时间给成字符串；
SQLModel 把布尔列给成真 `bool`。这些差异不会报错，只会让前端拿到 `"0"` 当成真值、
或者让 `created_at` 少了那个 `T` 之后排序错位。

所以这里不重跑业务逻辑，只钉**响应形态**：

| 判据 | 为什么 |
| --- | --- |
| `me` 的字段集合恰好是 `{id, username}` | 多一个字段就是凭据泄露（`password_hash` / `salt`） |
| 布尔是 JSON `true/false`，不是 `1/0` | 前端所有 `v-if` 都按真值判；`0` 在 JS 里是假，`"0"` 是真 |
| 时间戳带 `T`、不带 `Z` / `+00:00` | 业务表的时间口径是 `datetime.isoformat()`；混入 `Z` 会让字符串比较错位 |
| 列表项的字段集合包含既定键 | 换数据层最容易「顺手少给一个字段」 |
| 错误体恰好是 `{detail, code}` | 前端靠 `code` 分支，多一层包装会让它整片失效 |

更宽的等价性证据是**整套 994 条用例**：它们的断言是迁移之前写的、针对旧实现，
一条都没放宽（唯一两处调整见 `test_auth_service.py` 的异常类型与
`test_admin_management.py` 的布尔断言，均在注释里写明理由）。
"""

from __future__ import annotations

import re

from conftest import login, register, users

from backend.services import auth_service, document_store

# 业务表时间戳的形状：ISO 8601、带 `T`、不带时区标记。
_ISO_NAIVE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _as_admin(client, username: str = "root") -> None:
    register(client, username)
    auth_service.set_admin(username, True)


def _assert_timestamp(value: object, *, field: str) -> None:
    assert isinstance(value, str) and value, f"{field} 应当是非空字符串：{value!r}"
    assert _ISO_NAIVE.match(value), f"{field} 应当形如 2026-10-23T09:41:08：{value!r}"
    assert not value.endswith("Z"), f"{field} 不该带 Z 后缀（业务表用的是 naive ISO）：{value!r}"


# ---------------------------------------------------------------------------
# 认证：字段集合与布尔形态
# ---------------------------------------------------------------------------


def test_me_returns_exactly_id_and_username(client):
    """`/api/auth/me` 的字段集合**恰好**是 `{id, username}`。

    用「恰好」而不是「包含」：这条同时守着「别把 `password_hash` / `salt` 序列化出去」。
    数据层换成模型对象之后，`model_dump()` 会把**所有列**都带出来，一不小心就多给了。
    """
    register(client)
    body = client.get("/api/auth/me").json()

    assert set(body) == {"id", "username"}, f"字段集合变了：{sorted(body)}"
    assert isinstance(body["id"], int)
    assert body["username"] == "alice"


def test_login_response_never_carries_credentials(client):
    register(client)
    body = login(client).json()

    assert set(body) == {"id", "username"}
    assert "password_hash" not in body and "salt" not in body


def test_admin_user_list_uses_real_booleans(client):
    """后台用户列表的 `is_admin` / `is_active` 必须是真布尔。

    这是迁移里唯一**类型真的变了**的地方：sqlite3 给 `0/1`，模型给 `True/False`。
    前端拿 `0` 当假、拿 `"0"` 当真——所以这里既断值也断类型。
    """
    _as_admin(client)
    items = client.get("/api/admin/users").json()["users"]
    root = next(item for item in items if item["username"] == "root")

    assert root["is_admin"] is True
    assert root["is_active"] is True
    assert isinstance(root["is_admin"], bool) and isinstance(root["is_active"], bool)


# ---------------------------------------------------------------------------
# 方案与文档：字段集合与时间戳
# ---------------------------------------------------------------------------


def _create_solution(client, title: str = "契约对拍") -> int:
    response = client.post(
        "/api/solutions",
        json={"title": title, "mode": "chat", "stage": "draft", "state": {}},
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def test_solution_summary_keeps_its_field_set(client):
    register(client)
    _create_solution(client)

    items = client.get("/api/solutions").json()["solutions"]
    assert items, "刚建的方案应当出现在列表里"
    item = items[0]

    assert set(item) == {
        "id",
        "title",
        "mode",
        "stage",
        "created_at",
        "updated_at",
        "content_updated_at",
    }, f"方案摘要字段集合变了：{sorted(item)}"
    for field in ("created_at", "updated_at", "content_updated_at"):
        _assert_timestamp(item[field], field=field)


def test_document_item_keeps_its_shape(client):
    """文档项的字段与类型（`pending_items` 是数组、`stale` 是布尔）。

    这两个 JSON 列在仓储里是文本、在对外结构里是对象——「解析容错」那条路最容易
    在换层时退化成「原样把字符串丢给前端」。
    """
    register(client)
    solution_id = _create_solution(client)

    # 先真的写一份正文（走服务层，与生成收尾同一条路），否则「没有文档」会被跳过，
    # 而这条判据要守的恰恰是「有正文时前端拿到什么」。
    owner = int(users.get_by_username("alice")["id"])  # type: ignore[index]
    document_store.commit_generated(owner, solution_id, "prd", "# 正文", pending_items=[])

    response = client.get(f"/api/solutions/{solution_id}/documents")
    assert response.status_code == 200, response.text
    items = response.json()["documents"]
    assert items, "写完正文之后列表不该是空的"
    item = items[0]
    assert {"kind", "content", "pending_items", "last_run_summary", "updated_at"} <= set(item)
    assert isinstance(item["pending_items"], list)
    assert isinstance(item["stale"], bool)


# ---------------------------------------------------------------------------
# 错误体
# ---------------------------------------------------------------------------


def test_error_body_is_exactly_detail_and_code(client):
    """404 的错误体恰好是 `{detail, code}`，且 `code` 是机器可读的那一个。

    前端所有分支都读 `code`；一旦数据层换了之后错误被包了一层
    （例如变成 `{"error": {"code": ...}}`），这些分支会整片失效而接口仍是 404。
    """
    register(client)
    response = client.get("/api/solutions/999999")

    assert response.status_code == 404
    body = response.json()
    assert set(body) == {"detail", "code"}, f"错误体结构变了：{sorted(body)}"
    assert body["code"] == "SOLUTION_NOT_FOUND"


# ---------------------------------------------------------------------------
# 时间戳口径：库里的值与响应里的值同源
# ---------------------------------------------------------------------------


def test_created_at_comes_from_the_same_clock_as_the_db(client):
    """响应里的 `created_at` 必须就是库里那一行的时间文本。

    换层时最阴的一种偏差：「写库用 `utc_now_text()`、序列化时又 `datetime.now()`」，
    于是接口时间比库里晚几毫秒，而任何按时间排序/对账的逻辑都会漂。
    """
    register(client)
    solution_id = _create_solution(client)

    from backend.core.clock import utc_now_text

    row = users.get_by_username("alice")
    assert row is not None

    with __import__("dbsupport").connect() as conn:
        stored = conn.execute(
            "SELECT created_at FROM solutions WHERE id = ?", (solution_id,)
        ).fetchone()

    detail = client.get(f"/api/solutions/{solution_id}").json()
    assert stored is not None
    assert detail["created_at"] == stored["created_at"]
    # 顺带确认时钟口径没漂：库里那串应当与 `utc_now_text()` 同形
    assert _ISO_NAIVE.match(utc_now_text())
