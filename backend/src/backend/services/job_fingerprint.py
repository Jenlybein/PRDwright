"""生成任务的输入指纹（本期仅 PRD 使用）。

指纹用于判断「输入是否与上次完全一致」，从而在不必要时跳过重复生成、或复用已有产物。
本期只在创建 PRD 任务时计算并落库，**不参与任何逻辑分支**（值可为空），
为后续 T3 续传 / 缓存留出字段。

指纹 = `sha256(需求文本 + 模板版本 + 章节结构)`：
- 需求文本变了 → 指纹变；
- 模板版本变了（人工 +1）→ 指纹变；
- 章节结构（id + heading）变了 → 指纹变。

把模板版本与章节结构纳入，是为了「改了模板却沿用旧指纹」这类静默失效——
只要影响产出的因素任一变化，指纹就必须变化。
"""

from __future__ import annotations

import hashlib

from backend.services.prd_section_writer import PRD_SECTIONS

# PRD 模板版本。**改动 `skills/prd-generator/references/prd-template.md`
# 的章节结构或写作规范时，必须人工 +1**，否则旧指纹会被误判为「输入未变」。
PRD_TEMPLATE_VERSION = "v1"


def compute_input_fingerprint(requirements_text: str) -> str:
    """计算一次 PRD 生成输入的指纹。

    Args:
        requirements_text: 需求文本（自由文本，或由结构化摘要格式化而来）。

    Returns:
        十六进制 sha256 摘要。
    """
    fingerprint_source = "\x00".join(
        [
            requirements_text.strip(),
            PRD_TEMPLATE_VERSION,
            "\n".join(f"{section.id}:{section.heading}" for section in PRD_SECTIONS),
        ]
    )
    return hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()
