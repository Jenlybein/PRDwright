"""管理端「四个数字」（票 #89）：排队数 / 成功率 / 平均耗时 / 卡死数。

这四条用例守的不是「数字算出来了」，而是**四个会被读错的统计口径**：

1. **数字由服务端从数据库算，前端只展示**。前端自己推算，等于同一套口径写两遍，
   两边一旦漂移，管理员不知道该信哪个。
2. **成功率的分母只有「已定稿 + 真失败」**。`superseded` / `cancelled` 各有自己的
   状态、都没有结果，把它们算进分母会让平台显得比实际脆弱——而管理员正是靠这个
   数字判断「要不要现在就去查」。
3. **卡死只由服务端判定**，判据是「执行中」且（租约已过期 或 超过阈值未更新）。
   七态之后「仍 running 但租约已过期」是一个真实存在、由 #83 恢复器收口的**窗口**：
   恢复器还没跑到时，这些行必须被看见，否则「卡死数」永远为 0。
4. **四个数字与任务列表同源**：卡死数可点，点进去的列表必须恰好是那几条——
   两处各写一套判据，会出现「卡片说 1 个、列表里 0 个」。
"""

from __future__ import annotations

from datetime import timedelta

from conftest import login, register, users

from backend.core.clock import utc_now
from backend.services import auth_service
from dbsupport import connect


def _as_admin(client, username: str = "root") -> None:
    register(client, username)
    auth_service.set_admin(username, True)


def _user_id(username: str) -> int:
    row = users.get_by_username(username)
    assert row is not None
    return int(row["id"])


def _job(
    owner_id: int,
    *,
    status: str = "succeeded",
    age_seconds: int | None = None,
    created_at=None,
    updated_at=None,
    lease_token: str = "",
    lease_expire_at: str = "",
    next_run_at: str = "",
    error_code: str = "",
) -> int:
    """直接插一条任务（绕开仓储里「建行即 pending」的那条建法，便于造各种状态）。

    `age_seconds` 是「创建于多少秒之前、最后更新于此刻」——用它造平均耗时时，
    差值就是这次任务的生命周期长度。
    """
    now = utc_now()
    created = created_at or (now - timedelta(seconds=age_seconds or 0))
    updated = updated_at or now
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO generation_jobs "
            "(owner_id, solution_id, kind, doc_kind, chat_stage, status, phase, "
            " draft_content, completed_sections, pending_items, input_fingerprint, usage, "
            " max_provider_switches, error_code, error_message, "
            " provider_protocol, provider_config_id, provider_name, provider_model, "
            " prompt_tokens, completion_tokens, lease_owner, lease_token, lease_expire_at, "
            " next_run_at, created_at, updated_at, "
            " request_params, config_snapshot, skill_refs, skill_manifest) "
            "VALUES (%s, NULL, 'prd', '', '', %s, '', '', '[]', '[]', NULL, NULL, 2, %s, '', "
            " '', '', '', '', 0, 0, '', %s, %s, %s, %s, %s, '{}', '{}', '[]', '') "
            "RETURNING id",
            (
                owner_id,
                status,
                error_code,
                lease_token,
                lease_expire_at,
                next_run_at,
                created.isoformat(),
                updated.isoformat(),
            ),
        )
        job_id = int(cur.fetchone()["id"])
        conn.commit()
    return job_id


def _stats(client) -> dict:
    response = client.get("/api/admin/stats")
    assert response.status_code == 200, response.text
    return response.json()


def _jobs(client, **params) -> dict:
    response = client.get("/api/admin/jobs", params=params)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# AC1：四个数字由数据库查询得出
# ---------------------------------------------------------------------------


def test_four_numbers_are_computed_by_the_server_from_the_database(client):
    """四个数字一次请求全给，且各有独立来源（不是从前端列表里数出来的）。

    造 2 条排队、1 条成功（100 秒）、1 条失败（200 秒）：
    - 排队数 = 2；
    - 成功率 = 1 / (1 + 1) = 0.5；
    - 平均耗时 = (100 + 200) / 2 = 150 秒（用例只要求整数秒，允许边界抖动）；
    - 卡死数 = 0（没有执行中的行）。
    """
    _as_admin(client)
    owner = _user_id("root")
    _job(owner, status="pending")
    _job(owner, status="pending")
    _job(owner, status="succeeded", age_seconds=100)
    _job(owner, status="failed", age_seconds=200, error_code="LLM_UPSTREAM_ERROR")

    stats = _stats(client)

    assert stats["queue_count"] == 2
    assert stats["success_rate"] == 0.5
    assert 149 <= stats["avg_duration_seconds"] <= 151
    assert stats["stuck_count"] == 0


def test_empty_platform_reports_null_rate_and_duration(client):
    """一条任务都没有时，成功率与平均耗时是 `null` 而不是 0。

    `0%` 会被读成「全都失败了」，`0 秒`会被读成「任务瞬间就失败」——
    而真相是「还没有数据」。排队数与卡死数是 0，它们是计数，0 就是 0。
    """
    _as_admin(client)

    stats = _stats(client)

    assert stats["queue_count"] == 0
    assert stats["success_rate"] is None
    assert stats["avg_duration_seconds"] is None
    assert stats["stuck_count"] == 0


# ---------------------------------------------------------------------------
# AC2：成功率排除被取代与已取消
# ---------------------------------------------------------------------------


