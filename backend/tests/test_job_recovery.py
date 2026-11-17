"""恢复器（#83）：租约过期 / 执行者进程消失之后，由 `job_recovery` 收口。

## 这张票回答什么问题

执行权在 #82 之后是一条**会到期**的租约。于是「执行者还在跑」有了到期这条出路，
但也留下了一个必须收口的新空档：**执行者的进程崩了，租约到期，没人接手**。这一行
如果没人动，它会永远停在「执行中」——用户看到进度条一直转，运维看到一条说不清的
running 行。恢复器就是收这个口的：

1. 扫一批「该接手的行」（周期档 = 租约已过期；启动档 = 库里所有非终态行，
   因为进程刚起来时它上面不可能有真正的执行者）；
2. 用**扫描时读到的旧凭证**（凭证 + 到期时间）做条件更新去接手——凭证对不上就一行
   都改不到，跟 #82 判权是同一条规则，不另立一套；
3. 周期档按账本里已经记了几次（剩余次数）决定「回排队」还是「判失败」；启动档直接
   落终态，把 AC4「重启后不存在永远进行中」钉死；
4. 每一轮交接都记一条**超时账本**（`error_code=LEASE_EXPIRED`），写明超时原因，
   与状态更新同事务（#80 的口径）。

## 七态口径（#84）

状态是七个值（见 `test_job_seven_states.py`）。本文件关心恢复器碰得到的那几条：

- 「回排队」现在读作 `status='pending'`（不再与执行中共用 `running`）；
- 「判失败」落 `failed + LEASE_EXPIRED`；
- 启动档遇到**取消中**的行落 `cancelled`（用户的意图优先于「超时」）；
- 「取消中 + 租约过期」由周期档兜底落 `cancelled`（AC3）。

## 接缝（断言只架在这三处）

1. **`job_recovery.recover_once(limit)`**（本票新增、可直接调用的单元）：定时扫描的
   入口。它不起线程、不起定时器——接入调度循环是 #89 的事。
2. **`job_recovery.scan` / `recover_one`**：扫描与单条收口。「扫描之后、收口之前
   凭证被换掉」这段窗口是 AC3 的核心，所以这两个也属于公开面。
3. **`job_service.reap_orphans()`**（启动回收）：进程刚起来，库里任何既有租约都
   属于已经消失的执行者，所以它调的是 `recover_once(..., startup=True)`。
"""

from __future__ import annotations

import ast
import logging
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from conftest import job_create, job_get, register, users

from backend.core.clock import utc_now
from backend.core.engine import session_scope
from backend.repositories.job_attempt_repo import (
    OUTCOME_CANCELLED,
    OUTCOME_FAILED,
    JobAttemptRepository,
)
from backend.services import job_backoff, job_claimer, job_recovery, job_service, job_state

# 租约时长与 `job_claimer.DEFAULT_LEASE_SECONDS` 一致：用例只借它算「过期时刻」。
LEASE_SECONDS = job_claimer.DEFAULT_LEASE_SECONDS


def _alice_id() -> int:
    user = users.get_by_username("alice")
    assert user is not None
    return int(user["id"])


@pytest.fixture
def alice(client):
    """注册 alice 并返回其 id（generation_jobs.owner_id 有外键）。"""
    register(client)
    return _alice_id()


def _claim(
    owner_id: int, job_id: int, *, lease_owner: str = "proc-a", now: datetime | None = None
):
    """领一条任务的执行权；领不到直接失败（用例里「崩溃前它一定领到了」）。"""
    lease = job_claimer.claim(owner_id, job_id, lease_owner=lease_owner, now=now or utc_now())
    assert lease is not None, "用例前提：这一行在崩溃前被某个执行者领走了"
    return lease


def _expired_at(lease) -> datetime:
    """这一轮租约已经过期的那个时刻（+30 秒余量，别卡在边界上）。"""
    return datetime.fromisoformat(lease.expire_at) + timedelta(seconds=30)


def _attempts(owner_id: int, job_id: int) -> list[dict]:
    """读该任务的账本（经仓储的读方法，归属不符时为空列表）。"""
    with session_scope() as db:
        rows = JobAttemptRepository(db).list_by_job(owner_id, job_id)
        return [row.model_dump() for row in rows]


