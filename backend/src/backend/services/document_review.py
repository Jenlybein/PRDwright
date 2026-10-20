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
from typing import Any, AsyncIterator

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from backend.core.contracts import StreamEvent
from backend.services.llm_factory import StreamingChatModel
from backend.services.llm_utils import extract_json_object
from backend.services.pending_items import PendingBlockSuppressor, extract
from backend.services.prompts import (
    DOC_REVIEW_PROMPT,
    DOC_REVIEW_SYSTEM_PROMPT,
    DOC_REWRITE_PROMPT,
)
from backend.services.stream_reply import astream_text, collect_reply

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


@dataclass
class ReviewOutcome:
    """一次「审核 + 改写」的最终产物。

    异步生成器不能有返回值，因此结果由调用方传入一个空壳、由 `stream_review_and_rewrite`
    回填。调用方在**消费完事件流之后**读它，拿到的就是可以写进 `done` 的正文与清单。
    """

    # 最终正文：没改写就是初稿，改写成功则是改写稿（干净、已剥离标记块）
    final_reply: str = ""
    # 随最终正文一起定下来的待确认清单（由后端重新分配 id）
    pending_items: list[dict[str, Any]] = field(default_factory=list)
    # 是否真的发生了一次改写（供 review 结论标记）
    rewritten: bool = False


def review_messages(
    *,
    document: str,
    writing_rules: str,
    requirements: str,
) -> list[BaseMessage] | None:
    """组装整篇审核的消息；正文为空时返回 `None`（无可审核的内容）。"""
    text = (document or "").strip()
    if not text:
        return None
    return _build_messages(
        DOC_REVIEW_SYSTEM_PROMPT,
        DOC_REVIEW_PROMPT.format(
            writing_rules=writing_rules or "（未提供产出规范，按通用规范核对）",
            requirements=requirements or "（未提供上游材料）",
            document=text,
        ),
    )


def parse_review_text(raw: str) -> DocumentReviewResult:
    """把模型吐出的审核文本解析成判定。

    **永不抛异常**：JSON 解析不出来时按「通过」处理，与 PRD 的 `_safe_review` 同口径
    ——宁可漏审，也不要因为审核这一步把已经生成好的正文丢掉。
    """
    parsed = extract_json_object(raw)
    if parsed is None:
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


async def review_whole_document(
    llm: StreamingChatModel,
    *,
    document: str,
    writing_rules: str,
    requirements: str,
) -> DocumentReviewResult | None:
    """对一份完整文档做一次审核（**不流式**，只给需要结论的调用方用）。

    返回 `None` 表示「没审成」：正文为空或调用失败。调用方应按「通过」处理，
    不要因此把任务判失败。

    面向用户的生成链路不要用这个函数——它全程静默，用户只能干等。那边走
    `stream_review_and_rewrite`，审核与改写的增量会实时推给前端。
    """
    messages = review_messages(
        document=document, writing_rules=writing_rules, requirements=requirements
    )
    if messages is None:
        return None
    try:
        raw = await collect_reply(llm, messages)
    except Exception:  # noqa: BLE001 - 审核失败按通过处理
        logger.warning("整篇文档审核失败，按通过处理", exc_info=True)
        return None
    return parse_review_text(raw)


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


def stage_chunk_event(stage: str, text: str, doc_type: str) -> StreamEvent:
    """构造「阶段内的流式文本」事件（审核意见 / 改写稿）。

    它与正文 `chunk` 是两条独立通道：`chunk` 描述的是**产物正文**，前端会拼进预览；
    `stage_chunk` 描述的是**当前阶段模型的原始输出**，前端只在一个附属面板里滚动展示。
    混用会让审核意见跑到文档正文里去，所以宁可多一个事件类型。
    """
    return StreamEvent(
        type="stage_chunk",
        content=text,
        data={"stage": stage, "doc_type": doc_type},
    )


