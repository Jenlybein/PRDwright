"""失败退避（票 #88）：可重试失败回排队之前**先等一段时间**。

## 本模块回答什么问题

一条任务失败之后回排队，如果下一个执行者立刻把它领走，连续的上游限流能在几十秒内
把三次机会全部烧光——**不等，等于把「上游暂时忙」误判成「彻底失败」**。所以「回排队」
与「能再被领走」之间需要一个延迟：`generation_jobs.next_run_at`（迁移 `0006`）。

本模块只回答一个问题：**这种失败该等多久**。它提供两样东西：

1. **错误码 → 退避秒数的映射表**（`BACKOFF_SECONDS`），一处定义、不散落在多个 `if` 里；
2. `next_run_at_text()`：把「失败时刻 + 该等多久」折成落库的 ISO 文本。

至于「调不调它、什么时候调」由状态机（`job_state` 的 `REQUEUE` 分支）决定：
写 `next_run_at` 与清租约、记一条失败账本在**同一条条件 UPDATE、同一个事务**里完成，
不存在「退避另开一条写路径」。

## 为什么按**错误码值**分类，而不是文案 / 上游异常细节

错误码是仓库里已经在对外承诺的稳定标识（`core/errors.py` 的 `ErrorCode`，外加
`job_state` 给三个「不是错误」的中止标识）。按中文文案或 `detail` 分支会在文案一改
的当天静默失效——那正是 `test_error_contract` 守着的那条线。所以这张表只认
**code 值**。

## 映射表（唯一一处）

| 类别 | 判据（code 值） | 退避秒数 | 为什么 |
|---|---|---|---|
| 限流 | `INVALID_REQUEST` | **30** | 仓库里 HTTP **429** 的稳定码就是它（`quota_service.check` 明确复用 `INVALID_REQUEST` + 状态码 429，理由写在那个函数里：`ErrorCode` 是对外契约、不新增成员）。撞限流要退得最久，否则重试机会在几十秒内被烧光 |
| 上游 5xx | `LLM_UPSTREAM_ERROR` | **10** | 上游抖动 / 传输层失败：通常几秒到十几秒自愈，等 10 秒比立刻重试更可能撞上恢复 |
| 超时 | `LEASE_EXPIRED` | **5** | 租约到期（执行者进程没了、卡死）是**本票能真实产生的**那一路（恢复器回排队带的就是这个码）。超时的成因往往就在「刚才那段时间上游慢」，5 秒足够让下一次执行错开 |
| 其余 | 任何别的码 / 空串 | **5**（`DEFAULT_BACKOFF_SECONDS`） | 兜底取最短一档：说不出是哪一类时，宁可早一点再试，也不要把一条能跑的任务压住半分钟 |

**表外的码不代表「可重试」**：这张表只回答「要等多久」，不回答「该不该重试」。
「该不该重试」由调用方（恢复器按账本剩余次数）决定——一次 `INVALID_REQUEST`
（参数不合法 / 预算超顶）永远不会走到回排队，所以那条 30 秒只在「429 类失败被
回排队」时生效。

## 时间入口可注入（票 #88 的 AC5）

本模块的 `next_run_at_text(..., now_text=...)` 接受调用方给定的「现在」：状态机把
`transition(now_text=...)` 的那个时刻原样传进来，于是用例能在毫秒内演示 30 / 10 / 5
秒的间隔，不需要 `sleep`。生产不传，取 `core/clock.utc_now_text()`——**时间只有一个
入口**（`core/clock.py`），本模块不自己调 `datetime.now()`。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from backend.core.clock import utc_now_text

# 三类可重试失败的退避秒数（票面 AC1 的三个数字）。名字给读代码的人，数字只写一遍：
# 表里的每一项都引用这些常量，改一处即可全生效。
BACKOFF_RATE_LIMITED_SECONDS = 30.0
BACKOFF_UPSTREAM_SECONDS = 10.0
BACKOFF_TIMEOUT_SECONDS = 5.0

# 表外的码（含空串）：说不出类别时取最短一档，原因是「宁可早一点再试」。
DEFAULT_BACKOFF_SECONDS = BACKOFF_TIMEOUT_SECONDS

# 错误码值 → 退避秒数。**这是全仓唯一一张映射表**：加一类失败只改这里，
# 不要把「是限流就 30、是 5xx 就 10」抄进调用方的 if 里。
BACKOFF_SECONDS: dict[str, float] = {
    # HTTP 429：仓库里 429 的唯一稳定码（见模块 docstring）。
    "INVALID_REQUEST": BACKOFF_RATE_LIMITED_SECONDS,
    # 上游 5xx / 传输抖动（`ErrorCode.LLM_UPSTREAM_ERROR`）。
    "LLM_UPSTREAM_ERROR": BACKOFF_UPSTREAM_SECONDS,
    # 超时（租约到期，`job_state.CODE_LEASE_EXPIRED`）。
    "LEASE_EXPIRED": BACKOFF_TIMEOUT_SECONDS,
}


def seconds_for(error_code: str) -> float:
    """这次失败该等多少秒（表外取值一律落 `DEFAULT_BACKOFF_SECONDS`）。

    Args:
        error_code: 稳定错误码的**值**（不是中文文案、不是 `detail`）。

    Returns:
        退避秒数，恒为正数。
    """
    return BACKOFF_SECONDS.get(str(error_code or ""), DEFAULT_BACKOFF_SECONDS)


def next_run_at_text(error_code: str, *, now_text: str = "") -> str:
    """把「失败时刻 + 该等多久」折成 `next_run_at` 列的 ISO 文本。

    格式与 `core/clock.utc_now_text()` 完全一致（秒级、不带 `Z`）：抢占条件要拿它跟
    「现在」做**字符串比较**，格式一漂就不是字典序了——那类错误不会报错，只会静默
    算错（见 `core/clock.py` 的模块说明）。

    Args:
        error_code: 造成这次失败的错误码值。
        now_text: 判据用的「现在」，留空取 UTC 当前时刻。注入只为用例能演示时间跨度；
            生产路径由 `job_state.transition(now_text=...)` 传入同一个时刻——「判断」
            与「写入」必须看到同一秒。

    Returns:
        `now + seconds_for(error_code)` 的 ISO 文本。
    """
    base = now_text or utc_now_text()
    return (datetime.fromisoformat(base) + timedelta(seconds=seconds_for(error_code))).isoformat()


@dataclass(frozen=True, slots=True)
class Backoff:
    """一次退避的可读形状（错误码 + 等多久 + 到点时刻），供日志 / 报告消费。"""

    error_code: str
    seconds: float
    next_run_at: str


def of(error_code: str, *, now_text: str = "") -> Backoff:
    """按错误码算出这次退避的三件事（映射表仍只有 `BACKOFF_SECONDS` 一份）。"""
    return Backoff(
        error_code=str(error_code or ""),
        seconds=seconds_for(error_code),
        next_run_at=next_run_at_text(error_code, now_text=now_text),
    )


__all__ = [
    "BACKOFF_RATE_LIMITED_SECONDS",
    "BACKOFF_SECONDS",
    "BACKOFF_TIMEOUT_SECONDS",
    "BACKOFF_UPSTREAM_SECONDS",
    "Backoff",
    "DEFAULT_BACKOFF_SECONDS",
    "next_run_at_text",
    "of",
    "seconds_for",
]
