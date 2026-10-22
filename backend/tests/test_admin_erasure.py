"""彻底删除账号（v4.5 T5-4）：删干净、留该留的、以及删前的告知。

这一组用例守的是四件「删错了看不出来」的事：

1. **删不干净**：方案、文档、正文版本、对话、任务都靠外键级联，漏一张表就是
   「用户以为删了、数据还在」。级联在库里，所以这里直接查每张表的行数。
2. **删过头**：`admin_audit_log` 与 `app_logs` 按设计**保留**。审计是问责底线
   （账号没了更要有据可查），日志是运维记录、不是用户业务数据。测「留下」和测
   「删掉」同样重要。
3. **同名账号继承旧账**：登录失败计数按**用户名**存（登录时还没有 user_id），
   不随账号级联。不清的话，删掉一个失败过 6 次的 alice、再注册一个同名 alice，
   新账号一上来就被延时 2 秒。
4. **删前不停任务**：任务与连接解耦，删号不会让在途协程自己停下——它还会继续
   烧 token，直到跑完才发现没有归属可写。
"""

from __future__ import annotations

import pytest
from conftest import login, register

from backend.api import auth as auth_api
from backend.core.db import connect, utc_now
from backend.repositories.account_repo import AccountDataRepository
from backend.repositories.chat_turn_repo import ChatTurnRepository
from backend.services import auth_service, document_store, job_service


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    """把路由层的真实等待换成记录器（否则制造「失败 6 次」要真等 2 秒）。"""
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(auth_api, "_sleep", fake_sleep)
    return recorded


def _as_admin(client, username: str = "root") -> None:
    register(client, username)
    auth_service.set_admin(username, True)
    assert client.get("/api/admin/me").status_code == 200


def _user_id(username: str) -> int:
    row = auth_service.UserRepository.get_by_username(username)
    assert row is not None
    return int(row["id"])


def _new_solution(client, title: str = "支付方案") -> int:
    response = client.post(
        "/api/solutions",
        json={"title": title, "mode": "structured", "stage": "entry", "data": "{}"},
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _job(owner_id: int, *, status: str = "completed", prompt_tokens: int = 10) -> int:
    """直接插一条任务记录，返回其 id。"""
    moment = utc_now().isoformat()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO generation_jobs "
            "(owner_id, solution_id, kind, doc_kind, chat_stage, status, phase, "
            " draft_content, completed_sections, input_fingerprint, usage, "
            " max_provider_switches, error_code, error_message, "
            " prompt_tokens, completion_tokens, created_at, updated_at) "
            "VALUES (?, NULL, 'prd', '', '', ?, '', '', '[]', NULL, NULL, 2, '', '', ?, 7, ?, ?)",
            (owner_id, status, prompt_tokens, moment, moment),
        )
        conn.commit()
        return int(cur.lastrowid or 0)


def _seed_alice(client) -> int:
    """造一个「有方案 / 两版正文 / 对话 / 任务」的账号，返回其 user_id。"""
    register(client, "alice")
    solution_id = _new_solution(client, "支付方案")
    owner_id = _user_id("alice")
    document_store.commit_generated(owner_id, solution_id, "prd", "# 第一版")
    document_store.commit_generated(owner_id, solution_id, "prd", "# 第二版")
    document_store.commit_generated(owner_id, solution_id, "prompts", "第一步：建表")
    ChatTurnRepository.create(
        owner_id, solution_id, stage="chat", role="user", content="要支持退款吗？"
    )
    _job(owner_id)
    return owner_id


# ---------------------------------------------------------------------------
# 删除预览
# ---------------------------------------------------------------------------


