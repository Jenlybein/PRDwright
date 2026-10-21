"""生成主链路日志用例（v4 T4a：给日志页喂真实内容）。

T0 落地了日志管道（handler 挂上 → 队列 → `app_logs`），T4 要做检索页。但若主链路
一条 INFO 都不打，检索页就只有「服务启动」「接口被调」两条，管理员真正想看的
「这次生成走到哪一步、哪章定稿了、哪次调用重试了」一片空白。

本文件钉住的是「**主链路真的在往日志里写**」这件事，而不是日志管道本身
（那条已由 `test_log_store.py` 覆盖）。断言分两层：

1. **内容层**：跑一次真实生成，`app_logs` 里应出现 `job.started` / `job.stage` /
   `job.section_done` / `job.completed` 这几类事件。
2. **归属层**：这些记录必须带 `job_id`、`user_id`、`request_id`——日志页的
   「按任务串起来」与「按请求号反查」两条排障主路径都靠它们成立。

其中 `request_id` 是最容易被悄悄丢掉的：`LogStoreHandler._row` 在**后台线程**里
取 `request_id`，而 ContextVar 不跨线程传播，只有业务线程里 `extra={"request_id": ...}`
显式传入的才会落库。这里显式在 request 上下文里派发并断言它真的进了库。
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from conftest import register

from backend.core import logging_setup
from backend.core.config import get_settings
from backend.core.db import connect
from backend.core.request_context import reset_request_id, set_request_id
from backend.services import (
    document_service,
    job_service,
    llm_factory,
    prd_section_writer,
)
from fakes import ScriptedLLM


@pytest.fixture
def log_env(tmp_path, monkeypatch):  # noqa: ARG001 - tmp_path 由 isolated_db 共用
    """开启落库并调成测试友好节奏；结束彻底收尾，避免后台线程串到下一用例。"""
    monkeypatch.setenv("LOG_DB_ENABLED", "true")
    monkeypatch.setenv("LOG_DB_FLUSH_INTERVAL", "0.1")
    monkeypatch.setenv("LOG_DB_BATCH_SIZE", "5")
    get_settings.cache_clear()

    root = logging.getLogger()
    original_level = root.level
    logging_setup.configure_logging(level="INFO", fmt="text", force=True)
    yield
    logging_setup.reset_logging()
    root.setLevel(original_level)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def no_real_llm(monkeypatch):
    """硬约束：真实 LLM 工厂一被调用就抛，杜绝触网。"""

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("测试不应触网调用真实 LLM")

    monkeypatch.setattr(llm_factory, "get_llm", _boom)
    monkeypatch.setattr(document_service, "get_llm", _boom)
    monkeypatch.setattr(prd_section_writer, "get_llm", _boom)
    yield


@pytest.fixture(autouse=True)
def clean_job_state():
    """用例前后清空进程内运行时，避免串扰。"""
    job_service._RUNTIMES.clear()  # noqa: SLF001
    job_service._TASKS.clear()  # noqa: SLF001
    yield
    job_service._RUNTIMES.clear()  # noqa: SLF001
    job_service._TASKS.clear()  # noqa: SLF001


def _rows() -> list[dict]:
    """读出表内全部日志行。"""
    with connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT ts, level, logger, msg, request_id, user_id, event, extra_json "
                "FROM app_logs ORDER BY id"
            ).fetchall()
        ]


def _alice_id() -> int:
    from backend.services import auth_service

    user = auth_service.UserRepository.get_by_username("alice")
    assert user is not None
    return int(user["id"])


def test_main_chain_logs_carry_job_and_request_ids(client, log_env, monkeypatch):
    """端到端：跑一次生成，主链路 INFO 落库且带 job_id / user_id / request_id。"""
    register(client)
    owner_id = _alice_id()
    monkeypatch.setattr(
        document_service,
        "get_service",
        lambda: document_service.DocumentService(llm=ScriptedLLM(["章节正文"])),
    )

    async def scenario() -> None:
        job_id = job_service.create_job(owner_id=owner_id, solution_id=None, kind="prd")
        # 显式进入 request 上下文：模拟真实 HTTP 请求的 request_id 传播。
        # `create_task` 会复制 ContextVar，但落库线程读不到它，靠 `extra=` 显式传。
        token = set_request_id("req-test-123")
        try:
            job_service.dispatch(
                job_id,
                owner_id=owner_id,
                solution_id=None,
                kind="prd",
                request={"requirements": "做一个演示产品", "product_name": "演示产品"},
            )
        finally:
            reset_request_id(token)
        # 派发后立刻跑：事件循环必须让后台任务有机会推进并收尾
        # （这里把 open_stream 消费到底，顺带等任务收尾 flush 日志）。
        async for _ in job_service.open_stream(owner_id, job_id):
            pass

    asyncio.run(scenario())

    # 收尾 flush：日志落库是异步的，任务结束后日志线程仍需一小段窗口写库。
    # 直接关掉持久化 handler，让它把队列里剩下的 flush 掉（同步）。
    logging_setup.shutdown_log_persistence()

    events = [row["event"] for row in _rows()]
    # 主链路的关键节点都该出现
    assert "job.started" in events, f"缺任务起点日志，实际事件：{events}"
    assert "job.stage" in events, f"缺阶段推进日志，实际事件：{events}"
    assert "job.section_done" in events, f"缺章节定稿日志，实际事件：{events}"
    assert "job.completed" in events, f"缺收尾日志，实际事件：{events}"

    # 归属字段：日志页「按任务串」「按请求反查」两条主路径的前提
    chain_rows = [row for row in _rows() if row["event"] and str(row["event"]).startswith("job.")]
    assert chain_rows, "主链路日志一条都没落库"

    for row in chain_rows:
        payload = json.loads(row["extra_json"] or "{}")
        # job_id 在 extra_json 里（job_id 不是提升列，只有 event / user_id 是）
        assert "job_id" in payload, f"主链路日志丢了 job_id：{row}"
        # user_id 提升成列，必须能对上归属
        assert row["user_id"] == owner_id, f"user_id 归属错位：{row}"
        # request_id 必须显式带上——这是最容易静默丢失的一环
        assert row["request_id"] == "req-test-123", f"request_id 未落库：{row}"


def test_llm_retry_is_logged(client, log_env, monkeypatch):
    """模型调用失败重试不再是静默的：`llm.call_retry` 应落库。

    `ScriptedLLM(raise_at=...)` 会在指定块之前抛 `ConnectionError`，触发
    `stream_reply._astream_chunks` 的自愈重试。重试前必须留一条 INFO——
    这条是「哪一步重试了、为什么」的唯一可查落点（T4a 之前完全静默）。
    """
    register(client)
    owner_id = _alice_id()
    # 一进流就抛（raise_at=0）：`delivered=False`、`ConnectionError` 可重试，
    # 于是 `stream_reply._astream_chunks` 进入重试循环，每次中间失败都留一条
    # `llm.call_retry`。最终耗尽重试次数后走 `llm.call_failed` 并让任务失败——
    # 但「重试发生过」这件事必须先被记录下来（T4a 之前完全静默）。
    monkeypatch.setattr(
        document_service,
        "get_service",
        lambda: document_service.DocumentService(
            llm=ScriptedLLM(["章节正文"], raise_at=0)
        ),
    )

    async def scenario() -> None:
        job_id = job_service.create_job(owner_id=owner_id, solution_id=None, kind="prd")
        token = set_request_id("req-retry-456")
        try:
            job_service.dispatch(
                job_id,
                owner_id=owner_id,
                solution_id=None,
                kind="prd",
                request={"requirements": "演示", "product_name": "演示"},
            )
        finally:
            reset_request_id(token)
        async for _ in job_service.open_stream(owner_id, job_id):
            pass

    asyncio.run(scenario())
    logging_setup.shutdown_log_persistence()

    events = [row["event"] for row in _rows()]
    assert "llm.call_retry" in events, f"模型调用重试应被记录，实际：{events}"
