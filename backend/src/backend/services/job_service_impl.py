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
from backend.repositories.generation_job_repo import (
    ACTIVE_STATUSES,
    STATUS_CANCELLED,
    STATUS_CANCELLING,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    GenerationJobRepository,
)
from backend.repositories.job_event_repo import JobEventRepository
from backend.repositories.solution_repo import SolutionRepository
from backend.services import (
    conversation_service,
    document_service,
    job_claimer,
    job_events,
    job_recovery,
    job_state,
)
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
    _flush_now,
    _maybe_flush,
    _run_summary_event,
    _summary_data,
)
from backend.services.job_snapshot import _db_snapshot_event, _snapshot_event, parse_usage

if TYPE_CHECKING:
    from backend.services.job_runtime import JobRuntime

logger = logging.getLogger(__name__)

# 终态里「订阅侧该不该补一条 `error` 事件」的唯一判据是**状态**（票 #84 AC4）：




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

    **两阶段取消（#84）**：排队中的任务没有执行者，`REQUEST_CANCEL` 一步把它落成
    「已取消」；执行中的任务先进入「取消中」——执行者还要用它的租约把半截正文与
    已经烧掉的用量写下来，真正的终态由它在下一个动作点确认（`CONFIRM_CANCEL`）。
    改到 0 行（任务已经自然结束）时它如实回答 False——「你点的时候它刚好说完了」不是失败。
    """
    runtime = _RUNTIMES.get(job_id)
    if runtime is not None and runtime.owner_id != owner_id:
        # 归属不符的运行时不能结算（否则等于替别人的任务置取消意图）。
        runtime = None
    result = job_state.transition(
        job_state.JobEvent.REQUEST_CANCEL,
        job_state.job_scope(owner_id, job_id),
        runtime=runtime,
        error_message=message,
    )
    return result.applied


def _persist_in_flight_progress(owner_id: int, *, solution_id: int | None = None) -> None:
    """单步收口取消**之前**，把在途运行时已经产出的正文落一次盘。

    删方案 / 删账号是**一步落终态 + 清租约**（P0-4）：不会有「执行者确认退出」这一步，
    执行者醒来时的补写会因为凭证已清而改到 0 行。它内存里的那半截正文若不先落盘，
    这条任务记录就会写着「已取消」而正文是空的——而 #85 的口径是「已生成的内容不丢」
    （它不是一份文档版本，但它是这次执行在任务行上留下的唯一痕迹）。

    只写**进度列**（`draft_content` / `phase` / `pending_items`）与过程日志，不改状态：
    状态由紧随其后的那一次转换负责。失败只记日志——落盘半截正文失败不该挡住取消
    （取消的本意是「别再跑了」，不是「等写完」）。
    """
    for runtime in list(_RUNTIMES.values()):  # noqa: SLF001 - 包内互引
        if runtime.owner_id != owner_id:
            continue
        if solution_id is not None and runtime.solution_id != solution_id:
            continue
        if runtime.status not in (STATUS_RUNNING, STATUS_CANCELLING):
            continue
        try:
            _flush_now(runtime)
        except Exception:  # noqa: BLE001 - 落盘失败不该挡住取消
            logger.warning(
                "取消前落盘半截正文失败，取消照常进行",
                exc_info=True,
                extra={
                    "event": "job.cancel_flush_failed",
                    "job_id": runtime.job_id,
                    "user_id": runtime.owner_id,
                    "kind": runtime.kind,
                },
            )


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

    **单步收口**（P0-4）：方案已经不存在了，不会再有「执行者来确认退出」这件事。
    因此这里不用两阶段的「执行中 → 取消中 → 等确认」，而是一步把任意非终态改成
    `cancelled` 并清空租约（转换表里 `REQUEST_CANCEL` 的批量作用域那一行）。留着
    `cancelling` 只是把一行永远不会被确认的任务交给恢复器兜底，而恢复器的收口发生在
    租约到期之后——这段时间里用户看到的是一条「正在取消」却没人管的行。

    为什么放在 `job_service` 而不是 `solution_service`
    -------------------------------------------------
    在途任务的协作式取消标记（`_RUNTIMES`）住在同目录的 `job_runtime.py`：#5 拆模块时
    迁出，#40 删空再导出块后由包内互引直接使用。而本模块已经依赖 `solution_service`
    （收尾前要确认方案还在），让 `solution_service` 反向 import 就会成环。这里直接用
    仓储——`repositories` 是更下一层，不构成环。

    Returns:
        方案是否存在并被删除。`False` 表示不存在或不属于该用户（路由翻 404）。
    """
    # 先落一次半截正文（在删方案的事务**之外**）：单步收口之后执行者再也写不进去。
    _persist_in_flight_progress(owner_id, solution_id=solution_id)
    with session_scope() as session:
        # 同一条事务里**先删再取消**：事务内的顺序不影响原子性，但先判「方案在不在」
        # 可以避免「方案不存在却把它的任务改成 failed」这种越界写。
        deleted = SolutionRepository(session).delete(owner_id, solution_id)
        if not deleted:
            return False
        # 改道经状态机（#84）：取消的规则（排队中一步落已取消、执行中转取消中、
        # 只动在途行）只在 `job_state` 写一份；这里传 session，是为了让取消与删方案同生共死。
        cancelled = job_state.transition(
            job_state.JobEvent.REQUEST_CANCEL,
            job_state.solution_scope(owner_id, solution_id),
            error_message="方案已删除，任务已取消",
            session=session,
        )

    # 落库之后才置内存标记：事务失败时不会留下「协程停了、库里还在跑」的错位。
    # 这一刻到协程下一个事件点之间的窗口由 `_finalize` 的「方案不存在就不写文档」兜住。
    job_state.settle_after_commit(
        cancelled, job_state.solution_scope(owner_id, solution_id)
    )
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
    过滤维度是 `owner_id`。与删方案一样是**单步收口**（P0-4）：账号都要没了，不会有人
    来确认退出，一步把任意非终态改成 `cancelled` 并清租约。
    """
    _persist_in_flight_progress(owner_id)
    with session_scope() as session:
        cancelled = job_state.transition(
            job_state.JobEvent.REQUEST_CANCEL,
            job_state.owner_scope(owner_id),
            error_message=message,
            session=session,
        )
    job_state.settle_after_commit(cancelled, job_state.owner_scope(owner_id))
    return cancelled.rows


def running_runtimes() -> list[JobRuntime]:
    """当前在途（`status` 是执行中 / 取消中）的运行时的快照。

    调用方是同一生成任务包内的 `job_wait`：它把这份快照折算成只带 `job_id` 的抽象句柄
    交给包外排空，再由自己按句柄回答「等在途任务跑完、超时取消哪几支」。快照之后自然
    结束的任务会在 `cancel_in_flight` 里按当时的 `status` 被排除。

    「取消中」也算在途：那一支的执行者还活着（租约在它手里），排空同样要等它/收它；
    把它漏掉，重启时就会留下一条名义上「已请求取消」、实际还在烧 token 的协程。

    为什么判据用运行时上的 `status` 而不是 `_TASKS`：任务集合只是保活用的强引用，
    它不知道「这个任务是不是已经收尾了」；运行时上的 `status` 才是权威。
    """
    return [
        runtime
        for runtime in list(_RUNTIMES.values())
        if runtime.status in (STATUS_RUNNING, STATUS_CANCELLING)
    ]


def cancel_in_flight(reason: str, *, runtimes: list[JobRuntime] | None = None) -> int:
    """把在途任务收成「已取消」终态，返回被取消的条数。

    调用方是同一生成任务包内的 `job_wait`：等满窗口之后，由它把仍卡在模型调用里、
    自己走不出来的任务收掉，再把汇总数字交回包外排空。本函数是 `job_service` 公开面
    上的正常动作；`_failure_event` / `_finalize` / `_RUNTIMES` 都是本包私有名，
    排空只问三个问题，不直接调这里。

    Args:
        reason: 落进 `error_message` 的终止原因（**不是错误码**）。文案要说清**是谁**动的手
            （例如「服务正在重启」），否则运维会把排空取消当成生成失败。
        runtimes: 要取消的运行时。`None` 表示「当前全部在途任务」。排空传的是它等待
            之前的那份快照，这样「取消数 ≤ 开始时在途数」由构造保证。

    Returns:
        真正收成取消的条数；快照里已经自然收尾的任务会被跳过、不计数。

    实现上顺序不能换：**先记失败意图、再收尾**。`_finalize` 结尾会广播 `run_summary`，
    订阅者读到的是一条完整的「已取消」结局。末端那条 `job_bus.close()` 是给订阅者的
    哨兵：超时者可能正卡在模型调用里，等它自己的 `finally` 放哨兵就是「再等一个 30 秒」。

    **七态之后的取消结局（#84）**：落 `cancelled`，**不发 `error` 事件**——用户/运维
    主动取消不是一次生成失败，前端按回执里的 `status` 展示「已取消」即可。这里只置
    「取消意图」（`job_state.request_cancel`），落库与内存终态都交给 `_finalize` 那一次
    转换——于是「排空收一次、协程醒来又收一次」的第二次会被 `_finalize` 的内存闸挡掉。
    """
    targets = running_runtimes() if runtimes is None else list(runtimes)
    cancelled = 0
    for runtime in targets:
        if runtime.status not in (STATUS_RUNNING, STATUS_CANCELLING):
            # 快照之后已经自然收尾：它有自己更准确的结论，别再覆盖成「被取消」。
            continue
        cancelled += 1
        job_state.request_cancel(runtime)

        logger.warning(
            "取消在途任务",
            extra={
                "event": "job.cancel_in_flight",
                "job_id": runtime.job_id,
                "user_id": runtime.owner_id,
                "kind": runtime.kind,
                "reason": reason,
            },
        )
        _finalize(
            runtime,
            STATUS_CANCELLED,
            error_message=reason,
        )
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
    `use_lease` 同理（#82）：把这一轮的执行权挂到上下文，每次模型调用前由
    `job_claimer.renew_before_action` 续租。

    `event_stream_source` 是单②在造单时定格的事件流来源（生产真值 / 测试假值），
    派发时随单递入；本模块只有这一条事件流循环，包裹顺序、广播与收尾都只写一遍。

    ## 失去执行权 / 被请求取消（#82 + #84）

    续租改到 0 行有两种原因：

    - **这一轮已经被请求取消**（行在 `cancelling`）：执行者是唯一还有资格写这一行的人，
      必须由它**确认取消**（落 `cancelled`，并把半截正文与已经烧掉的用量补一次）；
    - **执行权已被接手**（凭证对不上 / 行已回排队）：此时**什么都不写**——不判失败、
      不写终态、不广播回执、不记账本。那一行可能正由新的执行者跑着，「我失去资格」
      不是「任务失败」。
    """
    guard = job_claimer.guard_for(runtime)
    try:
        # `use_config` 把派发那一刻的配置冻进上下文：整条生成链路（含并行子任务）
        # 读到的是同一份。任务跑到一半有人改配置，也不会让同一次生成前后参数不一致。
        with use_meter(runtime.meter), use_config(runtime.config), job_claimer.use_lease(guard):
            # 上游正文归服务端取（spec #51 / 票 #53 / #54）：解析发生在**建任务那一刻**
            # （路由层），结果随任务单定格、派发时已合进 `runtime.request`。
            async for event in event_stream_source(runtime.kind, runtime.request):
                # 方案在生成途中被删除 / 用户按了停止：下一个事件不再消费。
                # 判断放在 `_apply_event` **之前**——取消之后不再往运行时里写任何东西，
                # 收尾时 `_finalize` 也就拿不到新的 `done`，不会把半截过程当成「已产出」。
                #
                # `superseded` 同理：同一份文档已经有更新的任务在写，这一支的结果反正不会
                # 落库，继续烧 token 只是白花钱。
                #
                # `guard.lost` / `guard.cancel_requested`（#82 / #84）：续租时已经看见了
                # 「执行权丢了」或「被请求取消」。异常可能被流水线内部的 `except` 吞掉，
                # 所以这里再挡一道——下一个事件到达就停。
                if (
                    runtime.cancelled
                    or runtime.superseded
                    or (guard is not None and (guard.lost or guard.cancel_requested))
                ):
                    break
                _apply_event(runtime, event)
                # 过程日志：先记进待写缓冲，随后与进度列同一笔落盘（见 `job_events`）。
                job_events.record(runtime, event)
                _maybe_flush(runtime)
                job_bus.publish(runtime.job_id, event)
        if guard is not None and guard.cancel_requested and not guard.lost:
            # 看到取消标记：执行者退出并确认取消（AC2）。结局由运行时上的取消意图结算。
            _finalize(runtime, STATUS_CANCELLED, error_message="任务已取消")
            return
        if guard is not None and guard.lost:
            _log_lease_lost(runtime, "续租失败之后的下一个事件点")
            return
        _finalize(runtime, STATUS_FAILED if runtime.failed else STATUS_SUCCEEDED)
    except job_claimer.CancelRequestedError:
        # 取消信号（#84）：执行者在动作点看到 cancelling，确认退出。
        _finalize(runtime, STATUS_CANCELLED, error_message="任务已取消")
    except job_claimer.LeaseLostError as lost:
        # **不再写任何结果**，也不走下面的失败兜底：失去执行权 ≠ 这次生成失败。
        _log_lease_lost(runtime, lost.reason)
    except Exception as exc:  # noqa: BLE001 - 后台任务必须自兜异常，不能静默消失
        # 管线可能已把错误转成 `error` 事件（运行时因此带着错误码），别重复发第二条。
        already_reported = bool(runtime.error_code)
        if isinstance(exc, AppError):
            # 异常自带的码更准：预算超顶是 INVALID_REQUEST（重试也没用），
            # 不该被笼统记成「上游故障」，否则前端会去做无意义的重试。
            code = exc.code.value
            message = exc.message
        else:
            code = ErrorCode.LLM_UPSTREAM_ERROR.value
            message = f"生成失败：{exc}"
        # 只记「失败意图」，终态由下面的收尾统一结算（#79：内存与库不各改一份）。
        job_state.note_failure(runtime, error_code=code, error_message=message)
        # ⚠️ 兜底异常**必须广播**：前端只有收到 `error` 事件才判失败。只改运行时
        # 而不发事件，界面上就是「流结束、正文为空」——被当成生成成功（空内容）。
        # 无模型配置时 `get_llm()` 就在这一层抛出，正是这条路径。
        if not already_reported:
            failure = _failure_event(runtime.error_code, runtime.error_message)
            # 兜底异常的这一条同样要进过程日志：它可能是这条订阅看到的最后一条事件，
            # 只广播不落盘的话，按游标续读的读者永远拿不到它。
            job_events.record(runtime, failure)
            job_bus.publish(runtime.job_id, failure)
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
        _finalize(runtime, STATUS_FAILED)
    finally:
        # 先关总线（给订阅者放哨兵）再摘运行时：已订阅者仍能读到 run_summary 与结束标记；
        # 之后的迟到订阅者改走 DB 快照分支。
        job_bus.close(runtime.job_id)
        _RUNTIMES.pop(runtime.job_id, None)
        current = asyncio.current_task()
        if current is not None:
            _TASKS.discard(current)


