"""日志检索接口（v4 T4b）：把攒下来的日志变成可查的。

T4a 让主链路开始写日志，这里验的是**能把它查出来**。重点不在「能返回数据」，
而在三条排障时真正会用的路径：

1. **按级别筛**——出事了先看 ERROR，而「选 WARNING」必须含 ERROR（人说的
   「警告」几乎总是「警告及以上」）；
2. **按 `request_id` 反查**——粘一个请求号，拉出这一次请求的全部日志。
   这是排障主路径，也是 `app_logs.request_id` 这列存在的理由；
3. **分页**——日志只增不减，`total` 必须是**命中总数**而不是本页条数，
   否则前端算不出页数。

另外两条反向用例：非管理员 401/403 一律拦死（`require_admin` 是逐条挂的，
漏挂一个端点它就完全裸奔，而它看起来和别的一模一样），以及关键词里的 `%`
不能被当成通配符（搜「50%」不该把整表搜出来）。
"""

from __future__ import annotations

import json

import pytest
from conftest import register

from backend.core.clock import utc_ms_text, utc_now
from backend.core.engine import session_scope
from backend.repositories.log_repo import LogRepository


def _as_admin(client, username: str = "root") -> None:
    """注册并任命一个管理员（顺带让 client 处于该账号的登录态）。"""
    from backend.services import auth_service

    register(client, username)
    auth_service.set_admin(username, True)


def _insert_many(rows: list[dict]) -> int:
    """写一批日志（仓库不再自己提交，事务由这里显式开）。"""
    with session_scope() as session:
        return LogRepository(session).insert_many(rows)


