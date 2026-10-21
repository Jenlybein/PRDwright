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
from backend.core.runtime_config import get_runtime_config
from backend.services.doc_skeleton import (
    DocSection,
    join_sections,
    normalize_heading,
    resolve_where,
    sections_field_of,
    split_sections,
    strip_heading,
)
from backend.services.llm_factory import StreamingChatModel
from backend.services.llm_utils import extract_json_object
from backend.services.pending_items import (
    PendingBlockSuppressor,
    extract,
    finalize_items,
    render_spec,
)
from backend.services.prompts import (
    DOC_REVIEW_PROMPT,
    DOC_REVIEW_SYSTEM_PROMPT,
    DOC_REWRITE_PROMPT,
    DOC_SECTION_REWRITE_PROMPT,
)
from backend.services.stream_reply import astream_text, collect_reply

logger = logging.getLogger(__name__)

_REWRITE_SYSTEM_PROMPT = "你是一位严谨的技术文档撰写者。直接输出完整的 Markdown 正文，不要解释。"

# 严重度排序：`high` 最严重，改写门槛按「rank <= 门槛的 rank」判定
_SEVERITY_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}

# 一次分节改写最多重写几节。超过说明问题铺得太开，逐节重写要发 N 次调用、
# 还要承担「N 节之间口径不一致」的风险，不如整篇重写一次来得干净。
MAX_REWRITE_SECTIONS = 3

# 拼回后的正文如果比原文短这么多，说明分节改写把内容弄丢了，退回整篇重写
_MIN_KEPT_RATIO = 0.6

# `detail` 的软截断长度。模型偶尔会把整段原文塞进 detail，截断只是兜底——
# 真正的防线是提示词里那条「严禁摘录原文」。
_MAX_DETAIL_CHARS = 60


def _clip(text: str, limit: int = _MAX_DETAIL_CHARS) -> str:
    """把 `detail` 截断到指定长度（超长加省略号）。"""
    stripped = (text or "").strip()
    return stripped if len(stripped) <= limit else f"{stripped[:limit].rstrip()}…"


def reaches_severity(severity: str, min_severity: str) -> bool:
    """该严重度是否达到门槛（`high` 是最严重的一档）。

    整篇链路与分章节链路共用这一处判定——两条链路的改写门槛必须是同一个口径，
    否则「同一份文档在 PRD 侧改、在接口文档侧不改」就成了配置玄学。

    等级缺失或非法一律返回 `False`：与 `pending_items` 的重要性等级同口径，
    **不替模型猜等级**。

    Args:
        severity: 该条问题的严重度。
        min_severity: 门槛（`high` / `medium` / `low`）。

    Returns:
        是否达到门槛。
    """
    rank = _SEVERITY_RANK.get(str(severity or "").strip().lower())
    if rank is None:
        return False
    return rank <= _SEVERITY_RANK.get(str(min_severity).strip().lower(), 0)