def _log_lease_lost(runtime: JobRuntime, reason: str) -> None:
    """失去执行权：留一条日志，**别的什么都不做**。

    为什么不发 `error` 事件、不写终态、也不判死：这一行可能正由新的执行者跑完
    （租约被接手），把「我失去资格」说成「任务失败」会让用户看到一次并不存在的失败，
    而且晚到的失败写也写不进去（凭证对不上，数据库会改到 0 行）。真正的结局由
    现在的持有者写。这里只回答「为什么这一支停了」——排障时最常问的那个问题。
    """
    logger.warning(
        "本轮执行权已失效，任务就地停止（不写任何结果）",
        extra={
            "event": "job.execution_stopped",
            "job_id": runtime.job_id,
            "user_id": runtime.owner_id,
            "solution_id": runtime.solution_id,
            "kind": runtime.kind,
            "reason": reason,
            "request_id": current_request_id(),
        },
    )


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


def _terminal_tail(
    runtime: JobRuntime,
    *,
    snapshot_delivered: bool,
    saw_summary: bool,
    saw_error: bool,
) -> list[StreamEvent]:
    """订阅收尾时给「带快照的读者」补发结束事件（错误原因 + 回执）。

    为什么需要它：**订阅时任务已经是终态**的那条路上，结束事件早已落盘、且游标已经
    覆盖它（快照内容就包含它），所以续读不会再收到——于是「失败原因」与「用量回执」
    要单独补一份，否则前端只能看到一份没有结论的空白稿。

    **已经通过续读收到的就不要补**：续读路径上 `run_summary` 是真实那一笔（与落库的
    `usage` 同源）；再补一份「现算」的回执，`duration_ms` 会与落库值差几毫秒，前端
    就会看到「流里说的耗时」与「刷新后读到的耗时」不一致。`saw_summary` / `saw_error`
    就是为这个判据。

    带游标的读者**不补**：它读的是回放，游标之后有什么就给什么，不在这条链上加料。
    """
    if not snapshot_delivered or runtime.status in (STATUS_RUNNING, STATUS_CANCELLING):
        return []
    events: list[StreamEvent] = []
    if runtime.status == STATUS_FAILED and not saw_error:
        # 只有 `failed` 补 error（AC4）：已取消 / 被取代是正常结局，不是红色失败。
        events.append(_failure_event(runtime.error_code, runtime.error_message))
    if not saw_summary:
        events.append(_run_summary_event(_summary_data(runtime, runtime.status)))
    return events


