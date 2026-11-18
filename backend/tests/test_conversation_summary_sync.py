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


# ---------------------------------------------------------------------------
# 放宽口径（2026-11-18 用户决定）：AI 提问的结构化作答 = 用户明确表达
# ---------------------------------------------------------------------------


def test_prompts_treat_structured_answers_as_explicit_input():
    """提示词的**两处**（系统规则 + 任务段）都要点名「对提问的作答算明确表达」。

    这条用例防的是「只改一处」：只写进系统提示词、而任务段仍要求「仅当明确补充或修正」，
    模型会两边对冲，实际行为又退回保守口径（2026-11-18 线上就是这种「聊了半天啥也不回填」）。
    """
    from backend.services import prompts

    system = prompts.SUMMARY_SYNC_SYSTEM_PROMPT
    task = prompts.SUMMARY_SYNC_PROMPT_TEMPLATE

    for text, label in ((system, "系统提示词"), (task, "任务模板")):
        assert "答：" in text, f"{label}必须点名「答：」这种作答形态"
        assert "明确表达" in text, f"{label}必须把作答认定为明确表达"
    # 各字段的收纳口径也必须在系统提示词里写死，模型才知道并进哪里
    for field in ("platform", "mvp_features", "product_goal", "target_users"):
        assert field in system, f"收纳口径缺少字段 {field}"
    # 反方向的护栏还在：仍然不许新增顶层字段
    assert "不得新增输入里没有的键" in system


def test_merge_accepts_new_feature_item_from_conversation():
    """合并不掐掉用户新提的功能项（否则「并进 mvp_features」只是句空话）。"""
    from backend.services.document_service import _merge_summary

    original = {
        "product_name": "PRDwright",
        "platform": "",
        "mvp_features": [{"name": "需求澄清对话", "description": "", "user_value": ""}],
    }
    candidate = {
        "product_name": "PRDwright",
        "platform": "Web + 需登录 + 云端保存",
        "mvp_features": [
            {"name": "需求澄清对话", "description": "", "user_value": ""},
            {"name": "任务列表首页", "description": "首页展示任务列表与新建入口", "user_value": ""},
        ],
    }

    merged = _merge_summary(original, candidate)

    assert merged["platform"] == "Web + 需登录 + 云端保存"
    names = [item["name"] for item in merged["mvp_features"]]
    assert names == ["需求澄清对话", "任务列表首页"]
    # 顶层键仍然只允许原有集合：候选里多出来的键要丢掉
    assert set(merged) == set(original)
