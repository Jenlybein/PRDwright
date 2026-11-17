"""七态扩宽 + 取消中（#84）：状态取值、取消两阶段、恢复兜底、对外契约。

## 这张票回答什么问题

三态（`running` / `completed` / `failed`）里，「排队中」与「执行中」共用 `running`，
被取消与被顶替都靠 `error_code=CANCELLED/SUPERSEDED` 猜——同一列 `status` 说不了
「现在算什么」，前端也就没法让用户看见「正在取消」。本票把它扩成七个值：

| 标识符 | 中文 | 还能不能变 |
|---|---|---|
| `pending` | 排队中 | 可被领走，也可被直接取消 |
| `running` | 执行中 | 由它自己收尾，或由恢复器接手 |
| `cancelling` | 取消中 | 由执行者确认，或由恢复器兜底 |
| `succeeded` | 已定稿 | 终态 |
| `failed` | 失败 | 终态 |
| `superseded` | 被取代 | 终态（系统让位，不是用户意图） |
| `cancelled` | 已取消 | 终态（用户取消 / 方案被删） |

## 接缝（断言只架在这四处）

1. **`job_state.transition`**（#79 起唯一的转换入口）：取消请求、取消确认、
   恢复兜底、顶替、回排队全部经它；判据仍是**影响行数**。
2. **`job_service.cancel_job`**（用户按停止）与 `run_ticket_in_flight`（主接缝）：
   排队中取消立刻落已取消、执行中取消先进取消中、执行者在下一个动作点收口。
3. **`job_recovery.recover_once`**（#83 的恢复器）：执行者卡住不退出时，由它把
   「取消中 + 租约过期」收成已取消。
4. **管理端统计接口**（`GET /api/admin/stats/daily`）与任务列表接口：
   被取代与已取消在统计里是两件事，成功率排除这两者，口径不再按错误码猜。

## 不保留兼容映射层（AC4）

对外 `status` 直接给七值；`CANCELLED` / `SUPERSEDED` **不再写进 `error_code`**——
它们曾经承担状态语义，现在由 `status` 承担。下面的静态守卫把这件事钉住：
新的状态分支里不许再出现「按错误码猜状态」。
"""

from __future__ import annotations

import ast
import logging
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import job_claim, job_create, job_create_running, job_get, register, users
from dbsupport import connect

from backend.core.clock import utc_now
from backend.core.engine import session_scope
from backend.repositories.generation_job_repo import (
    ACTIVE_STATUSES,
    STATUS_CANCELLED,
    STATUS_CANCELLING,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    STATUS_SUPERSEDED,
    TERMINAL_STATUSES,
    GenerationJobRepository,
)
from backend.services import job_claimer, job_recovery, job_service, job_state

# 取消请求落进 `error_message` 的那句话（用户按停止）。文案由调用方给，不随状态变。
USER_CANCEL_MESSAGE = "用户停止了生成"


def _alice_id() -> int:
    user = users.get_by_username("alice")
    assert user is not None
    return int(user["id"])


@pytest.fixture
def alice(client):
    register(client)
    return _alice_id()


def _payload(draft: str = "", usage: str = '{"total_calls": 1}') -> job_state.FinishPayload:
    return job_state.FinishPayload(
        draft_content=draft,
        completed_sections=[],
        usage=usage,
        pending_items=[],
        prompt_tokens=3,
        completion_tokens=5,
        duration_ms=42,
    )


def _cancel_request(owner_id: int, job_id: int, message: str = USER_CANCEL_MESSAGE):
    """发一次「请求取消」（等价于用户按停止 / 删方案）。"""
    return job_state.transition(
        job_state.JobEvent.REQUEST_CANCEL,
        job_state.job_scope(owner_id, job_id),
        error_message=message,
    )


def _discards(caplog) -> list:
    return [
        record
        for record in caplog.records
        if getattr(record, "event", "") == "job.transition_discarded"
    ]


# ===========================================================================
# 一、AC1：排队中取消立刻落已取消；执行中取消先进入取消中
# ===========================================================================


