"""任务单的登记事务与派发实现（票 #36）。

本模块是「造单 → 登记 → 派发」三步的内部实现，不设兼容期、不保留旧入口；
`build_job_ticket` 不碰库，`register_job_ticket` 把「当场查方案 / 顶替双标记 /
落库投影 / 写 chat 输入轮次」收进**一个** `session_scope()`，`start_registered_job`
只认单②。

派发直接复用同一包内的 `JobRuntime` / `run_job` / `_RUNTIMES` / `_TASKS`
（包内互引私有名合法），因此 SSE 事件种类与编码、重连先快照后 live、
失败不覆写、被取代与已取消分开、排空顺序与 30 秒等既有语义一条都不变。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from backend.core.chat_kinds import CHAT_STAGES, is_valid_stage
from backend.core.engine import session_scope
from backend.core.errors import AppError, ErrorCode
from backend.core.request_context import current_request_id
from backend.repositories.chat_turn_repo import ChatTurnRepository
from backend.repositories.generation_job_repo import STATUS_RUNNING, GenerationJobRepository
from backend.repositories.solution_repo import SolutionRepository
from backend.services import job_service_impl
from backend.services.job_service_impl import run_job
from backend.services.job_runtime import _RUNTIMES, _TASKS, JobRuntime
from backend.services.job_ticket import (
    JobTicket,
    RegisteredJobTicket,
    as_event_request,
    thaw_object,
    with_registered_identity,
)
from backend.services.llm_meter import RunMeter

logger = logging.getLogger(__name__)


def register_in_transaction(ticket: JobTicket) -> RegisteredJobTicket:
    """把单①登记成单②：一个事务完成全部落库动作，产出新的不可变对象。

    顺序按新三步行事：先校验 chat 线程/并发、当场查方案是否仍存在；文档任务
    在同一事务里 `supersede_active` + `create`，chat 任务在同一事务里 `create`
    + 写输入轮次。`GenerationJobRepository.create` 沿用 #34 的签名，
    `request_params` / `config_snapshot` / `skill_refs` / 供应商四列一次写全。

    内存里旧运行时的顶替标记放在事务**提交之后**：事务失败时不会留下
    「协程停了、库里还在跑」的错位。登记失败即单①丢弃、不产生单②、不派发。
    """
    if ticket.kind == "chat":
        _validate_chat(ticket)

    with session_scope() as session:
        repo = GenerationJobRepository(session)
        # 「方案是否仍存在」是**登记当场查询**：与库写在同一事务里，不冻结在单上。
        _check_solution_exists(session, ticket)
        if ticket.kind == "chat":
            # 单写者：内存权威 + 库残留，与登记的准入判据一致。
            _check_chat_single_writer(session, ticket)
        if ticket.kind != "chat":
            # 库行顶替：与内存 `_mark_superseded` 是同一判据，两处不能分叉。
            repo.supersede_active(
                ticket.owner_id, solution_id=ticket.solution_id, kind=ticket.kind
            )
        job = repo.create(
            ticket.owner_id,
            ticket.kind,
            ticket.solution_id,
            ticket.input_fingerprint,
            doc_kind=ticket.doc_kind,
            chat_stage=ticket.chat_stage,
            request_params=thaw_object(ticket.request_params),
            config_snapshot=thaw_object(ticket.config_snapshot),
            skill_refs=thaw_object(ticket.skill_refs),
        )
        job_id = int(job.id or 0)

        input_turn_id = 0
        if ticket.kind == "chat" and ticket.chat_mode != "start" and (
            ticket.chat_content.strip() or ticket.chat_tool_result is not None
        ):
            # 先整体深还原再取值：嵌套的 answers 也要从 tuple 变回 list，
            # 否则 `ChatTurnRepository._dumps` 的 json.dumps 会撞上 mappingproxy。
            tool_result = (
                thaw_object(ticket.chat_tool_result)
                if ticket.chat_tool_result is not None
                else None
            )
            role = "tool" if tool_result is not None else "user"
            # 同一事务里写输入轮次：失败时连那条 running 行一起回滚。
            input_turn_id = ChatTurnRepository(session).create(
                ticket.owner_id,
                int(ticket.solution_id),
                stage=ticket.chat_stage,
                role=role,
                content=ticket.chat_content,
                job_id=job_id,
                tool_call_id=str((tool_result or {}).get("tool_call_id") or ""),
                tool_result=tool_result,
                answers=(tool_result or {}).get("answers") if tool_result else None,
            )

    if ticket.kind != "chat":
        # 事务提交之后才置内存标记：与登记事务「落库之后才置」同一口径。
        job_service_impl._mark_superseded(  # noqa: SLF001 - 包内互引私有名合法
            ticket.owner_id, solution_id=ticket.solution_id, kind=ticket.kind
        )

    return with_registered_identity(ticket, job_id=job_id, input_turn_id=input_turn_id)


def _check_solution_exists(session: Any, ticket: JobTicket) -> None:
    """当场查「方案是否仍存在」：不冻结在单上，与收尾的当场查询同一口径。

    用登记事务**同一个 Session** 查，避免「查完到落库之间方案被删」的窗口；
    与 `solution_service.solution_exists` 的判据逐字一致（`id + owner`）。
    """
    if ticket.solution_id is None:
        return
    if not SolutionRepository(session).exists(ticket.owner_id, int(ticket.solution_id)):
        raise AppError(ErrorCode.SOLUTION_NOT_FOUND, "方案不存在")


def _validate_chat(ticket: JobTicket) -> None:
    """chat 线程与方案归属的准入校验（登记阶段逐项完成）。"""
    if not is_valid_stage(ticket.chat_stage):
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            f"未知的对话线程：{ticket.chat_stage or '（空）'}"
            f"（可选：{'、'.join(CHAT_STAGES)}）",
        )
    if ticket.solution_id is None:
        raise AppError(ErrorCode.INVALID_REQUEST, "对话任务必须关联一份方案")


def _check_chat_single_writer(session: Any, ticket: JobTicket) -> None:
    """单写者：同一线程同时只允许一轮在跑（内存权威 + 库残留）。"""
    for runtime in _RUNTIMES.values():  # noqa: SLF001 - 包内互引
        if (
            runtime.owner_id == ticket.owner_id
            and runtime.solution_id == ticket.solution_id
            and runtime.kind == "chat"
            and runtime.chat_stage == ticket.chat_stage
            and runtime.status == STATUS_RUNNING
        ):
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                "这条对话还在生成中，等它说完或先点停止",
            )
    active = GenerationJobRepository(session).find_active(
        ticket.owner_id,
        solution_id=ticket.solution_id,
        kind="chat",
        chat_stage=ticket.chat_stage,
    )
    if active is not None:
        raise AppError(ErrorCode.INVALID_REQUEST, "这条对话还在生成中，等它说完或先点停止")


def dispatch_registered(ticket: RegisteredJobTicket) -> None:
    """只认单②：构造运行时、按定格值造计量器、在**无 await 临界区**起后台任务。

    配置冻结（HANDOFF-36 第 2 条）：`runtime.config` 与 `RunMeter` 的
    kind / protocol / model / config_id / name 全部取自单②里那份定格值，
    派发侧**不再**自行 `get_runtime_config()` 各取一次；生成中途改后台配置不漂移。
    """
    config = ticket.runtime_config
    # 计量器四格取深还原后的普通容器：与单②同源，且下游不必认识只读映射。
    active = thaw_object(ticket.config_snapshot).get("active_provider") or {}
    meter = RunMeter(
        kind=ticket.kind,
        protocol=str(active.get("provider_protocol") or ""),
        model=str(active.get("provider_model") or ""),
        config_id=str(active.get("provider_config_id") or ""),
        name=str(active.get("provider_name") or ""),
    )
    request = as_event_request(ticket)
    runtime = JobRuntime(
        job_id=ticket.job_id,
        owner_id=ticket.owner_id,
        solution_id=ticket.solution_id,
        kind=ticket.kind,
        request=request,
        chat_stage=ticket.chat_stage,
        chat_input_turn_id=ticket.input_turn_id,
        started_at=time.monotonic(),
        last_flush_at=time.monotonic(),
        meter=meter,
        config=config,
    )
    runtime.skill_refs = thaw_object(ticket.skill_refs)

    # 复用唯一的 `run_job` 循环（事件应用 / 同步广播 / 合并写 / 收尾 / finally
    # 关总线与摘运行时都只有一份实现），只把造单定格的事件流来源递进去。
    task = asyncio.create_task(run_job(runtime, ticket.event_stream_source))
    runtime.task = task
    # 先登记运行时再登记任务：两者都在 create_task 之后、任何 await 之前完成，
    # 因此「任务真正开始跑」之前运行时一定已可见（重连/订阅不会落空）。
    _RUNTIMES[ticket.job_id] = runtime
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    logger.info(
        "生成任务已派发",
        extra={
            "event": "job.started",
            "job_id": ticket.job_id,
            "user_id": ticket.owner_id,
            "solution_id": ticket.solution_id,
            "kind": ticket.kind,
            "request_id": current_request_id(),
        },
    )
