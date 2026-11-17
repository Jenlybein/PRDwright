"""任务状态机：三态先行的**唯一转换入口**（票 #79）。

## 为什么要有这个模块

改状态原先散在八处：`finish` / `fail` / `cancel_one` / `cancel_running` /
`cancel_running_for_owner` / `supersede_active` / `reap_running`，外加内存里的
`runtime.status` 一处。每一处都各自实现一遍「从什么状态能到什么状态、失败带哪个
错误码、内存那一份要不要跟着改」——同一份规则写六遍，迟早有一份写反。

两个终态写点（`finish` / `fail`）连 `WHERE status='running'` 都没有、也不看影响行数：
排空取消替任务收了一次尾，卡在模型调用里的协程醒来又收一次，第二次照样写库、照样
广播。「同一个任务被收尾两次」就是这样漏出来的。

## 本票的口径（**只做三态**）

- 状态集合仍是 `running` / `completed` / `failed`；`CANCELLED` / `SUPERSEDED` 仍是
  `error_code`，不是状态值——前端契约一个字都不变（见 `cancel_running` 的注释）。
  七态与「取消中」是 #84 的事，本票不提前引入。
- **唯一的合法前态是 `running`**：`WHERE status='running'` 是数据库层的机械强制，
  **影响行数是唯一判据**——不是「语句没报错」。
- 改到 0 行 = 本次结果被丢弃：只留一条丢弃日志（`event="job.transition_discarded"`），
  不改内存、不重试、不重新排队（spec 六·问题三：失去资格的执行者不许凭空制造新尝试）。
- 内存里的 `status` / `error_code` / `error_message` **只在结算这一步写**（`_settle`），
  落库有结果（或确认别处已落库）之后才写；两处各改一份的局面从此不存在。

## 对外只有一个转换动作

`transition(事件, 期望条件) -> TransitionResult`。事件是四种意图，期望条件由
`JobScope` 给出（哪条任务 / 哪个方案 / 哪个 owner / 哪个产物 / 维护面）：

| 事件 | 期望条件 | 落库 | 内存 |
|---|---|---|---|
| `SUCCEED` | 单条任务 | `finish`（completed） | status=completed、清错误码 |
| `FAIL` | 单条任务 / 维护面 | `fail`（failed + 错误码） | status=failed + 错误码 |
| `CANCEL` | 单条 / 方案 / owner | `failed + CANCELLED` | 只置「取消意图」标志 |
| `SUPERSEDE` | 同 owner+方案+产物 | `failed + SUPERSEDED` | 只置「被顶替意图」标志 |

四条终止路径全部改道经它：成功收尾与失败收尾在 `job_finalize._finalize`，
取消在 `job_service_impl.cancel_job` / `delete_solution_and_cancel_jobs` /
`cancel_for_user`，顶替在 `job_registration.register_in_transaction`。

### 取消、顶替只置「意图」，不置终态

它们只是「请求停止」：数据库立刻落成 `failed + 对应错误码`（用户按下停止就该马上看见，
这是既有契约），但**内存里只置意图标志**——真正的终态由那条协程自己收尾时结算。
所以「取消之后收尾还能把半截正文与用量记下来」（`abort` 那一条期望条件见下）。

### 0 行之后仍可能有「一次补载荷」

取消路径先把行落成 `failed + CANCELLED`，那一刻还没用量；被取消的那条协程醒来收尾时，
它的 `fail` 期望条件是「仍在 running **或者** 已经被同一个 abort 码落库、且用量还是空」。
这条条件让「取消」与「收尾」衔接成一次完整的账：状态早已终态、载荷只补一次，
并且**第二次补载荷同样是 0 行**（用量已经不是空，条件不再成立）。
别用「状态等于 failed 就允许写」这种更宽的条件：那正是「晚到的失败把
`INTERNAL_ERROR` 覆写成别的东西」的入口（§1.5 的覆盖窗口）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlmodel import Session

from backend.core.engine import session_scope
from backend.repositories.generation_job_repo import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    GenerationJobRepository,
)
from backend.services.job_runtime import _RUNTIMES

if TYPE_CHECKING:
    from backend.services.job_runtime import JobRuntime

logger = logging.getLogger(__name__)

# 取消 / 被顶替的 `error_code` 取值：**不是** `ErrorCode` 成员（它们不是错误，
# 只是「没跑成」的两种正常原因），由本模块统一给出，仓储与收尾都不再各写一份字面量。
CODE_CANCELLED = "CANCELLED"
CODE_SUPERSEDED = "SUPERSEDED"
# 「不是红色错误」的两个中止码：订阅侧据此决定补不补 `error` 事件。
ABORT_CODES = frozenset({CODE_CANCELLED, CODE_SUPERSEDED})


class JobEvent(StrEnum):
    """四种转换意图（三态下只有这四个出口）。"""

    SUCCEED = "succeed"
    FAIL = "fail"
    CANCEL = "cancel"
    SUPERSEDE = "supersede"


@dataclass(frozen=True, slots=True)
class JobScope:
    """转换的**期望条件**：作用对象 + 前置状态（三态下前置恒为 `running`）。

    由下面的具名构造器生成，不直接拼字段：拼错一个字段就是「改到了别人的任务」。
    `solution_filter` 单独一位，因为「按方案过滤」与「方案为空」是两件事：
    `solution_id=None` 是**合法的过滤值**（快捷入口逃生口那些没有方案的任务），
    不能拿 `None` 当「不过滤」用。
    """

    owner_id: int
    job_id: int | None = None
    solution_id: int | None = None
    solution_filter: bool = False
    kind: str | None = None
    owner_wide: bool = False
    maintenance: bool = False


def job_scope(owner_id: int, job_id: int) -> JobScope:
    """单条任务（成功收尾 / 失败收尾 / 用户按停止）。"""
    return JobScope(owner_id=owner_id, job_id=job_id)


def solution_scope(owner_id: int, solution_id: int) -> JobScope:
    """某方案下所有在途任务（删方案）。"""
    return JobScope(owner_id=owner_id, solution_id=solution_id, solution_filter=True)


def owner_scope(owner_id: int) -> JobScope:
    """某用户名下所有在途任务（删账号）。"""
    return JobScope(owner_id=owner_id, owner_wide=True)


def supersede_scope(owner_id: int, solution_id: int | None, kind: str) -> JobScope:
    """同 owner + 方案 + 产物（登记顶替旧任务）。"""
    return JobScope(
        owner_id=owner_id, solution_id=solution_id, solution_filter=True, kind=kind
    )


def maintenance_scope() -> JobScope:
    """维护面：启动回收残留的 running（无归属过滤，唯一没有 owner 的用户面外动作）。"""
    return JobScope(owner_id=0, maintenance=True)


@dataclass(frozen=True, slots=True)
class FinishPayload:
    """收尾要一起落库的载荷（正文 / 章节 / 用量 / 待确认清单 / token 两列）。"""

    draft_content: str = ""
    completed_sections: list[dict[str, Any]] | None = None
    usage: str | None = None
    pending_items: list[dict[str, Any]] | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass(frozen=True, slots=True)
class TransitionResult:
    """一次转换的结果：**行数是唯一判据**。

    `rows > 0` 才算真的改到了；`rows == 0` 时 `status` / `error_code` / `error_message`
    如实带回库里那一行的事实（单条任务读得到，批量作用域没有确定的「那一行」，为空串）。
    """

    event: JobEvent
    rows: int
    status: str = ""
    error_code: str = ""
    error_message: str = ""

    @property
    def applied(self) -> bool:
        """是否真的改到了行（判据只有这一个）。"""
        return self.rows > 0

    @property
    def discarded(self) -> bool:
        """本次结果是否被丢弃（改到 0 行）。"""
        return self.rows == 0


def _expected_status(event: JobEvent) -> str:
    """事件成功后任务应处于的状态（三态只有这两个终态）。"""
    return STATUS_COMPLETED if event is JobEvent.SUCCEED else STATUS_FAILED


def _scope_has_job(scope: JobScope) -> bool:
    return scope.job_id is not None


def _validate(event: JobEvent, scope: JobScope, payload: FinishPayload | None) -> None:
    """校验「事件 + 期望条件」的组合是否合法（拼错是程序错误，直接抛）。"""
    if scope.maintenance:
        if event is not JobEvent.FAIL:
            raise ValueError("维护面只允许「失败收尾」：启动回收把残留 running 判死")
        return
    if event is JobEvent.SUCCEED:
        if not _scope_has_job(scope) or payload is None:
            raise ValueError("成功收尾必须给单条任务与收尾载荷")
    elif event is JobEvent.FAIL:
        if not _scope_has_job(scope) or payload is None:
            raise ValueError("失败收尾必须给单条任务与收尾载荷")
    elif event is JobEvent.CANCEL:
        if not (_scope_has_job(scope) or scope.solution_filter or scope.owner_wide):
            raise ValueError("取消必须有作用对象（任务 / 方案 / owner）")
    elif event is JobEvent.SUPERSEDE:
        if scope.kind is None:
            raise ValueError("顶替的期望条件必须带产物类型")
    else:  # pragma: no cover - StrEnum 已穷尽
        raise ValueError(f"未知的转换事件：{event}")


def _write(
    event: JobEvent,
    scope: JobScope,
    payload: FinishPayload | None,
    *,
    error_code: str,
    error_message: str,
    session: Session,
) -> tuple[int, str, str, str]:
    """执行条件写，返回 `(行数, 观察到的状态, 观察到的错误码, 观察到的错误文案)`。

    0 行时（单条任务）在同一事务里读回那一行：调用方据此知道「别人已经把这一行
    收成了什么」，而不是猜。
    """
    repo = GenerationJobRepository(session)
    owner_id = scope.owner_id
    if event is JobEvent.SUCCEED:
        rows = repo.finish(
            owner_id,
            int(scope.job_id or 0),
            draft_content=payload.draft_content if payload else "",
            completed_sections=list(payload.completed_sections or []) if payload else [],
            usage=payload.usage if payload else None,
            pending_items=payload.pending_items if payload else None,
            prompt_tokens=payload.prompt_tokens if payload else 0,
            completion_tokens=payload.completion_tokens if payload else 0,
        )
    elif event is JobEvent.FAIL:
        if scope.maintenance:
            rows = repo.reap_running(
                error_code=error_code or "INTERNAL_ERROR",
                error_message=error_message or "服务重启，任务已中断",
            )
        else:
            rows = repo.fail(
                owner_id,
                int(scope.job_id or 0),
                error_code=error_code or "INTERNAL_ERROR",
                error_message=error_message or "生成失败",
                draft_content=payload.draft_content if payload else "",
                completed_sections=list(payload.completed_sections or []) if payload else [],
                usage=payload.usage if payload else None,
                pending_items=payload.pending_items if payload else None,
                prompt_tokens=payload.prompt_tokens if payload else 0,
                completion_tokens=payload.completion_tokens if payload else 0,
                # 中止类失败允许「补一次载荷」：状态早已被取消/顶替路径落成终态，
                # 但用量与半截正文还没记下来。条件在仓储里写死（见 `fail` 的注释）。
                abort_code=error_code if error_code in ABORT_CODES else "",
            )
    elif event is JobEvent.CANCEL:
        code = error_code or CODE_CANCELLED
        if _scope_has_job(scope):
            rows = 1 if repo.cancel_one(
                owner_id,
                int(scope.job_id or 0),
                error_code=code,
                error_message=error_message,
            ) else 0
        elif scope.solution_filter:
            rows = repo.cancel_running(
                owner_id,
                solution_id=int(scope.solution_id),
                error_code=code,
                error_message=error_message,
            )
        else:
            rows = repo.cancel_running_for_owner(
                owner_id, error_code=code, error_message=error_message
            )
    else:
        rows = repo.supersede_active(
            owner_id,
            solution_id=scope.solution_id,
            kind=str(scope.kind or ""),
            error_code=error_code or CODE_SUPERSEDED,
            error_message=error_message or "被新任务取代",
        )

    if rows or not _scope_has_job(scope):
        return rows, "", "", ""
    job = repo.get(owner_id, int(scope.job_id or 0))
    if job is None:
        return rows, "", "", ""
    return rows, job.status, job.error_code or "", job.error_message or ""


def _match_runtimes(scope: JobScope) -> list[JobRuntime]:
    """按期望条件挑出内存里的运行时（批量作用域用）。"""
    scope_kind = scope.kind
    matched: list[JobRuntime] = []
    for runtime in _RUNTIMES.values():
        if runtime.owner_id != scope.owner_id:
            continue
        if scope.job_id is not None and runtime.job_id != scope.job_id:
            continue
        if scope.solution_filter and runtime.solution_id != scope.solution_id:
            continue
        if scope_kind is not None and runtime.kind != scope_kind:
            continue
        matched.append(runtime)
    return matched


def request_cancel(runtime: JobRuntime) -> None:
    """置「取消意图」（用户停止 / 删方案 / 删账号 / 排空都只置这一个标记）。

    它不是一次状态转换：`runtime.status` 保持不变，真正的终态由这条协程自己收尾时
    由 `_settle` 结算。对话类另外记下「被中途停止」，收尾时据此保留已说出口的话。
    """
    runtime.cancelled = True
    if runtime.kind == "chat":
        runtime.chat_interrupted = True


def request_supersede(runtime: JobRuntime) -> None:
    """置「被顶替意图」：同一份文档已有更新的任务接手，这一支应主动让开。"""
    runtime.superseded = True


def _settle(
    result: TransitionResult,
    scope: JobScope,
    *,
    runtime: JobRuntime | None,
) -> None:
    """把转换结果结算到内存（**内存侧唯一的写点**）。

    只写该写的：收尾写 `status` / 错误码；取消与顶替只置意图标志（终态由被请求的
    那条协程自己收尾时结算）。容器是本模块的 `_RUNTIMES`，调用方不必伸手。
    """
    targets = [runtime] if runtime is not None else _match_runtimes(scope)
    for target in targets:
        if result.event is JobEvent.CANCEL:
            request_cancel(target)
            continue
        if result.event is JobEvent.SUPERSEDE:
            request_supersede(target)
            continue
        # 行都没了（例如账号被删、任务行随之消失）也照样结算：这一次收尾是确定的，
        # 内存里不该留着一个「还在 running」的运行时。
        settled_status = result.status or _expected_status(result.event)
        target.status = settled_status
        target.failed = settled_status == STATUS_FAILED
        target.error_code = result.error_code
        target.error_message = result.error_message


def _log_discard(event: JobEvent, scope: JobScope, rows: int, observed: str) -> None:
    """改到 0 行时留一条**可查的丢弃日志**（不重试、不重新排队，只说明发生了什么）。

    只对**单条任务**记：批量作用域（按方案 / 按 owner / 启动回收）改到 0 行是
    「本来就没有要改的」（例如给一份还没有任务的方案新建第一单时的顶替），
    那不是「晚到的结果被丢弃」，记一条 warning 只会污染日志页。
    """
    if scope.job_id is None:
        return
    logger.warning(
        "状态转换未改到任何行，本次结果已丢弃",
        extra={
            "event": "job.transition_discarded",
            "job_event": str(event),
            "job_id": scope.job_id,
            "user_id": scope.owner_id,
            "solution_id": scope.solution_id,
            "kind": scope.kind,
            "attempted_status": _expected_status(event),
            "observed_status": observed,
        },
    )


def transition(
    event: JobEvent,
    scope: JobScope,
    *,
    runtime: JobRuntime | None = None,
    payload: FinishPayload | None = None,
    error_code: str = "",
    error_message: str = "",
    session: Session | None = None,
) -> TransitionResult:
    """**唯一的状态转换动作**：事件 + 期望条件 → 结果（以影响行数为唯一判据）。

    Args:
        event: 想做什么（成功收尾 / 失败收尾 / 取消 / 顶替）。
        scope: 期望条件（哪一条 / 哪一批，前置状态恒为 `running`）。
        runtime: 单条任务的内存运行时。给了它，成功时同步结算 `status` / 错误码。
        payload: 收尾载荷（`SUCCEED` / `FAIL` 必填）。
        error_code: `FAIL` / `CANCEL` / `SUPERSEDE` 的错误码（后两者留空时取默认值）。
        error_message: 落进 `error_message` 的文案。
        session: 调用方自己持有事务时传入（删方案 / 删账号 / 登记顶替要和别的写同生共死）。
            **传入时本函数不结算内存**：调用方必须在事务提交之后调
            `settle_after_commit(result, scope)`，否则会留下「协程停了、库里还在跑」的错位。

    Returns:
        `TransitionResult`：`rows == 0` 表示本次结果被丢弃（已留丢弃日志）。
    """
    _validate(event, scope, payload)

    if session is not None:
        rows, observed_status, observed_code, observed_message = _write(
            event,
            scope,
            payload,
            error_code=error_code,
            error_message=error_message,
            session=session,
        )
        result = _result(
            event,
            rows,
            requested_code=error_code,
            requested_message=error_message,
            observed_status=observed_status,
            observed_code=observed_code,
            observed_message=observed_message,
        )
        if result.discarded:
            _log_discard(event, scope, rows, observed_status)
        return result

    with session_scope() as own_session:
        rows, observed_status, observed_code, observed_message = _write(
            event,
            scope,
            payload,
            error_code=error_code,
            error_message=error_message,
            session=own_session,
        )
    result = _result(
        event,
        rows,
        requested_code=error_code,
        requested_message=error_message,
        observed_status=observed_status,
        observed_code=observed_code,
        observed_message=observed_message,
    )
    if result.discarded:
        _log_discard(event, scope, rows, observed_status)
    _settle(result, scope, runtime=runtime)
    return result


def _result(
    event: JobEvent,
    rows: int,
    *,
    requested_code: str,
    requested_message: str,
    observed_status: str,
    observed_code: str,
    observed_message: str,
) -> TransitionResult:
    """把写入结果折成 `TransitionResult`（改到了取事件的目标值，没改到取观察到的事实）。"""
    if rows:
        if event is JobEvent.SUCCEED:
            return TransitionResult(event=event, rows=rows, status=STATUS_COMPLETED)
        return TransitionResult(
            event=event,
            rows=rows,
            status=STATUS_FAILED,
            error_code=requested_code or _default_code(event),
            error_message=requested_message,
        )
    return TransitionResult(
        event=event,
        rows=rows,
        status=observed_status,
        error_code=observed_code,
        error_message=observed_message,
    )


def _default_code(event: JobEvent) -> str:
    if event is JobEvent.CANCEL:
        return CODE_CANCELLED
    if event is JobEvent.SUPERSEDE:
        return CODE_SUPERSEDED
    return ""


def settle_after_commit(result: TransitionResult, scope: JobScope) -> None:
    """调用方持有事务时，**提交之后**把结果结算到内存（与 `transition(session=...)` 配对）。

    为什么必须等提交：事务回滚时库里什么都没变，提前置内存标记会留下「协程停了、
    库里还在跑」的错位——删方案失败却把在途任务掐掉就是这种坏中间态。
    """
    _settle(result, scope, runtime=None)


def note_failure(runtime: JobRuntime, *, error_code: str, error_message: str) -> None:
    """记录**失败意图**（上游 `error` 事件 / 兜底异常用），由收尾时结算成终态。

    它不是一次转换：`runtime.status` 保持不变，任务仍在 running。把它单独收在这里，
    是为了让 `runtime` 上「谁写终态、谁只记意图」这条线只有一处定义。
    """
    runtime.failed = True
    runtime.error_code = error_code
    runtime.error_message = error_message
