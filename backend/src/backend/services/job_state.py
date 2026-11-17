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

`transition(事件, 期望条件) -> TransitionResult`。事件是几种意图，期望条件由
`JobScope` 给出（哪条任务 / 哪个方案 / 哪个 owner / 哪个产物）：

| 事件 | 期望条件 | 落库 | 内存 |
|---|---|---|---|
| `CLAIM` | 单条任务 | 抢占：写租约三列（谁领的 / 本轮凭证 / 到期时间） | 不结算（抢占时还没有运行时） |
| `SUCCEED` | 单条任务 | `finish`（completed） | status=completed、清错误码 |
| `FAIL` | 单条任务 | `fail`（failed + 错误码） | status=failed + 错误码 |
| `CANCEL` | 单条 / 方案 / owner | `failed + CANCELLED` | 只置「取消意图」标志 |
| `SUPERSEDE` | 同 owner+方案+产物 | `failed + SUPERSEDED` | 只置「被顶替意图」标志 |
| `REQUEUE` | 单条任务 + 旧租约凭证 | 清租约三列（仍是 running）+ 写 `next_run_at`（按错误码的退避） | 不结算（执行者在别的进程里，已不在场） |
| `EXPIRE` | 单条任务 + 旧租约凭证 | `failed + LEASE_EXPIRED` + 清租约 | 同上 |

四条终止路径全部改道经它：成功收尾与失败收尾在 `job_finalize._finalize`，
取消在 `job_service_impl.cancel_job` / `delete_solution_and_cancel_jobs` /
`cancel_for_user`，顶替在 `job_registration.register_in_transaction`。

### 恢复器（#83）的两条转换

`REQUEUE` / `EXPIRE` 是 #83 加的：执行者的进程没了、租约过期，由**恢复器**接手。
它们与别的转换**不同的只有期望条件**——带的不是「我要写什么」，而是**扫描时读到的
旧租约凭证**（`LeaseFence`：凭证 + 到期时间）。仓储据此做条件更新：凭证被换掉、
或到期时间被续租推后，都一行改不到（与 #82 的写权判据同一条规则）。改到 0 行就是
「这一行已经不是我看到的那一份死租约了」，跳过、不报错、不记账（丢弃日志同其它转换）。

一条过期租约的去向（回排队 / 判失败）由恢复器按账本决定；状态机只负责「这条转换
能不能成立」，它从不替调用方挑去向，也不碰调度（怎么扫、扫多久是 `job_recovery` 的事）。

### 失败退避（#88）：回排队**不等于立刻能领**

`REQUEUE` 除了清租约，还要写下「最早什么时候可以被下一个执行者领走」——`next_run_at`。
这一列不靠调用方另发一条 UPDATE，而是与清租约、`updated_at` 在**同一条条件 UPDATE**里
写完：否则「已经把行交给队列」与「退避时间还没写上」之间就有一个能被立刻领走的窗口，
连续限流恰好会从这个窗口里把三次机会烧光。

该等多久唯一由 `services/job_backoff` 的映射表决定（**按错误码值查，不看中文文案**）：
限流 30 秒、上游 5xx 10 秒、超时 5 秒、其余 5 秒。状态机只做一件事：把
`transition(now_text=...)` 的那个「现在」与错误码一起交给它，保证「判断」与「写入」
看到同一秒（AC5 里「时间入口可注入」的落点也在这里）。

到期闸门与排队顺序只有仓储一处定义（`_due_now` / `_queue_order`）：未到期的行
`claim` / `claimable` 都够不着，到期之后才按 `updated_at` 排队。

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

## 执行账本（#80）：跟着收尾一起记一行

每次**执行有结论**（成功 / 失败 / 取消 / 租约过期被恢复器接手 / 启动回收判死）都在
`job_attempts` 记一行：
第几次、成还是败、为什么（错误分类 + 脱敏摘要）、花了多少、跑了多久。写入点在
`transition` 里、与状态更新**同一个事务**——账本不是另一份独立的写，而是这次转换的
一部分。三条口径：

