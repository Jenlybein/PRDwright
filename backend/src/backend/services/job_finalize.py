"""任务收尾与终态落库（`job_service` 的拆分子模块）。

从 `job_service` **机械迁出**（issue #5），函数体逐字未改：`_finalize` 置终态、
落用量、成功时调 `_write_document`、对话收尾调 `_write_chat_turn`、最后广播
`run_summary`；`_document_kind` / `_regenerates_whole_document` /
`_build_derived_from` / `_write_document` 决定「写哪份产物、追加还是就地改」。

事务边界不变：每个落库点仍是 `session_scope()` 各自一个短事务。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from backend.core.contracts import DOCUMENT_KINDS
from backend.core.engine import session_scope
from backend.core.errors import ErrorCode
from backend.core.request_context import current_request_id
from backend.repositories.chat_session_repo import ChatSessionRepository
from backend.repositories.chat_turn_repo import ChatTurnRepository
from backend.repositories.generation_job_repo import GenerationJobRepository
from backend.services import document_store, solution_service
from backend.services.job_bus import job_bus
from backend.services.job_product_kind import _target_product_kind
from backend.services.job_runtime_events import _draft, _run_summary_event, _summary_data

if TYPE_CHECKING:
    from backend.services.job_runtime import JobRuntime

logger = logging.getLogger(__name__)


def _finalize(runtime: JobRuntime, status: str) -> None:
    """收尾：置终态、落库（含用量）、**成功时把正文写进 `documents`**、广播 `run_summary`。

    保留运行时（直到 `run_job` 的 finally）让已订阅者把这一条读完。

    **只有成功才写文档**：失败的任务保留上一次可用的正文（半截输出不该顶掉一份
    完整文档）。这正是「文档所有权在后端」的关键收益——「谁写、什么时候写、
    写完之后长什么样」只在这一处决定，前端只负责读与提交用户变更。
    """
    # —— 删除与收尾可能赛跑，两道防御都留着 ——
    # 时间窗：用户点删除的那一刻，任务可能正好走到收尾。`delete_solution_and_cancel_jobs`
    # 在事务里先把任务写成终态（`CANCELLED`），提交后才置内存里的 `cancelled`，但收尾可能
    # 已经越过了「检查标记」那一行；反过来，运行时也可能在进程重启后丢失（标记无处可置），
    # 此时只剩「方案还在不在」这一条事实可查。
    # 两道判断覆盖的正是这两个方向，缺任何一道都会留下一条「方案已删、文档却写进去了」
    # 的路径（正文写进一个不存在的 solution_id，前端下次打开就是幽灵文档）。
    if status == "completed":
        abort_code = ""
        abort_message = ""
        if runtime.cancelled:
            abort_code = "CANCELLED"
            # 对话的「取消」有两种来源，文案必须分开：用户按了停止 vs 方案被删除。
            # 混在一起会让用户在自己点过停止之后读到「方案已删除」，以为数据没了。
            if runtime.kind == "chat" and runtime.chat_interrupted:
                abort_message = "已停止生成"
            else:
                abort_message = "方案已删除，任务已取消"
        elif runtime.superseded:
            # 同一份文档已经被更晚的任务接手：本次结果不能落库，否则会把新稿盖回旧版。
            # 用 `SUPERSEDED` 而不是 `CANCELLED`：这是「被顶替」，不是「用户删了东西」，
            # 排查时两者的处理方式完全不同（前者要看谁发起了新任务，后者要看删除路径）。
            abort_code = "SUPERSEDED"
            abort_message = "被同一份文档的新任务取代，本次结果不再写入"
        elif runtime.solution_id is not None and not solution_service.solution_exists(
            runtime.owner_id, runtime.solution_id
        ):
            abort_code = "CANCELLED"
            abort_message = "方案已删除，任务已取消"
            logger.warning(
                "生成任务收尾时方案已不存在，不再写入文档",
                extra={
                    "event": "job.solution_gone",
                    "job_id": runtime.job_id,
                    "solution_id": runtime.solution_id,
                },
            )
        if abort_code:
            # 语义上确实「没有成功」：复用 failed 而不是新造状态（状态值是前后端共享契约，
            # 新增取值会让前端每一处 status 分支都要多一条分支）。
            status = "failed"
            runtime.failed = True
            runtime.error_code = abort_code
            runtime.error_message = abort_message

    runtime.status = status
    draft = _draft(runtime)
    summary = _summary_data(runtime, status)
    usage = json.dumps(summary, ensure_ascii=False)
    # 终态日志：无论成功、失败、被取消还是被顶替，都从这里落一条，
    # 让日志页能把「这一次生成」的结局与 `job.started` 首尾对齐。
    # 归属字段带全，`request_id` 显式传（落库线程读不到 ContextVar）。
    logger.info(
        "生成任务收尾：%s",
        status,
        extra={
            "event": f"job.{status}",
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
    # 从汇总里取出两列副本：管理员后台按用户 / 按日统计消耗时用 SQL 求和，
    # 不用把每行 JSON 取回解析。取不到就当 0——宁可少算，不猜数字。
    prompt_tokens = int(summary.get("prompt_tokens") or 0)
    completion_tokens = int(summary.get("completion_tokens") or 0)
    # 供应商四列已在**建任务那一刻**从 `config_snapshot` 里的同一份定格一次写全
    # （见 `GenerationJobRepository.create`）。收尾不再从 `summary` 里另取四格、
    # 不再重读配置或另算一遍：配置会被改名/删除，回查等于让历史账单跟着今天的状态变。
    # `usage` JSON、token 两列、错误码与终态语义都不改。
    if status == "completed":
        with session_scope() as session:
            GenerationJobRepository(session).finish(
                runtime.owner_id,
                runtime.job_id,
                draft_content=draft,
                completed_sections=runtime.completed_sections,
                usage=usage,
                pending_items=runtime.pending_items,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
        _write_document(runtime, summary)
    else:
        with session_scope() as session:
            GenerationJobRepository(session).fail(
                runtime.owner_id,
                runtime.job_id,
                error_code=runtime.error_code or ErrorCode.INTERNAL_ERROR.value,
                error_message=runtime.error_message or "生成失败",
                draft_content=draft,
                completed_sections=runtime.completed_sections,
                # 失败的任务同样烧了 token，用量一并落库——不然「这次为什么这么贵」无从追溯
                usage=usage,
                pending_items=runtime.pending_items,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

    # 对话的产物是一条轮次，不是文档。**必须在广播之前写**：前端收到 `run_summary`
    # 后会去拉轮次表，晚一步就会拉到「还没有这一轮」的空列表，表现为
    # 「生成完了但界面没这条消息」——而刷新一次又出现了，最难查。
    if runtime.kind == "chat":
        _write_chat_turn(runtime)

    job_bus.publish(runtime.job_id, _run_summary_event(summary))


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
    """这次任务是否是「整篇重生成上游产物」——只有它会连带作废下游、且追加新版本。

    依据是产物依赖链（`document_store.DOWNSTREAM_KINDS`）：下游由上游正文推导而来，
    上游换了一版，下游就不再成立。反过来，`optimize`（按指令就地优化）与 `clarify`
    （按澄清改写）动的都是**同一份**文档的局部，其它产物仍然是按同一份上游推出来的，
    不该被顺手删掉。

    这个判断现在同时驱动两件事，且二者**恒等**（故合成一个 `append_version` 标志）：
    整篇生成 → 追加新版本 + 删下游；就地改写 → 改当前版本、不删下游。

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


