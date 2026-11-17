"""任务状态机（#79 立起 / #84 扩宽）：七态单一转换入口的契约测试。

接缝说明
--------
`job_state.transition` 是本票设计的**对外唯一转换动作**（事件 + 期望条件 → 结果），
它自己就是被检验的公开边界：本文件只跨它断言「改到几行、库里最终是什么、丢弃怎么记」，
不构造 `JobRuntime`、不探登记表、也不直调 `_finalize`（见 `test_no_internal_positioning.py`）。
四条真实终止路径（成功 / 失败 / 取消 / 顶替）另在主接缝上覆盖
（`test_generation_job.py` / `test_job_wait_in_flight.py` / `test_job_seven_states.py`）。

## 七态与转换表

完整表在 `job_state` 模块 docstring 里（**唯一一份**）。本文件按行覆盖：抢占、
成功、失败、请求取消（两种去向）、确认取消、取消兜底、请求顶替、确认顶替（补载荷）、
回排队、判超时。

## 判据只有一条：影响行数

所有写都是条件写，`rows == 0` = 本次结果被丢弃（留一条丢弃日志），不重试、不重新排队。
「语句没报错」不算数。
"""

from __future__ import annotations

import logging

import pytest
from conftest import job_claim, job_create, job_get, register, users

from backend.services import job_service, job_state

CANCEL_MESSAGE = "用户停止了生成"


def _alice_id() -> int:
    """取已注册用户 alice 的 id（generation_jobs.owner_id 有外键）。"""
    user = users.get_by_username("alice")
    assert user is not None
    return int(user["id"])


@pytest.fixture
def alice(client):
    """注册 alice 并返回其 id。"""
    register(client)
    return _alice_id()


def _payload(draft: str, usage: str = '{"total_calls": 1}') -> job_state.FinishPayload:
    return job_state.FinishPayload(
        draft_content=draft,
        usage=usage,
        pending_items=[],
    )


def _running(owner_id: int, kind: str = "prd", solution_id: int | None = None):
    """建一条并领走：行处于执行中，返回 `(job_id, lease)`。"""
    job_id = job_create(owner_id, kind, solution_id)
    return job_id, job_claim(owner_id, job_id)


def _discards(caplog) -> list:
    return [
        record
        for record in caplog.records
        if getattr(record, "event", "") == "job.transition_discarded"
    ]


# ---------------------------------------------------------------------------
# 一、收尾：成功 / 失败只从 running 改得到，第二次是 0 行
# ---------------------------------------------------------------------------


def test_succeed_writes_succeeded_and_reports_one_row(alice):
    """running → succeeded：改到 1 行，正文与用量一起落库，租约清空。"""
    job_id, lease = _running(alice)

    result = job_state.transition(
        job_state.JobEvent.SUCCEED,
        job_state.job_scope(alice, job_id),
        payload=_payload("正文"),
        lease_token=lease.token,
    )

    assert result.rows == 1
    assert result.applied is True
    assert result.status == "succeeded"
    row = job_get(alice, job_id)
    assert row["status"] == "succeeded", "成功收尾的对外取值是 succeeded（不再有 completed）"
    assert row["draft_content"] == "正文"
    assert row["usage"] == '{"total_calls": 1}'
    assert row["error_code"] == ""
    assert (row["lease_owner"], row["lease_token"], row["lease_expire_at"]) == ("", "", "")


def test_repeat_transition_is_discarded_and_logged(alice, caplog):
    """对已是终态的任务再转换一次：0 行、不写第二次、留一条丢弃日志。

    判据是**影响行数**，不是「语句没报错」——把这件事当成新的失败去重试或重新排队，
    就等于让一个已经失去资格的执行者继续制造写入。
    """
    job_id, lease = _running(alice)
    first = job_state.transition(
        job_state.JobEvent.SUCCEED,
        job_state.job_scope(alice, job_id),
        payload=_payload("第一版"),
        lease_token=lease.token,
    )
    assert first.rows == 1

    with caplog.at_level(logging.INFO):
        second = job_state.transition(
            job_state.JobEvent.SUCCEED,
            job_state.job_scope(alice, job_id),
            payload=_payload("第二版"),
        )

    assert second.rows == 0
    assert second.discarded is True
    # 丢弃时如实带回库里那一行的事实，调用方才知道「别人已经收尾成了什么」
    assert second.status == "succeeded"
    row = job_get(alice, job_id)
    assert row["draft_content"] == "第一版", "第二次的结果不得覆盖已落库的终态"
    discarded = _discards(caplog)
    assert len(discarded) == 1
    assert discarded[0].job_id == job_id
    assert discarded[0].attempted_status == "succeeded"