def test_cancelling_a_queued_job_lands_cancelled_immediately(alice):
    """排队中的任务没有执行者，取消**不需要等谁确认**：一步落已取消。

    这一条同时钉住「排队中」只有一种表示：新行建出来就是 `pending`、租约为空。
    """
    job_id = job_create(alice, "prd", None)
    row = job_get(alice, job_id)
    assert row["status"] == STATUS_PENDING
    assert (row["lease_owner"], row["lease_token"], row["lease_expire_at"]) == ("", "", "")

    assert job_service.cancel_job(alice, job_id) is True

    row = job_get(alice, job_id)
    assert row["status"] == STATUS_CANCELLED
    assert row["error_code"] == "", "`CANCELLED` 不再承担状态语义（AC4）"
    assert row["error_message"] == USER_CANCEL_MESSAGE
    # 已经终态：再点一次「停止」就是「你点的时候它刚好结束了」，不是错误
    assert job_service.cancel_job(alice, job_id) is False


def test_cancelling_a_running_job_enters_cancelling_and_keeps_the_lease(alice):
    """执行中取消只是**请求**：先进入取消中，租约不清（执行者还要用它收口）。

    AC1 的后半句与「取消请求不清租约」在这里合并验证：行变成 `cancelling`、
    凭证还是本轮那个、没有任何错误码。
    """
    job_id, lease = job_create_running(alice, "prd")
    row = job_get(alice, job_id)
    assert row["status"] == STATUS_RUNNING

    assert job_service.cancel_job(alice, job_id) is True

    row = job_get(alice, job_id)
    assert row["status"] == STATUS_CANCELLING, "执行中取消先进入取消中，不许一步落终态"
    assert row["lease_token"] == lease.token, "取消请求不清租约（执行者还要用它继续收口）"
    assert row["error_code"] == ""
    assert row["error_message"] == USER_CANCEL_MESSAGE


def test_second_cancel_request_on_a_cancelling_job_stays_cancelling(alice):
    """重复请求取消是幂等的：行仍停在取消中（文案可以刷新），不会跳成终态。"""
    job_id, _lease = job_create_running(alice, "prd")
    assert _cancel_request(alice, job_id).applied

    again = _cancel_request(alice, job_id, "管理员手动停止了任务")

    assert again.applied
    row = job_get(alice, job_id)
    assert row["status"] == STATUS_CANCELLING
    assert row["error_message"] == "管理员手动停止了任务"


# ===========================================================================
# 二、AC2：执行者在下一个动作点看到标记即退出，并落已取消
# ===========================================================================


def test_executor_confirms_the_cancel_at_the_next_action_point(client, alice):
    """主接缝：执行中 → 用户取消（取消中）→ 下一个事件点执行者退出 → 已取消。

    三件事一起钉住 AC2：

    1. 取消请求当刻，库里是**取消中**（租约还在，执行者仍在跑）；
    2. 执行者在下一个事件到达时停（`done` 未被应用，正文里没有它）；
    3. 由执行者自己的收尾落**已取消**（不是失败、不是已定稿），半截正文与用量
       仍然记下来（#79 的「取消之后收尾补一次载荷」在七态里的落点）。
    """
    import asyncio

    from backend.core.contracts import StreamEvent
    from ticket_support import run_ticket_async

    produced = asyncio.Event()
    release = asyncio.Event()
    seen: dict[str, object] = {}

    async def source(kind, request):
        yield StreamEvent(type="chunk", content="半截正文", data={})
        produced.set()
        # 执行者挂在这里：外部取消它，然后放开闸门 = 下一个动作点
        await release.wait()
        yield StreamEvent(type="done", content="", data={"reply": "# 不该被应用的正文"})

    async def cancel_while_running(job_id: int) -> None:
        await asyncio.wait_for(produced.wait(), timeout=5)
        before = job_service.get_job(alice, job_id)
        seen["before"] = before["status"]
        assert job_service.cancel_job(alice, job_id) is True
        after = job_service.get_job(alice, job_id)
        seen["after"] = after["status"]
        assert after["lease_token"] != "", "执行者还在收口，租约此刻不能被清"
        release.set()

    async def scenario():
        return await run_ticket_async(
            owner_id=alice,
            solution_id=None,
            kind="prd",
            request={},
            event_stream_source=source,
            before_open=cancel_while_running,
        )

    registered, events = asyncio.run(scenario())

    row = job_get(alice, registered.job_id)
    assert seen["before"] == STATUS_RUNNING
    assert seen["after"] == STATUS_CANCELLING
    assert row["status"] == STATUS_CANCELLED
    assert row["error_code"] == "", "已取消不是一个错误码，是一个状态"
    assert row["error_message"] == USER_CANCEL_MESSAGE
    assert (row["lease_owner"], row["lease_token"], row["lease_expire_at"]) == ("", "", ""), (
        "取消确认要清空租约三列"
    )
    # 半截正文与用量记下来了；`done` 没有被应用（执行者看到取消标记即停）
    assert "半截正文" in row["draft_content"]
    assert "不该被应用的正文" not in row["draft_content"]
    assert row["usage"] is not None
    # 订阅者看到的是回执而不是失败：没有 `error` 事件
    assert not [event for event in events if event.type == "error"], (
        "用户自己取消不是一次生成失败，不该补发 error 事件"
    )
    assert events[-1].type == "run_summary"
    assert events[-1].data["status"] == STATUS_CANCELLED


