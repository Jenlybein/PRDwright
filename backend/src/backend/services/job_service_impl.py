"""生成任务服务的内部实现：一次生成的持久化、重连与后台过程（票 #40）。

本模块是 `job_service` 公开面背后的**内部实现**，与同目录的 `job_*` 模块同属一个
内部包：包内互引私有名合法，包外只能经 `job_service.__all__` 的动作进入。
新三步「造单 → 登记 → 派发」住在 `job_ticket` / `job_registration`，派发只认单②；
排空留在包外（`job_drain`），只经 `job_wait` 问「还有哪些在跑 / 要不要等 /
超时取消哪几支」三个问题，运行时字段不越过包边界。

核心思想是**生成与浏览器连接解耦**：

- 派发在服务进程内起一个 `asyncio` 后台任务（`run_job`），任务的推进完全
  不依赖任何 HTTP 连接；浏览器断开只会让某个订阅者退出，任务照跑。
- 任务的每个事件既更新内存运行时（`JobRuntime`），又 `publish` 到 `job_bus`。
- 订阅（SSE）通过 `open_stream`：命中内存运行时则**先发一份 `snapshot`（补全已产出
  正文）再挂上 live 流**；未命中（进程重启 / 已完成过久）则读 DB 发一份快照即止。

持久化策略（合并写）：请求参数、配置/供应商快照与技能包身份在建任务那一刻
一次写全；内存运行时是权威，DB 每 ≤300ms 或 `live_buffer` 增量 >4KB
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
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable

from backend.core.contracts import StreamEvent
from backend.core.engine import session_scope
from backend.core.errors import AppError, ErrorCode
from backend.core.runtime_config import use_config
from backend.core.request_context import current_request_id
from backend.repositories.chat_session_repo import ChatSessionRepository
from backend.repositories.chat_turn_repo import ChatTurnRepository, to_history_item
from backend.repositories.generation_job_repo import STATUS_RUNNING, GenerationJobRepository
from backend.repositories.solution_repo import SolutionRepository
from backend.services import conversation_service, document_service
from backend.services.job_bus import job_bus
from backend.services.llm_meter import use_meter
from backend.services.skill_registry import describe_kind

# ----------------------------------------------------------------------------
# 内部实现按职责拆到同目录的模块；下面是本模块正文以裸名使用的本地 import，
# 不再对外再导出。包内互引私有名合法，包外只能经 `job_service` 的公开动作进入。
# ----------------------------------------------------------------------------
from backend.services.job_finalize import _finalize
from backend.services.job_product_kind import _target_product_kind
from backend.services.job_runtime import _RUNTIMES, _TASKS
from backend.services.job_runtime_events import (
    _apply_event,
    _maybe_flush,
    _run_summary_event,
    _summary_data,
)
from backend.services.job_snapshot import _db_snapshot_event, _snapshot_event, parse_usage

if TYPE_CHECKING:
    from backend.services.job_runtime import JobRuntime

logger = logging.getLogger(__name__)

# 失败收尾里「不是错误」的两个终态判定：已取消（用户走开 / 删方案）与被取代（同一份
# 文档已有更新的任务接手）。它们同样落 failed 状态，但不该作为红色错误弹给用户，
# 因此补发 `error` 事件时要排除——否则「用户自己按了停止」会被渲染成「生成失败」。
_ABORT_CODES = frozenset({"CANCELLED", "SUPERSEDED"})


# ----------------------------------------------------------------------------
# 对外动作
# ----------------------------------------------------------------------------


def _immutable_projection(
    kind: str, request: dict[str, Any], config: Any | None = None
) -> dict[str, Any]:
    """算出任务单里三块不可变数据（建任务那一刻一次定格）。

    - 原始请求参数：原样存调用方摊平后的 dict，不做二次加工；
    - 配置/供应商快照：只取热配置白名单里的生效值 + 激活供应商四格，
      **刻意不存整份 `Settings`**——里面有部署地址、初始管理员口令等不该进任务行的值；
    - 技能包身份：`_resolve_skill_refs` 与派发时用的是同一个判断（按目标产物查注册表）。

    这三块只服务于「DB 行是不可变数据的完整投影」。运行态仍以内存运行时为权威，
    按既有节奏落进度；生成中途改后台配置/模板，也不会让这一行跟着变。

    `config` 是「造单」传入的定格快照：不给时在这里取一次生产真值，
    造单与登记共用同一份值，生成中途改后台配置不会让这一单漂移。
    """
    from backend.core import runtime_config as rc

    config = config if config is not None else rc.get_runtime_config()
    spec = config.active_provider
    active_provider = {
        "provider_protocol": spec.protocol if spec is not None else "",
        "provider_config_id": spec.id if spec is not None else "",
        "provider_name": spec.name if spec is not None else "",
        "provider_model": spec.model if spec is not None else "",
    }
    settings = {
        key: getattr(config.settings, key)
        for key in rc.WHITELIST
        if hasattr(config.settings, key)
    }
    return {
        "request_params": request,
        "config_snapshot": {
            "active_provider": active_provider,
            "settings": settings,
            "revision": config.revision,
            "source": config.source,
        },
        "skill_refs": _resolve_skill_refs(kind, request),
    }


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
    在途任务的协作式取消标记（`_RUNTIMES`）住在同目录的 `job_runtime.py`：#5 拆模块时
    迁出，#40 删空再导出块后由包内互引直接使用。而本模块已经依赖 `solution_service`
    （收尾前要确认方案还在），让 `solution_service` 反向 import 就会成环。这里直接用
    仓储——`repositories` 是更下一层，不构成环。

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

    调用方是同一生成任务包内的 `job_wait`：它把这份快照折算成只带 `job_id` 的抽象句柄
    交给包外排空，再由自己按句柄回答「等在途任务跑完、超时取消哪几支」。快照之后自然
    结束的任务会在 `cancel_in_flight` 里按当时的 `status` 被排除。

    为什么判据用运行时上的 `status` 而不是 `_TASKS`：任务集合只是保活用的强引用，
    它不知道「这个任务是不是已经收尾了」；运行时上的 `status` 才是权威。

    本函数是 `job_service` 公开面上的正常动作：排空留在包外、只问三个问题，不伸手进
    `_RUNTIMES`；「谁可以直接读进程内登记表」在生成任务一侧收口，换存储结构时改动面
    只有本包。
    """
    return [
        runtime for runtime in list(_RUNTIMES.values()) if runtime.status == STATUS_RUNNING
    ]


