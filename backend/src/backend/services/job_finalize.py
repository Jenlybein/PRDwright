"""任务收尾与终态落库（`job_service` 的拆分子模块）。

`_finalize` 置终态、落用量、写正文 / 写审核结论、广播 `run_summary`；
`_document_kind` / `_regenerates_whole_document` / `_build_derived_from` / `_write_document`
决定「写哪份产物、追加还是就地改」。

## 事务边界（票 #81：原子收尾）

成功收尾的五件事——**宣布成功 / 记一条账 / 写正文新版本 / 写审核结论 / 广播回执**——
全部落在 `_finalize` 的**一个** `session_scope()` 里：

1. `job_state.transition(..., session=session)` 一次条件写占住终态，账本那一笔（#80）
   套在保存点里跟着这一笔走；
2. 正文写入走 `document_store.write_document(..., session=session)`（统一入口，带版本锁）；
3. 重审的结论写入走 `document_store.commit_review(..., session=session)`。

任一步抛错 → 事务整批回滚 → **任务仍是 `running`**（不谎报成功、也不凭空判死），
留一条 `job.closeout_rolled_back` warning，由恢复机制（#82 的租约 / 启动回收）接手。
例外是**重审结论写不进去**：那一次执行的唯一产出就是结论，写不成必须让任务失败并说明
原因——调用方据此再开一笔只落失败的事务（`_ReviewGateFailure`）。**版本锁拒绝**同理：
就地改写撞上「当前版本已经不是我以为那一版」时整批回滚，再以 `CONTENT_CHANGED` 判失败。

## 账本与硬事务的口径取舍（#80 vs #81）

正文与状态是**权威**，账本是**审计**：写正文失败整批回滚（AC1/AC5），而账本写失败
只丢这一行账（`#80` 的保存点口径）。两者在同一个 `job_state.transition(session=...)`
里同时满足——硬事务边界只在正文 / 状态 / 结论，账本用 `SAVEPOINT` 自保。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from backend.core.clock import utc_now_text
from backend.core.contracts import DOCUMENT_KINDS, StreamEvent
from backend.core.digest import content_fingerprint
from backend.core.engine import session_scope
from backend.core.errors import ErrorCode
from backend.core.request_context import current_request_id
from backend.repositories.chat_session_repo import ChatSessionRepository
from backend.repositories.chat_turn_repo import ChatTurnRepository
from backend.services import document_store, job_state, solution_service
from backend.services.job_bus import job_bus
from backend.services.job_product_kind import _target_product_kind
from backend.services.job_runtime_events import _draft, _run_summary_event, _summary_data

if TYPE_CHECKING:
    from backend.services.job_runtime import JobRuntime

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _ReviewWriteFailure:
    """重审收尾没能把结论写进去时，任务该报的失败码与文案。

    `error_code` 存的是 `ErrorCode` 的**取值**（`str`）：任务终态列与 SSE 的
    `error.code` 都是纯字符串，而前端码表按值逐字镜像后端枚举（见 `core/errors.py`）。
    构造处一律写 `ErrorCode.XXX.value`，不在这一层再拼一份字面量。
    """

    error_code: str
    error_message: str


class _ReviewGateFailure(Exception):
    """重审结论写不进去：**必须**把这次收尾改判失败（一次执行的唯一产出没落库）。

    在成功事务内部抛它 → 那一笔（状态 + 账本 + 结论）整批回滚 → 调用方再开一笔只落
    失败的事务，把「为什么没写成」如实写进任务终态与账本。
    """

    def __init__(self, failure: _ReviewWriteFailure) -> None:
        super().__init__(failure.error_message)
        self.failure = failure


class _CloseoutVersionConflict(Exception):
    """正文写入被版本锁拒绝（就地改写的「我以为当前是第几版」已不是当前版本）。

    在成功事务内部抛它 → 整批回滚（任务不进已定稿）→ 调用方再开一笔失败事务，
    以 `CONTENT_CHANGED` + `VERSION_CONFLICT_MESSAGE` 让用户知道该刷新重试。
    """


# 一次收尾的载荷：正文 / 章节 / 用量 / 待确认清单 / token 两列 / 耗时 / 部分完成标记。
# 与 `job_state.FinishPayload` 同形；抽出来是因为一次收尾可能要发两次（见模块 docstring）。
def _finish_payload(
    runtime: JobRuntime, summary: dict[str, Any], draft: str, *, partial: bool = False
) -> job_state.FinishPayload:
    return job_state.FinishPayload(
        draft_content=draft,
        completed_sections=runtime.completed_sections,
        usage=json.dumps(summary, ensure_ascii=False),
        pending_items=runtime.pending_items,
        prompt_tokens=int(summary.get("prompt_tokens") or 0),
        completion_tokens=int(summary.get("completion_tokens") or 0),
        # 账本要回答「跑了多久」：与 `usage.duration_ms` 同源，一次算好两处用。
        duration_ms=int(summary.get("duration_ms") or 0),
        # 「有内容可用但不完整」（票 #85）：与终态同一条语句落库，界面的两个问题
        #（这次任务怎么结束的 / 这份正文能不能用）因此不会各说各话。
        partial=partial,
    )


def _closeout_once(
    runtime: JobRuntime,
    status: str,
    summary: dict[str, Any],
    draft: str,
    *,
    error_code: str,
    error_message: str,
) -> job_state.TransitionResult:
    """**一个事务**里跑完一次收尾：状态机 → 账本 → 正文/结论，任一步抛错整批回滚。

    顺序与 spec 一致（状态机条件写在前，后续副作用只有「占住终态」才做）：
    `transition` 改到 0 行 = 本次结果被丢弃，那就什么都不写（正文也不写，否则晚到的
    执行者会覆盖别人已经写好的终态；#79 的口径）。

    返回值是 `TransitionResult`；调用方在提交之后才结算内存、写日志、广播回执。

    **执行权（#82）**：收尾的条件写带上本轮的 `lease_token`——状态仍是执行中、凭证仍是
    这一轮、到期时间还没过，三条同时成立才改到那一行。旧执行者的晚到收尾因此是 0 行：
    它随后既不写正文、也不记账本、不留结论，只走调用方那条「结果已丢弃」的分支。
    """
    event = job_state.JobEvent.SUCCEED if status == "completed" else job_state.JobEvent.FAIL
    # 失败收尾可能要把「已经写出来的正文」落成一版部分完成（票 #85）。这份内容在
    # **改终态之前**就算好：任务行的 `partial` 与正文写入必须同一条语句 / 同一个事务，
    # 否则会出现「任务说失败、正文没落」或「正文落了、任务说成功」的错配。
    partial_content = (
        "" if status == "completed" else _partial_document_content(runtime, draft)
    )
    payload = _finish_payload(
        runtime,
        summary,
        draft,
        # 成功收尾的「部分完成」来自 `done.partial`（章节重试用尽后落占位，管线照常收尾）；
        # 失败收尾的则来自「这次真的写出了一版部分完成正文」。
        partial=bool(runtime.partial) if status == "completed" else bool(partial_content),
    )
    with session_scope() as session:
        result = job_state.transition(
            event,
            job_state.job_scope(runtime.owner_id, runtime.job_id),
            payload=payload,
            error_code=error_code,
            error_message=error_message,
            session=session,
            lease_token=runtime.lease_token,
        )
        if not result.applied:
            return result

        if status == "completed":
            if runtime.kind == "review":
                # 重审的产出只有结论：先写结论，写不成整批回滚并改判失败（见下方 catch）。
                gate = _write_review(runtime, session=session)
                if gate is not None:
                    raise _ReviewGateFailure(gate)
            else:
                # 正文写入走唯一入口；版本冲突 / 真实的库异常都从里面抛出来。
                _write_document(runtime, summary, session=session)
        elif runtime.kind == "review":
            # 重审没跑成也要**落成事实**（US3）：面板据此显示「审核失败（时间）」。
            _mark_review_failed(runtime, session=session)
        elif partial_content:
            # 失败（含用户取消）但已经写出了正文：升格成一版「部分完成」（#85）。
            # 与任务终态 / 账本同一个事务——写不进去就整批回滚，不谎报「有内容可用」。
            _write_partial_document(runtime, summary, partial_content, session=session)
        return result


def _publish_gate_failure(
    runtime: JobRuntime, code: str, message: str
) -> None:
    """重审结论写不进去时，给仍连着的订阅者补一条 `error` 事件与结束回执。

    这次失败是收尾里才发现（正文 / 版本是在写入那一刻比的），不像普通失败那样有异常可抛；
    不补这一条，前端只会看到「流读完、任务却失败」。
    """
    job_bus.publish(
        runtime.job_id,
        StreamEvent(
            type="error",
            content=message,
            data={"error_type": "JobFailed"},
            code=code,
        ),
    )


def _finalize(
    runtime: JobRuntime,
    status: str,
    *,
    error_code: str = "",
    error_message: str = "",
) -> None:
    """收尾：置终态、落库（含用量）、**成功时把正文写进 `documents`**、广播 `run_summary`。

    保留运行时（直到 `run_job` 的 finally）让已订阅者把这一条读完。

    **成功与失败都落正文，但落法不同（票 #85）**：

    - 成功 → 追加 / 就地写入这一版的完整正文（失败进不了这条分支）；
    - 失败 / 用户取消 → 已生成的内容**追加**成一版 `partial=true` 的正文（`write_document(mode="append")`），
      不覆写当前完整正文，审核两列显式留空；一个字都没写出来时不落版本。
    - **被取代例外**：那种结局的口径是「本次结果不再写入」，更晚的任务已经接手同一份文档。

    这正是「文档所有权在后端」的关键收益——「谁写、什么时候写、写完之后长什么样」只在
    这一处决定，前端只负责读与提交用户变更。

    ## 幂等闸（#79）
    进来先看内存那份状态：**已经是终态就直接返回**，不落库、不写稿、不广播。
    「排空取消替任务收了一次尾、卡在模型调用里的协程醒来又收一次」这条路径上，
    第二次调用就是在这里被挡掉的——账只有一份、`run_summary` 只有一条。

    内存闸之外还有一道数据库闸：终态写是条件更新（`WHERE status='running'`），
    改到 0 行同样丢弃本次结果（跨进程 / 启动回收那种「别人已经收尾了」的情况）。
    两道闸的口径一致：**以数据库那一行为唯一裁判**，内存只是它的缓存。

    ## 原子收尾（#81）
    成功收尾的五件事在同一事务里（见 `_closeout_once`）。写正文失败时整批回滚，任务
    **保持 `running`**（既不是 completed 的假成功、也不是凭空判死），留一条
    `job.closeout_rolled_back` warning 交给恢复机制。旧实现把写正文异常吞成一条 warning
    并照报 completed——那正是「任务报完成、正文其实没进库」的那条路径。

    Args:
        status: 本次想落的终态（`completed` / `failed`）。
        error_code / error_message: 调用方已知的失败原因（排空取消传 `CANCELLED` 与原因）。
            留空时用运行时的失败意图（上游 `error` 事件 / 兜底异常已经记下）。
    """
    # —— 内存闸：本协程的第二次收尾到此为止 ——
    if runtime.status != job_state.STATUS_RUNNING:
        logger.info(
            "生成任务已是终态，本次收尾丢弃",
            extra={
                "event": "job.finalize_discarded",
                "job_id": runtime.job_id,
                "user_id": runtime.owner_id,
                "status": runtime.status,
                "attempted_status": status,
            },
        )
        return

    # —— 删除与收尾可能赛跑，两道防御都留着 ——
    # 时间窗：用户点删除的那一刻，任务可能正好走到收尾。`delete_solution_and_cancel_jobs`
    # 在事务里先把任务写成终态（`CANCELLED`），提交后才置内存里的 `cancelled`，但收尾可能
    # 已经越过了「检查标记」那一行；反过来，运行时也可能在进程重启后丢失（标记无处可置），
    # 此时只剩「方案还在不在」这一条事实可查。
    # 两道判断覆盖的正是这两个方向，缺任何一道都会留下一条「方案已删、文档却写进去了」
    # 的路径（正文写进一个不存在的 solution_id，前端下次打开就是幽灵文档）。
    if status == "completed":
        if runtime.cancelled:
            status = "failed"
            error_code = job_state.CODE_CANCELLED
            # 对话的「取消」有两种来源，文案必须分开：用户按了停止 vs 方案被删除。
            # 混在一起会让用户在自己点过停止之后读到「方案已删除」，以为数据没了。
            if runtime.kind == "chat" and runtime.chat_interrupted:
                error_message = "已停止生成"
            else:
                error_message = "方案已删除，任务已取消"
        elif runtime.superseded:
            # 同一份文档已经被更晚的任务接手：本次结果不能落库，否则会把新稿盖回旧版。
            # 用 `SUPERSEDED` 而不是 `CANCELLED`：这是「被顶替」，不是「用户删了东西」，
            # 排查时两者的处理方式完全不同（前者要看谁发起了新任务，后者要看删除路径）。
            status = "failed"
            error_code = job_state.CODE_SUPERSEDED
            error_message = "被同一份文档的新任务取代，本次结果不再写入"
        elif runtime.solution_id is not None and not solution_service.solution_exists(
            runtime.owner_id, runtime.solution_id
        ):
            status = "failed"
            error_code = job_state.CODE_CANCELLED
            error_message = "方案已删除，任务已取消"
            logger.warning(
                "生成任务收尾时方案已不存在，不再写入文档",
                extra={
                    "event": "job.solution_gone",
                    "job_id": runtime.job_id,
                    "solution_id": runtime.solution_id,
                },
            )

    draft = _draft(runtime)
    summary = _summary_data(runtime, status)
    # 从汇总里取出两列副本：管理员后台按用户 / 按日统计消耗时用 SQL 求和，
    # 不用把每行 JSON 取回解析。取不到就当 0——宁可少算，不猜数字。
    prompt_tokens = int(summary.get("prompt_tokens") or 0)
    completion_tokens = int(summary.get("completion_tokens") or 0)

    # 与旧 `fail()` 的兜底口径一致：没有明确原因时记成内部错误，而不是留空。
    requested_code = error_code or runtime.error_code or ErrorCode.INTERNAL_ERROR.value
    requested_message = error_message or runtime.error_message or "生成失败"

    try:
        result = _closeout_once(
            runtime,
            status,
            summary,
            draft,
            error_code=requested_code,
            error_message=requested_message,
        )
    except _ReviewGateFailure as failure:
        _finalize_review_gate_failure(runtime, failure.failure, draft)
        return
    except _CloseoutVersionConflict:
        _finalize_version_conflict(runtime, draft)
        return
    except Exception:  # noqa: BLE001 - 收尾整体兜底：整批已回滚，任务保持执行中
        # 这里**不**再判 failed：事务回滚后库里那一行仍是 running，凭空判死会让「这次
        # 没写进去」变成一次说不清的失败；正确处置是保持执行中、留痕、由恢复机制接手。
        logger.warning(
            "收尾事务失败，整批回滚，任务保持执行中（由恢复机制接手）",
            extra={
                "event": "job.closeout_rolled_back",
                "job_id": runtime.job_id,
                "user_id": runtime.owner_id,
                "kind": runtime.kind,
                "attempted_status": status,
                "error_code": requested_code,
                "request_id": current_request_id(),
            },
            exc_info=True,
        )
        return

    if not result.applied:
        # 改到 0 行：本次结果被丢弃（`job_state` 已留丢弃日志），不写稿、不广播。
        job_state.settle_after_commit(
            result, job_state.job_scope(runtime.owner_id, runtime.job_id)
        )
        return

    job_state.settle_after_commit(
        result, job_state.job_scope(runtime.owner_id, runtime.job_id)
    )
    settled_status = runtime.status or status

    # 终态日志：无论成功、失败、被取消还是被顶替，都从这里落一条，让日志页能把
    # 「这一次生成」的结局与 `job.started` 首尾对齐。归属字段带全，
    # `request_id` 显式传（落库线程读不到 ContextVar）。
    logger.info(
        "生成任务收尾：%s",
        settled_status,
        extra={
            "event": f"job.{settled_status}",
            "job_id": runtime.job_id,
            "user_id": runtime.owner_id,
            "solution_id": runtime.solution_id,
            "kind": runtime.kind,
            "duration_ms": summary.get("duration_ms"),
            "total_calls": summary.get("total_calls"),
            "prompt_tokens": summary.get("prompt_tokens"),
            "completion_tokens": summary.get("completion_tokens"),
            "error_code": runtime.error_code,
            "request_id": current_request_id(),
        },
    )

    # 对话的产物是一条轮次，不是文档。**必须在广播之前写**：前端收到 `run_summary`
    # 后会去拉轮次表，晚一步就会拉到「还没有这一轮」的空列表，表现为
    # 「生成完了但界面没这条消息」——而刷新一次又出现了，最难查。
    if runtime.kind == "chat":
        _write_chat_turn(runtime)

    job_bus.publish(runtime.job_id, _run_summary_event(summary))


def _finalize_review_gate_failure(
    runtime: JobRuntime, failure: _ReviewWriteFailure, draft: str
) -> None:
    """重审结论写不进去：第二笔事务**只**落失败（含账本与失败时间戳）。

    第一笔事务（声称成功）已经整批回滚——状态、账本、结论都没留下；这一笔把任务如实
    判为 failed，并补一条 `error` 事件给仍连着的订阅者。
    """
    status = "failed"
    summary = _summary_data(runtime, status)
    try:
        result = _closeout_once(
            runtime,
            status,
            summary,
            draft,
            error_code=failure.error_code,
            error_message=failure.error_message,
        )
        if result.applied:
            job_state.settle_after_commit(
                result, job_state.job_scope(runtime.owner_id, runtime.job_id)
            )
            settled_status = runtime.status or status
            logger.info(
                "生成任务收尾：%s",
                settled_status,
                extra={
                    "event": f"job.{settled_status}",
                    "job_id": runtime.job_id,
                    "user_id": runtime.owner_id,
                    "solution_id": runtime.solution_id,
                    "kind": runtime.kind,
                    "duration_ms": summary.get("duration_ms"),
                    "total_calls": summary.get("total_calls"),
                    "prompt_tokens": summary.get("prompt_tokens"),
                    "completion_tokens": summary.get("completion_tokens"),
                    "error_code": failure.error_code,
                    "request_id": current_request_id(),
                },
            )
            _publish_gate_failure(runtime, failure.error_code, failure.error_message)
            job_bus.publish(runtime.job_id, _run_summary_event(summary))
        else:
            job_state.settle_after_commit(
                result, job_state.job_scope(runtime.owner_id, runtime.job_id)
            )
    except Exception:  # noqa: BLE001 - 失败事务也写不进去：保持执行中，交给恢复机制
        logger.warning(
            "收尾失败事务写不进去，任务保持执行中（由恢复机制接手）",
            extra={
                "event": "job.closeout_rolled_back",
                "job_id": runtime.job_id,
                "user_id": runtime.owner_id,
                "kind": runtime.kind,
                "attempted_status": status,
                "error_code": failure.error_code,
                "request_id": current_request_id(),
            },
            exc_info=True,
        )


def _finalize_version_conflict(runtime: JobRuntime, draft: str) -> None:
    """就地改写被版本锁拒绝：整批回滚后以 `CONTENT_CHANGED` 判失败并说明原因。

    用户看到的正是「正文已被更新，请刷新后重试」——这时候覆盖回去会把别人（通常是刚
    生成完的那一版）的正文悄悄抹掉，比失败更糟。
    """
    status = "failed"
    summary = _summary_data(runtime, status)
    code = ErrorCode.CONTENT_CHANGED.value
    message = document_store.VERSION_CONFLICT_MESSAGE
    try:
        result = _closeout_once(
            runtime,
            status,
            summary,
            draft,
            error_code=code,
            error_message=message,
        )
        if result.applied:
            job_state.settle_after_commit(
                result, job_state.job_scope(runtime.owner_id, runtime.job_id)
            )
            logger.info(
                "生成任务收尾：%s",
                runtime.status or status,
                extra={
                    "event": f"job.{runtime.status or status}",
                    "job_id": runtime.job_id,
                    "user_id": runtime.owner_id,
                    "solution_id": runtime.solution_id,
                    "kind": runtime.kind,
                    "duration_ms": summary.get("duration_ms"),
                    "total_calls": summary.get("total_calls"),
                    "prompt_tokens": summary.get("prompt_tokens"),
                    "completion_tokens": summary.get("completion_tokens"),
                    "error_code": code,
                    "request_id": current_request_id(),
                },
            )
            _publish_gate_failure(runtime, code, message)
            job_bus.publish(runtime.job_id, _run_summary_event(summary))
        else:
            job_state.settle_after_commit(
                result, job_state.job_scope(runtime.owner_id, runtime.job_id)
            )
    except Exception:  # noqa: BLE001 - 失败事务也写不进去：保持执行中，交给恢复机制
        logger.warning(
            "收尾失败事务写不进去，任务保持执行中（由恢复机制接手）",
            extra={
                "event": "job.closeout_rolled_back",
                "job_id": runtime.job_id,
                "user_id": runtime.owner_id,
                "kind": runtime.kind,
                "attempted_status": status,
                "error_code": code,
                "request_id": current_request_id(),
            },
            exc_info=True,
        )


def _expected_version_no(runtime: JobRuntime) -> int | None:
    """任务单上定格的「我以为当前是第几版」（就地改写用；整篇生成不带版本期望）。

    `optimize` / `clarify` / `auto_clarify` 的正文由客户端给，版本号也随任务单一起定格；
    对不上就拒绝写入（见 `document_store.write_document` 的版本锁）。没有这个字段的旧
    客户端不持版本期望，退化成 `None`（不比对），行为与本改造前一致。
    """
    raw = runtime.request.get("expected_version_no")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    return None


def _review_body_changed(runtime: JobRuntime, *, session: Any) -> bool:
    """重审的正文在「建任务那一刻」与「收尾写结论」之间被改过没有。

    被审的那一份正文由 `job_inputs._review_inputs` 在建任务那一刻从库里取，随任务单进
    `runtime.request["document"]`；而结论只能写到「此刻的当前版本」上。两处指纹不一致，
    就说明用户在这段时间里手改过——此时写结论等于**给一份没审过的正文发判定**。

    取不到当前版本（文档被删、方案没了）时返回 `False`：那属于「写不进去」，
    由 `commit_review` 的 `missing` 分支处理，不该在这里报「正文已变更」。

    `session` 是收尾事务：读当前版本与写结论在同一个快照里，避免「比较之后、写入之前」
    再有第二次读看到不同的库状态。
    """
    kind = _document_kind(runtime)
    if kind is None or runtime.solution_id is None:
        return False
    meta = document_store.current_version_meta(
        runtime.owner_id, runtime.solution_id, kind, session=session
    )
    if meta is None:
        return False
    reviewed_sha = content_fingerprint(str(runtime.request.get("document") or ""))
    current_sha = str(meta.get("content_sha") or "")
    return bool(current_sha) and reviewed_sha != current_sha


def _write_chat_turn(runtime: JobRuntime) -> None:
    """把这一轮回复写成一条 `assistant` 轮次（对话收尾的唯一落点）。

    与文档 `_write_document` 的三点不同
    ----------------------------------
    1. **失败也写**：文档失败时半截正文会污染产物，所以不写；对话的半截回复
       是「用户已经看到的内容」，丢掉只会让界面和心理预期对不上。只有正文为空
       （一个字的没出）才跳过。
    2. **停止也写**：用户按停止通常是「够了」而不是「不要了」，轮次上打
       `interrupted` 让界面显示「已停止」。
    3. **落点是 `chat_turns`，不是 `documents`**：对话不产出文档。

    幂等由 `ux_chat_turns_job_role` 保证——重连、重试、取消与正常结束撞车，
    都不可能多写一条。

    写库出错只记日志、不改变任务终态：与 `_write_document` 同一口径，
    正文已经在 `generation_jobs.draft_content` 里有一份，丢的只是「升格成轮次」。
    """
    if runtime.solution_id is None or not runtime.chat_stage:
        return
    if not solution_service.solution_exists(runtime.owner_id, runtime.solution_id):
        return
    # 优先用定稿正文；没有 `done`（出错 / 被停止）时退回「已定稿 + 实时缓冲」的尽力还原
    content = (runtime.final_reply or "").strip() or _draft(runtime).strip()
    if not content:
        return
    try:
        # 轮次与会话 id 必须在**同一个事务**里写：只落轮次而没落会话 id，
        # 下一轮续聊就会重放历史（正确但慢）；反之只落会话 id 而没落轮次，
        # 上下文里会缺一条助手回复——那是不报错的错。
        with session_scope() as session:
            ChatTurnRepository(session).create(
                runtime.owner_id,
                runtime.solution_id,
                stage=runtime.chat_stage,
                role="assistant",
                content=content,
                job_id=runtime.job_id,
                asks=runtime.chat_asks or None,
                tool_calls=runtime.chat_tool_calls or None,
                interrupted=bool(runtime.chat_interrupted),
            )
            # 会话 id 随收尾落库：下一轮续聊直接命中内存记录，省一次历史重放。
            # 它只是加速器——命中不了会自动降级，丢了也不影响正确性。
            if runtime.chat_conversation_id:
                ChatSessionRepository(session).set(
                    runtime.owner_id,
                    runtime.solution_id,
                    runtime.chat_stage,
                    runtime.chat_conversation_id,
                )
    except Exception:  # noqa: BLE001 - 落库失败不该让已经成功的一轮变成失败
        logger.exception(
            "写入对话轮次失败",
            extra={
                "event": "job.chat_turn_failed",
                "job_id": runtime.job_id,
                "solution_id": runtime.solution_id,
            },
        )


def _regenerates_whole_document(kind: str) -> bool:
    """这次任务是否是「整篇重生成上游产物」——只有它会追加新版本并重算上游锚点。

    依据是产物依赖链（`document_store.DOWNSTREAM_KINDS`）：下游由上游正文推导而来，
    上游换了一版，下游就不再成立。反过来，`optimize`（按指令就地优化）与 `clarify`
    （按澄清改写）动的都是**同一份**文档的局部，其它产物仍然是按同一份上游推出来的，
    不该被顺手删掉。

    这个判断现在同时驱动两件事，且二者**恒等**（故合成一个 `append_version` 标志）：
    整篇生成 → 追加新版本 + 重算锚点；就地改写 → 改当前版本、不动锚点。

    **`review_fix` 也算整篇重生成**：它重写的是整篇正文，语义上换了一版产物，
    因此同样追加版本、重算 `derived_from`——下游据此按既有规则亮出过期黄条
    （C-R2：修复追加新版本）。

    ⚠️ 这条判断以前是隐式的（一律 `invalidate_downstream=True`），后果是「优化接口文档」
    会把基于它生成的提示词悄悄删掉——用户看不到任何提示，只是下次打开方案时东西不见了。
    """
    return kind in DOCUMENT_KINDS or kind == "review_fix"


def _document_kind(runtime: JobRuntime) -> str | None:
    """任务要写的产物类型；无法确定时返回 `None`（宁可不写，也不写错位置）。

    直接用 `_target_product_kind`：它与「回执报哪个包」用的是同一份判断，两处不会分叉。

    ⚠️ `optimize` / `clarify` 缺 `doc_kind` 时**刻意返回 None**：猜一个默认值（比如 prd）
    会让「优化接口文档」的结果覆盖掉 PRD 正文，那是**静默的数据损坏**。宁可这次不落库
    （改动只留在当次会话里），也不要写错位置。
    """
    return _target_product_kind(runtime.kind, runtime.request)


def _build_derived_from(
    runtime: JobRuntime, kind: str, *, session: Any
) -> dict[str, Any] | None:
    """为下游版本记录生成时的上游锚点（版本号 + 内容指纹）。

    只对首次 / 整篇重生成生效；`optimize` / `clarify` 是就地改写，沿用旧锚点。
    取不到上游（还没生成 PRD）时不写——空锚点会让后续「是否过期」的判断误报。

    `session` 是收尾事务：锚点读的是与写入同一个快照里的上游版本。
    """
    if kind not in ("api_docs", "prompts") or runtime.solution_id is None:
        return None
    prd = document_store.current_version_meta(
        runtime.owner_id, runtime.solution_id, "prd", session=session
    )
    if not prd:
        return None
    derived: dict[str, Any] = {"prd": prd}
    if kind == "prompts" and (runtime.request.get("api_docs_content") or "").strip():
        api = document_store.current_version_meta(
            runtime.owner_id, runtime.solution_id, "api_docs", session=session
        )
        if api:
            derived["api_docs"] = api
    return {"derived_from": derived}


def _partial_document_content(runtime: JobRuntime, draft: str) -> str:
    """失败收尾时「值得升格成一版「部分完成」」的正文；空串表示没有可落的内容。

    判据有三条，缺一不可：

    1. **不是被顶替**（`runtime.superseded`）：那种结局的口径是「本次结果不再写入」——
       更晚的任务已经接手同一份文档，把这次的半截稿追加成当前版本会把新任务的工作按下去；
    2. **方案还在**：用户删掉方案时，任务走的是「方案已删除，任务已取消」，这时把半截正文
       写进一个已经不存在的方案是没有意义的事（文档会跟着方案一起没）；
    3. **这份任务会产出整篇正文**（`_regenerates_whole_document`）：就地改写（optimize /
       clarify）动的是当前版本的局部，追加一版半截改写会让「当前正文」变成残缺稿，
       而那正是它本来要覆盖的那一版；`review` / `chat` 根本不产正文；
    4. **确实写出了内容**：一个字都没有就没有「可用」可言，落个空版本只是给用户
       一个点开是空的假产物。

    正文取「定稿正文优先、否则尽力还原」：失败的生成通常没有 `done.reply`，
    但若 `done` 已经到、随后才失败，定稿值才是权威的那一份。
    """
    if runtime.superseded:
        return ""
    if runtime.solution_id is None or not solution_service.solution_exists(
        runtime.owner_id, runtime.solution_id
    ):
        # 方案没了 = 用户删掉了这份工作：宁可不写，也不留一份挂在已删方案上的幽灵版本。
        return ""
    if not _regenerates_whole_document(runtime.kind):
        return ""
    if _document_kind(runtime) is None:
        # 没有可挂的产物 → 没有可落的版本（宁可不写，也不写错位置）
        return ""
    content = (runtime.final_reply or "").strip() or draft.strip()
    return content


def _write_partial_document(
    runtime: JobRuntime, summary: dict[str, Any], content: str, *, session: Any
) -> None:
    """把失败 / 取消时已经写出来的正文升格成一版「部分完成」（票 #85）。

    走 #81 的**唯一写入入口** `document_store.write_document(mode="append")`，
    并显式把审核两列传成 `None`：

    - `review=None`：这一版没审过。旧结论属于**别的正文**，搬过来会被读成本次结果；
    - `review_failed_at=None`：这次也没有「审核失败」这件事可记（审核可能压根没跑），
      把上一版的时间带过来同样是在说一件没发生过的事。

    追加（而不是就地改当前版本）：用户手里那份完整正文不能被半截稿替换，部分完成只进
    版本链——它可读、可下载、可恢复，但不会让「当前版本」变成一份残缺稿。
    """
    kind = _document_kind(runtime)
    if kind is None or runtime.solution_id is None:  # pragma: no cover - 调用方已判过
        return
    result = document_store.write_document(
        runtime.owner_id,
        runtime.solution_id,
        kind,
        content,
        mode="append",
        pending_items=runtime.pending_items,
        run_summary=summary,
        # 两列都显式给 None：不带旧结论，也不带旧的审核失败时间（AC2）。
        review=None,
        review_failed_at=None,
        partial=True,
        source=runtime.kind,
        metadata=_build_derived_from(runtime, kind, session=session),
        session=session,
    )
    # `result` 可能为 None：测试注入的替身（记录调用）不返回结果对象。真实入口
    # 总是给 `DocumentWriteResult`，因此只在拿到它时读结果码。
    if result is not None and result.outcome == "missing":
        logger.warning(
            "失败收尾未能写入部分完成版本（方案不存在或归属不符）",
            extra={
                "event": "job.partial_document_write_skipped",
                "job_id": runtime.job_id,
                "kind": kind,
                "solution_id": runtime.solution_id,
            },
        )


def _write_document(
    runtime: JobRuntime, summary: dict[str, Any], *, session: Any
) -> None:
    """把本次任务的权威正文写进 `documents`（成功收尾的唯一落点）。

    用 `done.reply` 而不是 `_draft()`：前者是流水线**定稿**的完整正文，后者是
    「已定稿段落 + 实时缓冲」的尽力还原，两者在正常情况下一致，但定稿值才是权威。

    写入走**唯一入口** `document_store.write_document(..., session=session)`：
    整篇生成 / 修复 / 恢复是 `append`（追加新版本），`optimize` / `clarify` 是
    `replace`（就地改当前版本，带版本锁）。

    无方案可挂（`solution_id` 为空，如快捷入口的逃生口）或归属不符时静默跳过——
    这属于「这次生成没地方落」，不该让一个已经成功的任务变成失败。

    **写库本身出意外不再吞掉**（#81 / AC1）：异常直接抛出，由 `_closeout_once` 的事务
    回滚把「宣布成功 + 账本 + 正文」整批撤销，任务保持执行中。旧实现的
    「记一条 warning 继续报成功」正是「任务报完成、正文其实没进库」。

    **重新审核（`kind='review'`）不走这里**：它不产出正文，收尾做的是「把结论写进
    被审的那一版」（`_write_review`）。
    """
    if runtime.kind == "review":
        # 显式排除，而不是靠「final_reply 恰好为空」这条偶然防线（见 #79 勘察 §3.2）。
        return
    kind = _document_kind(runtime)
    if kind is None or runtime.solution_id is None:
        return
    content = runtime.final_reply
    if not content.strip():
        return
    append_version = _regenerates_whole_document(runtime.kind)
    result = document_store.write_document(
        runtime.owner_id,
        runtime.solution_id,
        kind,
        content,
        mode="append" if append_version else "replace",
        # 整篇生成 / 修复 / 恢复是追加版本：不覆盖当前正文，因此不持版本期望。
        # 就地改写（optimize / clarify / auto_clarify）会覆盖当前正文，必须带版本锁。
        expected_version_no=None if append_version else _expected_version_no(runtime),
        pending_items=runtime.pending_items,
        run_summary=summary,
        # 审核结论与正文**同批写入**：它描述的就是这一版正文，分两次写就会出现
        # 「正文换了、结论还是旧的那条」。没审成时是 `None`，不写占位。
        review=runtime.review,
        # 「审核试过但没跑成」要把失败本身落成事实（`review_failed_at`）：
        # 刷新后面板才仍旧显示「审核失败（时间）」，而不是退化成「未审核」。
        # 两条链路同一列、同一展示：生成时审核失败（PRD / 接口文档 / 提示词）
        # 与修复后的重审失败。判据是运行态上那个**显式的「试过」标记**——
        # 显式关闭审核的入口（快捷流程）连 `review` 事件都不发，因此不写。
        review_failed_at=(
            utc_now_text() if runtime.review is None and runtime.review_attempted else None
        ),
        # 「部分完成」标记跟着这一版正文走（票 #85）：章节重试用尽后落的是失败占位，
        # 管线照常收尾，但这份正文可用而不完整——标记随正文同批写入新版本。
        partial=bool(runtime.partial),
        source=runtime.kind,
        # 只有整篇生成才更新锚点；就地改写沿用当前版本原有的 metadata。
        metadata=_build_derived_from(runtime, kind, session=session)
        if append_version
        else None,
        session=session,
    )
    if result.outcome == "version_conflict":
        raise _CloseoutVersionConflict()
    if result.outcome == "missing":
        logger.warning(
            "生成任务收尾未能写入文档（方案不存在或归属不符）",
            extra={
                "event": "job.document_write_skipped",
                "job_id": runtime.job_id,
                "kind": kind,
                "solution_id": runtime.solution_id,
            },
        )


def _mark_review_failed(runtime: JobRuntime, *, session: Any) -> None:
    """把「这次重审没跑成」记到当前版本上（US3：失败是持久化事实）。

    为什么值得一次写库：失败只说在会话内存里，刷新后就退化成「未审核」——用户再也
    看不出这份正文被审过、且没审成。时间戳落在**被审的那一版**上（与结论同一行），
    重审成功或拿到新结论时由 `commit_review` 清空。

    取不到当前版本（文档被删）时静默跳过：没有「哪一版失败」可言，任务失败本身已经
    写进 `generation_jobs`。

    `session` 是收尾事务：失败时间戳与任务终态 / 账本同一个事务（#81 AC4）。
    """
    kind = _document_kind(runtime)
    if kind is None or runtime.solution_id is None:
        return
    try:
        document_store.mark_review_failed(
            runtime.owner_id, runtime.solution_id, kind, session=session
        )
    except Exception:  # noqa: BLE001 - 记失败不该把收尾再炸一次
        logger.warning(
            "记录审核失败时间失败（面板刷新后会退化成未审核）",
            extra={
                "event": "job.review_failed_not_recorded",
                "job_id": runtime.job_id,
                "kind": kind,
            },
            exc_info=True,
        )


def _write_review(
    runtime: JobRuntime, *, session: Any
) -> _ReviewWriteFailure | None:
    """重新审核收尾：把结论写进**当前版本**的 `review` 列（不追加版本、不改正文）。

    返回 `None` 表示写成功了；否则返回任务该报的失败码与文案——**调用方据此把任务判失败**，
    并且此刻会补一条 `error` 事件给仍在线的订阅者。

    为什么失败必须让任务失败（而不是只记日志）：重审的**唯一产出就是判定**。结论没写进去，
    用户看到「任务成功、结论还是上一次那条」，会以为这次审过了——那是最误导人的结果
    （C-R1：没有结论 ≠ 通过）。

    三处口径：

    - **没有结论就不写**（`runtime.review is None`）：审核没跑成不是「通过」，
      写个占位就是一条假记录（C-R1）；
    - 归属不符 / 目标缺失（`commit_review` 返回 `missing`）：结论无处可写，同样判失败；
    - 写库出意外：判失败。这一次的结论确实没落库，报成功就是撒谎。

    两道条件写各司其职：`_review_body_changed` 是**事前比较**（建任务那一刻的快照
    vs 收尾时的当前版本），`expected_content_sha` 是**写入 SQL 里的条件**（关掉「比较之后、
    写入之前」那点时间窗）。两道都要判失败，结论也都原样保留。

    `session` 是收尾事务：结论与任务终态 / 账本同事务（#81 AC4）。
    """
    kind = _document_kind(runtime)
    if kind is None or runtime.solution_id is None:
        return _ReviewWriteFailure(
            ErrorCode.REVIEW_TARGET_MISSING.value,
            "这份文档已不存在或还没有正文，本次审核结论未写入，请重新生成后再审核",
        )
    if runtime.review is None:
        logger.warning(
            "重新审核没有给出结论，本次不写入（没有结论 ≠ 通过）",
            extra={
                "event": "job.review_skipped",
                "job_id": runtime.job_id,
                "kind": kind,
                "solution_id": runtime.solution_id,
            },
        )
        return _ReviewWriteFailure(
            ErrorCode.LLM_UPSTREAM_ERROR.value,
            "重新审核没有给出结论，本次审核结论未写入，请重新审核",
        )
    if _review_body_changed(runtime, session=session):
        # 结论一旦落在没审过的正文上，它看起来完全正常，却是错的（见 §3.8 / C-R1）。
        logger.warning(
            "重新审核的正文在审的过程中被改过，本次不写入",
            extra={
                "event": "job.review_body_changed",
                "job_id": runtime.job_id,
                "kind": kind,
                "solution_id": runtime.solution_id,
            },
        )
        return _ReviewWriteFailure(
            ErrorCode.CONTENT_CHANGED.value, "正文已变更，本次审核结论未写入，请重新审核"
        )
    reviewed = str(runtime.request.get("document") or "")
    expected_sha = content_fingerprint(reviewed) if reviewed.strip() else None
    try:
        outcome = document_store.commit_review(
            runtime.owner_id,
            runtime.solution_id,
            kind,
            runtime.review,
            expected_content_sha=expected_sha,
            session=session,
        )
    except Exception:  # noqa: BLE001 - 落库意外同样是「这次没写成」，任务照实失败
        logger.warning(
            "重新审核结论写入失败",
            extra={
                "event": "job.review_write_failed",
                "job_id": runtime.job_id,
                "kind": kind,
            },
            exc_info=True,
        )
        return _ReviewWriteFailure(
            ErrorCode.INTERNAL_ERROR.value,
            "审核结论写入失败，本次审核结论未写入，请重新审核",
        )
    if outcome == "ok":
        return None
    if outcome == "content_changed":
        # 第二道闸门命中：比较之后、写入之前，正文被改过。与事前比较同一句文案，
        # 用户不必分辨「是哪一道拦下的」。
        logger.warning(
            "重新审核结论未写入：条件写未命中（正文在写入前变了）",
            extra={
                "event": "job.review_write_skipped",
                "job_id": runtime.job_id,
                "kind": kind,
                "solution_id": runtime.solution_id,
                "outcome": outcome,
            },
        )
        return _ReviewWriteFailure(
            ErrorCode.CONTENT_CHANGED.value, "正文已变更，本次审核结论未写入，请重新审核"
        )
    logger.warning(
        "重新审核结论未写入：目标不存在（%s）",
        outcome,
        extra={
            "event": "job.review_write_skipped",
            "job_id": runtime.job_id,
            "kind": kind,
            "solution_id": runtime.solution_id,
            "outcome": outcome,
        },
    )
    return _ReviewWriteFailure(
        ErrorCode.REVIEW_TARGET_MISSING.value,
        "这份文档已不存在或还没有正文，本次审核结论未写入，请重新生成后再审核",
    )
