"""单次调用的自愈：超时与有限重试（ADR-016）。

本文件守的是三件事，都是「将来改坏也不容易被发现」的那类：

1. **重试只在首字之前发生**——一旦已经向调用方交付过增量就绝不再来，否则前端会
   收到「半截 + 一份完整」的重复内容（T0 时代并发订阅踩过同款的坑）。
2. **该重试的才重试**——传输层抖动值得重来；我们自己判定的失败（预算超顶、超过墙钟
   硬顶）与内容性质的失败（输出被截断）重来一万次结果也一样。
3. **失败也要记成本**——它占用了真实的时间与一次请求，漏记会让回执系统性偏乐观。

判定用白名单而非黑名单，因此新增异常类型默认是「不重试」——写测试时请顺应这个方向。
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import pytest
from langchain_core.messages import AIMessageChunk, BaseMessage, HumanMessage

from backend.core.config import get_settings
from backend.core.errors import AppError, ErrorCode
from backend.services.llm_meter import RunMeter, use_meter
from backend.services.stream_guard import OutputTruncatedError
from backend.services.stream_reply import (
    CallTimeoutError,
    _is_retryable,
    astream_text,
)
from backend.services.token_budget import BudgetExceededError
from fakes import STREAM_ERROR

_MESSAGES: list[BaseMessage] = [HumanMessage(content="写一段话")]


def _use_provider(*, api_key: str = "test-key", **overrides: Any) -> str:
    """建一条配置并设为「使用中」，返回它的 id。

    ⚠️ ADR-051 之后**没有 env 兜底了**：`.env` 里不再有供应商 / 地址 / 型号，
    `get_llm()` 在没有配置时直接抛「还没有任何模型配置」。所以凡是要真构造实例的
    用例都得先把那条配置建出来——这不是用例啰嗦，而是真实前提。
    """
    import json
    import uuid

    from backend.core import runtime_config as rc
    from backend.core import crypto
    from backend.core.migrations import run_migrations
    from backend.repositories.runtime_config_repo import (
        ModelProviderRepository,
        SystemConfigRepository,
    )

    run_migrations()
    provider_id = "cfg_" + uuid.uuid4().hex[:10]
    fields: dict[str, Any] = {
        "name": "用例配置",
        "protocol": "ark",
        "value_token": crypto.encrypt_value(api_key),
        "default_model": "doubao-test",
        "max_tokens": 4096,
    }
    fields.update(overrides)
    ModelProviderRepository.create(provider_id=provider_id, **fields)
    SystemConfigRepository.upsert_many(
        {rc.ACTIVE_PROVIDER_KEY: json.dumps(provider_id)}, updated_by=None
    )
    rc.invalidate()
    return provider_id


# ---------------------------------------------------------------------------
# 三个假定 differing 时点的假模型
# ---------------------------------------------------------------------------


class _FlakyLLM:
    """前 `fail_times` 次调用**一进流就抛**，之后正常流完。

    记录真实尝试次数——重试合不合预期全看这个数字。

    Args:
        pieces: 正常时吐出的文本块。
        fail_times: 前几次调用失败。
        error: 抛出的异常，默认用 `STREAM_ERROR`（`ConnectionError`，属于可重试那一类）。
    """

    def __init__(
        self,
        pieces: list[str] | None = None,
        *,
        fail_times: int = 0,
        error: Exception | None = None,
    ) -> None:
        self._pieces = list(pieces or ["你好", "世界"])
        self._fail_times = fail_times
        self._error = error or STREAM_ERROR
        self.calls = 0

    async def astream(self, messages: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._error
        for piece in self._pieces:
            await asyncio.sleep(0)
            yield AIMessageChunk(content=piece)


class _BreaksAfterLLM:
    """先吐完 `pieces` 再抛——模拟「生成中途断流」。"""

    def __init__(self, pieces: list[str], *, error: Exception | None = None) -> None:
        self._pieces = list(pieces)
        self._error = error or STREAM_ERROR
        self.calls = 0

    async def astream(self, messages: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
        self.calls += 1
        for piece in self._pieces:
            await asyncio.sleep(0)
            yield AIMessageChunk(content=piece)
        raise self._error


class _SlowLLM:
    """每块之间睡 `delay` 秒——用来触发墙钟硬顶。"""

    def __init__(self, *, delay: float = 0.2, pieces: list[str] | None = None) -> None:
        self._delay = delay
        self._pieces = list(pieces or ["慢", "吞", "吞"])
        self.calls = 0

    async def astream(self, messages: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
        self.calls += 1
        for piece in self._pieces:
            await asyncio.sleep(self._delay)
            yield AIMessageChunk(content=piece)


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch):
    """每个用例都从一套确定的配置出发，避免 `get_settings()` 的 lru_cache 串味。

    `LLM_RETRY_DELAY` 固定为 0：重试该不该发生，与等待多久无关，测试不该为此变慢。
    """
    monkeypatch.setenv("LLM_MAX_RETRIES", "1")
    monkeypatch.setenv("LLM_RETRY_DELAY", "0")
    monkeypatch.setenv("LLM_TOTAL_TIMEOUT", "0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _run(meter: RunMeter | None, llm: Any, messages: list[BaseMessage] | None = None) -> str:
    """在（可选的）计量器上下文里跑完 `astream_text`，返回拼接文本。"""

    async def run() -> str:
        return "".join([text async for text in astream_text(llm, messages or _MESSAGES)])

    if meter is None:
        return asyncio.run(run())
    with use_meter(meter):
        return asyncio.run(run())


# ---------------------------------------------------------------------------
# 1. 重试窗口：只在首字之前
# ---------------------------------------------------------------------------


def test_retries_when_failure_happens_before_first_chunk() -> None:
    """首字之前失败 → 整次重来，调用方对此毫无感知（一帧都还没出去）。"""
    llm = _FlakyLLM(fail_times=1)
    meter = RunMeter(kind="prompts")

    assert _run(meter, llm) == "你好世界"
    assert llm.calls == 2

    usage = meter.usage()
    assert usage["total_calls"] == 1
    # 废弃的那一次留下痕迹：不求这样用户才知道「为什么比平时慢」
    assert usage["failed_calls"] == 1
    assert usage["steps"][0]["failed_calls"] == 1


def test_mid_stream_failure_is_not_retried() -> None:
    """已经交付过增量就绝不重来——那些字符撤不回来，重来会产出重复内容。

    这是本批改动里最重要的一条守卫：它一旦退化，症状不是报错而是「文档莫名其妙重复」，
    很难归因到这里。
    """
    llm = _BreaksAfterLLM(["前半段", "后半段"])
    meter = RunMeter(kind="prompts")

    with pytest.raises(type(STREAM_ERROR)):
        _run(meter, llm)

    # 关键断言：只尝试了一次，第二次永远不会发生
    assert llm.calls == 1
    assert meter.usage()["failed_calls"] == 1
    assert meter.usage()["total_calls"] == 0


def test_retry_disabled_means_one_attempt(monkeypatch) -> None:
    """`LLM_MAX_RETRIES=0` 时不再重试：这个开关是关得掉的。"""
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    get_settings.cache_clear()

    llm = _FlakyLLM(fail_times=1)
    with pytest.raises(type(STREAM_ERROR)):
        _run(None, llm)
    assert llm.calls == 1


def test_retries_stop_at_the_configured_limit(monkeypatch) -> None:
    """重试次数用完就抛出原异常，不会无限重试。"""
    monkeypatch.setenv("LLM_MAX_RETRIES", "2")
    get_settings.cache_clear()

    llm = _FlakyLLM(fail_times=99)
    meter = RunMeter(kind="prompts")
    with pytest.raises(type(STREAM_ERROR)):
        _run(meter, llm)

    # 1 次首发 + 2 次重试
    assert llm.calls == 3
    # 三次都没成，账上一次成功调用都没有
    assert meter.usage()["total_calls"] == 0
    assert meter.usage()["failed_calls"] == 3


# ---------------------------------------------------------------------------
# 2. 该重试的才重试
# ---------------------------------------------------------------------------


def test_is_retryable_covers_transport_errors() -> None:
    """传输层抖动才重试；判据是白名单，未知异常默认不重试。"""

    class _Opaque(Exception):
        pass

    class _ServerBoom(Exception):
        status_code = 503

    class _ClientBoom(Exception):
        status_code = 429

    assert _is_retryable(ConnectionError("断连"))
    assert _is_retryable(TimeoutError())
    assert _is_retryable(_ServerBoom())
    # 4xx 一律不重试：参数不合法、限额、都不是「等一会儿就好」的问题
    assert not _is_retryable(_ClientBoom())
    # 白名单之外的一律保守处理
    assert not _is_retryable(_Opaque())


def test_our_own_failures_are_never_retried() -> None:
    """我们自己判定的失败，重试一万次结果也一样，纯粹是让用户多等一遍。"""
    assert not _is_retryable(AppError(ErrorCode.LLM_UPSTREAM_ERROR, "上游故障"))
    assert not _is_retryable(AppError(ErrorCode.INVALID_REQUEST, "预算超顶"))
    assert not _is_retryable(OutputTruncatedError())


def test_budget_exceeded_never_reaches_the_model() -> None:
    """预算超顶时不重试、也不发请求——这是对账最省的一条路径。"""
    llm = _FlakyLLM()
    huge = [HumanMessage(content="測" * 500_000)]

    with pytest.raises(BudgetExceededError):
        _run(None, llm, huge)
    assert llm.calls == 0


def test_truncated_output_is_not_retried() -> None:
    """输出被截断是内容性质的失败：重来只会拿到同样被截断的内容。"""

    class _TruncatedLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def astream(self, messages: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
            self.calls += 1
            await asyncio.sleep(0)
            yield AIMessageChunk(
                content="半句话",
                response_metadata={"finish_reason": "length"},
            )

    llm = _TruncatedLLM()
    with pytest.raises(OutputTruncatedError):
        _run(None, llm)
    assert llm.calls == 1


# ---------------------------------------------------------------------------
# 3. 墙钟硬顶
# ---------------------------------------------------------------------------


def test_total_timeout_cancels_the_call(monkeypatch) -> None:
    """慢慢吐字的模型逃得过帧间隔超时，逃不过墙钟硬顶。"""
    monkeypatch.setenv("LLM_TOTAL_TIMEOUT", "0.1")
    get_settings.cache_clear()

    llm = _SlowLLM(delay=0.05)
    meter = RunMeter(kind="prompts")
    with pytest.raises(CallTimeoutError):
        _run(meter, llm)

    # 硬顶触发说明这次调用太慢，重来一次只会同样慢——所以不重试
    assert llm.calls == 1
    assert meter.usage()["failed_calls"] == 1
    # 失败也占用墙钟，这段时间必须留在账上
    assert meter.usage()["steps"][0]["duration_ms"] >= 100


def test_total_timeout_disabled_when_zero() -> None:
    """`LLM_TOTAL_TIMEOUT=0` 表示不设硬顶（留给「就想让它慢慢跑」的场景）。"""
    llm = _SlowLLM(delay=0.01)

    assert _run(None, llm) == "慢吞吞"


# ---------------------------------------------------------------------------
# 4. 三个适配器的接线
# ---------------------------------------------------------------------------


def test_factories_disable_sdk_retries_and_pass_timeout(monkeypatch) -> None:
    """SDK 自带的重试必须显式关掉，否则与本层重试叠成 `2 × N` 次。

    顺带确认超时真的传到了各个适配器——三个的参数名各不相同
    （`timeout` / `request_timeout` / `default_request_timeout`），正是最容易漏的地方。
    """
    from backend.services.llm_factory import get_llm

    monkeypatch.setenv("ARK_API_KEY", "test-ark-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("LLM_TIMEOUT", "45")
    get_settings.cache_clear()

    _use_provider(protocol="ark", api_key="test-ark-key")
    ark = get_llm()
    assert ark.timeout == 45

    _use_provider(protocol="openai_compatible", api_key="test-openai-key")
    openai_llm = get_llm()
    assert openai_llm.request_timeout == 45
    assert openai_llm.max_retries == 0

    _use_provider(protocol="anthropic", api_key="test-anthropic-key")
    anthropic_llm = get_llm()
    assert anthropic_llm.default_request_timeout == 45
    assert anthropic_llm.max_retries == 0


def test_timeout_zero_falls_back_to_sdk_default(monkeypatch) -> None:
    """`LLM_TIMEOUT=0` 时不传该参数，沿用 SDK 自己的默认值。"""

    from backend.services.llm_factory import get_llm

    monkeypatch.setenv("ARK_API_KEY", "test-ark-key")
    monkeypatch.setenv("LLM_TIMEOUT", "0")
    get_settings.cache_clear()
    _use_provider(protocol="ark")

    assert get_llm().timeout is None


def test_max_tokens_override_reaches_the_ark_adapter(monkeypatch) -> None:
    """`get_llm(max_tokens=...)` 在方舟下必须真的生效。

    方舟分支曾经把 `**kwargs` 整个丢掉、硬写配置里的 max_tokens，于是
    「按调用调小输出窗口」在本项目**默认供应商**上是条死路——不报错、不警告，
    只是悄悄用了配置值。审核要设小窗口就指着这条路。

    ⚠️ 只能靠**独立实例**生效：`VolcanoArkChatModel._astream` 读的是
    `self.max_tokens`，`astream(..., max_tokens=...)` 不报错也不生效。
    """
    from backend.services.llm_factory import get_llm

    monkeypatch.setenv("ARK_API_KEY", "test-ark-key")
    get_settings.cache_clear()
    _use_provider(protocol="ark", max_tokens=131072)

    assert get_llm().max_tokens == 131072
    assert get_llm(max_tokens=2048).max_tokens == 2048
    # 不传时仍取配置值，别把默认路径一起改掉
    assert get_llm().max_tokens == 131072
