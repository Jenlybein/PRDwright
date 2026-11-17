"""执行账本（#80）：每次「已结束的执行」记一行，能回答「第几次、成还是败、为什么、花了多少、跑了多久」。

接缝
----
本票断言跨**三条**接缝，各有分工：

1. **状态机收尾 `job_state.transition`**（#79 的对外唯一转换动作）：账本的写入点就在它
   里面，与任务状态更新**同一个事务**。时机（只在有结论时写）、口径（成功 / 失败 /
   取消 / 被取代各自记不记）、原子性（同事务回滚）、以及「账本写失败不许把已成功的
   生成判死」都在这里验。
2. **主接缝**（造单 → 登记 → 派发 → 订阅，`ticket_support`）：真实执行路径上的成功、
   取消、被取代、启动回收都由它推出来——账本只在收尾后出现，在途时一行都没有。
3. **仓储接缝 `JobAttemptRepository`**：序号分配与 `(job_id, attempt_no)` 唯一约束是
   仓储层的职责（服务层不自己数），也只有这里能构造「同一任务的第二次执行」——
   今天是「每次执行都走到终态且不重跑」，第二次执行要等 #82 的重试。

为什么不直调 `_finalize` / `_record_attempt` 这类内部函数：见
`test_no_internal_positioning.py`（断言只架在公开边界上）。
"""

from __future__ import annotations

import json
import logging

import pytest
from conftest import job_claim, job_create, job_get, job_persist_progress, register, users
from dbsupport import connect

from backend.core import models
from backend.core.contracts import StreamEvent
from backend.core.engine import session_scope
from backend.repositories.job_attempt_repo import (
    OUTCOME_CANCELLED,
    OUTCOME_COMPLETED,
    OUTCOME_FAILED,
    JobAttemptRepository,
)
from backend.services import job_service, job_state
from ticket_support import build_and_register, run_ticket_events, run_ticket_in_flight


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------


def _alice_id() -> int:
    user = users.get_by_username("alice")
    assert user is not None
    return int(user["id"])


@pytest.fixture
def alice(client):
    """注册 alice 并返回其 id。"""
    register(client)
    return _alice_id()


def _attempts(owner_id: int, job_id: int) -> list[dict]:
    """读该任务的账本（经仓储的读方法，按序号升序）。"""
    with session_scope() as session:
        rows = JobAttemptRepository(session).list_by_job(owner_id, job_id)
        return [row.model_dump() for row in rows]