async def _wait_for_wake(wake: asyncio.Queue) -> bool:
    """等一次唤醒（或轮询超时），返回「是否收到结束哨兵」。

    唤醒队列只是**叫醒通道**：里面的内容（事件）在这里被丢弃，真正的数据永远从
    `job_events` 按游标读。没有唤醒就靠 `POLL_SECONDS` 超时轮询兜底——通知是加速器，
    不是数据通道（Postgres 的 NOTIFY 同样有上限、无监听者即丢、不保证送达）。
    """
    try:
        item = await asyncio.wait_for(wake.get(), timeout=job_events.POLL_SECONDS)
    except asyncio.TimeoutError:
        return False
    closed = item is None
    # 队列里可能已经堆了一串唤醒：一次排空，哨兵也可能排在事件后面。
    while not wake.empty():
        queued = wake.get_nowait()
        if queued is None:
            closed = True
    return closed


def _flush_before_snapshot(runtime: JobRuntime) -> int:
    """取快照之前把在途事件补写进表，返回快照游标。

    **为什么必须先补写**：内存快照是「全部已应用事件」的视图，而游标是「最后一条已落盘
    事件」的 id。若还有事件在内存缓冲里没落盘，快照就领先游标——拿着这个游标续读的
    读者会把那一段再收一遍（重复正文）。补写与快照在同一个无 await 临界区里完成，
    所以补写之后「快照内容 == 游标之前的事件之和」严格成立。

    没有在途事件时不做任何写：游标就是已有的 `last_event_id`，快照与它天然一致。
    """
    if not runtime.pending_events:
        return runtime.last_event_id
    return _flush_now(runtime)