def test_success_rate_excludes_superseded_and_cancelled(client):
    """分母只有已定稿 + 真失败；被取代与已取消既不进分子也不进分母。

    2 成功 / 1 失败 / 3 被取代 / 2 已取消：
    - 正确：2 / 3 ≈ 0.6667；
    - 错误（把终止态都算进去）：2 / 8 = 0.25 —— 平台会显得脆弱 4 倍。
    """
    _as_admin(client)
    owner = _user_id("root")
    for _ in range(2):
        _job(owner, status="succeeded", age_seconds=60)
    _job(owner, status="failed", age_seconds=60, error_code="LLM_UPSTREAM_ERROR")
    for _ in range(3):
        _job(owner, status="superseded")
    for _ in range(2):
        _job(owner, status="cancelled")

    stats = _stats(client)

    assert stats["success_rate"] == round(2 / 3, 4), "被取代 / 已取消不能改进分母"
    # 平均耗时同样只覆盖「已收尾」的那 3 条（成功 2 + 真失败 1）
    assert 59 <= stats["avg_duration_seconds"] <= 61


def test_success_rate_is_null_when_only_cancelled_and_superseded(client):
    """只有被取代 / 已取消时没有「已收尾」，成功率是 null（不是 0%）。"""
    _as_admin(client)
    owner = _user_id("root")
    _job(owner, status="superseded")
    _job(owner, status="cancelled")

    assert _stats(client)["success_rate"] is None


# ---------------------------------------------------------------------------
# AC3：卡死只由服务端判定（租约 / 更新时间）
# ---------------------------------------------------------------------------


def test_stuck_count_includes_a_running_row_whose_lease_has_expired(client):
    """「仍 running 但租约已过期」必须算进卡死——这正是恢复器收口之前的窗口。

    这条行 `updated_at` 是刚刚（看起来还在动），只有租约过期。若判据只看
    `updated_at`，卡死数会漏掉这一整类；而 #83 的恢复器尚未跑到时，库里就是这个样子。
    """
    _as_admin(client)
    owner = _user_id("root")
    now = utc_now()
    expired = _job(
        owner,
        status="running",
        updated_at=now,
        lease_token="tok-expired",
        lease_expire_at=(now - timedelta(seconds=30)).isoformat(),
    )
    # 对照：租约还活着、更新时间也新鲜 —— 不是卡死
    _job(
        owner,
        status="running",
        updated_at=now,
        lease_token="tok-alive",
        lease_expire_at=(now + timedelta(minutes=5)).isoformat(),
    )
    # 对照：没有任何租约（正常排队/收尾的行也没有），但更新停在 2 小时前 —— 卡死
    stale = _job(
        owner,
        status="running",
        created_at=now - timedelta(hours=3),
        updated_at=now - timedelta(hours=2),
    )

    stats = _stats(client)
    assert stats["stuck_count"] == 2, "过期租约 + 停更超阈值各算一条"

    listed = _jobs(client, stuck="true", status="running")
    ids = {item["id"] for item in listed["items"]}
    assert ids == {expired, stale}, "点进去的列表必须恰好是卡片上那几条"
    assert listed["total"] == stats["stuck_count"], "卡片与列表的卡死判据必须同一处"
    assert all(item["is_stuck"] for item in listed["items"])


def test_stuck_count_ignores_finished_rows_with_old_timestamps(client):
    """终态行即使「创建很久以前」也不算卡死：卡死只看还停在执行中的行。

    这条同时挡住一个常见错法：把「跑得久」当成「卡死」——收尾了的任务跑多久都正常。
    """
    _as_admin(client)
    owner = _user_id("root")
    now = utc_now()
    _job(
        owner,
        status="succeeded",
        created_at=now - timedelta(hours=3),
        updated_at=now - timedelta(hours=2),
    )
    _job(owner, status="pending", created_at=now - timedelta(hours=3),
         updated_at=now - timedelta(hours=2))

    assert _stats(client)["stuck_count"] == 0


# ---------------------------------------------------------------------------
# 另外两个口径：排队数含退避等待；平均耗时只看已收尾
# ---------------------------------------------------------------------------


def test_queue_count_includes_rows_waiting_for_retry_backoff(client):
    """「还没到期的退避行」也是排队中的任务，不能被算丢。

    它没在跑、也还没失败，只是要等一会儿；把它排除，排队数会在限流风暴里
    正好少掉最该被看见的那一批。
    """
    _as_admin(client)
    owner = _user_id("root")
    now = utc_now()
    _job(owner, status="pending")
    _job(owner, status="pending", next_run_at=(now + timedelta(seconds=30)).isoformat())

    assert _stats(client)["queue_count"] == 2


def test_average_duration_only_covers_settled_jobs(client):
    """平均耗时只算「成功 + 真失败」，取消 / 被取代不进（与成功率同一分母）。

    造 1 条 100 秒成功、1 条 200 秒失败、1 条 5000 秒取消：
    - 正确：150 秒；
    - 错误（把所有终态都算）：1766 秒 —— 把「用户点了一下就取消」拉成「系统很慢」。
    """
    _as_admin(client)
    owner = _user_id("root")
    _job(owner, status="succeeded", age_seconds=100)
    _job(owner, status="failed", age_seconds=200, error_code="LLM_UPSTREAM_ERROR")
    _job(owner, status="cancelled", age_seconds=5000)

    avg = _stats(client)["avg_duration_seconds"]
    assert 149 <= avg <= 151, f"取消的任务不能进平均耗时（得到 {avg}）"


# ---------------------------------------------------------------------------
# 门禁：统计接口沿用 require_admin（四个数字不是公开情报）
# ---------------------------------------------------------------------------


def test_stats_endpoint_requires_admin_for_the_four_numbers(client):
    """未登录 / 非管理员都拿不到这四个数——它们描述的是系统当前的真实压力。"""
    register(client, "bob")
    _as_admin(client, "root")
    login(client, "root")  # 先确认端点在管理员下可用
    assert client.get("/api/admin/stats").status_code == 200

    login(client, "bob")
    assert client.get("/api/admin/stats").status_code == 403
