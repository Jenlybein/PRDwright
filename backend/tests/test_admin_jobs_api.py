"""任务监控与手动取消（v4.5 T5-1）。

这些用例守的是四件「错了不报错、只是行为悄悄变了」的事：

1. **列表只能回该回的状态**：`status=running` 混进已完成任务，管理员会对着
   一条早已结束的记录反复点取消。
2. **卡死标注按 `updated_at` 算**：running 任务在流式写入时会刷新它，停住不动
   恰恰说明不再产出。用 `created_at` 算会把「跑得久但一直在写」的长任务误报。
3. **取消要同时落库**：只标记内存运行时的话，进程重启后列表仍显示「生成中」。
4. **取消已结束的任务不算失败**：返回 `cancelled=false` 而不是报错——
   「你点的时候它刚好说完了」不是错误。

另加两条门禁与一条审计：`require_admin` 是逐条挂的，漏一条端点就裸奔；
管理员的每次取消都必须留下可追责的记录。
"""

from __future__ import annotations

from datetime import timedelta

from conftest import login, register, users

from backend.core.clock import utc_now
from dbsupport import connect
from conftest import audit_log as AuditRepository
from backend.services import auth_service


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
    status: str = "running",
    kind: str = "prd",
    created_at=None,
    updated_at=None,
    error_code: str = "",
    tokens: int = 0,
) -> int:
    """直接插一条任务（绕开仓储的 running-only 建法，便于造各种状态）。"""
    created = created_at or utc_now()
    updated = updated_at or created
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO generation_jobs "
            "(owner_id, solution_id, kind, doc_kind, chat_stage, status, phase, "
            " draft_content, completed_sections, input_fingerprint, usage, "
            " max_provider_switches, error_code, error_message, "
            " prompt_tokens, completion_tokens, created_at, updated_at) "
            "VALUES (?, NULL, ?, '', '', ?, 'writing', '', '[]', NULL, NULL, 2, ?, '', ?, ?, ?, ?)",
            (
                owner_id,
                kind,
                status,
                error_code,
                tokens,
                tokens,
                created.isoformat(),
                updated.isoformat(),
            ),
        )
        job_id = int(cur.lastrowid or 0)
        conn.commit()
    return job_id


def _row(job_id: int) -> dict:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM generation_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        assert row is not None
        return dict(row)


# ---------------------------------------------------------------------------
# 列表与筛选
# ---------------------------------------------------------------------------


def test_running_list_only_returns_running(client, isolated_db):  # noqa: ARG001
    """`status=running` 不能混进已完成任务：那会让管理员对着死任务点取消。"""
    _as_admin(client)
    owner = _user_id("root")
    running = _job(owner, status="running")
    _job(owner, status="completed")

    response = client.get("/api/admin/jobs", params={"status": "running"})
    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body["items"]] == [running]
    assert body["total"] == 1


def test_failed_list_only_returns_failed(client, isolated_db):  # noqa: ARG001
    """「最近失败」标签页只回 failed。"""
    _as_admin(client)
    owner = _user_id("root")
    _job(owner, status="running")
    failed = _job(owner, status="failed", error_code="LLM_UPSTREAM_ERROR")

    response = client.get("/api/admin/jobs", params={"status": "failed"})
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [failed]


def test_stuck_is_computed_from_updated_at(client, isolated_db):  # noqa: ARG001
    """停更超过阈值才叫卡死；一直在刷新的长任务不算。

    这里刻意造两条 running：一条 3 小时没动、一条刚更新。
    若判据错用 `created_at`，两条都会是「跑了很久」，就区分不出来。
    """
    _as_admin(client)
    owner = _user_id("root")
    now = utc_now()
    stuck = _job(
        owner,
        status="running",
        created_at=now - timedelta(hours=4),
        updated_at=now - timedelta(hours=2),
    )
    fresh = _job(
        owner,
        status="running",
        created_at=now - timedelta(hours=4),
        updated_at=now,
    )

    response = client.get("/api/admin/jobs", params={"status": "running"})
    by_id = {item["id"]: item for item in response.json()["items"]}
    assert by_id[stuck]["is_stuck"] is True
    assert by_id[stuck]["stuck_minutes"] >= 120
    assert by_id[fresh]["is_stuck"] is False