async def _db_stream(
    owner_id: int, job_id: int, cursor: int | None
) -> AsyncIterator[StreamEvent]:
    """运行时未命中时的订阅：DB 快照兜底（或按游标回放已落盘事件）。

    - 不带游标（刷新 / 新读者）：先 `snapshot`（正文取 `draft_content`，游标取
      `MAX(job_events.id)`——两者在同一笔事务里写过，所以快照内容覆盖到该游标为止），
      再补发失败原因与回执，然后结束。**不回放过程日志**（否则每次刷新都重演一遍）。
    - 带游标：不重发快照，只把游标之后的已落盘事件发完（一个短循环，读空即止）。
      进程重启后运行时已不在，这里就是「把断线期间落盘的那一段补给你」的唯一通道。
    """
    with session_scope() as session:
        job = GenerationJobRepository(session).get(owner_id, job_id)
        row = job.model_dump() if job else None
        snapshot_cursor = JobEventRepository(session).max_id(job_id) if job else 0
    if row is None:
        return
    if cursor is None:
        snapshot = _db_snapshot_event(row)
        snapshot.event_id = snapshot_cursor
        yield snapshot
        status = str(row.get("status") or "")
        code = str(row.get("error_code") or "")
        # 只有真失败补一条 error：已取消 / 被取代按状态展示即可（AC4 / AC5）。
        if status == STATUS_FAILED:
            yield _failure_event(code, str(row.get("error_message") or ""))
        usage = parse_usage(row.get("usage"))
        if usage is not None and status not in ACTIVE_STATUSES:
            yield _run_summary_event(usage)
        return
    current = cursor
    while True:
        events, current = job_events.read_after(job_id, current)
        if not events:
            return
        for event in events:
            yield event


