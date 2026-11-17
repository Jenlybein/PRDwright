"""任务状态机：**七态**下唯一的转换入口（票 #79 立起，票 #84 扩宽）。

## 为什么要有这个模块

改状态原先散在八处：`finish` / `fail` / 取消三兄弟 / `supersede_active` /
`reap_running`，外加内存里的 `runtime.status` 一处。每一处都各自实现一遍
「从什么状态能到什么状态、失败带哪个错误码、内存那一份要不要跟着改」——同一份规则
写六遍，迟早有一份写反。

七态（票 #84）之后这件事更必须收口：状态从三个值变成七个值，取消从一步变成两步
（请求 → 确认），「被取代」和「已取消」从 `error_code` 里拆出来变成两个状态。
分支只会更多，不可能靠「六处各自记得改」活下去。

## 七个状态与完整转换表

取值定义在 `repositories/generation_job_repo.py`（全仓唯一一处）：

`pending`（排队中）· `running`（执行中）· `cancelling`（取消中）·
`succeeded`（已定稿）· `failed`（失败）· `superseded`（被取代）· `cancelled`（已取消）

**这张表就是唯一的规则**（事件 → 前态 → 后态 → 条件）：

| 事件 | 前态 | 后态 | 条件 |
|---|---|---|---|
| `CLAIM` | `pending` | `running` | 单条任务；到期闸门 `next_run_at <= now`；写租约三列 |
| `SUCCEED` | `running` | `succeeded` | 单条任务；给出凭证时叠「凭证仍是本轮 + 未过期」 |
| `FAIL` | `running` | `failed` | 单条任务；给出凭证时叠「凭证仍是本轮 + 未过期」 |
| `REQUEST_CANCEL` | `pending` | `cancelled` | 单条任务；**没人在跑，立刻终态** |
| `REQUEST_CANCEL` | `running` / `cancelling` | `cancelling` | 单条任务；**保留租约**（执行者还要用它确认） |
| `REQUEST_CANCEL` | 任意非终态 | `cancelled` | **批量作用域**（删方案 / 删账号）：方案或账号已经不存在，没人会来确认退出，因此**单步收口**并清租约（P0-4） |
| `CONFIRM_CANCEL` | `running` / `cancelling` | `cancelled` | 单条任务（给出凭证时叠「本轮凭证 + 未过期」）；清租约、补一次载荷 |
| `EXPIRE_CANCELLING` | `cancelling` | `cancelled` | 单条任务 + 旧租约 fence（恢复器兜底，无载荷） |
| `EXPIRE_PENDING` | `pending` | `failed` | 单条任务；启动档专用（没人会来领的排队行） |
| `SUPERSEDE` | `pending` / `running` | `superseded` | 同 owner + 方案 + 产物；清租约。**不碰 `cancelling`**（用户已表达停止意图，见 P0-3） |
| `CONFIRM_SUPERSEDED` | `superseded` 且 `usage IS NULL` | `superseded` | 单条任务；只补一次载荷（不改状态） |
| `REQUEUE` | `running` | `pending` | 单条任务 + 旧租约 fence；清租约 + 写 `next_run_at` |
| `EXPIRE` | `running` | `failed` | 单条任务 + 旧租约 fence；清租约 |

四个终态（`succeeded` / `failed` / `superseded` / `cancelled`）**一律清空租约三列**；
唯一的例外是「取消请求」——它只是请求，租约留给还有资格确认的那位执行者。

## 判据：影响行数是唯一的裁判

所有写都是**条件写**，`rows == 0` 表示本次结果被丢弃：只留一条丢弃日志
（`event="job.transition_discarded"`），不改内存、不重试、不重新排队
（spec 六·问题三：失去资格的执行者不许凭空制造新尝试）。

## 取消为什么是两步（AC1 / AC2 / AC3）

- **排队中取消**：没有执行者，谁也不需要确认它退出——一步落 `cancelled`（AC1）。
- **执行中取消**：执行者可能正卡在模型调用里。先把行改成 `cancelling`（用户立刻看见
  「正在取消」），**不清租约**：那个执行者还有资格、也还需要用它把半截正文与用量
  写下来。它在**下一个动作点**（动作点续租 / 下一个事件）看到标记后退出并确认
  （`CONFIRM_CANCEL`，AC2）。
- **执行者卡住不退出**：租约到期，恢复器扫到 `cancelling + 过期租约`，用扫描时读到的
  旧凭证把它收成 `cancelled`（`EXPIRE_CANCELLING`，AC3）。

**唯一的例外是批量作用域**（删方案 / 删账号，P0-4）：方案或账号已经不在了，不会再有
「执行者来确认退出」这件事，所以任意非终态**一步**落 `cancelled` 并清租约。执行者
醒来照常收尾，但凭证已经不作数 → 改到 0 行 → 丢弃（它这一轮烧掉的用量不再补记，
因为已经没有一行属于它了；取消记账在请求那一刻完成）。

## 谁在调这些转换

单条转换的调用方是交互路径与执行者；批量转换的调用方是删方案 / 删账号；`REQUEUE` /
`EXPIRE` / `EXPIRE_CANCELLING` / `EXPIRE_PENDING` 的调用方是恢复器——它由
`services/job_scheduler.py` 的周期任务在生产里驱动（此前这三个维护单元没有调用者）。

## 对外 API 的 `status` 就是这七个值

`error_code` **不再承担状态语义**（AC4）：已取消 / 被取代的行的 `error_code` 是空串，
原因写在 `error_message`；`status` 才是「现在算什么」的唯一答案。唯一保留的
「非 `ErrorCode` 成员」的码是 `LEASE_EXPIRED`——它是**失败原因**（租约到期），
不是状态。

## 执行账本（#80）：跟着收尾一起记一行

每次**执行有结论**都在 `job_attempts` 记一行。写点在 `transition` 里、与状态更新
**同一个事务**：

| 事件 | 记不记账本 | 理由 |
|---|---|---|
| `SUCCEED` | 记（`completed`） | 执行有结论 |
| `FAIL` | 记（`failed`） | 执行有结论 |
| `CONFIRM_CANCEL` / `EXPIRE_CANCELLING` | 记（`cancelled`） | 这一轮没跑成，但原因是用户取消——结论仍是确定的 |
| `REQUEUE` / `EXPIRE` / `EXPIRE_PENDING` | 记（`failed`） | 这一轮超时没跑成也是结论 |
| `REQUEST_CANCEL`（单条） | **不记** | 它只是「请求停止」：真正的结论由确认那一方给 |
| `REQUEST_CANCEL`（批量） | 记（`cancelled`） | 单步收口：这一批行此刻就进了终态，不会再有确认方 |
| `SUPERSEDE` / `CONFIRM_SUPERSEDED` | **不记** | 被取代不是这次执行的结局（票面口径） |

**账本写失败的口径**：审计数据写不进去要**可见**（`job.attempt_record_failed`
warning），但**不许把一次真实成功的生成判死**——账本用**保存点**写完，写坏只回滚
这一笔。副作用失败不改变结论，但必须留痕。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlmodel import Session

from backend.core.clock import utc_now_text
from backend.core.engine import session_scope
from backend.repositories.generation_job_repo import (
    STATUS_CANCELLED,
    STATUS_CANCELLING,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    STATUS_SUPERSEDED,
    GenerationJobRepository,
)
from backend.repositories.job_attempt_repo import (
    OUTCOME_CANCELLED,
    OUTCOME_COMPLETED,
    OUTCOME_FAILED,
    JobAttemptRepository,
)
from backend.services import job_backoff
from backend.services.job_runtime import _RUNTIMES

if TYPE_CHECKING:
    from backend.services.job_runtime import JobRuntime

logger = logging.getLogger(__name__)

# 租约到期的 `error_code`：**不是** `ErrorCode` 成员（它不是模型层错误，只是「这一轮
# 没跑成」的原因），由本模块统一给出，仓储与收尾都不再各写一份字面量。
# 它是**失败原因**，不是状态：`failed + LEASE_EXPIRED` 说「为什么失败」。
CODE_LEASE_EXPIRED = "LEASE_EXPIRED"


class JobEvent(StrEnum):
    """转换意图（七态的全部出口）。

    取消相关的三条是票 #84 新增的：

    - `REQUEST_CANCEL`：**请求停止**。单条任务里，排队中的行一步落已取消、执行中的行
      只转成「取消中」并保留租约——执行者还要用它收口；**批量作用域**（删方案 /
      删账号）没有任何人会来确认，因此任意非终态一步落已取消并清租约（P0-4）。
    - `CONFIRM_CANCEL`：**执行者确认退出**。把取消中 / 执行中落成已取消，清租约，
      并补一次载荷（半截正文与已经烧掉的用量）。
    - `EXPIRE_CANCELLING`：**恢复器兜底**。执行者卡住不退出、租约过期之后，由它把
      取消中落成已取消。

    `SUPERSEDE` 仍是「请求顶替」（批量写终态），`CONFIRM_SUPERSEDED` 是执行者醒来后
    「补一次载荷」：顶替是登记路径先落终态，被顶替那一支烧掉的 token 只能由它自己补。

    `EXPIRE_PENDING` 是启动档专用（票 #84）：进程刚起来时，库里那条**排队中**的行
    没有任何执行者会来领（启动档跑在调度循环之前），必须收成「服务重启中断」的失败——
    不然它就永远停在排队中。它不能复用 `FAIL`：`FAIL` 的前态是执行中，放宽到
    `pending` 会让「旧执行者的晚到失败」写进一条已经被恢复器交还队列的行。
    """

    CLAIM = "claim"
    SUCCEED = "succeed"
    FAIL = "fail"
    REQUEST_CANCEL = "request_cancel"
    CONFIRM_CANCEL = "confirm_cancel"
    EXPIRE_CANCELLING = "expire_cancelling"
    EXPIRE_PENDING = "expire_pending"
    SUPERSEDE = "supersede"
    CONFIRM_SUPERSEDED = "confirm_superseded"
    REQUEUE = "requeue"
    EXPIRE = "expire"


# 会让任务落进终态的事件（对外「这次转换有没有结束任务」的判断用）。
TERMINAL_EVENTS: frozenset[JobEvent] = frozenset(
    {
        JobEvent.SUCCEED,
        JobEvent.FAIL,
        JobEvent.REQUEST_CANCEL,
        JobEvent.CONFIRM_CANCEL,
        JobEvent.EXPIRE_CANCELLING,
        JobEvent.EXPIRE_PENDING,
        JobEvent.SUPERSEDE,
        JobEvent.EXPIRE,
    }
)


@dataclass(frozen=True, slots=True)
class LeaseGrant:
    """一次抢占要写进库的执行权：谁领的 / 本轮凭证 / 到几点失效 / 判据用的「现在」。"""

    owner: str
    token: str
    expire_at: str
    now_text: str


@dataclass(frozen=True, slots=True)
class LeaseFence:
    """恢复器（#83）扫描时读到的**旧租约凭证**：本轮凭证 + 到期时间。

    两列一起进条件写，回答的是「这一行还是不是我扫描时看到的那一份死租约」：
    凭证被换掉（别人已经接手）→ 条件不成立；到期时间被推后（执行者其实还在跑，
    刚续了租）→ 条件同样不成立。
    """

    token: str
    expire_at: str


@dataclass(frozen=True, slots=True)
class JobScope:
    """转换的**期望条件**：作用对象（哪一条 / 哪一批）。

    由具名构造器生成，不直接拼字段。批次维度用四个互斥的开关表达：`job_id`（单条）、
    `solution_filter`（按方案）、`owner_wide`（按 owner）、`kind`（同产物）。
    `solution_filter` 单独一位，因为「按方案过滤」与「方案为空」是两件事。
    """

    owner_id: int
    job_id: int | None = None
    solution_id: int | None = None
    solution_filter: bool = False
    kind: str | None = None
    owner_wide: bool = False


def job_scope(owner_id: int, job_id: int) -> JobScope:
    """单条任务（成功 / 失败 / 取消确认 / 恢复器接手）。"""
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


@dataclass(frozen=True, slots=True)
class FinishPayload:
    """收尾要一起落库的载荷（正文 / 用量 / 待确认清单 / token 两列 / 耗时）。

    `duration_ms` 与 `usage` 里的 `duration_ms` 是同一个值：账本要回答「跑了多久」，
    而它属于收尾载荷、不该在状态机里再从 usage JSON 里解析一遍。
    """

    draft_content: str = ""
    usage: str | None = None
    pending_items: list[dict[str, Any]] | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0
    # 「有内容可用但不完整」（票 #85）。与终态同一条语句落库。
    partial: bool = False


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
    """事件成功后任务**最终**应处于的状态（丢弃日志与结果上报用）。"""
    if event is JobEvent.CLAIM:
        return STATUS_RUNNING
    if event is JobEvent.REQUEUE:
        return STATUS_PENDING
    if event is JobEvent.SUCCEED:
        return STATUS_SUCCEEDED
    if event is JobEvent.SUPERSEDE or event is JobEvent.CONFIRM_SUPERSEDED:
        return STATUS_SUPERSEDED
    if event in (
        JobEvent.FAIL,
        JobEvent.EXPIRE,
        JobEvent.EXPIRE_PENDING,
    ):
        return STATUS_FAILED
    return STATUS_CANCELLED


def _scope_has_job(scope: JobScope) -> bool:
    return scope.job_id is not None


def _validate(
    event: JobEvent,
    scope: JobScope,
    payload: FinishPayload | None,
    lease_grant: LeaseGrant | None = None,
    lease_fence: LeaseFence | None = None,
) -> None:
    """校验「事件 + 期望条件」的组合是否合法（拼错是程序错误，直接抛）。"""
    if event is JobEvent.CLAIM:
        if not _scope_has_job(scope) or lease_grant is None:
            raise ValueError("抢占必须给单条任务与本轮执行权（谁领的 / 凭证 / 到期时间）")
        return
    if event is JobEvent.SUCCEED or event is JobEvent.FAIL:
        if not _scope_has_job(scope) or payload is None:
            raise ValueError("成功 / 失败收尾必须给单条任务与收尾载荷")
    elif event is JobEvent.REQUEST_CANCEL:
        if not (_scope_has_job(scope) or scope.solution_filter or scope.owner_wide):
            raise ValueError("取消请求必须有作用对象（任务 / 方案 / owner）")
    elif event is JobEvent.CONFIRM_CANCEL:
        if not _scope_has_job(scope):
            raise ValueError("取消确认只针对单条任务（批量作用域没有「被谁确认」这回事）")
    elif event is JobEvent.EXPIRE_CANCELLING:
        if not _scope_has_job(scope) or lease_fence is None or not lease_fence.token:
            raise ValueError("取消兜底必须给单条任务与扫描时读到的旧租约凭证")
    elif event is JobEvent.EXPIRE_PENDING:
        if not _scope_has_job(scope):
            raise ValueError("启动档清理排队行只针对单条任务")
    elif event is JobEvent.SUPERSEDE:
        if scope.kind is None:
            raise ValueError("顶替的期望条件必须带产物类型")
    elif event is JobEvent.CONFIRM_SUPERSEDED:
        if not _scope_has_job(scope):
            raise ValueError("被顶替之后的载荷补记只针对单条任务")
    elif event is JobEvent.REQUEUE or event is JobEvent.EXPIRE:
        if not _scope_has_job(scope) or lease_fence is None or not lease_fence.token:
            raise ValueError(
                "恢复事件必须给单条任务与扫描时读到的旧租约凭证（凭证 / 到期时间）"
            )
    else:  # pragma: no cover - StrEnum 已穷尽
        raise ValueError(f"未知的转换事件：{event}")


@dataclass(frozen=True, slots=True)
class _WriteResult:
    """一次条件写的**原始结果**：行数 + 读回的事实。

    `affected_ids` 只有**批量作用域**会给（删方案 / 删账号的单步收口）：那时账本要逐条
    记，而「改到了几行」回答不了「是哪几行」。单条任务本来就知道 id，不必从库里读回来。
    """

    rows: int
    observed_status: str = ""
    observed_code: str = ""
    observed_message: str = ""
    affected_ids: tuple[int, ...] = ()


def _write(
    event: JobEvent,
    scope: JobScope,
    payload: FinishPayload | None,
    *,
    error_code: str,
    error_message: str,
    session: Session,
    lease_grant: LeaseGrant | None = None,
    lease_token: str = "",
    lease_fence: LeaseFence | None = None,
    now_text: str = "",
) -> _WriteResult:
    """执行条件写，返回行数与观察到的状态（0 行时在**同一事务**里读回那一行）。"""
    repo = GenerationJobRepository(session)
    owner_id = scope.owner_id
    payload = payload or FinishPayload()
    # 批量作用域才需要「改到了哪几条」（账本逐条记）；单条任务本来就知道 id。
    affected: tuple[int, ...] = ()
    if event is JobEvent.CLAIM:
        grant = lease_grant or LeaseGrant(owner="", token="", expire_at="", now_text="")
        rows = repo.claim(
            owner_id,
            int(scope.job_id or 0),
            lease_owner=grant.owner,
            lease_token=grant.token,
            expire_at=grant.expire_at,
            now_text=grant.now_text,
        )
    elif event is JobEvent.SUCCEED:
        rows = repo.finish(
            owner_id,
            int(scope.job_id or 0),
            draft_content=payload.draft_content,
            usage=payload.usage,
            pending_items=payload.pending_items,
            prompt_tokens=payload.prompt_tokens,
            completion_tokens=payload.completion_tokens,
            partial=bool(payload.partial),
            lease_token=lease_token,
            now_text=now_text,
        )
    elif event is JobEvent.FAIL:
        rows = repo.fail(
            owner_id,
            int(scope.job_id or 0),
            error_code=error_code or "INTERNAL_ERROR",
            error_message=error_message or "生成失败",
            draft_content=payload.draft_content,
            usage=payload.usage,
            pending_items=payload.pending_items,
            prompt_tokens=payload.prompt_tokens,
            completion_tokens=payload.completion_tokens,
            partial=bool(payload.partial),
            lease_token=lease_token,
            now_text=now_text,
        )
    elif event is JobEvent.REQUEST_CANCEL:
        if _scope_has_job(scope):
            rows = 1 if repo.request_cancel_one(
                owner_id, int(scope.job_id or 0), error_message=error_message
            ) else 0
        elif scope.solution_filter:
            ids = repo.request_cancel_solution(
                owner_id,
                solution_id=int(scope.solution_id),
                error_message=error_message,
            )
            rows, affected = len(ids), tuple(ids)
        else:
            ids = repo.request_cancel_owner(owner_id, error_message=error_message)
            rows, affected = len(ids), tuple(ids)
    elif event is JobEvent.CONFIRM_CANCEL:
        rows = repo.confirm_cancel(
            owner_id,
            int(scope.job_id or 0),
            error_message=error_message or "任务已取消",
            draft_content=payload.draft_content,
            usage=payload.usage,
            pending_items=payload.pending_items,
            prompt_tokens=payload.prompt_tokens,
            completion_tokens=payload.completion_tokens,
            partial=bool(payload.partial),
            lease_token=lease_token,
            now_text=now_text,
        )
    elif event is JobEvent.EXPIRE_CANCELLING:
        fence = lease_fence or LeaseFence(token="", expire_at="")
        rows = repo.cancel_expired(
            owner_id,
            int(scope.job_id or 0),
            lease_token=fence.token,
            lease_expire_at=fence.expire_at,
            error_message=error_message or "任务已取消",
            now_text=now_text,
        )
    elif event is JobEvent.EXPIRE_PENDING:
        rows = repo.fail_queued(
            owner_id,
            int(scope.job_id or 0),
            error_code=error_code or "INTERNAL_ERROR",
            error_message=error_message or "服务重启，任务已中断",
            draft_content=payload.draft_content,
            usage=payload.usage,
            pending_items=payload.pending_items,
            prompt_tokens=payload.prompt_tokens,
            completion_tokens=payload.completion_tokens,
            partial=bool(payload.partial),
            now_text=now_text,
        )
    elif event is JobEvent.SUPERSEDE:
        rows = repo.supersede_active(
            owner_id,
            solution_id=scope.solution_id,
            kind=str(scope.kind or ""),
            error_message=error_message or "被新任务取代",
        )
    elif event is JobEvent.CONFIRM_SUPERSEDED:
        rows = repo.settle_superseded_payload(
            owner_id,
            int(scope.job_id or 0),
            draft_content=payload.draft_content,
            usage=payload.usage,
            pending_items=payload.pending_items,
            prompt_tokens=payload.prompt_tokens,
            completion_tokens=payload.completion_tokens,
            partial=bool(payload.partial),
        )
    elif event is JobEvent.REQUEUE:
        fence = lease_fence or LeaseFence(token="", expire_at="")
        # 失败退避（#88）：回排队**不等于立刻能领**。该等多久由唯一映射表算
        # （`services/job_backoff`，按错误码值查），写进 `next_run_at`，与清租约、
        # 改回 pending、更新 `updated_at` 放在**同一条 UPDATE**里。
        requeue_now = now_text or utc_now_text()
        backoff = job_backoff.of(error_code or _default_code(event), now_text=requeue_now)
        rows = repo.requeue_lease(
            owner_id,
            int(scope.job_id or 0),
            lease_token=fence.token,
            lease_expire_at=fence.expire_at,
            now_text=requeue_now,
            next_run_at=backoff.next_run_at,
        )
    elif event is JobEvent.EXPIRE:
        fence = lease_fence or LeaseFence(token="", expire_at="")
        rows = repo.fail_expired(
            owner_id,
            int(scope.job_id or 0),
            lease_token=fence.token,
            lease_expire_at=fence.expire_at,
            error_code=error_code or CODE_LEASE_EXPIRED,
            error_message=error_message or "执行者进程已消失，租约已过期",
            now_text=now_text,
        )
    else:  # pragma: no cover - StrEnum 已穷尽
        raise ValueError(f"未知的转换事件：{event}")

    # 单条任务的「改到了哪儿」总是从库里读回来：取消请求可能落在「排队中 → 已取消」
    # 或「执行中 → 取消中」两种去向，结果必须如实带出去（调用方据此决定内存怎么结算）。
    if not _scope_has_job(scope) or (rows and event not in _STATUS_REPORTING_EVENTS):
        return _WriteResult(rows=rows, affected_ids=affected)
    job = repo.get(owner_id, int(scope.job_id or 0))
    if job is None:
        return _WriteResult(rows=rows, affected_ids=affected)
    return _WriteResult(
        rows=rows,
        observed_status=job.status,
        observed_code=job.error_code or "",
        observed_message=job.error_message or "",
        affected_ids=affected,
    )


# 结果里必须带「库里那一行现在是什么状态」的事件：取消请求的去向有两种，不能猜。
_STATUS_REPORTING_EVENTS: frozenset[JobEvent] = frozenset(
    {
        JobEvent.REQUEST_CANCEL,
        JobEvent.CONFIRM_CANCEL,
        JobEvent.CONFIRM_SUPERSEDED,
    }
)


# ----------------------------------------------------------------------------
# 执行账本（#80）：跟着这次转换记一行
# ----------------------------------------------------------------------------

_MAX_SUMMARY_CHARS = 240

# 疑似凭据的片段：上游回显 `sk-...`、`Bearer ...`、`api_key=...` 这类串时，账本里
# 不能原样留下。规则刻意保守——只抹「明显是密钥形态」的片段，不动普通文案。
_SUMMARY_SECRETS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}"), "<已脱敏>"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._-]{8,}"), r"\1 <已脱敏>"),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?key|token|password|secret)\s*[=:]\s*"
            r"[\"']?[^\s,;\"']{6,}"
        ),
        r"\1=<已脱敏>",
    ),
)


def _sanitize_summary(message: str) -> str:
    """把错误文案压成账本能长期存的样子：单行、截断、抹掉疑似凭据。"""
    text = re.sub(r"\s+", " ", str(message or "")).strip()
    for pattern, replacement in _SUMMARY_SECRETS:
        text = pattern.sub(replacement, text)
    if len(text) > _MAX_SUMMARY_CHARS:
        text = text[: _MAX_SUMMARY_CHARS - 1] + "…"
    return text


def _attempt_outcome(event: JobEvent, error_code: str, scope: JobScope) -> str | None:
    """这次转换该记成哪种结论；`None` = **不是执行结论，不记账本**。

    批量作用域的 `REQUEST_CANCEL`（删方案 / 删账号）是**一步落终态**，此刻就有结论，
    所以它要记；单条任务的 `REQUEST_CANCEL` 只是「请求停止」，结论由确认那一方给。
    """
    if event is JobEvent.SUCCEED:
        return OUTCOME_COMPLETED
    if event is JobEvent.CONFIRM_CANCEL or event is JobEvent.EXPIRE_CANCELLING:
        return OUTCOME_CANCELLED
    if event is JobEvent.REQUEST_CANCEL and scope.job_id is None:
        return OUTCOME_CANCELLED
    if (
        event is JobEvent.REQUEUE
        or event is JobEvent.EXPIRE
        or event is JobEvent.FAIL
        or event is JobEvent.EXPIRE_PENDING
    ):
        return OUTCOME_FAILED
    return None


def _attempt_targets(scope: JobScope, write: _WriteResult) -> tuple[int, ...]:
    """这次转换要给哪几条任务记账。

    单条任务就是它自己；批量作用域用条件写带回来的 `affected_ids`（`UPDATE … RETURNING`
    给的就是「真的改到了哪几行」，不必再猜）。
    """
    if write.rows <= 0:
        return ()
    if scope.job_id is not None:
        return (int(scope.job_id),)
    return write.affected_ids


def _record_attempt(
    event: JobEvent,
    scope: JobScope,
    write: _WriteResult,
    *,
    payload: FinishPayload | None,
    error_code: str,
    error_message: str,
    session: Session,
) -> None:
    """把这一次转换记进执行账本（**与状态更新同一个事务**）。

    ## 为什么用保存点

    账本是审计用途：写不进去必须**可见**（一条 `job.attempt_record_failed` warning），
    但**不许因此把一个真实成功的生成判死**。所以账本这一笔套在 `begin_nested()`
    （Postgres 的 SAVEPOINT）里：写坏只回滚到保存点，终态照常提交。
    """
    outcome = _attempt_outcome(event, error_code, scope)
    if outcome is None or write.rows <= 0:
        return
    targets = _attempt_targets(scope, write)
    if not targets:
        return

    if outcome == OUTCOME_COMPLETED:
        ledger_code = ""
        summary = ""
    elif outcome == OUTCOME_CANCELLED:
        # 已取消不是失败：结论里的 `outcome` 已经说清，错误码留空（AC4 的口径）。
        ledger_code = ""
        summary = _sanitize_summary(error_message)
    else:
        ledger_code = error_code or _default_code(event) or "INTERNAL_ERROR"
        summary = _sanitize_summary(error_message)
    prompt_tokens = payload.prompt_tokens if payload else 0
    completion_tokens = payload.completion_tokens if payload else 0
    duration_ms = payload.duration_ms if payload else 0

    repo = JobAttemptRepository(session)
    for job_id in targets:
        try:
            with session.begin_nested():
                repo.append(
                    job_id,
                    outcome=outcome,
                    error_code=ledger_code,
                    error_summary=summary,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    duration_ms=duration_ms,
                )
        except Exception:  # noqa: BLE001 - 审计写失败不许把已成功的生成判死
            logger.warning(
                "执行账本写入失败，本次结论不受影响",
                extra={
                    "event": "job.attempt_record_failed",
                    "job_id": job_id,
                    "outcome": outcome,
                    "error_code": ledger_code,
                },
                exc_info=True,
            )


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


def request_cancel(runtime: JobRuntime, message: str = "") -> None:
    """置「取消意图」（用户停止 / 删方案 / 删账号 / 排空都只置这一个标记）。

    它不是一次终态结算：`runtime.status` 保持不变（执行中），真正的终态由这条协程
    在下一个动作点确认时结算（`CONFIRM_CANCEL`）。对话类另外记下「被中途停止」，
    收尾时据此保留已说出口的话。`message` 是**原因文案**：取消不是失败，所以它落在
    `cancel_message` 而不是失败原因那一列；确认取消时原样写进 `error_message`。
    """
    runtime.cancelled = True
    if message:
        runtime.cancel_message = message
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

    - 抢占 / 回排队 / 判超时 / 取消兜底 **不结算**：前两者改的是租约（抢占时还没有
      运行时），后两者面对的是**别的进程**（或已经消失）里的执行者，本进程不该替它
      造一份终态（它能不能写结果由凭证判，不由内存状态判）。
    - 请求取消 / 请求顶替只置**意图**：终态由那条协程自己收尾时结算。
    - 确认取消 / 确认顶替 / 成功 / 失败写终态与错误码。
    """
    if result.event in (
        JobEvent.CLAIM,
        JobEvent.REQUEUE,
        JobEvent.EXPIRE,
        JobEvent.EXPIRE_CANCELLING,
        JobEvent.EXPIRE_PENDING,
    ):
        return
    targets = [runtime] if runtime is not None else _match_runtimes(scope)
    for target in targets:
        if result.event is JobEvent.REQUEST_CANCEL:
            request_cancel(target, result.error_message)
            continue
        if result.event is JobEvent.SUPERSEDE:
            # 「取消中」的那一支不带被顶替标记（P0-3）：库里那一行也没被改写
            # （`SUPERSEDABLE_STATUSES` 不含 `cancelling`），内存与库必须同一个口径。
            if target.status != STATUS_CANCELLING:
                request_supersede(target)
            continue
        if result.event is JobEvent.CONFIRM_SUPERSEDED:
            target.status = STATUS_SUPERSEDED
            target.failed = False
            target.error_code = ""
            target.error_message = target.error_message or result.error_message
            continue
        settled_status = result.status or _expected_status(result.event)
        target.status = settled_status
        target.failed = settled_status == STATUS_FAILED
        target.error_code = result.error_code
        target.error_message = result.error_message