def _discards(caplog) -> list:
    return [
        record
        for record in caplog.records
        if getattr(record, "event", "") == "job.transition_discarded"
    ]


# ---------------------------------------------------------------------------
# 一、AC1：执行者的进程没了，任务最终离开「执行中」
# ---------------------------------------------------------------------------


def test_a_dead_executors_job_leaves_executing_and_is_claimable_again(alice):
    """崩溃之后第一次周期恢复：回排队（回到 `pending`、租约清空、还能被领走）。

    七态之后「离开执行中」有一个**确定的取值**：`status='pending'`（不是「`running`
    加一个空租约」这种同形表达）。它没有被凭空判死——这一次执行只是没跑成（记进账本，
    见 AC2），任务本身回到队列里等下一次执行（调度循环是 #89）。
    """
    job_id = job_create(alice, "prd", None)
    started = utc_now()
    _claim(alice, job_id, now=started)
    died_later = started + timedelta(minutes=10)

    report = job_recovery.recover_once(10, now=died_later)

    assert report.scanned == 1
    assert report.requeued == 1
    assert report.failed == 0
    row = job_get(alice, job_id)
    assert row["status"] == "pending", "回排队要落回排队中这个值"
    assert (row["lease_owner"], row["lease_token"], row["lease_expire_at"]) == ("", "", "")
    # 不是「永远进行中」：等过失败退避（#88，超时那一路 5 秒）之后，下一个执行者
    # 就能把它领走。退避到期之前领不走——那是 #88 的到期闸门，本文件只借它算时刻。
    due = died_later + timedelta(seconds=job_backoff.BACKOFF_TIMEOUT_SECONDS)
    assert job_claimer.claim(alice, job_id, lease_owner="proc-b", now=due) is not None


def test_used_up_attempts_turn_the_job_failed_with_lease_expired(alice):
    """次数用完：判失败、落确定终态，错误码是稳定的 `LEASE_EXPIRED`。

    「最终离开执行中」在这里是**终态**：连续崩三次（每次都被恢复器接手并记一笔），
    第三次之后不再回排队，行落 `failed + LEASE_EXPIRED`，租约同样交还。
    """
    job_id = job_create(alice, "prd", None)
    moment = utc_now()
    last = None
    for _ in range(job_recovery.DEFAULT_MAX_ATTEMPTS):
        assert job_claimer.claim(alice, job_id, lease_owner="proc-a", now=moment) is not None
        moment = moment + timedelta(minutes=5)
        last = job_recovery.recover_once(10, now=moment)
        # #88：回排队之后要先等过失败退避才允许被领走（超时那一路 5 秒），
        # 所以下一轮的「现在」推到到期时刻，而不是接着上一轮的失败时刻。
        moment = moment + timedelta(seconds=job_backoff.BACKOFF_TIMEOUT_SECONDS)

    assert last is not None
    assert last.failed == 1, "次数用完之后不该再回排队"
    row = job_get(alice, job_id)
    assert row["status"] == "failed"
    assert row["error_code"] == "LEASE_EXPIRED"
    assert row["lease_token"] == "" and row["lease_expire_at"] == ""
    attempts = _attempts(alice, job_id)
    assert [item["error_code"] for item in attempts] == ["LEASE_EXPIRED"] * (
        job_recovery.DEFAULT_MAX_ATTEMPTS
    )
    assert [item["outcome"] for item in attempts] == [OUTCOME_FAILED] * (
        job_recovery.DEFAULT_MAX_ATTEMPTS
    )


def test_recovery_is_a_noop_when_nothing_has_expired(alice):
    """还没有过期的租约不在候选里：正在跑的任务不会被恢复器抢走。"""
    job_id = job_create(alice, "prd", None)
    lease = _claim(alice, job_id)
    still_running = datetime.fromisoformat(lease.expire_at) - timedelta(seconds=30)

    report = job_recovery.recover_once(10, now=still_running)

    assert report.scanned == 0
    row = job_get(alice, job_id)
    assert row["lease_token"] == lease.token, "没到期的租约不许被清掉"
    assert _attempts(alice, job_id) == []


