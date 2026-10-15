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

**用量计量**：每个任务自带一个 `RunMeter`（`llm_meter`），`run_job` 用 `use_meter`
把它挂到当前上下文，流水线深处的 `stream_reply.astream_text` 因此能在一处完成累加；
本模块只做三件事——喂阶段（`_apply_event`）、收尾汇总（`_summary_data`）、
落库与广播。事件与落库共用同一份 JSON，前端「流里看到的」与「重连后读到的」不会两样。

**文档落库**：任务成功收尾时，`_write_document` 把权威正文写进 `documents` 表
（见 `document_store.commit_generated`）。这是「文档所有权在后端」的落点：一份产物
什么时候产生、产生成什么样，只在这里决定；前端只读、只提交用户的手改。
失败的任务**不写**——半截输出不该顶掉一份完整文档。

**不改动现有 LLM 生成链路**：`_build_event_stream` 直接复用 `document_service` 的
模块级流式入口，本模块只负责编排 + 记录。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from backend.core.config import get_settings
from backend.core.contracts import DOCUMENT_KINDS, StreamEvent
from backend.core.db import utc_now
from backend.core.errors import AppError, ErrorCode
from backend.repositories.generation_job_repo import STATUS_RUNNING, GenerationJobRepository
from backend.services import document_service, document_store
from backend.services.job_bus import job_bus
from backend.services.llm_meter import RunMeter, use_meter
from backend.services.skill_registry import describe_kind

logger = logging.getLogger(__name__)

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
    # 本次任务的用量计量器（在 `dispatch` 里按任务类型与当前供应商构造）
    meter: RunMeter = field(default_factory=RunMeter)
    # 注入的技能包身份（PRD 任务才有），进 `run_summary.injected_skills`。
    # 在 dispatch 那一刻定格：万一生成中途改了模板，回执仍能说明「这次用的是哪一套」。
    skill_refs: list[dict[str, Any]] = field(default_factory=list)
    # 待确认清单：由 `done` 事件带来（生成器在定稿正文里剥离标记块后产出）。
    # 与正文同源、同一次收尾落库，因此「换了正文却忘了重算清单」没有结构落脚点。
    pending_items: list[dict[str, Any]] = field(default_factory=list)


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


def _target_product_kind(kind: str, request: dict[str, Any]) -> str | None:
    """任务要**产出/改写**的是哪一份产物；无法确定时返回 `None`。

    - `prd` / `api_docs` / `prompts`：整篇生成，种类本身就是产物类型。
    - `optimize` / `clarify`：对某一份**已有**文档的动作，产物类型来自请求体的 `doc_kind`。

    返回 `None` 表示取不到（如 `optimize` 漏传 `doc_kind`）。调用方据此**既不注入技能资源
    也不在回执里报技能包身份**——报一个跟实际注入不符的包，比不报更糟。
    """
    if kind in DOCUMENT_KINDS:
        return kind
    if kind in ("optimize", "clarify"):
        doc_kind = request.get("doc_kind")
        return doc_kind if doc_kind in DOCUMENT_KINDS else None
    return None


