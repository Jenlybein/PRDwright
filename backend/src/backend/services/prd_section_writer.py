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

事件契约
--------
两类事件共同驱动前端的实时预览：

- `stage`：上报「写到第几章、当前在写还是审」，与前端 `prdGenerationStage`
  （idle / writing / reviewing / rewriting / done）五态一一对应；
- `part`：**一段正文一被定稿就整段下发**（文档头一次、每章一次），
  `data.text` 是可直接追加到文档末尾的字符串。

为什么正文要「定稿后再整段下发」
--------------------------------
`chunk` 是模型的**原始输出**，而最终正文还要过两道加工：`_ensure_heading()`
补标题行（模型偶尔漏写，尤其改写时）、以及章与章之间补 `\n\n` 分隔。前端若只
按 `chunk` 逐字拼接，这两道加工它就看不到，表现为「某一章的标题凭空消失」
「标题与上一段的文字粘在同一行」（粘住后 markdown 不再认它是标题）。

所以这里让后端做正文的唯一来源：前端维护「已定稿部分 + 当前章实时缓冲」，
`part` 一到就用权威文本覆盖该段、清空缓冲。于是「预览与最终 `done.reply`
逐字一致」成为**结构性保证**，而不是靠前后端各自拼得一样。

`stage: rewriting` 时该章还没有 `part`，前端只需丢弃自己的实时缓冲即可，
**不需要按字符数回退**——按累加长度猜截断点非常脆，任何计数漂移都会切偏。

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

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, AsyncIterator

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from backend.core.config import get_settings
from backend.core.contracts import StreamEvent
from backend.core.errors import AppError
from backend.services.llm_factory import StreamingChatModel, get_llm
from backend.services.llm_meter import use_step
from backend.services.llm_utils import extract_json_object
from backend.services.pending_items import (
    PendingBlockSuppressor,
    extract,
    render_spec,
)
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
from backend.services.stream_reply import astream_text, collect_reply, error_event

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
            "acceptance 缺失时写「待确认」。mvp_features 为空数组（用户未填）时，依据 ui_pages 的页面 / 模块 /"
            "关键交互推导一份候选功能清单：每条功能都要能落到某个页面模块或关键交互上，不得造与页面无关的功能；"
            "功能点名称后统一加「（建议）」后缀，优先级无依据时写「待确认」，验收标准一律写「待确认：该功能的验收标准」；"
            "表格上方加一句总说明「以下功能清单由模型依据页面结构与关键交互推断，均标注（建议），请确认或修改」，"
            "数量宁少勿滥。ui_pages 也为空时才写「待确认：MVP 功能清单未提供」，两者都登记进待确认清单。"
            "2.2 未来功能按 v2_features 逐条列出，为空时写「用户未提供后续规划，本节暂空」。"
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
            "6.1 在范围内必须与第 2.1 节一致，只列功能编号与名称、不复述描述，"
            "推断得来的功能项保留名称后的「（建议）」后缀；"
            "mvp_features 与 ui_pages 均为空时 6.1 写「待确认：MVP 功能清单未提供」，不要留空。"
            "6.2 不在范围内列入 v2_features 的每一项，并补充工程层面明确不属于本版的内容。"
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


@dataclass
class _SectionState:
    """并行链路里单章的运行态（只活在本次 `stream()` 的闭包里）。"""

    index: int
    section: PrdSection
    status: str = "pending"  # pending / writing / reviewing / rewriting / done / failed
    draft: str = ""
    part: str = ""
    failed: bool = False
    reason: str = ""
    review: SectionReview | None = None
    pending_items: list[dict[str, Any]] = field(default_factory=list)


