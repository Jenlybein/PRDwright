"""任务状态机（#79）：三态单一转换入口的契约测试。

接缝说明
--------
`job_state.transition` 是本票设计的**对外唯一转换动作**（事件 + 期望条件 → 结果），
它自己就是被检验的公开边界：本文件只跨它断言「改到几行、库里最终是什么、丢弃怎么记」，
不构造 `JobRuntime`、不探登记表、也不直调 `_finalize`（见 `test_no_internal_positioning.py`）。
四条真实终止路径（成功 / 失败 / 取消 / 顶替）另在主接缝上覆盖
（`test_generation_job.py` / `test_job_wait_in_flight.py`）。

本票只做三态：`running` / `completed` / `failed`；`CANCELLED` / `SUPERSEDED` 仍是
`error_code`，不是状态值。七态与「取消中」是 #84 的事。
"""

from __future__ import annotations

import logging

import pytest
from conftest import job_create, job_get, register, users

from backend.services import job_service, job_state


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
        completed_sections=[],
        usage=usage,
        pending_items=[],
    )


def _discards(caplog) -> list:
    return [
        record
        for record in caplog.records
        if getattr(record, "event", "") == "job.transition_discarded"
    ]


# ---------------------------------------------------------------------------
# 一、收尾：成功 / 失败只从 running 改得到，第二次是 0 行
# ---------------------------------------------------------------------------


def test_succeed_writes_completed_and_reports_one_row(alice):
    """running → completed：改到 1 行，正文与用量一起落库。"""
    job_id = job_create(alice, "prd", None)

    result = job_state.transition(
        job_state.JobEvent.SUCCEED,
        job_state.job_scope(alice, job_id),
        payload=_payload("正文"),
    )

    assert result.rows == 1
    assert result.applied is True
    assert result.status == "completed"
    row = job_get(alice, job_id)
    assert row["status"] == "completed"
    assert row["draft_content"] == "正文"
    assert row["usage"] == '{"total_calls": 1}'
    assert row["error_code"] == ""