def _resolve_skill_refs(kind: str, request: dict[str, Any]) -> list[dict[str, Any]]:
    """本次任务注入的技能包身份：按**目标产物**去技能注册表里查。

    「哪个产物用哪个技能包」由技能包自己在 `skill.yaml` 的 `applies_to` 里声明，
    这里**不写死具体包名**——新加一个技能包不需要改这个函数（这正是注册表的意义）。

    ⚠️ 查的是 `_target_product_kind` 而不是 `kind` 本身：`optimize` / `clarify` 的 `kind`
    只说「做了一件什么事」，回执若照它去查，就会给「优化接口文档」标上 PRD 那个包——
    而实际注入的（若 `doc_kind` 缺失则一包都没注入）与回执不符，又是一次静默不一致。

    技能包没声明服务于该产物、不在位、元数据损坏或资源不齐时返回空列表——生成会降级
    到内置提示词，这件事必须能在回执上看得见（债务 D8），所以这里既不填占位值也不抛异常
    让任务起不来（能否 fail-fast 由启动自检管，见 `settings.skills_strict`）。

    读盘本身出意外（权限、编码）也按「没加载上」处理并留一条日志：它属于
    「该起的自检没拦住」，不让一次具体的生成请求无缘无故失败。
    """
    product_kind = _target_product_kind(kind, request)
    if product_kind is None:
        return []
    try:
        described = describe_kind(product_kind)
    except Exception:  # noqa: BLE001 - 自检类问题不该让生成任务起不来
        logger.warning(
            "读取技能包身份信息失败，本次回执不记录技能版本",
            extra={"event": "job.skill_ref_failed", "kind": product_kind},
            exc_info=True,
        )
        return []
    return [described] if described else []


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

    计量器随任务一起建，并记下**当前**供应商与模型：任务的执行环境在这一刻就定下来了，
    事后回看「这次生成花了多少、用的哪个模型」才不会因为中途改配置而对不上账。

    另：`create_task` 会**复制当前上下文**，因此请求中间件写入的 `request_id`
    自动随任务带走，`run_summary` 里能直接读到，无需任何显式传参。
    """
    settings = get_settings()
    runtime = JobRuntime(
        job_id=job_id,
        owner_id=owner_id,
        solution_id=solution_id,
        kind=kind,
        request=request or {},
        started_at=time.monotonic(),
        last_flush_at=time.monotonic(),
        meter=RunMeter(
            kind=kind,
            provider=settings.default_llm_provider,
            model=settings.default_llm_model,
        ),
    )
    runtime.skill_refs = _resolve_skill_refs(kind, runtime.request)

    task = asyncio.create_task(run_job(runtime))
    runtime.task = task
    # 先登记运行时再登记任务：两者都在 create_task 之后、任何 await 之前完成，
    # 因此「任务真正开始跑」之前，运行时一定已可见（重连/订阅不会落空）。
    _RUNTIMES[job_id] = runtime
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def run_job(runtime: JobRuntime) -> None:
    """后台任务主体：驱动事件流、更新内存、广播、按节奏落库、收尾。

    `use_meter` 包住整条事件流的消费过程：计量器是环境值，流水线深处的
    `stream_reply.astream_text` 靠它把自己记到这次任务上（见 `llm_meter`）。
    """
    try:
        with use_meter(runtime.meter):
            async for event in _build_event_stream(runtime.kind, runtime.request):
                _apply_event(runtime, event)
                job_bus.publish(runtime.job_id, event)
                _maybe_flush(runtime)
        _finalize(runtime, "failed" if runtime.failed else "completed")
    except Exception as exc:  # noqa: BLE001 - 后台任务必须自兜异常，不能静默消失
        runtime.failed = True
        if isinstance(exc, AppError):
            # 异常自带的码更准：预算超顶是 INVALID_REQUEST（重试也没用），
            # 不该被笼统记成「上游故障」，否则前端会去做无意义的重试。
            runtime.error_code = exc.code.value
            runtime.error_message = exc.message
        else:
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
      先 `yield` 快照；若任务已非 running 则补发一份 `run_summary` 后立即返回，
      否则循环消费队列直到哨兵 `None`。
    - 未命中或 owner 不符：读 DB 发一份快照后返回（进程重启 / 已完成过久 / 越权）；
      终态任务若已落用量，同样补发 `run_summary`。

    「无 await 临界区」是正确性的关键：取快照与订阅之间一旦让出事件循环，
    任务就可能在这段窗口里产事件并被丢弃，导致「快照与 live 之间缺一段」。

    **为什么要补发 `run_summary`**：收尾事件是在任务结束那一刻广播的，只在当刻连着的人
    收得到。刷新页面、或任务结束得早于订阅，都会漏掉它——那样「花了多少 token」就只在
    没人看的时候存在。终态任务的用量已经落库，重连时补一份即可，前端因此无需再单独
    发一次 `GET /api/jobs/{id}` 就能显示用量。
    """
    runtime = _RUNTIMES.get(job_id)
    if runtime is None or runtime.owner_id != owner_id:
        row = GenerationJobRepository.get(owner_id, job_id)
        if row is None:
            return
        yield _db_snapshot_event(row)
        usage = parse_usage(row.get("usage"))
        if usage is not None and row.get("status") != STATUS_RUNNING:
            yield _run_summary_event(usage)
        return

    # —— 无 await 临界区 ——
    snapshot = _snapshot_event(runtime)
    queue = job_bus.subscribe(job_id)
    # ————————————————————————
    try:
        yield snapshot
        if runtime.status != "running":
            yield _run_summary_event(_summary_data(runtime, runtime.status))
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
    - `stage` → 更新 phase（**同时同步给计量器**，计量器据此把调用归到 `类型:阶段` 这一步）；
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
        runtime.meter.set_phase(runtime.phase)
    elif event_type == "error":
        runtime.failed = True
        runtime.error_code = event.code or ErrorCode.INTERNAL_ERROR.value
        runtime.error_message = event.content
    elif event_type == "done":
        runtime.final_reply = str(event.data.get("reply", ""))
        # 待确认清单随 `done` 一道抵达：生成器已在定稿正文里剥离标记块并算好。
        # 用 `or []` 兜底，生成器漏带该字段时不至于把整型 / None 写进 DB。
        pending = event.data.get("pending_items")
        runtime.pending_items = pending if isinstance(pending, list) else []


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
            pending_items=runtime.pending_items,
        )
        runtime.last_flush_at = now
        runtime.flushed_len = len(runtime.live_buffer)