# ---------------------------------------------------------------------------
# 二、AC2：恢复时记一条超时账本，写明超时原因（LEASE_EXPIRED）
# ---------------------------------------------------------------------------


def test_recovery_records_the_timeout_with_a_stable_reason(alice):
    """回排队也要记账：这一次执行是「超时没跑成」，原因写进账本。

    账本与状态更新同事务（#80 的口径）——所以这里既能看到「租约到期」那一笔，
    又没有任何用量（执行者不在场，token / 耗时是未知而不是 0 成本）。
    """
    job_id = job_create(alice, "prd", None)
    started = utc_now()
    lease = _claim(alice, job_id, now=started)

    job_recovery.recover_once(10, now=_expired_at(lease))

    attempts = _attempts(alice, job_id)
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt["attempt_no"] == 1
    assert attempt["outcome"] == OUTCOME_FAILED
    assert attempt["error_code"] == "LEASE_EXPIRED", "错误码是给机器看的稳定标识符"
    assert "租约" in attempt["error_summary"] and "进程" in attempt["error_summary"], (
        f"账本要写明超时原因，实际是：{attempt['error_summary']!r}"
    )
    assert (attempt["prompt_tokens"], attempt["completion_tokens"]) == (0, 0)
    assert attempt["duration_ms"] == 0


def test_each_recovery_round_appends_exactly_one_ledger_row(alice):
    """每一轮交接恰好一笔账：两轮 → 两行，序号递增，不会把上一轮重复记。"""
    job_id = job_create(alice, "prd", None)
    started = utc_now()

    first = _claim(alice, job_id, now=started)
    assert job_recovery.recover_once(10, now=_expired_at(first)).requeued == 1
    second = _claim(alice, job_id, now=utc_now() + timedelta(minutes=30))
    assert job_recovery.recover_once(10, now=_expired_at(second)).requeued == 1

    attempts = _attempts(alice, job_id)
    assert [item["attempt_no"] for item in attempts] == [1, 2]
    assert _attempts(alice, job_id) == attempts, "再次读到同一份账本（没有重复行）"


# ---------------------------------------------------------------------------
# 三、AC3：凭证已被换掉的行不被误改（改到 0 行即跳过，不是报错）
# ---------------------------------------------------------------------------


def test_a_replaced_token_is_skipped_instead_of_updated(alice, caplog):
    """扫描之后这一行被回排队又被别人领走：恢复器手里那份死租约改到 0 行 → 跳过。

    这条钉住 fencing 的写法：判权用**扫描时读到的旧凭证**，不是「刚才查到的行」。
    七态之后「被别人接手」的实际路径是「恢复器先回排队，再由下一个执行者抢占」——
    无论哪条路径，扫描时那一份凭证都已经失效，必须一行都改不到。
    """
    job_id = job_create(alice, "prd", None)
    started = utc_now()
    first = _claim(alice, job_id, lease_owner="proc-a", now=started)
    later = _expired_at(first)

    candidates = job_recovery.scan(10, now=later)
    assert [candidate.job_id for candidate in candidates] == [job_id]
    assert candidates[0].lease_token == first.token

    # 窗口内这一行被另一路恢复器回排队、随后被 proc-b 领走：凭证与状态都变了。
    assert job_state.transition(
        job_state.JobEvent.REQUEUE,
        job_state.job_scope(alice, job_id),
        error_code=job_state.CODE_LEASE_EXPIRED,
        error_message="执行者进程已消失，租约到期未续",
        lease_fence=candidates[0].fence,
        now_text=later.isoformat(),
    ).applied
    second = _claim(
        alice,
        job_id,
        lease_owner="proc-b",
        now=later + timedelta(seconds=job_backoff.BACKOFF_TIMEOUT_SECONDS),
    )
    assert second.token != first.token

    with caplog.at_level(logging.INFO):
        outcome = job_recovery.recover_one(candidates[0])

    assert outcome.action == job_recovery.ACTION_SKIPPED
    assert outcome.rows == 0
    row = job_get(alice, job_id)
    assert row["status"] == "running"
    assert row["lease_token"] == second.token, "新执行者的租约不许被恢复器清掉"
    assert row["error_code"] == ""
    # 上一轮回排队记了一笔（那一笔是真发生的结论）；本次 0 行不记账。
    assert len(_attempts(alice, job_id)) == 1
    assert len(_discards(caplog)) == 1, "0 行要留可查的丢弃日志（与其它转换同一口径）"


