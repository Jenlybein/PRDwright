"""创建去重（票 #87）：同一 owner + 方案 + 产物 + 输入指纹在 10 秒内复用既有任务。

本文件只跨公开接缝断言，不探内部状态：

| 接缝 | 断言的可观察行为 |
| --- | --- |
| `register_job_ticket` | 第二次相同提交返回**既有** `job_id`、不落新行、不顶替 |
| `start_registered_job` | 复用单是空操作：同一 `job_id` 不会起第二支协程（模型只跑一次） |
| `POST /api/jobs` | 连点两次只一条任务；第二次 201 + 同一编号，只有一支在途运行时 |
| 判据边界 | 超 10 秒窗 / 指纹不同 / 方案不同 / 终态任务 都照常新建，顶替语义不变 |

「不依赖客户端额外传参」= 请求体与从前逐字相同：去重键里的指纹是服务端在
`POST /api/jobs` 里按方案算出来的 `input_fingerprint`，没有新增任何请求字段。

所有生成链路走离线假环境（造单时传入的假事件流 / 假 LLM），零 token。
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import timedelta
from typing import Any, AsyncIterator

import pytest
from conftest import register, users
from dbsupport import connect
from fakes import ScriptedLLM

from backend.core import clock
from backend.core.contracts import StreamEvent
from backend.services import document_service, job_dedupe, job_runtime, job_service, job_ticket

# 等待后台任务收尾的上限：正常在毫秒级完成，留足余量以免慢机器上偶发失败。
_SETTLE_TIMEOUT_SECONDS = 10.0


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------


def _alice_id() -> int:
    """取已注册用户 alice 的 id（generation_jobs.owner_id 有外键）。"""
    user = users.get_by_username("alice")
    assert user is not None
    return int(user["id"])


@pytest.fixture
def alice(client):
    """注册 alice 并返回其 id（顺带保证 DB 已初始化）。"""
    register(client)
    return _alice_id()


@pytest.fixture(autouse=True)
def clean_job_state():
    """清空进程内运行时、任务集合与总线订阅，避免用例互相污染。"""
    job_runtime._RUNTIMES.clear()  # noqa: SLF001
    job_runtime._TASKS.clear()  # noqa: SLF001
    yield
    job_runtime._RUNTIMES.clear()  # noqa: SLF001
    job_runtime._TASKS.clear()  # noqa: SLF001


def _row_count() -> int:
    """任务表当前行数（「只产生一条任务」的可观察证据）。"""
    with connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM generation_jobs").fetchone()[0])


def _raw(job_id: int) -> dict[str, Any]:
    """按 id 直读整行（绕开 ORM，验证真的落盘了）。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM generation_jobs WHERE id = %s", (job_id,)
        ).fetchone()
    assert row is not None
    return dict(row)


def _seed_solution(client, title: str = "去重方案") -> int:
    """走既有路由建一份方案，返回 id（登记要当场查它还在不在）。"""
    created = client.post("/api/solutions", json={"title": title})
    assert created.status_code == 201, created.text
    return int(created.json()["id"])


def _create_solution_with_requirements(client, title: str = "去重方案") -> int:
    """建一份带需求输入的方案（PRD 的输入由服务端从这一份取，指纹由它算出）。"""
    created = client.post(
        "/api/solutions",
        json={
            "title": title,
            "state": {
                "entry_mode": "prd-quick",
                "quick_product_name": "演示产品",
                "quick_requirements": "做一个演示产品",
            },
        },
    )
    assert created.status_code == 201, created.text
    return int(created.json()["id"])


def _ticket(
    owner_id: int,
    solution_id: int,
    input_fingerprint: str,
    *,
    kind: str = "prd",
    stream: Any = None,
) -> job_ticket.JobTicket:
    """走公开正门造一张 prd 单①（指纹是路由层算好的数据，登记时随单落列）。"""
    return job_ticket.build_job_ticket(
        owner_id=owner_id,
        solution_id=solution_id,
        kind=kind,
        request={"requirements": "做一个演示产品"},
        input_fingerprint=input_fingerprint,
        event_stream_source=stream,
    )