def _summary_data(runtime: JobRuntime, status: str) -> dict[str, Any]:
    """组装完整的 `run_summary`。

    这一份同时是**落进 `generation_jobs.usage` 的那份 JSON**：事件与落库共用同一结构，
    前端从流里拿到的和刷新后从 `GET /api/jobs/{id}` 拿到的必然一致，不会出现两套口径。

    总耗时用 `job_service` 自己的时钟（含章节之间的空档），而 `steps[]` 里是每一步的
    模型调用耗时——两者不相等是正常的：差额就是「组织提示词、落库、广播」这些开销。
    """
    duration_ms = int(max(0.0, time.monotonic() - runtime.started_at) * 1000)
    return {
        "kind": runtime.kind,
        "status": status,
        "duration_ms": duration_ms,
        **runtime.meter.usage(),
        # 放在展开之后覆盖：`usage()` 里的 `injected_skills` 恒为空（计量器不知道技能包的事），
        # 技能身份由编排层在任务开始时定格，生成中途改模板也不会让回执前后不一致。
        "injected_skills": runtime.skill_refs,
    }


def _run_summary_event(data: dict[str, Any]) -> StreamEvent:
    """由汇总数据构造 `run_summary` 事件。"""
    return StreamEvent(type="run_summary", content="", data=data)


def _finalize(runtime: JobRuntime, status: str) -> None:
    """收尾：置终态、落库（含用量）、**成功时把正文写进 `documents`**、广播 `run_summary`。

    保留运行时（直到 `run_job` 的 finally）让已订阅者把这一条读完。

    **只有成功才写文档**：失败的任务保留上一次可用的正文（半截输出不该顶掉一份
    完整文档）。这正是「文档所有权在后端」的关键收益——「谁写、什么时候写、
    写完之后长什么样」只在这一处决定，前端只负责读与提交用户变更。
    """
    runtime.status = status
    draft = _draft(runtime)
    summary = _summary_data(runtime, status)
    usage = json.dumps(summary, ensure_ascii=False)
    if status == "completed":
        GenerationJobRepository.finish(
            runtime.owner_id,
            runtime.job_id,
            draft_content=draft,
            completed_sections=runtime.completed_sections,
            usage=usage,
            pending_items=runtime.pending_items,
        )
        _write_document(runtime, summary)
    else:
        GenerationJobRepository.fail(
            runtime.owner_id,
            runtime.job_id,
            error_code=runtime.error_code or ErrorCode.INTERNAL_ERROR.value,
            error_message=runtime.error_message or "生成失败",
            draft_content=draft,
            completed_sections=runtime.completed_sections,
            # 失败的任务同样烧了 token，用量一并落库——不然「这次为什么这么贵」无从追溯
            usage=usage,
            pending_items=runtime.pending_items,
        )

    job_bus.publish(runtime.job_id, _run_summary_event(summary))


def _regenerates_whole_document(kind: str) -> bool:
    """这次任务是否是「整篇重生成上游产物」——只有它会连带作废下游。

    依据是产物依赖链（`document_store.DOWNSTREAM_KINDS`）：下游由上游正文推导而来，
    上游换了一版，下游就不再成立。反过来，`optimize`（按指令就地优化）与 `clarify`
    （按澄清改写）动的都是**同一份**文档的局部，其它产物仍然是按同一份上游推出来的，
    不该被顺手删掉。

    ⚠️ 这条判断以前是隐式的（一律 `invalidate_downstream=True`），后果是「优化接口文档」
    会把基于它生成的提示词悄悄删掉——用户看不到任何提示，只是下次打开方案时东西不见了。
    """
    return kind in DOCUMENT_KINDS


def _document_kind(runtime: JobRuntime) -> str | None:
    """任务要写的产物类型；无法确定时返回 `None`（宁可不写，也不写错位置）。

    直接用 `_target_product_kind`：它与「回执报哪个包」用的是同一份判断，两处不会分叉。

    ⚠️ `optimize` / `clarify` 缺 `doc_kind` 时**刻意返回 None**：猜一个默认值（比如 prd）
    会让「优化接口文档」的结果覆盖掉 PRD 正文，那是**静默的数据损坏**。宁可这次不落库
    （改动只留在当次会话里），也不要写错位置。
    """
    return _target_product_kind(runtime.kind, runtime.request)