def test_fail_cannot_flip_a_succeeded_job(alice):
    """已定稿的不能被晚到的失败覆写回 failed（旧代码的「状态与产物不一致」）。"""
    job_id, lease = _running(alice)
    job_state.transition(
        job_state.JobEvent.SUCCEED,
        job_state.job_scope(alice, job_id),
        payload=_payload("正文"),
        lease_token=lease.token,
    )

    result = job_state.transition(
        job_state.JobEvent.FAIL,
        job_state.job_scope(alice, job_id),
        payload=_payload("半截"),
        error_code="INTERNAL_ERROR",
        error_message="晚到的失败",
        lease_token=lease.token,
    )

    assert result.rows == 0
    row = job_get(alice, job_id)
    assert row["status"] == "succeeded"
    assert row["error_code"] == ""


def test_a_stale_token_cannot_succeed(alice):
    """旧执行者的收尾写不进去：凭证已经不是本轮（#82 的写权判据）。"""
    job_id, lease = _running(alice)
    assert job_service.cancel_job(alice, job_id) is True
    assert job_state.transition(
        job_state.JobEvent.CONFIRM_CANCEL,
        job_state.job_scope(alice, job_id),
        error_message=CANCEL_MESSAGE,
        lease_token=lease.token,
    ).applied

    stale = job_state.transition(
        job_state.JobEvent.SUCCEED,
        job_state.job_scope(alice, job_id),
        payload=_payload("晚到的正文"),
        lease_token=lease.token,
    )

    assert stale.rows == 0
    assert job_get(alice, job_id)["status"] == "cancelled"


# ---------------------------------------------------------------------------
# 二、取消：请求是两段，确认与兜底各一段（#84 的 AC1 / AC2 / AC3）
# ---------------------------------------------------------------------------


def test_request_cancel_on_a_queued_job_lands_cancelled_at_once(alice):
    """排队中取消：没人在跑，一步落已取消（AC1 前半句）。"""
    job_id = job_create(alice, "prd", None)
    assert job_get(alice, job_id)["status"] == "pending"

    result = job_state.transition(
        job_state.JobEvent.REQUEST_CANCEL,
        job_state.job_scope(alice, job_id),
        error_message=CANCEL_MESSAGE,
    )

    assert result.rows == 1
    assert result.status == "cancelled"
    row = job_get(alice, job_id)
    assert row["status"] == "cancelled"
    assert row["error_code"] == "", "已取消不带状态性错误码（AC4）"


def test_request_cancel_on_a_running_job_enters_cancelling_keeping_lease(alice):
    """执行中取消：先进入取消中，租约不动（执行者还要用它确认，AC1 后半句）。"""
    job_id, lease = _running(alice)

    result = job_state.transition(
        job_state.JobEvent.REQUEST_CANCEL,
        job_state.job_scope(alice, job_id),
        error_message=CANCEL_MESSAGE,
    )

    assert result.rows == 1
    assert result.status == "cancelling"
    row = job_get(alice, job_id)
    assert row["status"] == "cancelling"
    assert row["lease_token"] == lease.token, "取消请求不清租约"


def test_confirm_cancel_lands_cancelled_with_one_payload_and_clears_lease(alice):
    """执行者确认取消：落已取消、清租约、半截正文与用量补一次（AC2）。"""
    job_id, lease = _running(alice)
    assert job_state.transition(
        job_state.JobEvent.REQUEST_CANCEL,
        job_state.job_scope(alice, job_id),
        error_message=CANCEL_MESSAGE,
    ).applied

    confirmed = job_state.transition(
        job_state.JobEvent.CONFIRM_CANCEL,
        job_state.job_scope(alice, job_id),
        payload=_payload("半截正文"),
        error_message=CANCEL_MESSAGE,
        lease_token=lease.token,
    )

    assert confirmed.applied
    row = job_get(alice, job_id)
    assert row["status"] == "cancelled"
    assert row["draft_content"] == "半截正文"
    assert row["usage"] == '{"total_calls": 1}'
    assert row["error_code"] == ""
    assert (row["lease_owner"], row["lease_token"], row["lease_expire_at"]) == ("", "", "")

    # 第二次确认改到 0 行：取消确认只发生一次
    again = job_state.transition(
        job_state.JobEvent.CONFIRM_CANCEL,
        job_state.job_scope(alice, job_id),
        payload=_payload("第二版"),
        error_message=CANCEL_MESSAGE,
        lease_token=lease.token,
    )
    assert again.rows == 0
    assert job_get(alice, job_id)["draft_content"] == "半截正文"


