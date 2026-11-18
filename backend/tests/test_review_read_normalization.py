"""历史审核结论的**读取侧**归一：旧数据里的 `##` 不能在界面上复活。

2026-11-18 反馈「已有 `##` 这种 md 格式露出来了」。新结论在 `normalize_issue` 里已经洗过，
但**这之前落库的结论**读出来时不会再归一——不洗的话，历史版本永远露着 `##`。
前端按约束不做清洗（那会变成第二个真源），所以归一必须在读路径上做一次。
"""

from __future__ import annotations

import json

from backend.services.document_store import _load_review
from backend.services.review_defects import clean_conclusion


def test_load_review_strips_markdown_from_stored_conclusion() -> None:
    """落库的 `## 1. 产品概述` 读出来是纯文本，且章节号保留。"""
    raw = json.dumps(
        {
            "verdict": "revise",
            "issues": [
                {"severity": "high", "where": "### 1. 产品概述", "detail": "**编造**了鉴权方式"},
                {"severity": "medium", "where": "## 2. 功能需求", "detail": "- 表格列与规范不一致"},
            ],
        },
        ensure_ascii=False,
    )

    conclusion = _load_review(raw)

    assert conclusion is not None
    assert conclusion["verdict"] == "revise"
    assert conclusion["issues"][0]["where"] == "1. 产品概述"
    assert conclusion["issues"][0]["detail"] == "编造了鉴权方式"
    assert conclusion["issues"][1]["where"] == "2. 功能需求"
    assert conclusion["issues"][1]["detail"] == "表格列与规范不一致"


def test_load_review_is_idempotent_and_tolerates_old_shapes() -> None:
    """已经洗过的结论再洗一遍不变；非结论形状原样返回，不报错。"""
    already_clean = {"verdict": "revise", "issues": [{"where": "2. 功能需求", "detail": "空泛"}]}
    assert _load_review(json.dumps(already_clean, ensure_ascii=False)) == already_clean

    assert _load_review("") is None
    assert _load_review("not json") is None
    assert _load_review(json.dumps({"verdict": "pass"}, ensure_ascii=False)) == {"verdict": "pass"}


def test_clean_conclusion_keeps_non_issue_fields_untouched() -> None:
    """只洗 `issues` 里的文本字段：`summary` / `reviewed_at` 这些不该被碰。"""
    conclusion = {
        "verdict": "revise",
        "summary": "**总体** 有问题",
        "reviewed_at": "2026-11-18T00:00:00Z",
        "issues": [{"where": "## 1. 背景", "detail": "*空泛*描述"}],
    }

    cleaned = clean_conclusion(conclusion)

    assert cleaned is not None
    assert cleaned["summary"] == "**总体** 有问题", "summary 是另一类文本，不在本次清洗范围"
    assert cleaned["reviewed_at"] == conclusion["reviewed_at"]
    assert cleaned["issues"][0] == {"where": "1. 背景", "detail": "空泛描述"}