def _all_attempts() -> list[dict]:
    """整表读一遍账本（只用于「在途时一行都没有」这种全表断言）。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT job_id, attempt_no, outcome FROM job_attempts "
            "ORDER BY job_id, attempt_no"
        ).fetchall()
    return [dict(row) for row in rows]


def _append(job_id: int, *, outcome: str = OUTCOME_COMPLETED, **kwargs) -> int:
    """经仓储接缝追加一行账本，返回分配到的序号（序号分配是本层的职责）。"""
    with session_scope() as session:
        attempt = JobAttemptRepository(session).append(job_id, outcome=outcome, **kwargs)
        return int(attempt.attempt_no)


def _payload(draft: str = "", usage: str = '{"total_calls": 1}', duration_ms: int = 0):
    return job_state.FinishPayload(
        draft_content=draft,
        usage=usage,
        pending_items=[],
        duration_ms=duration_ms,
    )


def _events_with(caplog, event: str) -> list:
    return [record for record in caplog.records if getattr(record, "event", "") == event]


def _running(owner_id: int, kind: str = "prd"):
    """建一条任务并领走（执行中），返回 `(job_id, lease)`。"""
    job_id = job_create(owner_id, kind, None)
    return job_id, job_claim(owner_id, job_id)


# ---------------------------------------------------------------------------
# 一、AC1：抢占 / 过程不记账本，只有「执行有结论」才写一行
# ---------------------------------------------------------------------------


def test_creating_and_progress_writes_no_ledger_row(alice):
    """建单与合并写进度都不是结论：账本里一行都没有。

    「抢占当下不写账本」在本票的三态形态里就是「建单 / 领到任务 / 写进度」这些**开始与
    过程**的时刻——它们如果也记一行，账本就变成「每次动手都记一笔」的流水，
    「第几次、成还是败」再也答不出来。
    """
    job_id = job_create(alice, "prd", None)
    job_persist_progress(
        alice, job_id, draft_content="半截正文", phase="writing"
    )

    assert _attempts(alice, job_id) == []


def test_in_flight_execution_has_no_ledger_row_until_it_concludes(alice):
    """任务还在跑时全表零账本行；跑完才恰好一行（`completed`）。

    跨主接缝：假事件流卡在闸门上，在途那一刻读一次账本（必须为空），放开后订阅到收尾，
    再读一次（恰好一行）。这条与「建单」那条合起来就是 AC1 的两面。
    """
    observed: dict[str, list[dict]] = {}

    def while_running() -> None:
        observed["ledger"] = _all_attempts()

    registered, events = run_ticket_in_flight(
        alice,
        None,
        "prd",
        {},
        while_running,
        StreamEvent(type="chunk", content="第一段", data={}),
        reply="# 正文",
    )

    assert observed["ledger"] == [], "在途时不该有账本行"
    attempts = _attempts(alice, registered.job_id)
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == OUTCOME_COMPLETED
    assert events[-1].data["status"] == "succeeded"


def test_successful_execution_records_one_completed_row(alice):
    """成功收尾：一行 `completed`，序号从 1 起，耗时与用量和任务行同源。

    耗时断言刻意拿**任务行里那份 `usage` JSON** 当独立真值：账本若偷懒记 0，
    两边就对不上（`duration_ms` 在快速假流里也可能正好是 0，所以只比「与 usage 相等」，
    不硬写一个常数）。
    """
    registered, _ = run_ticket_events(
        alice, None, "prd", {}, StreamEvent(type="done", content="", data={"reply": "# 正文"})
    )

    attempts = _attempts(alice, registered.job_id)
    assert len(attempts) == 1
    attempt = attempts[0]
    assert (attempt["job_id"], attempt["attempt_no"]) == (registered.job_id, 1)
    assert attempt["outcome"] == OUTCOME_COMPLETED
    assert attempt["error_code"] == ""
    assert attempt["error_summary"] == ""

    row = job_get(alice, registered.job_id)
    assert row["status"] == "succeeded"
    usage = json.loads(row["usage"])
    assert attempt["duration_ms"] == int(usage["duration_ms"])
    assert attempt["prompt_tokens"] == int(row["prompt_tokens"])
    assert attempt["completion_tokens"] == int(row["completion_tokens"])


def test_repeat_transition_does_not_record_a_second_row(alice):
    """已终态再收尾一次：改到 0 行，账本也不多第二行（一次执行一行账）。"""
    job_id, lease = _running(alice)
    first = job_state.transition(
        job_state.JobEvent.SUCCEED,
        job_state.job_scope(alice, job_id),
        payload=_payload("第一版"),
        lease_token=lease.token,
    )
    assert first.rows == 1

    second = job_state.transition(
        job_state.JobEvent.SUCCEED,
        job_state.job_scope(alice, job_id),
        payload=_payload("第二版"),
    )
    assert second.rows == 0
    assert [attempt["attempt_no"] for attempt in _attempts(alice, job_id)] == [1]


# ---------------------------------------------------------------------------
# 二、AC2：序号从 1 起递增、不重复（唯一约束）
# ---------------------------------------------------------------------------


def test_attempt_numbers_start_at_one_and_increase(alice):
    """同一任务：1、2、3 逐个递增；另一条任务从 1 重新起算。"""
    job_id = job_create(alice, "prd", None)
    other_id = job_create(alice, "prompts", None)

    assert _append(job_id, error_code="LLM_UPSTREAM_ERROR") == 1
    assert _append(job_id, outcome=OUTCOME_FAILED, error_code="TIMEOUT") == 2
    assert _append(job_id) == 3
    assert _append(other_id) == 1, "序号按任务各自起算，不是全表自增"

    assert [attempt["attempt_no"] for attempt in _attempts(alice, job_id)] == [1, 2, 3]


def test_duplicate_attempt_number_is_rejected_by_unique_constraint(alice):
    """`(job_id, attempt_no)` 唯一约束是真的：重复序号在 flush 阶段就被数据库拒绝。

    这是 AC2「不重复」的**数据库级**保证——不能只靠「服务层记得不要写两次」：
    重试 / 重连 / 双收尾任何一处漏判都会变成多一行账，而唯一约束把漏判变成一次
    可观察的失败。
    """
    from sqlalchemy.exc import IntegrityError

    from backend.core.engine import session

    job_id = job_create(alice, "prd", None)
    assert _append(job_id) == 1

    with session() as db:
        db.add(
            models.JobAttempt(
                job_id=job_id,
                attempt_no=1,
                outcome=OUTCOME_COMPLETED,
                created_at="2026-01-01T00:00:00",
            )
        )
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()

    assert [attempt["attempt_no"] for attempt in _attempts(alice, job_id)] == [1]


# ---------------------------------------------------------------------------
# 三、AC3：失败也记账，含错误分类、脱敏摘要、耗时
# ---------------------------------------------------------------------------


def test_failed_execution_records_code_summary_and_duration(alice):
    """失败收尾：一行 `failed` + 错误分类 + 摘要，并带耗时与（空的）用量列。"""
    registered, _ = run_ticket_events(
        alice,
        None,
        "prd",
        {},
        StreamEvent(
            type="error",
            content="上游中断",
            code="LLM_UPSTREAM_ERROR",
            data={"error_type": "JobFailed"},
        ),
    )

    attempts = _attempts(alice, registered.job_id)
    assert len(attempts) == 1, "失败也要记账（一次执行一行）"
    attempt = attempts[0]
    assert attempt["outcome"] == OUTCOME_FAILED
    assert attempt["error_code"] == "LLM_UPSTREAM_ERROR"
    assert attempt["error_summary"] == "上游中断"

    row = job_get(alice, registered.job_id)
    assert row["status"] == "failed" and row["error_code"] == "LLM_UPSTREAM_ERROR"
    assert attempt["duration_ms"] == int(json.loads(row["usage"])["duration_ms"])


def test_error_summary_is_sanitized_and_truncated(alice):
    """摘要落库前压成单行、截断、抹掉疑似凭据；任务行上的原文不动。

    账本是长期留存的审计数据：上游回显 `sk-...` / `api_key=...` 时原样抄进去，
    等于把密钥写进一张不会被清理的表。同时说明「脱敏只发生在账本」——任务行仍保留
    原文，排障时读得到完整信息。
    """
    raw_message = (
        "上游返回 500：key=sk-live-abcdefghijklmnop api_key=secretvalue999\n"
        + "详情 " * 120
    )
    registered, _ = run_ticket_events(
        alice,
        None,
        "prd",
        {},
        StreamEvent(
            type="error", content=raw_message, code="LLM_UPSTREAM_ERROR", data={}
        ),
    )

    attempt = _attempts(alice, registered.job_id)[0]
    summary = attempt["error_summary"]
    assert "\n" not in summary, "摘要必须是单行"
    assert len(summary) <= 240, "摘要必须截断（上游可能回显整段响应体）"
    assert "sk-live-abcdefghijklmnop" not in summary, "疑似密钥不许原样落库"
    assert "secretvalue999" not in summary, "`api_key=...` 形式同样要抹掉"
    assert summary.startswith("上游返回 500：")

    row = job_get(alice, registered.job_id)
    assert row["error_message"] == raw_message, "任务行保留原文（脱敏只在账本做）"


# ---------------------------------------------------------------------------
# 四、AC4：任务行只留最近一次错误，完整历史查账本
# ---------------------------------------------------------------------------


def test_job_row_keeps_one_error_while_ledger_keeps_full_history(alice):
    """同一任务两次执行：任务行只有一份错误，账本两行都在、各自带自己的原因。

    第二次执行经仓储接缝造出来（今天不重跑，`#82` 的重试才会让它成为常态）：
    重点是「历史不依赖任务行」——任务行没有、也不该有一排历史列。
    """
    job_id, lease = _running(alice)
    first = job_state.transition(
        job_state.JobEvent.FAIL,
        job_state.job_scope(alice, job_id),
        payload=_payload("半截正文"),
        error_code="LLM_UPSTREAM_ERROR",
        error_message="第一次：上游 500",
        lease_token=lease.token,
    )
    assert first.rows == 1
    second_no = _append(
        job_id,
        outcome=OUTCOME_COMPLETED,
        prompt_tokens=120,
        completion_tokens=80,
        duration_ms=900,
    )
    assert second_no == 2

    # 任务行：只保留最近一次落库的那份错误（没有历史列）。
    row = job_get(alice, job_id)
    assert row["status"] == "failed"
    assert row["error_code"] == "LLM_UPSTREAM_ERROR"
    assert row["error_message"] == "第一次：上游 500"

    # 账本：完整历史，按序号读得到两次。
    attempts = _attempts(alice, job_id)
    assert [attempt["attempt_no"] for attempt in attempts] == [1, 2]
    assert attempts[0]["outcome"] == OUTCOME_FAILED
    assert attempts[0]["error_summary"] == "第一次：上游 500"
    assert attempts[1]["outcome"] == OUTCOME_COMPLETED
    assert attempts[1]["error_code"] == ""
    assert attempts[1]["prompt_tokens"] == 120
    assert attempts[1]["completion_tokens"] == 80
    assert attempts[1]["duration_ms"] == 900


# ---------------------------------------------------------------------------
# 五、AC5：取消与被取代的口径区分开
# ---------------------------------------------------------------------------


def test_cancelled_execution_is_recorded_as_cancelled_not_failed(alice):
    """取消：**请求停止当下不记账**，收尾时记一行 `cancelled`（与普通失败区分）。

    取消在被请求那一刻先把执行中的行落成 `cancelling`（用户要立刻看得见），
    但账本等到执行者确认收尾才写：否则一次执行会有「按下停止」与「真正结束」两行账。
    已取消的账本错误码是空串——状态由 `outcome` / 任务行 `status` 表达（票 #84 AC4）。
    """
    observed: dict[str, list[dict]] = {}

    def cancel_while_running() -> None:
        assert job_service.cancel_for_user(alice) == 1
        observed["ledger_right_after_cancel"] = _all_attempts()

    registered, _ = run_ticket_in_flight(
        alice,
        None,
        "prd",
        {},
        cancel_while_running,
        StreamEvent(type="chunk", content="第一段", data={}),
        reply="完整正文",
    )

    assert observed["ledger_right_after_cancel"] == [], "取消请求当下不记账本"
    attempts = _attempts(alice, registered.job_id)
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt["outcome"] == OUTCOME_CANCELLED
    assert attempt["outcome"] != OUTCOME_FAILED, "取消不是普通失败，口径要分开"
    assert attempt["error_code"] == "", "已取消不是错误码，状态由 status / outcome 表达"
    assert attempt["error_summary"] == "账号已删除，任务已取消"

    row = job_get(alice, registered.job_id)
    assert row["status"] == "cancelled" and row["error_code"] == ""


def test_superseded_execution_records_no_ledger_row(alice):
    """被取代：**不记账本**——它不是这次执行的结论，而是被更晚的任务接手。

    七态之后这是两个状态值：登记顶替先把旧行落成 `superseded`，旧协程醒来又确认一次
    「被取代」，两条路径都不该留下账本行（否则一次正常让位会被记成一次失败）。
    """
    observed: dict[str, int] = {}

    def supersede_while_running() -> None:
        new = build_and_register(
            owner_id=alice, solution_id=None, kind="prd", request={"requirements": "新任务"}
        )
        assert new.job_id > 0
        observed["new_job_id"] = new.job_id
        assert job_get(alice, new.job_id)["status"] == "pending", (
            "登记只接单、未派发的任务处于排队中"
        )

    old, _ = run_ticket_in_flight(
        alice,
        None,
        "prd",
        {},
        supersede_while_running,
        StreamEvent(type="chunk", content="旧稿半截", data={}),
        reply="# 旧稿",
    )

    old_row = job_get(alice, old.job_id)
    assert old_row["status"] == "superseded"
    assert old_row["error_code"] == "", "被取代不是错误码，是一个状态"
    assert _attempts(alice, old.job_id) == [], "被取代不是执行结论，不记账本"
    # 接手的新任务还在排队（没有结论）→ 也没有账本行。
    assert _attempts(alice, observed["new_job_id"]) == []


def test_reaped_execution_is_recorded_as_failed(alice):
    """启动回收把残留的非终态行判死：每个被回收的任务记一行 `failed`。

    这是「执行有结论」的第四种来源（服务重启后执行者已不在）——不记的话，最需要
    查的「为什么昨天那个任务没了」在账本里反而是一片空白。
    """
    job_id = job_create(alice, "prd", None)
    assert job_get(alice, job_id)["status"] == "pending"

    job_service.reap_orphans()

    row = job_get(alice, job_id)
    assert row["status"] == "failed" and row["error_code"] == "INTERNAL_ERROR"

    attempts = _attempts(alice, job_id)
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt["outcome"] == OUTCOME_FAILED
    assert attempt["error_code"] == "INTERNAL_ERROR"
    assert attempt["error_summary"] == "服务重启，任务已中断"
    # 执行者不在场：用量与耗时是**未知**，记 0（#82 的租约恢复会把用量带进账本）。
    assert (attempt["prompt_tokens"], attempt["completion_tokens"]) == (0, 0)
    assert attempt["duration_ms"] == 0


# ---------------------------------------------------------------------------
# 六、原子性与口径：同事务、写失败不判死
# ---------------------------------------------------------------------------


def test_ledger_and_state_change_share_one_transaction(alice):
    """账本与任务状态**同一个事务**：调用方回滚，两边一起回滚。

    这是「同事务」的证伪方式：先让状态更新与账本都写进去，再在同一事务里抛错。
    若账本另开事务（或先提交），它会残留一行指向一个仍是 `running` 的任务——
    那种账比没有账更误导（「已经结束了」却还在跑）。
    """
    job_id, lease = _running(alice)

    with pytest.raises(RuntimeError, match="同一事务里的后续步骤失败"):
        with session_scope() as session:
            job_state.transition(
                job_state.JobEvent.SUCCEED,
                job_state.job_scope(alice, job_id),
                payload=_payload("# 正文"),
                session=session,
                lease_token=lease.token,
            )
            raise RuntimeError("同一事务里的后续步骤失败")

    assert job_get(alice, job_id)["status"] == "running", "状态更新必须一起回滚"
    assert _attempts(alice, job_id) == [], "账本必须跟着回滚"


def test_ledger_write_failure_is_logged_but_does_not_kill_the_conclusion(alice, monkeypatch, caplog):
    """账本写不进去：任务照常判成功（并留一条警告），不许因为审计写失败把生成判死。

    口径与 `_write_document` 同源：副作用失败不改变结论，但必须留痕。
    实现上账本那一笔是**保存点**（写坏只回滚这一笔），所以状态更新不受污染——
    若让异常直接飞出去，整笔事务会连那条已写好的终态一起回滚，任务卡回 `running`。
    """
    job_id, lease = _running(alice)

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("账本写不进去")

    monkeypatch.setattr(job_state.JobAttemptRepository, "append", _boom)

    with caplog.at_level(logging.WARNING):
        result = job_state.transition(
            job_state.JobEvent.SUCCEED,
            job_state.job_scope(alice, job_id),
            payload=_payload("# 正文"),
            lease_token=lease.token,
        )

    assert result.rows == 1
    assert job_get(alice, job_id)["status"] == "succeeded", "不许因为账本写失败而判失败"
    assert _attempts(alice, job_id) == [], "这一笔账确实没写成"
    warnings = _events_with(caplog, "job.attempt_record_failed")
    assert len(warnings) == 1, "写不进去必须可见（留一条 warning）"
    assert warnings[0].job_id == job_id
    assert warnings[0].outcome == OUTCOME_COMPLETED