async def open_stream(
    owner_id: int, job_id: int, cursor: int | None = None
) -> AsyncIterator[StreamEvent]:
    """订阅一个任务的事件流：**按游标读表**，先快照（不带游标时）再追新事件。

    ## 游标

    `cursor` 是客户端手里**已经收到的最后一条事件**的 id（事件 `data.cursor` 给出）。
    带上它就只读 `id > cursor` 的那些行——断线重连因此不重不漏；多个读者各持各的游标，
    互不影响。不带游标（刷新 / 新读者）则先发一份快照 + 快照游标，**不回放历史**。

    ## 数据通道与唤醒

    数据只从 `job_events` 读；进程内的 `job_bus` 只负责**叫醒**（与 Postgres NOTIFY
    同角色：有上限、无监听者即丢、不保证送达）。没有唤醒时靠 `POLL_SECONDS` 轮询兜底，
    所以「通知丢了」只影响延迟，不影响正确性。

    ## 无 await 临界区

    带快照的那条路上，「补写在途事件 → 取快照 → 订阅唤醒队列」之间**一次都不让出
    事件循环**：一旦让出，任务就可能在这段窗口里产事件（既没进快照、也没被唤醒队列
    记下），表现为「快照与续流之间缺一段」。
    """
    runtime = _RUNTIMES.get(job_id)
    if runtime is not None and runtime.owner_id != owner_id:
        runtime = None
    if runtime is None:
        async for event in _db_stream(owner_id, job_id, cursor):
            yield event
        return

    # —— 无 await 临界区 ——
    if cursor is None:
        snapshot_cursor = _flush_before_snapshot(runtime)
        snapshot: StreamEvent | None = _snapshot_event(runtime)
        snapshot.event_id = snapshot_cursor
        start = snapshot_cursor
    else:
        snapshot = None
        start = cursor
    wake = job_bus.subscribe(job_id)
    # ————————————————————————
    try:
        if snapshot is not None:
            yield snapshot
        current = start
        closed = False
        saw_summary = False
        saw_error = False

        def _observe(event: StreamEvent) -> None:
            """记住「这两类结束事件是否已经从续读里收到」（用于避免重复补发）。"""
            nonlocal saw_summary, saw_error
            if event.type == "run_summary":
                saw_summary = True
            elif event.type == "error":
                saw_error = True

        while True:
            events, current = job_events.read_after(job_id, current)
            if not events and not closed and not _is_terminal(runtime):
                # 表里还没有新行，但运行时里躺着还没落盘的增量：替写者把它们合并写下去。
                #
                # 为什么是**读者**来写：写者只在「来下一个事件时」检查合并写窗口，
                # 一旦它挂在模型调用上（一次调用可能几十秒），最后一段增量就会一直
                # 留在内存里——表现为进度条卡住、而任务其实还在跑。轮询兜底必须能
                # 推进**数据**，而不只是「醒过来再读一次空表」。
                if runtime.pending_events:
                    try:
                        _flush_now(runtime, progress=False)
                    except Exception:  # noqa: BLE001 - 写不进去不该打断订阅，下一轮再试
                        logger.warning(
                            "订阅侧合并写失败，下一轮重试",
                            extra={
                                "event": "job.flush_on_poll_failed",
                                "job_id": job_id,
                                "request_id": current_request_id(),
                            },
                        )
                    continue  # 刚落盘的事件可能就有人要读，立刻再读一遍
                closed = await _wait_for_wake(wake)
                continue
            for event in events:
                _observe(event)
                yield event
            if _is_terminal(runtime):
                # 终态与收尾事件同一笔提交：醒来先读空剩余行（含 run_summary），再收尾。
                while True:
                    more, current = job_events.read_after(job_id, current)
                    if not more:
                        break
                    for event in more:
                        _observe(event)
                        yield event
                for event in _terminal_tail(
                    runtime,
                    snapshot_delivered=snapshot is not None,
                    saw_summary=saw_summary,
                    saw_error=saw_error,
                ):
                    yield event
                return
            if closed:
                return
    finally:
        job_bus.unsubscribe(job_id, wake)