def test_repeat_transition_is_discarded_and_logged(alice, caplog):
    """对已是终态的任务再转换一次：0 行、不写第二次、留一条丢弃日志。

    判据是**影响行数**，不是「语句没报错」——把这件事当成新的失败去重试或重新排队，
    就等于让一个已经失去资格的执行者继续制造写入。
    """
    job_id = job_create(alice, "prd", None)
    first = job_state.transition(
        job_state.JobEvent.SUCCEED, job_state.job_scope(alice, job_id), payload=_payload("第一版")
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
    assert second.status == "completed"
    row = job_get(alice, job_id)
    assert row["draft_content"] == "第一版", "第二次的结果不得覆盖已落库的终态"
    discarded = _discards(caplog)
    assert len(discarded) == 1
    assert discarded[0].job_id == job_id
    assert discarded[0].attempted_status == "completed"


def test_fail_cannot_flip_a_completed_job(alice):
    """完成的不能被晚到的失败覆写回 failed（旧代码的「状态与产物不一致」）。"""
    job_id = job_create(alice, "prd", None)
    job_state.transition(
        job_state.JobEvent.SUCCEED, job_state.job_scope(alice, job_id), payload=_payload("正文")
    )

    result = job_state.transition(
        job_state.JobEvent.FAIL,
        job_state.job_scope(alice, job_id),
        payload=_payload(""),
        error_code="LLM_UPSTREAM_ERROR",
        error_message="晚到的失败",
    )

    assert result.rows == 0
    row = job_get(alice, job_id)
    assert row["status"] == "completed"
    assert row["error_code"] == ""
    assert row["error_message"] == ""


def test_transition_on_unknown_job_is_discarded_without_raising(alice, caplog):
    """对象不存在 / 归属不符：0 行 + 丢弃日志，不抛异常（不是程序错误）。"""
    with caplog.at_level(logging.INFO):
        result = job_state.transition(
            job_state.JobEvent.SUCCEED,
            job_state.job_scope(alice, 999_999),
            payload=_payload("正文"),
        )
    assert result.rows == 0
    assert result.status == ""
    assert len(_discards(caplog)) == 1


# ---------------------------------------------------------------------------
# 二、取消 / 顶替：按条件批量改，只有 running 会被改到
# ---------------------------------------------------------------------------


def test_cancel_scope_only_touches_running_jobs_of_that_solution(alice):
    """按方案取消：只改该方案下仍在 running 的行，已终态与别的方案都不动。"""
    target = job_create(alice, "prd", 11)
    other_solution = job_create(alice, "prd", 12)
    finished = job_create(alice, "prd", 11)
    job_state.transition(
        job_state.JobEvent.SUCCEED,
        job_state.job_scope(alice, finished),
        payload=_payload("正文"),
    )

    result = job_state.transition(
        job_state.JobEvent.CANCEL,
        job_state.solution_scope(alice, 11),
        error_code=job_state.CODE_CANCELLED,
        error_message="方案已删除，任务已取消",
    )

    assert result.rows == 1
    cancelled = job_get(alice, target)
    assert cancelled["status"] == "failed"
    assert cancelled["error_code"] == "CANCELLED"
    assert cancelled["error_message"] == "方案已删除，任务已取消"
    assert job_get(alice, other_solution)["status"] == "running"
    assert job_get(alice, finished)["status"] == "completed"


def test_supersede_scope_only_touches_same_owner_solution_and_kind(alice):
    """顶替：同一 owner + 方案 + 产物下旧的 running 落 failed + SUPERSEDED。"""
    old = job_create(alice, "prd", None)
    other_kind = job_create(alice, "prompts", None)

    result = job_state.transition(
        job_state.JobEvent.SUPERSEDE,
        job_state.supersede_scope(alice, None, "prd"),
        error_code=job_state.CODE_SUPERSEDED,
        error_message="被新任务取代",
    )

    assert result.rows == 1
    row = job_get(alice, old)
    assert row["status"] == "failed"
    assert row["error_code"] == "SUPERSEDED"
    assert row["error_message"] == "被新任务取代"
    assert job_get(alice, other_kind)["status"] == "running"


# ---------------------------------------------------------------------------
# 三、0 行之后：abort 已落库时补一次载荷，再来一次同样是丢弃
# ---------------------------------------------------------------------------


def test_aborted_row_accepts_one_payload_completion_then_discards(alice, caplog):
    """取消先落 `failed + CANCELLED`（还没用量），收尾用同一 abort 码补一次载荷。

    取消是「请求停止」，收尾才是把半截正文与用量记下来那一刻——但只准补一次：
    第二次同样是 0 行并被丢弃。否则「重复收尾」会变成「重复记账」。
    """
    job_id = job_create(alice, "prd", None)
    assert job_service.cancel_job(alice, job_id) is True

    completed = job_state.transition(
        job_state.JobEvent.FAIL,
        job_state.job_scope(alice, job_id),
        payload=_payload("半截正文", usage='{"total_calls": 2}'),
        error_code=job_state.CODE_CANCELLED,
        error_message="用户停止了生成",
    )
    assert completed.rows == 1
    row = job_get(alice, job_id)
    assert row["status"] == "failed"
    assert row["error_code"] == "CANCELLED"
    assert row["draft_content"] == "半截正文"
    assert row["usage"] == '{"total_calls": 2}'

    with caplog.at_level(logging.INFO):
        again = job_state.transition(
            job_state.JobEvent.FAIL,
            job_state.job_scope(alice, job_id),
            payload=_payload("又一次", usage='{"total_calls": 3}'),
            error_code=job_state.CODE_CANCELLED,
            error_message="用户停止了生成",
        )
    assert again.rows == 0
    assert job_get(alice, job_id)["draft_content"] == "半截正文"
    assert len(_discards(caplog)) == 1


def test_reaped_row_is_not_overwritten_by_a_late_abort(alice):
    """启动回收写的 `INTERNAL_ERROR` 不能被晚到的取消收尾覆写（§1.5 的覆盖窗口）。"""
    job_id = job_create(alice, "prd", None)
    job_service.reap_orphans()

    result = job_state.transition(
        job_state.JobEvent.FAIL,
        job_state.job_scope(alice, job_id),
        payload=_payload("半截正文"),
        error_code=job_state.CODE_CANCELLED,
        error_message="用户停止了生成",
    )

    assert result.rows == 0
    row = job_get(alice, job_id)
    assert row["status"] == "failed"
    assert row["error_code"] == "INTERNAL_ERROR"
    assert row["error_message"] == "服务重启，任务已中断"