def _register(owner_id: int, solution_id: int, input_fingerprint: str) -> job_ticket.RegisteredJobTicket:
    """造单 → 登记（不派发）：去重判据的可观察出口。"""
    return job_ticket.register_job_ticket(
        _ticket(owner_id, solution_id, input_fingerprint)
    )


# ---------------------------------------------------------------------------
# 一、连续两次相同提交只产生一条任务
# ---------------------------------------------------------------------------


def test_second_identical_submission_reuses_the_running_job(client, alice):
    """同一 owner + 方案 + 产物 + 指纹的第二次提交，返回既有编号、不再落一行。

    复用走的不是「再登记一次」：既不新建任务行，也不顶替既有任务（否则第一次
    提交正在进行的那一支会被自己掐掉）。返回的单②带 `reused` 标记，派发侧据此
    不重复起协程。
    """
    solution_id = _seed_solution(client)
    first = _register(alice, solution_id, "fp-same")
    assert first.reused is False
    assert _row_count() == 1

    second = _register(alice, solution_id, "fp-same")

    assert second.reused is True
    assert second.job_id == first.job_id
    assert _row_count() == 1
    # 既有的任务原样继续跑（没有被这次提交顶替成 failed）
    assert _raw(first.job_id)["status"] == "running"


# ---------------------------------------------------------------------------
# 二、指纹不同 / 超过时间窗：照常新建（顶替语义不变）
# ---------------------------------------------------------------------------


def test_fingerprint_change_still_creates_a_new_job(client, alice):
    """指纹变了就不是同一次提交：照常新建，并顶替同一方案下还在跑的旧任务。"""
    solution_id = _seed_solution(client)
    first = _register(alice, solution_id, "fp-before")
    second = _register(alice, solution_id, "fp-after")

    assert second.reused is False
    assert second.job_id != first.job_id
    assert _row_count() == 2
    # 正常建单语义不变：同 owner + 方案 + 产物的在途旧任务被顶替（#79 的既有口径）
    assert _raw(first.job_id)["status"] == "failed"
    assert _raw(first.job_id)["error_code"] == "SUPERSEDED"


def test_outside_window_still_creates_a_new_job(client, alice, monkeypatch):
    """超过 10 秒窗不再复用：即使任务还在跑，也照常新建（用户是「再提交一次」）。

    用**挪时钟**而不是 sleep 11 秒：`job_dedupe` 只从 `core/clock.py` 取时间，
    把它的 `utc_now` 换成「真时间 + 11 秒」，窗口下界就落在既有 `created_at` 之后。
    """
    solution_id = _seed_solution(client)
    first = _register(alice, solution_id, "fp-window")
    real_utc_now = clock.utc_now
    monkeypatch.setattr(
        job_dedupe, "utc_now", lambda: real_utc_now() + timedelta(seconds=11)
    )

    second = _register(alice, solution_id, "fp-window")

    assert second.reused is False
    assert second.job_id != first.job_id
    assert _row_count() == 2


def test_inside_window_still_reuses(client, alice, monkeypatch):
    """窗口按「既有任务 `created_at`」起算：只过了 9 秒仍在窗内，复用。"""
    solution_id = _seed_solution(client)
    first = _register(alice, solution_id, "fp-inside")
    real_utc_now = clock.utc_now
    monkeypatch.setattr(
        job_dedupe, "utc_now", lambda: real_utc_now() + timedelta(seconds=9)
    )

    second = _register(alice, solution_id, "fp-inside")

    assert second.reused is True
    assert second.job_id == first.job_id
    assert _row_count() == 1


def test_other_solution_is_not_a_duplicate(client, alice):
    """方案不同就不是同一次提交：两条任务互不顶替（方案也在判据键里）。"""
    first_solution = _seed_solution(client, "方案甲")
    second_solution = _seed_solution(client, "方案乙")
    first = _register(alice, first_solution, "fp-shared")
    second = _register(alice, second_solution, "fp-shared")

    assert second.reused is False
    assert second.job_id != first.job_id
    assert _row_count() == 2
    assert _raw(first.job_id)["status"] == "running"


