"""抢占 + 租约 + 动作点续租（#82）：执行权是数据库里一条**会到期**的租约。

## 为什么要这张票

以前「谁在执行」是进程内存里的一个标志：多开一个进程，两份内存各说各话；进程崩了，
那一行仍是 `running`，没人知道它其实没人在跑。改成数据库里的租约之后，执行权由
**三件事**共同表达——`lease_owner`（谁领的，仅供排查）、`lease_token`（本轮凭证，
参与判权）、`lease_expire_at`（到期时间）——于是「两个进程抢同一行」有一个唯一裁判，
「领走的人崩了」有了到期这条出路。

## 接缝（断言只架在这三处）

1. **`job_claimer`**（本票新增的公开动作）：`claim` / `claim_once` / `renew`。
   判据是「改到几行」：改到 1 行才算领到，改到 0 行说明别人先领了或租约还没到期。
2. **`job_state.transition`**（#79 的唯一转换入口）：抢占是「排队中 → 执行中」的转换，
   走 `JobEvent.CLAIM`；收尾回写带「状态 + 凭证 + 未过期」三条件，改到 0 行 = 本次结果
   被丢弃（只留一条丢弃日志，不重试、不重新排队）。
3. **主接缝**（造单 → 登记 → 派发 → 订阅，`ticket_support`）：单进程仍**立刻**派发；
   同一个既领取过的任务不会再被派发第二次。

「排队中」在这张票里是「`status='running'` 且**没有有效租约**」——七态（含显式的
`pending`）是 #84 的事，本票不提前引入第四个状态值，前端契约一个字不变。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timedelta

import pytest
from conftest import job_create, job_get, register, users
from fakes import ScriptedLLM

from backend.core.clock import utc_now
from backend.core.contracts import StreamEvent
from backend.core.engine import session, session_scope
from backend.repositories.generation_job_repo import GenerationJobRepository
from backend.repositories.job_attempt_repo import JobAttemptRepository
from backend.services import job_backoff, job_claimer, job_state
from backend.services.stream_reply import astream_text
from ticket_support import run_ticket_async

LEASE_SECONDS = 120


def _alice_id() -> int:
    user = users.get_by_username("alice")
    assert user is not None
    return int(user["id"])


@pytest.fixture
def alice(client):
    """注册 alice 并返回其 id（generation_jobs.owner_id 有外键）。"""
    register(client)
    return _alice_id()


def _payload(draft: str, usage: str = '{"total_calls": 1}') -> job_state.FinishPayload:
    return job_state.FinishPayload(
        draft_content=draft,
        completed_sections=[],
        usage=usage,
        pending_items=[],
    )


def _events(caplog, name: str) -> list:
    return [record for record in caplog.records if getattr(record, "event", "") == name]


def _attempts(owner_id: int, job_id: int) -> list[dict]:
    """读该任务的账本（经仓储的读方法）。租约失效时不许凭空多出一行账。"""
    with session_scope() as session_scope_db:
        rows = JobAttemptRepository(session_scope_db).list_by_job(owner_id, job_id)
        return [row.model_dump() for row in rows]


def _expire(lease: job_claimer.Lease, seconds: int = LEASE_SECONDS + 30) -> datetime:
    """模拟「这一轮的执行者卡住了、租约已经过期」：返回过期的那个时刻。"""
    return datetime.fromisoformat(lease.expire_at) + timedelta(seconds=seconds)


def _take_over(
    owner_id: int,
    job_id: int,
    lease: job_claimer.Lease,
    *,
    lease_owner: str = "proc-b",
    now: datetime | None = None,
) -> job_claimer.Lease:
    """模拟「恢复器回排队 → 下一个执行者领走」这一次交接（七态，票 #84）。

    三态里可以直接抢一条「过期 running」；七态之后这条路收口到恢复器：过期租约先经
    `REQUEUE` 落回 `pending`（记一笔超时账），再由下一个执行者 `claim` 领走。用例沿用
    真实的交接路径，而不是走一条生产里已经不存在的捷径。
    """
    moment = now or _expire(lease)
    row = job_get(owner_id, job_id)
    assert job_state.transition(
        job_state.JobEvent.REQUEUE,
        job_state.job_scope(owner_id, job_id),
        error_code=job_state.CODE_LEASE_EXPIRED,
        error_message="执行者进程已消失，租约到期未续",
        lease_fence=job_state.LeaseFence(
            token=row["lease_token"], expire_at=row["lease_expire_at"]
        ),
        now_text=moment.isoformat(),
    ).applied
    due = moment + timedelta(seconds=job_backoff.BACKOFF_TIMEOUT_SECONDS)
    next_lease = job_claimer.claim(owner_id, job_id, lease_owner=lease_owner, now=due)
    assert next_lease is not None, "用例前提：交接之后这一行应当能被重新领走"
    return next_lease


# ---------------------------------------------------------------------------
# 一、AC2：执行权含三件事（谁领的 / 本轮凭证 / 到期时间）
# ---------------------------------------------------------------------------


def test_claim_writes_owner_token_and_expiry(alice):
    """抢占成功：三个字段一起落到行上，缺一个都不算「我有执行权」。

    - `lease_owner`：谁领的，**仅供排查**（同名进程再来一轮也不会被认成同一轮）；
    - `lease_token`：本轮凭证，逐轮更换，参与判权；
    - `lease_expire_at`：到期时间，默认 120 秒后（动作点续租靠它把运行拉长）。
    """
    job_id = job_create(alice, "prd", None)
    started = utc_now()

    lease = job_claimer.claim(alice, job_id, lease_owner="proc-a", now=started)

    assert lease is not None
    row = job_get(alice, job_id)
    assert row["lease_owner"] == "proc-a"
    assert row["lease_token"] == lease.token
    assert lease.token and len(lease.token) >= 16, "凭证必须是逐轮随机的串，不是常量"
    assert datetime.fromisoformat(row["lease_expire_at"]) == started + timedelta(
        seconds=LEASE_SECONDS
    )


def test_each_round_gets_a_fresh_token_so_owner_name_cannot_reuse_rights(alice):
    """凭证逐轮更换；**判权看凭证不看归属名**——同名进程的上一轮也不能再写。

    这条直说 AC2 的分工：`lease_owner` 是「出问题时问谁」，`lease_token` 才是裁判。
    若判权用归属名（两个进程同名），「旧执行者的晚到结果」就写得进去。
    """
    job_id = job_create(alice, "prd", None)
    first = job_claimer.claim(alice, job_id, lease_owner="同一个进程")
    assert first is not None

    later = _expire(first)
    second = _take_over(alice, job_id, first, lease_owner="同一个进程", now=later)
    assert second.token != first.token, "换一轮必须换凭证"

    stale = job_state.transition(
        job_state.JobEvent.SUCCEED,
        job_state.job_scope(alice, job_id),
        payload=_payload("上一轮的结果"),
        lease_token=first.token,
        now_text=later.isoformat(),
    )
    assert stale.rows == 0
    assert job_get(alice, job_id)["draft_content"] == ""


# ---------------------------------------------------------------------------
# 二、AC1：两个并发抢占同一行，只有一个改到 1 行
# ---------------------------------------------------------------------------


def test_claiming_a_held_lease_returns_zero_rows(alice):
    """租约还在有效期内时，第二次抢占改到 0 行（连凭证都不会被换掉）。"""
    job_id = job_create(alice, "prd", None)
    first = job_claimer.claim(alice, job_id, lease_owner="proc-a")
    assert first is not None

    second = job_claimer.claim(alice, job_id, lease_owner="proc-b")

    assert second is None, "有效租约还在，第二个执行者必须改到 0 行"
    row = job_get(alice, job_id)
    assert row["lease_owner"] == "proc-a"
    assert row["lease_token"] == first.token


def test_claim_is_decided_by_one_conditional_update_under_a_held_row_lock(alice):
    """A 抢到但**还没提交**时，B 的抢占会排队；A 提交后 B 的条件已不成立 → 0 行。

    这条钉的是「判断与修改压进同一条 UPDATE」：B 的条件是数据库在改之前重新判的，
    所以它看到的不是 A 抢占之前的那份快照。若改成「先查有没有人领、再写」，
    B 会用自己读到的旧事实把 A 的凭证覆盖掉——那正是两个执行者同时以为自己领到了。
    """
    job_id = job_create(alice, "prd", None)
    later = utc_now() + timedelta(seconds=LEASE_SECONDS)
    unlocked = threading.Event()
    finished = threading.Event()
    outcome: dict[str, object] = {}
    errors: list[BaseException] = []

    def contender() -> None:
        try:
            unlocked.wait(5)
            outcome["lease"] = job_claimer.claim(alice, job_id, lease_owner="proc-b")
        except BaseException as exc:  # noqa: BLE001 - 线程里什么都可能抛，全都要收回来
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=contender, name="second-claimer")
    thread.start()
    try:
        with session() as db:
            repo = GenerationJobRepository(db)
            # A：抢占成功，但**事务一直开着**（行锁在手里）
            rows = repo.claim(
                alice,
                job_id,
                lease_owner="proc-a",
                lease_token="token-of-a",
                expire_at=later.isoformat(),
                now_text=utc_now().isoformat(),
            )
            assert rows == 1
            unlocked.set()
            time.sleep(0.3)  # 给 B 足够时间真正撞上行锁、进入等待
            assert not finished.is_set(), "抢占同一行却没有排队：行锁没生效"
            db.commit()
    finally:
        thread.join(10)

    assert errors == [], f"抢占线程抛了异常（应当是排队而不是报错）：{errors!r}"
    assert finished.wait(5)
    assert outcome["lease"] is None, "A 先提交，B 必须改到 0 行"
    row = job_get(alice, job_id)
    assert row["lease_owner"] == "proc-a"
    assert row["lease_token"] == "token-of-a"


def test_two_processes_racing_for_one_row_have_exactly_one_winner(alice):
    """真并发：两个线程同时抢同一行，多轮里每轮都**恰好一个**成功、另一个 0 行。"""
    for round_no in range(5):
        job_id = job_create(alice, "prd", None)
        barrier = threading.Barrier(2)
        results: dict[str, object] = {}
        errors: list[BaseException] = []

        def worker(name: str) -> None:
            try:
                barrier.wait(5)
                results[name] = job_claimer.claim(alice, job_id, lease_owner=name)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(f"proc-{index}",)) for index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)

        assert errors == [], f"第 {round_no + 1} 轮并发抢占抛异常：{errors!r}"
        winners = [name for name, lease in results.items() if lease is not None]
        assert len(winners) == 1, (
            f"第 {round_no + 1} 轮应当恰好一个执行者改到 1 行，实际赢家 {winners}"
        )
        row = job_get(alice, job_id)
        assert row["lease_owner"] == winners[0]
        assert row["lease_token"] == results[winners[0]].token


def test_scanning_the_queue_hands_each_row_to_one_owner_at_a_time(alice):
    """队列入口 `claim_once`：一次扫描把**还没有人领**的行领走，第二轮扫描领不到。"""
    first = job_create(alice, "prd", None)
    second = job_create(alice, "ai_docs", None)

    claimed = job_claimer.claim_once(10, lease_owner="proc-a")

    assert [lease.job_id for lease in claimed] == [first, second]
    assert job_get(alice, first)["lease_owner"] == "proc-a"
    assert job_claimer.claim_once(10, lease_owner="proc-b") == [], "已领走的行不会再被领一次"


# ---------------------------------------------------------------------------
# 三、AC4：旧凭证的晚到结果改到 0 行，只留丢弃日志，不重试、不重新排队
# ---------------------------------------------------------------------------


def test_late_success_from_a_stale_token_is_discarded(alice, caplog):
    """执行权被接手之后，原执行者「成功了」也写不进去：0 行 + 丢弃日志。

    丢弃之后**什么都不许发生**：不重试、不重新排队（行仍是新执行者的 running）、
    不记账本（一次失去资格的写入不是一次执行结论）。
    """
    job_id = job_create(alice, "prd", None)
    first = job_claimer.claim(alice, job_id, lease_owner="proc-a")
    assert first is not None
    later = _expire(first)
    second = _take_over(alice, job_id, first, now=later)

    with caplog.at_level(logging.INFO):
        late = job_state.transition(
            job_state.JobEvent.SUCCEED,
            job_state.job_scope(alice, job_id),
            payload=_payload("A 的晚到结果"),
            lease_token=first.token,
            now_text=later.isoformat(),
        )

    assert late.rows == 0
    assert late.discarded is True
    row = job_get(alice, job_id)
    assert row["status"] == "running", "晚到的结果不许把这一行改成终态"
    assert row["draft_content"] == ""
    assert row["usage"] is None
    assert row["lease_token"] == second.token, "执行权仍在 B 手里"
    discarded = _events(caplog, "job.transition_discarded")
    assert len(discarded) == 1
    assert discarded[0].job_id == job_id
    # 账本只有「恢复器回排队」那一笔；晚到的写入不是执行结论，不许记账本。
    attempts = _attempts(alice, job_id)
    assert [item["error_code"] for item in attempts] == ["LEASE_EXPIRED"]


def test_late_failure_from_a_stale_token_is_discarded(alice, caplog):
    """旧凭证连「失败」也写不进去：否则失去资格的进程能凭空把别人的任务判死。"""
    job_id = job_create(alice, "prd", None)
    first = job_claimer.claim(alice, job_id, lease_owner="proc-a")
    assert first is not None
    later = _expire(first)
    second = _take_over(alice, job_id, first, now=later)

    with caplog.at_level(logging.INFO):
        late = job_state.transition(
            job_state.JobEvent.FAIL,
            job_state.job_scope(alice, job_id),
            payload=_payload("半截正文"),
            error_code="LLM_UPSTREAM_ERROR",
            error_message="A 的晚到失败",
            lease_token=first.token,
            now_text=later.isoformat(),
        )

    assert late.rows == 0
    row = job_get(alice, job_id)
    assert row["status"] == "running"
    assert row["error_code"] == ""
    assert row["lease_token"] == second.token
    assert len(_events(caplog, "job.transition_discarded")) == 1
    assert [item["error_code"] for item in _attempts(alice, job_id)] == ["LEASE_EXPIRED"]


def test_an_expired_lease_cannot_write_even_before_someone_takes_over(alice, caplog):
    """租约过期了但还没人接手：同样写不进去（到期时间本身就是一条判据）。"""
    job_id = job_create(alice, "prd", None)
    lease = job_claimer.claim(alice, job_id, lease_owner="proc-a")
    assert lease is not None
    later = _expire(lease)

    with caplog.at_level(logging.INFO):
        late = job_state.transition(
            job_state.JobEvent.SUCCEED,
            job_state.job_scope(alice, job_id),
            payload=_payload("过期之后才到的结果"),
            lease_token=lease.token,
            now_text=later.isoformat(),
        )

    assert late.rows == 0
    assert job_get(alice, job_id)["status"] == "running"
    assert len(_events(caplog, "job.transition_discarded")) == 1


def test_this_rounds_token_writes_the_result_once(alice):
    """本轮凭证还在有效期内时，收尾照常改到 1 行（丢弃不是常态，别把闸门焊死）。"""
    job_id = job_create(alice, "prd", None)
    lease = job_claimer.claim(alice, job_id, lease_owner="proc-a")
    assert lease is not None

    result = job_state.transition(
        job_state.JobEvent.SUCCEED,
        job_state.job_scope(alice, job_id),
        payload=_payload("本轮结果"),
        lease_token=lease.token,
    )

    assert result.rows == 1
    row = job_get(alice, job_id)
    assert row["status"] == "succeeded"
    assert row["draft_content"] == "本轮结果"
    assert row["lease_token"] == "", "收尾之后这一轮的执行权要交还（清租约）"


# ---------------------------------------------------------------------------
# 四、AC5：单任务跑十几分钟，租约不中途失效（动作点续租）
# ---------------------------------------------------------------------------


def test_renewal_pushes_the_expiry_forward(alice):
    """续租把到期时间往后推；凭证不变（还是这一轮）。"""
    job_id = job_create(alice, "prd", None)
    started = utc_now()
    lease = job_claimer.claim(alice, job_id, lease_owner="proc-a", now=started)
    assert lease is not None

    renewed = job_claimer.renew(lease, now=started + timedelta(minutes=1))

    assert renewed.token == lease.token
    assert datetime.fromisoformat(renewed.expire_at) == started + timedelta(
        minutes=1, seconds=LEASE_SECONDS
    )
    assert job_get(alice, job_id)["lease_expire_at"] == renewed.expire_at


def test_a_fifteen_minute_run_never_loses_its_right_to_settle(alice):
    """AC5：15 分钟、每 60 秒一个动作点，续租一次 → 收尾时凭证仍然有效。

    时间用注入的 `now` 走：租约 120 秒 vs 任务 15 分钟，真等一遍是 15 分钟，
    而这里 **一次续租都不能少**（去掉任意一次，下一次续租/收尾就会撞上过期）。
    """
    job_id = job_create(alice, "prd", None)
    started = utc_now()
    lease = job_claimer.claim(alice, job_id, lease_owner="proc-a", now=started)
    assert lease is not None

    for minute in range(1, 16):  # 第 1..15 分钟：每次模型调用前续一次
        lease = job_claimer.renew(lease, now=started + timedelta(minutes=minute))

    settled_at = started + timedelta(minutes=15)
    result = job_state.transition(
        job_state.JobEvent.SUCCEED,
        job_state.job_scope(alice, job_id),
        payload=_payload("跑完的长任务"),
        lease_token=lease.token,
        now_text=settled_at.isoformat(),
    )

    assert result.rows == 1, "15 分钟的长任务不该在收尾时才发现租约早过期了"
    assert job_get(alice, job_id)["status"] == "succeeded"


def test_without_renewal_the_same_fifteen_minute_run_is_discarded(alice, caplog):
    """反证：同样的 15 分钟**一次都不续**，收尾就是 0 行——上面那条不是白给的。"""
    job_id = job_create(alice, "prd", None)
    started = utc_now()
    lease = job_claimer.claim(alice, job_id, lease_owner="proc-a", now=started)
    assert lease is not None

    with caplog.at_level(logging.INFO):
        result = job_state.transition(
            job_state.JobEvent.SUCCEED,
            job_state.job_scope(alice, job_id),
            payload=_payload("没人续租的结果"),
            lease_token=lease.token,
            now_text=(started + timedelta(minutes=15)).isoformat(),
        )

    assert result.rows == 0
    assert job_get(alice, job_id)["status"] == "running"
    assert len(_events(caplog, "job.transition_discarded")) == 1


def test_renewal_stops_working_once_the_lease_was_taken_over(alice):
    """续租失败 = 已失去执行权（被接手了），抛一个明确的错误让链路停。"""
    job_id = job_create(alice, "prd", None)
    first = job_claimer.claim(alice, job_id, lease_owner="proc-a")
    assert first is not None
    later = _expire(first)
    _take_over(alice, job_id, first, now=later)

    with pytest.raises(job_claimer.LeaseLostError):
        job_claimer.renew(first, now=later)


# ---------------------------------------------------------------------------
# 五、AC3：续租失败时执行者立刻停止，不再写任何结果
# ---------------------------------------------------------------------------


def test_a_model_call_raises_lease_lost_before_it_starts(alice):
    """每次模型调用前续租；凭证已失效时**调用都发不出去**，直接抛「已失去执行权」。"""
    job_id = job_create(alice, "prd", None)
    lease = job_claimer.claim(alice, job_id, lease_owner="proc-a")
    assert lease is not None
    later = _expire(lease)
    _take_over(alice, job_id, lease, now=later)
    llm = ScriptedLLM(["不该出现的一段"])

    async def scenario() -> str:
        with job_claimer.use_lease(job_claimer.LeaseGuard(lease)):
            return "".join([text async for text in astream_text(llm, [])])

    with pytest.raises(job_claimer.LeaseLostError):
        asyncio.run(scenario())
    assert llm.calls == [], "失去执行权之后不该再发出模型调用"


def test_job_stops_and_writes_nothing_when_the_lease_is_lost_mid_run(client, alice, caplog):
    """主接缝：任务跑到一半被接手 → 下一次模型调用前续租失败 → 立刻停，什么都不写。

    三件事一起成立才算「立刻停止、不再写任何结果」：
    1. 库里那一行**一动不动**（既不是 succeeded 也不是 failed，没有被写正文/用量）；
    2. 没有 `run_summary`（没结论就没有回执）；账本上只有恢复器那一笔超时账
       （那是交接时真实发生的结论，不是这一支晚到的写入）；
    3. 留一条 `job.lease_lost`，说明「是我失去资格了，不是上游出错」。
    """
    llm = ScriptedLLM(["第一段", "第二段"])
    taken: dict[str, int] = {}

    async def stream(kind, request):
        # 第一次模型调用：动作点续租成功，正常产出
        async for text in astream_text(llm, []):
            yield StreamEvent(type="chunk", content=text, data={})
        # 第二次模型调用：此时执行权已被另一个执行者接手（见 on_event）
        async for text in astream_text(llm, []):
            yield StreamEvent(type="chunk", content=text, data={})
        yield StreamEvent(type="done", content="", data={"reply": "# 正文"})

    def on_event(event: StreamEvent) -> None:
        if event.type != "chunk" or "taken" in taken:
            return
        # 另一个执行者接手：等价于恢复器在租约过期后回排队、再由下一个执行者领走。
        # 「现在」推到这一轮租约到期之后，一行就到手了（不需要等真的过 120 秒）。
        taken["taken"] = 1
        row = job_get(alice, taken["job_id"])
        assert job_state.transition(
            job_state.JobEvent.REQUEUE,
            job_state.job_scope(alice, taken["job_id"]),
            error_code=job_state.CODE_LEASE_EXPIRED,
            error_message="执行者进程已消失，租约到期未续",
            lease_fence=job_state.LeaseFence(
                token=row["lease_token"], expire_at=row["lease_expire_at"]
            ),
            now_text=(utc_now() + timedelta(minutes=10)).isoformat(),
        ).applied
        assert job_claimer.claim(
            alice,
            taken["job_id"],
            lease_owner="proc-b",
            now=utc_now() + timedelta(minutes=10, seconds=job_backoff.BACKOFF_TIMEOUT_SECONDS),
        ) is not None

    async def scenario():
        produced = asyncio.Event()

        async def source(kind, request):
            async for event in stream(kind, request):
                yield event
            produced.set()

        def remember(job_id: int) -> None:
            taken["job_id"] = job_id

        return await run_ticket_async(
            owner_id=alice,
            solution_id=None,
            kind="prd",
            request={},
            event_stream_source=source,
            before_open=remember,
            on_event=on_event,
        )

    with caplog.at_level(logging.INFO):
        registered, events = asyncio.run(scenario())

    row = job_get(alice, registered.job_id)
    assert row["status"] == "running", "失去执行权之后不许再写终态"
    assert row["draft_content"] == ""
    assert row["usage"] is None
    assert row["lease_owner"] == "proc-b", "执行权还在新执行者手里"
    assert not [event for event in events if event.type == "run_summary"], (
        "没有结论就不该有回执"
    )
    assert [item["error_code"] for item in _attempts(alice, registered.job_id)] == [
        "LEASE_EXPIRED"
    ]
    lost = _events(caplog, "job.lease_lost")
    assert len(lost) == 1
    assert lost[0].job_id == registered.job_id


# ---------------------------------------------------------------------------
# 六、登记 → 抢占 → 派发：单进程仍立刻跑；同一个任务只会被派发一次
# ---------------------------------------------------------------------------


def test_registered_job_is_claimed_before_it_is_dispatched(alice):
    """登记之后**当场抢占再派发**（不把即时 UX 拖成「等轮询」）：行上带着本轮凭证跑完。"""
    produced = asyncio.Event()

    async def stream(kind, request):
        yield StreamEvent(type="chunk", content="在途", data={})
        produced.set()
        yield StreamEvent(type="done", content="", data={"reply": "# 正文"})

    async def scenario():
        return await run_ticket_async(
            owner_id=alice,
            solution_id=None,
            kind="prd",
            request={},
            event_stream_source=stream,
        )

    registered, events = asyncio.run(scenario())

    assert produced.is_set()
    assert events[-1].data["status"] == "succeeded"
    row = job_get(alice, registered.job_id)
    assert row["status"] == "succeeded"
    assert row["lease_token"] == "", "收尾之后执行权交还"


def test_second_dispatch_of_a_claimed_job_never_starts_a_second_executor(alice):
    """同一个任务被两个执行者派发时，只有领到租约的那个跑起来（另一个 0 行、不入表）。

    这条替代不了真跨进程的验证，但它钉住了本票在单进程里的可见后果：
    「派发」不再是「无脑起一支协程」，而是「先抢到执行权再起」。
    """
    from ticket_support import build_and_register

    calls = {"n": 0}

    async def stream(kind, request):
        calls["n"] += 1
        yield StreamEvent(type="chunk", content="只许跑一次", data={})
        yield StreamEvent(type="done", content="", data={"reply": "# 正文"})

    async def scenario():
        registered = build_and_register(
            owner_id=alice,
            solution_id=None,
            kind="prd",
            request={},
            event_stream_source=stream,
        )
        # 另一个执行者先领走（模拟第二个进程）
        later = utc_now() + timedelta(seconds=LEASE_SECONDS)
        blocked = job_claimer.claim(
            alice, registered.job_id, lease_owner="proc-b", now=later
        )
        assert blocked is not None
        # 本进程派发：抢占改到 0 行 → 不派发
        from backend.services.job_ticket import start_registered_job

        start_registered_job(registered)
        await asyncio.sleep(0.05)
        return registered

    registered = asyncio.run(scenario())

    assert registered.job_id not in _runtime_ids()
    assert calls["n"] == 0, "没抢到执行权的进程不许真的去调模型"
    row = job_get(alice, registered.job_id)
    assert row["status"] == "running", "别人领着的任务仍然是「执行中」"
    assert row["lease_owner"] == "proc-b"


def _runtime_ids() -> set[int]:
    """当前进程里在途任务的 id（经公开动作 `job_service.running_runtimes` 取）。"""
    from backend.services import job_service

    return {runtime.job_id for runtime in job_service.running_runtimes()}
