"""快照与回放：把内存运行时 / DB 行还原成 `snapshot` 事件（`job_service` 的拆分子模块）。

从 `job_service` **机械迁出**（issue #5），函数体逐字未改：`_snapshot_event` 读内存
运行时（含按章半成品），`_db_snapshot_event` 在运行时未命中时用 DB 行兜底；
`parse_usage` / `_load_pending_items` / `_elapsed_ms_from_row` 是它们的三条容错解析。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from backend.core.clock import utc_now
from backend.core.contracts import StreamEvent
from backend.repositories.generation_job_repo import STATUS_RUNNING
from backend.services.job_finalize import _document_kind
from backend.services.job_runtime_events import _finalized_text

if TYPE_CHECKING:
    from backend.services.job_runtime import JobRuntime


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


def _load_immutable_object(raw: Any) -> dict[str, Any]:
    """读回任务单的不可变对象（请求参数 / 配置快照）；坏数据一律退化成空对象。

    与 `_load_pending_items` 同一口径：旧库回填的是 `'{}'`、将来也可能被改过结构，
    读不出来不该让整个 DB 兜底快照炸掉。字符串形式的数组 / 标量同样视为无效。
    """
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _load_skill_refs(raw: Any) -> list[dict[str, Any]]:
    """读回技能包身份清单；坏 JSON / 非数组一律退化成空清单。"""
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
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
    ordered_sections = sorted(
        runtime.completed_sections,
        key=lambda item: (item.get("index") is None, item.get("index") or 0),
    )
    return StreamEvent(
        type="snapshot",
        content="",
        data={
            "job_id": runtime.job_id,
            "kind": runtime.kind,
            # 目标产物：与 `kind` 分开。`optimize` / `clarify` 的 kind 只说「做了什么」，
            # 前端重连时要靠这个字段知道「改的是哪一份」，否则只能猜（猜错即串稿）。
            "doc_kind": _document_kind(runtime),
            "status": runtime.status,
            "phase": runtime.phase,
            "finalized_text": _finalized_text(runtime),
            "live_buffer": runtime.live_buffer,
            # 分章节的**按章半成品**：刷新重连时前端据此把「正在写的几章」摆回去，
            # 并接着往下长。只含还没定稿的章——定稿的已在 `completed_sections` 里，
            # 两边都发会让同一段正文出现两次。
            "live_by_section": [
                {
                    "section_id": slot.section_id,
                    "index": index,
                    "text": slot.text,
                    "phase": slot.phase,
                }
                for index, slot in sorted(runtime.live_by_index.items())
                if slot.text
            ],
            # 当前阶段模型的原始输出（审核意见 / 改写稿）：刷新重连后接着展示，
            # 否则面板会空着，用户看不到「审核到哪一步了」。它**不落库**（见 `_db_snapshot_event`），
            # 因为它是过程信息、且整篇量级、重复写没有收益。
            "stage_text": runtime.stage_text,
            "completed_sections": ordered_sections,
            "solution_id": runtime.solution_id,
            "error_code": runtime.error_code,
            "error_message": runtime.error_message,
            # 待确认清单：刷新重连时一并恢复，前端无需另发一次请求就能展示。
            "pending_items": runtime.pending_items,
            # 部分完成：刷新后仍能看到「哪几章失败了」
            "partial": runtime.partial,
            "failed_sections": runtime.failed_sections,
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
    sections = sorted(
        sections,
        key=lambda item: (
            item.get("index") is None if isinstance(item, dict) else True,
            item.get("index") or 0 if isinstance(item, dict) else 0,
        ),
    )
    pending_items = _load_pending_items(row.get("pending_items"))
    try:
        usage = json.loads(row.get("usage") or "{}")
    except (json.JSONDecodeError, TypeError):
        usage = {}
    if not isinstance(usage, dict):
        usage = {}
    failed_sections = usage.get("failed_sections")
    if not isinstance(failed_sections, list):
        failed_sections = []
    return StreamEvent(
        type="snapshot",
        content="",
        data={
            "job_id": row.get("id"),
            "kind": row.get("kind", ""),
            # 旧行没有这一列时为 `''`，这里统一退化成 None（「未知」），不猜成 prd。
            "doc_kind": row.get("doc_kind") or None,
            "status": row.get("status", ""),
            "phase": row.get("phase", ""),
            "finalized_text": row.get("draft_content", "") or "",
            "live_buffer": "",
            # 按章半成品同样只活在内存运行时里（不落库，见 `JobRuntime.live_by_index`）：
            # DB 兜底快照拿不到它，显式给空数组，语义与 `stage_text` 一致。
            "live_by_section": [],
            # 阶段输出只活在内存运行时里，DB 没有这一列：进程重启后的兜底快照拿不到它。
            # 这里**显式给空串而不是省略**——前端解析器对缺失字段同样退化为空串，
            # 但显式写出来能让「这份快照没有该字段」的语义出现在契约里，不至于被当成漏实现。
            "stage_text": "",
            "completed_sections": sections,
            "solution_id": row.get("solution_id"),
            "error_code": row.get("error_code", "") or "",
            "error_message": row.get("error_message", "") or "",
            # 待确认清单：DB 兜底快照同样携带，刷新重连后前端直接展示。
            "pending_items": pending_items,
            "partial": bool(usage.get("partial")),
            "failed_sections": failed_sections,
            "elapsed_ms": _elapsed_ms_from_row(row),
            # 任务单里不可变数据的完整投影：运行时未命中时，DB 兜底快照仍读得回
            # 「这次生成用的原始请求、哪套配置/供应商、哪套技能包」。坏数据按上面
            # 三个容错解析退化成空容器，行为与既有 `usage` / `pending_items` 一致。
            "request_params": _load_immutable_object(row.get("request_params")),
            "config_snapshot": _load_immutable_object(row.get("config_snapshot")),
            "skill_refs": _load_skill_refs(row.get("skill_refs")),
        },
    )
