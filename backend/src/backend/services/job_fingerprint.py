"""生成任务的输入指纹（本期仅 PRD 使用）。

指纹用于判断「输入是否与上次完全一致」，从而在不必要时跳过重复生成、或复用已有产物。
本期只在创建 PRD 任务时计算并落库，**不参与任何逻辑分支**（值可为空），
为后续 T3 续传 / 缓存留出字段。

指纹 = `sha256(需求文本 + 模板版本 + 章节结构)`：
- 需求文本变了 → 指纹变；
- 模板版本变了（人工 +1）→ 指纹变；
- 章节结构（模板里的一级标题）变了 → 指纹变。

把模板版本与章节结构纳入，是为了「改了模板却沿用旧指纹」这类静默失效——
只要影响产出的因素任一变化，指纹就必须变化。

票 #92 起章节结构**从技能包模板本身取**（`extract_headings`）：PRD 已改整篇生成，
不再有代码里那份 `PRD_SECTIONS` 清单，结构真源回到 `prd-template.md`。
"""

from __future__ import annotations

import hashlib

from backend.services.doc_skeleton import extract_headings
from backend.services.skill_registry import load_skill_artifacts

# PRD 模板版本。**改动 `skills/prd-generator/references/prd-template.md`
# 的章节结构或写作规范时，必须人工 +1**，否则旧指纹会被误判为「输入未变」。
PRD_TEMPLATE_VERSION = "v2"


def _template_structure() -> tuple[str, ...]:
    """模板的一级章节（`## `）：这就是 PRD 的产出结构。

    技能包不在位时返回空元组——指纹此时只由需求文本与模板版本决定，
    与「本次生成会退回内置提示词」这件事一致（不能凭空编一份结构进去）。
    """
    artifacts = load_skill_artifacts("prd")
    if not artifacts:
        return ()
    return tuple(extract_headings(artifacts.get("prd_template", "")))


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
            "\n".join(_template_structure()),
        ]
    )
    return hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()
