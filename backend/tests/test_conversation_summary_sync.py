"""摘要回填（`sync-summary-from-conversation`）的契约测试。

盯的是 2026-11-18 线上暴露的那个 502：`DocumentService` 上的
`sync_summary_from_conversation` 在 #92（PRD 整篇化 + 分章退役）大规模删代码时
连带被删，而模块级的包装函数还在——于是每次调用都 `AttributeError`，路由层
`except Exception` 一律转成 502，4 毫秒返回、日志里一个字都不留。

两条用例分别钉住：
1. **正门能走通**：注入离线假模型后，模块级入口返回合并后的完整摘要，不抛异常；
2. **模型输出不是合法 JSON 时保持原摘要**：回填是锦上添花，不能把用户填好的需求弄丢。

零 token：真实 LLM 工厂不参与，`document_service.get_service` 被替换成注入了脚本模型的实例。
"""

from __future__ import annotations

import copy
import json

import pytest

from backend.services import document_service
from fakes import ScriptedLLM


@pytest.fixture
def use_llm(monkeypatch):
    """把文档服务换成注入了脚本模型的实例（惰性 property 打补丁打不到，要换实例）。"""

    def _install(chunks: list[str]):
        monkeypatch.setattr(
            document_service,
            "get_service",
            lambda: document_service.DocumentService(llm=ScriptedLLM(chunks)),
        )

    return _install


def test_summary_sync_returns_merged_summary(use_llm):
    """正门能走通：模型回一份 JSON，入口返回与它合并后的完整摘要。"""
    summary = {"product_name": "PRDwright", "problem": ""}
    use_llm([json.dumps({"product_name": "PRDwright", "problem": "想法说不清"}, ensure_ascii=False)])

    import asyncio

    updated = asyncio.run(
        document_service.sync_summary_from_conversation(
            summary=summary,
            history=[{"role": "user", "content": "它要解决的问题是：脑子里有想法但说不清。"}],
        )
    )

    assert updated["product_name"] == "PRDwright"
    assert updated["problem"] == "想法说不清"


def test_summary_sync_keeps_original_when_model_returns_garbage(use_llm):
    """模型输出不是合法 JSON：原摘要原样返回，不报错、不丢字段。"""
    summary = {"product_name": "PRDwright", "problem": "已有问题描述"}
    before = copy.deepcopy(summary)
    use_llm(["这不是 JSON，只是一段话。"])

    import asyncio

    updated = asyncio.run(
        document_service.sync_summary_from_conversation(summary=summary, history=None)
    )

    assert updated == before