| 事件 | 记不记账本 | 理由 |
|---|---|---|
| `SUCCEED` | 记（`completed`） | 执行有结论 |
| `FAIL` | 记（`failed` / `cancelled`） | 执行有结论（含上游报错、预算超顶、回收判死） |
| `REQUEUE` | 记（`failed` + `LEASE_EXPIRED`） | 这一轮超时没跑成也是结论；不记的话「为什么重试」在账本里是空白 |
| `EXPIRE` | 记（`failed` + `LEASE_EXPIRED`） | 同上，且是最后一次 |
| `CANCEL` | **不记** | 它只是「请求停止」：行先落 `failed + CANCELLED` 让人马上看见，真正的结论由被请求的那条协程收尾时给出——那一刻才记（`FAIL` 分支）。不这么分就会出现「按停止记一笔、收尾再记一笔」，一次执行两行账 |
| `SUPERSEDE` | **不记** | 被取代**不是执行结论**（票面明确）：它是「同一份文档有更晚的任务接手」，`SUPERSEDED` 留在任务行上供排查。若在这里记账，账本会把一次正常让位记成一次失败 |

**账本写失败的口径**：审计数据写不进去要**可见**（留一条 `job.attempt_record_failed`
warning，带 job_id 与结论），但**不许把一次真实成功的生成判死**——账本用**保存点**
写完，写坏只回滚这一笔，任务终态照常提交。这与 `_write_document` 吞异常的口径同源：
「副作用失败不改变结论，但必须留痕」。反过来若让整笔事务回滚，任务会卡在 `running`
等回收，代价远大于少一行账。
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
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
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

# 取消 / 被顶替 / 租约过期的 `error_code` 取值：**不是** `ErrorCode` 成员（它们不是
# 错误，只是「没跑成」的几种正常原因），由本模块统一给出，仓储与收尾都不再各写一份
# 字面量。`LEASE_EXPIRED` 是恢复器（#83）给「执行者的进程没了、租约到期」的稳定标识符
# ——前端按 `error_code` 分支展示终态文案，换一个字符串就等于换一处契约。
CODE_CANCELLED = "CANCELLED"
CODE_SUPERSEDED = "SUPERSEDED"
CODE_LEASE_EXPIRED = "LEASE_EXPIRED"
# 「不是红色错误」的两个中止码：订阅侧据此决定补不补 `error` 事件。
ABORT_CODES = frozenset({CODE_CANCELLED, CODE_SUPERSEDED})


class JobEvent(StrEnum):
    """转换意图（三态下只有这几个出口）。

    `CLAIM` 是 #82 加的：**抢占**是「排队中 → 执行中」的转换，和终止事件一样经
    `transition` 走，不另写一套改状态的路径。三态里「排队中」与「执行中」共用
    `status='running'`，区别只在**有没有有效租约**——所以这条事件改的是租约三列，
    不是 status（七态是 #84 的事）。

    `REQUEUE` / `EXPIRE` 是 #83 加的：**恢复器**接手一条过期租约的两种去向。
    前者把租约交还队列（状态不变，下一次抢占会重新写凭证），后者判失败。两者都必须
    带扫描时读到的旧凭证（`LeaseFence`）——恢复器的判权与 #82 的写权是同一条规则。

    `REQUEUE` 在 #88 之后还多写一列 `next_run_at`（按 `error_code` 的退避时长）：回排队
    之后要等一段时间才允许被领走，避免连续限流把重试机会在几十秒内耗光。
    """

    CLAIM = "claim"
    SUCCEED = "succeed"
    FAIL = "fail"
    CANCEL = "cancel"
    SUPERSEDE = "supersede"
    REQUEUE = "requeue"
    EXPIRE = "expire"


@dataclass(frozen=True, slots=True)
class LeaseGrant:
    """一次抢占要写进库的执行权：谁领的 / 本轮凭证 / 到几点失效 / 判据用的「现在」。

    `now_text` 一起传是为了让「判断」与「修改」看到同一个时刻：抢占条件是
    「没有有效租约」，而「有效」是相对某个时刻说的；两处各取一次 `now()` 会让
    边界上的行为取决于毫秒级抖动。
    """

    owner: str
    token: str
    expire_at: str
    now_text: str


@dataclass(frozen=True, slots=True)
class LeaseFence:
    """恢复器（#83）扫描时读到的**旧租约凭证**：本轮凭证 + 到期时间。

    两列一起进条件写，回答的是「这一行还是不是我扫描时看到的那一份死租约」：
    - 凭证被换掉（别人已经接手）→ 条件不成立；
    - 到期时间被推后（执行者其实还在跑，刚续了租）→ 条件同样不成立。

    为什么不是只比凭证：续租不改凭证（`job_claimer.renew` 保持 token），只比凭证的
    话恢复器会把一条刚续过租、正在正常跑的任务抢回队列。到期时间同样是「这一轮」
    的身份。
    """

    token: str
    expire_at: str