def test_a_renewed_lease_is_not_stolen(alice, caplog):
    """凭证没换但到期时间被续租推后：同样改到 0 行（防止抢走正在跑的任务）。

    扫描读到的是「凭证 + 到期时间」两列；执行者在窗口里续了一次租（凭证不变、
    到期时间往后），拿旧到期时间去条件更新就一行都改不到——这是 fencing 比
    「只比凭证」更严的那一步。
    """
    job_id = job_create(alice, "prd", None)
    started = utc_now()
    lease = _claim(alice, job_id, lease_owner="proc-a", now=started)
    later = _expired_at(lease)
    candidate = job_recovery.scan(10, now=later)[0]

    renewed = job_claimer.renew(lease, now=later)
    assert renewed.token == lease.token

    with caplog.at_level(logging.INFO):
        outcome = job_recovery.recover_one(candidate)

    assert outcome.action == job_recovery.ACTION_SKIPPED
    assert job_get(alice, job_id)["lease_expire_at"] == renewed.expire_at
    assert _attempts(alice, job_id) == []
    assert len(_discards(caplog)) == 1


# ---------------------------------------------------------------------------
# 四、AC4：重启后不存在「永远进行中」的任务（reap_orphans 收编为 recover_once）
# ---------------------------------------------------------------------------


def test_startup_recovery_settles_every_job_that_was_executing(alice):
    """启动回收：进程刚起来时库里所有租约都属于死掉的执行者——包括还没到期的。

    造「崩溃前 1 秒领到、租约还剩 119 秒」两条：只扫过期租约的话它们要等两分钟
    才被动，那就还是「永远进行中」。启动档不看到期时间，一次收干净。
    判据：收完之后没有一条行还处于执行中（全部终态）、账本各记一笔、都领不走了。
    """
    first = job_create(alice, "prd", None)
    second = job_create(alice, "prompts", None)
    _claim(alice, first, lease_owner="proc-a")
    _claim(alice, second, lease_owner="proc-b")

    job_service.reap_orphans()

    for job_id in (first, second):
        row = job_get(alice, job_id)
        assert row["status"] == "failed", f"重启后仍有行声称自己『执行中』：{row}"
        assert row["error_code"] == "LEASE_EXPIRED"
        assert (row["lease_owner"], row["lease_token"], row["lease_expire_at"]) == ("", "", "")
        attempts = _attempts(alice, job_id)
        assert [item["error_code"] for item in attempts] == ["LEASE_EXPIRED"]
    assert job_claimer.claim_once(10, lease_owner="proc-c") == [], "终态的行不该再被领走"


def test_startup_recovery_reports_the_restart_for_a_row_never_claimed(alice):
    """从没被领过的残留 running 行：判失败，但原因写「服务重启中断」而不是「租约过期」。

    启动那一刻库里不可能有真正的执行者（单 worker 硬约束：`_RUNTIMES` 在进程内），
    所以没有租约的残留行同样是孤儿。它的原因不是「超时」——租约一次都没发过，
    说成过期是把排查方向带偏；旧口径（`INTERNAL_ERROR` +「服务重启，任务已中断」）
    在这里保留，账本同样记一笔。
    """
    job_id = job_create(alice, "prd", None)

    job_service.reap_orphans()

    row = job_get(alice, job_id)
    assert row["status"] == "failed"
    assert row["error_code"] == "INTERNAL_ERROR"
    assert row["error_message"] == job_recovery.MESSAGE_RESTART_INTERRUPTED
    attempts = _attempts(alice, job_id)
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == OUTCOME_FAILED
    assert attempts[0]["error_summary"] == job_recovery.MESSAGE_RESTART_INTERRUPTED
    assert job_claimer.claim_once(10, lease_owner="proc-c") == []