def _row(**overrides) -> dict:
    """造一行可入库的日志，未指定的字段用合法默认值。"""
    base = {
        "ts": utc_ms_text(utc_now()),
        "level": "INFO",
        "logger": "backend.services.job_service",
        "msg": "生成任务已派发",
        "request_id": None,
        "user_id": None,
        "event": None,
        "extra_json": "",
        "exc": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 隔离：本文件的用例自己往 `app_logs` 造数据，不允许旁路再异步写进来
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_async_log_writes(isolated_db, monkeypatch):  # noqa: ARG001
    """关掉「日志落库」这一路，只留文件。

    为什么必须关：`deps.py` 的中间件会把**每个请求**记一条 `request.completed`，
    经 `LogStoreHandler` 的后台线程异步写进 `app_logs`。而本文件好几条用例的断言是
    「插了 N 条 → 全表就是 N 条」。两者赛跑的结果是同一份代码**时绿时红**：

    - 断言先跑完 → 冲刷还没落地 → 绿；
    - 冲刷先落地 → `total` 变成 N+5 → 红。

    这正是「单文件跑绿、全量跑红」的来源——全量时机器更忙、前一串用例让它更热，
    后台线程更早把那一批写进去。**这不是产品缺陷，是断言建立在一个会变的量上。**

    关掉落库对断言没有放松：本文件被测的是**检索接口**，落库链路由
    `test_log_store.py` 专门覆盖。关掉之后，「全表」重新等于「本用例插入的那些行」。
    """
    from backend.core.config import get_settings

    monkeypatch.setenv("LOG_DB_ENABLED", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------


def test_logs_endpoint_returns_items_and_total(client):
    """基本形状：返回一页日志与**命中总数**。"""
    _as_admin(client)
    _insert_many(
        [
            _row(msg="第一条", event="job.started", extra_json=json.dumps({"job_id": 1})),
            _row(msg="第二条", level="ERROR", event="job.failed"),
        ]
    )

    response = client.get("/api/admin/logs")
    assert response.status_code == 200, response.text

    data = response.json()
    assert data["total"] == 2
    assert data["page"] == 1
    assert len(data["items"]) == 2
    # 倒序：最新的那条在前（这里两条同毫秒，按 id 倒序兜住）
    assert data["items"][0]["msg"] == "第二条"
    # `extra` 已由后端解析成对象，前端不必再解一遍 JSON
    assert data["items"][1]["extra"] == {"job_id": 1}


def test_level_filter_includes_higher_levels(client):
    """选 WARNING = 「警告及以上」：必须把 ERROR 也带出来。

    这条防的是把 `>=` 写成 `=`——那样管理员筛 WARNING 会看不见 ERROR，
    而「出事了先看有哪些异常」正是他打开这个页面的原因。
    """
    _as_admin(client)
    _insert_many(
        [
            _row(msg="普通", level="INFO"),
            _row(msg="警告", level="WARNING"),
            _row(msg="错误", level="ERROR"),
            _row(msg="崩溃", level="CRITICAL"),
        ]
    )

    response = client.get("/api/admin/logs", params={"level": "WARNING"})
    assert response.status_code == 200, response.text

    messages = {item["msg"] for item in response.json()["items"]}
    assert messages == {"警告", "错误", "崩溃"}, f"WARNING 应含更高级别，实际：{messages}"


def test_request_id_lookup_returns_whole_trace(client):
    """粘一个请求号，拉出该请求的**全部**日志（排障主路径）。"""
    _as_admin(client)
    _insert_many(
        [
            _row(msg="这次请求的 1", request_id="req-abc", event="job.started"),
            _row(msg="这次请求的 2", request_id="req-abc", event="job.stage"),
            _row(msg="这次请求的 3", request_id="req-abc", event="job.completed"),
            _row(msg="别人的", request_id="req-other"),
            _row(msg="没有请求号的启动日志"),
        ]
    )

    response = client.get("/api/admin/logs", params={"request_id": "req-abc"})
    assert response.status_code == 200, response.text

    data = response.json()
    assert data["total"] == 3
    assert {item["msg"] for item in data["items"]} == {
        "这次请求的 1",
        "这次请求的 2",
        "这次请求的 3",
    }


def test_pagination_reports_total_not_page_size(client):
    """`total` 是命中总数，`items` 只是当前页——否则前端算不出页数。"""
    _as_admin(client)
    _insert_many([_row(msg=f"日志 {index}") for index in range(25)])

    first = client.get("/api/admin/logs", params={"page": 1, "page_size": 10})
    assert first.status_code == 200, first.text
    assert first.json()["total"] == 25
    assert len(first.json()["items"]) == 10

    third = client.get("/api/admin/logs", params={"page": 3, "page_size": 10})
    assert third.status_code == 200, third.text
    assert len(third.json()["items"]) == 5

    # 两页之间不该有重叠：分页靠 OFFSET 推进，写错就会「第二页重复第一页的内容」
    first_ids = {item["id"] for item in first.json()["items"]}
    third_ids = {item["id"] for item in third.json()["items"]}
    assert not (first_ids & third_ids)


def test_keyword_percent_is_literal_not_wildcard(client):
    """搜「50%」时 `%` 必须当普通字符，不能变成「匹配一切」。

    不转义的话这条查询会返回整张表——用户看到的是「搜什么都全中」，
    而他不会想到是通配符在作祟。
    """
    _as_admin(client)
    _insert_many(
        [
            _row(msg="完成度 50% 左右"),
            _row(msg="完全无关的一条"),
        ]
    )

    response = client.get("/api/admin/logs", params={"keyword": "50%"})
    assert response.status_code == 200, response.text

    messages = {item["msg"] for item in response.json()["items"]}
    assert messages == {"完成度 50% 左右"}, f"`%` 被当成了通配符，实际：{messages}"


def test_event_and_user_filters(client):
    """按 event 精确筛、按 user_id 归属筛。"""
    _as_admin(client)
    _insert_many(
        [
            _row(msg="A", event="job.started", user_id=7),
            _row(msg="B", event="job.failed", user_id=7),
            _row(msg="C", event="job.started", user_id=9),
        ]
    )

    by_event = client.get("/api/admin/logs", params={"event": "job.started"})
    assert {item["msg"] for item in by_event.json()["items"]} == {"A", "C"}

    by_user = client.get("/api/admin/logs", params={"user_id": 7})
    assert {item["msg"] for item in by_user.json()["items"]} == {"A", "B"}


def test_exclude_event_hides_access_logs_but_keeps_null_events(client):
    """排掉访问日志时，**没有事件名的日志必须保留**。

    这条最容易写错：`event != 'request.completed'` 在 SQL 里对 `NULL` 求值为
    `NULL`（不成立），会把所有「没有事件名」的日志一起丢掉——而那些恰恰包括
    启动日志、迁移日志这类最该看得见的东西，且丢掉时不报错、只是「少了几行」。
    """
    _as_admin(client)
    _insert_many(
        [
            _row(msg="业务起点", event="job.started"),
            _row(msg="访问日志 1", event="request.completed"),
            _row(msg="访问日志 2", event="request.completed"),
            # 没有事件名的日志（如迁移/启动阶段）
            _row(msg="无事件名", event=None),
        ]
    )

    response = client.get(
        "/api/admin/logs", params={"exclude_event": "request.completed"}
    )
    assert response.status_code == 200, response.text

    messages = {item["msg"] for item in response.json()["items"]}
    assert messages == {"业务起点", "无事件名"}, f"排除结果不对，实际：{messages}"
    assert response.json()["total"] == 2


def test_broken_extra_json_degrades_to_empty_object(client):
    """`extra_json` 解析失败时给 `{}`，不抛错——

    一条格式异常的旧日志不该让整个列表 500（那时管理员恰恰最需要看到列表）。
    """
    _as_admin(client)
    _insert_many([_row(msg="坏 JSON", extra_json="{不是合法 JSON")])

    response = client.get("/api/admin/logs")
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["extra"] == {}


# ---------------------------------------------------------------------------
# 门禁：没有管理员身份就看不到日志
# ---------------------------------------------------------------------------


def test_logs_requires_admin(client):
    """普通用户（已登录）访问日志接口必须 403。

    `require_admin` 是**逐条**挂的：漏挂一个端点，它就完全裸奔，
    而它看起来和别的受保护端点一模一样。
    """
    register(client)  # alice：普通用户

    response = client.get("/api/admin/logs")
    assert response.status_code == 403, response.text


def test_logs_requires_login(client):
    """未登录必须 401（而不是 403）：401 让前端去登录，403 会让它停在无权限页。"""
    response = client.get("/api/admin/logs")
    assert response.status_code == 401, response.text