def test_renewal_on_a_cancelling_job_is_a_cancel_signal_not_a_lease_loss(alice):
    """动作点续租看到「取消中」：抛取消信号（而不是「执行权丢了」）。

    两者的处置相反：执行权丢了要**什么都不写**（别人接手了）；取消中则要由
    这位执行者把行确认成已取消（它是唯一还有资格写这一行的人）。
    """
    job_id, lease = job_create_running(alice, "prd")
    assert _cancel_request(alice, job_id).applied

    with pytest.raises(job_claimer.CancelRequestedError):
        job_claimer.renew(lease, now=utc_now())


def test_renewal_after_takeover_still_means_lease_lost(alice):
    """反证：行被恢复器回排队之后，续租仍是「执行权丢了」——两种失败不许混成一种。"""
    job_id, lease = job_create_running(alice, "prd")
    expired = utc_now() + timedelta(seconds=lease_seconds() + 30)
    with session_scope() as session:
        repo = GenerationJobRepository(session)
        assert repo.requeue_lease(
            alice,
            job_id,
            lease_token=lease.token,
            lease_expire_at=lease.expire_at,
            now_text=expired.isoformat(),
        ) == 1

    with pytest.raises(job_claimer.LeaseLostError):
        job_claimer.renew(lease, now=expired)


def lease_seconds() -> int:
    return int(job_claimer.DEFAULT_LEASE_SECONDS)


# ===========================================================================
# 三、AC3：执行者卡住不退出时由恢复器兜底落已取消
# ===========================================================================


def test_recovery_settles_a_stuck_cancelling_job_as_cancelled(alice):
    """取消中 + 租约过期：执行者没来确认，恢复器替它落已取消（AC3）。

    判据同时钉住三件事：状态落 `cancelled`（不是 `failed`）、错误码为空
    （不是 `LEASE_EXPIRED`）、账本记一笔 `cancelled` 结论、租约交还。
    """
    job_id, lease = job_create_running(alice, "prd")
    assert _cancel_request(alice, job_id).applied
    assert job_get(alice, job_id)["status"] == STATUS_CANCELLING

    expired_at = utc_now() + timedelta(seconds=lease_seconds() + 30)
    report = job_recovery.recover_once(10, now=expired_at)

    assert report.scanned == 1
    assert report.cancelled == 1, "取消中的行由恢复器收口，不是判失败"
    assert report.failed == 0
    row = job_get(alice, job_id)
    assert row["status"] == STATUS_CANCELLED
    assert row["error_code"] == ""
    assert (row["lease_owner"], row["lease_token"], row["lease_expire_at"]) == ("", "", "")
    attempts = _attempts(alice, job_id)
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "cancelled"
    assert attempts[0]["error_code"] == ""


