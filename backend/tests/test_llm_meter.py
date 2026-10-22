"""用量计量（`services/llm_meter.py` 与 `stream_reply.astream_text`）的契约测试。

计量是「环境值」而不是参数：模型调用发生在流水线最深处，而「这是哪一次生成、第几步」
只有最外层知道。本文件把这条约定的几个关键行为钉死：

- 计量器按「任务类型 + 阶段」自动分步累加（`prd:writing` / `prd:reviewing` …），
  调用点不需要自己打标签，也就不会「新增调用点忘了统计」。
- `usage()` 产出的字段与 `RunSummary` 契约一致，且 `request_id` 是**现读**上下文。
- `astream_text` 是唯一累加点：一次调用记一次，**被截断也照记**（截断同样烧了 token）。
- 没有任务上下文时（`current_meter()` 为 `None`）计量自动退化为无操作，不报错、不需要开关。

全部离线，不触网。
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import pytest
from langchain_core.messages import AIMessageChunk, BaseMessage

from backend.core.contracts import StepUsage
from backend.core.request_context import reset_request_id, set_request_id
from backend.services.llm_meter import RunMeter, current_meter, use_meter
from backend.services.stream_guard import OutputTruncatedError
from backend.services.stream_reply import astream_text


class _FakeLLM:
    """按预设分块作答；末块可带 `usage_metadata` 与 `finish_reason`。"""

    def __init__(
        self,
        pieces: list[str],
        *,
        usage: dict[str, Any] | None = None,
        finish_reason: str | None = None,
    ) -> None:
        self._pieces = list(pieces)
        self._usage = usage
        self._finish_reason = finish_reason

    async def astream(self, messages: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
        last = len(self._pieces) - 1
        for index, piece in enumerate(self._pieces):
            extra: dict[str, Any] = {}
            if index == last:
                if self._usage is not None:
                    extra["usage_metadata"] = self._usage
                if self._finish_reason:
                    extra["response_metadata"] = {"finish_reason": self._finish_reason}
            yield AIMessageChunk(content=piece, **extra)


def _run_text(meter: RunMeter | None, llm: Any) -> str:
    """在一个（可选的）计量器上下文里跑完 `astream_text`，返回拼接文本。"""

    async def run() -> str:
        if meter is None:
            return "".join([text async for text in astream_text(llm, [])])
        with use_meter(meter):
            return "".join([text async for text in astream_text(llm, [])])

    return asyncio.run(run())


def _run_expecting_truncation(meter: RunMeter, llm: Any) -> None:
    """跑一段注定被截断的流，吞掉 `OutputTruncatedError`（计量应在抛错前完成）。"""

    async def run() -> None:
        with use_meter(meter):
            async for _ in astream_text(llm, []):
                pass

    with pytest.raises(OutputTruncatedError):
        asyncio.run(run())


# ---------------------------------------------------------------------------
# 计量器本身
# ---------------------------------------------------------------------------


def test_record_accumulates_per_step_and_total() -> None:
    """按步骤累加：同一步的多次调用合并，跨步骤分别记账，总计等于各步之和。"""
    meter = RunMeter(
        kind="prd",
        protocol="ark",
        model="doubao-seed",
        config_id="cfg_abc123",
        name="方舟-主力",
    )
    meter.set_phase("writing")
    meter.record(input_tokens=100, output_tokens=50, duration_ms=1200)
    meter.record(input_tokens=10, output_tokens=5, duration_ms=300)
    meter.set_phase("reviewing")
    meter.record(input_tokens=200, output_tokens=20, duration_ms=800)

    usage = meter.usage()

    assert usage["total_calls"] == 3
    assert usage["prompt_tokens"] == 310
    assert usage["completion_tokens"] == 75
    # 步骤按发生先后输出，读起来与生成过程同序
    assert [step["step"] for step in usage["steps"]] == ["prd:writing", "prd:reviewing"]
    assert usage["steps"][0] == {
        "step": "prd:writing",
        "calls": 2,
        "failed_calls": 0,
        "prompt_tokens": 110,
        "completion_tokens": 55,
        "duration_ms": 1500,
    }
    assert usage["steps"][1]["calls"] == 1
    assert sum(step["calls"] for step in usage["steps"]) == usage["total_calls"]
    assert sum(step["prompt_tokens"] for step in usage["steps"]) == usage["prompt_tokens"]
    # 供应商身份拆成四个键：协议查限值/聚合账单、config_id 定位到具体一条、
    # name 是给人看的快照、model 是模型。混成一个字符串就分不清「同协议两条配置」。
    assert usage["protocol"] == "ark"
    assert usage["provider_config_id"] == "cfg_abc123"
    assert usage["provider_name"] == "方舟-主力"
    assert usage["model"] == "doubao-seed"


def test_step_name_derives_from_kind_and_phase() -> None:
    """步骤名由「任务类型 + 阶段」推出；无阶段时退化为任务类型，绝不出现空步骤名。"""
    meter = RunMeter(kind="api_docs")
    assert meter.current_step() == "api_docs"
    meter.set_phase("writing")
    assert meter.current_step() == "api_docs:writing"
    # 阶段被清空也退化回任务类型
    meter.set_phase("")
    assert meter.current_step() == "api_docs"


def test_usage_shape_matches_run_summary_fields() -> None:
    """`usage()` 恰好产出属于计量器的那些字段，且 `steps` 项满足 `StepUsage` 契约。"""
    meter = RunMeter(kind="prompts")
    assert set(meter.usage()) == {
        "total_calls",
        "failed_calls",
        "prompt_tokens",
        "completion_tokens",
        "steps",
        "injected_skills",
        "warnings",
        "protocol",
        "provider_config_id",
        "provider_name",
        "model",
        "request_id",
    }
    meter.set_phase("writing")
    meter.record(input_tokens=1, output_tokens=2, duration_ms=3)
    # 不抛异常即字段齐备（多余的键会被 pydantic 忽略，缺键会报错）
    StepUsage(**meter.usage()["steps"][0])


def test_request_id_is_read_live_from_context() -> None:
    """`request_id` 在出口处现读上下文：任务边界继承的是请求的上下文，非构造时快照。"""
    meter = RunMeter(kind="prd")
    assert meter.usage()["request_id"] == ""

    token = set_request_id("req-abc123")
    try:
        assert meter.usage()["request_id"] == "req-abc123"
    finally:
        reset_request_id(token)

    assert meter.usage()["request_id"] == ""


def test_no_meter_outside_task_context() -> None:
    """没有任务上下文时 `current_meter()` 为 `None`（计量自动无操作）。"""
    assert current_meter() is None


def test_note_warning_keeps_worst_per_step() -> None:
    """同一 step 的告警只留最严重的一条：一条生成里同一步会调用十几次，
    全部累加会把回执撑成一堆重复行，留下最逼近上限的那次才有用。"""
    meter = RunMeter(kind="prd")
    assert meter.usage()["warnings"] == []

    meter.note_warning("prd:writing", 0.82, "prd:writing 达 82%")
    meter.note_warning("prd:writing", 0.91, "prd:writing 达 91%")
    # 更轻的一条不覆盖更重的
    meter.note_warning("prd:writing", 0.85, "prd:writing 达 85%")
    meter.note_warning("prd:reviewing", 0.88, "prd:reviewing 达 88%")

    assert meter.usage()["warnings"] == ["prd:writing 达 91%", "prd:reviewing 达 88%"]


def test_use_meter_scopes_and_restores() -> None:
    """`use_meter` 只在 with 块内生效，退出后还原（同事件循环上多任务不会串）。"""
    outer = RunMeter(kind="outer")
    inner = RunMeter(kind="inner")

    with use_meter(outer):
        assert current_meter() is outer
        with use_meter(inner):
            assert current_meter() is inner
        assert current_meter() is outer
    assert current_meter() is None


def test_use_meter_restores_even_on_exception() -> None:
    """异常路径也必须还原，否则后续任务会读到别人的计量器。"""
    with pytest.raises(RuntimeError):
        with use_meter(RunMeter(kind="x")):
            raise RuntimeError("boom")
    assert current_meter() is None


# ---------------------------------------------------------------------------
# `astream_text` 是唯一累加点
# ---------------------------------------------------------------------------


def test_astream_text_records_single_call_with_usage() -> None:
    """一次流式调用记一次，并取末块的 `usage_metadata` 作为 token 用量。"""
    meter = RunMeter(kind="prd")
    meter.set_phase("writing")
    llm = _FakeLLM(
        ["甲", "乙"], usage={"input_tokens": 30, "output_tokens": 7, "total_tokens": 37}
    )

    text = _run_text(meter, llm)

    assert text == "甲乙"
    usage = meter.usage()
    assert usage["total_calls"] == 1
    assert usage["prompt_tokens"] == 30
    assert usage["completion_tokens"] == 7
    assert usage["steps"][0]["step"] == "prd:writing"
    assert usage["steps"][0]["duration_ms"] >= 0


def test_astream_text_records_even_when_truncated() -> None:
    """被截断的调用照样烧了 token：先计量再查截断，抛错不影响已完成的累加。"""
    meter = RunMeter(kind="prd")
    llm = _FakeLLM(
        ["半截"],
        usage={"input_tokens": 5, "output_tokens": 1, "total_tokens": 6},
        finish_reason="length",
    )

    _run_expecting_truncation(meter, llm)

    usage = meter.usage()
    assert usage["total_calls"] == 1
    assert usage["prompt_tokens"] == 5
    assert usage["completion_tokens"] == 1


def test_call_without_usage_is_still_counted() -> None:
    """上游不回吐用量时记零 token，但「调了几次、花了多久」不能丢。"""
    meter = RunMeter(kind="prompts")
    text = _run_text(meter, _FakeLLM(["正文"]))

    assert text == "正文"
    usage = meter.usage()
    assert usage["total_calls"] == 1
    assert usage["prompt_tokens"] == 0
    assert usage["completion_tokens"] == 0
    assert usage["steps"][0]["calls"] == 1


def test_astream_text_without_meter_is_a_no_op() -> None:
    """无计量器上下文时照常产出文本，不报错、不需要分支开关。"""
    llm = _FakeLLM(["正文"], usage={"input_tokens": 3, "output_tokens": 1, "total_tokens": 4})

    assert _run_text(None, llm) == "正文"