def test_same_fingerprint_on_another_kind_is_not_a_duplicate(client, alice):
    """`kind` / `doc_kind` 也参与判据：不同动作 + 不同产物绝不能被当成重复。

    「生成 PRD」与「优化接口文档」是两个动作；即使指纹碰巧相同（今天只有 prd 算
    指纹，这里靠公开正门手递同一指纹），也不能把优化请求接到 PRD 任务上。
    """
    solution_id = _seed_solution(client)
    prd = _register(alice, solution_id, "fp-cross-kind")
    optimize = job_ticket.register_job_ticket(
        job_ticket.build_job_ticket(
            owner_id=alice,
            solution_id=solution_id,
            kind="optimize",
            request={"document": "# 旧接口文档", "instruction": "改一下", "doc_kind": "api_docs"},
            input_fingerprint="fp-cross-kind",
        )
    )

    assert optimize.reused is False
    assert optimize.job_id != prd.job_id
    assert _row_count() == 2
    assert _raw(prd.job_id)["status"] == "running"
    assert _raw(prd.job_id)["doc_kind"] == "prd"
    assert _raw(optimize.job_id)["doc_kind"] == "api_docs"


# ---------------------------------------------------------------------------
# 三、复用不再派发：同一 job_id 只有一支协程（模型只跑一次）
# ---------------------------------------------------------------------------


def _wait_status(client, job_id: int, wanted: str, timeout: float = _SETTLE_TIMEOUT_SECONDS) -> str:
    """轮询任务状态直到变成 `wanted`（超时即失败并带出最后状态）。"""
    deadline = time.monotonic() + timeout
    status = ""
    while time.monotonic() < deadline:
        status = client.get(f"/api/jobs/{job_id}").json()["status"]
        if status == wanted:
            return status
        time.sleep(0.02)
    pytest.fail(f"任务没有在 {timeout} 秒内变成 {wanted}，最后状态：{status}")


def test_reused_ticket_is_not_dispatched_again(client, alice):
    """复用单交给派发口是**空操作**：同一次生成只跑一支，绝不重复调用模型。

    事件流来源是造单时传入的假环境（生产真值 / 测试假值的唯一接缝）：它被
    调起几次就是「模型被跑了几次」。第二次提交拿到复用单后再派发一次，计数仍是 1。
    """
    solution_id = _seed_solution(client)
    started: list[int] = []

    async def source(kind: str, request: Any) -> Any:
        started.append(1)
        yield StreamEvent(type="chunk", content="正文", data={})
        yield StreamEvent(type="done", content="", data={"reply": "# 完成"})

    async def scenario():
        first = job_ticket.register_job_ticket(
            _ticket(alice, solution_id, "fp-dispatch", stream=source)
        )
        job_ticket.start_registered_job(first)
        second = job_ticket.register_job_ticket(
            _ticket(alice, solution_id, "fp-dispatch", stream=source)
        )
        # 复用的单②也照常交给派发口：它必须什么都不做
        job_ticket.start_registered_job(second)
        events = [
            event async for event in job_service.open_stream(alice, second.job_id)
        ]
        return first, second, events

    first, second, events = asyncio.run(scenario())

    assert second.reused is True
    assert second.job_id == first.job_id
    assert started == [1], "只在跑一次生成；第二次提交又派发了一次"
    assert _row_count() == 1
    assert events[-1].type == "run_summary"
    assert events[-1].data["status"] == "completed"


# ---------------------------------------------------------------------------
# 四、生产路由：连点两次只看到一条任务（不依赖客户端额外传参）
# ---------------------------------------------------------------------------


class _GatedLLM(ScriptedLLM):
    """首块产出前先等一个**跨线程**闸门，让「连点第二次」发生在生成途中。"""

    def __init__(self, gate: threading.Event, pieces: list[str] | None = None) -> None:
        super().__init__(pieces)
        self._gate = gate

    async def astream(self, messages: list[Any]) -> AsyncIterator[Any]:
        await asyncio.to_thread(self._gate.wait, _SETTLE_TIMEOUT_SECONDS)
        async for chunk in super().astream(messages):
            yield chunk


def _install_gated_doc_service(monkeypatch, gate: threading.Event) -> _GatedLLM:
    """把文档生成链路换成「先等闸门、再吐正文」的假实现，返回假 LLM。"""
    llm = _GatedLLM(gate, ["章节正文"])
    monkeypatch.setattr(
        document_service,
        "get_service",
        lambda: document_service.DocumentService(llm=llm),
    )
    return llm