# ---------------------------------------------------------------------------
# 五、AC5：恢复器自身也守状态机规则，不越权改状态
# ---------------------------------------------------------------------------


def test_recovery_settles_a_cancelling_row_as_cancelled_not_failed(alice):
    """取消中的行由恢复器兜底收成已取消（AC3），绝不记成一次超时失败。

    用户按了停止之后，执行者卡在模型调用里没来确认；租约到期，恢复器接手。
    「谁按下停止」是用户的意图，所以结局必须是 `cancelled`——把它覆写成
    `failed + LEASE_EXPIRED` 会让统计与排查都读错（那看起来像模型坏了）。
    """
    job_id = job_create(alice, "prd", None)
    lease = _claim(alice, job_id)
    cancelled = job_state.transition(
        job_state.JobEvent.REQUEST_CANCEL,
        job_state.job_scope(alice, job_id),
        error_message="用户停止了生成",
    )
    assert cancelled.rows == 1
    assert job_get(alice, job_id)["status"] == "cancelling"

    report = job_recovery.recover_once(10, now=_expired_at(lease))

    assert report.scanned == 1
    assert report.cancelled == 1
    assert report.failed == 0
    row = job_get(alice, job_id)
    assert row["status"] == "cancelled"
    assert row["error_code"] == "", "已取消不是失败，不许带 LEASE_EXPIRED"
    assert (row["lease_owner"], row["lease_token"], row["lease_expire_at"]) == ("", "", "")
    attempts = _attempts(alice, job_id)
    assert [item["outcome"] for item in attempts] == [OUTCOME_CANCELLED]


def test_a_row_cancelled_mid_window_is_settled_as_cancelled_not_overwritten(alice, caplog):
    """扫描之后行进入取消中：恢复器的下一次扫描按**取消**（不是超时）接手它。

    恢复的候选集与去向都由扫描那一刻的状态决定：上一轮扫到的是「执行中」，但在这一轮
    收口之前用户按了停止，行已变成 `cancelling`——下一次扫描就把它归到取消兜底。
    写进去的结局是 `cancelled`，`error_code` 保持空串（不是 `LEASE_EXPIRED`）。
    """
    job_id = job_create(alice, "prd", None)
    lease = _claim(alice, job_id)
    later = _expired_at(lease)

    assert job_service.cancel_job(alice, job_id) is True
    assert job_get(alice, job_id)["status"] == "cancelling"

    # 用取消之后重新扫描到的候选（状态已是 cancelling）；旧候选的状态字段已过期。
    candidate = job_recovery.scan(10, now=later)[0]
    assert candidate.status == "cancelling"

    with caplog.at_level(logging.INFO):
        outcome = job_recovery.recover_one(candidate, now=later)

    assert outcome.action == job_recovery.ACTION_CANCELLED
    row = job_get(alice, job_id)
    assert row["status"] == "cancelled"
    assert row["error_code"] == ""
    assert [item["outcome"] for item in _attempts(alice, job_id)] == [OUTCOME_CANCELLED]


def test_recovery_module_only_writes_state_through_the_state_machine() -> None:
    """守卫：恢复器不越权改状态——所有落库的状态变更都必须经 `job_state.transition`。

    用 AST 找真实调用（注释/docstring 里提到不算）：恢复器可以读（扫候选、数账本），
    但不许自己调仓储的终态写（`finish` / `fail` / 取消系列 / 旧的 `reap_running`）。
    恢复器那几个条件写（回排队、判超时、判重启中断）都住在状态机里，由
    `JobEvent.REQUEUE` / `EXPIRE` / `FAIL` 三条转换承担。
    """
    path = (
        Path(__file__).resolve().parents[1] / "src" / "backend" / "services" / "job_recovery.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert "transition" in calls, "恢复器必须经 job_state.transition 改状态"
    forbidden = {
        "finish",
        "fail",
        "cancel_one",
        "cancel_running",
        "cancel_running_for_owner",
        "supersede_active",
        "reap_running",
        "reap",
        "requeue_lease",
        "fail_expired",
    }
    offenders = sorted(calls & forbidden)
    assert offenders == [], f"恢复器绕过了状态机直接改库：{offenders}"