def _build_derived_from(runtime: JobRuntime, kind: str) -> dict[str, Any] | None:
    """为下游版本记录生成时的上游锚点（版本号 + 内容指纹）。

    只对首次 / 整篇重生成生效；`optimize` / `clarify` 是就地改写，沿用旧锚点。
    取不到上游（还没生成 PRD）时不写——空锚点会让后续「是否过期」的判断误报。
    """
    if kind not in ("api_docs", "prompts") or runtime.solution_id is None:
        return None
    prd = document_store.current_version_meta(runtime.owner_id, runtime.solution_id, "prd")
    if not prd:
        return None
    derived: dict[str, Any] = {"prd": prd}
    if kind == "prompts" and (runtime.request.get("api_docs_content") or "").strip():
        api = document_store.current_version_meta(
            runtime.owner_id, runtime.solution_id, "api_docs"
        )
        if api:
            derived["api_docs"] = api
    return {"derived_from": derived}


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
    append_version = _regenerates_whole_document(runtime.kind)
    try:
        written = document_store.commit_generated(
            runtime.owner_id,
            runtime.solution_id,
            kind,
            content,
            pending_items=runtime.pending_items,
            run_summary=summary,
            # 审核结论与正文**同批写入**：它描述的就是这一版正文，分两次写就会出现
            # 「正文换了、结论还是旧的那条」。没审成时是 `None`，不写占位。
            review=runtime.review,
            # `append_version=True`（整篇生成）同时意味着「追加新版本」；
            # `False`（optimize / clarify）就是就地改当前版本。下游不再删除，
            # 是否过期由读取侧比较 derived_from 得出。
            append_version=append_version,
            # 版本来源标签：整篇生成时是产物类型，就地改写时是动作类型（optimize / clarify）
            source=runtime.kind,
            # 只有整篇生成才更新锚点；就地改写沿用当前版本原有的 metadata。
            metadata=_build_derived_from(runtime, kind) if append_version else None,
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