def test_expire_cancelling_is_the_recovery_fallback(alice):
    """取消兜底（AC3）：取消中 + 旧凭证 → 已取消；凭证对不上就 0 行。"""
    job_id, lease = _running(alice)
    assert job_state.transition(
        job_state.JobEvent.REQUEST_CANCEL,
        job_state.job_scope(alice, job_id),
        error_message=CANCEL_MESSAGE,
    ).applied

    result = job_state.transition(
        job_state.JobEvent.EXPIRE_CANCELLING,
        job_state.job_scope(alice, job_id),
        error_message="执行者未确认取消，租约到期后已收为已取消",
        lease_fence=job_state.LeaseFence(token=lease.token, expire_at=lease.expire_at),
    )

    assert result.applied
    row = job_get(alice, job_id)
    assert row["status"] == "cancelled"
    assert row["error_code"] == ""

    # 终态之后再来一次：0 行
    again = job_state.transition(
        job_state.JobEvent.EXPIRE_CANCELLING,
        job_state.job_scope(alice, job_id),
        error_message=CANCEL_MESSAGE,
        lease_fence=job_state.LeaseFence(token=lease.token, expire_at=lease.expire_at),
    )
    assert again.rows == 0


# ---------------------------------------------------------------------------
# 三、顶替：请求落 superseded；执行者醒来只补一次载荷（不写正文）
# ---------------------------------------------------------------------------


def test_supersede_scope_only_touches_active_rows_of_same_owner_solution_and_kind(alice):
    """顶替的期望条件：同 owner + 同方案 + 同产物 + **排队中 / 执行中**；别的行一条都不动。

    **「取消中」不在其中**（P0-3）：用户已经表达了停止的意图，把这一行改写成
    `superseded` 会把「用户不要了」说成「被新任务挤掉」——两个含义相反的状态混成一句，
    统计与排查都再也分不开。它由执行者确认、或恢复器兜底，收成 `cancelled`。
    """
    solution_id = 11
    other_solution = 12
    target = _running(alice, "prd", solution_id)[0]
    queued = job_create(alice, "prd", solution_id)
    cancelling = _running(alice, "prd", solution_id)
    assert job_state.transition(
        job_state.JobEvent.REQUEST_CANCEL,
        job_state.job_scope(alice, cancelling[0]),
        error_message=CANCEL_MESSAGE,
    ).applied
    other = _running(alice, "prd", other_solution)[0]
    other_kind = _running(alice, "prompts", solution_id)[0]
    finished = _running(alice, "prd", solution_id)
    assert job_state.transition(
        job_state.JobEvent.SUCCEED,
        job_state.job_scope(alice, finished[0]),
        payload=_payload("正文"),
        lease_token=finished[1].token,
    ).applied

    result = job_state.transition(
        job_state.JobEvent.SUPERSEDE,
        job_state.supersede_scope(alice, solution_id, "prd"),
    )

    assert result.rows == 2
    for job_id in (target, queued):
        row = job_get(alice, job_id)
        assert row["status"] == "superseded"
        assert row["error_code"] == "", "被取代不是一个错误码，是一个状态"
        assert row["error_message"] == "被新任务取代"
        assert (row["lease_owner"], row["lease_token"], row["lease_expire_at"]) == ("", "", "")
    # 取消中的那一行原样停着：状态与租约都不动（执行者还要用租约确认退出）
    cancel_row = job_get(alice, cancelling[0])
    assert cancel_row["status"] == "cancelling"
    assert cancel_row["lease_token"] == cancelling[1].token
    assert job_get(alice, other)["status"] == "running"
    assert job_get(alice, other_kind)["status"] == "running"
    assert job_get(alice, finished[0])["status"] == "succeeded"


def test_confirm_superseded_backfills_the_payload_exactly_once(alice):
    """被顶替的执行者醒来：补一次载荷（状态已经是 superseded，不改状态）。"""
    job_id, _lease = _running(alice)
    assert job_state.transition(
        job_state.JobEvent.SUPERSEDE, job_state.supersede_scope(alice, None, "prd")
    ).applied

    backfill = job_state.transition(
        job_state.JobEvent.CONFIRM_SUPERSEDED,
        job_state.job_scope(alice, job_id),
        payload=_payload("半截正文"),
    )

    assert backfill.applied
    row = job_get(alice, job_id)
    assert row["status"] == "superseded"
    assert row["draft_content"] == "半截正文"
    assert row["usage"] == '{"total_calls": 1}'

    again = job_state.transition(
        job_state.JobEvent.CONFIRM_SUPERSEDED,
        job_state.job_scope(alice, job_id),
        payload=_payload("第二版"),
    )
    assert again.rows == 0
    assert job_get(alice, job_id)["draft_content"] == "半截正文"


# ---------------------------------------------------------------------------
# 四、恢复：回排队落 pending；判超时落 failed（都用扫描时的旧凭证 fencing）
# ---------------------------------------------------------------------------