@dataclass(frozen=True, slots=True)
class JobScope:
    """转换的**期望条件**：作用对象 + 前置状态（三态下前置恒为 `running`）。

    由下面的具名构造器生成，不直接拼字段：拼错一个字段就是「改到了别人的任务」。
    批次维度用四个互斥的开关表达：`job_id`（单条）、`solution_filter`（按方案）、
    `owner_wide`（按 owner）、`kind`（同产物）。`solution_filter` 单独一位，因为
    「按方案过滤」与「方案为空」是两件事：`solution_id=None` 是**合法的过滤值**
    （快捷入口逃生口那些没有方案的任务），不能拿 `None` 当「不过滤」用。

    「启动回收」不再是一档作用域（#83）：它改成逐条按租约收口（见 `job_recovery`），
    所以「没有 owner 的批量维护面」这个逃生口随旧实现一起删掉了。
    """

    owner_id: int
    job_id: int | None = None
    solution_id: int | None = None
    solution_filter: bool = False
    kind: str | None = None
    owner_wide: bool = False


def job_scope(owner_id: int, job_id: int) -> JobScope:
    """单条任务（成功收尾 / 失败收尾 / 用户按停止 / 恢复器接手）。"""
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
    """收尾要一起落库的载荷（正文 / 章节 / 用量 / 待确认清单 / token 两列 / 耗时）。

    `duration_ms` 与 `usage` 里的 `duration_ms` 是同一个值（都由
    `job_runtime_events._summary_data` 算出来）：账本要回答「跑了多久」，而它属于收尾
    载荷、不该在状态机里再从 usage JSON 里解析一遍。
    """

    draft_content: str = ""
    completed_sections: list[dict[str, Any]] | None = None
    usage: str | None = None
    pending_items: list[dict[str, Any]] | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0
    # 「有内容可用但不完整」（票 #85）：成功收尾时来自 `done.partial`（章节占位），
    # 失败收尾时来自「已生成正文已落成一版部分完成版本」。与终态同一条语句落库。
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
    """事件成功后任务应处于的状态（三态只有两个终态；抢占与回排队之后仍是 `running`）。"""
    if event is JobEvent.CLAIM or event is JobEvent.REQUEUE:
        return STATUS_RUNNING
    return STATUS_COMPLETED if event is JobEvent.SUCCEED else STATUS_FAILED


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
        # 抢占只针对**一条**具体任务：批量抢占没有意义（每个执行者只该拿走它要跑的那一条）。
        if not _scope_has_job(scope) or lease_grant is None:
            raise ValueError("抢占必须给单条任务与本轮执行权（谁领的 / 凭证 / 到期时间）")
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
    elif event is JobEvent.REQUEUE or event is JobEvent.EXPIRE:
        # 恢复器只接一条过期租约：没有「批量接手」这回事，且**必须**带旧凭证——
        # 不带凭证的恢复等于按 id 盲写，那正是 #82 挡掉的那条路径。
        if not _scope_has_job(scope) or lease_fence is None or not lease_fence.token:
            raise ValueError(
                "恢复事件必须给单条任务与扫描时读到的旧租约凭证（凭证 / 到期时间）"
            )
    else:  # pragma: no cover - StrEnum 已穷尽
        raise ValueError(f"未知的转换事件：{event}")


