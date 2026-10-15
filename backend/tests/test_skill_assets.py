"""PRD 生成专属层（`prd_skill`）：资源取用与提示词材料格式化。

通用的技能包加载（目录发现、缓存、指纹、自检、按产物找包）在
`test_skill_registry.py`；这里只覆盖 PRD 这一层自己的事：
**资源清单来自技能包声明**，以及缺资源 / 元数据坏时的降级行为。
"""

from __future__ import annotations

from conftest import PRD_SKILL_ARTIFACTS, write_skill_package

from backend.services.prd_skill import (
    NO_CONVERSATION_PLACEHOLDER,
    build_prd_system_prompt,
    format_conversation,
    format_freeform_requirements,
    load_prd_skill_artifacts,
)
from backend.services.prompts import SYSTEM_PROMPT


# ----------------------------------------------------------------------------
# 资源取用
# ----------------------------------------------------------------------------


def test_artifacts_follow_skill_declaration(temp_skills_root) -> None:
    """有哪几份资源、各叫什么键，全看技能包的声明——代码里不再有一份并行清单。

    这条守住的是「加资源只改技能包」：声明两份就返回两份，代码不必知道。
    """
    write_skill_package(
        temp_skills_root,
        "prd-generator",
        artifacts={
            "field_schema": "references/field-schema.json",
            "writing_rules": "references/writing-rules.md",
        },
    )

    artifacts = load_prd_skill_artifacts()

    assert set(artifacts) == {"field_schema", "writing_rules"}
    assert artifacts["writing_rules"].startswith("初始内容")


def test_artifacts_are_file_contents(temp_skills_root) -> None:
    """返回的是资源内容而不是路径——写成路径会一路带到提示词里，且很难察觉。"""
    skill_dir = write_skill_package(temp_skills_root, "prd-generator")
    (skill_dir / PRD_SKILL_ARTIFACTS["prd_template"]).write_text("# 模板正文\n", encoding="utf-8")

    artifacts = load_prd_skill_artifacts()

    assert artifacts["prd_template"] == "# 模板正文\n"


def test_returns_none_when_skill_missing(temp_skills_root) -> None:
    """技能包不在位 → 返回 `None`，由调用方走内置提示词。

    这条降级必须在回执上看得见（本次的 `injected_skills` 为空），
    所以这里刻意不返回空字典、也不补占位内容。
    """
    assert load_prd_skill_artifacts() is None


def test_returns_none_when_manifest_is_broken(temp_skills_root) -> None:
    """元数据写坏了也只降级、不抛异常：一次生成不该因为资产文件写错就整个失败。"""
    skill_dir = temp_skills_root / "prd-generator"
    skill_dir.mkdir()
    (skill_dir / "skill.yaml").write_text("name: [未闭合\n", encoding="utf-8")

    assert load_prd_skill_artifacts() is None


def test_returns_none_when_any_artifact_missing(temp_skills_root) -> None:
    """任一资源缺失就整体返回 `None`：半套规范比没有规范更危险（风格会前后不一致）。"""
    write_skill_package(temp_skills_root, "prd-generator")
    assert load_prd_skill_artifacts() is not None

    (temp_skills_root / "prd-generator" / PRD_SKILL_ARTIFACTS["writing_rules"]).unlink()

    assert load_prd_skill_artifacts() is None


# ----------------------------------------------------------------------------
# 提示词材料格式化
# ----------------------------------------------------------------------------


def test_freeform_requirements_declares_it_is_not_structured() -> None:
    """自由文本入口必须自己说清「这不是结构化摘要」。

    提示词里同时带着字段契约，不说清的话模型容易把契约里的字段当成「用户已提供」，
    于是凭空编出用户从没提过的内容。
    """
    text = format_freeform_requirements("做一个记账小程序")

    assert "结构化摘要" in text
    assert "不是" in text
    assert "做一个记账小程序" in text
    assert "待确认" in text


def test_freeform_requirements_includes_product_name_when_given() -> None:
    without_name = format_freeform_requirements("需求正文")
    with_name = format_freeform_requirements("需求正文", "记账本")

    assert "记账本" not in without_name
    assert "用户填写的产品名称：记账本" in with_name


def test_conversation_placeholder_when_empty() -> None:
    """没有对话记录时给一句明确的「无」，而不是留空白（留白会让模型以为漏了内容）。"""
    assert format_conversation(None) == NO_CONVERSATION_PLACEHOLDER
    assert format_conversation([]) == NO_CONVERSATION_PLACEHOLDER


def test_conversation_renders_speakers_and_skips_blank() -> None:
    text = format_conversation(
        [
            {"role": "user", "content": "要支持导出"},
            {"role": "assistant", "content": "已记录"},
            {"role": "user", "content": "   "},
        ]
    )

    assert "**用户**：要支持导出" in text
    assert "**AI**：已记录" in text
    # 空白内容的消息不进结果，避免提示词里出现一行没说话的「用户：」
    assert text.count("**用户**") == 1


def test_system_prompt_joins_persona_and_writing_rules() -> None:
    """默认人设与技能包写作规范必须同时在场——整篇生成与分章节生成共用这一处拼接。"""
    prompt = build_prd_system_prompt("规范正文")

    assert SYSTEM_PROMPT in prompt
    assert prompt.endswith("规范正文")