def _log_discard(event: JobEvent, scope: JobScope, rows: int, observed: str) -> None:
    """改到 0 行时留一条**可查的丢弃日志**（不重试、不重新排队，只说明发生了什么）。

    只对**单条任务**记：批量作用域改到 0 行是「本来就没有要改的」，不是「晚到的结果
    被丢弃」，记一条 warning 只会污染日志页。
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
    lease_grant: LeaseGrant | None = None,
    lease_token: str = "",
    lease_fence: LeaseFence | None = None,
    now_text: str = "",
) -> TransitionResult:
    """**唯一的状态转换动作**：事件 + 期望条件 → 结果（以影响行数为唯一判据）。

    事件、前态、后态与条件见模块 docstring 的转换表——那张表是唯一的规则来源。

    Args:
        event: 想做什么（抢占 / 成功 / 失败 / 请求取消 / 确认取消 / 取消兜底 /
            请求顶替 / 确认顶替 / 回排队 / 判超时）。
        scope: 期望条件（哪一条 / 哪一批）。
        runtime: 单条任务的内存运行时。给了它，终态与错误码同步结算到这里。
        payload: 收尾载荷（`SUCCEED` / `FAIL` / `CONFIRM_CANCEL` / `CONFIRM_SUPERSEDED`
            用它；取消确认也带——半截正文与已经烧掉的用量要记下来）。
        error_code: `FAIL` / `EXPIRE` / `REQUEUE` 的错误码（后两者留空取默认值）。
            ⚠️ 取消 / 顶替**不再写错误码**：状态由 `status` 表达（AC4）。
        error_message: 落进 `error_message`（以及账本摘要）的文案。
        session: 调用方自己持有事务时传入（删方案 / 删账号 / 登记顶替要和别的写同生共死）。
            传入时本函数**不结算内存**：调用方必须在事务提交之后调
            `settle_after_commit(result, scope)`。
        lease_grant: `CLAIM` 必填：这一轮的执行权。
        lease_token: 收尾 / 取消确认时带上**本轮凭证**（仓储据此叠「凭证仍是本轮」判据）。
        lease_fence: `REQUEUE` / `EXPIRE` / `EXPIRE_CANCELLING` 必填：恢复器扫描时读到的
            旧租约凭证（凭证 + 到期时间）。
        now_text: 判据用的「现在」（留空取 `utc_now_text()`）。

    Returns:
        `TransitionResult`：`rows == 0` 表示本次结果被丢弃（已留丢弃日志）。
    """
    _validate(event, scope, payload, lease_grant, lease_fence)

    if session is not None:
        write = _write(
            event,
            scope,
            payload,
            error_code=error_code,
            error_message=error_message,
            session=session,
            lease_grant=lease_grant,
            lease_token=lease_token,
            lease_fence=lease_fence,
            now_text=now_text,
        )
        _record_attempt(
            event,
            scope,
            write,
            payload=payload,
            error_code=error_code,
            error_message=error_message,
            session=session,
        )
        result = _result(
            event,
            write.rows,
            requested_code=error_code,
            requested_message=error_message,
            observed_status=write.observed_status,
            observed_code=write.observed_code,
            observed_message=write.observed_message,
        )
        if result.discarded:
            _log_discard(event, scope, write.rows, write.observed_status)
        return result

    with session_scope() as own_session:
        write = _write(
            event,
            scope,
            payload,
            error_code=error_code,
            error_message=error_message,
            session=own_session,
            lease_grant=lease_grant,
            lease_token=lease_token,
            lease_fence=lease_fence,
            now_text=now_text,
        )
        _record_attempt(
            event,
            scope,
            write,
            payload=payload,
            error_code=error_code,
            error_message=error_message,
            session=own_session,
        )
    result = _result(
        event,
        write.rows,
        requested_code=error_code,
        requested_message=error_message,
        observed_status=write.observed_status,
        observed_code=write.observed_code,
        observed_message=write.observed_message,
    )
    if result.discarded:
        _log_discard(event, scope, write.rows, write.observed_status)
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
        return TransitionResult(
            event=event,
            rows=rows,
            status=observed_status or _expected_status(event),
            error_code=(
                requested_code
                if event in (JobEvent.FAIL, JobEvent.EXPIRE, JobEvent.EXPIRE_PENDING)
                else ""
            ),
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
    if event is JobEvent.REQUEUE or event is JobEvent.EXPIRE:
        return CODE_LEASE_EXPIRED
    return ""


def settle_after_commit(result: TransitionResult, scope: JobScope) -> None:
    """调用方持有事务时，**提交之后**把结果结算到内存（与 `transition(session=...)` 配对）。

    为什么必须等提交：事务回滚时库里什么都没变，提前置内存标记会留下「协程停了、
    库里还在跑」的错位。
    """
    _settle(result, scope, runtime=None)


def note_failure(runtime: JobRuntime, *, error_code: str, error_message: str) -> None:
    """记录**失败意图**（上游 `error` 事件 / 兜底异常用），由收尾时结算成终态。

    它不是一次转换：`runtime.status` 保持不变，任务仍在执行中。
    """
    runtime.failed = True
    runtime.error_code = error_code
    runtime.error_message = error_message
