"""生成任务服务：把「一次生成」变成可持久化、可重连的后台过程。

核心思想是**生成与浏览器连接解耦**：

- `dispatch` 在服务进程内起一个 `asyncio` 后台任务（`run_job`），任务的推进完全
  不依赖任何 HTTP 连接；浏览器断开只会让某个订阅者退出，任务照跑。
- 任务的每个事件既更新内存运行时（`JobRuntime`），又 `publish` 到 `job_bus`。
- 订阅（SSE）通过 `open_stream`：命中内存运行时则**先发一份 `snapshot`（补全已产出
  正文）再挂上 live 流**；未命中（进程重启 / 已完成过久）则读 DB 发一份快照即止。

持久化策略（合并写）：内存运行时是权威，DB 每 ≤300ms 或 `live_buffer` 增量 >4KB
`flush` 一次（`persist_progress`）；任务收尾时再落一次终态。快照读的是内存运行时，
DB 只在「运行时未命中」时兜底。

**不改动现有 LLM 生成链路**：`_build_event_stream` 直接复用 `document_service` 的
模块级流式入口，本模块只负责编排 + 记录。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from backend.core.contracts import StreamEvent
from backend.core.errors import ErrorCode
from backend.repositories.generation_job_repo import GenerationJobRepository
from backend.services import document_service
from backend.services.job_bus import job_bus

# 合并写的节奏：距上次 flush 超过该秒数，或 live_buffer 增量超过该字节数，就写一次
_FLUSH_INTERVAL_SECONDS = 0.3
_FLUSH_BYTES = 4096

# 进程内的任务运行时：`job_id -> JobRuntime`
_RUNTIMES: dict[int, "JobRuntime"] = {}
# 保活集合：持有 `asyncio.Task` 强引用，防止未来得及跑完就被垃圾回收
_TASKS: set[asyncio.Task] = set()


@dataclass
class JobRuntime:
    """一次生成任务在内存里的运行态（权威正文就在这里）。"""

    job_id: int
    owner_id: int
    solution_id: int | None
    kind: str
    status: str = "running"
    phase: str = ""
    # 已定稿段落（文档头 + 各章 part.text），按序拼接即「已产出正文」
    finalized_parts: list[str] = field(default_factory=list)
    # 当前段的实时缓冲（chunk 原始增量），遇到下一个 part 时清空
    live_buffer: str = ""
    # 已定稿章节的结构化记录（供快照恢复时定位「写到第几章」）
    completed_sections: list[dict[str, Any]] = field(default_factory=list)
    # 原样保存的请求参数，供 `_build_event_stream` 重建生成
    request: dict[str, Any] = field(default_factory=dict)
    task: asyncio.Task | None = None
    failed: bool = False
    error_code: str = ""
    error_message: str = ""
    # 上次 flush 的时间与当时 live_buffer 的长度（用于「≤300ms 或增量 >4KB」判断）
    last_flush_at: float = 0.0
    flushed_len: int = 0
    # 任务开始时刻（算 duration_ms）
    started_at: float = 0.0
    # done 事件带来的权威完整正文
    final_reply: str = ""


# ----------------------------------------------------------------------------
# 对外动作
# ----------------------------------------------------------------------------


def create_job(
    *,
    owner_id: int,
    solution_id: int | None,
    kind: str,
    input_fingerprint: str | None = None,
) -> int:
    """新建一次生成任务，返回其 id。

    先 `supersede_active` 把同 owner + solution + kind 下旧的 running 置为 failed，
    保证「同一组合至多一个 running」。随后落一行 running。
    """
    GenerationJobRepository.supersede_active(owner_id, solution_id=solution_id, kind=kind)
    return GenerationJobRepository.create(owner_id, kind, solution_id, input_fingerprint)


def dispatch(
    job_id: int,
    *,
    owner_id: int,
    solution_id: int | None,
    kind: str,
    request: dict[str, Any],
) -> None:
    """构造运行时并起一个后台任务推进生成（不阻塞调用方）。

    路由是 async，调用方已在事件循环内，因此这里可以 `asyncio.create_task`。
    任务会被 `_TASKS` 持有强引用，跑完后由 done 回调移除。
    """
    runtime = JobRuntime(
        job_id=job_id,
        owner_id=owner_id,
        solution_id=solution_id,
        kind=kind,
        request=request or {},
        started_at=time.monotonic(),
        last_flush_at=time.monotonic(),
    )
    task = asyncio.create_task(run_job(runtime))
    runtime.task = task
    # 先登记运行时再登记任务：两者都在 create_task 之后、任何 await 之前完成，
    # 因此「任务真正开始跑」之前，运行时一定已可见（重连/订阅不会落空）。
    _RUNTIMES[job_id] = runtime
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def run_job(runtime: JobRuntime) -> None:
    """后台任务主体：驱动事件流、更新内存、广播、按节奏落库、收尾。"""
    try:
        async for event in _build_event_stream(runtime.kind, runtime.request):
            _apply_event(runtime, event)
            job_bus.publish(runtime.job_id, event)
            _maybe_flush(runtime)
        _finalize(runtime, "failed" if runtime.failed else "completed")
    except Exception as exc:  # noqa: BLE001 - 后台任务必须自兜异常，不能静默消失
        runtime.failed = True
        runtime.error_code = ErrorCode.LLM_UPSTREAM_ERROR.value
        runtime.error_message = f"生成失败：{exc}"
        _finalize(runtime, "failed")
    finally:
        # 先关总线（给订阅者放哨兵）再摘运行时：已订阅者仍能读到 run_summary 与结束标记；
        # 之后的迟到订阅者改走 DB 快照分支。
        job_bus.close(runtime.job_id)
        _RUNTIMES.pop(runtime.job_id, None)
        current = asyncio.current_task()
        if current is not None:
            _TASKS.discard(current)


async def open_stream(owner_id: int, job_id: int) -> AsyncIterator[StreamEvent]:
    """订阅一个任务的事件流：先 `snapshot`，再 live。

    - 内存运行时命中且 owner 相符：在**无 await 的临界区**内取快照并订阅队列，
      先 `yield` 快照；若任务已非 running 则立即返回，否则循环消费队列直到哨兵 `None`。
    - 未命中或 owner 不符：读 DB 发一份快照后返回（进程重启 / 已完成过久 / 越权）。

    「无 await 临界区」是正确性的关键：取快照与订阅之间一旦让出事件循环，
    任务就可能在这段窗口里产事件并被丢弃，导致「快照与 live 之间缺一段」。
    """
    runtime = _RUNTIMES.get(job_id)
    if runtime is None or runtime.owner_id != owner_id:
        row = GenerationJobRepository.get(owner_id, job_id)
        if row is None:
            return
        yield _db_snapshot_event(row)
        return

    # —— 无 await 临界区 ——
    snapshot = _snapshot_event(runtime)
    queue = job_bus.subscribe(job_id)
    # ————————————————————————
    try:
        yield snapshot
        if runtime.status != "running":
            return
        while True:
            event = await queue.get()
            if event is None:
                return
            yield event
    finally:
        job_bus.unsubscribe(job_id, queue)


def get_job(owner_id: int, job_id: int) -> dict[str, Any] | None:
    """读取单条任务（含全列）；不存在或越权返回 None。"""
    return GenerationJobRepository.get(owner_id, job_id)


def list_jobs(
    owner_id: int,
    *,
    solution_id: int | None = None,
    kind: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """列出该 owner 的任务，可选按 solution_id / kind / status 过滤。"""
    return GenerationJobRepository.list_by_owner(
        owner_id,
        status=status,
        solution_id=solution_id,
        kind=kind,
    )


def reap_orphans() -> None:
    """启动时回收上次残留的 running 任务（进程重启后运行时已丢失）。"""
    GenerationJobRepository.reap_running()


# ----------------------------------------------------------------------------
# 事件处理（内存运行时更新 / 合并写 / 收尾 / 快照）
# ----------------------------------------------------------------------------


def _apply_event(runtime: JobRuntime, event: StreamEvent) -> None:
    """把一个事件应用到内存运行时（同步、无 await）。

    对照 `document_service` / `prd_section_writer` 的事件契约：
    - `chunk` → 追加到当前段缓冲；
    - `part` → 定稿一段：并入已定稿、清空缓冲；带 `section_id` 时登记章节；
    - `stage` → 更新 phase；
    - `error` → 标记失败并记下错误码 / 文案；
    - `done` → 记下权威完整正文。
    """
    event_type = event.type
    if event_type == "chunk":
        runtime.live_buffer += event.content
    elif event_type == "part":
        text = str(event.data.get("text", ""))
        runtime.finalized_parts.append(text)
        runtime.live_buffer = ""
        section_id = event.data.get("section_id")
        if section_id:
            runtime.completed_sections.append(
                {
                    "section_id": section_id,
                    "heading": str(event.data.get("section_heading", "")),
                    "text": text,
                }
            )
    elif event_type == "stage":
        runtime.phase = str(event.data.get("stage", ""))
    elif event_type == "error":
        runtime.failed = True
        runtime.error_code = event.code or ErrorCode.INTERNAL_ERROR.value
        runtime.error_message = event.content
    elif event_type == "done":
        runtime.final_reply = str(event.data.get("reply", ""))


def _draft(runtime: JobRuntime) -> str:
    """尽力还原的完整正文：已定稿部分 + 当前缓冲（**不用原始 chunk 拼**）。"""
    return "".join(runtime.finalized_parts) + runtime.live_buffer


def _maybe_flush(runtime: JobRuntime) -> None:
    """按「≤300ms 或增量 >4KB」的节奏合并写一次进度。"""
    now = time.monotonic()
    grew = len(runtime.live_buffer) - runtime.flushed_len
    if grew >= _FLUSH_BYTES or (now - runtime.last_flush_at) >= _FLUSH_INTERVAL_SECONDS:
        GenerationJobRepository.persist_progress(
            runtime.owner_id,
            runtime.job_id,
            draft_content=_draft(runtime),
            completed_sections=runtime.completed_sections,
            phase=runtime.phase,
        )
        runtime.last_flush_at = now
        runtime.flushed_len = len(runtime.live_buffer)


def _finalize(runtime: JobRuntime, status: str) -> None:
    """收尾：置终态、落库、广播最小版 `run_summary`。

    保留运行时（直到 `run_job` 的 finally）让已订阅者把这一条读完。
    """
    runtime.status = status
    draft = _draft(runtime)
    if status == "completed":
        GenerationJobRepository.finish(
            runtime.owner_id,
            runtime.job_id,
            draft_content=draft,
            completed_sections=runtime.completed_sections,
            usage=None,
        )
    else:
        GenerationJobRepository.fail(
            runtime.owner_id,
            runtime.job_id,
            error_code=runtime.error_code or ErrorCode.INTERNAL_ERROR.value,
            error_message=runtime.error_message or "生成失败",
            draft_content=draft,
            completed_sections=runtime.completed_sections,
        )

    duration_ms = int(max(0.0, time.monotonic() - runtime.started_at) * 1000)
    # 最小版：token 计量留待后续阶段
    job_bus.publish(
        runtime.job_id,
        StreamEvent(
            type="run_summary",
            content="",
            data={"kind": runtime.kind, "status": status, "duration_ms": duration_ms},
        ),
    )


def _snapshot_event(runtime: JobRuntime) -> StreamEvent:
    """由内存运行时构造 `snapshot` 事件（补全已产出正文）。"""
    return StreamEvent(
        type="snapshot",
        content="",
        data={
            "job_id": runtime.job_id,
            "kind": runtime.kind,
            "status": runtime.status,
            "phase": runtime.phase,
            "finalized_text": "".join(runtime.finalized_parts),
            "live_buffer": runtime.live_buffer,
            "completed_sections": runtime.completed_sections,
            "solution_id": runtime.solution_id,
            "error_code": runtime.error_code,
            "error_message": runtime.error_message,
        },
    )


def _db_snapshot_event(row: dict[str, Any]) -> StreamEvent:
    """由 DB 行构造 `snapshot` 事件（运行时未命中时的兜底）。"""
    try:
        sections = json.loads(row.get("completed_sections") or "[]")
    except (json.JSONDecodeError, TypeError):
        sections = []
    if not isinstance(sections, list):
        sections = []
    return StreamEvent(
        type="snapshot",
        content="",
        data={
            "job_id": row.get("id"),
            "kind": row.get("kind", ""),
            "status": row.get("status", ""),
            "phase": row.get("phase", ""),
            "finalized_text": row.get("draft_content", "") or "",
            "live_buffer": "",
            "completed_sections": sections,
            "solution_id": row.get("solution_id"),
            "error_code": row.get("error_code", "") or "",
            "error_message": row.get("error_message", "") or "",
        },
    )


# ----------------------------------------------------------------------------
# 生成链路的复用入口（**不改动**现有 LLM 生成实现）
# ----------------------------------------------------------------------------


def _build_event_stream(kind: str, request: dict[str, Any]) -> AsyncIterator[StreamEvent]:
    """按任务类型复用 `document_service` 的模块级流式入口，返回 `StreamEvent` 流。

    这里只做「任务类型 → 现有生成入口」的映射，生成逻辑（提示词、分章节、审核、
    截断检测）全部在 `document_service` / `prd_section_writer` / `stream_reply` 里，
    本模块一行不改。
    """
    if kind == "prd":
        return document_service.generate_prd_from_summary_stream(
            summary=request.get("summary"),
            requirements=request.get("requirements", "") or "",
            product_name=request.get("product_name", "") or "",
            history=request.get("history") or None,
        )
    if kind == "api_docs":
        return document_service.generate_api_docs_stream(
            prd_content=request.get("prd_content", "") or "",
            rag_hits=request.get("rag_hits") or None,
        )
    if kind == "prompts":
        return document_service.generate_prompts_stream(
            prd_content=request.get("prd_content", "") or "",
        )
    if kind == "optimize":
        return document_service.optimize_document_stream(
            document=request.get("document", "") or "",
            instruction=request.get("instruction", "") or "",
        )
    raise ValueError(f"未知的生成任务类型：{kind}")