def _failed_placeholder(section: PrdSection, reason: str) -> str:
    """生成失败的章节占位：保留标题结构，并明确写出失败原因。"""
    return (
        f"{section.heading}\n\n"
        f"> 本章生成失败：{reason}。请稍后重新生成本章以补齐本节。\n\n"
    )


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
        # 并发度与章节重试次数：只在并行链路生效；串行链路保持原行为。
        self._concurrency = max(1, settings.prd_pipeline_concurrency)
        self._max_retries = max(0, settings.prd_section_max_retries)

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
        """按配置选择串行 / 并行生成。

        并发度为 1 时走原来的串行链路（回滚开关）；大于 1 时走三车道并行。
        两条链路产出同一套 `StreamEvent` 契约，`document_service` 无需分支。
        """
        if self._concurrency <= 1:
            async for event in self._stream_linear(
                summary=summary,
                requirements=requirements,
                product_name=product_name,
                history=history,
            ):
                yield event
            return
        async for event in self._stream_parallel(
            summary=summary,
            requirements=requirements,
            product_name=product_name,
            history=history,
        ):
            yield event

    async def _stream_linear(
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
            `status` → `part`（文档头）→ 每章 `stage`/`chunk`…/`part` → `stage: done` → `done`；
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

        # 文档头由代码产出：它不属于任何一章，因此不能被「改写某章」波及。
        # 走 part 事件而不是 chunk：前端据此把它归入「已定稿部分」，否则它会被
        # 当成第一章的实时缓冲，在第一章开始时被一并清掉。
        header = build_document_header(name)
        yield _part_event(header)
        parts: list[str] = [header]

        # 已定稿章节的正文，作为后续章节的上下文（含标题行）
        written: list[str] = []

        # 待确认清单：跨章节累加，最终随 `done` 事件交给编排层落库。
        # 每条的 id 由后端按「章节 + 序号」分配，保证全文档唯一、稳定。
        pending_items: list[dict[str, Any]] = []

        for index, section in enumerate(PRD_SECTIONS):
            section_template = template_sections.get(section.heading, "")

            # 每章一个抑制器：标记块可能被切在 chunk 边界，抑制器保证实时 chunk 流
            # 永远不含哨兵（正文定稿后仍会再 `extract` 一次，这里是双保险）。
            suppressor = PendingBlockSuppressor()

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
                    clean = suppressor.feed(delta)
                    if clean:
                        yield StreamEvent(type="chunk", content=clean)
            except Exception as exc:  # noqa: BLE001 - 流式接口需把异常转成事件
                yield error_event(exc)
                return
            trailing = suppressor.flush()
            if trailing:
                yield StreamEvent(type="chunk", content=trailing)
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
                            clean = suppressor.feed(delta)
                            if clean:
                                yield StreamEvent(type="chunk", content=clean)
                    except Exception as exc:  # noqa: BLE001
                        yield error_event(exc)
                        return
                    trailing = suppressor.flush()
                    if trailing:
                        yield StreamEvent(type="chunk", content=trailing)
                    section_text = "".join(buffer).strip()

            # 定稿后剥离标记块：正文落 `part` / `done.reply` / `documents.content` 三处
            # 天然不含哨兵；清单按本章节累加（id 前缀用章节 id，跨章节不重号）。
            section_text = _ensure_heading(section_text, section.heading)
            clean_text, items = extract(section_text, id_prefix=section.id)
            if items:
                pending_items.extend(items)
            if clean_text:
                # 加工后的定稿正文整段下发：前端的实时缓冲只见过未加工的原始
                # chunk（可能漏标题、章间没空行），这里把它替换成权威文本
                written.append(clean_text)
                part = f"{clean_text}\n\n"
                parts.append(part)
                yield _part_event(part, section=section, index=index, total=total)

        yield StreamEvent(
            type="stage",
            content="PRD 已生成",
            data={"stage": "done", "index": total, "total": total},
        )
        yield StreamEvent(
            type="done",
            content="",
            data={
                "doc_type": "prd",
                "reply": "".join(parts).strip(),
                "pending_items": pending_items,
            },
        )

    async def _stream_parallel(
        self,
        *,
        summary: dict[str, Any] | None = None,
        requirements: str = "",
        product_name: str = "",
        history: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """三车道并行：撰写严格按章序，审核 / 改写以章节任务并发执行。

        与串行版的关键差异：

        - **关闭 `chunk`**：多章同时流式会串字，也会让「整章重试」变得不安全
          （已交付的增量撤不回来）。前端只消费 `part` 定稿段与 `stage` 阶段。
        - **`part` 可乱序到达**：每段带 `index`，`done.reply` 由本方法按 index 装配；
          前端按 same index 归位，因此顺序不因到达先后而错乱。
        - **单章失败不终止全篇**：写占位继续，最后用 `partial` + `failed_sections` 标记。
        - **所有模型调用共用一个信号量**：并发上限来自 `PRD_PIPELINE_CONCURRENCY`，
          计量用 `use_step` 归因，避免多路互相污染。
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
            content=f"正在并行生成 {total} 章 PRD…",
            data={"doc_type": "prd", "total_sections": total, "parallel": True},
        )

        header = build_document_header(name)
        yield _part_event(header)
        parts: dict[int, str] = {-1: header}
        drafts: list[str] = [""] * total
        states = [_SectionState(index=i, section=s) for i, s in enumerate(PRD_SECTIONS)]

        queue: asyncio.Queue[StreamEvent | None] = asyncio.Queue()
        sem = asyncio.Semaphore(self._concurrency)
        tasks: set[asyncio.Task[None]] = set()

        async def put(event: StreamEvent) -> None:
            await queue.put(event)

        async def call_text(messages: list[BaseMessage], step: str) -> str:
            # ⚠️ 信号量只包一次模型调用：持有它再等别的章节任务会死锁。
            async with sem:
                with use_step(step):
                    return "".join([delta async for delta in self._stream_text(messages)])

        async def review_with_retry(
            section: PrdSection, section_text: str, prior_sections: str
        ) -> SectionReview | None:
            for attempt in range(self._max_retries + 1):
                try:
                    async with sem:
                        with use_step("reviewing"):
                            return await self._review(
                                section=section,
                                section_text=section_text,
                                writing_rules=writing_rules,
                                requirements=requirements_text,
                                prior_sections=prior_sections,
                            )
                except AppError:
                    # 预算超顶 / 墙钟硬顶：重来多少次都一样，直接走致命路径
                    raise
                except Exception:  # noqa: BLE001 - 单章审核失败可重试
                    if attempt >= self._max_retries:
                        logger.warning(
                            "章节 %s 审核连续失败，保留正文不判失败",
                            section.id,
                            exc_info=True,
                        )
                        return None
            return None

        async def section_task(idx: int, draft: str, prior: str) -> None:
            st = states[idx]
            review: SectionReview | None = None
            rewrites = 0
            if self._review_enabled:
                while True:
                    st.status = "reviewing"
                    await put(_section_stage_event("reviewing", st.section, idx, total))
                    review = await review_with_retry(st.section, draft, prior)
                    st.review = review
                    if review is None or not review.needs_revision:
                        break
                    if rewrites >= self._max_rewrites:
                        break
                    rewrites += 1
                    st.status = "rewriting"
                    await put(
                        _section_stage_event(
                            "rewriting",
                            st.section,
                            idx,
                            total,
                            note=review.summary(),
                            issues=review.issues,
                        )
                    )
                    try:
                        new_draft = await call_text(
                            self._rewrite_messages(
                                section=st.section,
                                section_template=template_sections.get(st.section.heading, ""),
                                system_prompt=system_prompt,
                                requirements=requirements_text,
                                conversation=conversation,
                                field_schema=artifacts["field_schema"],
                                prior_sections=prior,
                                review=review,
                            ),
                            "rewriting",
                        )
                    except AppError:
                        raise
                    except Exception:  # noqa: BLE001 - 改写失败保留改写前版本
                        logger.warning(
                            "章节 %s 改写失败，保留改写前版本", st.section.id, exc_info=True
                        )
                        break
                    if new_draft.strip():
                        draft = new_draft

            text = _ensure_heading(draft, st.section.heading)
            clean_text, items = extract(text, id_prefix=st.section.id)
            st.pending_items = items
            if clean_text:
                st.status = "done"
                st.part = f"{clean_text}\n\n"
            else:
                st.status = "failed"
                st.failed = True
                st.reason = "正文为空"
                st.part = _failed_placeholder(st.section, st.reason)
            parts[idx] = st.part
            await put(
                _part_event(
                    st.part,
                    section=st.section,
                    index=idx,
                    total=total,
                    failed=st.failed,
                )
            )

        async def generation_lane() -> None:
            try:
                for idx, section in enumerate(PRD_SECTIONS):
                    st = states[idx]
                    st.status = "writing"
                    await put(_section_stage_event("writing", section, idx, total))
                    prior = _join_prior([draft for draft in drafts[:idx] if draft])

                    draft = ""
                    last_exc: Exception | None = None
                    for attempt in range(self._max_retries + 1):
                        try:
                            draft = await call_text(
                                self._section_messages(
                                    section=section,
                                    section_template=template_sections.get(section.heading, ""),
                                    system_prompt=system_prompt,
                                    requirements=requirements_text,
                                    conversation=conversation,
                                    field_schema=artifacts["field_schema"],
                                    prior_sections=prior,
                                ),
                                "writing",
                            )
                            break
                        except AppError:
                            raise
                        except Exception as exc:  # noqa: BLE001 - 单章撰写失败可重试
                            last_exc = exc
                            if attempt >= self._max_retries:
                                break

                    if not draft.strip():
                        st.status = "failed"
                        st.failed = True
                        st.reason = (
                            f"撰写连续失败：{last_exc}" if last_exc else "撰写结果为空"
                        )
                        st.part = _failed_placeholder(section, st.reason)
                        parts[idx] = st.part
                        await put(
                            _section_stage_event(
                                "failed", section, idx, total, note=st.reason
                            )
                        )
                        await put(
                            _part_event(
                                st.part,
                                section=section,
                                index=idx,
                                total=total,
                                failed=True,
                            )
                        )
                        continue

                    drafts[idx] = draft
                    st.draft = draft
                    task = asyncio.create_task(section_task(idx, draft, prior))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)

                if tasks:
                    await asyncio.gather(*list(tasks))

                failed_sections = [
                    {
                        "section_id": st.section.id,
                        "title": st.section.title,
                        "index": st.index,
                        "reason": st.reason,
                    }
                    for st in states
                    if st.failed
                ]
                if len(failed_sections) == total:
                    raise RuntimeError("全部章节生成失败")

                issues: list[dict[str, Any]] = []
                for st in states:
                    if st.review and st.review.needs_revision:
                        for issue in st.review.issues:
                            issues.append(
                                {
                                    "section_id": st.section.id,
                                    "section_title": st.section.title,
                                    "severity": str(issue.get("severity") or "medium"),
                                    "detail": str(issue.get("detail") or ""),
                                }
                            )
                verdict = "revise" if issues else "pass"
                conclusion = (
                    f"{total - len(failed_sections)} 章完成，{len(issues)} 个待处理问题"
                )
                await put(
                    StreamEvent(
                        type="review",
                        content=conclusion,
                        data={
                            "kind": "prd",
                            "verdict": verdict,
                            "summary": conclusion,
                            "issues": issues,
                            "rewritten": False,
                            "failed_sections": failed_sections,
                        },
                    )
                )

                pending_items = [item for st in states for item in st.pending_items]
                reply = (
                    parts[-1]
                    + "".join(parts[i] for i in sorted(parts) if i >= 0)
                ).strip()
                await put(
                    StreamEvent(
                        type="stage",
                        content="PRD 已生成",
                        data={"stage": "done", "index": total, "total": total},
                    )
                )
                await put(
                    StreamEvent(
                        type="done",
                        content="",
                        data={
                            "doc_type": "prd",
                            "reply": reply,
                            "pending_items": pending_items,
                            "partial": bool(failed_sections),
                            "failed_sections": failed_sections,
                        },
                    )
                )
            except Exception as exc:  # noqa: BLE001 - 致命失败统一转 error 事件
                await put(error_event(exc))
            finally:
                await queue.put(None)

        driver = asyncio.create_task(generation_lane())
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield event
        finally:
            if not driver.done():
                driver.cancel()
            for task in list(tasks):
                task.cancel()
            await asyncio.gather(driver, *list(tasks), return_exceptions=True)

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
            pending_spec=render_spec(),
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
            pending_spec=render_spec(),
        )
        user_content += PRD_SECTION_REWRITE_APPENDIX.format(
            issues=review.issues_text(),
            instruction=review.instruction.strip() or "（未给出具体指令，请按上述问题自行修正）",
        )
        return _build_messages(system_prompt, user_content)

    # ------------------------------------------------------------------
    # 模型调用
    # ------------------------------------------------------------------

    def _stream_text(self, messages: list[BaseMessage]) -> AsyncIterator[str]:
        """流式产出一次调用的纯文本增量，结束时检查是否被截断。

        复用 `stream_reply.astream_text`：截断检测与文本归一化只有一份实现。
        """
        return astream_text(self.llm, messages)

    async def _collect(self, messages: list[BaseMessage]) -> str:
        """把流式回复收拢成完整文本（审核需要整体 JSON 才能解析）。"""
        return await collect_reply(self.llm, messages)

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


def _part_event(
    text: str,
    *,
    section: PrdSection | None = None,
    index: int = 0,
    total: int = 0,
    failed: bool = False,
) -> StreamEvent:
    """构造「一段正文已定稿」的 `part` 事件。

    `data.text` 是**可直接追加到文档末尾**的字符串——文档头自带结尾空行，
    章节正文也在这里补好章间分隔空行。把这些容易写岔的细节收在产出侧，
    是「前端预览与最终 `done.reply` 逐字一致」的前提。

    并行链路会额外带 `data.failed=true`：该章是占位文本，不是模型产出的正文。
    前端据此把它标红，但仍按 `index` 归位（顺序不因失败而错乱）。

    Args:
        text: 可直接追加的定稿文本。
        section: 所属章节；文档头等不属于任何章节的部分传 `None`。
        index: 章节下标（从 0 开始）；仅章节事件携带。
        total: 章节总数；仅章节事件携带。
        failed: 该段是否为失败占位。

    Returns:
        形如 `{"text": "…", "section_id": "features", …}` 的 `part` 事件。
    """
    data: dict[str, Any] = {"text": text}
    if section is not None:
        data.update(
            {
                "section_id": section.id,
                "section_title": section.title,
                "section_heading": section.heading,
                "index": index,
                "total": total,
            }
        )
    if failed:
        data["failed"] = True
    return StreamEvent(type="part", content="", data=data)


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

    `data.section_id` 标识当前章节，前端据此判定「是否换章、要不要重置实时缓冲」，
    必须稳定。改写（rewriting）时该章尚无 `part`，前端只需丢弃自己的缓冲即可，
    不需要按字符数回退。
    """
    labels = {
        "writing": "正在撰写",
        "reviewing": "正在审核",
        "rewriting": "正在改写",
        "failed": "生成失败",
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


