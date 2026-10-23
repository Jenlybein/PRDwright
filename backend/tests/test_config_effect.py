"""配置改动**真的生效**（v4 T3 的核心承诺）。

T3 最容易交付成一个「看起来能用」的功能：接口返回 200、页面显示「已保存」，
但生成用的还是旧参数。它的失败方式不是报错，而是**静默不生效**——所以用例必须
断言「行为变了」，而不是「接口通了」。

这里钉的是五条最关键的链路：

1. **死配置被修好**：章节审核的 `max_tokens` 真的取自 `prd_section_review_max_tokens`。
   修之前它被 `doc_review_max_tokens` 顶掉，改它一点用没有。
2. **LLM 实例按指纹重建**：改配置后拿到的是新实例，同配置则不重建（不白建客户端）。
3. **构造期快照已拆除**：同一个 `PrdSectionWriter` 实例，改并发度后行为跟着变。
4. **任务级冻结**：任务跑到一半改配置，这次任务的回执仍记旧供应商。
5. **自定义供应商的窗口**：填了 `context_window` 就不再被 32k 兜底误判超顶。
"""

from __future__ import annotations

import json

import pytest

from backend.core import runtime_config as rc
from backend.core.config import get_settings
from dbsupport import connect
from conftest import model_providers as ModelProviderRepository, system_config as SystemConfigRepository
from backend.services import document_service, llm_factory


class _FakeLLM:
    """够用的假模型：只用来占位与比较身份，不会被真正调用。"""

    def __init__(self, label: str = "") -> None:
        self.label = label

    def astream(self, messages, **kwargs):  # noqa: ANN001, ANN003
        raise AssertionError("本用例不应真正调用模型")

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
        return self


@pytest.fixture(autouse=True)
def fresh():
    rc.invalidate()
    yield
    rc.invalidate()


def _set(**values) -> None:
    SystemConfigRepository.upsert_many(
        {key: json.dumps(value) for key, value in values.items()}, updated_by=1
    )
    rc.invalidate()


def _clear() -> None:
    SystemConfigRepository.delete_many([key for key in rc.WHITELIST])
    rc.invalidate()


# ---------------------------------------------------------------------------
# 1. 死配置修复
# ---------------------------------------------------------------------------


def test_section_review_llm_uses_its_own_token_limit(monkeypatch):
    """章节审核实例的上限来自 `prd_section_review_max_tokens`。

    ⚠️ 这条钉住一个**曾经真实存在的 bug**：`_get_section_writer` 把整篇审核用的
    `review_llm`（`doc_review_max_tokens`=2048）传给了 writer，于是
    `prd_section_review_max_tokens` 从未被读取——改它没有任何效果。
    """
    _set(prd_section_review_max_tokens=1234)

    seen: list[dict] = []

    def _fake_get_llm(**kwargs):  # noqa: ANN003
        seen.append(kwargs)
        return _FakeLLM(f"mt={kwargs.get('max_tokens')}")

    monkeypatch.setattr(document_service, "get_llm", _fake_get_llm)

    service = document_service.DocumentService()
    instance = service.section_review_llm

    assert seen, "应当创建了一个审核实例"
    assert seen[0]["max_tokens"] == 1234
    assert instance.label == "mt=1234"


def test_whole_and_section_review_limits_are_independent(monkeypatch):
    """整篇审核与单章审核是两个独立配置，各管一处。"""
    _set(doc_review_max_tokens=3000, prd_section_review_max_tokens=900)

    seen: list[int | None] = []

    def _fake_get_llm(**kwargs):  # noqa: ANN003
        seen.append(kwargs.get("max_tokens"))
        return _FakeLLM()

    monkeypatch.setattr(document_service, "get_llm", _fake_get_llm)

    service = document_service.DocumentService()
    service.review_llm
    service.section_review_llm

    assert 3000 in seen, f"整篇审核上限没生效：{seen}"
    assert 900 in seen, f"单章审核上限没生效：{seen}"


# ---------------------------------------------------------------------------
# 2. LLM 实例按指纹重建
# ---------------------------------------------------------------------------


def test_llm_instance_rebuilt_after_config_change(monkeypatch):
    """改配置 → 下一次取 `llm` 是新实例；同配置再取则复用（不白建客户端）。"""
    calls: list[int] = []

    def _fake_get_llm(**kwargs):  # noqa: ANN003
        calls.append(1)
        return _FakeLLM(f"n={len(calls)}")

    monkeypatch.setattr(llm_factory, "get_llm", _fake_get_llm)
    monkeypatch.setattr(document_service, "get_llm", _fake_get_llm)

    service = document_service.DocumentService()
    first = service.llm
    assert service.llm is first, "配置没变时不该重复创建"
    assert len(calls) == 1

    _set(llm_max_retries=3)
    rebuilt = service.llm
    assert rebuilt is not first, "配置变了必须重建，否则仍用旧供应商/旧模型"
    assert len(calls) == 2


def test_writer_rebuilds_llm_when_config_changes(monkeypatch):
    """`PrdSectionWriter` 的 llm 同样按指纹重建。"""
    from backend.services.prd_section_writer import PrdSectionWriter

    calls: list[int] = []

    def _fake_get_llm(**kwargs):  # noqa: ANN003
        calls.append(1)
        return _FakeLLM(f"n={len(calls)}")

    monkeypatch.setattr(
        "backend.services.prd_section_writer.get_llm", _fake_get_llm, raising=False
    )

    writer = PrdSectionWriter(llm=None)
    first = writer.llm
    assert len(calls) == 1

    _set(llm_retry_delay=2.5)
    assert writer.llm is not first
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# 3. 构造期快照已拆除
# ---------------------------------------------------------------------------