def test_requeue_fence_must_match_the_scan_time_lease(alice):
    """回排队：凭证 / 到期时间对得上才改到行，且落回 `pending`。"""
    job_id, lease = _running(alice)
    fence = job_state.LeaseFence(token=lease.token, expire_at=lease.expire_at)

    result = job_state.transition(
        job_state.JobEvent.REQUEUE,
        job_state.job_scope(alice, job_id),
        error_code=job_state.CODE_LEASE_EXPIRED,
        error_message="执行者进程已消失，租约到期未续",
        lease_fence=fence,
    )

    assert result.applied
    row = job_get(alice, job_id)
    assert row["status"] == "pending"
    assert (row["lease_owner"], row["lease_token"], row["lease_expire_at"]) == ("", "", "")

    # 同一份旧凭证再来一次：0 行（这一行已经不是那份死租约了）
    again = job_state.transition(
        job_state.JobEvent.REQUEUE,
        job_state.job_scope(alice, job_id),
        error_code=job_state.CODE_LEASE_EXPIRED,
        error_message="同一份旧凭证",
        lease_fence=fence,
    )
    assert again.rows == 0


def test_expire_writes_failed_with_lease_expired(alice):
    """判超时：落 `failed + LEASE_EXPIRED`（失败原因仍是错误码），清租约。"""
    job_id, lease = _running(alice)
    result = job_state.transition(
        job_state.JobEvent.EXPIRE,
        job_state.job_scope(alice, job_id),
        error_code=job_state.CODE_LEASE_EXPIRED,
        error_message="执行者进程已消失，租约到期未续",
        lease_fence=job_state.LeaseFence(token=lease.token, expire_at=lease.expire_at),
    )

    assert result.applied
    row = job_get(alice, job_id)
    assert row["status"] == "failed"
    assert row["error_code"] == "LEASE_EXPIRED"
    assert (row["lease_owner"], row["lease_token"], row["lease_expire_at"]) == ("", "", "")


# ---------------------------------------------------------------------------
# 五、批量作用域的取消：只动在途，一步收口成已取消（P0-4）
# ---------------------------------------------------------------------------


def test_cancel_scope_settles_every_active_job_of_that_solution_in_one_step(alice):
    """按方案取消（删方案）：**任意非终态一步落已取消**并清租约，终态与别的方案不动。

    为什么不是「执行中 → 取消中 → 等确认」（P0-4）：方案已经不存在了，不会再有执行者
    来确认退出。留着 `cancelling` 只是把这一行交给恢复器、等租约到期才收口——用户会
    看到一条「正在取消」却没人管的行。
    """
    target = _running(alice, "prd", 11)
    queued = job_create(alice, "prd", 11)
    other_solution = _running(alice, "prd", 12)[0]
    finished = _running(alice, "prd", 11)
    assert job_state.transition(
        job_state.JobEvent.SUCCEED,
        job_state.job_scope(alice, finished[0]),
        payload=_payload("正文"),
        lease_token=finished[1].token,
    ).applied

    result = job_state.transition(
        job_state.JobEvent.REQUEST_CANCEL,
        job_state.solution_scope(alice, 11),
        error_message="方案已删除，任务已取消",
    )

    assert result.rows == 2
    for job_id in (target[0], queued):
        row = job_get(alice, job_id)
        assert row["status"] == "cancelled"
        assert row["error_code"] == "", "已取消不是一个错误码，是一个状态"
        assert row["error_message"] == "方案已删除，任务已取消"
        # 清租约：这一行没有执行者，也再没有「谁来确认」这件事
        assert (row["lease_owner"], row["lease_token"], row["lease_expire_at"]) == ("", "", "")
    assert job_get(alice, other_solution)["status"] == "running"
    assert job_get(alice, finished[0])["status"] == "succeeded"


def test_owner_scope_cancels_every_active_job_of_that_owner(alice):
    """按 owner 取消（删账号）：覆盖名下所有方案，任意非终态一步落已取消。"""
    running = _running(alice, "prd", 11)[0]
    queued = job_create(alice, "prd", None)
    other = _running(alice, "prompts", 12)[0]
    finished = _running(alice, "api_docs", 13)
    assert job_state.transition(
        job_state.JobEvent.SUCCEED,
        job_state.job_scope(alice, finished[0]),
        payload=_payload("正文"),
        lease_token=finished[1].token,
    ).applied

    result = job_state.transition(
        job_state.JobEvent.REQUEST_CANCEL,
        job_state.owner_scope(alice),
        error_message="账号已删除",
    )

    assert result.rows == 3
    for job_id in (running, other, queued):
        assert job_get(alice, job_id)["status"] == "cancelled"
    assert job_get(alice, finished[0])["status"] == "succeeded"
