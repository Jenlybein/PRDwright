"""PRD 生成专属的技能资源取用与输入材料格式化。

这里只放 PRD 才有的东西：把需求摘要 / 自由文本 / 对话历史转成提示词里的需求文本，
以及「默认人设 + 写作规范」的拼接。

**技能包的定位、加载、指纹与自检都不在这里**，而在 `skill_registry`——那是所有技能包
共用的能力，本模块只是它的一个调用方。资源清单也不再写死在本文件里，由技能包自己在
`skill.yaml` 的 `references` 段声明（新增/改名资源只动技能包，不动代码）。

抽成独立模块的由来：让**整篇生成**（document_service）与**分章节生成**
（prd_section_writer）共用同一套逻辑，同时避免两个 service 互相导入形成循环依赖。

本模块不导入任何业务 service。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from backend.services.prompts import SYSTEM_PROMPT
from backend.services.skill_registry import (
    SkillManifestError,
    load_manifest,
    missing_artifacts,
    read_text_cached,
)

logger = logging.getLogger(__name__)

# PRD 生成依赖的技能包目录名（<skills 根目录>/<名称>/）。
# 「它服务于 prd 这个产物」由技能包自己在 `skill.yaml` 的 `applies_to` 里声明，
# 这里保留常量只是为了明确「PRD 这条线用哪个包」这个产品决定本身。
PRD_SKILL_NAME = "prd-generator"

# 空对话历史的占位文案（提示词里需要一个明确的「无」而不是空白）
NO_CONVERSATION_PLACEHOLDER = "（无补充对话记录）"


def load_prd_skill_artifacts() -> dict[str, str] | None:
    """加载 PRD 生成所需的技能资源。

    有哪几份资源、各叫什么键，全部由技能包在 `skill.yaml` 的 `references` 里声明
    （`key` 即这里的字典键），本函数不硬编码文件名。

    Returns:
        键为 `field_schema` / `prd_template` / `writing_rules` 的资源字典；
        技能包不在位、元数据损坏或任一资源缺失时返回 `None`，由调用方降级到内置提示词
        ——这条降级路径必须能在回执上看见（债务 D8），所以这里**不补占位空串**，
        回执上体现为 `injected_skills` 为空。
    """
    try:
        manifest = load_manifest(PRD_SKILL_NAME)
    except SkillManifestError as exc:
        logger.warning(
            "PRD 技能包元数据无法解析（%s），本次改用内置提示词生成。",
            exc,
            extra={"event": "prd_skill.manifest_invalid", "skill": PRD_SKILL_NAME},
        )
        return None

    if manifest is None:
        logger.warning(
            "PRD 技能包不在位（%s），本次改用内置提示词生成。"
            "生产环境应把 PRDWRIGHT_SKILLS_STRICT 设为 true，让缺失在启动时暴露",
            PRD_SKILL_NAME,
            extra={"event": "prd_skill.missing", "skill": PRD_SKILL_NAME},
        )
        return None

    missing = missing_artifacts(manifest)
    if missing:
        logger.warning(
            "PRD 技能资源缺失（%s），本次改用内置提示词生成。"
            "生产环境应把 PRDWRIGHT_SKILLS_STRICT 设为 true，让缺失在启动时暴露",
            "、".join(missing),
            extra={
                "event": "prd_skill.assets_missing",
                "skill": manifest.name,
                "missing": missing,
            },
        )
        return None

    return {
        key: read_text_cached(manifest.artifact_path(key)) for key in manifest.artifacts
    }


def format_summary_as_requirements(summary: dict[str, Any]) -> str:
    """把结构化需求摘要转成「需求文本」，供 PRD 提示词使用。

    这里刻意保留 schema 原貌（缩进 JSON）而不改写成散文：提示词里已经带了
    `field-schema.json` 字段契约，模型可以据此直接逐字段校验与归一化；
    转成散文反而会丢掉数组结构（MVP 功能逐行、页面清单、范围内外等）。

    Args:
        summary: 结构化需求摘要。

    Returns:
        形如「说明 + JSON 代码块」的需求文本。
    """
    body = json.dumps(summary, ensure_ascii=False, indent=2)
    return (
        "以下为结构化需求摘要（字段与「输入字段契约」一致，数组为逐项录入结果）：\n\n"
        "```json\n"
        f"{body}\n"
        "```"
    )


def format_freeform_requirements(requirements: str, product_name: str = "") -> str:
    """把**自由文本**需求（PRD 快捷入口的一段话描述）转成提示词里的需求文本。

    与 `format_summary_as_requirements` 并列，区别在于输入形态：那条路径输入的是
    与 schema 对齐的 JSON，这条路径输入的是用户随手写下的一段话。提示词里同时带着
    `field-schema.json` 字段契约，若不明说，模型容易误以为输入是结构化摘要、进而
    把契约里的字段当成「用户已提供」；因此这里显式声明输入形态，并重申缺失信息按
    「缺失信息处理」标注「待确认」。

    Args:
        requirements: 用户输入的自由文本需求。
        product_name: 用户在快捷入口单独填写的产品名称，可为空。

    Returns:
        形如「输入形态说明 + 产品名（可选）+ 需求正文 + 缺失信息提醒」的需求文本。
    """
    name = product_name.strip()
    name_line = f"用户填写的产品名称：{name}\n\n" if name else ""
    return (
        "以下为用户用自然语言直接描述的需求（**不是**结构化摘要，也不保证字段齐全）：\n\n"
        f"{name_line}"
        f"{(requirements or '').strip()}\n\n"
        "上述描述中未出现、而「输入字段契约」里列为必填的信息，一律按写作规范的"
        "「缺失信息处理」标注「待确认」，禁止自行补全或编造。"
    )


def format_conversation(history: list[dict[str, Any]] | None) -> str:
    """把对话历史（`{role, content}` 列表）拼成易读的 Markdown 文本。

    Args:
        history: 前端回传的对话消息，元素形如 `{"role": ..., "content": ...}`。

    Returns:
        形如 `**用户**：...` / `**AI**：...` 的多行文本；空历史返回占位文案。
    """
    if not history:
        return NO_CONVERSATION_PLACEHOLDER

    lines: list[str] = []
    for item in history:
        role = str(item.get("role", "")).strip().lower()
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        speaker = "用户" if role == "user" else "AI" if role == "assistant" else role or "系统"
        lines.append(f"**{speaker}**：{content}")
    return "\n\n".join(lines) if lines else NO_CONVERSATION_PLACEHOLDER


def build_prd_system_prompt(writing_rules: str) -> str:
    """把默认人设与技能包的写作规范拼成 PRD 生成的系统提示词。

    整篇生成与分章节生成必须用同一套人设与规范，否则两条路径产出的文档风格会不一致，
    因此把拼接逻辑收口在这里，两边共用。

    Args:
        writing_rules: 技能包内 `writing_rules` 资源的全文。

    Returns:
        系统提示词：默认人设 + 分隔线 + 写作规范。
    """
    return (
        f"{SYSTEM_PROMPT}\n\n---\n\n"
        "# PRD 写作规范（生成时必须逐条遵守）\n\n"
        f"{writing_rules}"
    )
