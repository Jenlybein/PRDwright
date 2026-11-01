"""事件应用、正文拼装与合并写（`job_service` 的拆分子模块）。

从 `job_service` **机械迁出**（issue #5），函数体逐字未改：`_apply_event` 把
`StreamEvent` 归并进 `JobRuntime`；`_finalized_text` / `_draft` 是「已产出正文」
的两种拼法；`_maybe_flush` 按「≤300ms 或增量 >4KB」合并写进度；
`_summary_data` / `_run_summary_event` 组装收尾回执。
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from backend.core.contracts import StreamEvent
from backend.core.engine import session_scope
from backend.core.errors import ErrorCode
from backend.core.request_context import current_request_id
from backend.repositories.generation_job_repo import GenerationJobRepository
from backend.services.job_runtime import _SectionLive

if TYPE_CHECKING:
    from backend.services.job_runtime import JobRuntime

logger = logging.getLogger(__name__)

# 合并写的节奏：距上次 flush 超过该秒数，或 live_buffer 增量超过该字节数，就写一次
_FLUSH_INTERVAL_SECONDS = 0.3
_FLUSH_BYTES = 4096


def _apply_event(runtime: JobRuntime, event: StreamEvent) -> None:
    """把一个事件应用到内存运行时（同步、无 await）。

    对照 `document_service` / `prd_section_writer` 的事件契约：
    - `chunk` → 追加到缓冲；**带 `section_id` + `index` 时进该章自己的格子**
      （分章节链路多章并行，混在一起就分不开了）；
    - `part` → 定稿一段：并入已定稿、清掉**该章**的半成品；带 `section_id` 时登记章节；
    - `stage` → 更新 phase（**同时同步给计量器**，计量器据此把调用归到 `类型:阶段` 这一步），
      换阶段时清空上一阶段的输出；章节级 `writing` / `rewriting` 清掉该章半成品；
    - `stage_chunk` → 追加到当前阶段的输出（审核意见 / 改写稿），与正文缓冲互不干扰；
    - `error` → 标记失败并记下错误码 / 文案；
    - `done` → 记下权威完整正文。
    """
    event_type = event.type
    if event_type == "chunk":
        section_id = str(event.data.get("section_id") or "")
        index = event.data.get("index")
        if section_id and isinstance(index, int):
            # 分章节的增量：按章存。多章同时在吐字，混到一个缓冲里就再也分不开，
            # 前端只能整批丢弃（「PRD 不流式」的历史成因）。
            slot = runtime.live_by_index.setdefault(index, _SectionLive())
            slot.section_id = section_id
            slot.text += event.content
        else:
            runtime.live_buffer += event.content
    elif event_type == "stage_chunk":
        # 阶段内输出只进 `stage_text`，绝不碰 `live_buffer`：后者会拼进正文预览。
        # 增量自带的 stage 与当前 phase 不一致，说明对应的 `stage` 事件没被收到
        # （订阅晚了一步），这里纠正过来——否则面板标题会与内容对不上。
        stage = str(event.data.get("stage", ""))
        if stage and stage != runtime.phase:
            runtime.stage_text = ""
            runtime.phase = stage
            runtime.meter.set_phase(stage)
        runtime.stage_text += event.content
    elif event_type == "part":
        text = str(event.data.get("text", ""))
        index = event.data.get("index")
        if event.data.get("replace") is True:
            # 下游改写稿整段覆盖初稿（审核发现问题后的那一次改写）
            runtime.finalized_parts = [text]
            runtime.parts_by_index.clear()
            # 覆盖是**整篇**语义：所有章节的半成品一并作废，否则它们会浮在权威正文之上
            runtime.live_by_index.clear()
            runtime.live_buffer = ""
            return
        if isinstance(index, int):
            runtime.parts_by_index[index] = text
            # ⚠️ 只清**这一章**：并行时别的章还在写，清全场会把它们的字一起抹掉
            runtime.live_by_index.pop(index, None)
        else:
            runtime.finalized_parts.append(text)
            # 文档头（没有 index）：整篇缓冲到此定稿
            runtime.live_buffer = ""
        section_id = event.data.get("section_id")
        if section_id:
            entry = {
                "section_id": section_id,
                "heading": str(event.data.get("section_heading", "")),
                "text": text,
                "index": index if isinstance(index, int) else None,
                "failed": bool(event.data.get("failed")),
            }
            # 同一章节以最后一次为准：重试 / 重复订阅都不该留下两条
            runtime.completed_sections = [
                item
                for item in runtime.completed_sections
                if item.get("section_id") != section_id
            ]
            runtime.completed_sections.append(entry)
            # 章节定稿是「这一章写完了」的可观测信号。分章节链路里多章并行，
            # 没有它，日志页只能看到一连串 stage，看不出到底写成了几章、哪章失败。
            logger.info(
                "章节定稿：%s",
                section_id,
                extra={
                    "event": "job.section_done",
                    "job_id": runtime.job_id,
                    "user_id": runtime.owner_id,
                    "kind": runtime.kind,
                    "section_id": section_id,
                    "index": index if isinstance(index, int) else None,
                    "failed": bool(event.data.get("failed")),
                    "chars": len(text),
                    "request_id": current_request_id(),
                },
            )
    elif event_type == "stage":
        stage = str(event.data.get("stage", ""))
        # 换阶段就清空上一阶段的输出：审核意见不该留在改写阶段的面板里
        if stage != runtime.phase:
            runtime.stage_text = ""
        runtime.phase = stage
        runtime.meter.set_phase(runtime.phase)
        # 阶段推进是生成生命线的骨架：记一条 INFO，让日志页能看出「走到哪一步」。
        # `chunk`/`stage_chunk` 是高频增量不打日志（会刷屏），边界事件才值得留。
        index = event.data.get("index")
        section_id = str(event.data.get("section_id") or "")
        if section_id and isinstance(index, int) and stage in ("writing", "rewriting"):
            slot = runtime.live_by_index.setdefault(index, _SectionLive())
            slot.section_id = section_id
            slot.text = ""
            slot.phase = stage
        if stage in ("writing", "reviewing", "rewriting", "done"):
            logger.info(
                "生成阶段推进：%s",
                stage,
                extra={
                    "event": "job.stage",
                    "job_id": runtime.job_id,
                    "user_id": runtime.owner_id,
                    "kind": runtime.kind,
                    "stage": stage,
                    "section_id": section_id or None,
                    "index": index if isinstance(index, int) else None,
                    "request_id": current_request_id(),
                },
            )
    elif event_type == "error":
        runtime.failed = True
        runtime.error_code = event.code or ErrorCode.INTERNAL_ERROR.value
        runtime.error_message = event.content
        # 生成链路内部主动产出的 error 事件（不是 `run_job` 的兜底异常），
        # 单独留一条：它与 `job.crashed` 的区别在于「这是可预期的失败，不是崩了」。
        logger.warning(
            "生成链路报错",
            extra={
                "event": "job.error",
                "job_id": runtime.job_id,
                "user_id": runtime.owner_id,
                "kind": runtime.kind,
                "error_code": runtime.error_code,
                "request_id": current_request_id(),
            },
        )
    elif event_type == "done":
        runtime.final_reply = str(event.data.get("reply", ""))
        runtime.partial = bool(event.data.get("partial"))
        failed = event.data.get("failed_sections")
        runtime.failed_sections = failed if isinstance(failed, list) else []
        # 待确认清单随 `done` 一道抵达：生成器已在定稿正文里剥离标记块并算好。
        # 用 `or []` 兜底，生成器漏带该字段时不至于把整型 / None 写进 DB。
        pending = event.data.get("pending_items")
        runtime.pending_items = pending if isinstance(pending, list) else []
        if runtime.kind == "chat":
            # 对话的 `done` 比文档多三样东西：这一轮的选题、它发起的工具调用、
            # 以及（可能变了）的会话 id。它们不进正文，但缺了会让下一轮
            # 「选项卡配不上对」——上游直接报错，而界面上只是「点不动了」。
            #
            # ⚠️ 键名是 `clarification_asks`（对话服务下发的原名），不是 `asks`。
            # 写成 `asks` 不会报错，只会让每一轮都存下空数组——界面表现为
            # 「选项卡再也没出现过」，而日志里 `asks_count` 明明大于 0。
            asks = event.data.get("clarification_asks")
            runtime.chat_asks = asks if isinstance(asks, list) else []
            calls = event.data.get("tool_calls")
            runtime.chat_tool_calls = calls if isinstance(calls, list) else []
            runtime.chat_conversation_id = str(event.data.get("conversation_id") or "")


def _finalized_text(runtime: JobRuntime) -> str:
    """已定稿正文（不含实时缓冲）：文档头 + 按 index 排序的章节。"""
    if runtime.parts_by_index:
        header = "".join(runtime.finalized_parts)
        return header + "".join(
            runtime.parts_by_index[i] for i in sorted(runtime.parts_by_index)
        )
    return "".join(runtime.finalized_parts)


def _draft(runtime: JobRuntime) -> str:
    """尽力还原的完整正文：已定稿部分 + 各章半成品（**不用原始 chunk 拼**）。

    ⚠️ 必须**按 index 交错归并**，不能「先所有定稿段、再所有半成品」：并行时第 1 章
    可能还在写、第 2 章已经定稿，简单拼接会把正文排成 2、3、1。同一 index 上定稿优先
    （定稿那一刻该章半成品已被清掉，这里再判一次是防御：重复 `part` 也不该出两份）。
    """
    header = "".join(runtime.finalized_parts)
    if not runtime.parts_by_index and not runtime.live_by_index:
        return header + runtime.live_buffer

    merged: list[str] = []
    for index in sorted(set(runtime.parts_by_index) | set(runtime.live_by_index)):
        finalized = runtime.parts_by_index.get(index)
        if finalized is not None:
            merged.append(finalized)
            continue
        slot = runtime.live_by_index.get(index)
        if slot is not None and slot.text:
            merged.append(slot.text)
    # `live_buffer` 在分章节链路里恒为空（那里无归属的增量不会出现），
    # 保留这一项是为了「整篇文档 + 章节」混用时不丢最后一段。
    return header + "".join(merged) + runtime.live_buffer


def _live_len(runtime: JobRuntime) -> int:
    """「还在变的部分」有多长：整篇缓冲 + 各章半成品。

    合并写靠它判断「增量是否 >4KB」。并行链路里 `live_buffer` 恒为空，若还按它算，
    进度落库就只剩 300ms 的定时器在兜——长章节会多丢一截，刷新后接回来的字变少。
    """
    return len(runtime.live_buffer) + sum(
        len(slot.text) for slot in runtime.live_by_index.values()
    )


def _maybe_flush(runtime: JobRuntime) -> None:
    """按「≤300ms 或增量 >4KB」的节奏合并写一次进度。"""
    now = time.monotonic()
    grew = _live_len(runtime) - runtime.flushed_len
    if grew >= _FLUSH_BYTES or (now - runtime.last_flush_at) >= _FLUSH_INTERVAL_SECONDS:
        with session_scope() as session:
            GenerationJobRepository(session).persist_progress(
                runtime.owner_id,
                runtime.job_id,
                draft_content=_draft(runtime),
                completed_sections=runtime.completed_sections,
                phase=runtime.phase,
                pending_items=runtime.pending_items,
            )
        runtime.last_flush_at = now
        runtime.flushed_len = _live_len(runtime)


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
        # 部分完成：正文可用但不完整。随 usage 落库，刷新后 `JobStatus.partial` 仍读得到。
        "partial": runtime.partial,
        "failed_sections": runtime.failed_sections,
    }


def _run_summary_event(data: dict[str, Any]) -> StreamEvent:
    """由汇总数据构造 `run_summary` 事件。"""
    return StreamEvent(type="run_summary", content="", data=data)