def test_writer_reads_concurrency_at_use_time():
    """同一个 writer 实例，改并发度后属性值跟着变。

    改造前这五项是 `__init__` 里拷进实例的常量，改了配置要重建 writer 才生效；
    而 `DocumentService` 会**复用**同一个 writer——于是「改了后台配置，本次生成不变」
    成了常态。这条断言的是「属性每次现读」这个行为本身。
    """
    from backend.services.prd_section_writer import PrdSectionWriter

    writer = PrdSectionWriter(llm=_FakeLLM())
    assert writer._concurrency == get_settings().prd_pipeline_concurrency  # noqa: SLF001

    _set(prd_pipeline_concurrency=6)
    assert writer._concurrency == 6, "构造期快照没拆干净"  # noqa: SLF001

    _set(prd_pipeline_concurrency=1)
    assert writer._concurrency == 1  # noqa: SLF001


def test_writer_review_switch_is_live():
    """审核开关同样现读；但**调用方显式传入的覆盖**优先。"""
    from backend.services.prd_section_writer import PrdSectionWriter

    writer = PrdSectionWriter(llm=_FakeLLM())
    default_on = get_settings().prd_section_review_enabled
    assert writer._review_enabled is default_on  # noqa: SLF001

    _set(prd_section_review_enabled=not default_on)
    assert writer._review_enabled is (not default_on)  # noqa: SLF001

    # 快捷流程会显式传 False；这种覆盖必须稳定，不能被配置改动顶掉
    forced = PrdSectionWriter(llm=_FakeLLM(), review_enabled=False)
    assert forced._review_enabled is False  # noqa: SLF001
    _set(prd_section_review_enabled=True)
    assert forced._review_enabled is False  # noqa: SLF001


# ---------------------------------------------------------------------------
# 4. 任务级冻结
# ---------------------------------------------------------------------------


def test_task_snapshot_does_not_drift():
    """任务开始后改配置，这次任务读到的仍是派发那一刻的快照。

    `dispatch` 取一份快照，`run_job` 用 `use_config` 冻进上下文。少了它，
    一次 20 分钟的生成跑到一半就会换参数——表现为章节乱序或正文重复。
    """
    rc.invalidate()
    snapshot = rc.get_runtime_config()
    before = snapshot.settings.prd_pipeline_concurrency

    with rc.use_config(snapshot):
        _set(prd_pipeline_concurrency=before + 1 if before < 8 else before - 1)
        inside = rc.get_runtime_config()
        assert inside is snapshot
        assert inside.settings.prd_pipeline_concurrency == before

    # 出了任务上下文，新任务读到新值
    assert rc.get_runtime_config().settings.prd_pipeline_concurrency != before


def test_job_meter_uses_active_provider(monkeypatch):
    """任务回执里记的是**使用中的那条配置**，而不是 `DEFAULT_LLM_PROVIDER`。

    切过配置之后，后者可能与实际发出的请求不符——回执与账单就对不上了。
    """
    from backend.services import job_service

    ModelProviderRepository.create(
        provider_id="cfg_claude01",
        name="Claude-测试",
        protocol="anthropic",
        default_model="claude-test",
    )
    _set(active_provider_id="cfg_claude01")
    rc.invalidate()

    captured = {}

    class _Stop(Exception):
        pass

    async def _noop_stream(kind, request):  # noqa: ANN001, ARG001
        raise _Stop
        yield  # pragma: no cover

    monkeypatch.setattr(job_service, "_build_event_stream", _noop_stream)

    original = job_service.JobRuntime

    class _Spy(original):  # type: ignore[misc, valid-type]
        def __init__(self, **kwargs):  # noqa: ANN003
            captured.update(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(job_service, "JobRuntime", _Spy)

    import asyncio

    async def _run() -> None:
        job_service.dispatch(
            999,
            owner_id=1,
            solution_id=None,
            kind="prd",
            request={"requirements": "x", "product_name": "y"},
        )
        # 等任务跑完（它会因 _Stop 而落 failed）
        await asyncio.sleep(0.05)

    asyncio.run(_run())

    meter = captured.get("meter")
    assert meter is not None
    # 协议与条目 id 必须同时记下：前者用于查限值与聚合账单，后者用于定位到具体一条
    assert meter.protocol == "anthropic", f"应按协议记录，实际 {meter.protocol}"
    assert meter.config_id == "cfg_claude01"
    assert meter.name == "Claude-测试"
    assert meter.model == "claude-test"
    assert captured.get("config") is not None


# ---------------------------------------------------------------------------
# 5. 自定义供应商的窗口
# ---------------------------------------------------------------------------


def test_custom_provider_window_no_longer_falls_back_to_32k():
    """供应商行里填了 `context_window` 就用它，不再被 `_FALLBACK`（32k）误判。

    自定义供应商（OpenAI 兼容 + 自己的模型名）在 `model_registry` 里查不到。
    不填这一列时它会落到 32k 兜底，症状是「换了个 128k 的模型，却老报输入超顶」。
    """
    ModelProviderRepository.create(
        provider_id="cfg_mylocal1",
        name="本地 vLLM",
        protocol="openai_compatible",
        base_url="http://127.0.0.1:8000/v1",
        value_token="",
        default_model="qwen-72b",
        context_window=100_000,
        max_output=8_000,
    )
    _set(active_provider_id="cfg_mylocal1")

    limits = rc.resolve_model_limits()
    assert limits.context_window == 100_000
    assert limits.max_output == 8_000
    # 输入上限 = 窗口 − 预留输出
    assert rc.current_input_ceiling() == 92_000

    # 没填窗口时退回注册表（openai_compatible 的 128k，不是 32k）
    ModelProviderRepository.update("cfg_mylocal1", {"context_window": 0, "max_output": 0})
    rc.invalidate()
    assert rc.resolve_model_limits().context_window == 128_000
