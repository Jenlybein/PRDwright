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
import logging
import time
from typing import Any, AsyncIterator

from backend.core.chat_kinds import CHAT_STAGES, is_valid_stage
from backend.core.contracts import StreamEvent
from backend.core.engine import session_scope
from backend.core.errors import AppError, ErrorCode
from backend.core.runtime_config import get_runtime_config, use_config
from backend.core.request_context import current_request_id
from backend.repositories.chat_session_repo import ChatSessionRepository
from backend.repositories.chat_turn_repo import ChatTurnRepository, to_history_item
from backend.repositories.generation_job_repo import STATUS_RUNNING, GenerationJobRepository
from backend.repositories.solution_repo import SolutionRepository
from backend.services import conversation_service, document_service, solution_service
from backend.services.job_bus import job_bus
from backend.services.llm_meter import RunMeter, use_meter
from backend.services.skill_registry import describe_kind

# ----------------------------------------------------------------------------
# 内部实现已按职责拆到同目录的模块（单文件阈值）。
# 下面全部**再导出**：`job_service` 的对外形状与拆分前逐字一致，调用方（含测试）
# 仍然只从本模块取这些名字。
# ----------------------------------------------------------------------------
from backend.services.job_finalize import (  # noqa: F401 - 再导出
    _build_derived_from,
    _document_kind,
    _finalize,
    _regenerates_whole_document,
    _write_chat_turn,
    _write_document,
)
from backend.services.job_product_kind import _target_product_kind  # noqa: F401 - 再导出
from backend.services.job_runtime import (  # noqa: F401 - 再导出
    _RUNTIMES,
    _SectionLive,
    _TASKS,
    JobRuntime,
)
from backend.services.job_runtime_events import (  # noqa: F401 - 再导出
    _FLUSH_BYTES,
    _FLUSH_INTERVAL_SECONDS,
    _apply_event,
    _draft,
    _finalized_text,
    _live_len,
    _maybe_flush,
    _run_summary_event,
    _summary_data,
)
from backend.services.job_snapshot import (  # noqa: F401 - 再导出
    _db_snapshot_event,
    _elapsed_ms_from_row,
    _load_pending_items,
    _snapshot_event,
    parse_usage,
)

logger = logging.getLogger(__name__)

