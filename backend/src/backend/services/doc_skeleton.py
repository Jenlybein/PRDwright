"""把一篇 Markdown 压成「可核对事实」的骨架，以及按章节切分 / 拼回。

为什么需要它：审核 Agent 的输入里有两份长文本——上游材料（`requirements`）与
前序章节（`prior_sections`）。它们原本是**整篇原样注入**的，于是审核一次调用要
prefill 掉整篇 PRD，而 PRD 又是分章节生成的：第 k 章的审核输入含前 k−1 章全文，
整篇是 O(N²)。

但审核真正要核对的东西很窄——「有没有编造」「有没有漏」「编号口径一不一致」。
它需要的是**事实索引**，不是散文。本模块把正文压成「标题树 + 表格首列 + 编号条目
+ 含数字的句」，通常能砍掉一半以上的字符，且**不需要任何额外的 LLM 调用**。

同一个模块也放「按 `## ` 切分 / 拼回」，因为分节改写要用它：改写只重写受影响的
章节，拼回全文后再整段覆盖预览。

只依赖标准库与 `pending_items.collapse_blank_lines`，不碰 `api/`、不碰供应商 SDK。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from backend.services.pending_items import collapse_blank_lines

# 各级标题行
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", re.MULTILINE)
# 代码块围栏的起止（``` 或 ```json）
_FENCE_RE = re.compile(r"^[ \t]*```")
# 表格分隔行：`|---|---|` 或 `| --- |`
_TABLE_DELIM_RE = re.compile(r"^[ \t]*\|?[ \t]*:?-{2,}:?[ \t]*(\|[ \t]*:?-{2,}:?[ \t]*)*\|?[ \t]*$")
# 表格数据行：`| a | b |`
_TABLE_ROW_RE = re.compile(r"^[ \t]*\|(.+)\|[ \t]*$")
# 列表条目：`- x` / `* x` / `1. x` / `1) x` / `1、x`
_BULLET_RE = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)、])[ \t]+(.+)$")
# 含显式数字（可核对的事实：99.9% / < 200ms / 2026-10-21）
_DIGIT_RE = re.compile(r"\d")
# 标题归一化时要抹掉的字符（空格 / 全角空格 / 常见标点）
_NOISE_RE = re.compile(r"[\s　:：,，.。、\-—*`()（）【】\[\]《》]")
# 标题行前导的序号：`1. ` / `1) ` / `1、 `
_LEADING_NO_RE = re.compile(r"^[0-9]+([.)、]\s*)?")

# 骨架的字符硬顶。超过就截断——宁可让审核少看一点，也不要把 prefill 拖到几十秒。
DEFAULT_MAX_CHARS = 6000
# 单行最多保留多少字符（列表条目 / 含数字的行）
_KEEP_CHARS = 80


def skeletonize(
    text: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    heading_levels: int = 3,
) -> str:
    """把正文压成「可核对事实」骨架。

    五条保真规则（顺序即优先级）：

    1. `#`~`###` 标题**整行保留**——产品名、章节名、接口名都在这里；
    2. 表格行**整行保留**（截断到 `_KEEP_CHARS`），分隔行丢弃。
       只留首列是错的：`F-01` 在首列，但接口路径、字段名这类最该核对的事实
       往往在后面的列；
    3. `-` / `*` / `1.` 列表条目整行保留，截断到 `_KEEP_CHARS`——编号与短指标；
    4. 普通段落**只有含显式数字的行才留**（`99.9%`、`< 200ms`、`2026-10-21`）——
       可核对事实的主要载体，同时也是正文里最啰嗦的部分；
    5. 代码块只留围栏首行（语言标签 / 文件名），块体丢弃——接口文档里的 JSON 极长。

    总量超过 `max_chars` 时截断，并**显式写出省略了多少字符**：让审核 Agent 知道
    「后面还有」，而不是以为文档就这么短——后者会直接导致「覆盖缺失」的误报。

    Args:
        text: 待压缩的正文（Markdown）。
        max_chars: 骨架的字符硬顶；`0` 表示不截断。
        heading_levels: 保留到第几级标题（1 = 只留 `#`，3 = 留到 `###`）。

    Returns:
        骨架文本。输入为空时返回一句显式说明（不是空串——提示词里留空会被模型
        读成「没有上游材料」，与 ADR-020 要消灭的静默降级是同一类问题）。
    """
    if not (text or "").strip():
        return "（内容为空）"

    out: list[str] = []
    in_fence = False
    for raw_line in (text or "").splitlines():
        line = raw_line.rstrip()
        if _FENCE_RE.match(line):
            # 规则 5：围栏本身留一行（带语言标签），块体整体丢弃
            in_fence = not in_fence
            if in_fence:
                out.append(line.strip())
            continue
        if in_fence:
            continue
        if not line.strip():
            out.append("")
            continue

        heading = _HEADING_RE.match(line)
        if heading and len(heading.group(1)) <= heading_levels:
            out.append(line.strip())  # 规则 1
            continue
        if _TABLE_DELIM_RE.match(line):
            continue
        row = _TABLE_ROW_RE.match(line)
        if row:
            cells = [cell.strip() for cell in row.group(1).split("|")]
            # 分隔行偶尔会被当成数据行混进来（`|---|---|` 的单元格全是 `-`）
            if cells and not set(cells[0]) <= set("-: "):
                # 保留**整行**而不是只留首列：接口路径、字段名这类可核对的事实
                # 往往不在首列，只留首列等于把最该核对的东西丢掉了。
                out.append(f"- {_truncate(' | '.join(cell for cell in cells if cell))}")
            continue
        bullet = _BULLET_RE.match(line)
        if bullet:
            out.append(f"- {_truncate(bullet.group(1).strip())}")  # 规则 3
            continue
        if _DIGIT_RE.search(line):
            out.append(_truncate(line.strip()))  # 规则 4

    result = collapse_blank_lines("\n".join(out))
    if max_chars and len(result) > max_chars:
        omitted = len(text or "") - max_chars
        result = f"{result[:max_chars].rstrip()}\n\n（骨架已截断，后面还有约 {max(omitted, 0)} 字符未展示）"
    return result


def extract_headings(text: str, *, level: int = 2) -> list[str]:
    """抽取**指定级别**的标题行原文，作为审核 `where` 的**取值域**。

    让模型从这份清单里挑，而不是自由发挥写 `where`——后者经常写出文档里不存在的
    章节名，定位就无从谈起。

    Args:
        text: 正文。
        level: 只要第几级标题。`2`（默认）只取 `## `——H1 是文档头，不属于任何
            一节，把它算进来会让 `where` 取值域混进一个不该被改写的目标。

    Returns:
        标题行原文的列表（形如 `## 2. 接口列表`）。
    """
    return [
        match.group(0).strip()
        for match in _HEADING_RE.finditer(text or "")
        if len(match.group(1)) == level
    ]


@dataclass(frozen=True)
class DocSection:
    """一节的标题行与正文（正文不含标题行本身）。"""

    heading: str
    body: str


def split_sections(text: str, *, level: int = 2) -> tuple[str, list[DocSection]]:
    """按**指定级别**的标题切分正文。

    `preamble`（第一个标题之前的内容）**必须保留**：接口文档 / 提示词套件的 H1 与
    前言都在这里，丢掉会让拼回的全文缺头。

    Args:
        text: 正文。
        level: 按第几级标题切。`2`（默认）按 `## ` 切，此时 H1 与它下面的前言
            一起落在 `preamble` 里——正是我们要的。

    Returns:
        `(preamble, sections)`；文档里没有对应级别的标题时 `sections` 为空列表。
    """
    source = text or ""
    matches = [
        match for match in _HEADING_RE.finditer(source) if len(match.group(1)) == level
    ]
    if not matches:
        return source, []

    preamble = source[: matches[0].start()]
    sections: list[DocSection] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        sections.append(
            DocSection(heading=match.group(0).strip(), body=source[match.end() : end].strip("\n"))
        )
    return preamble, sections


def join_sections(preamble: str, sections: list[DocSection]) -> str:
    """把 `split_sections` 的结果拼回全文（每个节补回它自己的标题行）。"""
    blocks = [(preamble or "").rstrip("\n")]
    blocks += [f"{section.heading}\n\n{section.body}".rstrip("\n") for section in sections]
    return collapse_blank_lines("\n\n".join(block for block in blocks if block.strip())) + "\n"


def normalize_heading(text: str) -> str:
    """归一化标题用于比较：去 `#`、去前导序号、去空白与常见标点、转小写。

    模型写 `where` 时常常少带 `##`、多带序号、或只写章节名的一半，逐字相等是
    匹配不上的；归一化后既能精确比，也能做「互相包含」的宽松匹配。
    """
    stripped = (text or "").strip().lstrip("#").strip()
    stripped = _LEADING_NO_RE.sub("", stripped)
    return _NOISE_RE.sub("", stripped).lower()


def resolve_where(where: str, sections: list[DocSection]) -> DocSection | None:
    """把审核给的 `where` 映射到实际章节；匹配不上返回 `None`（由调用方降级）。

    两级匹配：① 归一化后精确相等；② 互相包含（模型常少带 / 多带前后缀）。
    **绝不做模糊到「猜一个最近的」** —— 定位错了会改写错误的章节，比不改写更糟。
    """
    target = normalize_heading(where)
    if not target:
        return None
    for section in sections:
        if target == normalize_heading(section.heading):
            return section
    for section in sections:
        normed = normalize_heading(section.heading)
        if target and normed and (target in normed or normed in target):
            return section
    return None


def strip_heading(text: str, heading: str) -> str:
    """去掉正文开头的标题行（模型常把标题行再输出一遍）。

    不去掉的话 `join_sections` 会再加一次，标题就重复了。只处理**开头**的那一处：
    正文中间出现同名标题通常是子结构，不该动。
    """
    body = (text or "").strip()
    first_line = body.splitlines()[0].strip() if body else ""
    if first_line and normalize_heading(first_line) == normalize_heading(heading):
        return body.split("\n", 1)[1].strip() if "\n" in body else ""
    return body


def _truncate(text: str) -> str:
    """单行截断到 `_KEEP_CHARS`，超长时加省略号。"""
    return text if len(text) <= _KEEP_CHARS else f"{text[:_KEEP_CHARS].rstrip()}…"


def sections_field_of(item: dict[str, Any]) -> list[str]:
    """取清单项里的 `sections` 字段（模型可能填成字符串或列表）。"""
    raw = item.get("sections")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(entry) for entry in raw]
    return []
