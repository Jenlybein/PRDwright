"""整篇审核 / 一次改写：解析口径与失败降级。

守住三条：
1. 合法 JSON 能识别出 `revise` 与逐条问题；
2. 非法 JSON / 调用失败一律按「通过」处理，不能把任务判失败；
3. 改写提示词必须同时带上「问题清单」与「上一版全文」。
"""

from __future__ import annotations

import asyncio

from backend.services.document_review import (
    DocumentReviewResult,
    review_whole_document,
    rewrite_whole_document_messages,
)
from fakes import ScriptedLLM


def _run(coro):
    return asyncio.run(coro)


def test_review_parses_revise_json() -> None:
    llm = ScriptedLLM(
        ['{"verdict":"revise","issues":[{"severity":"high","detail":"编造了接口"}],"instruction":"删掉"}']
    )

    result = _run(
        review_whole_document(
            llm, document="# 文档", writing_rules="规范", requirements="PRD"
        )
    )

    assert result is not None
    assert result.needs_revision is True
    assert "编造了接口" in result.issues_text()


def test_review_invalid_json_falls_back_to_pass() -> None:
    llm = ScriptedLLM(["这不是 JSON"])

    result = _run(
        review_whole_document(llm, document="# 文档", writing_rules="", requirements="")
    )

    assert result is not None
    assert result.verdict == "pass"
    assert result.needs_revision is False


def test_review_failure_returns_none() -> None:
    llm = ScriptedLLM([], raise_at=0)

    assert (
        _run(
            review_whole_document(
                llm, document="# 文档", writing_rules="", requirements=""
            )
        )
        is None
    )


def test_empty_document_is_not_reviewed() -> None:
    llm = ScriptedLLM(["不该被调用"])

    assert (
        _run(
            review_whole_document(
                llm, document="   ", writing_rules="", requirements=""
            )
        )
        is None
    )


def test_rewrite_messages_include_issues_and_document() -> None:
    review = DocumentReviewResult(
        verdict="revise",
        issues=[{"severity": "high", "detail": "缺接口"}],
        instruction="补上",
    )

    messages = rewrite_whole_document_messages(
        document="# 原文档", review=review, writing_rules="规范"
    )
    text = "\n".join(str(message.content) for message in messages)

    assert "缺接口" in text
    assert "# 原文档" in text
    assert "补上" in text