# 失败收尾里「不是错误」的两个中止码：取消（用户走开 / 删方案）与被顶替（同一份
# 文档已有更新的任务接手）。它们同样落 failed 状态，但不该作为红色错误弹给用户，
# 因此补发 `error` 事件时要排除——否则「用户自己按了停止」会被渲染成「生成失败」。
_ABORT_CODES = frozenset({"CANCELLED", "SUPERSEDED"})


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

    with session_scope() as session:
        GenerationJobRepository(session).supersede_active(
            owner_id, solution_id=solution_id, kind=kind
        )
    _mark_superseded(owner_id, solution_id=solution_id, kind=kind)
    doc_kind = _target_product_kind(kind, request) or ""
    with session_scope() as session:
        job = GenerationJobRepository(session).create(
            owner_id, kind, solution_id, input_fingerprint, doc_kind=doc_kind
        )
        return int(job.id or 0)


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
    with session_scope() as session:
        active = GenerationJobRepository(session).find_active(
            owner_id, solution_id=solution_id, kind="chat", chat_stage=stage
        )
    if active is not None:
        raise AppError(ErrorCode.INVALID_REQUEST, "这条对话还在生成中，等它说完或先点停止")

    chat_mode = str(request.get("chat_mode") or "continue")
    content = str(request.get("content") or "")
    tool_result = request.get("tool_result")
    tool_result = tool_result if isinstance(tool_result, dict) else None

    with session_scope() as session:
        job = GenerationJobRepository(session).create(
            owner_id, "chat", solution_id, None, doc_kind="", chat_stage=stage
        )
        job_id = int(job.id or 0)

    # 「开新会话」没有用户输入（用户提交的是表单，界面上只显示 AI 的第一句）；
    # 「续聊」一定有一句用户输入。两种都落在同一条 `user`/`tool` 轮次上。
    if chat_mode != "start" and (content.strip() or tool_result is not None):
        role = "tool" if tool_result is not None else "user"
        # 只包住「写这一条轮次」，不包住创建任务前后的其它调用：
        # 事务越窄，撞锁的窗口越小，出错时也越好定位。
        with session_scope() as session:
            request["input_turn_id"] = ChatTurnRepository(session).create(
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


def cancel_job(owner_id: int, job_id: int, *, message: str = "用户停止了生成") -> bool:
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

    `message` 是落进 `error_message` 的终止原因：用户点停止写「用户停止了生成」，
    管理员在后台清理卡死任务写「管理员手动停止」——两者都走 `CANCELLED`，
    但运维看这一行时要知道是谁动的手。
    """
    runtime = _RUNTIMES.get(job_id)
    marked = False
    if runtime is not None and runtime.owner_id == owner_id:
        if runtime.status == STATUS_RUNNING:
            runtime.cancelled = True
            if runtime.kind == "chat":
                runtime.chat_interrupted = True
            marked = True
    with session_scope() as session:
        persisted = GenerationJobRepository(session).cancel_one(
            owner_id, job_id, message=message
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


def delete_solution_and_cancel_jobs(owner_id: int, solution_id: int) -> bool:
    """删除方案，并在**同一个事务**里把挂在它上面的在飞任务收成终态。

    为什么合成一个函数
    ------------------
    「删方案」与「取消它的任务」原先由路由层分两次调用（各自一个事务），于是存在
    一个真实的坏中间态：方案行已删掉、取消却没跑成——库里留下一批 `running` 的孤儿任务，
    它们既没有归属可写正文，又会在重启后被 `reap_orphans` 当成「进程崩了」再报一次失败。

    现在两件事同生共死：要么方案没了且任务已收，要么两件都没发生。
    事务内先删再取消——顺序不影响原子性，但先判「方案在不在」能顺带挡掉
    「方案不存在却把它的任务改成 failed」这种越界写。

    为什么放在 `job_service` 而不是 `solution_service`
    -------------------------------------------------
    在途任务的协作式取消标记（`_RUNTIMES`）住在同目录的 `job_runtime.py`（#5 拆模块时迁出，
    本模块只再导出）；而本模块已经依赖 `solution_service`（收尾前要确认方案还在），
    让 `solution_service` 反向 import 就会成环。这里直接用仓储——`repositories` 是更下一层，
    不构成环。

    Returns:
        方案是否存在并被删除。`False` 表示不存在或不属于该用户（路由翻 404）。
    """
    with session_scope() as session:
        # 同一条事务里**先删再取消**：事务内的顺序不影响原子性，但先判「方案在不在」
        # 可以避免「方案不存在却把它的任务改成 failed」这种越界写。
        deleted = SolutionRepository(session).delete(owner_id, solution_id)
        if not deleted:
            return False
        GenerationJobRepository(session).cancel_running(owner_id, solution_id=solution_id)

    # 落库之后才置内存标记：事务失败时不会留下「协程停了、库里还在跑」的错位。
    # 这一刻到协程下一个事件点之间的窗口由 `_finalize` 的「方案不存在就不写文档」兜住。
    for runtime in _RUNTIMES.values():
        if runtime.owner_id == owner_id and runtime.solution_id == solution_id:
            runtime.cancelled = True
    return True


def cancel_for_user(owner_id: int, *, message: str = "账号已删除，任务已取消") -> int:
    """取消某用户名下正在跑的全部任务，返回落库条数（删除账号前必须调用）。

    为什么删号前必须先停任务
    ------------------------
    任务与浏览器连接是**解耦**的（刷新只退订、不中断），所以「删账号」这件事
    并不会让在途的协程自己停下来：它还会继续调上游、继续烧 token，直到跑完才发现
    方案和账号都没了。

    `_finalize` 里确实有一道兜底（方案不存在就不写文档），但那只是**不写坏数据**，
    并不省钱。真正让它停下的只有内存运行时上的 `cancelled` 标记。

    与 `cancel_job`（单条）和 `delete_solution_and_cancel_jobs`（单方案）同一套机制，区别只有
    过滤维度是 `owner_id`。
    """
    with session_scope() as session:
        affected = GenerationJobRepository(session).cancel_running_for_owner(
            owner_id, message=message
        )
    for runtime in _RUNTIMES.values():
        if runtime.owner_id == owner_id and runtime.status == STATUS_RUNNING:
            runtime.cancelled = True
    return affected


def running_runtimes() -> list[JobRuntime]:
    """当前在途（`status == running`）的运行时的快照。

    调用方是排空（`services/job_drain.py`）：它要「等在途任务跑完、再挑出还没跑完的」，
    需要一份稳定快照，而不是随任务收尾不断变化的视图。快照之后自然结束的任务会在
    `cancel_in_flight` 里按当时的 `status` 被排除。

    为什么判据用运行时上的 `status` 而不是 `_TASKS`：任务集合只是保活用的强引用，
    它不知道「这个任务是不是已经收尾了」；运行时上的 `status` 才是权威。

    这是**公开入口**：排空不该伸手进 `_RUNTIMES`。工程上「谁可以直接读进程内登记表」
    只有本模块一处，换了存储结构时改动面因此只有这个文件。
    """
    return [
        runtime for runtime in list(_RUNTIMES.values()) if runtime.status == STATUS_RUNNING
    ]


def cancel_in_flight(reason: str, *, runtimes: list[JobRuntime] | None = None) -> int:
    """把在途任务收成「已取消」终态，返回被取消的条数。

    调用方是排空（`services/job_drain.py`）：它在等满窗口之后，把仍卡在模型调用里、
    自己走不出来的任务收掉。**这是那条路径唯一的公开入口**——`_failure_event` /
    `_finalize` / `_RUNTIMES` 都是本模块的私有名，排空只调这一个函数。

    Args:
        reason: 落进 `error_message` 与 `error` 事件的终止原因。文案要说清**是谁**动的手
            （例如「服务正在重启」），否则前端与运维会把排空取消当成生成失败。
        runtimes: 要取消的运行时。`None` 表示「当前全部在途任务」。排空传的是它等待
            之前的那份快照，这样「取消数 ≤ 开始时在途数」由构造保证，汇总里的三个
            数字不可能互相对不上。

    Returns:
        真正置为取消的条数；快照里已经自然收尾的任务会被跳过、不计数。

    实现上三步顺序不能换：**先发错误事件、再收尾**。`_finalize` 结尾会广播
    `run_summary`，订阅者按「error → run_summary」读到的是一条完整的坏消息；反过来
    先 `run_summary` 的话，前端会在收到「已结束」之后才收到原因。
    末端那条 `job_bus.close()` 是给订阅者的哨兵：超时者可能正卡在模型调用里，
    等它自己的 `finally` 放哨兵就是「再等一个 30 秒」。
    """
    targets = running_runtimes() if runtimes is None else list(runtimes)
    cancelled = 0
    for runtime in targets:
        if runtime.status != STATUS_RUNNING:
            # 快照之后已经自然收尾：它有自己更准确的结论，别再覆盖成「被取消」。
            continue
        cancelled += 1
        runtime.cancelled = True
        runtime.failed = True
        runtime.error_code = "CANCELLED"
        runtime.error_message = reason
        if runtime.kind == "chat":
            # 对话的「被中止」要保留已经说出口的话（`_write_chat_turn` 按这一位处理）
            runtime.chat_interrupted = True

        logger.warning(
            "取消在途任务",
            extra={
                "event": "job.cancel_in_flight",
                "job_id": runtime.job_id,
                "user_id": runtime.owner_id,
                "kind": runtime.kind,
                "error_code": runtime.error_code,
                "reason": reason,
            },
        )
        # 错误事件走 `_failure_event`（与异常兜底同一个构造）：订阅者只看有没有
        # `error` 事件判断失败，自造一个形状不同的错误事件等于让前端多一条解析分支。
        job_bus.publish(runtime.job_id, _failure_event(runtime.error_code, runtime.error_message))
        _finalize(runtime, "failed")
        # 收尾的哨兵由本任务自己的 `finally` 放；超时者可能卡在模型调用里，这里替它放。
        job_bus.close(runtime.job_id)
    return cancelled


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
    config = get_runtime_config()
    # 这一笔 meter 记的是「这次任务**实际**用了哪条配置、哪个模型」——所以取使用中的那条，
    # 而不是 `DEFAULT_LLM_PROVIDER`：后台切过配置之后，后者可能与实际发出的请求不符，
    # 回执与账单就对不上了。
    #
    # 四个字段分开传：`protocol` 用于查限值/聚合账单，`config_id` 用于定位到具体一条，
    # `name` 是给人看的快照。合成一个字符串会在「同一协议两条配置」时分不清账。
    spec = config.active_provider
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
            protocol=spec.protocol if spec is not None else "",
            model=spec.model if spec is not None else "",
            config_id=spec.id if spec is not None else "",
            name=spec.name if spec is not None else "",
        ),
        config=config,
    )
    runtime.skill_refs = _resolve_skill_refs(kind, runtime.request)

    task = asyncio.create_task(run_job(runtime))
    runtime.task = task
    # 先登记运行时再登记任务：两者都在 create_task 之后、任何 await 之前完成，
    # 因此「任务真正开始跑」之前，运行时一定已可见（重连/订阅不会落空）。
    _RUNTIMES[job_id] = runtime
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    # 任务起点的 INFO 日志：让日志页能按 job_id 串起「这一次生成」的完整生命线。
    # request_id 在此显式带上——`create_task` 复制了 ContextVar，但落库线程读不到它
    # （见 logging_setup._row 的说明），只有 `extra=` 里显式传的才会进 `app_logs`。
    logger.info(
        "生成任务已派发",
        extra={
            "event": "job.started",
            "job_id": job_id,
            "user_id": owner_id,
            "solution_id": solution_id,
            "kind": kind,
            "request_id": current_request_id(),
        },
    )


async def run_job(runtime: JobRuntime) -> None:
    """后台任务主体：驱动事件流、更新内存、广播、按节奏落库、收尾。

    `use_meter` 包住整条事件流的消费过程：计量器是环境值，流水线深处的
    `stream_reply.astream_text` 靠它把自己记到这次任务上（见 `llm_meter`）。
    """
    try:
        # `use_config` 把派发那一刻的配置冻进上下文：整条生成链路（含并行子任务）
        # 读到的是同一份。任务跑到一半有人改配置，也不会让同一次生成前后参数不一致。
        with use_meter(runtime.meter), use_config(runtime.config):
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
        # 管线可能已把错误转成 `error` 事件（运行时因此带着错误码），别重复发第二条。
        already_reported = bool(runtime.error_code)
        if isinstance(exc, AppError):
            # 异常自带的码更准：预算超顶是 INVALID_REQUEST（重试也没用），
            # 不该被笼统记成「上游故障」，否则前端会去做无意义的重试。
            runtime.error_code = exc.code.value
            runtime.error_message = exc.message
        else:
            runtime.error_code = ErrorCode.LLM_UPSTREAM_ERROR.value
            runtime.error_message = f"生成失败：{exc}"
        # ⚠️ 兜底异常**必须广播**：前端只有收到 `error` 事件才判失败。只改运行时
        # 而不发事件，界面上就是「流结束、正文为空」——被当成生成成功（空内容）。
        # 无模型配置时 `get_llm()` 就在这一层抛出，正是这条路径。
        if not already_reported:
            job_bus.publish(
                runtime.job_id,
                _failure_event(runtime.error_code, runtime.error_message),
            )
        # 兜底异常单独留一条 ERROR（带 traceback）：`_finalize` 的终态日志是「收尾了」，
        # 这一条才是「为什么收尾」——它俩缺一，排障时都得猜另一半。
        logger.error(
            "生成任务异常终止",
            exc_info=True,
            extra={
                "event": "job.crashed",
                "job_id": runtime.job_id,
                "user_id": runtime.owner_id,
                "kind": runtime.kind,
                "error_code": runtime.error_code,
                "request_id": current_request_id(),
            },
        )
        _finalize(runtime, "failed")
    finally:
        # 先关总线（给订阅者放哨兵）再摘运行时：已订阅者仍能读到 run_summary 与结束标记；
        # 之后的迟到订阅者改走 DB 快照分支。
        job_bus.close(runtime.job_id)
        _RUNTIMES.pop(runtime.job_id, None)
        current = asyncio.current_task()
        if current is not None:
            _TASKS.discard(current)


def _failure_event(code: str, message: str) -> StreamEvent:
    """由终态的错误码 / 文案补一条 `error` 事件。

    为什么需要它：任务可能在**订阅者挂上之前**就已经失败。收尾那一刻广播的 `error`
    没有听众，而前端 live 路径（`useJob.start`）只看有没有收到 `error` 事件——
    于是「流正常结束 + 正文为空」会被当成生成成功。补这一条，迟到的订阅者也能拿到
    真实原因（例如「还没有任何模型配置」），而不是对着空白稿发呆。
    """
    return StreamEvent(
        type="error",
        content=message or "生成失败",
        data={"error_type": "JobFailed"},
        code=code or ErrorCode.INTERNAL_ERROR.value,
    )


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
        with session_scope() as session:
            job = GenerationJobRepository(session).get(owner_id, job_id)
            row = job.model_dump() if job else None
        if row is None:
            return
        yield _db_snapshot_event(row)
        status = str(row.get("status") or "")
        code = str(row.get("error_code") or "")
        # 已经失败且不是「取消 / 被顶替」：补一条 error，迟到的订阅者才知道为什么空。
        if status == "failed" and code not in _ABORT_CODES:
            yield _failure_event(code, str(row.get("error_message") or ""))
        usage = parse_usage(row.get("usage"))
        if usage is not None and status != STATUS_RUNNING:
            yield _run_summary_event(usage)
        return

    # —— 无 await 临界区 ——
    snapshot = _snapshot_event(runtime)
    queue = job_bus.subscribe(job_id)
    # ————————————————————————
    try:
        yield snapshot
        if runtime.status != "running":
            # 同上：快照里已是 failed 时补一条 error，让「失败发生在订阅之前」也能被看见。
            if runtime.status == "failed" and runtime.error_code not in _ABORT_CODES:
                yield _failure_event(runtime.error_code, runtime.error_message)
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
    with session_scope() as session:
        job = GenerationJobRepository(session).get(owner_id, job_id)
        return job.model_dump() if job else None


def list_jobs(
    owner_id: int,
    *,
    solution_id: int | None = None,
    kind: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """列出该 owner 的任务，可选按 solution_id / kind / status 过滤。"""
    with session_scope() as session:
        jobs = GenerationJobRepository(session).list_by_owner(
            owner_id,
            status=status,
            solution_id=solution_id,
            kind=kind,
        )
        return [job.model_dump() for job in jobs]


def reap_orphans() -> None:
    """启动时回收上次残留的 running 任务（进程重启后运行时已丢失）。"""
    with session_scope() as session:
        GenerationJobRepository(session).reap_running()


# ----------------------------------------------------------------------------
# 事件处理（内存运行时更新 / 合并写 / 收尾 / 快照）已按职责拆到
# `job_runtime_events` / `job_finalize` / `job_snapshot`——只搬家，函数体未改。
# ----------------------------------------------------------------------------


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
            # 快捷流程显式传 False：整篇一遍过，不审也不改
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
            """把开场白落成 `system` 轮次（不展示，只作为后续轮次的上下文）。

            本函数是**同步**的，`with` 块里没有 `await`——它是流里的一个回调，
            数据库那几行写完就归还连接，不会握着池连接去等模型输出。
            """
            if solution_id and text.strip():
                with session_scope() as session:
                    ChatTurnRepository(session).create(
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
    # 历史、会话 id 各开一次短会话：两次读之间没有一致性要求（会话 id 变了，
    # 下一轮本来就会因为 `SESSION_NOT_FOUND` 降级重放历史），而把两次读塞进
    # 一个事务会让这段代码在流开始之前多持有一条连接。
    if input_turn_id:
        with session_scope() as session:
            rows = ChatTurnRepository(session).list_history(
                owner_id, solution_id, stage, before_id=input_turn_id
            )
            history = [to_history_item(row) for row in rows]
    else:
        history = []
    content = str(request.get("content") or "")
    tool_result = request.get("tool_result")
    tool_result = tool_result if isinstance(tool_result, dict) else None
    with session_scope() as session:
        conversation_id = ChatSessionRepository(session).get(owner_id, solution_id, stage)

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
        # 清掉失效的会话 id：短事务，块内没有 `await`。
        with session_scope() as session:
            ChatSessionRepository(session).set(owner_id, solution_id, stage, "")
