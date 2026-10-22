"""平台统计：按日趋势与用户排行（v4 T2）。

这些用例守的不是「数字算出来了」，而是**三个容易算错却不报错的口径**：

1. **主动取消不算失败**。`CANCELLED` / `SUPERSEDED` 在库里同样是 `status='failed'`，
   直接 `COUNT(status='failed')` 会让平台看起来比实际脆弱——而管理员正是靠这个
   数字判断「模型稳不稳」。
2. **没有任务的那天要补 0 行**。折线图跳过空白日，会把「那天没人用」画成一条平滑
   斜线，看起来像用量在缓慢变化。0 与「没有这一天」是两个不同的结论。
3. **按日按本地时区，不按 UTC**。东八区 23:30 生成的任务，UTC 已是次日——按 UTC
   分组会把它算到明天，于是「昨晚用的」出现在「今天」那一格。

另外还有两条门禁用例：`require_admin` 是逐条挂的，漏一个端点它就完全裸奔。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import login, register

from backend.core.db import connect, utc_now


def _as_admin(client, username: str = "root") -> None:
    """注册并任命一个管理员（顺带让 client 处于该账号的登录态）。"""
    from backend.services import auth_service

    register(client, username)
    auth_service.set_admin(username, True)


def _user_id(username: str) -> int:
    from backend.services import auth_service

    row = auth_service.UserRepository.get_by_username(username)
    assert row is not None
    return int(row["id"])


def _job(
    owner_id: int,
    *,
    status: str = "completed",
    error_code: str = "",
    created_at: str | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> None:
    """直接插一条任务（测试造数据用，绕开仓储的 running-only 建法）。"""
    moment = created_at or utc_now().isoformat()
    with connect() as conn:
        conn.execute(
            "INSERT INTO generation_jobs "
            "(owner_id, solution_id, kind, doc_kind, chat_stage, status, phase, "
            " draft_content, completed_sections, input_fingerprint, usage, "
            " max_provider_switches, error_code, error_message, "
            " prompt_tokens, completion_tokens, created_at, updated_at) "
            "VALUES (?, NULL, 'prd', '', '', ?, '', '', '[]', NULL, NULL, 2, ?, '', ?, ?, ?, ?)",
            (
                owner_id,
                status,
                error_code,
                prompt_tokens,
                completion_tokens,
                moment,
                moment,
            ),
        )
        conn.commit()


def _days_ago(days: int, *, hour: int = 12) -> str:
    """造一个「本地 days 天前 hour 点」的 UTC 串（按 UTC 直接算，不做时区换算）。"""
    moment = utc_now() - timedelta(days=days)
    return moment.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# 按日趋势
# ---------------------------------------------------------------------------


def test_daily_fills_missing_days_with_zero(client):
    """没有任务的那天也要出现（各项为 0），否则折线会跨过空白日。

    用例只造「今天」的数据，其余 6 天必须都是一行 0——若 SQL 只返回有数据的组，
    这里会拿到 1 行，而图上看不出到底是「没人用」还是「这一天不存在」。
    """
    _as_admin(client)
    owner_id = _user_id("root")
    _job(owner_id)

    response = client.get(
        "/api/admin/stats/daily", params={"days": 7, "tz_offset_minutes": 0}
    )
    assert response.status_code == 200, response.text

    data = response.json()
    assert data["days"] == 7
    assert len(data["items"]) == 7
    # 日期必须连续且升序，最后一个才是今天
    days = [item["day"] for item in data["items"]]
    assert days == sorted(days)
    assert days[-1] == utc_now().date().isoformat()
    # 中间那几天都是 0
    assert all(item["task_count"] == 0 for item in data["items"][:-1])
    assert data["items"][-1]["task_count"] == 1


def test_cancelled_and_superseded_are_not_counted_as_failures(client):
    """主动取消 / 被顶替不算真失败，但它们的数量单独可见。

    这是本文件最重要的一条。两者在库里都是 `status='failed'`：
    - `CANCELLED`：用户自己删了方案或点了停止；
    - `SUPERSEDED`：同一份文档被更晚的任务接手，旧的主动让开。

    把它们算进失败率，平台会显得比实际脆弱得多。
    """
    _as_admin(client)
    owner_id = _user_id("root")
    _job(owner_id, status="completed")
    _job(owner_id, status="failed", error_code="LLM_UPSTREAM_ERROR")
    _job(owner_id, status="failed", error_code="CANCELLED")
    _job(owner_id, status="failed", error_code="SUPERSEDED")

    response = client.get(
        "/api/admin/stats/daily", params={"days": 7, "tz_offset_minutes": 0}
    )
    today = response.json()["items"][-1]

    assert today["task_count"] == 4
    assert today["completed_count"] == 1
    # 4 条 failed 里只有一条是真失败
    assert today["failed_count"] == 1, "只有 LLM_UPSTREAM_ERROR 算真失败"
    assert today["cancelled_count"] == 2, "取消与顶替要单独计数，不能凭空消失"
    # 分母 = 已收尾（1 成功 + 1 真失败），取消的不进分母
    assert today["failure_rate"] == 0.5


def test_running_jobs_are_excluded_from_failure_rate_denominator(client):
    """还在跑的任务既不算成功也不算失败，不该进失败率的分母。

    放进分母会把失败率人为压低：一个正在跑的任务会把 1/1 的失败率稀释成 1/2。
    """
    _as_admin(client)
    owner_id = _user_id("root")
    _job(owner_id, status="failed", error_code="LLM_UPSTREAM_ERROR")
    _job(owner_id, status="running")
    _job(owner_id, status="running")

    response = client.get(
        "/api/admin/stats/daily", params={"days": 7, "tz_offset_minutes": 0}
    )
    today = response.json()["items"][-1]

    assert today["task_count"] == 3
    assert today["running_count"] == 2
    # 1 失败 / 1 已收尾 = 100%，而不是 1/3
    assert today["failure_rate"] == 1.0


def test_empty_day_has_null_failure_rate(client):
    """没有任务的那天，失败率是 `null` 而不是 0。

    0% 会被读成「全成功了」，而真相是「那天没有任何任务」。
    """
    _as_admin(client)

    response = client.get(
        "/api/admin/stats/daily", params={"days": 3, "tz_offset_minutes": 0}
    )
    for item in response.json()["items"]:
        assert item["failure_rate"] is None, f"空日应给 null：{item}"


def test_daily_groups_by_local_date_not_utc(client):
    """同一个 UTC 时刻，按东八区与按 UTC 会落到不同的「日」。

    构造 UTC 16:30 —— 在东八区是次日 00:30。这条若按 UTC 分组，管理员会看到
    「昨晚用的」出现在「今天」那一格。

    ⚠️ 刻意用**昨天** 16:30，而不是「今天 16:30」：后者在 UTC 16:30 之前跑用例时
    还属于**未来**，查询窗口不含未来数据，用例会在每天上午随机变红（与代码无关）。
    判据只看「有没有跨过 UTC 与东八区的日界」，用哪天都成立。
    """
    _as_admin(client)
    owner_id = _user_id("root")

    moment = (utc_now() - timedelta(days=1)).replace(
        hour=16, minute=30, second=0, microsecond=0
    )
    _job(owner_id, created_at=moment.isoformat())

    # 按 UTC 看：归在 moment 当天
    utc_scope = client.get(
        "/api/admin/stats/daily", params={"days": 3, "tz_offset_minutes": 0}
    ).json()
    utc_days = {item["day"]: item["task_count"] for item in utc_scope["items"]}
    assert utc_days.get(moment.date().isoformat()) == 1

    # 按东八区看：同一个任务落在次日
    local_scope = client.get(
        "/api/admin/stats/daily", params={"days": 3, "tz_offset_minutes": 480}
    ).json()
    local_days = {item["day"]: item["task_count"] for item in local_scope["items"]}
    expected_day = (moment + timedelta(hours=8)).date().isoformat()
    assert local_days.get(expected_day) == 1, f"应按本地日期归组，实际：{local_days}"


def test_daily_aggregates_tokens(client):
    """Token 按天累加，读的是冗余列而不是解析 usage JSON。"""
    _as_admin(client)
    owner_id = _user_id("root")
    _job(owner_id, prompt_tokens=100, completion_tokens=50)
    _job(owner_id, prompt_tokens=200, completion_tokens=80)

    today = client.get(
        "/api/admin/stats/daily", params={"days": 1, "tz_offset_minutes": 0}
    ).json()["items"][-1]

    assert today["prompt_tokens"] == 300
    assert today["completion_tokens"] == 130


def test_summary_failure_rate_is_weighted_not_averaged(client):
    """区间失败率是**加权**的（区间失败 / 区间已收尾），不是「每日失败率的平均」。

    造一个极端：昨天 1 个任务全失败（100%），今天 9 个任务全成功（0%）。
    - 正确（加权）：1 / 10 = 10%
    - 错误（每日平均）：(100% + 0%) / 2 = 50%

    差 5 倍。任务量少的那天与任务量大的那天等权，会让真实失败率严重失真——
    而管理员正是靠这个数字判断「今天要不要去查」。
    """
    _as_admin(client)
    owner_id = _user_id("root")

    _job(owner_id, status="failed", error_code="LLM_UPSTREAM_ERROR", created_at=_days_ago(1))
    for _ in range(9):
        _job(owner_id, status="completed", created_at=_days_ago(0))

    data = client.get(
        "/api/admin/stats/daily", params={"days": 3, "tz_offset_minutes": 0}
    ).json()

    summary = data["summary"]
    assert summary is not None, "响应里必须带区间汇总，否则前端只能自己再算一遍"
    assert summary["task_count"] == 10
    assert summary["completed_count"] == 9
    assert summary["failed_count"] == 1
    assert summary["failure_rate"] == 0.1, (
        f"应为加权 1/10=0.1，若得到 0.5 说明用了每日平均：{summary['failure_rate']}"
    )
    # 汇总行的 day 用空串明确表示「不是某一天」
    assert summary["day"] == ""


def test_summary_failure_rate_is_null_when_nothing_finished(client):
    """区间内没有任何已收尾任务时，汇总是 `null` 而不是 0。"""
    _as_admin(client)
    owner_id = _user_id("root")
    _job(owner_id, status="running")

    summary = client.get(
        "/api/admin/stats/daily", params={"days": 3, "tz_offset_minutes": 0}
    ).json()["summary"]
    assert summary["failure_rate"] is None


# ---------------------------------------------------------------------------
# 用户排行
# ---------------------------------------------------------------------------


def test_top_users_excludes_users_without_jobs(client):
    """排行榜只列真的生成过任务的人：塞一堆 0 会把真用户挤下去。"""
    _as_admin(client)
    root_id = _user_id("root")
    register(client, "idle")  # 注册了但从未生成
    register(client, "busy")

    _job(root_id)
    _job(_user_id("busy"))
    # `register` 会把 client 的登录态换成新注册的普通用户，必须切回管理员
    login(client, "root")

    response = client.get("/api/admin/stats/users")
    assert response.status_code == 200, response.text

    names = [item["username"] for item in response.json()["users"]]
    assert "idle" not in names, "零任务的用户不该出现在排行榜里"
    assert set(names) == {"root", "busy"}


def test_top_users_ordered_by_task_count_then_tokens(client):
    """排序：先按任务数，再按总 Token（任务数相同的人里，烧得多的更值得注意）。"""
    _as_admin(client)
    root_id = _user_id("root")
    register(client, "heavy")
    register(client, "light")

    _job(root_id, prompt_tokens=100, completion_tokens=100)
    _job(_user_id("heavy"), prompt_tokens=5000, completion_tokens=5000)
    _job(_user_id("light"), prompt_tokens=10, completion_tokens=10)
    _job(_user_id("heavy"), prompt_tokens=5000, completion_tokens=5000)
    login(client, "root")

    users = client.get("/api/admin/stats/users").json()["users"]

    assert users[0]["username"] == "heavy"  # 2 个任务，最多
    # root 与 light 都是 1 个任务，Token 多的在前
    assert users[1]["username"] == "root"
    assert users[2]["username"] == "light"


def test_top_users_limit(client):
    """`limit` 生效，且不超上限。"""
    _as_admin(client)
    root_id = _user_id("root")
    for index in range(3):
        register(client, f"user{index}")
        _job(_user_id(f"user{index}"))
    _job(root_id)
    login(client, "root")

    users = client.get("/api/admin/stats/users", params={"limit": 2}).json()["users"]
    assert len(users) == 2


def test_top_users_matches_user_list_numbers(client):
    """排行榜与用户列表对同一个用户必须给出同一个 Token 数。

    两处各写一遍聚合，迟早会出现两个数（100 万 vs 98 万），
    而管理员不知道该信哪个。这条钉住「共用同一段表达式」。
    """
    _as_admin(client)
    register(client, "alice2")
    alice_id = _user_id("alice2")
    _job(alice_id, prompt_tokens=1234, completion_tokens=567)
    _job(alice_id, prompt_tokens=1000, completion_tokens=400)
    login(client, "root")

    listed = {row["username"]: row for row in client.get("/api/admin/users").json()["users"]}
    ranked = {row["username"]: row for row in client.get("/api/admin/stats/users").json()["users"]}

    assert listed["alice2"]["prompt_tokens"] == ranked["alice2"]["prompt_tokens"] == 2234
    assert listed["alice2"]["completion_tokens"] == ranked["alice2"]["completion_tokens"] == 967
    assert listed["alice2"]["task_count"] == ranked["alice2"]["task_count"] == 2


# ---------------------------------------------------------------------------
# 门禁
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/admin/stats/daily", "/api/admin/stats/users"])
def test_stats_endpoints_require_admin(client, path):
    """普通用户（已登录）访问统计接口必须 403。

    `require_admin` 是逐条挂的：新加端点时最容易漏，而漏了的端点和别的一模一样。
    """
    register(client)  # alice：普通用户
    assert client.get(path).status_code == 403


@pytest.mark.parametrize("path", ["/api/admin/stats/daily", "/api/admin/stats/users"])
def test_stats_endpoints_require_login(client, path):
    """未登录 401（而不是 403）：401 让前端去登录，403 会让它停在无权限页。"""
    assert client.get(path).status_code == 401
