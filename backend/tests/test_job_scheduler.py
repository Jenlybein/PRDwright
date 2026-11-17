"""调度循环（P0-1）：三个维护单元终于有了**生产调用者**。

`claim_once` / `recover_once` / `purge_expired_events` 此前只在用例里被调用，于是运行期
有三件事从来没发生过：退避到期的任务没人领、卡住的取消没人兜底、过程日志永不清理。
本文件只验这三件事各自**真的跑起来**，以及「循环是一个可以干净起停的后台任务」：

1. 一条**排队中且已到期**的任务被循环领走并跑完（到期闸门同时挡住未到期的行）；
2. 一条**过了期的执行中**任务被循环收口回排队（恢复器那一轮）；
3. `lifespan` 起得来、关得掉——关闭之后没有悬挂 task，观测句柄也清空；
4. 用例环境默认**关掉**它（开关的意义：一千多条用例不该各自起三个周期任务）。

接缝：循环对外的三条动作（`run_once` / `recover_expired` / `purge_events`）就是它的一轮，
用例直接驱动一轮而不是等真实秒数；派发出去的仍然是真实的 `run_job`，只替换模型（
`document_service.get_service`），不动任何内部零件。
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from typing import Any

import pytest
from conftest import job_create, job_get, job_claim, register, users
from dbsupport import connect
from fakes import ScriptedLLM

from backend.core.clock import utc_now
from backend.core.config import get_settings
from backend.services import document_service, job_runtime, job_scheduler
from backend.services.job_bus import job_bus

_TERMINAL = ("succeeded", "failed", "superseded", "cancelled")
_SETTLE_TIMEOUT_SECONDS = 20.0


@pytest.fixture(autouse=True)
def clean_job_state():
    """每个用例前后清空进程内运行时与总线订阅（走公开的重置口）。"""
    job_runtime.reset_for_tests()
    job_bus.reset_for_tests()
    yield
    job_runtime.reset_for_tests()
    job_bus.reset_for_tests()


def _alice_id() -> int:
    user = users.get_by_username("alice")
    assert user is not None
    return int(user["id"])


def _create_solution(client) -> int:  # noqa: ANN001 - TestClient
    """建一份带自由文本需求的方案（PRD 的上游输入由服务端按方案取）。"""
    response = client.post(
        "/api/solutions",
        json={
            "title": "调度循环用例",
            "mode": "structured",
            "stage": "form",
            "state": {
                "entry_mode": "prd-quick",
                "quick_product_name": "演示产品",
                "quick_requirements": "做一个演示产品",
            },
        },
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _offline_service(monkeypatch) -> None:
    """把文档服务换成离线假模型：派发出去的仍是真实 `run_job`，只有模型是假的。"""
    monkeypatch.setattr(
        document_service,
        "get_service",
        lambda: document_service.DocumentService(
            llm=ScriptedLLM(["# 演示 PRD\n\n## 1. 概述\n\n正文。\n"])
        ),
    )


def _set_next_run_at(job_id: int, when: str) -> None:
    """直接改 `next_run_at`（退避闸门的判据列）：造一条「还没到期」的行。

    形态必须与 `core/clock.utc_now_text()` 一致（无时区的 ISO 文本）——闸门比的是
    **字符串的字典序**，写成带时区的形态会在比较里悄悄错位。
    """
    with connect() as conn:
        conn.execute(
            "UPDATE generation_jobs SET next_run_at = %s WHERE id = %s", (when, job_id)
        )
        # 底层连接不自动提交（`dbsupport.connect` 只是借一条池连接）：不 commit，
        # 这条 UPDATE 会在归还连接时被回滚——用例就会静默地测到「闸门不存在」。
        conn.commit()


def _scheduler() -> job_scheduler.JobScheduler:
    """不启周期任务的循环实例：用例自己驱动「一轮」。"""
    return job_scheduler.JobScheduler(
        claim_interval=3600.0,
        recovery_interval=3600.0,
        purge_interval=3600.0,
        claim_batch=5,
    )


async def _wait_settled(owner_id: int, job_id: int) -> dict[str, Any]:
    deadline = time.monotonic() + _SETTLE_TIMEOUT_SECONDS
    while True:
        row = job_get(owner_id, job_id)
        assert row is not None
        if row["status"] in _TERMINAL:
            return row
        assert time.monotonic() < deadline, f"循环派发的任务没有收尾：{row}"
        await asyncio.sleep(0.02)


def test_scheduler_claims_a_due_pending_job_and_runs_it(client, monkeypatch) -> None:
    """**一条排队中且已到期的任务**被循环领走、派发、跑完（#88 AC1 在运行期可达）。

    同时验到期闸门：`next_run_at` 在未来的那一行**这一轮领不走**——它还没到该跑的时候。
    「到期」与「排队顺序」是两件事，闸门在 `WHERE` 里（`_due_now`），不是排序键。
    """
    _offline_service(monkeypatch)
    register(client)
    owner_id = _alice_id()
    solution_id = _create_solution(client)

    due_job = job_create(owner_id, "prd", solution_id)
    future_job = job_create(owner_id, "prd", solution_id)
    _set_next_run_at(future_job, (utc_now() + timedelta(days=365)).isoformat())

    scheduler = _scheduler()

    async def scenario() -> list[job_scheduler.DispatchOutcome]:
        outcomes = await scheduler.run_once()
        await _wait_settled(owner_id, due_job)
        return outcomes

    outcomes = asyncio.run(scenario())

    assert [item.job_id for item in outcomes] == [due_job], (
        f"这一轮应当只领到**到期**的那一条：{outcomes}"
    )
    assert outcomes[0].dispatched is True

    row = job_get(owner_id, due_job)
    assert row is not None
    assert row["status"] == "succeeded", f"循环派发出去的任务必须真的跑完：{row}"
    assert row["lease_token"] == "", "成功收尾会清租约"

    from backend.services import document_store

    versions = document_store.list_versions(owner_id, solution_id, "prd")
    assert [item["version_no"] for item in versions] == [1], "跑完必须落一版正文"

    # 未到期的那一条一动没动
    future_row = job_get(owner_id, future_job)
    assert future_row is not None
    assert future_row["status"] == "pending"
    assert future_row["lease_token"] == ""


def test_scheduler_recovery_requeues_an_expired_running_job(client) -> None:
    """**一条过了期的执行中任务**被循环收口：清租约、回排队、记一条超时账。

    这就是「执行者进程消失了」在运行期的收口路径（#83 AC1 / AC3）：没有循环时，
    这一行会永远停在 `running`，用户看到进度条一直转。
    """
    from backend.core.engine import session_scope
    from backend.repositories.job_attempt_repo import JobAttemptRepository

    register(client)
    owner_id = _alice_id()
    solution_id = _create_solution(client)
    job_id = job_create(owner_id, "prd", solution_id)
    # 十分钟前领的租约：120 秒的租约因此八分钟前就过期了（`expire_at = now + 120s`）
    lease = job_claim(owner_id, job_id, now=utc_now() - timedelta(minutes=10))

    scheduler = _scheduler()
    report = asyncio.run(scheduler.recover_expired())

    assert report.requeued == 1, f"过期租约应当被收口回排队：{report}"
    row = job_get(owner_id, job_id)
    assert row is not None
    assert row["status"] == "pending", "回排队之后落回 pending（等下一轮被领走）"
    assert (row["lease_owner"], row["lease_token"], row["lease_expire_at"]) == ("", "", ""), (
        "回排队要清租约：这一行已经没有执行者了"
    )
    assert row["next_run_at"] != "", "回排队同时写失败退避（到期之前领不走）"
    assert lease.token != ""

    with session_scope() as session:
        attempts = JobAttemptRepository(session).list_by_job(owner_id, job_id)
    assert len(attempts) == 1, "每收口一行记一条超时账"
    assert attempts[0].error_code == "LEASE_EXPIRED"


def test_lifespan_starts_and_stops_the_scheduler_cleanly(isolated_db, monkeypatch) -> None:  # noqa: ARG001
    """`lifespan` 起得来、**关得掉**：关闭之后没有悬挂 task，观测句柄也清空。

    「干净退出」不是形容词：三个周期任务必须都已经 `done()`，否则进程退出时会留一堆
    被强制取消的后台任务（在排空与日志 flush 之后还在写库，那是最脏的一种退出）。
    """
    monkeypatch.setenv("JOB_SCHEDULER_ENABLED", "true")
    get_settings.cache_clear()

    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as test_client:
        scheduler = job_scheduler.active()
        assert scheduler is not None, "开关打开时 lifespan 必须把循环起起来"
        assert scheduler.running is True
        tasks = scheduler.tasks
        assert len(tasks) == 3, f"三个周期各一支：{[task.get_name() for task in tasks]}"
        assert test_client.get("/healthz").status_code == 200

    assert all(task.done() for task in tasks), "关闭阶段必须 cancel + await 三个周期任务"
    assert job_scheduler.active() is None, "观测句柄要清空，否则下一个应用会以为循环还在"


def test_scheduler_is_off_by_default_in_tests() -> None:
    """用例环境默认**关掉**循环（`tests/conftest.py` 的开关）：一千多条用例不该各自起循环。

    生产默认是开的（配置里 `job_scheduler_enabled=True`）——那才是它要修的问题；
    测试只是把「后台任务与人共享数据库」这件事显式挡掉，由本文件单独打开来验证。
    """
    assert get_settings().job_scheduler_enabled is False
