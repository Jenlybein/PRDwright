"""PRD 分章节生成器（Writer Agent + Review Agent）。

为什么分章节
------------
`skills/prd-generator/references/prd-template.md` 本身就是「6 个一级章节 + 待确认附录」
的结构，天然可切。更重要的是有两处**跨章节依赖**，决定了章节必须**串行**生成：

- 6.1「在范围内」要引用 2.1 节的功能编号（F-01、F-02…）；
- 附录「待确认问题」要汇总全文出现的所有「待确认」项。

因此每一章都在「已定稿的前序章节正文」前提下撰写，后续章节才可能与前文口径一致。

三步流程
--------
对每一章依次执行：

1. **撰写 writing**：带上字段契约、全文模板、已定稿的前序章节，流式写出本章；
2. **审核 reviewing**：审核 Agent 依据写作规范核对本章，返回 JSON 判定；
3. **改写 rewriting（可选）**：判定为 `revise` 时带着具体问题重写本章。
   改写后重新审核；改写轮数上限由 `PRD_SECTION_MAX_REWRITES` 控制（默认 1 轮），
   达到上限就保留当前版本，绝不无限循环。

阶段事件
--------
每一步都通过 `stage` 事件上报，与前端 `prdGenerationStage`
（idle / writing / reviewing / rewriting / done）五态一一对应。改写时会重新流式输出
整章，前端按 `section_id` 丢弃该章已接收的文本——用户看到的是「这一章在重写」，
而不是「内容凭空被撤回」。

文档头由代码生成，不交给模型：产品名取自结构化摘要、日期取服务器当天，都是确定的事实
（模板明确要求「无法确定当天日期时写『待确认』，禁止编造日期」，交给模型反而容易编）。

降级
----
技能包资源缺失、或模板章节与 `PRD_SECTIONS` 对不上时不启动本生成器
（`is_available()` 返回 `False`），由 `document_service` 退回整篇一次性生成。

输入形态
--------
需求输入可以是**结构化摘要**（完整流程：表单 + 对话澄清后产出的 JSON），也可以是
**自由文本**（PRD 快捷入口：用户直接写下的一段话）。两者共用本模块的全部环节，
差别只在 `prd_skill` 里选哪个格式化函数、以及文档头的产品名从哪里来。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, AsyncIterator

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from backend.api.schemas import StreamEvent
from backend.core.config import get_settings
from backend.services.llm_factory import StreamingChatModel, get_llm
from backend.services.llm_utils import extract_json_object, extract_text
from backend.services.prd_skill import (
    build_prd_system_prompt,
    format_conversation,
    format_freeform_requirements,
    format_summary_as_requirements,
    load_prd_skill_artifacts,
)
from backend.services.prompts import (
    PRD_SECTION_GENERATION_PROMPT,
    PRD_SECTION_REVIEW_PROMPT,
    PRD_SECTION_REVIEW_SYSTEM_PROMPT,
    PRD_SECTION_REWRITE_APPENDIX,
)
from backend.services.stream_guard import ensure_not_truncated

logger = logging.getLogger(__name__)

# 匹配模板里的一级章节标题（`## ` 但排除 `### `）
_SECTION_HEADING = re.compile(r"^## (?!#)(.*)$", re.MULTILINE)


# ----------------------------------------------------------------------------
# 章节定义
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class PrdSection:
    """一个 PRD 章节的元信息。

    `heading` 必须与 `prd-template.md` 里的一级标题**逐字一致**，它既是模板切片的键，
    也是要求模型输出的第一行。
    """

    # 稳定标识：前端据此累积与丢弃某章文本，不要随意改动
    id: str
    # 模板中的一级标题行，如 `## 2. 功能需求`
    heading: str
    # 展示名，如「功能需求」
    title: str
    # 本章的写作要点（补充模板注释之外的要求）
    focus: str


# 与 `references/prd-template.md` 的一级章节一一对应，顺序即生成顺序
PRD_SECTIONS: tuple[PrdSection, ...] = (
    PrdSection(
        id="overview",
        heading="## 1. 产品概述",
        title="产品概述",
        focus=(
            "写清 1.1 产品名称、1.2 核心问题（谁在什么场景下遇到什么问题、现在没有这个产品时怎么办）、"
            "1.3 目标用户（按 target_users 逐条渲染，只有一项时用段落表述）、1.4 平台。"
            "只描述输入中给出的事实，不补充市场调研与竞品数据。"
        ),
    ),
    PrdSection(
        id="features",
        heading="## 2. 功能需求",
        title="功能需求",
        focus=(
            "2.1 MVP 功能按 mvp_features 逐行渲染进表格，编号从 F-01 起连续唯一，"
            "全文引用功能时只引用编号；module 缺失归入「基础功能」；description 缺失时用 name 展开并标注「（建议）」；"
            "acceptance 缺失时写「待确认」。2.2 未来功能按 v2_features 逐条列出，为空时写「用户未提供后续规划，本节暂空」。"
        ),
    ),
    PrdSection(
        id="pages",
        heading="## 3. 页面设计",
        title="页面设计",
        focus=(
            "页面表按 ui_pages 渲染（页面 / 核心模块 / 关键交互），再依次写 3.1 页面流转路径与 "
            "3.2 交互与状态约定。ui_pages 为空且平台是纯 API 服务时整节写「本产品为纯 API 服务，无页面设计，本节不适用」。"
        ),
    ),
    PrdSection(
        id="tech",
        heading="## 4. 技术规格",
        title="技术规格",
        focus=(
            "只记录 technical_constraints 中明确给出的约束，未给出的项写「待确认」；"
            "禁止代用户选定框架、云厂商、数据库或第三方服务。"
        ),
    ),
    PrdSection(
        id="nonfunctional",
        heading="## 5. 非功能需求",
        title="非功能需求",
        focus=(
            "保留性能 / 安全 / 可用性 / 兼容性四个条目，未提及的项写「待确认」，不要删除条目，"
            "也不要凭空写出「响应时间 < 200ms」这类具体数值指标。"
        ),
    ),
    PrdSection(
        id="scope",
        heading="## 6. 项目范围",
        title="项目范围",
        focus=(
            "6.1 在范围内必须与第 2.1 节一致，只列功能编号与名称、不复述描述；"
            "6.2 不在范围内列入 v2_features 的每一项，并补充工程层面明确不属于本版的内容。"
        ),
    ),
    PrdSection(
        id="pending",
        heading="## 附：待确认问题",
        title="待确认问题",
        focus=(
            "汇总前文所有出现的「待确认」项，逐条写清「问题 —— 影响章节；需要谁拍板」；"
            "必须与正文中的「待确认」一一对应，无待确认项时写「无」。"
        ),
    ),
)


def split_template_by_section(template: str) -> dict[str, str]:
    """把技能包模板按一级章节切片。

    Args:
        template: `references/prd-template.md` 全文。

    Returns:
        `{标题行: 该章节的模板片段}`，标题行形如 `## 1. 产品概述`；
        模板中不存在的一级标题不会出现在结果里。
    """
    matches = list(_SECTION_HEADING.finditer(template))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(template)
        sections[match.group(0).strip()] = template[match.start() : end].strip()
    return sections


def is_available() -> bool:
    """判断当前技能包是否支持分章节生成。

    需要同时满足：三份技能资源都能读到，且模板里能找到全部章节的一级标题
    （模板被改写导致标题对不上时，分章节会产出残章，不如退回整篇生成）。

    Returns:
        可用返回 `True`；否则返回 `False` 并记录原因，由调用方降级。
    """
    artifacts = load_prd_skill_artifacts()
    if artifacts is None:
        return False

    template_sections = split_template_by_section(artifacts["prd_template"])
    missing = [section.heading for section in PRD_SECTIONS if section.heading not in template_sections]
    if missing:
        logger.warning(
            "PRD 模板中缺少这些一级标题，无法分章节生成：%s", "、".join(missing)
        )
        return False
    return True


def build_document_header(product_name: str) -> str:
    """生成 PRD 的文档头（H1 标题 + 状态行）。

    由代码生成而不是交给模型：产品名由调用方提供（结构化摘要里的 `product_name`，
    或快捷入口用户单独填写的名称）、日期取服务器当天，两者都是确定的事实。模板里明确
    要求「无法确定当日日期时写『待确认』，禁止编造日期」——交给模型写反而会凭空编一个
    日期出来（已实际踩过）。

    Args:
        product_name: 产品名称；空串时退化为「未命名产品」。

    Returns:
        形如 `# {产品名} 产品需求文档（PRD）\\n\\n> 文档状态：…\\n\\n` 的文本。
    """
    name = (product_name or "").strip() or "未命名产品"
    return (
        f"# {name} 产品需求文档（PRD）\n\n"
        f"> 文档状态：待评审 ｜ 版本：v1.0 ｜ 最后更新：{date.today().isoformat()}\n\n"
    )


# ----------------------------------------------------------------------------
# 审核结论
# ----------------------------------------------------------------------------


@dataclass
class SectionReview:
    """一次章节审核的结论。"""

    # `pass` 或 `revise`
    verdict: str
    # 问题列表，每项含 severity / detail
    issues: list[dict[str, Any]] = field(default_factory=list)
    # 给撰写者的修改指令
    instruction: str = ""

    @property
    def needs_revision(self) -> bool:
        """是否需要改写：判定为 revise 且至少给出了问题或修改指令。"""
        return self.verdict == "revise" and bool(self.issues or self.instruction.strip())

    def issues_text(self) -> str:
        """把问题列表渲染成提示词里的编号清单。"""
        if not self.issues:
            return "（审核未给出具体问题）"
        lines: list[str] = []
        for index, issue in enumerate(self.issues, start=1):
            severity = str(issue.get("severity", "")).strip() or "medium"
            detail = str(issue.get("detail", "")).strip() or "未说明"
            lines.append(f"{index}. [{severity}] {detail}")
        return "\n".join(lines)

    def summary(self) -> str:
        """给前端一句话展示的改写原因：优先取第一条 high 问题。"""
        highs = [
            issue
            for issue in self.issues
            if str(issue.get("severity", "")).strip().lower() == "high"
        ]
        candidates = highs or self.issues
        first = str(candidates[0].get("detail", "")).strip() if candidates else ""
        return first or "审核发现需要调整的问题"


# ----------------------------------------------------------------------------
# 生成器
# ----------------------------------------------------------------------------


class PrdSectionWriter:
    """逐章撰写 + 审核 + 改写的 PRD 生成器。

    与 `document_service` 的其他流式方法一样，产出 `StreamEvent` 事件流；区别是多了
    `stage` 事件，用于把「写到第几章、当前在写还是审」实时告诉前端。
    """

    def __init__(
        self,
        llm: StreamingChatModel | None = None,
        *,
        review_enabled: bool | None = None,
        max_rewrites: int | None = None,
    ) -> None:
        """初始化生成器。

        Args:
            llm: 自定义 LLM 实例；不传则首次使用时惰性创建。
            review_enabled: 是否启用审核环节；不传则取配置 `PRD_SECTION_REVIEW_ENABLED`。
            max_rewrites: 每章最多改写轮数；不传则取配置 `PRD_SECTION_MAX_REWRITES`。
        """
        self._llm = llm
        settings = get_settings()
        self._review_enabled = (
            settings.prd_section_review_enabled if review_enabled is None else review_enabled
        )
        self._max_rewrites = (
            settings.prd_section_max_rewrites if max_rewrites is None else max_rewrites
        )

    @property
    def llm(self) -> StreamingChatModel:
        """LLM 实例，首次访问时才创建（避免服务初始化阶段就要求密钥）。"""
        if self._llm is None:
            self._llm = get_llm()
        return self._llm

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    async def stream(
        self,
        *,
        summary: dict[str, Any] | None = None,
        requirements: str = "",
        product_name: str = "",
        history: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """按章节串行生成 PRD，流式产出事件。

        需求输入有两种形态，二者共用同一条链路，只有「需求文本怎么拼」与「文档头
        用哪个产品名」不同：

        - `summary`（完整流程）：与 `field-schema.json` 对齐的结构化摘要；
        - `requirements`（PRD 快捷入口）：用户直接用自然语言写下的一段话。

        两种形态都走同一套「逐章撰写 → 审核 → 改写」，因此审核标准、写作规范与
        产出结构完全一致——入口只决定输入怎么来，不决定文档怎么写。

        Args:
            summary: 结构化需求摘要；给出时优先于 `requirements`。
            requirements: 自由文本需求；仅在 `summary` 为空时使用。
            product_name: 产品名称，仅用于文档头；结构化摘要里已有名称时以摘要为准。
            history: 补充对话记录（`{role, content}` 列表），可为空。

        Yields:
            `status` → `chunk`（文档头）→ 每章 `stage`/`chunk`… → `stage: done` → `done`；
            中途失败时产出 `error` 并结束（已流出的内容保留在前端）。

        Raises:
            ValueError: 两种需求输入都为空——没有需求就不该产出文档，宁可报错也不编。
        """
        artifacts = load_prd_skill_artifacts()
        if artifacts is None:
            raise RuntimeError("PRD 技能资源不可用，无法分章节生成")

        if summary:
            requirements_text = format_summary_as_requirements(summary)
            name = str(summary.get("product_name") or "").strip() or product_name
        elif requirements.strip():
            requirements_text = format_freeform_requirements(requirements, product_name)
            name = product_name
        else:
            raise ValueError("summary 与 requirements 至少需要提供一个")

        writing_rules = artifacts["writing_rules"]
        system_prompt = build_prd_system_prompt(writing_rules)
        conversation = format_conversation(history or [])
        template_sections = split_template_by_section(artifacts["prd_template"])

        total = len(PRD_SECTIONS)
        yield StreamEvent(
            type="status",
            content=f"正在分 {total} 章生成 PRD…",
            data={"doc_type": "prd", "total_sections": total},
        )

        # 文档头由代码产出：它不属于任何一章，因此不能被「改写某章」波及
        header = build_document_header(name)
        yield StreamEvent(type="chunk", content=header)
        parts: list[str] = [header]

        # 已定稿章节的正文，作为后续章节的上下文（含标题行）
        written: list[str] = []

        for index, section in enumerate(PRD_SECTIONS):
            section_template = template_sections.get(section.heading, "")

            # ---------------- 1. 撰写 ----------------
            yield _section_stage_event("writing", section, index, total)
            section_text = ""
            buffer: list[str] = []
            try:
                async for delta in self._stream_text(
                    self._section_messages(
                        section=section,
                        section_template=section_template,
                        system_prompt=system_prompt,
                        requirements=requirements_text,
                        conversation=conversation,
                        field_schema=artifacts["field_schema"],
                        prior_sections=_join_prior(written),
                    )
                ):
                    buffer.append(delta)
                    yield StreamEvent(type="chunk", content=delta)
            except Exception as exc:  # noqa: BLE001 - 流式接口需把异常转成事件
                yield _error_event(exc)
                return
            section_text = "".join(buffer).strip()

            # ---------------- 2. 审核 + 3. 改写 ----------------
            if self._review_enabled and section_text:
                rewrites_done = 0
                while True:
                    yield _section_stage_event("reviewing", section, index, total)
                    review = await self._safe_review(
                        section=section,
                        section_text=section_text,
                        writing_rules=writing_rules,
                        requirements=requirements_text,
                        prior_sections=_join_prior(written),
                    )
                    if review is None or not review.needs_revision:
                        break
                    if rewrites_done >= self._max_rewrites:
                        logger.info(
                            "章节 %s 已达改写上限（%d 轮），保留当前版本",
                            section.id,
                            self._max_rewrites,
                        )
                        break

                    rewrites_done += 1
                    yield _section_stage_event(
                        "rewriting",
                        section,
                        index,
                        total,
                        note=review.summary(),
                        issues=review.issues,
                    )
                    buffer = []
                    try:
                        async for delta in self._stream_text(
                            self._rewrite_messages(
                                section=section,
                                section_template=section_template,
                                system_prompt=system_prompt,
                                requirements=requirements_text,
                                conversation=conversation,
                                field_schema=artifacts["field_schema"],
                                prior_sections=_join_prior(written),
                                review=review,
                            )
                        ):
                            buffer.append(delta)
                            yield StreamEvent(type="chunk", content=delta)
                    except Exception as exc:  # noqa: BLE001
                        yield _error_event(exc)
                        return
                    section_text = "".join(buffer).strip()

            section_text = _ensure_heading(section_text, section.heading)
            if section_text:
                written.append(section_text)
                parts.append(section_text + "\n\n")

        yield StreamEvent(
            type="stage",
            content="PRD 已生成",
            data={"stage": "done", "index": total, "total": total},
        )
        yield StreamEvent(
            type="done",
            content="",
            data={"doc_type": "prd", "reply": "".join(parts).strip()},
        )

    # ------------------------------------------------------------------
    # 消息组装
    # ------------------------------------------------------------------

    def _section_messages(
        self,
        *,
        section: PrdSection,
        section_template: str,
        system_prompt: str,
        requirements: str,
        conversation: str,
        field_schema: str,
        prior_sections: str,
    ) -> list[BaseMessage]:
        """组装「撰写某一章」的消息。"""
        user_content = PRD_SECTION_GENERATION_PROMPT.format(
            requirements=requirements,
            conversation=conversation,
            field_schema=field_schema,
            prd_template=section_template,
            prior_sections=prior_sections,
            section_heading=section.heading,
            section_title=section.title,
            section_focus=section.focus,
        )
        return _build_messages(system_prompt, user_content)

    def _rewrite_messages(
        self,
        *,
        section: PrdSection,
        section_template: str,
        system_prompt: str,
        requirements: str,
        conversation: str,
        field_schema: str,
        prior_sections: str,
        review: SectionReview,
    ) -> list[BaseMessage]:
        """组装「按审核意见重写某一章」的消息：撰写提示词 + 改写附录。"""
        user_content = PRD_SECTION_GENERATION_PROMPT.format(
            requirements=requirements,
            conversation=conversation,
            field_schema=field_schema,
            prd_template=section_template,
            prior_sections=prior_sections,
            section_heading=section.heading,
            section_title=section.title,
            section_focus=section.focus,
        )
        user_content += PRD_SECTION_REWRITE_APPENDIX.format(
            issues=review.issues_text(),
            instruction=review.instruction.strip() or "（未给出具体指令，请按上述问题自行修正）",
        )
        return _build_messages(system_prompt, user_content)

    # ------------------------------------------------------------------
    # 模型调用
    # ------------------------------------------------------------------

    async def _stream_text(self, messages: list[BaseMessage]) -> AsyncIterator[str]:
        """流式产出一次调用的纯文本增量，结束时检查是否被截断。"""
        last_chunk = None
        async for chunk in self.llm.astream(messages):
            text = extract_text(chunk.content)
            last_chunk = chunk
            if text:
                yield text
        if last_chunk is not None:
            ensure_not_truncated(last_chunk)

    async def _collect(self, messages: list[BaseMessage]) -> str:
        """把流式回复收拢成完整文本（审核需要整体 JSON 才能解析）。"""
        chunks: list[str] = []
        last_chunk = None
        async for chunk in self.llm.astream(messages):
            text = extract_text(chunk.content)
            last_chunk = chunk
            if text:
                chunks.append(text)
        if last_chunk is not None:
            ensure_not_truncated(last_chunk)
        return "".join(chunks).strip()

    async def _safe_review(
        self,
        *,
        section: PrdSection,
        section_text: str,
        writing_rules: str,
        requirements: str,
        prior_sections: str,
    ) -> SectionReview | None:
        """审核一章；调用或解析失败时返回 `None`（按「通过」处理）。

        审核是「锦上添花」的一步：它自己出错不应该把整篇生成拖垮，因此失败即放行。
        """
        try:
            return await self._review(
                section=section,
                section_text=section_text,
                writing_rules=writing_rules,
                requirements=requirements,
                prior_sections=prior_sections,
            )
        except Exception:  # noqa: BLE001 - 审核失败不阻断主流程
            logger.warning("章节 %s 审核调用失败，本次按通过处理", section.id, exc_info=True)
            return None

    async def _review(
        self,
        *,
        section: PrdSection,
        section_text: str,
        writing_rules: str,
        requirements: str,
        prior_sections: str,
    ) -> SectionReview:
        """调用审核 Agent，解析出结构化结论。"""
        user_content = PRD_SECTION_REVIEW_PROMPT.format(
            writing_rules=writing_rules,
            requirements=requirements,
            prior_sections=prior_sections,
            section_heading=section.heading,
            section_text=section_text,
        )
        messages = _build_messages(PRD_SECTION_REVIEW_SYSTEM_PROMPT, user_content)
        raw = await self._collect(messages)

        parsed = extract_json_object(raw)
        if parsed is None:
            logger.warning(
                "章节 %s 审核未返回合法 JSON，按通过处理。原始输出：%s", section.id, raw[:500]
            )
            return SectionReview(verdict="pass")

        verdict = str(parsed.get("verdict", "")).strip().lower()
        raw_issues = parsed.get("issues")
        issues = [item for item in raw_issues if isinstance(item, dict)] if isinstance(raw_issues, list) else []
        instruction = str(parsed.get("instruction") or "").strip()

        if verdict not in {"pass", "revise"}:
            # 判定词不合法时按「有没有问题」推断，避免因为一个词毁掉整段输出
            verdict = "revise" if issues else "pass"

        return SectionReview(verdict=verdict, issues=issues, instruction=instruction)


# ----------------------------------------------------------------------------
# 模块级小工具
# ----------------------------------------------------------------------------


def _build_messages(system_prompt: str, user_content: str) -> list[BaseMessage]:
    """把系统提示词与用户消息组装成 LangChain 消息列表。"""
    return [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_content),
    ]


def _join_prior(written: list[str]) -> str:
    """把已定稿章节拼成上下文文本；首章时给出明确说明而不是空串。"""
    if not written:
        return "（本章是第一章，暂无上游章节）"
    return "\n\n".join(written)


def _ensure_heading(section_text: str, heading: str) -> str:
    """保证章节正文以标题行开头。

    模型偶尔会漏掉标题行（尤其是改写时），导致正文与标题层级错位；
    这里做一次低成本修补，而不是为它再发一轮请求。
    """
    text = section_text.strip()
    if not text:
        return ""
    if text.startswith(heading):
        return text
    return f"{heading}\n\n{text}"


def _section_stage_event(
    stage: str,
    section: PrdSection,
    index: int,
    total: int,
    *,
    note: str = "",
    issues: list[dict[str, Any]] | None = None,
) -> StreamEvent:
    """构造章节级的 `stage` 事件。

    `data.section_id` 是前端丢弃「被改写那一章」已接收文本的依据，必须稳定。
    """
    labels = {
        "writing": "正在撰写",
        "reviewing": "正在审核",
        "rewriting": "正在改写",
    }
    label = labels.get(stage, "正在处理")
    data: dict[str, Any] = {
        "stage": stage,
        "section_id": section.id,
        "section_title": section.title,
        "section_heading": section.heading,
        "index": index,
        "total": total,
    }
    if note:
        data["note"] = note
    if issues:
        data["issues"] = issues
    return StreamEvent(
        type="stage",
        content=f"{label} {section.heading}（{index + 1}/{total}）…",
        data=data,
    )


def _error_event(exc: Exception) -> StreamEvent:
    """把异常转成 `error` 事件（与 `document_service._stream_reply` 的口径一致）。"""
    return StreamEvent(
        type="error",
        content=f"生成失败：{exc}",
        data={"error_type": type(exc).__name__},
    )