def test_deletion_preview_reports_what_will_be_deleted(client) -> None:
    """预览的每个数字都要与库里对得上——它是「点删除之前唯一的信息来源」。"""
    owner_id = _seed_alice(client)
    _as_admin(client)

    data = client.get(f"/api/admin/users/{owner_id}/deletion-preview").json()

    assert data["username"] == "alice"
    assert data["solutions"] == 1
    assert data["documents"] == 2  # prd + prompts
    assert data["document_versions"] == 3  # prd 两版 + prompts 一版
    assert data["chat_turns"] == 1
    assert data["jobs"] == 1
    assert data["running_jobs"] == 0
    assert data["sessions"] == 1  # 注册即登录
    assert data["completion_tokens"] == 7


def test_deletion_preview_counts_running_jobs(client) -> None:
    """在飞任务要单独报出来：它们会在删除前被取消。"""
    register(client, "alice")
    owner_id = _user_id("alice")
    _job(owner_id, status="running")
    _as_admin(client)

    data = client.get(f"/api/admin/users/{owner_id}/deletion-preview").json()

    assert data["jobs"] == 1
    assert data["running_jobs"] == 1


def test_deletion_preview_requires_admin(client) -> None:
    register(client, "alice")
    register(client, "bob")

    assert client.get(f"/api/admin/users/{_user_id('alice')}/deletion-preview").status_code == 403


def test_deletion_preview_unknown_user_returns_404(client) -> None:
    _as_admin(client)

    assert client.get("/api/admin/users/9999/deletion-preview").status_code == 404


# ---------------------------------------------------------------------------
# 删干净
# ---------------------------------------------------------------------------


def test_delete_removes_all_business_data(client) -> None:
    """方案 / 文档 / 版本 / 对话 / 任务 / 会话，删除后一行都不剩。"""
    owner_id = _seed_alice(client)
    _as_admin(client)

    assert client.delete(f"/api/admin/users/{owner_id}").status_code == 200

    counts = AccountDataRepository.counts(owner_id)
    assert counts["solutions"] == 0
    assert counts["documents"] == 0
    assert counts["document_versions"] == 0
    assert counts["chat_turns"] == 0
    assert counts["jobs"] == 0
    assert counts["sessions"] == 0