def test_recovery_does_not_touch_a_cancelling_job_whose_lease_is_still_alive(alice):
    """租约还没过期：执行者可能正正常收口，恢复器不许抢——候选集不含它。"""
    job_id, _lease = job_create_running(alice, "prd")
    assert _cancel_request(alice, job_id).applied

    report = job_recovery.recover_once(10, now=utc_now())

    assert report.scanned == 0
    assert job_get(alice, job_id)["status"] == STATUS_CANCELLING


def test_startup_recovery_cancels_a_cancelling_row_instead_of_failing_it(alice):
    """启动档：取消中的残留行按**已取消**收口，不许被记成一次失败。"""
    job_id, _lease = job_create_running(alice, "prd")
    assert _cancel_request(alice, job_id).applied

    job_service.reap_orphans()

    row = job_get(alice, job_id)
    assert row["status"] == STATUS_CANCELLED
    assert row["error_code"] == ""


def test_startup_recovery_settles_a_queued_row_as_interrupted(alice):
    """启动档：排队中但从未被领走的行没有执行者会来领（本票不做调度循环），
    按「服务重启中断」判失败——不许留在永远排队中。"""
    job_id = job_create(alice, "prd", None)
    job_service.reap_orphans()

    row = job_get(alice, job_id)
    assert row["status"] == STATUS_FAILED
    assert row["error_code"] == "INTERNAL_ERROR"
    assert row["error_message"] == job_recovery.MESSAGE_RESTART_INTERRUPTED


def _attempts(owner_id: int, job_id: int) -> list[dict]:
    with session_scope() as db:
        from backend.repositories.job_attempt_repo import JobAttemptRepository

        return [row.model_dump() for row in JobAttemptRepository(db).list_by_job(owner_id, job_id)]


# ===========================================================================
# 四、AC4：七个取值是唯一的表示；不许按错误码猜状态
# ===========================================================================


def test_status_values_are_exactly_the_seven_and_partitioned():
    """七个取值是全部；「在途」与「终态」两套集合互斥且覆盖全部（唯一真源）。"""
    assert {
        STATUS_PENDING,
        STATUS_RUNNING,
        STATUS_CANCELLING,
        STATUS_SUCCEEDED,
        STATUS_FAILED,
        STATUS_SUPERSEDED,
        STATUS_CANCELLED,
    } == {
        "pending",
        "running",
        "cancelling",
        "succeeded",
        "failed",
        "superseded",
        "cancelled",
    }
    assert set(ACTIVE_STATUSES) | set(TERMINAL_STATUSES) == {
        "pending",
        "running",
        "cancelling",
        "succeeded",
        "failed",
        "superseded",
        "cancelled",
    }
    assert set(ACTIVE_STATUSES) & set(TERMINAL_STATUSES) == set()
    assert ACTIVE_STATUSES == (STATUS_PENDING, STATUS_RUNNING, STATUS_CANCELLING)


def test_successful_run_reports_succeeded_on_every_public_surface(client, alice):
    """完成的任务对外只叫 `succeeded`（不再有 `completed`）：库行、快照、回执一致。"""
    from ticket_support import run_ticket_events

    registered, events = run_ticket_events(
        alice,
        None,
        "prd",
        {},
        reply="# 正文",
    )

    row = job_get(alice, registered.job_id)
    assert row["status"] == STATUS_SUCCEEDED
    summary = [event for event in events if event.type == "run_summary"][-1]
    assert summary.data["status"] == STATUS_SUCCEEDED
    body = client.get(f"/api/jobs/{registered.job_id}").json()
    assert body["status"] == STATUS_SUCCEEDED


