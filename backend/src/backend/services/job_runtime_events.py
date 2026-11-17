"""事件应用、正文拼装与合并写（`job_service` 的拆分子模块）。

从 `job_service` **机械迁出**（issue #5），函数体逐字未改：`_apply_event` 把
`StreamEvent` 归并进 `JobRuntime`；`_finalized_text` / `_draft` 是「已产出正文」
的两种拼法（票 #92 起只有整篇形态：已定稿段 + 一段实时缓冲）；`_maybe_flush` 按
「≤300ms 或增量 >4KB」合并写进度；`_summary_data` / `_run_summary_event` 组装收尾回执。
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
from backend.services import job_events, job_state

if TYPE_CHECKING:
    from backend.services.job_runtime import JobRuntime

logger = logging.getLogger(__name__)

# 合并写的节奏：距上次 flush 超过该秒数，或 live_buffer 增量超过该字节数，就写一次
_FLUSH_INTERVAL_SECONDS = 0.3
_FLUSH_BYTES = 4096


def _apply_event(runtime: JobRuntime, event: StreamEvent) -> None:
    """把一个事件应用到内存运行时（同步、无 await）。

    对照 `document_service` 的事件契约（三份产物同一条整篇路径，票 #92）：
    - `chunk` → 追加到实时缓冲（不带章节归属，没有第二条轨道）；
    - `part` → 定稿一段；`replace: true` 是整段覆盖（审核发现问题后的那次改写）；
    - `stage` → 更新 phase（**同时同步给计量器**，计量器据此把调用归到 `类型:阶段` 这一步），
      换阶段时清空上一阶段的输出；
    - `stage_chunk` → 追加到当前阶段的输出（大纲 / 审核意见 / 改写稿），与正文缓冲互不干扰；
    - `review` → 记下本次审核结论与「这次试没试过」，收尾时随正文写进被审的那一版（一版一条）；
    - `error` → 标记失败并记下错误码 / 文案；
    - `done` → 记下权威完整正文。
    """
    event_type = event.type
    if event_type == "chunk":
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
        if event.data.get("replace") is True:
            # 改写稿整段覆盖初稿：初稿已经以 `chunk` 流出去过，追加会叠字。
            runtime.finalized_parts = [text]
            runtime.live_buffer = ""
            return
        runtime.finalized_parts.append(text)
        # 定稿一段：整篇缓冲到此作废（权威文本已经接管）
        runtime.live_buffer = ""
    elif event_type == "stage":
        stage = str(event.data.get("stage", ""))
        # 换阶段就清空上一阶段的输出：审核意见不该留在改写阶段的面板里
        if stage != runtime.phase:
            runtime.stage_text = ""
        runtime.phase = stage
        runtime.meter.set_phase(runtime.phase)
        # 阶段推进是生成生命线的骨架：记一条 INFO，让日志页能看出「走到哪一步」。
        # `chunk`/`stage_chunk` 是高频增量不打日志（会刷屏），边界事件才值得留。
        if stage in ("outlining", "writing", "reviewing", "rewriting", "done"):
            logger.info(
                "生成阶段推进：%s",
                stage,
                extra={
                    "event": "job.stage",
                    "job_id": runtime.job_id,
                    "user_id": runtime.owner_id,
                    "kind": runtime.kind,
                    "stage": stage,
                    "request_id": current_request_id(),
                },
            )
    elif event_type == "review":
        # 审核结论：只认真正跑出来的判定（pass / revise）。空串是「没审成」，
        # 收尾时**不写结论**——没有结论 ≠ 通过，写个占位就是一条假记录。
        # 同一任务内只会有一条，后到覆盖先到即可。
        #
        # `attempted` 是「这次到底试没试」的**显式标记**（#61）：审失败要落
        # `review_failed_at`，而「根本没审」不许落。两者都不能靠空串去猜。
        # 取「或」而不是直接赋值：同一任务里只要试过一次，就算试过。
        data = event.data if isinstance(event.data, dict) else {}
        runtime.review_attempted = runtime.review_attempted or bool(data.get("attempted"))
        verdict = str(data.get("verdict") or "").strip().lower()
        runtime.review = dict(data) if verdict in ("pass", "revise") else None
    elif event_type == "error":
        # 只记「失败意图」：终态（status / 落库）由收尾经状态机结算（#79）。
        job_state.note_failure(
            runtime,
            error_code=event.code or ErrorCode.INTERNAL_ERROR.value,
            error_message=event.content,
        )
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
    """已定稿正文（不含实时缓冲）：整篇生成只有一条拼接路径。"""
    return "".join(runtime.finalized_parts)


def _draft(runtime: JobRuntime) -> str:
    """尽力还原的完整正文：已定稿部分 + 实时缓冲（**不用原始 chunk 拼**）。

    整篇生成里 `finalized_parts` 只会在「改写稿整段覆盖」时变成一段；其余时刻是
    「都已定稿 + 最后一段还在长」。因此这里就是直接拼接，不再有按章归并。
    """
    return _finalized_text(runtime) + runtime.live_buffer


def _live_len(runtime: JobRuntime) -> int:
    """「还在变的部分」有多长（合并写靠它判断「增量是否 >4KB」）。"""
    return len(runtime.live_buffer)


def _flush_now(runtime: JobRuntime, *, progress: bool = True) -> int:
    """立刻合并写一笔，返回本次写到的游标。

    `progress=True`（写者的正常路径）：**进度列 + 事件同一笔事务**——两件事必须同生
    共死，否则「快照游标」就没有意义：

    - 进度列（`draft_content` / `phase` / `pending_items`）是**快照**，冷读者一次拿走；
    - `job_events` 的追加行是**过程日志**，按游标续读。

    快照游标 = 本笔写到的最后一条事件 id。若两者分两笔提交，就会出现「快照已经包含
    某段正文、而那条事件还没落盘」的窗口——拿着游标续读的客户端会重复收到那一段。

    `progress=False`（订阅侧的轮询补写，见 `job_service_impl.open_stream`）：**只追加
    过程日志**。订阅者替卡在模型调用里的写者把事件落盘，让「轮询兜底」真的能推进数据；
    但它**不碰进度列**——那是写者的产物，读者去写等于在别人的执行里留痕，会破坏
    「失去执行权后什么都不写」这条判据（进度列写者下一轮会自己补上）。

    调用点有两处：合并写窗口到点（`_maybe_flush`），以及**取快照之前**（`open_stream`
    必须先把在途事件补写完，否则内存快照领先游标，重连会重放已含内容）。
    """
    with session_scope() as session:
        cursor = job_events.flush_pending(runtime, session)
        if progress:
            GenerationJobRepository(session).persist_progress(
                runtime.owner_id,
                runtime.job_id,
                draft_content=_draft(runtime),
                phase=runtime.phase,
                pending_items=runtime.pending_items,
            )
    # 事务提交之后才清空待写缓冲（回滚时那一批必须还在）
    job_events.mark_flushed(runtime, cursor)
    if progress:
        # 只有写者推进了「已落盘的正文」才算窗口到点；订阅侧的补写不动这两项，
        # 让写者的下一轮合并写尽快把进度列补上。
        runtime.last_flush_at = time.monotonic()
        runtime.flushed_len = _live_len(runtime)
    return cursor



def _maybe_flush(runtime: JobRuntime) -> None:
    """按「≤300ms 或增量 >4KB」的节奏合并写一次（进度列 + 过程事件）。"""
    now = time.monotonic()
    grew = _live_len(runtime) - runtime.flushed_len
    if grew >= _FLUSH_BYTES or (now - runtime.last_flush_at) >= _FLUSH_INTERVAL_SECONDS:
        _flush_now(runtime)


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