def cancel_in_flight(reason: str, *, runtimes: list[JobRuntime] | None = None) -> int:
    """把在途任务收成「已取消」终态，返回被取消的条数。

    调用方是同一生成任务包内的 `job_wait`：等满窗口之后，由它把仍卡在模型调用里、
    自己走不出来的任务收掉，再把汇总数字交回包外排空。本函数是 `job_service` 公开面
    上的正常动作；`_failure_event` / `_finalize` / `_RUNTIMES` 都是本包私有名，
    排空只问三个问题，不直接调这里。

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
            # 对话的「已取消」要保留已经说出口的话（`_write_chat_turn` 按这一位处理）
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


async def run_job(
    runtime: JobRuntime, event_stream_source: Callable[..., AsyncIterator[StreamEvent]]
) -> None:
    """后台任务主体：驱动事件流、更新内存、广播、按节奏落库、收尾。

    `use_meter` 包住整条事件流的消费过程：计量器是环境值，流水线深处的
    `stream_reply.astream_text` 靠它把自己记到这次任务上（见 `llm_meter`）。

    `event_stream_source` 是单②在造单时定格的事件流来源（生产真值 / 测试假值），
    派发时随单递入；本模块只有这一条事件流循环，包裹顺序、广播与收尾都只写一遍。
    """
    try:
        # `use_config` 把派发那一刻的配置冻进上下文：整条生成链路（含并行子任务）
        # 读到的是同一份。任务跑到一半有人改配置，也不会让同一次生成前后参数不一致。
        with use_meter(runtime.meter), use_config(runtime.config):
            # 上游正文归服务端取（spec #51 / 票 #53 / #54）：解析发生在**建任务那一刻**
            # （路由层），结果随任务单定格、派发时已合进 `runtime.request`。
            # 这里不再解析：开流时才查库，会让「缺材料」变成一条流内错误而不是明确 400，
            # 而且与建任务时看到的库状态可能已经不是同一份。
            async for event in event_stream_source(runtime.kind, runtime.request):
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
    if kind == "review":
        # 只审不改：被审的正文与它比对的上游材料都在 `run_job` 开流前由服务端解析好
        # （`job_inputs.resolve_runtime_inputs`），这里只按目标产物分派。
        return document_service.review_document_stream(
            doc_kind=_target_product_kind(kind, request) or "",
            document=request.get("document", "") or "",
            prd_content=request.get("prd_content", "") or "",
            api_docs_content=request.get("api_docs_content", "") or "",
            summary=request.get("summary"),
            requirements=request.get("requirements", "") or "",
            product_name=request.get("product_name", "") or "",
            history=request.get("history") or None,
        )
    if kind == "review_fix":
        # 按结论修复：被修的正文、那条结论与上游材料同样由服务端解析（`_fix_inputs`）。
        # 上游对 PRD 是两条澄清线程、对其余两份是上游全文（ADR 0002 的上下文窗口）。
        return document_service.fix_document_stream(
            doc_kind=_target_product_kind(kind, request) or "",
            document=request.get("document", "") or "",
            conclusion=request.get("review_conclusion"),
            prd_content=request.get("prd_content", "") or "",
            api_docs_content=request.get("api_docs_content", "") or "",
            summary=request.get("summary"),
            requirements=request.get("requirements", "") or "",
            product_name=request.get("product_name", "") or "",
            history=request.get("history") or None,
            clarify_history=request.get("clarify_history") or None,
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
