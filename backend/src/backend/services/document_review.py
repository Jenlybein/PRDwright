"""整篇文档审核与一次改写（接口文档 / 提示词套件共用）。

为什么单独一个模块而不是塞进 `prd_section_writer`：
- PRD 是**按章节**审核的，每章一次、带前序章节上下文；
- 接口文档与提示词是**整篇**生成的，只审一次整篇。
两者共用「结构化 JSON 判定」的形态，但上下文与调用位置不同，混在一起会让
`prd_section_writer` 的章节循环更难读。

审核失败一律按「通过」处理、改写失败一律保留初稿：这两步是锦上添花，
不能因为它们挂掉就把已经生成的正文丢掉。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from backend.core.contracts import StreamEvent
from backend.services.llm_factory import StreamingChatModel
from backend.services.llm_utils import extract_json_object
from backend.services.prompts import (
    DOC_REVIEW_PROMPT,
    DOC_REVIEW_SYSTEM_PROMPT,
    DOC_REWRITE_PROMPT,
)
from backend.services.stream_reply import collect_reply

logger = logging.getLogger(__name__)

_REWRITE_SYSTEM_PROMPT = "你是一位严谨的技术文档撰写者。直接输出完整的 Markdown 正文，不要解释。"


@dataclass
class DocumentReviewResult:
    """一次整篇审核的判定。字段与 PRD 的单章节审核保持一致的语义。"""

    verdict: str
    issues: list[dict[str, Any]] = field(default_factory=list)
    instruction: str = ""

    @property
    def needs_revision(self) -> bool:
        return self.verdict == "revise" and bool(self.issues or self.instruction.strip())

    def issues_text(self) -> str:
        """把问题清单排成给改写提示词用的编号列表。"""
        lines: list[str] = []
        for index, issue in enumerate(self.issues, start=1):
            severity = str(issue.get("severity") or "medium")
            detail = str(issue.get("detail") or "")
            lines.append(f"{index}. [{severity}] {detail}")
        return "\n".join(lines) if lines else "（审核未给出逐条问题）"

    def summary(self) -> str:
        """一句话结论：优先第一条 high，否则第一条问题。"""
        for issue in self.issues:
            if str(issue.get("severity") or "") == "high":
                return str(issue.get("detail") or "")
        return str(self.issues[0].get("detail") or "") if self.issues else ""


async def review_whole_document(
    llm: StreamingChatModel,
    *,
    document: str,
    writing_rules: str,
    requirements: str,
) -> DocumentReviewResult | None:
    """对一份完整文档做一次审核。

    返回 `None` 表示「没审成」：正文为空或调用失败。调用方应按「通过」处理，
    不要因此把任务判失败。
    """
    text = (document or "").strip()
    if not text:
        return None
    messages = _build_messages(
        DOC_REVIEW_SYSTEM_PROMPT,
        DOC_REVIEW_PROMPT.format(
            writing_rules=writing_rules or "（未提供产出规范，按通用规范核对）",
            requirements=requirements or "（未提供上游材料）",
            document=text,
        ),
    )
    try:
        raw = await collect_reply(llm, messages)
    except Exception:  # noqa: BLE001 - 审核失败按通过处理
        logger.warning("整篇文档审核失败，按通过处理", exc_info=True)
        return None

    parsed = extract_json_object(raw)
    if parsed is None:
        # 解析不出来时按通过：与 PRD 的 `_safe_review` 同口径，宁可漏审不要误判失败
        logger.warning("整篇文档审核返回的不是合法 JSON，按通过处理")
        return DocumentReviewResult(verdict="pass")

    issues = parsed.get("issues")
    normalized_issues = [item for item in issues if isinstance(item, dict)] if isinstance(issues, list) else []
    verdict = str(parsed.get("verdict") or "").strip().lower()
    if verdict not in ("pass", "revise"):
        verdict = "revise" if normalized_issues else "pass"
    return DocumentReviewResult(
        verdict=verdict,
        issues=normalized_issues,
        instruction=str(parsed.get("instruction") or ""),
    )


def rewrite_whole_document_messages(
    *,
    document: str,
    review: DocumentReviewResult,
    writing_rules: str,
) -> list[BaseMessage]:
    """组装「按审核问题重写全文」的消息。"""
    return _build_messages(
        _REWRITE_SYSTEM_PROMPT,
        DOC_REWRITE_PROMPT.format(
            issues=review.issues_text(),
            instruction=review.instruction or "按问题清单逐条修正",
            writing_rules=writing_rules or "（未提供产出规范，按通用规范核对）",
            document=document,
        ),
    )


def review_event(
    kind: str,
    review: DocumentReviewResult | None,
    *,
    rewritten: bool = False,
    failed_sections: list[dict[str, Any]] | None = None,
) -> StreamEvent:
    """构造不持久化的 `review` 事件（前端只在本次订阅内展示）。"""
    verdict = review.verdict if review else "pass"
    issues = review.issues if review else []
    summary = review.summary() if review else "审核未完成，按通过处理"
    return StreamEvent(
        type="review",
        content=summary,
        data={
            "kind": kind,
            "verdict": verdict,
            "summary": summary,
            "issues": issues,
            "rewritten": rewritten,
            "failed_sections": failed_sections or [],
        },
    )


def _build_messages(system_prompt: str, user_content: str) -> list[BaseMessage]:
    return [SystemMessage(content=system_prompt), HumanMessage(content=user_content)]
