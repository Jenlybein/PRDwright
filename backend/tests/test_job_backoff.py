"""失败延迟退避（#88）：回排队之前先等一段时间，按错误类型区分。

## 这张票回答什么问题

一条任务失败之后回排队，如果下一个执行者立刻领走，连续的上游限流能在几十秒内把
第三次机会也烧掉——**不等，等于把「上游暂时忙」误判成「彻底失败」**。解法只是一列
`next_run_at`（迁移 `0006`）：抢占条件多一条「到期闸门」，未到期的行一行都改不到。

## 接缝（断言只架在这三处）

1. **`services/job_backoff`**（本票新增的纯函数模块）：错误码 → 退避秒数的**唯一**
   映射表（限流 30 秒 / 上游 5xx 10 秒 / 超时 5 秒）。AC1 的一半断言架在它上面：
   表只有这一处，改数字只改一处。
2. **`job_state.transition`**（#79 的唯一转换入口）：`REQUEUE` 事件把退避写进
   `next_run_at`，与清租约、`updated_at` 在同一条条件 UPDATE、同一个事务里
   （「回排队了但还能立刻被领」那个窗口不存在）。
3. **`job_claimer.claim` / `claimable`** 与 `job_recovery.recover_once`：
   - AC2：未到期的任务不被抢占（`claim` 改到 0 行、`claimable` 选不出来）；
   - AC3：排序规则「先过到期闸门，到期之后按 `updated_at` 排队」；
   - AC4：同一任务连续失败两次，每次失败之后要等的时间都等于退避值。

## 时间入口（AC5）

所有时刻都是**注入**的（`now=` / `now_text=` 参数，来自 `core/clock.py` 那一个入口），
`next_run_at` 也直接用相对时间写库（`dbsupport.connect` 的裸 SQL）。本文件里没有
一个 `sleep`——30 秒的退避在毫秒内演示完。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from conftest import job_create, job_get, register, users
from dbsupport import connect

from backend.core.clock import utc_now
from backend.core.errors import ErrorCode
from backend.repositories.generation_job_repo import GenerationJobRepository
from backend.core.engine import session_scope
from backend.services import job_backoff, job_claimer, job_recovery, job_state
from ticket_support import build_and_register, first_snapshot_event

# 租约时长（`job_claimer.DEFAULT_LEASE_SECONDS`）：用例只借它算「过期时刻」。
LEASE_SECONDS = job_claimer.DEFAULT_LEASE_SECONDS
# 恢复器回排队时带的码（超时那一路）。写成 `job_state` 的常量而不是字面量：
# 它是「回排队这类转换带什么码」的唯一来源。
TIMEOUT_CODE = job_state.CODE_LEASE_EXPIRED


def _alice_id() -> int:
    user = users.get_by_username("alice")
    assert user is not None
    return int(user["id"])


@pytest.fixture
def alice(client):
    """注册 alice 并返回其 id（generation_jobs.owner_id 有外键）。"""
    register(client)
    return _alice_id()


def _claim(owner_id: int, job_id: int, *, now: datetime):
    """领一条任务的执行权；领不到直接失败（用例前提：这一行被某个执行者领走了）。"""
    lease = job_claimer.claim(owner_id, job_id, lease_owner="proc-a", now=now)
    assert lease is not None, "用例前提：这一行在失败之前被某个执行者领走了"
    return lease


def _expired_at(lease, *, slack: int = 30) -> datetime:
    """这一轮租约已经过期的那个时刻（+30 秒余量，别卡在边界上）。"""
    return datetime.fromisoformat(lease.expire_at) + timedelta(seconds=slack)


def _advance(code: str, now: datetime) -> str:
    """期望的 `next_run_at`：注入的「现在」+ 该错误码的退避秒数（直接从映射表取）。"""
    return (now + timedelta(seconds=job_backoff.seconds_for(code))).isoformat()


def _write_row(job_id: int, **values: str) -> None:
    """直接改库造场景（AC5 允许「把 `next_run_at` 写成相对时间」）。

    不经过仓储：仓储就是被测对象，用它来摆场景等于用被测逻辑构造断言前提。
    """
    columns = ", ".join(f"{name}=%s" for name in values)
    with connect() as conn:
        conn.execute(
            f"UPDATE generation_jobs SET {columns} WHERE id=%s",
            (*values.values(), job_id),
        )
        conn.commit()


# ===========================================================================
# 一、AC1：错误码 → 退避秒数的映射**只有一处**（限流 30 / 上游 5xx 10 / 超时 5）
# ===========================================================================


def test_backoff_table_is_the_single_mapping():
    """三档退避秒数与它们的错误码**挤在一张表里**，不在调用方的多个 if 里。

    这张表是全仓唯一一处「哪个错等多久」：断言表本身，比断言某个调用点更可靠——
    将来加一类失败只改这里，调用方（状态机的 `REQUEUE` 分支）不需要知道任何数字。
    """
    assert job_backoff.BACKOFF_SECONDS == {
        # HTTP 429（限流）：仓库里 429 的稳定码就是 INVALID_REQUEST（见 quota_service）
        ErrorCode.INVALID_REQUEST.value: 30.0,
        # 上游 5xx / 传输抖动
        ErrorCode.LLM_UPSTREAM_ERROR.value: 10.0,
        # 超时（租约到期，恢复器回排队带的就是这个码）
        TIMEOUT_CODE: 5.0,
    }


def test_backoff_keys_are_all_stable_codes():
    """表里的键必须都是**真实存在的稳定错误码**（防手滑写错一个字符串）。

    按码值分类而不是按中文文案分类：文案是给人看的，改一个字就静默失效；
    码值是契约（`test_error_contract` 守着它）。这里把「表里的键都在契约里」也钉住。
    """
    known = {code.value for code in ErrorCode} | {
        job_state.CODE_CANCELLED,
        job_state.CODE_SUPERSEDED,
        job_state.CODE_LEASE_EXPIRED,
    }
    assert set(job_backoff.BACKOFF_SECONDS) <= known, "表里出现了不存在的错误码"


def test_unknown_code_falls_back_to_the_shortest_wait():
    """说不出类别的失败取最短一档：宁可早一点再试，也不要把能跑的任务压半分钟。"""
    assert job_backoff.DEFAULT_BACKOFF_SECONDS == 5.0
    assert job_backoff.seconds_for("") == 5.0
    assert job_backoff.seconds_for("SOMETHING_NEW") == 5.0


@pytest.mark.parametrize(
    ("error_code", "expected_seconds"),
    [
        (ErrorCode.INVALID_REQUEST.value, 30.0),
        (ErrorCode.LLM_UPSTREAM_ERROR.value, 10.0),
        (TIMEOUT_CODE, 5.0),
    ],
    ids=["限流-30秒", "上游5xx-10秒", "超时-5秒"],
)
def test_requeue_presses_the_backoff_of_its_failure_code(alice, error_code, expected_seconds):
    """回排队时 `next_run_at` = 失败时刻 + 该错误码的退避秒数（AC1 的写入侧）。

    三类码各有各的等待：限流 30 秒、上游 5xx 10 秒、超时 5 秒。判据是**行上的那一列**，
    不是调用方手里的变量——所以它能被下一个执行者的抢占条件直接看到。

    三个码走的是同一处映射表、同一条写入路径：本票能真实产生的是超时那一路
    （恢复器回排队）；另外两类由将来的「可重试失败」转换送进来（#84），
    但**退避写在哪、写多少**在这里已经全部定死。
    """
    job_id = job_create(alice, "prd", None)
    started = utc_now()
    _claim(alice, job_id, now=started)
    failed_at = started + timedelta(minutes=10)

    with session_scope() as session:
        lease = GenerationJobRepository(session).get(alice, job_id)
        assert lease is not None
        fence = job_state.LeaseFence(token=lease.lease_token, expire_at=lease.lease_expire_at)

    result = job_state.transition(
        job_state.JobEvent.REQUEUE,
        job_state.job_scope(alice, job_id),
        error_code=error_code,
        error_message="可重试失败",
        lease_fence=fence,
        now_text=failed_at.isoformat(),
    )

    assert result.applied, "回排队应当改到 1 行"
    row = job_get(alice, job_id)
    assert row is not None
    assert row["next_run_at"] == (failed_at + timedelta(seconds=expected_seconds)).isoformat()
    # 回排队本身仍是「排队中」：状态不变、租约清空（等下一次抢占）
    assert row["status"] == "running"
    assert row["lease_token"] == "" and row["lease_expire_at"] == ""


# ===========================================================================
# 二、AC2：未到期的任务不被抢占
# ===========================================================================


def test_a_backoff_row_stays_unclaimed_until_its_time_comes(alice):
    """退避未到期：`claim` 改到 0 行（不靠调用方自律，靠 SQL 的到期闸门）。

    超时那一路走真实链路：抢占 → 租约过期 → 恢复器回排队。回排队写下的 `next_run_at`
    是「过期那一刻 + 5 秒」，于是这一刻到 +4 秒都领不走，+5 秒之后才领得到。
    """
    job_id = job_create(alice, "prd", None)
    started = utc_now()
    lease = _claim(alice, job_id, now=started)
    died_at = _expired_at(lease)

    report = job_recovery.recover_once(10, now=died_at)
    assert report.requeued == 1
    due = died_at + timedelta(seconds=job_backoff.BACKOFF_TIMEOUT_SECONDS)

    row = job_get(alice, job_id)
    assert row is not None
    assert row["next_run_at"] == due.isoformat()

    # 未到期：领不到，且行上的租约仍是空的（那次尝试没留下任何占用）
    assert job_claimer.claim(alice, job_id, now=due - timedelta(seconds=1)) is None
    still = job_get(alice, job_id)
    assert still is not None
    assert still["lease_token"] == ""
    assert still["next_run_at"] == due.isoformat()

    # 到期那一刻起可以领走（边界取 `<=`，与判据一致）
    assert job_claimer.claim(alice, job_id, now=due) is not None


def test_the_queue_scan_also_skips_rows_that_are_not_due_yet(alice):
    """`claimable`（批量扫待领）同样看不到未到期的行——两个入口种口径。

    `claim_once` 的候选集来自 `claimable`：扫描侧漏了闸门，就会出现「挑出来了但
    条件写改到 0 行」的空转（不是正确性漏洞，但每次扫描都白跑一遍）。
    """
    ready = job_create(alice, "prd", None)
    waiting = job_create(alice, "prompts", None)
    moment = utc_now()
    _write_row(ready, next_run_at="")
    _write_row(waiting, next_run_at=(moment + timedelta(seconds=30)).isoformat())

    with session_scope() as session:
        candidates = GenerationJobRepository(session).claimable(
            now_text=moment.isoformat(), limit=10
        )

    assert (alice, ready) in candidates
    assert (alice, waiting) not in candidates


# ===========================================================================
# 三、AC3：排序规则固定为「先看是否到期，到期之后按 `updated_at` 排队」
# ===========================================================================


def test_queue_order_is_due_gate_then_updated_at(alice):
    """到期闸门在前、`updated_at` 排队在后（AC3 的唯一定义处：`_due_now` / `_queue_order`）。

    三件事一起钉住：

    1. 未到期的行**不是候选**（不论它多早建的）；
    2. 到期之后按 `updated_at` 升序（越早更新越靠前 = 越早回排队越早被领）；
    3. `created_at` / id 不参与排序——建得早不等于该先跑。
    """
    early = job_create(alice, "prd", None)  # id 最小、但 updated_at 被推到后面
    late = job_create(alice, "api_docs", None)
    waiting = job_create(alice, "prompts", None)
    moment = datetime(2030, 1, 1, 12, 0, 0)

    _write_row(
        early,
        updated_at=(moment - timedelta(seconds=10)).isoformat(),
        next_run_at="",
    )
    _write_row(
        late,
        updated_at=(moment - timedelta(seconds=30)).isoformat(),
        next_run_at="",
    )
    _write_row(
        waiting,
        updated_at=(moment - timedelta(seconds=60)).isoformat(),
        next_run_at=(moment + timedelta(seconds=30)).isoformat(),
    )

    def candidates(now: datetime) -> list[tuple[int, int]]:
        with session_scope() as session:
            return [
                (owner, job_id)
                for owner, job_id in GenerationJobRepository(session).claimable(
                    now_text=now.isoformat(), limit=10
                )
                if job_id in {early, late, waiting}
            ]

    # `late` 的 updated_at 更早 → 先排；`waiting` 尚未到期 → 不是候选
    assert candidates(moment) == [(alice, late), (alice, early)]

    # 到期之后 `waiting` 才进入队列，位置由它的 updated_at 决定（它最早 → 排最前）
    later = moment + timedelta(seconds=45)
    assert candidates(later) == [(alice, waiting), (alice, late), (alice, early)]


def test_a_requeued_row_goes_to_the_tail_of_the_due_queue(alice):
    """回排队会把 `updated_at` 推到现在 → 到期之后排在同优先级队尾。

    这就是 spec 说的「两套排序语义叠加」的实际表现：退避决定**能不能领**，
    `updated_at` 决定**到期之后谁先领**，两者各管一段、不打架。
    """
    first = job_create(alice, "prd", None)
    second = job_create(alice, "prompts", None)
    sentinel = datetime(2030, 1, 1, 12, 0, 0)
    _write_row(first, updated_at=(sentinel - timedelta(minutes=2)).isoformat(), next_run_at="")
    _write_row(second, updated_at=(sentinel - timedelta(minutes=1)).isoformat(), next_run_at="")

    # 把 `first` 回排队一次：`updated_at` 推到 sentinel+5，退避到 sentinel+10
    with session_scope() as session:
        GenerationJobRepository(session).requeue_lease(
            alice,
            first,
            lease_token="",
            lease_expire_at="",
            now_text=(sentinel + timedelta(seconds=5)).isoformat(),
            next_run_at=(sentinel + timedelta(seconds=10)).isoformat(),
        )

    due = sentinel + timedelta(seconds=30)
    with session_scope() as session:
        candidates = [
            (owner, job_id)
            for owner, job_id in GenerationJobRepository(session).claimable(
                now_text=due.isoformat(), limit=10
            )
            if job_id in {first, second}
        ]
    # 回排队过的 `first` 的 updated_at 更晚 → 排到队尾
    assert candidates == [(alice, second), (alice, first)]


# ===========================================================================
# 四、AC4：同一任务连续失败的时间间隔符合退避设置
# ===========================================================================


def test_two_consecutive_failures_each_wait_the_full_backoff(alice):
    """连续失败两次：每一次回排队都重新等满 5 秒，而不是「第一次等了、第二次立刻重试」。

    时长链条（全部由注入的时刻算出，不靠真实时间流逝）：

    1. `t0` 抢占，租约 120 秒；
    2. 过期后恢复器在 `t0+150` 回排队 → 下一次可领时刻 = `t0+155`（+5 秒退避）；
    3. `t0+155` 再抢占（这一轮租约到 `t0+275`）；
    4. 过期后恢复器在 `t0+305` 再回排队 → 下一次可领时刻 = `t0+310`（同样 +5 秒）。

    断言「每次失败到下一次可领之间恰好一个退避值」，以及「两次失败之间至少一个退避值」
    ——后者是「不等会把机会烧光」的反面：不许出现「回排队了、立刻又失败」的零间隔。
    """
    job_id = job_create(alice, "prd", None)
    started = utc_now()
    first_lease = _claim(alice, job_id, now=started)

    first_failure = _expired_at(first_lease)
    assert job_recovery.recover_once(10, now=first_failure).requeued == 1
    first_due = first_failure + timedelta(seconds=job_backoff.BACKOFF_TIMEOUT_SECONDS)
    row = job_get(alice, job_id)
    assert row is not None
    assert row["next_run_at"] == first_due.isoformat()

    second_lease = _claim(alice, job_id, now=first_due)
    second_failure = _expired_at(second_lease)
    assert job_recovery.recover_once(10, now=second_failure).requeued == 1
    second_due = second_failure + timedelta(seconds=job_backoff.BACKOFF_TIMEOUT_SECONDS)
    row = job_get(alice, job_id)
    assert row is not None
    assert row["next_run_at"] == second_due.isoformat()

    # 每次失败之后等的时间 = 退避值（一秒钟都不少）
    assert first_due - first_failure == timedelta(seconds=5)
    assert second_due - second_failure == timedelta(seconds=5)
    # 连续两次失败之间隔着「上次退避 + 一整轮租约」，不是零间隔重试
    assert second_failure - first_due >= timedelta(seconds=job_backoff.BACKOFF_TIMEOUT_SECONDS)


def test_a_rate_limited_task_waits_thirty_seconds_between_retries(alice):
    """限流那一路：两次回排队各等 30 秒，中间没有「立刻再来一次」的窗口。

    这类失败本票还没有生产者（「可重试失败」转换属于 #84），但退避**必须**已经能对它
    生效：状态机的 `REQUEUE` 只认错误码，不认代码从哪来。用例直接调转换来钉住这件事。
    """
    job_id = job_create(alice, "prd", None)
    started = utc_now()
    lease = _claim(alice, job_id, now=started)

    def requeue_once(failed_at: datetime) -> str:
        with session_scope() as session:
            row = GenerationJobRepository(session).get(alice, job_id)
            assert row is not None
            fence = job_state.LeaseFence(
                token=row.lease_token, expire_at=row.lease_expire_at
            )
        result = job_state.transition(
            job_state.JobEvent.REQUEUE,
            job_state.job_scope(alice, job_id),
            error_code=ErrorCode.INVALID_REQUEST.value,
            error_message="上游限流（HTTP 429）",
            lease_fence=fence,
            now_text=failed_at.isoformat(),
        )
        assert result.applied
        refreshed = job_get(alice, job_id)
        assert refreshed is not None
        return refreshed["next_run_at"]

    first_failure = started + timedelta(minutes=5)
    first_due = requeue_once(first_failure)
    assert first_due == _advance(ErrorCode.INVALID_REQUEST.value, first_failure)
    assert job_claimer.claim(alice, job_id, now=datetime.fromisoformat(first_due) - timedelta(seconds=1)) is None

    second_lease = job_claimer.claim(alice, job_id, now=datetime.fromisoformat(first_due))
    assert second_lease is not None
    second_failure = _expired_at(second_lease)
    second_due = requeue_once(second_failure)
    assert second_due == _advance(ErrorCode.INVALID_REQUEST.value, second_failure)
    assert datetime.fromisoformat(second_due) - datetime.fromisoformat(first_due) > timedelta(
        seconds=job_backoff.BACKOFF_RATE_LIMITED_SECONDS
    )


# ===========================================================================
# 五、AC5：时间入口可注入，用例不靠 sleep 推进
# ===========================================================================


def test_backoff_time_is_a_pure_function_of_the_injected_clock():
    """`next_run_at` 只由「注入的现在 + 表里的秒数」算出：同一个输入永远同一个结果。

    生产不传 `now_text` 时取 `core/clock.utc_now_text()`——时间只有那一个入口，
    退避模块自己不调 `datetime.now()`，于是本文件一个 `sleep` 都不需要。
    """
    moment = "2030-01-01T00:00:00"
    assert job_backoff.next_run_at_text(ErrorCode.INVALID_REQUEST.value, now_text=moment) == (
        "2030-01-01T00:00:30"
    )
    assert job_backoff.next_run_at_text(ErrorCode.LLM_UPSTREAM_ERROR.value, now_text=moment) == (
        "2030-01-01T00:00:10"
    )
    assert job_backoff.next_run_at_text(TIMEOUT_CODE, now_text=moment) == "2030-01-01T00:00:05"
    # 不传「现在」时才回落到唯一时钟入口，且仍是合法 ISO 文本
    assert datetime.fromisoformat(job_backoff.next_run_at_text(TIMEOUT_CODE)) > datetime.fromisoformat(
        utc_now().isoformat()
    )


def test_the_due_judgement_follows_the_injected_moment_not_the_wall_clock(alice):
    """到期判据看的是**注入的时刻**（以及库里存的那一列），不是「过了多久」。

    `next_run_at` 被写成相对真实时间 +60 秒，抢占用的「现在」却直接给 +61 秒：
    `claim` 必须领到——它证明判据完全是「注入时刻 vs 库里的列」的字符串比较，
    与真实流逝无关（AC5 的可演示性）。
    """
    job_id = job_create(alice, "prd", None)
    wall = utc_now()
    _write_row(
        job_id,
        next_run_at=(wall + timedelta(seconds=60)).isoformat(),
        lease_token="",
        lease_expire_at="",
    )

    assert job_claimer.claim(alice, job_id, now=wall + timedelta(seconds=59)) is None
    lease = job_claimer.claim(alice, job_id, now=wall + timedelta(seconds=61))
    assert lease is not None


# ===========================================================================
# 六、周边：退避期间仍可取消（前端「取消仍可用」的后端依据）、快照带等待信息
# ===========================================================================


def test_cancelling_a_waiting_job_still_works_immediately(alice):
    """退避期间用户按停止：立刻落 `failed + CANCELLED`，不用等退避到期。

    取消不是「抢占」：它要的是「这条任务别再跑了」，因此不受到期闸门约束
    （判据与 `claim` 不同）。前端在等待态仍显示取消入口，依据就是这一条。
    """
    job_id = job_create(alice, "prd", None)
    started = utc_now()
    lease = _claim(alice, job_id, now=started)
    failed_at = _expired_at(lease)
    assert job_recovery.recover_once(10, now=failed_at).requeued == 1

    result = job_state.transition(
        job_state.JobEvent.CANCEL,
        job_state.job_scope(alice, job_id),
        error_code=job_state.CODE_CANCELLED,
        error_message="方案已删除，任务已取消",
    )

    assert result.applied
    row = job_get(alice, job_id)
    assert row is not None
    assert row["status"] == "failed"
    assert row["error_code"] == job_state.CODE_CANCELLED


def test_the_db_snapshot_tells_the_frontend_when_the_retry_comes(alice):
    """DB 兜底快照带 `next_run_at`：前端据此显示「等待重试，约 xx 秒后」。

    等待态的任务**没有运行时**（执行者已经不在场），所以订阅者只能走 DB 兜底快照
    （`first_snapshot_event` 正是那条路）。快照里没有这一列，界面只能无限转圈——
    这正是票面要求「排队中要能显示等待原因」的数据来源。
    """
    registered = build_and_register(owner_id=alice, solution_id=None, kind="prd", request={})
    due = utc_now() + timedelta(seconds=30)
    _write_row(registered.job_id, next_run_at=due.isoformat())

    snapshot = first_snapshot_event(alice, registered.job_id)

    assert snapshot.data["next_run_at"] == due.isoformat()
    assert snapshot.data["status"] == "running", "等待重试不是失败，状态仍是执行中/排队中"


def test_a_job_that_never_failed_carries_an_empty_next_run_at(alice):
    """没有退避过的任务这一列是空串 = 立即到期（不是 NULL、也不是任意时间）。"""
    job_id = job_create(alice, "prd", None)
    row = job_get(alice, job_id)
    assert row is not None
    assert row["next_run_at"] == ""
    assert job_claimer.claim(alice, job_id, now=utc_now()) is not None
