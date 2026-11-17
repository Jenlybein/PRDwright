"""配置改动**真的生效**（v4 T3 的核心承诺）。

T3 最容易交付成一个「看起来能用」的功能：接口返回 200、页面显示「已保存」，
但生成用的还是旧参数。它的失败方式不是报错，而是**静默不生效**——所以用例必须
断言「行为变了」，而不是「接口通了」。

这里钉的是五条最关键的链路：

1. **审核窗口可配**：整篇审核的 `max_tokens` 真的取自 `doc_review_max_tokens`
   （票 #92 起三份产物共用这一个窗口，PRD 也整篇审）。
2. **LLM 实例按指纹重建**：改配置后拿到的是新实例，同配置则不重建（不白建客户端）。
4. **任务级冻结**：任务跑到一半改配置，这次任务的回执仍记旧供应商。
5. **自定义供应商的窗口**：填了 `context_window` 就不再被 32k 兜底误判超顶。
"""

from __future__ import annotations

import json

import pytest

from backend.core import runtime_config as rc
from backend.core.config import get_settings
from backend.core.contracts import StreamEvent
from dbsupport import connect
from conftest import (
    model_providers as ModelProviderRepository,
    register,
    system_config as SystemConfigRepository,
)
from backend.services import document_service, llm_factory
from ticket_support import run_ticket_events


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


def test_review_llm_uses_the_configurable_window(monkeypatch):
    """整篇审核实例的上限来自 `doc_review_max_tokens`（三份产物共用同一个开关）。

    票 #92 起 PRD 也整篇审，因此「单章审核上限」那份配置随分章链路一起退役：
    现在只有一个审核窗口，改它就是改三份产物的审核调用。
    """
    _set(doc_review_max_tokens=1234)

    seen: list[int | None] = []

    def _fake_get_llm(**kwargs):  # noqa: ANN003
        seen.append(kwargs.get("max_tokens"))
        return _FakeLLM(f"mt={kwargs.get('max_tokens')}")

    monkeypatch.setattr(document_service, "get_llm", _fake_get_llm)

    service = document_service.DocumentService()
    instance = service.review_llm

    assert seen == [1234], f"审核实例的窗口没取配置：{seen}"
    assert instance.label == "mt=1234"


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


# ---------------------------------------------------------------------------
# 4. 任务级冻结
# ---------------------------------------------------------------------------


def test_task_snapshot_does_not_drift():
    """任务开始后改配置，这次任务读到的仍是派发那一刻的快照。

    「造单」定格一份配置/供应商快照，后台任务用 `use_config` 冻进上下文。少了它，
    一次 20 分钟的生成跑到一半就会换参数——表现为章节乱序或正文重复。
    """
    rc.invalidate()
    snapshot = rc.get_runtime_config()
    before = snapshot.settings.doc_review_max_tokens

    with rc.use_config(snapshot):
        _set(doc_review_max_tokens=before + 1)
        inside = rc.get_runtime_config()
        assert inside is snapshot
        assert inside.settings.doc_review_max_tokens == before

    # 出了任务上下文，新任务读到新值
    assert rc.get_runtime_config().settings.doc_review_max_tokens != before


def test_job_meter_uses_active_provider(client):
    """任务回执里记的是**使用中的那条配置**，而不是 `DEFAULT_LLM_PROVIDER`。

    切过配置之后，后者可能与实际发出的请求不符——回执与账单就对不上了。

    ⚠️ #81 起「收尾」是一次条件写（`WHERE status='running'`）+ 同事务副作用；不登记的假任务
    改不到任何行，收尾按「本次结果被丢弃」处理、不再广播回执（旧实现会照发一份）。因此
    这条用例改成走真实的「登记 → 派发 → 订阅」接缝（主接缝 `ticket_support` 本来就是
    这类用例的标准写法），断言的仍是同一件事：`run_summary` 里的四格供应商身份。
    """
    ModelProviderRepository.create(
        provider_id="cfg_claude01",
        name="Claude-测试",
        protocol="anthropic",
        default_model="claude-test",
    )
    _set(active_provider_id="cfg_claude01")
    rc.invalidate()
    register(client)

    registered, events = run_ticket_events(
        1,
        None,
        "prd",
        {"requirements": "x", "product_name": "y"},
        StreamEvent(type="chunk", content="假流", data={}),
        reply="假流",
    )

    summary = next(event for event in reversed(events) if event.type == "run_summary").data
    # 协议与条目 id 必须同时记下：前者用于查限值与聚合账单，后者用于定位到具体一条
    assert summary["protocol"] == "anthropic", f"应按协议记录，实际 {summary['protocol']}"
    assert summary["provider_config_id"] == "cfg_claude01"
    assert summary["provider_name"] == "Claude-测试"
    assert summary["model"] == "claude-test"
    assert summary["status"] == "succeeded"
    assert registered.job_id > 0


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
