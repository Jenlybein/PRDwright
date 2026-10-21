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

from backend.core.chat_kinds import CHAT_STAGES, is_valid_stage
from backend.core.config import get_settings
from backend.core.contracts import DOCUMENT_KINDS, StreamEvent
from backend.core.db import utc_now
from backend.core.errors import AppError, ErrorCode
from backend.repositories.chat_session_repo import ChatSessionRepository
from backend.repositories.chat_turn_repo import ChatTurnRepository, to_history_item
from backend.repositories.generation_job_repo import STATUS_RUNNING, GenerationJobRepository
from backend.services import conversation_service, document_service, document_store, solution_service
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
    # 并行分章节：按 index 存放的定稿段（各章 0..n）。与 finalized_parts 分工：
    # 文档头仍进 finalized_parts（它没有 index），章节进这里，`_draft` 按 index 排序装配。
    parts_by_index: dict[int, str] = field(default_factory=dict)
    # 是否有章节失败后被占位（仅 PRD 并行链路）
    partial: bool = False
    failed_sections: list[dict[str, Any]] = field(default_factory=list)
    # 当前段的实时缓冲（chunk 原始增量），遇到下一个 part 时清空
    live_buffer: str = ""
    # 当前阶段模型的原始输出（审核意见 / 改写稿），只给阶段条下方的面板看。
    # 与 `live_buffer` 分道：后者是**产物正文**，混进来会把审核 JSON 写进文档预览。
    # 从前端内存独占这份文本，刷新就丢；改为运行时持有后，重连的 `snapshot` 能把它带回来。
    stage_text: str = ""
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
    # 是否已被取消（方案被删除时置位）。取消后事件循环不再消费后续事件，
    # 收尾也**不写**文档——见 `cancel_for_solution` / `_finalize`。
    cancelled: bool = False
    # 是否已被**同一份文档的新任务**顶替（`create_job` 在建新任务时置位）。
    #
    # 与 `cancelled` 分开：那个是「方案没了，谁也别写」，这个是「有更晚的结果要写，
    # 你先让开」。两者在收尾时都不写文档，但原因不同，`error_code` 也要能区分
    # （`CANCELLED` vs `SUPERSEDED`）——否则排查时会误以为是用户删了方案。
    #
    # ⚠️ 只落库是不够的：`supersede_active` 改的是**库里那一行**，内存里的旧运行时
    # 完全不知道自己被顶替了，于是它收尾时照样 `_write_document`，把旧稿盖在新稿上。
    # 用户看到的是「新任务跑完了，正文却是上一版的」——不报错，只是内容对不上。
    superseded: bool = False

    # --- 对话任务（`kind='chat'`）专有 ---
    #
    # 对话与文档的关键差别：**对话只追加、不覆盖**。因此它既不需要 `superseded`，
    # 也不能沿用「取消就不落库」的口径——用户按停止，已经说出口的话仍然算数。
    #
    # 这一轮属于哪条线程（`requirement` / `prd-clarify` / …）。四条线程的人设与
    # 上下文互不相通，没有它就分不清「这轮回复该记到谁头上」。
    chat_stage: str = ""
    # 本轮结束后要写的轮次 id（创建任务时已落库的那一条输入轮次）
    chat_input_turn_id: int = 0
    # 后端会话 id（`done` 事件带回）：只用于下一轮续聊，丢了会自动降级为无状态模式
    chat_conversation_id: str = ""
    # 这一轮的提问与它发起的工具调用，都来自 `done`
    chat_asks: list[dict[str, Any]] = field(default_factory=list)
    chat_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    # 被用户中途停止：收尾时保留已生成的部分，并给轮次打上标记
    chat_interrupted: bool = False


# ----------------------------------------------------------------------------
# 对外动作
# ----------------------------------------------------------------------------