def _write_document(runtime: JobRuntime, summary: dict[str, Any]) -> None:
    """把本次任务的权威正文写进 `documents`（成功收尾的唯一落点）。

    用 `done.reply` 而不是 `_draft()`：前者是流水线**定稿**的完整正文，后者是
    「已定稿段落 + 实时缓冲」的尽力还原，两者在正常情况下一致，但定稿值才是权威。

    无方案可挂（`solution_id` 为空，如快捷入口的逃生口）或归属不符时静默跳过——
    这属于「这次生成没地方落」，不该让一个已经成功的任务变成失败。

    写库本身出意外也只记日志：正文已经在 `generation_jobs.draft_content` 里存了一份，
    丢的只是「升格成文档」这一步，不该把整个任务判失败。
    """
    kind = _document_kind(runtime)
    if kind is None or runtime.solution_id is None:
        return
    content = runtime.final_reply
    if not content.strip():
        return
    try:
        written = document_store.commit_generated(
            runtime.owner_id,
            runtime.solution_id,
            kind,
            content,
            pending_items=runtime.pending_items,
            run_summary=summary,
            # 只有整篇重生成才让下游失效；`optimize` / `clarify` 是对同一份文档的就地改写，
            # 顺手删掉下游属于静默的数据丢失（见 `_regenerates_whole_document`）
            invalidate_downstream=_regenerates_whole_document(runtime.kind),
        )
    except Exception:  # noqa: BLE001 - 落文档不该把已成功的任务判失败
        logger.warning(
            "生成任务收尾写入文档失败，正文仍保留在任务记录里",
            extra={
                "event": "job.document_write_failed",
                "job_id": runtime.job_id,
                "kind": kind,
            },
            exc_info=True,
        )
        return
    if written is None:
        logger.warning(
            "生成任务收尾未能写入文档（方案不存在或归属不符）",
            extra={
                "event": "job.document_write_skipped",
                "job_id": runtime.job_id,
                "kind": kind,
                "solution_id": runtime.solution_id,
            },
        )


def parse_usage(raw: Any) -> dict[str, Any] | None:
    """解析 `generation_jobs.usage` 里存的 JSON；缺失或非法时返回 `None`。

    **容错是刻意的**：这一列在早年版本里恒为 `NULL`，将来也可能被改过结构；
    读不出来的正确反应是「这条任务没有用量可展示」，而不是让整个列表接口 500。
    只接受对象（字典），数组 / 字符串一律当作无效。
    """
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _load_pending_items(raw: Any) -> list[dict[str, Any]]:
    """把 `generation_jobs.pending_items` 的 JSON 文本解析成清单；异常一律退化为空列表。

    这一列在早年版本恒为 `NULL`、将来也可能被改过结构；读不出来就当「没有待确认项」，
    不该让整个快照事件构造失败。
    """
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _elapsed_ms_from_row(row: dict[str, Any]) -> int:
    """由 DB 行的创建 / 更新时间推算耗时（毫秒）；时间格式异常时返回 0。

    终态任务用 `updated_at - created_at`（≈ 整个过程），仍在跑的任务用「到现在」。
    这只是给前端的「已用时」一个起点，精度到秒即可，因此不做时区之外的额外校正。
    """
    try:
        created = datetime.fromisoformat(str(row.get("created_at") or ""))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        end = utc_now() if row.get("status") == STATUS_RUNNING else datetime.fromisoformat(
            str(row.get("updated_at") or "")
        )
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return int(max(0.0, (end - created).total_seconds()) * 1000)
    except (ValueError, TypeError):
        return 0


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
            # 待确认清单：刷新重连时一并恢复，前端无需另发一次请求就能展示。
            "pending_items": runtime.pending_items,
            # 服务端算的已用时：刷新重连后前端接着它继续走，客户端计时器归零也不影响
            "elapsed_ms": int(max(0.0, time.monotonic() - runtime.started_at) * 1000),
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
    pending_items = _load_pending_items(row.get("pending_items"))
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
            # 待确认清单：DB 兜底快照同样携带，刷新重连后前端直接展示。
            "pending_items": pending_items,
            "elapsed_ms": _elapsed_ms_from_row(row),
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
        )
    if kind == "prompts":
        return document_service.generate_prompts_stream(
            prd_content=request.get("prd_content", "") or "",
        )
    if kind == "optimize":
        return document_service.optimize_document_stream(
            document=request.get("document", "") or "",
            instruction=request.get("instruction", "") or "",
            # 目标产物决定注入哪一套产出规范；与回执报的技能包用同一份判断
            doc_kind=_target_product_kind(kind, request) or "",
        )
    if kind == "clarify":
        return document_service.apply_clarification_stream(
            document=request.get("document", "") or "",
            history=request.get("history") or None,
            doc_kind=_target_product_kind(kind, request) or "",
        )
    raise ValueError(f"未知的生成任务类型：{kind}")