async def stream_review_and_rewrite(
    llm: StreamingChatModel,
    *,
    document: str,
    writing_rules: str,
    requirements: str,
    doc_type: str,
    kind: str,
    label: str,
    outcome: ReviewOutcome,
) -> AsyncIterator[StreamEvent]:
    """整篇审核 →（必要时）改写一次，**全程把模型输出流给前端**。

    为什么要流：审核与改写都是整篇量级的调用，静默期动辄几十秒，用户看不到任何进展，
    只能怀疑卡死了。这里把两步的原始输出实时推出去，前端挂在阶段条下方滚动展示，
    「正在审核 / 正在改写」于是有了可感知的证据。

    产出的事件序列：
    - `stage(reviewing)` → 若干 `stage_chunk`（审核意见原文）
    - 需要改写时 `stage(rewriting)` → 若干 `stage_chunk`（改写稿原文）
    - 改写成功时 `part{replace}`（定稿正文，整段覆盖初稿）
    - 末尾一条 `review`（结论，不持久化）

    Args:
        llm: 流式模型。
        document: 待审的完整正文（已剥离标记块的干净正文）。
        writing_rules: 该产物的产出规范（技能包整段注入）。
        requirements: 上游材料，审核时用来核对「有没有漏」。
        doc_type: 事件 `data.doc_type`，取 `api_doc` / `prompts`。
        kind: `review` 事件的产物类型，取 `api_docs` / `prompts`。
        label: 阶段文案里的产物名，如「接口文档」。
        outcome: **输出参数**。生成器没有返回值，最终正文与清单回填到这里；
            调用方在消费完事件流后读取。审核通过且无须改写时它保持调用方给的原值。

    审核失败按「通过」处理、改写失败保留初稿——这两步是锦上添花，不能因为它们挂掉
    就把已经生成好的正文丢掉（与 `review_whole_document` 同口径）。
    """
    messages = review_messages(
        document=document, writing_rules=writing_rules, requirements=requirements
    )
    if messages is None:
        # 正文为空：没什么可审的，直接给一个「通过」结论收场
        yield review_event(kind, None)
        return

    yield StreamEvent(
        type="stage",
        content=f"正在审核{label}…",
        data={"stage": "reviewing", "doc_type": doc_type},
    )
    raw_chunks: list[str] = []
    try:
        async for delta in astream_text(llm, messages):
            raw_chunks.append(delta)
            yield stage_chunk_event("reviewing", delta, doc_type)
    except Exception:  # noqa: BLE001 - 审核失败按通过处理
        logger.warning("整篇文档审核失败，按通过处理", exc_info=True)
        yield review_event(kind, None)
        return

    review = parse_review_text("".join(raw_chunks))
    if not review.needs_revision:
        yield review_event(kind, review)
        return

    yield StreamEvent(
        type="stage",
        content="审核发现问题，正在改写…",
        data={
            "stage": "rewriting",
            "doc_type": doc_type,
            "note": review.summary(),
            "issues": review.issues,
        },
    )
    # 改写稿里同样可能有待确认标记块：实时增量先过抑制器，别把哨兵漏到界面上
    suppressor = PendingBlockSuppressor()
    rewritten_chunks: list[str] = []
    try:
        async for delta in astream_text(
            llm,
            rewrite_whole_document_messages(
                document=document, review=review, writing_rules=writing_rules
            ),
        ):
            rewritten_chunks.append(delta)
            clean = suppressor.feed(delta)
            if clean:
                yield stage_chunk_event("rewriting", clean, doc_type)
        trailing = suppressor.flush()
        if trailing:
            yield stage_chunk_event("rewriting", trailing, doc_type)

        rewritten_text = "".join(rewritten_chunks).strip()
        if rewritten_text:
            final_reply, items = extract(rewritten_text)
            outcome.final_reply = final_reply
            outcome.pending_items = items
            outcome.rewritten = True
            # 初稿已经以 `chunk` 流出去过，改写稿必须整段覆盖，否则预览里会叠字
            yield StreamEvent(
                type="part",
                content="",
                data={"text": final_reply, "replace": True},
            )
    except Exception:  # noqa: BLE001 - 改写失败保留初稿
        logger.warning("%s改写失败，保留初稿", label, exc_info=True)

    yield review_event(kind, review, rewritten=outcome.rewritten)


def _build_messages(system_prompt: str, user_content: str) -> list[BaseMessage]:
    return [SystemMessage(content=system_prompt), HumanMessage(content=user_content)]