def create_job(
    *,
    owner_id: int,
    solution_id: int | None,
    kind: str,
    input_fingerprint: str | None = None,
    request: dict[str, Any] | None = None,
) -> int:
    """新建一次生成任务，返回其 id。

    先 `supersede_active` 把同 owner + solution + kind 下旧的 running 置为 failed，
    保证「同一组合至多一个 running」。随后落一行 running。

    被顶替的不只是库里那一行，**内存里的旧运行时也要标记**（`superseded`）：只改库的话
    旧任务收尾时仍然会 `_write_document`，把旧稿盖在新稿上（不报错，只是内容对不上）。
    标记之后它在下一个事件处跳出循环、收尾落 `failed + SUPERSEDED` 且**不写文档**。

    `request` 是**摊平后的生成参数**（`api/jobs.py::_build_request` 的产物）。创建时就传进来
    是为了算 `doc_kind`：`optimize` / `clarify` 靠 `kind` 定不了产物，只有请求体知道；
    落成列之后，刷新、进程重启、`GET /api/jobs` 都能回答「这个任务改的是哪一份」。
    """
    request = request or {}
    if kind == "chat":
        return _create_chat_job(owner_id, solution_id, request)

    GenerationJobRepository.supersede_active(owner_id, solution_id=solution_id, kind=kind)
    _mark_superseded(owner_id, solution_id=solution_id, kind=kind)
    doc_kind = _target_product_kind(kind, request) or ""
    return GenerationJobRepository.create(
        owner_id, kind, solution_id, input_fingerprint, doc_kind=doc_kind
    )


def _create_chat_job(owner_id: int, solution_id: int | None, request: dict[str, Any]) -> int:
    """新建一轮对话任务：校验线程、拦住并发、把「进去的那一条」先落库。

    为什么对话**不走 `supersede`**
    ------------------------------
    文档的同种任务互相顶替，是因为它们**写同一个位置**：旧任务晚到的结果会把新稿
    覆盖成旧结果。对话只追加，两条并存不会互相破坏；照抄顶替只会得到
    「AI 说到一半消失」这一个纯损失。因此这里改成**有活跃任务就直接拒绝**，
    把「同一时刻只有一轮」变成一条前置条件，而不是一次破坏性动作。

    为什么输入轮次现在就落库
    ------------------------
    ① 它拿到了 id，历史可以按「id 小于它」精确取，不会把本轮输入重复算进上下文；
    ② 它带上了 `job_id`，于是「一个任务两条轮次」由唯一索引兜住，
       重连 / 重试 / 收尾撞车都不可能多写一条。
    """
    stage = str(request.get("stage") or "")
    if not is_valid_stage(stage):
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            f"未知的对话线程：{stage or '（空）'}（可选：{'、'.join(CHAT_STAGES)}）",
        )
    if solution_id is None:
        raise AppError(ErrorCode.INVALID_REQUEST, "对话任务必须关联一份方案")
    if not solution_service.solution_exists(owner_id, solution_id):
        raise AppError(ErrorCode.SOLUTION_NOT_FOUND, "方案不存在")

    # 单写者：同一线程同时只允许一轮在跑。先看内存（权威），再看库（进程重启后的残留）。
    for runtime in _RUNTIMES.values():
        if (
            runtime.owner_id == owner_id
            and runtime.solution_id == solution_id
            and runtime.kind == "chat"
            and runtime.chat_stage == stage
            and runtime.status == STATUS_RUNNING
        ):
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                "这条对话还在生成中，等它说完或先点停止",
            )
    if (
        GenerationJobRepository.find_active(
            owner_id, solution_id=solution_id, kind="chat", chat_stage=stage
        )
        is not None
    ):
        raise AppError(ErrorCode.INVALID_REQUEST, "这条对话还在生成中，等它说完或先点停止")

    chat_mode = str(request.get("chat_mode") or "continue")
    content = str(request.get("content") or "")
    tool_result = request.get("tool_result")
    tool_result = tool_result if isinstance(tool_result, dict) else None

    job_id = GenerationJobRepository.create(
        owner_id, "chat", solution_id, None, doc_kind="", chat_stage=stage
    )

    # 「开新会话」没有用户输入（用户提交的是表单，界面上只显示 AI 的第一句）；
    # 「续聊」一定有一句用户输入。两种都落在同一条 `user`/`tool` 轮次上。
    if chat_mode != "start" and (content.strip() or tool_result is not None):
        role = "tool" if tool_result is not None else "user"
        request["input_turn_id"] = ChatTurnRepository.create(
            owner_id,
            solution_id,
            stage=stage,
            role=role,
            content=content,
            job_id=job_id,
            tool_call_id=str((tool_result or {}).get("tool_call_id") or ""),
            tool_result=tool_result,
            answers=(tool_result or {}).get("answers") if tool_result else None,
        )
    request["job_id"] = job_id
    # 事件流只拿得到 `request`（`_build_event_stream(kind, request)`），
    # 而拼历史与写轮次都要 owner / 方案。放进来而不是另开参数：
    # 这几项本来就属于「这一轮任务的身份」。
    request["owner_id"] = owner_id
    request["solution_id"] = solution_id
    return job_id


