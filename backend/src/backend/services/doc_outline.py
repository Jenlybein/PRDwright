"""三份产物的大纲：从上游材料**抽取**结构，抽不到就标「待确认」。

票 #91「大纲扩到三份」。三份产物（PRD / 接口文档 / 提示词套件）在写正文之前都先出一份
大纲，告诉用户「接下来要写哪几节、每节的事实来自哪里」。

## 为什么是纯字符串抽取，而不是再调一次模型

「只抽取、不发明」这条是红线：大纲条目必须能追溯到上游材料。让模型写大纲，就把
「别编造」压在一句提示词上——它今天守规矩、明天不守，而产物里多一节没人写过的结构
是**看不出来**的。本模块因此**零 LLM 调用**：

1. 结构骨架来自产物自己的规范（PRD 的模板章节、接口文档与提示词套件的输出要求），
   这几节本来就是要写的，不是凭空来的；
2. 每节的内容条目一律从上游原文里**逐字摘**（标题行 / 列表项 / JSON 字段行），
   摘不到的节写「待确认」并说明为什么；
3. 抽出后过一次 `verify_outline` 后校验：上游里查不到出处的锚点作废，条目随之降级为
   「待确认」——即便将来有别的途径塞条目进来，也过不了这一道。

## 三份来源

| 产物 | 上游材料 | 骨架 |
|---|---|---|
| PRD | 需求输入（结构化摘要 / 自由文本） | 技能包 `prd-template.md` 的章节 |
| 接口文档 | PRD 的接口清单 | 生成提示词的输出要求（概述 / 错误码 / 接口列表 / 数据模型 / 附录） |
| 提示词套件 | PRD（+ 可选的接口文档） | 技能包 `prompts-guide.md` 的输出要求 |

下游只从上游取：接口文档不读接口文档（那是它自己的产物），提示词不发明第三份材料。

## 降级

抽不出可追溯结构（上游空白、骨架缺失、一条都摘不到）时 `build_outline` 返回 `None`，
调用方把它当成「这次没有大纲」直接进入撰写——大纲是锦上添花，不能因为它把链路卡住。

本模块不碰数据库、不碰 `api/`、不发事件：事件由调用方按既有 `stage` / `stage_chunk`
通道下发。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Iterable, Iterator

from backend.core.contracts import StreamEvent
from backend.services.doc_skeleton import normalize_heading

logger = logging.getLogger(__name__)

# 待确认标记：与待确认清单（`pending_items`）、写作规范里的「待确认」是**同一个词**。
# 大纲里的未决项必须用它，界面与正文才是一套口径（「待定」「缺失」这类变体一律不用）。
PENDING_MARK = "待确认"

# 产物显示名（只用于大纲标题；不参与事件字段）
_KIND_LABELS: dict[str, str] = {
    "prd": "PRD",
    "api_docs": "接口文档",
    "prompts": "提示词套件",
}

# 结构骨架：接口文档与提示词套件的输出要求。
#
# ⚠️ 这两份元组是**提示词里「输出要求」那一节的目录**，不是另起一套口径：
# `tests/test_doc_outline.py` 有一条守卫逐项断言它们在对应的生成提示词 / 技能包文本里
# 真的存在。改输出要求时这里要一起改，否则大纲会预报出产物里没有的节。
API_DOCS_STRUCTURE: tuple[str, ...] = ("概述", "错误码约定", "接口列表", "数据模型", "附录")
PROMPTS_STRUCTURE: tuple[str, ...] = (
    "提示词设计原则",
    "提示词清单",
    "变量约定",
    "调优建议",
)

# 每节的关键词：判定「上游这一行属于哪一节」。
# 只做归类，不做改写——命中的行原样成为大纲条目。
#
# ⚠️ 别往里塞过泛的字（`页` / `用` / `值`）：它们会把「分页上限」判成「附录」、
# 把「用户登录」判成「概述」，越是常见的字越容易把条目抢到错的节上。
_SECTION_HINTS: dict[str, tuple[str, ...]] = {
    # --- 接口文档 ---
    "概述": ("概述", "定位", "背景", "目标", "用户", "平台", "产品", "风格"),
    "错误码约定": ("错误", "异常", "失败", "校验", "错误码"),
    "接口列表": ("接口", "api", "登录", "注册", "查询", "创建", "导出", "上传", "列表", "功能"),
    "数据模型": ("数据", "字段", "实体", "模型", "表", "分类", "金额"),
    "附录": ("分页", "时间", "格式", "约束", "兼容", "性能", "安全", "上限"),
    # --- 提示词套件 ---
    "提示词设计原则": ("原则", "设计", "约束", "目标", "用户", "场景", "风格", "背景"),
    "提示词清单": ("场景", "功能", "流程", "接口", "页面", "api", "创建", "列表", "登录"),
    "变量约定": ("变量", "字段", "占位", "参数"),
    "调优建议": ("调优", "风险", "问题", "限制", "性能", "建议", "兼容"),
}

# 需求摘要的**字段名 → PRD 章节**：JSON 上游靠路径归类，不靠猜正文的措辞。
# 这张表就是 `prd-generator` 的字段契约（`field-schema.json`）在「这一节写什么」上的读法。
_FIELD_SECTION_HINTS: dict[str, str] = {
    "product_name": "产品概述",
    "product_goal": "产品概述",
    "target_users": "产品概述",
    "platform": "产品概述",
    "mvp_features": "功能需求",
    "v2_features": "功能需求",
    "ui_pages": "页面设计",
    "technical_constraints": "技术规格",
    "non_functional": "非功能需求",
    "scope": "项目范围",
}

# 「这行像不像一个接口条目」：功能编号 / HTTP 方法 + 路径 / REST 路径。
# 命中就强制归到「接口清单」那一节——这是「接口文档大纲来自 PRD 的接口清单」最硬的一条。
_INTERFACE_LINE_RE = re.compile(
    r"(?:\bF-?\d+\b|(?:GET|POST|PUT|PATCH|DELETE)\s+/|\b/api/v?\d*/)", re.IGNORECASE
)

# PRD 的模板章节没有固定关键词表，按章节名归一到一组提示词。
# 归一用 `doc_skeleton.normalize_heading`（去 `#`、去前导序号、去标点、转小写）。
_PRD_SECTION_HINTS: dict[str, tuple[str, ...]] = {
    "产品概述": ("产品", "名称", "目标", "用户", "平台", "背景", "问题"),
    "功能需求": ("功能", "mvp", "需求", "能力", "特性", "p0", "p1", "p2"),
    "页面设计": ("页面", "界面", "流程", "交互", "状态", "页"),
    "技术规格": ("技术", "架构", "依赖", "存储", "数据库", "接口", "实现", "鉴权"),
    "非功能需求": ("性能", "安全", "兼容", "可用", "合规", "并发", "秒", "指标"),
    "项目范围": ("范围", "scope", "不做", "边界", "里程碑", "计划", "交付"),
}

# 每节最多几条锚点：再多就不是「先定了哪些结构」，而是一份摘要。
# 取 6 是为了让**两份上游材料**（提示词的 PRD + 接口文档）在一节里都还有位置：
# 只要 4 条的话，先遍历到的 PRD 功能条目会把「接口文档里的接口条目」全部挤掉。
_MAX_ANCHORS_PER_SECTION = 6
# 单条锚点在面板里的显示长度上限（**只截显示、不截锚点本身**：追溯必须逐字）。
_DISPLAY_LIMIT = 90

# 上游里没有可摘内容的行（纯符号 / 代码围栏 / JSON 括号）
_SKIP_LINE_RE = re.compile(r"^(?:```|~~~|[\[\]{}()<>,;:|\-*#=]+)$")
# JSON 行里的键名太吵，但不丢：它本身也是可追溯的上游原文
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class OutlineEntry:
    """大纲里的一节：结构名 + 上游锚点（或「待确认」）。

    Attributes:
        heading: 结构名（骨架里的那一节，如 `接口列表`）。
        anchors: 从上游原文**逐字摘**的条目；待确认时为空元组。
        is_pending: 是否抽不出（真 = 这一节标「待确认」，不补写）。
        reason: 待确认原因（写给人看，说明缺什么）。
    """

    heading: str
    anchors: tuple[str, ...] = ()
    is_pending: bool = False
    reason: str = ""


@dataclass(frozen=True)
class Outline:
    """一份产物的大纲。

    Attributes:
        doc_kind: `prd` / `api_docs` / `prompts`。
        source_note: 来源说明（写清这条大纲是从哪些上游材料摘的）。
        entries: 按骨架顺序排列的节。
    """

    doc_kind: str
    source_note: str
    entries: tuple[OutlineEntry, ...] = field(default_factory=tuple)

    @property
    def pending_count(self) -> int:
        """待确认节数（回执 / 测试用；不参与交付判据）。"""
        return sum(1 for entry in self.entries if entry.is_pending)

    @property
    def anchor_count(self) -> int:
        """可追溯锚点总数（0 表示这份大纲其实什么都没抽到）。"""
        return sum(len(entry.anchors) for entry in self.entries)


def normalize_text(text: str) -> str:
    """归一化比较用的文本：连续空白压一格、去首尾、转小写。

    只用于「这句话在上游里有没有」的判断，不用于展示；中文没有大小写，
    英文关键词（`API` / `MVP`）靠转小写互相命中。
    """
    return _WS_RE.sub(" ", (text or "").strip()).lower()


def is_traceable(anchor: str, upstream: str) -> bool:
    """锚点是否真的出自上游原文（归一化后做子串判断）。

    Args:
        anchor: 候选锚点（应逐字摘自上游）。
        upstream: 上游材料全文。

    Returns:
        `True` 表示这串内容在上游里找得到，可以算「抽取」而不是「发明」。
    """
    target = normalize_text(anchor)
    return bool(target) and target in normalize_text(upstream)


def verify_outline(entries: Iterable[OutlineEntry], upstream: str) -> tuple[OutlineEntry, ...]:
    """后校验：上游里查不到出处的锚点一律作废；条目随之降级为「待确认」。

    这是「只抽取不发明」的第二道闸。第一道是 `build_outline` 根本不写自由文本，
    但闸门不能只有一道——只要走到这一步的条目在材料里找不到原文，就不许以
    「已定结构」的形态出现。

    Args:
        entries: 候选节。
        upstream: 上游材料全文（多份材料拼接后传入）。

    Returns:
        校验后的节：保留可追溯锚点；原本有锚点却被全部作废的节转为待确认。
    """
    verified: list[OutlineEntry] = []
    for entry in entries:
        kept = tuple(anchor for anchor in entry.anchors if is_traceable(anchor, upstream))
        if kept or entry.is_pending:
            verified.append(
                OutlineEntry(
                    heading=entry.heading,
                    anchors=kept,
                    is_pending=entry.is_pending,
                    reason=entry.reason,
                )
            )
            continue
        verified.append(
            OutlineEntry(
                heading=entry.heading,
                is_pending=True,
                reason=f"上游材料里查不到这一节的出处（{PENDING_MARK}）",
            )
        )
    return tuple(verified)


def build_outline(
    doc_kind: str,
    *,
    upstream: str,
    secondary: str = "",
    structure: Iterable[str] | None = None,
) -> Outline | None:
    """按产物类型从上游材料抽取一份大纲；抽不出时返回 `None`（由调用方降级）。

    Args:
        doc_kind: 产物类型（`prd` / `api_docs` / `prompts`）。
        upstream: 主要上游材料（PRD 的需求输入 / 接口文档的上游 PRD / 提示词的 PRD）。
        secondary: 第二份上游材料（仅提示词用：接口文档正文）。
        structure: 结构骨架；PRD 由调用方传入「即将写入的模板章节」，其余两份不传
            （默认取本模块的结构常量）。

    Returns:
        `Outline`；上游空白、骨架为空、或一条锚点都摘不到时返回 `None`
        ——全空的「大纲」没有信息，还会让阶段条看起来像走过了这一步。
    """
    if not (upstream or "").strip():
        return None

    skeleton = tuple(structure) if structure is not None else _default_structure(doc_kind)
    if not skeleton:
        return None

    combined = "\n".join(part for part in (upstream, secondary) if (part or "").strip())
    entries = _assign_anchors(doc_kind, skeleton, upstream, secondary)
    if not any(entry.anchors for entry in entries):
        return None

    verified = verify_outline(entries, combined)
    return Outline(
        doc_kind=doc_kind,
        source_note=_source_note(doc_kind, has_secondary=bool((secondary or "").strip())),
        entries=verified,
    )


def render_outline(outline: Outline | None) -> str:
    """把大纲渲染成阶段输出面板里那份纯文本（Markdown 风格、逐节列条目）。

    Args:
        outline: 大纲；`None` 返回空串（调用方据此不下发 `stage_chunk`）。

    Returns:
        形如「标题 + 来源说明 + 每节若干 `· 出处条目`」的文本；
        抽不出的节写「待确认（原因）」。
    """
    if outline is None:
        return ""
    label = _KIND_LABELS.get(outline.doc_kind, "文档")
    lines = [
        f"# {label}大纲",
        f"> 来源：{outline.source_note}。条目逐字摘自上游材料，"
        f"抽不到的结构标「{PENDING_MARK}」，不补写。",
    ]
    for entry in outline.entries:
        if entry.is_pending:
            reason = entry.reason or f"上游材料未给出与「{entry.heading}」相关的内容"
            lines.append(f"- {entry.heading}：{PENDING_MARK}（{reason}）")
            continue
        lines.append(f"- {entry.heading}")
        lines.extend(f"    · {_clip(anchor)}" for anchor in entry.anchors)
    return "\n".join(lines)


def outline_events(
    doc_kind: str,
    *,
    upstream: str,
    doc_type: str,
    secondary: str = "",
    structure: Iterable[str] | None = None,
) -> Iterator[StreamEvent]:
    """把大纲阶段发成既有事件：`stage(outlining)` + 一条 `stage_chunk(outlining)`。

    三份产物的接线都走这里，事件对外形状与撰写 / 审核阶段**完全一样**（只多一个
    `stage` 取值 `outlining`）——前端因此不需要第二条轨道。

    抽不出大纲（或抽取异常）时**只发一条说明性的 `stage(outlining)`**，随后由调用方
    照常发 `stage(writing)`：**不发 `error`**。大纲是锦上添花，它没成不该让任务判失败，
    也不该让阶段条停在一个看起来出错的位置。

    Args:
        doc_kind: 产物类型。
        upstream: 主要上游材料。
        doc_type: 事件里的产物字段（`prd` / `api_doc` / `prompts`）。
        secondary: 第二份上游材料（仅提示词用）。
        structure: 结构骨架（PRD 由调用方传入即将写入的模板章节）。

    Yields:
        `stage(outlining)`、可选的一条 `stage_chunk(outlining)`。
    """
    outline: Outline | None = None
    try:
        outline = build_outline(
            doc_kind, upstream=upstream, secondary=secondary, structure=structure
        )
    except Exception:  # noqa: BLE001 - 大纲失败一律降级，绝不因此中断生成
        logger.warning(
            "大纲抽取异常，本次直接撰写正文",
            exc_info=True,
            extra={"event": "outline.failed", "doc_kind": doc_kind},
        )

    if outline is None:
        logger.warning(
            "上游材料抽不出可追溯的结构，本次直接撰写正文",
            extra={"event": "outline.degraded", "doc_kind": doc_kind},
        )
        yield StreamEvent(
            type="stage",
            content="上游材料抽不出可追溯的结构，直接开始撰写正文…",
            data={"stage": "outlining", "doc_type": doc_type},
        )
        return

    yield StreamEvent(
        type="stage",
        content="正在从上游材料抽取大纲…",
        data={"stage": "outlining", "doc_type": doc_type},
    )
    text = render_outline(outline)
    if text:
        yield StreamEvent(
            type="stage_chunk",
            content=text,
            data={"stage": "outlining", "doc_type": doc_type},
        )
    logger.info(
        "大纲抽取完成",
        extra={
            "event": "outline.done",
            "doc_kind": doc_kind,
            "entries": len(outline.entries),
            "pending": outline.pending_count,
            "anchors": outline.anchor_count,
        },
    )


def _default_structure(doc_kind: str) -> tuple[str, ...]:
    """取该产物的默认骨架；未知产物类型返回空（调用方随之降级）。"""
    if doc_kind == "api_docs":
        return API_DOCS_STRUCTURE
    if doc_kind == "prompts":
        return PROMPTS_STRUCTURE
    return ()


def _source_note(doc_kind: str, *, has_secondary: bool) -> str:
    """来源说明：如实写清这份大纲是从哪几份上游材料抽的。"""
    if doc_kind == "api_docs":
        return "PRD 的接口清单"
    if doc_kind == "prompts":
        if has_secondary:
            return "PRD 的功能与场景 + 接口文档的接口条目"
        return "PRD 的功能与场景（本次未提供接口文档）"
    return "需求输入 + 技能包模板章节"


def _assign_anchors(
    doc_kind: str,
    skeleton: Iterable[str],
    upstream: str,
    secondary: str,
) -> tuple[OutlineEntry, ...]:
    """把上游的每一行归到骨架里的一节；归不上的行不入大纲。

    判据三层，从硬到软：

    1. **接口条目**（功能编号 / HTTP 方法 + 路径 / `/api/…`）→ 强制归「接口清单」那节；
    2. **结构化摘要的字段名**（`mvp_features` / `ui_pages` …）→ 按字段契约归到对应章节；
    3. 其余按关键词命中数归类，命中相同时取骨架里靠前的节（稳定、可预期）。

    一节一条都归不上 → 该节写「待确认」。待确认**不是**空实现：它明确告诉用户
    「这一节要么补上游材料、要么就空着」，而不是替他编一段。
    """
    sections = tuple(skeleton)
    hints = _hints_for(doc_kind, sections)
    # 接口条目该落到哪一节：接口文档是「接口清单」，提示词套件是「提示词清单」
    interface_section = {
        "api_docs": "接口列表",
        "prompts": "提示词清单",
    }.get(doc_kind, "")

    assigned: dict[str, list[str]] = {section: [] for section in sections}
    pairs = _candidate_pairs(upstream) + _candidate_pairs(secondary)

    def place(candidate: str, winner: str) -> None:
        if not winner:
            return
        bucket = assigned[winner]
        if candidate in bucket or len(bucket) >= _MAX_ANCHORS_PER_SECTION:
            return
        bucket.append(candidate)

    # 两趟：先收**最硬的结构证据**（功能编号 / HTTP 方法 + 路径），再按关键词归类。
    # 反过来会让「功能需求」这类泛条目先把「接口清单」的坑位占满，
    # 于是接口文档的大纲里一个接口路径都没有——那正是这份大纲最该说清的东西。
    for candidate, _context in pairs:
        if interface_section and _INTERFACE_LINE_RE.search(candidate):
            place(candidate, interface_section)

    for candidate, context in pairs:
        if interface_section and _INTERFACE_LINE_RE.search(candidate):
            continue  # 第一趟已经处理过
        winner = _classify(doc_kind, sections, hints, candidate, context, interface_section)
        place(candidate, winner)

    entries: list[OutlineEntry] = []
    for section in sections:
        anchors = tuple(assigned[section])
        if anchors:
            entries.append(OutlineEntry(heading=section, anchors=anchors))
            continue
        entries.append(
            OutlineEntry(
                heading=section,
                is_pending=True,
                reason=f"上游材料未给出与「{section}」相关的内容",
            )
        )
    return tuple(entries)


def _classify(
    doc_kind: str,
    sections: tuple[str, ...],
    hints: dict[str, tuple[str, ...]],
    candidate: str,
    context: str,
    interface_section: str,
) -> str:
    """判定一行上游内容属于哪一节；判不出来返回空串（该行不入大纲）。"""
    if interface_section and interface_section in sections and _INTERFACE_LINE_RE.search(candidate):
        return interface_section

    # 结构化摘要：路径里的字段名就是「这一段属于哪一章」的权威说法
    if doc_kind == "prd" and context:
        for key in context.split():
            mapped = _FIELD_SECTION_HINTS.get(key)
            if mapped:
                # 章节名带模板序号（`2. 功能需求`），按归一化后的名字对上
                for section in sections:
                    if normalize_heading(section) == normalize_heading(mapped):
                        return section
                return ""

    scores = {
        section: _hit_count(candidate, hints.get(section, ()))
        + _hit_count(context, hints.get(section, ()))
        for section in sections
    }
    best = max(scores.values(), default=0)
    if best <= 0:
        return ""
    # 命中数相同时取骨架靠前的那一节（不能用 dict 的插入顺序去赌）
    return next(section for section in sections if scores[section] == best)


def _hints_for(doc_kind: str, sections: Iterable[str]) -> dict[str, tuple[str, ...]]:
    """取每节的关键词。

    PRD 的章节名来自技能包模板（`1. 产品概述` 这类），先归一去序号再查表；
    表里没有的章节退回「章节名本身的关键词」——模板新增章节时不会静默失配。
    """
    hints: dict[str, tuple[str, ...]] = {}
    for section in sections:
        if doc_kind == "prd":
            key = normalize_heading(section)
            hints[section] = _PRD_SECTION_HINTS.get(key) or tuple(
                word for word in (key,) if word
            )
        else:
            hints[section] = _SECTION_HINTS.get(section, (section,))
    return hints


def _candidate_pairs(text: str) -> list[tuple[str, str]]:
    """上游材料里「可以当条目」的行 + 它的**路径上下文**（`(行, 上下文)`）。

    纯文本材料的上下文是空串；**结构化摘要**（JSON）的上下文是这行数据在 JSON 里的
    字段路径（`mvp_features name`）——于是「`"name": "结构化录入",` 这一行属于功能需求」
    有据可依，不必靠猜正文措辞。

    刻意不复用 `doc_skeleton.skeletonize`：那是给审核用的压缩器，会丢掉不含数字的
    普通段落，还会把 JSON 代码块整块丢弃——而需求摘要恰恰是 JSON，丢的是事实本身。
    """
    json_pairs = _json_candidate_pairs(text)
    if json_pairs is not None:
        return json_pairs
    return [(line, "") for line in _candidate_lines(text)]


def _candidate_lines(text: str) -> list[str]:
    """上游材料里「可以当条目」的行：整行保留、跳过纯符号与空行。"""
    if not (text or "").strip():
        return []
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or _SKIP_LINE_RE.match(line):
            continue
        # 锚点**不截断**：追溯要求逐字命中上游，截断过的串就不再是「摘录」了。
        # 显示长度由 `_clip` 在渲染时处理。
        if line not in lines:
            lines.append(line)
    return lines


def _json_candidate_pairs(text: str) -> list[tuple[str, str]] | None:
    """JSON 形态的上游材料：每条标量值 → `(它所在的行, 字段路径)`。

    不是 JSON（或解析失败）返回 `None`，由调用方退回纯文本抽取。锚点仍是**上游原文的
    整行**（逐字可追溯），路径只用来归类。

    兼容两种形态：上游本身就是 JSON（`json.dumps` 的摘要），以及
    `format_summary_as_requirements` 产出的「说明 + ```json 块」——后者解析的是代码块，
    但锚点仍从**整份上游原文**里按行找，所以引用是逐字的。
    """
    if not (text or "").strip():
        return []
    payload = _json_payload(text)
    if payload is None:
        return None

    lines = text.splitlines()
    flattened: list[tuple[str, str]] = []

    def walk(node: object, keys: tuple[str, ...]) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, (*keys, str(key)))
            return
        if isinstance(node, list):
            for item in node:
                walk(item, keys)
            return
        if not isinstance(node, str) or not node.strip():
            return
        encoded = json.dumps(node, ensure_ascii=False)
        # 该值在原文里的位置：找**第一行**含它的行；找不到就丢弃（不能伪造出处）
        for raw in lines:
            if encoded in raw:
                line = raw.strip()
                if line and line not in {item[0] for item in flattened}:
                    flattened.append((line, " ".join(keys)))
                return

    walk(payload, ())
    return flattened


# 上游里的 JSON 代码块（`format_summary_as_requirements` 的产物形态）
_JSON_FENCE_RE = re.compile(r"```(?:json)?[ \t]*\n(.*?)```", re.DOTALL)


def _json_payload(text: str) -> object | None:
    """从上游材料里取 JSON 载荷：整份就是 JSON，或含一个 ```json 代码块。"""
    source = (text or "").strip()
    if not source:
        return None
    for candidate in (source,) + tuple(
        match.group(1) for match in _JSON_FENCE_RE.finditer(source)
    ):
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, (dict, list)):
            return parsed
    return None


def _hit_count(candidate: str, hints: tuple[str, ...]) -> int:
    """这一行命中了几个关键词（大小写不敏感；只数命中个数，不做权重）。"""
    lowered = candidate.lower()
    return sum(1 for hint in hints if hint and hint in lowered)


def _clip(text: str) -> str:
    """单条锚点的显示截断（**只截展示**：锚点本身保持逐字，追溯不受影响）。"""
    return text if len(text) <= _DISPLAY_LIMIT else f"{text[:_DISPLAY_LIMIT].rstrip()}…"