def _no_real_llm(monkeypatch) -> None:
    """真实 LLM 工厂一被调用就抛，杜绝任何测试触网。"""

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("测试不应触网调用真实 LLM")

    from backend.services import prd_section_writer

    monkeypatch.setattr(document_service, "get_llm", _boom)
    monkeypatch.setattr(prd_section_writer, "get_llm", _boom)


def test_two_identical_http_submissions_share_one_running_job(client, monkeypatch):
    """连点两次「生成 PRD」：只一条任务、第二次 201 回同一编号、只有一支运行时。

    请求体与从前逐字相同（`kind` + `solution_id`）——去重键里的指纹是服务端按方案
    算的 `input_fingerprint`，没有新增任何客户端参数（AC4）。
    """
    register(client)
    solution_id = _create_solution_with_requirements(client)
    _no_real_llm(monkeypatch)
    gate = threading.Event()
    _install_gated_doc_service(monkeypatch, gate)

    payload = {"kind": "prd", "solution_id": solution_id}
    first = client.post("/api/jobs", json=payload)
    assert first.status_code == 201, first.text
    job_id = int(first.json()["job_id"])
    # 生成已经起跑（卡在闸门上），占住一个在途运行时
    assert [runtime.job_id for runtime in job_service.running_runtimes()] == [job_id]

    second = client.post("/api/jobs", json=payload)
    assert second.status_code == 201, second.text
    assert int(second.json()["job_id"]) == job_id
    assert second.json()["status"] == "running"
    # 不新建、不重复派发：库里只有一条，在途仍只有那一条
    assert _row_count() == 1
    assert [runtime.job_id for runtime in job_service.running_runtimes()] == [job_id]

    gate.set()
    _wait_status(client, job_id, "completed")
    assert _row_count() == 1


def test_http_submission_after_completion_still_creates_a_new_job(client, monkeypatch):
    """终态任务不复用：生成结束之后再点一次是「重新生成」，照常新建。

    这是「去重不改变正常建单语义」的守点——若终态也复用，第二次点击会静默拿到
    上一次的旧结果，用户以为点了新生成却什么也没跑。
    """
    register(client)
    solution_id = _create_solution_with_requirements(client)
    _no_real_llm(monkeypatch)
    monkeypatch.setattr(
        document_service,
        "get_service",
        lambda: document_service.DocumentService(llm=ScriptedLLM(["章节正文"])),
    )

    payload = {"kind": "prd", "solution_id": solution_id}
    first = client.post("/api/jobs", json=payload)
    assert first.status_code == 201, first.text
    first_id = int(first.json()["job_id"])
    assert client.get(f"/api/jobs/{first_id}/stream").status_code == 200
    _wait_status(client, first_id, "completed")

    second = client.post("/api/jobs", json=payload)
    assert second.status_code == 201, second.text
    second_id = int(second.json()["job_id"])

    assert second_id != first_id
    assert _row_count() == 2
    assert _raw(first_id)["status"] == "completed"


# ---------------------------------------------------------------------------
# 五、并发：两个请求同时到达也只产生一条任务
# ---------------------------------------------------------------------------


def test_concurrent_identical_submissions_create_one_job(client, alice):
    """两个同时到达的相同提交只建一行：登记的临界区把「查重 → 建行 → 提交」包住。

    断言「恰好一条新建 + 一条复用」：没有互斥时两个线程会各自查不到、各自建一行。
    """
    solution_id = _seed_solution(client)
    drafts = [_ticket(alice, solution_id, "fp-race") for _ in range(2)]
    barrier = threading.Barrier(2)
    outcomes: list[job_ticket.RegisteredJobTicket] = []
    failures: list[BaseException] = []

    def submit(draft: job_ticket.JobTicket) -> None:
        try:
            barrier.wait(timeout=10)
            outcomes.append(job_ticket.register_job_ticket(draft))
        except BaseException as exc:  # noqa: BLE001 - 线程里的异常要带回主线程
            failures.append(exc)

    threads = [threading.Thread(target=submit, args=(draft,)) for draft in drafts]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not failures, failures
    assert len(outcomes) == 2
    assert outcomes[0].job_id == outcomes[1].job_id
    assert sorted(item.reused for item in outcomes) == [False, True]
    assert _row_count() == 1