def cancel_job(owner_id: int, job_id: int) -> bool:
    """停止一次任务，返回是否确实停下了一个仍在运行的任务。

    「停止」与「退订」必须分开
    -------------------------
    - **退订**（切方案 / 刷新 / 关页面）= 我先不看，任务继续跑，回来还能接上；
    - **停止**（用户按按钮）= 别做了。

    两者混在一起，就会得到这个项目最早的那个 bug 的反面：切走一次，正在生成的
    内容就白等了。因此这里标记的是**运行时**（让事件循环在下一个 tick 跳出），
    而不是断开某个连接。

    收尾由 `_finalize` 负责：文档任务被取消**不写**文档（半截正文会污染产物），
    对话被停止**要写**轮次并标 `interrupted`（用户按停止通常是「够了」，
    已经说出口的话比消失有用）。这个差别在 `_finalize` 里按 `kind` 分支。
    """
    runtime = _RUNTIMES.get(job_id)
    marked = False
    if runtime is not None and runtime.owner_id == owner_id:
        if runtime.status == STATUS_RUNNING:
            runtime.cancelled = True
            if runtime.kind == "chat":
                runtime.chat_interrupted = True
            marked = True
    persisted = GenerationJobRepository.cancel_one(
        owner_id, job_id, message="用户停止了生成"
    )
    return marked or persisted


def _mark_superseded(owner_id: int, *, solution_id: int | None, kind: str) -> int:
    """把内存里同 owner + solution + kind 的旧运行时标记为「已被顶替」，返回条数。

    判据与 `GenerationJobRepository.supersede_active` 逐字一致——两处若分叉，
    就会出现「库里说被取代了、内存里还在跑」这种最难查的组合。
    """
    marked = 0
    for runtime in _RUNTIMES.values():
        if (
            runtime.owner_id == owner_id
            and runtime.solution_id == solution_id
            and runtime.kind == kind
            and not runtime.superseded
        ):
            runtime.superseded = True
            marked += 1
    return marked


def cancel_for_solution(owner_id: int, solution_id: int) -> int:
    """取消某方案下正在跑的全部任务，返回被取消（落库）的条数。

    删除方案时必须走这里：方案没了，任务再跑完也没有地方落正文，继续跑只是白烧 token，
    并且会在收尾那一刻去写一份注定被丢弃的文档。

    两件事都要做，缺一不可：

    - **标记内存运行时**（`cancelled`）：`run_job` 在消费下一个事件前会看到它并跳出循环，
      真正让在途的 LLM 调用不再往文档里写东西；
    - **落库终态**：进程重启后运行时已丢失，库里那些 `running` 必须一并收掉，
      否则刷新页面会看到一个永远「生成中」的任务。

    返回值取落库的受影响条数：那是「有多少任务确实从 running 变成 failed」的权威口径；
    内存标记的数量可能与之不同（运行时可能已经结束，只是行还没被读到）。
    """
    affected = GenerationJobRepository.cancel_running(owner_id, solution_id=solution_id)
    for runtime in _RUNTIMES.values():
        if runtime.owner_id == owner_id and runtime.solution_id == solution_id:
            runtime.cancelled = True
    return affected