def _is_terminal(runtime: JobRuntime) -> bool:
    """运行时是否已到终态（内存状态由状态机在**提交之后**结算，见 #79）。

    「取消中」不是终态：执行者还活着、还在收口，订阅要继续读（AC2）。
    """
    return runtime.status not in (STATUS_RUNNING, STATUS_CANCELLING)


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
    """列出该 owner 的任务，可选按 solution_id / kind / **状态集合** 过滤。

    `status` 接受逗号分隔的多个取值（例如 `pending,running,cancelling` = 在途）：
    七态之后「在途」不再是一个值，对外也就不能只按单个 `running` 过滤。
    """
    statuses = tuple(
        value.strip() for value in (status or "").split(",") if value.strip()
    )
    with session_scope() as session:
        jobs = GenerationJobRepository(session).list_by_owner(
            owner_id,
            statuses=statuses or None,
            solution_id=solution_id,
            kind=kind,
        )
        return [job.model_dump() for job in jobs]


def reap_orphans() -> None:
    """启动回收：把上次进程留下的非终态行收成确定终态（票 #83，七态版 #84）。

    收编进恢复器（#83）：这里不再是一次「维护面」的批量判死，而是
    `job_recovery.recover_once(..., startup=True)` 的启动档——同一套判权（扫描时
    读到的旧租约凭证 fencing）、同一套账本（每行一条，原因写明）、同一个状态机入口。
    区别只在候选集：启动档不看到期时间、也不区分「有没有租约」，因为进程刚起来时
    库里不可能有真正的执行者（单 worker 硬约束，`_RUNTIMES` 在进程内）。

    启动档去向见 `job_recovery` 模块 docstring：取消中的行按**已取消**收口（用户的
    意图优先于「超时」），有租约的执行中行按**租约超时**判失败（`LEASE_EXPIRED`），
    从没被领过的残留行（含排队中）按**服务重启中断**判失败（`INTERNAL_ERROR`）。
    不回排队：这些行是上一次进程留下的残骸，如实报一次「中断」比静默重跑诚实——
    重跑是用户的决定（再点一次生成），不该由服务替他把上一次的失败抹掉。
    （调度循环已在 `services/job_scheduler.py` 接上，但它只领**本轮进程**登记 /
    回排队的行；启动档的判据与它不重叠，两者不冲突。）
    """
    job_recovery.recover_once(startup=True)


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
    截断检测）全部在 `document_service` / `stream_reply` 里，
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
            # 当前版本的待确认清单：重审六类全开，清单整份注入（ADR 0003）
            pending_items=request.get("pending_items") or [],
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
            # 当前版本的待确认清单：修复提示词里摆出「已经登记过哪些」（ADR 0003）
            pending_items=request.get("pending_items") or [],
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
