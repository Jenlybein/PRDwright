"""一次生成任务的 token 用量计量。

## 为什么要一个「环境计量器」而不是给函数加参数

用量是在流水线最深处产生的（`stream_reply.astream_text` 消费模型分块），而「这是哪一次
生成、第几步」只有最外层（`job_service`）知道。若靠参数一路传下去，`document_service`、
`prd_section_writer`、`conversation_service` 的每个流式入口签名都要改——而 v3 的设计约定是
**业务层调用方式不变**（见 `docs/architecture/v3/ARCHITECTURE.md` 第 4.2 节：观测层挂在
模型调用之外，业务层无感）。

因此计量器用 `ContextVar` 挂成**环境值**：`job_service.run_job` 在任务开头 `use_meter(...)`，
深处只需要 `current_meter()`；没有任务上下文时（澄清对话、脚本、测试直接调服务）返回 `None`，
计量自然而然地「无操作」，不需要任何分支开关。

任务边界会复制上下文，所以 `asyncio.create_task` 起的后台任务自动继承同一个计量器与
`request_id`，无需额外接线。

## 步骤名从哪来

`step` 不额外引入一套命名，而是由 **任务类型 + 当前阶段** 推出：任务类型来自 Job，
阶段来自流水线已经在发的 `stage` 事件（`writing` / `reviewing` / `rewriting`）。
于是 `prd:writing`、`prd:reviewing`、`api_docs` 这些标签**零成本**得到——
不需要在 10 个调用点上各写一遍自己的名字，也不会出现「新增调用点忘了打标签」。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from backend.core.contracts import StepUsage
from backend.core.request_context import current_request_id


class RunMeter:
    """一次生成任务的用量累计器（纯内存、无 I/O，方便单测）。

    计时与 token 都只做累加，不判断成功失败：**被截断或失败的调用同样消耗了 token**，
    半途而废的生成恰恰是最需要看见成本的场合。

    Args:
        kind: 任务类型（`prd` / `api_docs` / `prompts` / `optimize`）。
        provider: 供应商名，来自配置。
        model: 模型名，来自配置。
    """

    def __init__(self, kind: str = "", provider: str = "", model: str = "") -> None:
        self.kind = kind
        self.provider = provider
        self.model = model
        # 当前阶段，由 `set_phase` 随 `stage` 事件更新
        self.phase = ""
        # 步骤名 → 用量；dict 保序，汇总时按发生先后输出，读起来与生成过程同序
        self._steps: dict[str, StepUsage] = {}
        # 事前预算的告警：step → (占预算比例, 中文提示)；同一步只留最严重的一条
        self._warnings: dict[str, tuple[float, str]] = {}
        self._total_calls = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        # 失败调用单独计数：重试机制的废弃尝试也得看得见（见 `record_failed`）
        self._failed_calls = 0

    # ------------------------------------------------------------------
    # 供流水线侧写入
    # ------------------------------------------------------------------

    def set_phase(self, phase: str) -> None:
        """更新当前阶段（由 `job_service` 转发 `stage` 事件时调用）。"""
        self.phase = phase

    def current_step(self) -> str:
        """推导当前步骤名：有阶段就用 `类型:阶段`，否则用任务类型本身。

        例：`prd:writing` / `prd:reviewing` / `api_docs` / `prompts`。
        """
        return f"{self.kind}:{self.phase}" if self.phase else self.kind

    def note_warning(self, step: str, ratio: float, message: str) -> None:
        """记一条事前预算的告警；同一步只保留**最严重**（比值最大）的那条。

        一条生成里同一步会调用很多次（7 章 × 撰写/审核/改写），逐步累加会把回执
        撑成一堆重复行；留下最逼近上限的那一次，才是对用户有用的信息。
        「超顶」不在这里记：那时调用根本没发出，会直接转成 `error` 事件。

        Args:
            step: 步骤名。
            ratio: 估算值占预算的比例。
            message: 可直接展示的中文提示。
        """
        previous = self._warnings.get(step)
        if previous is None or ratio > previous[0]:
            self._warnings[step] = (ratio, message)

    def _accumulate(
        self,
        step: str,
        *,
        input_tokens: int,
        output_tokens: int,
        duration_ms: int,
        failed: bool,
    ) -> None:
        """成功与失败共用的累加入口，区别只在「记到哪一列」。

        失败的那次拿不到用量（多半连第一个字节都还没回来），但它**占用了时间与一次调用**，
        两者都要记：否则「回执说 12 秒 3 次调用，实际花了 25 秒」这种偏差无从解释。
        """
        usage = self._steps.get(step)
        if usage is None:
            usage = StepUsage(step=step)
            self._steps[step] = usage

        usage.duration_ms += max(0, duration_ms)
        if failed:
            usage.failed_calls += 1
            self._failed_calls += 1
            return

        usage.calls += 1
        usage.prompt_tokens += input_tokens
        usage.completion_tokens += output_tokens
        self._total_calls += 1
        self._prompt_tokens += input_tokens
        self._completion_tokens += output_tokens

    def record(self, *, input_tokens: int = 0, output_tokens: int = 0, duration_ms: int = 0) -> None:
        """记一次**拿到结果**的模型调用（含「结果被截断」——那也照烧 token）。

        上游没给用量（部分供应商的流式集成默认不回吐）时传 0 即可：这一次调用
        仍然计入 `calls` 与 `duration_ms`，「调了几次、花了多久」不会因此丢失。
        """
        self._accumulate(
            self.current_step(),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=duration_ms,
            failed=False,
        )

    def record_failed(self, *, duration_ms: int = 0) -> None:
        """记一次**失败**的模型调用（网络断开、超时、5xx，以及重试前的废弃尝试）。

        `calls` 保持只统计成功的调用，失败进 `failed_calls`：两个数字分开，
        「总共发了几次请求」与「真正跑完了几次」才对得上。

        Args:
            duration_ms: 这次失败耗掉的时间；失败同样占用墙钟，必须计入。
        """
        self._accumulate(
            self.current_step(),
            input_tokens=0,
            output_tokens=0,
            duration_ms=duration_ms,
            failed=True,
        )

    # ------------------------------------------------------------------
    # 供出口侧读取
    # ------------------------------------------------------------------

    def usage(self) -> dict[str, Any]:
        """汇总出 `RunSummary` 里**属于计量器**的那部分字段。

        `kind` / `status` / `duration_ms` 不在其中：前两者由 Job 决定，总耗时由
        `job_service` 用自己的时钟算（计量器只管模型调用这一段）。
        `request_id` 在这里现读（而不是构造时固定）：后台任务继承的就是请求的上下文。
        """
        return {
            "total_calls": self._total_calls,
            # 失败调用单独给一个数字：它与成功调用都是真金白银的等待，不该混在 total_calls 里
            "failed_calls": self._failed_calls,
            "prompt_tokens": self._prompt_tokens,
            "completion_tokens": self._completion_tokens,
            "steps": [step.model_dump() for step in self._steps.values()],
            # 技能注册表在 v3.1 统一后填充，当前恒为空
            "injected_skills": [],
            "warnings": [message for _, message in self._warnings.values()],
            "provider": self.provider,
            "model": self.model,
            "request_id": current_request_id(),
        }


# 当前任务环境的计量器；无任务上下文时为 None（计量自动变为无操作）
_current_meter: ContextVar[RunMeter | None] = ContextVar("run_meter", default=None)


@contextmanager
def use_meter(meter: RunMeter) -> Iterator[None]:
    """把计量器挂到当前上下文，退出时还原。

    由 `job_service.run_job` 包住整条事件流的消费过程；`finally` 里必须还原，
    否则同一个事件循环上先后跑的任务会读到别人的计量器。
    """
    token = _current_meter.set(meter)
    try:
        yield
    finally:
        _current_meter.reset(token)


def current_meter() -> RunMeter | None:
    """取当前上下文的计量器；没有任务在跑时返回 `None`。"""
    return _current_meter.get()