@dataclass(frozen=True, slots=True)
class _WriteResult:
    """一次条件写的**原始结果**：行数 + 读回的事实。

    0 行时在**同一事务**里回读那一行（单条任务），调用方据此知道「别人已经把这一行
    收成了什么」，而不是猜。
    """

    rows: int
    observed_status: str = ""
    observed_code: str = ""
    observed_message: str = ""


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
            draft_content=payload.draft_content if payload else "",
            completed_sections=list(payload.completed_sections or []) if payload else [],
            usage=payload.usage if payload else None,
            pending_items=payload.pending_items if payload else None,
            prompt_tokens=payload.prompt_tokens if payload else 0,
            completion_tokens=payload.completion_tokens if payload else 0,
            partial=bool(payload.partial) if payload else False,
            lease_token=lease_token,
            now_text=now_text,
        )
    elif event is JobEvent.FAIL:
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
            partial=bool(payload.partial) if payload else False,
            # 中止类失败允许「补一次载荷」：状态早已被取消/顶替路径落成终态，
            # 但用量与半截正文还没记下来。条件在仓储里写死（见 `fail` 的注释）。
            abort_code=error_code if error_code in ABORT_CODES else "",
            lease_token=lease_token,
            now_text=now_text,
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
    elif event is JobEvent.REQUEUE:
        fence = lease_fence or LeaseFence(token="", expire_at="")
        # 失败退避（#88）：回排队**不等于立刻能领**。该等多久由唯一映射表算
        # （`services/job_backoff`，按错误码值查），写进 `next_run_at` 与清租约、
        # 更新 `updated_at` 放在**同一条 UPDATE**里——不存在「先回排队、再补退避时间」
        # 那种能被抢跑的中间态，也不存在收尾之外的第二条写路径。
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
    else:
        rows = repo.supersede_active(
            owner_id,
            solution_id=scope.solution_id,
            kind=str(scope.kind or ""),
            error_code=error_code or CODE_SUPERSEDED,
            error_message=error_message or "被新任务取代",
        )

    if rows or not _scope_has_job(scope):
        return _WriteResult(rows=rows)
    job = repo.get(owner_id, int(scope.job_id or 0))
    if job is None:
        return _WriteResult(rows=rows)
    return _WriteResult(
        rows=rows,
        observed_status=job.status,
        observed_code=job.error_code or "",
        observed_message=job.error_message or "",
    )


# ----------------------------------------------------------------------------
# 执行账本（#80）：跟着这次转换记一行
# ----------------------------------------------------------------------------

# 脱敏摘要的长度上限：账本是长期留存的审计数据，一行足够定位问题，不必抄全文。
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
    """把错误文案压成账本能长期存的样子：单行、截断、抹掉疑似凭据。

    三步都要：换行压成空格（否则一行账本在日志页/导出里会撑成好几行）、超长截断
    （上游报错可能带整段响应体）、凭据片段替换（`sk-...` 之类原样落库等于把密钥
    抄进一张不会被清理的表里）。它是**纯函数**，所以可以直接对着一串构造输入验证。
    """
    text = re.sub(r"\s+", " ", str(message or "")).strip()
    for pattern, replacement in _SUMMARY_SECRETS:
        text = pattern.sub(replacement, text)
    if len(text) > _MAX_SUMMARY_CHARS:
        text = text[: _MAX_SUMMARY_CHARS - 1] + "…"
    return text


def _attempt_outcome(event: JobEvent, error_code: str) -> str | None:
    """这次转换该记成哪种结论；`None` = **不是执行结论，不记账本**。

    只有成功收尾、失败收尾、以及恢复器接手的两种超时去向是结论（超时是「这一轮没
    跑成」的结论，码是 `LEASE_EXPIRED`，见 #83）。取消 / 顶替两条事件都只是「请求
    停止」：前者由被请求的那条协程收尾时经 `FAIL` 记成 `cancelled`（一次执行只留
    一行），后者按票面口径**根本不记账**（被取代不是这次执行的结局）。
    """
    if event is JobEvent.SUCCEED:
        return OUTCOME_COMPLETED
    if event is JobEvent.REQUEUE or event is JobEvent.EXPIRE:
        return OUTCOME_FAILED
    if event is JobEvent.FAIL:
        if error_code == CODE_SUPERSEDED:
            return None
        if error_code == CODE_CANCELLED:
            return OUTCOME_CANCELLED
        return OUTCOME_FAILED
    return None