def test_superseded_and_cancelled_are_two_distinct_states_with_empty_codes(alice):
    """被取代与已取消是两个状态值；两者的 `error_code` 都是空串。

    这条直说 AC4 / AC5：「CANCELLED」「SUPERSEDED」不再承担状态语义，
    界面与统计按 `status` 区分，而不是按错误码猜。
    """
    queued = job_create(alice, "prd", None)
    assert job_state.transition(
        job_state.JobEvent.SUPERSEDE, job_state.supersede_scope(alice, None, "prd")
    ).applied
    superseded = job_get(alice, queued)
    assert superseded["status"] == STATUS_SUPERSEDED
    assert superseded["error_code"] == ""
    assert superseded["error_message"] == "被新任务取代"

    cancelled_id = job_create(alice, "prompts", None)
    assert job_service.cancel_job(alice, cancelled_id) is True
    cancelled = job_get(alice, cancelled_id)
    assert cancelled["status"] == STATUS_CANCELLED
    assert cancelled["error_code"] == ""

    assert superseded["status"] != cancelled["status"]
    assert superseded["id"] != cancelled["id"]


def test_terminal_transitions_clear_all_three_lease_columns(alice):
    """终态一律清空租约三列：已定稿 / 失败 / 被取代 / 已取消四条路都要清。"""
    succeeded_id, lease = job_create_running(alice, "prd")
    assert job_state.transition(
        job_state.JobEvent.SUCCEED,
        job_state.job_scope(alice, succeeded_id),
        payload=_payload("正文"),
        lease_token=lease.token,
    ).applied
    assert job_get(alice, succeeded_id)["status"] == STATUS_SUCCEEDED

    failed_id, lease = job_create_running(alice, "prompts")
    assert job_state.transition(
        job_state.JobEvent.FAIL,
        job_state.job_scope(alice, failed_id),
        payload=_payload("半截"),
        error_code="LLM_UPSTREAM_ERROR",
        error_message="上游中断",
        lease_token=lease.token,
    ).applied

    superseded_id, _lease = job_create_running(alice, "api_docs")
    assert job_state.transition(
        job_state.JobEvent.SUPERSEDE,
        job_state.supersede_scope(alice, None, "api_docs"),
    ).applied

    cancelling_id, lease = job_create_running(alice, "review")
    assert _cancel_request(alice, cancelling_id).applied
    assert job_state.transition(
        job_state.JobEvent.CONFIRM_CANCEL,
        job_state.job_scope(alice, cancelling_id),
        payload=_payload("半截"),
        error_message=USER_CANCEL_MESSAGE,
        lease_token=lease.token,
    ).applied

    for job_id, expected in (
        (succeeded_id, STATUS_SUCCEEDED),
        (failed_id, STATUS_FAILED),
        (superseded_id, STATUS_SUPERSEDED),
        (cancelling_id, STATUS_CANCELLED),
    ):
        row = job_get(alice, job_id)
        assert row["status"] == expected
        assert (row["lease_owner"], row["lease_token"], row["lease_expire_at"]) == ("", "", ""), (
            f"{expected} 没有清空租约三列"
        )


def test_admin_state_branches_do_not_guess_status_from_error_codes():
    """静态守卫：管理端不再按错误码猜状态（AC5 的「不许用兼容映射层」）。

    `error_code` 是失败原因（给人看的那句「为什么没跑成」），不是状态。
    如果它又出现在「哪些失败不算真失败」的判据里，就说明状态语义仍有两处真源。
    """
    admin_repo = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "backend"
        / "repositories"
        / "admin_repo.py"
    ).read_text(encoding="utf-8")
    # 只盯「当成错误码用的字面量」，不误伤 `STATUS_CANCELLED` / `STATUS_SUPERSEDED` 常量名。
    assert '"CANCELLED"' not in admin_repo
    assert '"SUPERSEDED"' not in admin_repo
    assert "'CANCELLED'" not in admin_repo
    assert "'SUPERSEDED'" not in admin_repo
    assert "error_code.in_" not in admin_repo
    assert "error_code.notin_" not in admin_repo

    service_impl = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "backend"
        / "services"
        / "job_service_impl.py"
    ).read_text(encoding="utf-8")
    assert "ABORT_CODES" not in service_impl, "取消/顶替不再是错误码，订阅侧按状态判"