def test_filter_by_username(client, isolated_db):  # noqa: ARG001
    """用户名模糊匹配：管理员通常只记得半个名字。"""
    _as_admin(client)
    register(client, "alice")
    alice = _user_id("alice")
    login(client, "root")  # register 会把登录态切成 alice，必须切回来

    _job(_user_id("root"), status="running")
    alice_job = _job(alice, status="running")

    response = client.get("/api/admin/jobs", params={"status": "running", "username": "ali"})
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [alice_job]


def test_filter_by_kind(client, isolated_db):  # noqa: ARG001
    """类型筛选用于「只看 PRD」这类排障。"""
    _as_admin(client)
    owner = _user_id("root")
    _job(owner, status="running", kind="prd")
    prompts = _job(owner, status="running", kind="prompts")

    response = client.get("/api/admin/jobs", params={"status": "running", "kind": "prompts"})
    assert [item["id"] for item in response.json()["items"]] == [prompts]


# ---------------------------------------------------------------------------
# 取消
# ---------------------------------------------------------------------------


def test_admin_cancel_marks_failed_and_cancelled(client, isolated_db):  # noqa: ARG001
    """取消必须**落库**成 failed + CANCELLED，重启后不能还显示「生成中」。"""
    _as_admin(client)
    owner = _user_id("root")
    job_id = _job(owner, status="running")

    response = client.post(f"/api/admin/jobs/{job_id}/cancel")
    assert response.status_code == 200
    assert response.json()["cancelled"] is True

    row = _row(job_id)
    assert row["status"] == "failed"
    assert row["error_code"] == "CANCELLED"
    # 措辞要能区分「管理员清理」与「用户点了停止」
    assert "管理员" in row["error_message"]


def test_cancel_finished_job_returns_false(client, isolated_db):  # noqa: ARG001
    """任务已经自然结束：返回 false，而不是报一个红错。

    报错会让管理员以为操作失败而反复点击；false 配上「已经结束了」的提示
    才符合事实。
    """
    _as_admin(client)
    owner = _user_id("root")
    job_id = _job(owner, status="completed")

    response = client.post(f"/api/admin/jobs/{job_id}/cancel")
    assert response.status_code == 200
    assert response.json()["cancelled"] is False
    # 终态不能被改坏
    assert _row(job_id)["status"] == "completed"


def test_cancel_unknown_job_is_404(client, isolated_db):  # noqa: ARG001
    """不存在的任务不能静默成功，否则管理员以为已经掐掉了。"""
    _as_admin(client)
    response = client.post("/api/admin/jobs/999999/cancel")
    assert response.status_code == 404


def test_cancel_stuck_only_touches_stuck(client, isolated_db):  # noqa: ARG001
    """批量取消只动卡死的，正常在跑的一个都不碰——误伤会把正文断在半截。"""
    _as_admin(client)
    owner = _user_id("root")
    now = utc_now()
    stuck_a = _job(owner, status="running", created_at=now - timedelta(hours=3), updated_at=now - timedelta(hours=2))
    stuck_b = _job(owner, status="running", created_at=now - timedelta(hours=3), updated_at=now - timedelta(hours=1))
    fresh = _job(owner, status="running", created_at=now, updated_at=now)

    response = client.post("/api/admin/jobs/cancel-stuck")
    assert response.status_code == 200
    assert response.json()["cancelled"] == 2

    assert _row(stuck_a)["status"] == "failed"
    assert _row(stuck_b)["status"] == "failed"
    assert _row(fresh)["status"] == "running"


def test_cancel_is_audited(client, isolated_db):  # noqa: ARG001
    """管理员的取消必须留痕：事后要能回答「这个任务是谁掐的」。"""
    _as_admin(client)
    owner = _user_id("root")
    job_id = _job(owner, status="running")

    client.post(f"/api/admin/jobs/{job_id}/cancel")

    rows = AuditRepository.list_recent(limit=20)
    hits = [row for row in rows if row.get("action") == "job.cancel"]
    assert hits, "没有记下 job.cancel 审计"
    assert hits[0]["target"] == str(job_id)


# ---------------------------------------------------------------------------
# 门禁
# ---------------------------------------------------------------------------


def test_non_admin_cannot_list_or_cancel(client, isolated_db):  # noqa: ARG001
    """普通用户既看不到任务列表，也不能取消别人的任务。"""
    _as_admin(client)
    owner = _user_id("root")
    job_id = _job(owner, status="running")

    register(client, "dave")  # 注册即登录，且不是管理员
    assert client.get("/api/admin/jobs").status_code == 403
    assert client.post(f"/api/admin/jobs/{job_id}/cancel").status_code == 403
    # 别人的任务没被碰
    assert _row(job_id)["status"] == "running"