def _target_product_kind(kind: str, request: dict[str, Any]) -> str | None:
    """任务要**产出/改写**的是哪一份产物；无法确定时返回 `None`。

    - `prd` / `api_docs` / `prompts`：整篇生成，种类本身就是产物类型。
    - `optimize` / `clarify` / `auto_clarify`：对某一份**已有**文档的动作，产物类型来自请求体的 `doc_kind`。

    返回 `None` 表示取不到（如 `optimize` 漏传 `doc_kind`）。调用方据此**既不注入技能资源
    也不在回执里报技能包身份**——报一个跟实际注入不符的包，比不报更糟。
    """
    if kind in DOCUMENT_KINDS:
        return kind
    if kind in ("optimize", "clarify", "auto_clarify"):
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
        # 对话任务的身份：线程决定「这轮回复记到谁头上」，输入轮次决定「历史取到哪儿为止」
        chat_stage=str((request or {}).get("stage") or "") if kind == "chat" else "",
        chat_input_turn_id=int((request or {}).get("input_turn_id") or 0),
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
                # 方案在生成途中被删除：下一个事件不再消费。
                # 判断放在 `_apply_event` **之前**——取消之后不再往运行时里写任何东西，
                # 收尾时 `_finalize` 也就拿不到新的 `done`，不会把半截过程当成「已产出」。
                #
                # `superseded` 同理：同一份文档已经有更新的任务在写，这一支的结果反正不会
                # 落库，继续烧 token 只是白花钱，而且它的 `done` 还会覆盖运行时的终稿。
                if runtime.cancelled or runtime.superseded:
                    break
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
    - `stage` → 更新 phase（**同时同步给计量器**，计量器据此把调用归到 `类型:阶段` 这一步），
      换阶段时清空上一阶段的输出；
    - `stage_chunk` → 追加到当前阶段的输出（审核意见 / 改写稿），与正文缓冲互不干扰；
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
        runtime.live_buffer = ""
        if event.data.get("replace") is True:
            # 下游改写稿整段覆盖初稿（审核发现问题后的那一次改写）
            runtime.finalized_parts = [text]
            runtime.parts_by_index.clear()
            return
        index = event.data.get("index")
        if isinstance(index, int):
            runtime.parts_by_index[index] = text
        else:
            runtime.finalized_parts.append(text)
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
    elif event_type == "stage":
        stage = str(event.data.get("stage", ""))
        # 换阶段就清空上一阶段的输出：审核意见不该留在改写阶段的面板里
        if stage != runtime.phase:
            runtime.stage_text = ""
        runtime.phase = stage
        runtime.meter.set_phase(runtime.phase)
    elif event_type == "error":
        runtime.failed = True
        runtime.error_code = event.code or ErrorCode.INTERNAL_ERROR.value
        runtime.error_message = event.content
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
    """尽力还原的完整正文：已定稿部分 + 当前缓冲（**不用原始 chunk 拼**）。"""
    return _finalized_text(runtime) + runtime.live_buffer


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
        # 部分完成：正文可用但不完整。随 usage 落库，刷新后 `JobStatus.partial` 仍读得到。
        "partial": runtime.partial,
        "failed_sections": runtime.failed_sections,
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
    # —— 删除与收尾可能赛跑，两道防御都留着 ——
    # 时间窗：用户点删除的那一刻，任务可能正好走到收尾。`cancel_for_solution` 先置
    # `cancelled` 再落库，但收尾可能已经越过了「检查标记」那一行；反过来，运行时也可能
    # 在进程重启后丢失（标记无处可置），此时只剩「方案还在不在」这一条事实可查。
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
        ChatTurnRepository.create(
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
            ChatSessionRepository.set(
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
            # 快捷流程显式传 False（只撰写、不审不改）；`None` 时由服务层取配置
            review_enabled=request.get("review_enabled"),
        )
    if kind == "api_docs":
        return document_service.generate_api_docs_stream(
            prd_content=request.get("prd_content", "") or "",
            # 快捷流程显式传 False：整篇一遍过，不审也不改（ADR-034）
            review_enabled=request.get("review_enabled"),
        )
    if kind == "prompts":
        return document_service.generate_prompts_stream(
            prd_content=request.get("prd_content", "") or "",
            # 可选：来自「我有 PRD + 接口文档」入口。留空时生成口径与从前完全一致
            api_docs_content=request.get("api_docs_content", "") or "",
            # 与接口文档同口径：快捷流程一遍过
            review_enabled=request.get("review_enabled"),
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
    if kind == "auto_clarify":
        return document_service.auto_clarify_stream(
            document=request.get("document", "") or "",
            # 清单是**结构化数据**：提示词怎么用它由后端决定，前端不参与措辞
            pending_items=request.get("pending_items") or [],
            doc_kind=_target_product_kind(kind, request) or "",
        )
    if kind == "chat":
        return _chat_event_stream(request)
    raise ValueError(f"未知的生成任务类型：{kind}")


def _chat_event_stream(request: dict[str, Any]) -> AsyncIterator[StreamEvent]:
    """一轮对话的事件流：历史由后端组装，前端不再回传。"""
    return _chat_events(request)


async def _chat_events(request: dict[str, Any]) -> AsyncIterator[StreamEvent]:
    """驱动一轮对话，产生与文档任务同一套 `StreamEvent`。

    三个关键取舍
    ------------
    1. **历史从 `chat_turns` 拼**，不再由前端随请求回传。前端持有历史就等于
       前端是权威：两台机器各持一份，谁对？现在只有一个来源，且它与
       「另一台电脑看到什么」是同一份。
    2. **首轮把开场白存成一条 `system` 轮次**。开场白（含表单/文档内容）是喂给
       模型却不展示给用户的那部分上下文；不存它，第二轮模型就只看到自己上一句
       回复，完全不知道用户提交过什么。
    3. **会话 id 只是个加速器，不是依赖**。拿它续聊命中内存记录时省一次历史重放；
       命中不了（进程重启）时收到 `SESSION_NOT_FOUND`，就地清掉并改用历史重试——
       这段降级原先写在前端，现在归后端：前端不该知道「会话会不会过期」。
    """
    owner_id = int(request.get("owner_id") or 0)
    solution_id = int(request.get("solution_id") or 0)
    stage = str(request.get("stage") or "")
    profile_key = str(request.get("profile_key") or "")
    chat_mode = str(request.get("chat_mode") or "continue")
    service = conversation_service.get_service()

    if chat_mode == "start":
        # 未注册的档案在这里抛 `AppError`：它发生在流开始之前，路由层还能变成 400
        conversation_service.check_profile_key(profile_key)
        form_data = request.get("form_data")
        form_data = form_data if isinstance(form_data, dict) else {}

        def _persist_opening(text: str) -> None:
            """把开场白落成 `system` 轮次（不展示，只作为后续轮次的上下文）。"""
            if solution_id and text.strip():
                ChatTurnRepository.create(
                    owner_id,
                    solution_id,
                    stage=stage,
                    role="system",
                    content=text,
                    job_id=int(request.get("job_id") or 0) or None,
                )

        async for event in service.start_conversation_stream(
            form_data,
            title=str(request.get("title") or ""),
            profile_key=profile_key,
            on_opening=_persist_opening,
        ):
            yield event
        return

    input_turn_id = int(request.get("input_turn_id") or 0)
    rows = (
        ChatTurnRepository.list_history(
            owner_id, solution_id, stage, before_id=input_turn_id
        )
        if input_turn_id
        else []
    )
    history = [to_history_item(row) for row in rows]
    content = str(request.get("content") or "")
    tool_result = request.get("tool_result")
    tool_result = tool_result if isinstance(tool_result, dict) else None
    conversation_id = ChatSessionRepository.get(owner_id, solution_id, stage)

    def _stream(cid: str) -> AsyncIterator[StreamEvent]:
        return service.continue_conversation_stream(
            content,
            conversation_id=cid or None,
            history=history,
            profile_key=profile_key,
            tool_result=tool_result,
        )

    retried = False
    cid = conversation_id
    while True:
        stale = False
        async for event in _stream(cid):
            if (
                cid
                and not retried
                and event.type == "error"
                and event.code == ErrorCode.SESSION_NOT_FOUND.value
            ):
                stale = True
                break
            yield event
        if not stale:
            return
        # 会话在服务内存里已不存在（重启 / 多实例）：清掉 id，改用历史重放一次。
        # 只重试一次——再失败就是真的失败，无限重试会把一次故障放大成一次风暴。
        retried = True
        cid = ""
        ChatSessionRepository.set(owner_id, solution_id, stage, "")