@dataclass
class DocumentReviewResult:
    """一次整篇审核的判定。字段与 PRD 的单章节审核保持一致的语义。"""

    verdict: str
    issues: list[dict[str, Any]] = field(default_factory=list)
    instruction: str = ""

    @property
    def needs_revision(self) -> bool:
        return self.verdict == "revise" and bool(self.issues or self.instruction.strip())

    def triggers_rewrite(self, min_severity: str = "high") -> bool:
        """是否真的要动笔改写：审核认为有问题，且**严重度达到门槛**。

        为什么与 `needs_revision` 分开：审核的判定与「要不要花一次调用去改」是两件事。
        `verdict=revise` 只说明「有问题」，而 medium 里的「结构不符」「空泛表述」
        这类口径几乎必然命中——按它改写会让「审一次 → 改一次」变成常态，
        时延翻倍而收益极小。因此改写只看 high，medium 照样报给用户，只是不自动改。

        Args:
            min_severity: 触发改写的最低严重度（`high` / `medium` / `low`）。

        Returns:
            是否触发改写。
        """
        if not self.needs_revision:
            return False
        if not self.issues:
            # 只给了指令、没有逐条问题：无从按等级筛选，按原口径放行
            return True
        for issue in self.issues:
            if reaches_severity(str(issue.get("severity") or ""), min_severity):
                return True
        return False

    def issues_text(self) -> str:
        """把问题清单排成给改写提示词用的编号列表（带上 `where` 定位）。"""
        lines: list[str] = []
        for index, issue in enumerate(self.issues, start=1):
            severity = str(issue.get("severity") or "medium")
            detail = str(issue.get("detail") or "")
            where = str(issue.get("where") or "").strip()
            prefix = f"[{severity}]"
            if where:
                prefix += f" {where}"
            lines.append(f"{index}. {prefix}：{_clip(detail)}")
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
    section_titles: str = "",
) -> list[BaseMessage] | None:
    """组装整篇审核的消息；正文为空时返回 `None`（无可审核的内容）。

    Args:
        document: 待审正文。
        writing_rules: 产出规范。
        requirements: 上游材料（**调用方应先骨架化**，否则这次调用的 prefill 会很贵）。
        section_titles: 本文档的章节标题清单，作为审核 `where` 的取值域。
            留空时提示词里那一节会写成「（未能识别章节）」，模型仍可判定，
            只是后续无法定位到具体章节、只能退回整篇重写。
    """
    text = (document or "").strip()
    if not text:
        return None
    return _build_messages(
        DOC_REVIEW_SYSTEM_PROMPT,
        DOC_REVIEW_PROMPT.format(
            writing_rules=writing_rules or "（未提供产出规范，按通用规范核对）",
            requirements=requirements or "（未提供上游材料）",
            section_titles=section_titles.strip() or "（未能识别章节标题，无法定位的问题请写空串）",
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
        logger.warning(
            "整篇文档审核返回的不是合法 JSON，按通过处理",
            extra={"event": "doc.review_invalid_json"},
        )
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
    section_titles: str = "",
) -> DocumentReviewResult | None:
    """对一份完整文档做一次审核（**不流式**，只给需要结论的调用方用）。

    返回 `None` 表示「没审成」：正文为空或调用失败。调用方应按「通过」处理，
    不要因此把任务判失败。

    面向用户的生成链路不要用这个函数——它全程静默，用户只能干等。那边走
    `stream_review_and_rewrite`，审核与改写的增量会实时推给前端。
    """
    messages = review_messages(
        document=document,
        writing_rules=writing_rules,
        requirements=requirements,
        section_titles=section_titles,
    )
    if messages is None:
        return None
    try:
        raw = await collect_reply(llm, messages)
    except Exception:  # noqa: BLE001 - 审核失败按通过处理
        logger.warning(
            "整篇文档审核失败，按通过处理",
            exc_info=True,
            extra={"event": "doc.review_degraded"},
        )
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


def section_rewrite_messages(
    *,
    section_heading: str,
    section_text: str,
    review: DocumentReviewResult,
    writing_rules: str,
) -> list[BaseMessage]:
    """组装「只重写某一节」的消息。

    与 `rewrite_whole_document_messages` 的差别只有一处：这里喂的是**那一节**而不是
    整篇，也只要求输出那一节。输出量因此从「整篇」降到「一节」——这是整条链路里
    最贵的一次调用（`DOC_REWRITE_PROMPT` 要求整篇重吐），也是省时的主要来源。
    """
    return _build_messages(
        _REWRITE_SYSTEM_PROMPT,
        DOC_SECTION_REWRITE_PROMPT.format(
            issues=review.issues_text(),
            instruction=review.instruction or "按问题清单逐条修正",
            section_heading=section_heading,
            section_text=section_text or "（该节当前没有内容）",
            writing_rules=writing_rules or "（未提供产出规范，按通用规范核对）",
            pending_spec=render_spec(),
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


@dataclass
class _RewritePlan:
    """一次分节改写的计划：哪些节要重写、各自背着哪些问题。"""

    preamble: str
    sections: list[DocSection]
    targets: list[tuple[DocSection, list[dict[str, Any]]]]


def plan_section_rewrites(
    document: str, review: DocumentReviewResult
) -> _RewritePlan | None:
    """决定这次走「分节改写」还是「整篇重写」。

    返回 `None` 表示**退回整篇重写**。兜底条件（宁可多花一次调用，也不要改错地方
    或把正文改坏）：

    - 文档切不出任何 `## ` 章节——无从定位；
    - `where` 一条都对不上——模型没给定位，或给了文档里不存在的章节名；
    - 命中的章节超过 `MAX_REWRITE_SECTIONS`——问题铺得太开，逐节重写要发 N 次调用，
      还要承担「N 节之间口径不一致」的风险，不如整篇重写一次来得干净。

    Args:
        document: 待改写的完整正文（已剥离标记块）。
        review: 审核结论。

    Returns:
        改写计划；`None` 表示走整篇重写。
    """
    preamble, sections = split_sections(document)
    if not sections:
        return None

    grouped: dict[str, list[dict[str, Any]]] = {}
    for issue in review.issues:
        section = resolve_where(str(issue.get("where") or ""), sections)
        if section is None:
            continue  # 无人认领的问题：交整篇重写一并处理
        grouped.setdefault(section.heading, []).append(issue)

    if not grouped or len(grouped) > MAX_REWRITE_SECTIONS:
        return None

    return _RewritePlan(
        preamble=preamble,
        sections=sections,
        targets=[
            (section, grouped[section.heading])
            for section in sections
            if section.heading in grouped
        ],
    )


def merge_pending_items(
    base: list[dict[str, Any]],
    extra: list[dict[str, Any]],
    *,
    rewritten_headings: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """合并改前与改后的待确认清单，**重新排序并重新分配 id**。

    为什么需要它：分节改写只重写其中几节，重新 `extract` 拿到的只有那几节的新项。
    若直接覆盖，未改写章节的待确认项会**整批丢失**——用户会发现「改了一节，
    清单里少了一半问题」。

    合并规则：
    - 属于**被改写章节**的旧项出局（该节的问题已被重新判定：解决了的会消失，
      还在的新稿里会重新出现）；
    - 新稿的项 + 保留的旧项，按 `question` 去重（新稿优先）；
    - 最后统一走 `finalize_items` 重新排序、重新分配 id，保证 id 连续不重号。

    Args:
        base: 改写前的清单。
        extra: 改写节新 `extract` 出来的项。
        rewritten_headings: 本次真正被改写的章节标题行。

    Returns:
        合并后的清单。
    """
    targets = [text for text in (normalize_heading(h) for h in rewritten_headings) if text]

    def belongs_to_rewritten(item: dict[str, Any]) -> bool:
        for field in (normalize_heading(name) for name in sections_field_of(item)):
            if not field:
                continue
            for target in targets:
                if field == target or field in target or target in field:
                    return True
        return False

    kept = [item for item in base if not belongs_to_rewritten(item)]

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in list(extra) + kept:
        question = str(item.get("question") or "").strip()
        if question:
            if question in seen:
                continue
            seen.add(question)
        merged.append(item)
    return finalize_items(merged)


async def _stream_whole_rewrite(
    llm: StreamingChatModel,
    *,
    document: str,
    review: DocumentReviewResult,
    writing_rules: str,
    doc_type: str,
    label: str,
    outcome: ReviewOutcome,
) -> AsyncIterator[StreamEvent]:
    """整篇重写一次（改造前的原行为，现在是分节改写的兜底路径）。

    事件序列与改造前**逐字一致**：若干 `stage_chunk(rewriting)` → 一条
    `part{replace}`。改写失败保留初稿，不抛异常。
    """
    # 改写稿里同样可能有待确认标记块：实时增量先过抑制器，别把哨兵漏到界面上
    suppressor = PendingBlockSuppressor()
    chunks: list[str] = []
    try:
        async for delta in astream_text(
            llm,
            rewrite_whole_document_messages(
                document=document, review=review, writing_rules=writing_rules
            ),
        ):
            chunks.append(delta)
            clean = suppressor.feed(delta)
            if clean:
                yield stage_chunk_event("rewriting", clean, doc_type)
        trailing = suppressor.flush()
        if trailing:
            yield stage_chunk_event("rewriting", trailing, doc_type)

        rewritten_text = "".join(chunks).strip()
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
        logger.warning(
            "%s改写失败，保留初稿", label, exc_info=True, extra={"event": "doc.rewrite_failed"}
        )


async def _stream_section_rewrites(
    llm: StreamingChatModel,
    *,
    plan: _RewritePlan,
    review: DocumentReviewResult,
    document: str,
    writing_rules: str,
    doc_type: str,
    label: str,
    outcome: ReviewOutcome,
) -> AsyncIterator[StreamEvent]:
    """按计划逐节重写，拼回全文后整段覆盖。

    只重写命中的那几节，输出量从「整篇」降到「N 节」——这是整条链路里最贵的一次
    调用，也是省时的主要来源。

    兜底：
    - **单节**失败或返回空 → 只保留该节原稿，其余节照常替换，**不**连累整篇；
    - 一节都没改成 → 退回整篇重写。

    ⚠️ 退回整篇重写时，此前已经流出去的分节 `stage_chunk` 会残留在阶段面板里
    （前端只在换阶段时清空，而两步同属 `rewriting`）。这是罕见路径，宁可面板上
    多一段，也不能把正文改坏。
    """
    suppressor = PendingBlockSuppressor()
    final_sections = list(plan.sections)
    rewritten_headings: list[str] = []
    new_items: list[dict[str, Any]] = []

    for target, issues in plan.targets:
        # 只把归属本节的问题喂给改写：其它节的问题与它无关，混进去会诱导顺带改写
        scoped = DocumentReviewResult(
            verdict="revise", issues=issues, instruction=review.instruction
        )
        chunks: list[str] = []
        try:
            async for delta in astream_text(
                llm,
                section_rewrite_messages(
                    section_heading=target.heading,
                    section_text=target.body,
                    review=scoped,
                    writing_rules=writing_rules,
                ),
            ):
                chunks.append(delta)
                clean = suppressor.feed(delta)
                if clean:
                    yield stage_chunk_event("rewriting", clean, doc_type)
            trailing = suppressor.flush()
            if trailing:
                yield stage_chunk_event("rewriting", trailing, doc_type)
        except Exception:  # noqa: BLE001 - 单节失败不影响其它节
            logger.warning(
                "章节 %s 改写失败，保留原稿",
                target.heading,
                exc_info=True,
                extra={"event": "doc.section_rewrite_failed"},
            )
            continue

        # 模型常把标题行再输出一遍，`join_sections` 也会加一次——去掉，否则标题重复
        section_body = strip_heading("".join(chunks).strip(), target.heading)
        if not section_body:
            continue
        clean_body, items = extract(section_body)
        new_items.extend(items)
        final_sections = [
            DocSection(target.heading, clean_body)
            if section.heading == target.heading
            else section
            for section in final_sections
        ]
        rewritten_headings.append(target.heading)

    if not rewritten_headings:
        logger.warning(
            "%s 分节改写没有产出任何内容，退回整篇重写",
            label,
            extra={"event": "doc.section_rewrite_empty"},
        )
        async for event in _stream_whole_rewrite(
            llm,
            document=document,
            review=review,
            writing_rules=writing_rules,
            doc_type=doc_type,
            label=label,
            outcome=outcome,
        ):
            yield event
        return

    merged_text = join_sections(plan.preamble, final_sections)
    if len(merged_text) < len(document) * _MIN_KEPT_RATIO:
        # 拼回来比原文短太多 = 内容被弄丢了。整篇重写一次，别把残稿交给用户。
        logger.warning(
            "%s 分节改写后正文从 %d 字符缩到 %d，退回整篇重写",
            label,
            len(document),
            len(merged_text),
            extra={"event": "doc.section_rewrite_shrunk"},
        )
        async for event in _stream_whole_rewrite(
            llm,
            document=document,
            review=review,
            writing_rules=writing_rules,
            doc_type=doc_type,
            label=label,
            outcome=outcome,
        ):
            yield event
        return

    outcome.final_reply = merged_text
    outcome.pending_items = merge_pending_items(
        outcome.pending_items, new_items, rewritten_headings=tuple(rewritten_headings)
    )
    outcome.rewritten = True
    # 预览里的正文是整篇：这里必须整段覆盖，不能只发被改写的那一节
    yield StreamEvent(
        type="part",
        content="",
        data={"text": merged_text, "replace": True},
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
    review_llm: StreamingChatModel | None = None,
    section_titles: str = "",
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

    改写优先走**分节改写**（只重写受影响的章节），定位不到或面太大时退回整篇重写
    （见 `plan_section_rewrites`）。

    Args:
        llm: 流式模型（撰写用的大窗口实例）。
        document: 待审的完整正文（已剥离标记块的干净正文）。
        writing_rules: 该产物的产出规范（技能包整段注入）。
        requirements: 上游材料，审核时用来核对「有没有漏」。**调用方应先骨架化**。
        doc_type: 事件 `data.doc_type`，取 `api_doc` / `prompts`。
        kind: `review` 事件的产物类型，取 `api_docs` / `prompts`。
        label: 阶段文案里的产物名，如「接口文档」。
        outcome: **输出参数**。生成器没有返回值，最终正文与清单回填到这里；
            调用方在消费完事件流后读取。审核通过且无须改写时它保持调用方给的原值。
        review_llm: 审核专用的**小输出窗口**实例。审核只吐 JSON 批注，用不着生成级
            的输出上限；不传则退回 `llm`（测试注入的假模型正是走这条路）。
        section_titles: 本文档的章节标题清单，作为审核 `where` 的取值域。

    审核失败按「通过」处理、改写失败保留初稿——这两步是锦上添花，不能因为它们挂掉
    就把已经生成好的正文丢掉（与 `review_whole_document` 同口径）。
    """
    settings = get_runtime_config().settings
    auditor = review_llm or llm

    messages = review_messages(
        document=document,
        writing_rules=writing_rules,
        requirements=requirements,
        section_titles=section_titles,
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
        async for delta in astream_text(auditor, messages):
            raw_chunks.append(delta)
            yield stage_chunk_event("reviewing", delta, doc_type)
    except Exception:  # noqa: BLE001 - 审核失败按通过处理
        logger.warning(
            "整篇文档审核失败，按通过处理",
            exc_info=True,
            extra={"event": "doc.review_degraded"},
        )
        yield review_event(kind, None)
        return

    review = parse_review_text("".join(raw_chunks))
    # 门槛：审核结论照给（含 medium），但只有达到门槛才真动笔——
    # medium 里的口径几乎必然命中，按它改写会把「审一次 → 改一次」变成常态
    if not review.triggers_rewrite(settings.review_rewrite_min_severity):
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

    plan = plan_section_rewrites(document, review) if settings.doc_section_rewrite_enabled else None
    if plan is None:
        async for event in _stream_whole_rewrite(
            llm,
            document=document,
            review=review,
            writing_rules=writing_rules,
            doc_type=doc_type,
            label=label,
            outcome=outcome,
        ):
            yield event
    else:
        async for event in _stream_section_rewrites(
            llm,
            plan=plan,
            review=review,
            document=document,
            writing_rules=writing_rules,
            doc_type=doc_type,
            label=label,
            outcome=outcome,
        ):
            yield event

    yield review_event(kind, review, rewritten=outcome.rewritten)


def _build_messages(system_prompt: str, user_content: str) -> list[BaseMessage]:
    return [SystemMessage(content=system_prompt), HumanMessage(content=user_content)]