def _attempt_targets(scope: JobScope, write: _WriteResult) -> tuple[int, ...]:
    """这次转换要给哪几条任务记账（只有单条任务的转换记账）。"""
    if write.rows <= 0 or scope.job_id is None:
        return ()
    return (int(scope.job_id),)


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
    但**不许因此把一个真实成功的生成判死**。同一个事务里如果让异常直接飞出去，
    整笔事务（含那条刚写好的终态）会一起回滚——任务卡回 `running` 等回收，代价比
    少一行账大得多。所以账本这一笔套在 `begin_nested()`（Postgres 的 SAVEPOINT）里：
    写坏只回滚到保存点，终态照常提交。这与 `_write_document` 吞异常的口径同源。

    ## 记什么

    结论（`completed` / `failed` / `cancelled`）、错误分类、脱敏摘要、两列 token 与耗时
    全部来自本次收尾的载荷——**不在事后从任务行回读**：任务行只保留最近一次错误，
    账本要的是「这一次」的事实。
    """
    outcome = _attempt_outcome(event, error_code)
    if outcome is None or write.rows <= 0:
        return
    targets = _attempt_targets(scope, write)
    if not targets:
        return

    if outcome == OUTCOME_COMPLETED:
        ledger_code = ""
        summary = ""
    else:
        # 与 `_write` 的兜底口径一致：恢复类的两种事件没有明确原因时记成
        # `LEASE_EXPIRED`（它们的语义就是这个），其余仍兜底成内部错误。
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

    **抢占 / 回排队 / 判超时不结算内存**：前两者改的是租约（抢占时还没有运行时），
    第三种面对的是**别的进程**（或者已经消失）里的执行者——本进程的 `_RUNTIMES`
    里不该为它留一份状态，更不该顺手把某个运行时的 `status` 改掉（那会掩盖它其实
    还没停的事实；它能不能写结果由 `lease_token` 判，不由内存状态判）。
    """
    if (
        result.event is JobEvent.CLAIM
        or result.event is JobEvent.REQUEUE
        or result.event is JobEvent.EXPIRE
    ):
        return
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
    lease_grant: LeaseGrant | None = None,
    lease_token: str = "",
    lease_fence: LeaseFence | None = None,
    now_text: str = "",
) -> TransitionResult:
    """**唯一的状态转换动作**：事件 + 期望条件 → 结果（以影响行数为唯一判据）。

    Args:
        event: 想做什么（抢占 / 成功收尾 / 失败收尾 / 取消 / 顶替 / 回排队 / 判超时）。
        scope: 期望条件（哪一条 / 哪一批，前置状态恒为 `running`）。
        runtime: 单条任务的内存运行时。给了它，成功时同步结算 `status` / 错误码。
        payload: 收尾载荷（`SUCCEED` / `FAIL` 必填）。
        error_code: `FAIL` / `CANCEL` / `SUPERSEDE` / `REQUEUE` / `EXPIRE` 的错误码
            （后三者留空时取默认值）。`REQUEUE` 还用它查**退避时长**（`job_backoff`
            的唯一映射表）——所以回排队这类转换的码值不是装饰，它决定「等多久」。
        error_message: 落进 `error_message`（以及账本摘要）的文案。
        session: 调用方自己持有事务时传入（删方案 / 删账号 / 登记顶替要和别的写同生共死）。
            **传入时本函数不结算内存**：调用方必须在事务提交之后调
            `settle_after_commit(result, scope)`，否则会留下「协程停了、库里还在跑」的错位。
        lease_grant: `CLAIM` 必填：这一轮的执行权（谁领的 / 凭证 / 到期时间 / 判据时刻）。
        lease_token: 收尾（`SUCCEED` / `FAIL`）时带上**本轮凭证**：仓储据此叠「凭证仍是
            本轮 + 没到期」两条判据，改到 0 行就是「我已经没资格写了」。
        lease_fence: `REQUEUE` / `EXPIRE` 必填：**恢复器扫描时读到的旧租约凭证**
            （凭证 + 到期时间）。两列一起进 WHERE：凭证被换掉、或到期时间被续租推后，
            都改到 0 行（#82 判权规则，不由本模块另立一套）。
        now_text: 判据用的「现在」（留空取 `utc_now_text()`）。注入它是为了让用例能在
            毫秒内演示「租约 120 秒、任务跑十几分钟」这类时间跨度；生产不传。

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
        # 账本与状态更新同事务：调用方持事务时账本也留在它的事务里，要么一起成、
        # 要么账本那一笔（且只有那一笔）被保存点回滚。
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
        if event is JobEvent.CLAIM or event is JobEvent.REQUEUE:
            # 抢占与回排队之后任务仍在执行中（三态下「排队中」也读作 running）：
            # 这两条事件改的是租约三列，不是 status。
            return TransitionResult(event=event, rows=rows, status=STATUS_RUNNING)
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
    if event is JobEvent.REQUEUE or event is JobEvent.EXPIRE:
        return CODE_LEASE_EXPIRED
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
