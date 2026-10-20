"""提示词生成的输入：PRD **必给**，接口文档**可选**。

这一组守的是「我有 PRD + 接口文档」入口带进来的第二份材料怎么走到模型面前。它比看起来
需要多守两件事：

1. **模板的占位符集合是一个契约。** 用户消息模板有两份（`skills/prompts-generator` 的规范版
   与 `prompts.py` 的兜底版），代码统一按同一组变量 `format` 它们。少一个占位符的后果不是
   「那一节空着」而是 `KeyError`——整条提示词生成线挂掉，且只在技能包与兜底不一致时暴露。
2. **没有接口文档时那一节不能留空。** 留空会让模型看到「一个标题 + 一片空白」，它既可能
   当没看见，也可能自己补一份出来——后者更糟：凭空多出一份没人写过的接口契约，用户很难
   发现那是编的。因此代码填的是一句显式说明（`NO_API_DOCS_NOTE`）。
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

from fakes import ScriptedLLM

from backend.api import jobs, schemas
from backend.services import prompts as prompts_module
from backend.services.document_service import DocumentService
from backend.services.prompts import NO_API_DOCS_NOTE
from backend.services.skill_registry import load_skill_artifacts

# 用户消息里必须出现的三个占位符：契约两端（技能包 / 兜底）都要声明它们
_PLACEHOLDERS = ("prd_content", "api_docs_content", "pending_spec")


async def _drain(events: AsyncIterator) -> list:
    """同步跑完一条生成流，收集全部事件。"""
    return [event async for event in events]


def _user_message(llm: ScriptedLLM) -> str:
    """取出**生成那一次**调用收到的用户消息正文。

    取第一次而不是最后一次：接入整篇审核后，`calls` 里会多出一次审核调用
    （审核提示词里没有 `NO_API_DOCS_NOTE`，拿最后一次会误判成「没填说明」）。
    生成永远是这条流里的第一次调用。
    """
    assert llm.calls, "生成没有调用模型"
    return str(llm.calls[0][-1].content)


# ---------------------------------------------------------------------------
# 模板契约
# ---------------------------------------------------------------------------


def test_both_templates_declare_the_optional_input() -> None:
    """技能包版与兜底版都要带 `{api_docs_content}`，且能按同一组变量填充。

    只查「有没有」不够——真正的失败形态是 `format` 抛 `KeyError`，所以这里直接填一遍。
    """
    guide = (load_skill_artifacts("prompts") or {}).get("prompts_guide", "")
    assert guide, "prompts-generator 技能包没加载到，这条契约就白守了"

    for name, template in (("技能包", guide), ("兜底", prompts_module.PROMPTS_GENERATION_PROMPT)):
        for placeholder in _PLACEHOLDERS:
            assert "{%s}" % placeholder in template, f"{name}模板缺少 {{{placeholder}}}"
        rendered = template.format(
            prd_content="P",
            api_docs_content="A",
            pending_spec="S",
        )
        assert "P" in rendered and "A" in rendered


def test_no_api_docs_note_is_not_empty_text() -> None:
    """替代空值的说明必须**有实际内容**：它存在的前提就是「空白会被误读」。"""
    assert NO_API_DOCS_NOTE.strip()
    assert "未提供" in NO_API_DOCS_NOTE


# ---------------------------------------------------------------------------
# 走到模型面前
# ---------------------------------------------------------------------------


def test_api_docs_content_reaches_the_model() -> None:
    """带上接口文档时，那份正文要真的出现在用户消息里。"""
    llm = ScriptedLLM(["# 提示词套件"])
    service = DocumentService(llm=llm)

    asyncio.run(
        _drain(
            service.generate_prompts_stream(
                prd_content="# PRD 正文",
                api_docs_content="# 接口文档正文",
            )
        )
    )

    message = _user_message(llm)
    assert "# PRD 正文" in message
    assert "# 接口文档正文" in message
    # 有材料时不该再出现「未提供」那套说辞
    assert NO_API_DOCS_NOTE not in message


def test_missing_api_docs_becomes_an_explicit_note() -> None:
    """不带接口文档（默认）= 旧行为：那一节写「本次未提供」，而不是一片空白。"""
    llm = ScriptedLLM(["# 提示词套件"])
    service = DocumentService(llm=llm)

    asyncio.run(_drain(service.generate_prompts_stream(prd_content="# PRD 正文")))

    message = _user_message(llm)
    assert "# PRD 正文" in message
    assert NO_API_DOCS_NOTE in message


def test_whitespace_only_api_docs_counts_as_missing() -> None:
    """只有空白字符与「没填」同义：粘贴框里敲了几个空格不代表有材料。"""
    llm = ScriptedLLM(["# 提示词套件"])
    service = DocumentService(llm=llm)

    asyncio.run(
        _drain(service.generate_prompts_stream(prd_content="# PRD", api_docs_content=" \n\t "))
    )

    assert NO_API_DOCS_NOTE in _user_message(llm)


# ---------------------------------------------------------------------------
# 任务入参
# ---------------------------------------------------------------------------


def test_job_request_carries_the_optional_input() -> None:
    """路由把请求体摊平时不能漏掉它——漏了的表现是「用户贴了但模型没看到」。"""
    payload = schemas.JobCreateRequest(
        kind="prompts",
        prd_content="# PRD",
        api_docs_content="# 接口文档",
    )
    assert jobs._build_request(payload)["api_docs_content"] == "# 接口文档"  # noqa: SLF001


def test_job_request_defaults_the_optional_input_to_empty() -> None:
    """老客户端（不传这个字段）照旧可用：缺省是空串，不是必填。"""
    payload = schemas.JobCreateRequest(kind="prompts", prd_content="# PRD")
    assert jobs._build_request(payload)["api_docs_content"] == ""  # noqa: SLF001
