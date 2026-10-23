"""事前 token 预算（`core/model_registry.py` + `services/token_budget.py` + 调用闸门）。

事后计量只能回答「刚才花了多少」，钱已经付了；本文件守的是**发出之前**那一道：

- 物理顶来自模型注册表（按 provider + model 两级登记），业务顶来自配置，**取小者**。
- 估算按字符启发式（不引 tiktoken），误差方向刻意偏高估。
- 超顶时抛 `BudgetExceededError`，**这次调用根本不发出**；错误码复用 `INVALID_REQUEST`。
- 逼近上限时只记一条告警（随 `run_summary` 回传），不拦截。

全部离线，不触网。
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import pytest
from langchain_core.messages import AIMessageChunk, BaseMessage, HumanMessage, SystemMessage

from backend.core.config import get_settings
from backend.core.errors import ErrorCode
from backend.core.model_registry import resolve_limits, input_ceiling
from backend.services.llm_meter import RunMeter, use_meter
from backend.services.stream_reply import astream_text, iter_reply_events
from backend.services.token_budget import (
    BudgetExceededError,
    estimate_messages_tokens,
    estimate_tokens,
    enforce,
    evaluate,
    step_cap,
)


def _apply_env(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    """改环境变量并让配置单例重新读（`get_settings` 是 lru_cache，不清就还是旧值）。"""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


def _ascii(n: int) -> str:
    """构造 n 个 ASCII 字符（估算口径：3.5 字符 ≈ 1 token）。"""
    return "x" * n


# ---------------------------------------------------------------------------
# 模型注册表
# ---------------------------------------------------------------------------


def test_known_model_beats_provider_default() -> None:
    """型号在册时用登记值，不用 provider 默认值。"""
    limits = resolve_limits("ark", "doubao-seed-2-1-turbo-260628")
    assert limits.context_window == 256_000
    # 同供应商下未登记的型号应退回 provider 默认值（比登记值小）
    assert resolve_limits("ark", "some-unlisted-model").context_window != limits.context_window


def test_unknown_provider_falls_back_to_conservative_default() -> None:
    """供应商也没登记时用全局兜底，且它是三者里最保守的（不因查不到就放开）。"""
    fallback = resolve_limits("who-knows", "whatever")
    assert fallback.context_window == 32_000
    assert fallback.context_window <= resolve_limits("ark", "unlisted").context_window


def test_input_ceiling_reserves_room_for_the_reply() -> None:
    """可输入额度 = 窗口 − 预留输出：把整个窗口当输入额度会让回复写到一半被截断。"""
    limits = resolve_limits("ark", "doubao-seed-2-1-turbo-260628")
    assert input_ceiling("ark", "doubao-seed-2-1-turbo-260628") == (
        limits.context_window - limits.max_output
    )


def test_input_ceiling_override_replaces_window_only() -> None:
    """`override` 只换窗口、不动预留输出：厂商改了窗口时不必改代码。"""
    limits = resolve_limits("ark", "doubao-seed-2-1-turbo-260628")
    assert input_ceiling("ark", "doubao-seed-2-1-turbo-260628", override=50_000) == (
        50_000 - limits.max_output
    )
    # 0 / 负数表示「不覆盖」
    assert input_ceiling("ark", "doubao-seed-2-1-turbo-260628", override=0) == (
        limits.context_window - limits.max_output
    )


def test_input_ceiling_never_collapses_below_floor() -> None:
    """窗口配得比预留输出还小时，额度退到下界而不是变成 0（0 会让一切都被拒）。"""
    assert input_ceiling("ark", "unlisted", override=100) == 1_000


# ---------------------------------------------------------------------------
# 估算
# ---------------------------------------------------------------------------


def test_estimate_tokens_empty_and_scaling() -> None:
    """空文本为 0；ASCII 按 3.5 字符 1 token 线性增长。"""
    assert estimate_tokens("") == 0
    assert estimate_tokens(_ascii(350)) == 100
    assert estimate_tokens(_ascii(700)) == 200


def test_estimate_tokens_treats_cjk_as_denser() -> None:
    """中日韩文字的 token 密度远高于拉丁字母：同样字数，估值必须更大。"""
    cjk = estimate_tokens("产" * 100)
    ascii_same_length = estimate_tokens(_ascii(100))
    assert cjk == 80  # 100 × 0.8
    assert cjk > ascii_same_length


def test_estimate_messages_tokens_adds_per_message_overhead() -> None:
    """每条消息另计结构开销（角色名与分隔符不计入正文字符数）。"""
    single = estimate_messages_tokens([HumanMessage(content=_ascii(350))])
    assert single == 100 + 4

    double = estimate_messages_tokens(
        [SystemMessage(content=_ascii(350)), HumanMessage(content=_ascii(350))]
    )
    assert double == single * 2
    assert estimate_messages_tokens([]) == 0


def test_estimate_messages_tokens_flattens_structured_content() -> None:
    """结构化 content（Anthropic 风格列表）与纯字符串估算一致：走同一条归一化。"""
    plain = estimate_messages_tokens([HumanMessage(content=_ascii(350))])
    structured = estimate_messages_tokens(
        [HumanMessage(content=[{"type": "text", "text": _ascii(350)}])]
    )
    assert structured == plain


# ---------------------------------------------------------------------------
# 业务顶
# ---------------------------------------------------------------------------


def test_step_cap_falls_back_to_default(monkeypatch) -> None:
    """未配置任何逐步上限时统一用默认值。"""
    _apply_env(monkeypatch, LLM_BUDGET_DEFAULT_CAP="12345", LLM_BUDGET_CAPS="")
    assert step_cap("prd:writing") == 12_345
    assert step_cap("") == 12_345


def test_step_cap_prefers_exact_step_then_kind(monkeypatch) -> None:
    """完整 step 优先，其次任务类型（一条 `prd=9000` 管住整条流水线）。"""
    _apply_env(
        monkeypatch,
        LLM_BUDGET_DEFAULT_CAP="100",
        LLM_BUDGET_CAPS="prd=9000,prd:writing=5000,api_docs=7000",
    )
    assert step_cap("prd:writing") == 5_000
    assert step_cap("prd:reviewing") == 9_000  # 未单列的阶段吃类型级上限
    assert step_cap("api_docs") == 7_000
    assert step_cap("prompts") == 100  # 都没配 → 默认


def test_step_cap_ignores_malformed_entries(monkeypatch) -> None:
    """配置项写错不该让服务起不来：非法项静默丢弃，合法项照常生效。"""
    _apply_env(
        monkeypatch,
        LLM_BUDGET_DEFAULT_CAP="100",
        LLM_BUDGET_CAPS="prd:writing=5000,broken,=1,zero=0,negative=-5,api_docs=abc",
    )
    assert step_cap("prd:writing") == 5_000
    assert step_cap("api_docs") == 100
    assert step_cap("zero") == 100


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------


def test_budget_is_the_smaller_of_two_ceilings(monkeypatch) -> None:
    """有效预算 = min(物理顶, 业务顶)：任何一层收紧都立刻生效。"""
    # 业务顶远大于物理顶 → 物理顶说话
    _apply_env(monkeypatch, LLM_BUDGET_DEFAULT_CAP="900000")
    physical_wins = evaluate([HumanMessage(content="hi")], provider="ark", model="unlisted-model")
    assert physical_wins.budget == physical_wins.physical_ceiling
    assert physical_wins.business_cap == 900_000

    # 业务顶更小 → 业务顶说话（「让用户早点收束」就是这个意思）
    _apply_env(monkeypatch, LLM_BUDGET_DEFAULT_CAP="1000")
    business_wins = evaluate([HumanMessage(content="hi")], provider="ark", model="unlisted-model")
    assert business_wins.budget == 1_000
    assert business_wins.physical_ceiling > 1_000


def test_evaluate_flags_warn_and_exceed(monkeypatch) -> None:
    """逼近上限 → warned；超过上限 → exceeded（两者互斥，不会既放行又告警）。"""
    _apply_env(monkeypatch, LLM_BUDGET_DEFAULT_CAP="1000", LLM_BUDGET_WARN_RATIO="0.8")
    settings = get_settings()
    assert settings.llm_budget_warn_ratio == 0.8

    # 估值含每条消息 4 token 的结构开销：700/3.5 = 200，+4 = 204
    under = evaluate([HumanMessage(content=_ascii(700))])
    assert under.estimated_tokens == 204
    assert not under.warned and not under.exceeded

    warned = evaluate([HumanMessage(content=_ascii(3_000))])  # 857 + 4 = 861 → 86%
    assert warned.warned and not warned.exceeded
    assert 0.86 <= warned.ratio < 0.87

    over = evaluate([HumanMessage(content=_ascii(4_200))])  # 1200 + 4 = 1204 → 120%
    assert over.exceeded and not over.warned


def test_warn_ratio_configurable(monkeypatch) -> None:
    """告警比例可调：调高即收紧提示时机。"""
    _apply_env(monkeypatch, LLM_BUDGET_DEFAULT_CAP="1000", LLM_BUDGET_WARN_RATIO="0.5")
    assert evaluate([HumanMessage(content=_ascii(2_100))]).warned  # 604 → 60% ≥ 50%
    _apply_env(monkeypatch, LLM_BUDGET_DEFAULT_CAP="1000", LLM_BUDGET_WARN_RATIO="0.9")
    assert not evaluate([HumanMessage(content=_ascii(2_100))]).warned  # 60% < 90%


def test_messages_explain_the_numbers(monkeypatch) -> None:
    """文案必须讲清「超了多少、上限是谁定的」——否则用户无从下手。"""
    _apply_env(monkeypatch, LLM_BUDGET_DEFAULT_CAP="1000")
    decision = evaluate([HumanMessage(content=_ascii(4_200))])
    assert decision.estimated_tokens == 1_204
    rejection = decision.rejection_message()
    assert "1,204" in rejection and "1,000" in rejection
    assert "物理顶" in rejection and "业务顶" in rejection
    assert "1,000" in decision.warning_message()


# ---------------------------------------------------------------------------
# 拦截
# ---------------------------------------------------------------------------


def test_enforce_raises_before_any_call(monkeypatch) -> None:
    """超顶时抛错，且错误码复用 `INVALID_REQUEST`（预算不新增错误码）。"""
    _apply_env(monkeypatch, LLM_BUDGET_DEFAULT_CAP="1000")
    with pytest.raises(BudgetExceededError) as excinfo:
        enforce([HumanMessage(content=_ascii(4_200))])

    error = excinfo.value
    assert error.code is ErrorCode.INVALID_REQUEST
    assert error.status_code == 400
    assert error.decision.exceeded


def _seed_ark_provider() -> None:
    """放一条 ark + doubao 配置并设为使用中。

    ⚠️ 物理顶是按「当前使用中那条配置」算的（`runtime_config.resolve_model_limits`）。
    ADR-051 之后 `.env` 不再提供默认供应商，一条配置都没有时只能落到 32k 兜底——
    于是「默认业务顶给真实提示词留了余量」这条断言会假失败（其实是被 32k 卡住）。
    """
    import json

    from backend.core import runtime_config as rc
    from conftest import model_providers as ModelProviderRepository, system_config as SystemConfigRepository
    provider_id = "cfg_budget001"
    ModelProviderRepository.create(
        provider_id=provider_id,
        name="预算用例",
        protocol="ark",
        value_token="",
        default_model="doubao-seed-2-1-turbo-260628",
        max_tokens=131072,
    )
    SystemConfigRepository.upsert_many(
        {rc.ACTIVE_PROVIDER_KEY: json.dumps(provider_id)}, updated_by=None
    )
    rc.invalidate()


def test_default_cap_leaves_headroom_for_real_prompts() -> None:
    """默认业务顶必须给真实提示词留足余量，否则正常生成会被误拦。

    一次章节撰写的输入是「写作规范 + 字段契约 + 章节模板 + 已定稿全文 + 需求」，
    实测这三份技能资源合计约 22KB 中文（≈1.8 万 token），加上滚雪球式增长的前序
    章节，最坏情况约 4 万 token。默认上限应留出数倍余量——**闸门的目的是拦失控，
    不是拦正常使用**。
    """
    _seed_ark_provider()
    realistic_cjk_chars = 50_000  # ≈4 万 token，接近最坏情况
    decision = evaluate([HumanMessage(content="产" * realistic_cjk_chars)])

    assert decision.estimated_tokens == 40_004  # 4 万 + 每条消息 4 token 的结构开销
    assert not decision.exceeded
    assert not decision.warned
    # 最坏情况也只占掉约三分之一，余量充足——闸门的目的是拦失控，不是拦正常使用
    assert decision.ratio < 0.35


def test_enforce_passes_under_budget(monkeypatch) -> None:
    """未超顶时返回判定结果，不抛错（调用方据此决定是否记告警）。"""
    _apply_env(monkeypatch, LLM_BUDGET_DEFAULT_CAP="1000")
    assert enforce([HumanMessage(content=_ascii(700))]).estimated_tokens == 204


# ---------------------------------------------------------------------------
# 调用闸门（`stream_reply.astream_text` 是唯一检查点）
# ---------------------------------------------------------------------------


class _CountingLLM:
    """记录自己是否被真正调用过的假 LLM。"""

    def __init__(self) -> None:
        self.calls = 0

    async def astream(self, messages: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
        self.calls += 1
        assert False, "不该走到这里：超预算的请求必须在调用模型之前被拒"  # pragma: no cover
        yield AIMessageChunk(content="")  # pragma: no cover


class _RecordingLLM:
    """正常作答并记下调用次数的假 LLM。"""

    def __init__(self) -> None:
        self.calls = 0

    async def astream(self, messages: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
        self.calls += 1
        yield AIMessageChunk(content="正文")


def _run(meter: RunMeter | None, llm: Any, messages: list[BaseMessage]):
    """在可选计量器上下文里跑完 `astream_text`，返回其同步结果。"""

    async def run() -> str:
        if meter is None:
            return "".join([text async for text in astream_text(llm, messages)])
        with use_meter(meter):
            return "".join([text async for text in astream_text(llm, messages)])

    return asyncio.run(run())


async def _collect(llm: Any, messages: list[BaseMessage]) -> str:
    """把 `astream_text` 跑完，返回拼接文本。"""
    return "".join([text async for text in astream_text(llm, messages)])


def test_gate_rejects_before_calling_the_model(monkeypatch) -> None:
    """超预算时请求**根本不会发出**：这是本模块存在的全部意义。"""
    _apply_env(monkeypatch, LLM_BUDGET_DEFAULT_CAP="1000")
    llm = _CountingLLM()

    with pytest.raises(BudgetExceededError):
        asyncio.run(_collect(llm, [HumanMessage(content=_ascii(4_200))]))

    assert llm.calls == 0


def test_gate_allows_normal_requests(monkeypatch) -> None:
    """正常体积的请求不受影响：闸门只在真的失控时才拦。"""
    _apply_env(monkeypatch, LLM_BUDGET_DEFAULT_CAP="1000")
    llm = _RecordingLLM()

    assert _run(None, llm, [HumanMessage(content=_ascii(700))]) == "正文"
    assert llm.calls == 1


def test_gate_disabled_does_not_intercept(monkeypatch) -> None:
    """`LLM_BUDGET_ENABLED=false` 时只做事后计量，不做任何拦截（排查误拦用）。"""
    _apply_env(monkeypatch, LLM_BUDGET_ENABLED="false", LLM_BUDGET_DEFAULT_CAP="1000")
    llm = _RecordingLLM()

    assert _run(None, llm, [HumanMessage(content=_ascii(4_200))]) == "正文"
    assert llm.calls == 1


def test_gate_uses_meter_identity_for_step(monkeypatch) -> None:
    """有计量器时用它的协议/型号/步骤名：判定与记账必须同源，否则口径会打架。"""
    _apply_env(monkeypatch, LLM_BUDGET_CAPS="prd=5000,api_docs=100", LLM_BUDGET_DEFAULT_CAP="1000")
    meter = RunMeter(kind="prd", protocol="ark", model="doubao-seed-2-1-turbo-260628")
    meter.set_phase("writing")
    llm = _RecordingLLM()

    # prd 类型上限 5000（宽松），同样体积在 api_docs（上限 100）下会被拒
    assert _run(meter, llm, [HumanMessage(content=_ascii(10_500))]) == "正文"

    other = RunMeter(kind="api_docs", protocol="ark", model="doubao-seed-2-1-turbo-260628")
    with pytest.raises(BudgetExceededError):
        _run(other, _CountingLLM(), [HumanMessage(content=_ascii(10_500))])


def test_gate_records_warning_on_the_meter(monkeypatch) -> None:
    """逼近上限时只记告警、不拦截；同一步保留最严重的一条。"""
    _apply_env(monkeypatch, LLM_BUDGET_DEFAULT_CAP="1000", LLM_BUDGET_WARN_RATIO="0.8")
    meter = RunMeter(kind="prd")
    meter.set_phase("writing")
    llm = _RecordingLLM()

    _run(meter, llm, [HumanMessage(content=_ascii(3_000))])  # ≈857 → 85%

    warnings = meter.usage()["warnings"]
    assert len(warnings) == 1
    assert "prd:writing" in warnings[0]
    assert "86%" in warnings[0]
    assert llm.calls == 1  # 告警不阻断


def test_budget_error_reaches_frontend_as_invalid_request(monkeypatch) -> None:
    """流里的 `error` 事件带上 `INVALID_REQUEST`：前端据此知道「重试也没用」。"""
    _apply_env(monkeypatch, LLM_BUDGET_DEFAULT_CAP="1000")
    llm = _CountingLLM()

    async def run() -> list[Any]:
        return [
            event
            async for event in iter_reply_events(
                llm,
                [HumanMessage(content=_ascii(4_200))],
                status_text="正在生成…",
            )
        ]

    events = asyncio.run(run())

    assert [event.type for event in events] == ["status", "error"]
    assert events[-1].code == ErrorCode.INVALID_REQUEST.value
    assert "预算上限" in events[-1].content
    assert llm.calls == 0