def test_state_machine_module_has_no_status_carrying_error_codes():
    """静态守卫：状态机里不再有 `CODE_CANCELLED` / `CODE_SUPERSEDED` 这类字面量。

    唯一保留的「不是错误、但会写进 error_code」的值是 `LEASE_EXPIRED`——它是
    **失败的原因**（租约到期），不是状态。
    """
    job_state_source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "backend"
        / "services"
        / "job_state.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(job_state_source)
    assigned = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert "CODE_CANCELLED" not in assigned
    assert "CODE_SUPERSEDED" not in assigned
    assert '"CANCELLED"' not in job_state_source
    assert '"SUPERSEDED"' not in job_state_source


# ===========================================================================
# 五、「排队中」只有一种表示：`pending`，不再靠「running + 没有租约」
# ===========================================================================


def test_requeue_returns_the_row_to_pending_not_running_without_lease(alice):
    """回排队 = 回到 `pending`（单一表示）：清租约、写退避，抢占只认 `pending`。"""
    job_id, lease = job_create_running(alice, "prd")
    moment = utc_now()

    with session_scope() as session:
        row = GenerationJobRepository(session).get(alice, job_id)
        assert row is not None
        fence = job_state.LeaseFence(token=row.lease_token, expire_at=row.lease_expire_at)

    result = job_state.transition(
        job_state.JobEvent.REQUEUE,
        job_state.job_scope(alice, job_id),
        error_code=job_state.CODE_LEASE_EXPIRED,
        error_message="执行者进程已消失，租约已到期",
        lease_fence=fence,
        now_text=moment.isoformat(),
    )

    assert result.applied
    row = job_get(alice, job_id)
    assert row["status"] == STATUS_PENDING, "回排队要落回排队中这个值，而不是 running + 空租约"
    assert (row["lease_owner"], row["lease_token"], row["lease_expire_at"]) == ("", "", "")
    assert row["next_run_at"] != ""


def test_claim_refuses_an_executing_row_even_when_its_lease_expired(alice):
    """过期租约的「执行中」行不再能被直接抢：接手路径只有恢复器（#83）。

    七态之后「谁在跑」由 `running` 表达，`pending` 才领得走。直接抢一条过期
    `running` 行等于绕过账本与恢复器的去向判定，那会重新造出第二个入口。
    """
    job_id, lease = job_create_running(alice, "prd")
    later = utc_now() + timedelta(seconds=lease_seconds() + 30)

    assert job_claimer.claim(alice, job_id, now=later) is None
    assert job_get(alice, job_id)["status"] == STATUS_RUNNING
    assert job_get(alice, job_id)["lease_token"] == lease.token


def test_a_queued_row_is_claimable_exactly_once_per_claim(alice):
    """抢占把 `pending` 改成 `running`；第二次抢占（有效期内的租约）改到 0 行。"""
    job_id = job_create(alice, "prd", None)
    first = job_claimer.claim(alice, job_id, lease_owner="proc-a")
    assert first is not None
    assert job_get(alice, job_id)["status"] == STATUS_RUNNING
    assert job_claimer.claim(alice, job_id, lease_owner="proc-b") is None


# ===========================================================================
# 六、AC4 / AC5：对外过滤与统计按七值走
# ===========================================================================


def test_job_list_filter_accepts_the_active_status_set(client, alice):
    """`GET /api/jobs?status=pending,running,cancelling` 只回在途任务，不再用 running 一个值兜。"""
    pending_id = job_create(alice, "prd", None)
    running_id, _lease = job_create_running(alice, "prompts")
    done_id = job_create(alice, "api_docs", None)
    job_claimer.claim(alice, done_id, lease_owner="proc-a")
    assert job_service.cancel_job(alice, done_id) is True  # 执行中 → 取消中

    response = client.get(
        "/api/jobs", params={"status": "pending,running,cancelling"}
    )
    assert response.status_code == 200, response.text
    ids = {item["id"] for item in response.json()["jobs"]}
    assert ids == {pending_id, running_id, done_id}