def test_delete_cancels_running_jobs_before_removing(client) -> None:
    """在飞任务先被取消（审计里能看到条数），而不是「跟着账号一起消失」。"""
    register(client, "alice")
    owner_id = _user_id("alice")
    _job(owner_id, status="running")
    _job(owner_id, status="running")
    _as_admin(client)

    assert client.delete(f"/api/admin/users/{owner_id}").status_code == 200

    with connect() as conn:
        row = conn.execute(
            "SELECT detail FROM admin_audit_log WHERE action = 'user.delete' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert "取消在飞任务 2 个" in str(row["detail"])


def test_delete_user_without_any_data_works(client) -> None:
    """刚注册、什么都没做的账号也能删（不能因为「没有数据」就报错）。"""
    register(client, "alice")
    owner_id = _user_id("alice")
    _as_admin(client)

    assert client.delete(f"/api/admin/users/{owner_id}").status_code == 200
    assert auth_service.UserRepository.get_by_id(owner_id) is None


def test_deleted_user_can_no_longer_login(client) -> None:
    """删号必然等于强制下线：会话被级联删掉，原密码也登不进来。"""
    register(client, "alice")
    owner_id = _user_id("alice")
    _as_admin(client)
    client.delete(f"/api/admin/users/{owner_id}")

    assert login(client, "alice").status_code == 401


# ---------------------------------------------------------------------------
# 留该留的
# ---------------------------------------------------------------------------


def test_delete_keeps_audit_records(client) -> None:
    """审计必须留下：账号没了，「他做过什么」更要能查。"""
    register(client, "alice")
    owner_id = _user_id("alice")
    _as_admin(client)
    # 三个字段都是必填的，少传会 422 —— 那样后面「审计里没有 user.quota」的断言
    # 会因为「操作根本没发生」而失败，看起来像删号把审计弄丢了。所以这里断言状态码。
    quota = client.patch(
        f"/api/admin/users/{owner_id}/quota",
        json={"daily_task_limit": 5, "daily_token_limit": -1, "max_running": -1},
    )
    assert quota.status_code == 200, quota.text

    client.delete(f"/api/admin/users/{owner_id}")

    with connect() as conn:
        # ⚠️ 不同动作的 `target` 口径并不统一：额度记的是**用户 id**，删号记的是**用户名**。
        # 只按用户名查会漏掉额度那条，看起来像「删号把审计弄丢了」——其实是查错了。
        rows = conn.execute(
            "SELECT action FROM admin_audit_log WHERE target IN (?, ?) ORDER BY id",
            ("alice", str(owner_id)),
        ).fetchall()
    actions = [str(row["action"]) for row in rows]
    assert "user.quota" in actions, "删号前对该账号的操作记录不该消失"
    assert actions[-1] == "user.delete"


def test_delete_clears_login_failures_for_that_username(client) -> None:
    """同名账号不继承旧账：重建的 alice 不该一上来就被延时。

    这条守的是一个真实的坑：`login_failures` 按**用户名**记（登录时还没有 user_id），
    所以它不参与级联删除。不清的话，删掉一个失败过 6 次的 alice、再注册一个同名
    alice，新账号第一次登录就要先等 2 秒——它继承了一笔没欠过的账。
    """
    register(client, "alice")
    owner_id = _user_id("alice")
    _as_admin(client)
    # 先制造 6 次失败（默认阈值 5，第 6 次起开始延时）
    for _ in range(6):
        assert login(client, "alice", "wrong-pass-1").status_code == 401
    assert auth_service.login_delay("alice") > 0, "先确认失败计数确实累加到阈值以上"

    client.delete(f"/api/admin/users/{owner_id}")

    with connect() as conn:
        left = conn.execute(
            "SELECT COUNT(*) AS n FROM login_failures WHERE username = 'alice'"
        ).fetchone()
    assert int(left["n"]) == 0, "删号必须清掉该用户名的失败记录"

    # 重建同名账号：第一次登录不该有任何延时
    assert register(client, "alice").status_code == 200
    assert auth_service.login_delay("alice") == 0.0


# ---------------------------------------------------------------------------
# 停任务（服务层）
# ---------------------------------------------------------------------------


def test_cancel_for_user_marks_db_rows_and_memory_runtime(client) -> None:
    """删除前取消任务：库里的行落 CANCELLED，内存里的运行时也要被打标记。

    只改库不改内存的话，在途协程不知道自己被取消，收尾时照样去写文档——
    「删了账号，正文却还在往里写」，不报错、只是数据对不上。
    """
    register(client, "alice")
    owner_id = _user_id("alice")
    job_id = _job(owner_id, status="running")

    runtime = job_service.JobRuntime(
        job_id=job_id, owner_id=owner_id, solution_id=None, kind="prd"
    )
    job_service._RUNTIMES[job_id] = runtime  # noqa: SLF001 - 直接摆一支在途运行时
    try:
        affected = job_service.cancel_for_user(owner_id)

        assert affected == 1
        assert runtime.cancelled is True
        with connect() as conn:
            row = conn.execute(
                "SELECT status, error_code FROM generation_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        assert row["status"] == "failed"
        assert row["error_code"] == "CANCELLED"
    finally:
        job_service._RUNTIMES.pop(job_id, None)  # noqa: SLF001


def test_cancel_for_user_leaves_finished_jobs_alone(client) -> None:
    """已完成的任务不受影响：只动 running。"""
    register(client, "alice")
    owner_id = _user_id("alice")
    done_id = _job(owner_id, status="completed")

    assert job_service.cancel_for_user(owner_id) == 0

    with connect() as conn:
        row = conn.execute(
            "SELECT status, error_code FROM generation_jobs WHERE id = ?", (done_id,)
        ).fetchone()
    assert row["status"] == "completed"
    assert row["error_code"] == ""