def test_admin_daily_stats_separate_cancelled_from_superseded(client):
    """AC5：管理端统计里被取代与已取消是两件事；成功率排除这两者。"""
    register(client, username="root")
    from backend.services import auth_service

    auth_service.set_admin("root", True)
    admin_id = _alice_id_for("root")

    _insert_job(admin_id, STATUS_SUCCEEDED)
    _insert_job(admin_id, STATUS_FAILED, error_code="LLM_UPSTREAM_ERROR")
    _insert_job(admin_id, STATUS_CANCELLED)
    _insert_job(admin_id, STATUS_SUPERSEDED)
    _insert_job(admin_id, STATUS_PENDING)
    _insert_job(admin_id, STATUS_RUNNING)
    _insert_job(admin_id, STATUS_CANCELLING)

    response = client.get(
        "/api/admin/stats/daily", params={"days": 7, "tz_offset_minutes": 0}
    )
    assert response.status_code == 200, response.text
    today = response.json()["items"][-1]

    assert today["task_count"] == 7
    assert today["succeeded_count"] == 1
    assert today["failed_count"] == 1, "只有 failed 是真失败"
    assert today["cancelled_count"] == 1, "已取消单独计数"
    assert today["superseded_count"] == 1, "被取代单独计数，不许并进已取消"
    assert today["active_count"] == 3, "排队中 / 执行中 / 取消中都是尚未收尾"
    # 成功率 / 失败率的分母只含已定稿与真失败（AC5）
    assert today["failure_rate"] == 0.5


def test_admin_job_list_status_filter_accepts_the_active_set(client):
    """管理端任务列表同样按七值过滤（「在飞」= pending,running,cancelling）。"""
    register(client, username="root")
    from backend.services import auth_service

    auth_service.set_admin("root", True)
    admin_id = _alice_id_for("root")

    active_ids = {
        _insert_job(admin_id, STATUS_PENDING),
        _insert_job(admin_id, STATUS_RUNNING),
        _insert_job(admin_id, STATUS_CANCELLING),
    }
    _insert_job(admin_id, STATUS_CANCELLED)

    response = client.get(
        "/api/admin/jobs", params={"status": "pending,running,cancelling"}
    )
    assert response.status_code == 200, response.text
    assert {item["id"] for item in response.json()["items"]} == active_ids


def _alice_id_for(username: str) -> int:
    row = users.get_by_username(username)
    assert row is not None
    return int(row["id"])


def _insert_job(
    owner_id: int,
    status: str,
    *,
    error_code: str = "",
    created_at: str | None = None,
) -> int:
    """直接插一条任务（管理端统计只关心状态列，不必走仓储的状态写）。"""
    moment = created_at or utc_now().isoformat()
    with connect() as conn:
        job_id = int(
            conn.execute(
                "INSERT INTO generation_jobs "
                "(owner_id, solution_id, kind, doc_kind, chat_stage, status, phase, "
                " draft_content, completed_sections, pending_items, input_fingerprint, usage, "
                " max_provider_switches, error_code, error_message, "
                " provider_protocol, provider_config_id, provider_name, provider_model, "
                " prompt_tokens, completion_tokens, created_at, updated_at, "
                " request_params, config_snapshot, skill_refs, skill_manifest) "
                "VALUES (%s, NULL, 'prd', '', '', %s, '', '', '[]', '[]', NULL, NULL, 2, %s, '', "
                " '', '', '', '', 0, 0, %s, %s, '{}', '{}', '[]', '') RETURNING id",
                (owner_id, status, error_code, moment, moment),
            ).fetchone()[0]
        )
        conn.commit()
    return job_id


# ===========================================================================
# 七、迁移 0007：旧取值一次性映射（幂等、可重复跑）
# ===========================================================================


def test_migration_0007_maps_legacy_status_rows_and_is_idempotent():
    """旧行迁移：`failed+CANCELLED/SUPERSEDED` → 对应状态；`running` 无租约 → `pending`。

    同时在同一条用例里钉住「可重复跑」：把库退回 0006 再升一次，映射结果不变，
    已经映射过的行一行都不再被改（0007 的 downgrade 是空操作，数据映射不回溯）。
    """
    from alembic import command
    from alembic.script import ScriptDirectory

    import dbsupport
    from backend.core.database import build_engine
    from backend.core.schema import (
        _BACKEND_ROOT,
        _alembic_config,
        current_revision,
        head_revision,
    )

    head = head_revision()
    assert head == "0007"
    previous = (
        ScriptDirectory.from_config(_alembic_config(_BACKEND_ROOT.as_posix()))
        .get_revision(head)
        .down_revision
    )
    assert previous == "0006"

    with dbsupport.empty_database() as url:
        engine = build_engine(url)
        try:
            cfg = _alembic_config(url)
            command.upgrade(cfg, previous)
            assert current_revision(engine) == previous

            with engine.begin() as conn:
                conn.exec_driver_sql(
                    "INSERT INTO users (username, password_hash, salt, created_at) "
                    "VALUES ('legacy', 'h', %s, '2026-01-01T00:00:00')",
                    ("s",),
                )
                # 五条旧行：两条「failed + 状态性错误码」、一条真失败、
                # 一条「running 且没有租约」（旧口径的排队中）、一条正常执行中。
                legacy = [
                    ("failed", "CANCELLED", "", ""),
                    ("failed", "SUPERSEDED", "", ""),
                    ("failed", "LLM_UPSTREAM_ERROR", "", ""),
                    ("running", "", "", ""),
                    ("running", "anything", "proc-5", "2026-01-01T00:10:00"),
                ]
                for index, (status, code, lease_owner, expire_at) in enumerate(legacy, 1):
                    conn.exec_driver_sql(
                        "INSERT INTO generation_jobs "
                        "(owner_id, solution_id, kind, doc_kind, chat_stage, status, phase, "
                        " draft_content, completed_sections, pending_items, input_fingerprint, usage, "
                        " max_provider_switches, error_code, error_message, "
                        " provider_protocol, provider_config_id, provider_name, provider_model, "
                        " prompt_tokens, completion_tokens, created_at, updated_at, "
                        " request_params, config_snapshot, skill_refs, skill_manifest, "
                        " lease_owner, lease_token, lease_expire_at, next_run_at) "
                        "VALUES (1, NULL, 'prd', '', '', %s, '', '', '[]', '[]', NULL, NULL, 2, %s, '', "
                        " '', '', '', '', 0, 0, %s, %s, '{}', '{}', '[]', '', "
                        " %s, %s, %s, '')",
                        (
                            status,
                            code,
                            f"2026-01-01T00:00:0{index}",
                            f"2026-01-01T00:00:0{index}",
                            lease_owner,
                            f"token-{index}" if lease_owner else "",
                            expire_at,
                        ),
                    )

            command.upgrade(cfg, head)
            assert current_revision(engine) == head

            with engine.connect() as conn:
                rows = conn.exec_driver_sql(
                    "SELECT status, error_code, lease_token FROM generation_jobs ORDER BY id"
                ).all()
            assert [row[0] for row in rows] == [
                "cancelled",
                "superseded",
                "failed",
                "pending",
                "running",
            ], "旧行映射：状态性错误码改成状态；无租约的 running 改成排队中"
            assert [row[1] for row in rows] == [
                "",
                "",
                "LLM_UPSTREAM_ERROR",
                "",
                "anything",
            ]
            assert rows[3][2] == ""

            # 可重复跑：退回 0006（数据迁移没有 DDL、downgrade 是空操作）再升一次。
            command.downgrade(cfg, previous)
            command.upgrade(cfg, head)
            with engine.connect() as conn:
                again = conn.exec_driver_sql(
                    "SELECT status, error_code FROM generation_jobs ORDER BY id"
                ).all()
            assert [row[0] for row in again] == [
                "cancelled",
                "superseded",
                "failed",
                "pending",
                "running",
            ]
            assert [row[1] for row in again] == [
                "",
                "",
                "LLM_UPSTREAM_ERROR",
                "",
                "anything",
            ]
        finally:
            engine.dispose()
