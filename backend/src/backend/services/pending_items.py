"""待确认清单：标记块规范、剥离、流式抑制、提示词说明。

待确认清单是「一次生成」产出的**第一类元数据**：它和正文来自同一次模型调用，
因此「剥离标记块、解析清单、随正文一起落库」三者必须在同一处完成——结构上
没有「换了正文却忘了重算清单」的落脚点（见设计文档）。

为什么用 HTML 注释式哨兵
------------------------
标记块形如 ``<!-- PENDING:BEGIN --> ... <!-- PENDING:END -->``。即使抑制逻辑有漏、
块体漏到了前端，Markdown 渲染里也**不可见**，不会把半截 JSON 当成「正文」闪现出来。
这是与「审核失败按通过处理」「生成失败不覆写」同一套「降级不污染产物」口径的延伸。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# 标记块哨兵：用 HTML 注释包裹，渲染时不可见。
BEGIN_SENTINEL = "<!-- PENDING:BEGIN -->"
END_SENTINEL = "<!-- PENDING:END -->"

# 清单项允许从模型输出里保留的字段（其余一律丢弃；尤其是不信任模型给的 `id`）。
_ITEM_FIELDS = ("question", "sections", "decider", "importance")

# 待确认项的「重要性等级」，数字越小越重要。
# 七个等级由模型在生成正文时随清单一起给出；澄清阶段据此逐级推进，
# 保证「越重要越先问」。
#
# ⚠️ **没有兜底等级**：等级缺失或取值不合法时，该项**不通过校验、直接丢弃**
# （见 `_normalize_importance` 与 `extract`）。不替模型猜一个默认档，是因为猜出来的
# 等级既会打乱「越重要越先问」的顺序，又会把一条没标级的问题伪装成已分级——
# 静默的错误排序比少一条问题更难被发现。
IMPORTANCE_ORDER: dict[str, int] = {
    "P0": 0,
    "P1": 1,
    "P2": 2,
    "P3": 3,
    "P4": 4,
    "P5": 5,
    "P6": 6,
}


def render_spec() -> str:
    """渲染「在文档末尾附待确认清单块」的指令文本，供提示词引用。

    返回的段落既说明「附在哪」、又给出**字面量**哨兵格式与示例，模型据此产出可被
    `extract` 精确剥离的结构化清单。

    Returns:
        可直接拼进用户提示词的说明段落。
    """
    return (
        "在文档末尾（所有正文之后）附上「待确认问题」清单，用如下 HTML 注释标记块包裹；"
        "块内是 JSON 数组，每项含 "
        "`question`（必填，一条待确认的问题）、`sections`（受影响章节编号数组，可为空）、"
        "`decider`（拍板人，可为空）、`importance`（重要性等级）：\n"
        f"{BEGIN_SENTINEL}\n"
        '[{{"question": "…", "sections": ["2.1", "6.1"], "decider": "产品负责人", '
        '"importance": "P0"}}]\n'
        f"{END_SENTINEL}\n"
        "`importance` **必填**，取值必须是 `P0` / `P1` / `P2` / `P3` / `P4` / `P5` / `P6` 之一"
        "（数字越小越要紧），按「不问就没法开工」的程度递减：`P0` 是不确认就会让整份文档写错、"
        "或整个项目无法开工的问题，`P6` 是可以留到最后再敲定的措辞与细节。"
        "请按实际严重程度**分散取值**，不要把所有条目都堆在同一个等级上——"
        "等级是澄清时「先问哪一批」的依据，堆在一档就等于没分级。"
        "**缺了等级、或写了 `P0` ~ `P6` 之外的值，该项会被直接丢弃**，请务必逐项给出。\n"
        "`question` **必须写成一句面向用户的人话问题**（用户要能直接读出来并回答），例如"
        "「登录态 7 天与 refresh_token 30 天是什么关系？」。**不要**把 `id` / `topic` / "
        "`context` / `owner` 之类的键值对拼成一行塞进 `question`——那是模型的内部记号，"
        "不是用户能回答的问题；受影响章节填 `sections`、拍板人填 `decider`，不要写进题干。\n"
        "没有待确认项时附 `[]`。清单须与正文中的「待确认」一一对应；不要把这段标记写进章节里。\n"
        "⚠️ **不要**写成人读形态（例如「## 附：待确认问题」+ 编号列表）：那种形态不算清单，"
        "会被当作正文展示。清单**只能**放在上面这对 HTML 注释标记里，格式就是 JSON 数组。\n"
        "⚠️ 标记块**不要**放进代码围栏（不要写成 ```json…```）：它必须直接落在正文末尾，"
        "围栏会把它变成示例代码，抽取时还会留下多余的反引号。"
    )


def format_pending_items(items: list[dict[str, Any]]) -> str:
    """把清单渲染成给人/给模型看的编号列表（供「AI 自行澄清」的提示词引用）。

    与 `render_spec()`（教模型**怎么产出**清单）方向相反：这里是把**已有**的清单
    摊平成文本，让模型逐条拍板。

    等级与受影响章节都带上：等级决定「拿不准时先保住哪一条」，章节号决定结论该写去哪。
    空清单给一句明确的「没有」，而不是留白——留白会让模型以为漏读了材料。

    Args:
        items: 后端结构的清单项（`question` / `sections` / `decider` / `importance`）。

    Returns:
        编号列表文本；无待确认项时返回一句说明。
    """
    if not items:
        return "（本次没有待确认问题）"
    lines: list[str] = []
    for index, item in enumerate(items, start=1):
        question = str(item.get("question") or "").strip() or "（未描述）"
        importance = str(item.get("importance") or "").strip() or "未标注"
        sections = item.get("sections")
        section_text = "、".join(str(part) for part in sections) if isinstance(sections, list) else ""
        parts = [f"{index}. [{importance}] {question}"]
        if section_text:
            parts.append(f"（影响章节：{section_text}）")
        decider = str(item.get("decider") or "").strip()
        if decider:
            parts.append(f"（原定拍板人：{decider}）")
        lines.append("".join(parts))
    return "\n".join(lines)


def _normalize_importance(raw: Any) -> str | None:
    """把模型给的 importance 规范成 `P0` ~ `P6`；缺失或不认识的值返回 `None`。

    返回 `None` 表示**该项没通过校验**——调用方据此丢弃它，而不是替它挑一个默认等级。
    大小写与首尾空白会被规整（` p3 ` → `P3`），其余一律视为不合法。

    Args:
        raw: 模型输出里该项的 `importance` 原值，可能是任意类型。

    Returns:
        `IMPORTANCE_ORDER` 里的某一个等级；不合法时为 `None`。
    """
    if isinstance(raw, str):
        value = raw.strip().upper()
        if value in IMPORTANCE_ORDER:
            return value
    return None


def _importance_rank(item: dict[str, Any]) -> int:
    """清单项的排序键：重要性数字越小越靠前（P0 最优先）。

    `extract` 已把等级非法的项挡在排序之前，这里的 `.get` 兜底只是让排序键永远可比较
    （拿不到等级的项排在最后），不承担「替模型猜等级」的职责。
    """
    return IMPORTANCE_ORDER.get(str(item.get("importance", "")), len(IMPORTANCE_ORDER))


# 第三种形态：正文里的**规范取舍表**。模型不写清单块，而是用一张「规范 vs PRD」的对照表
# 交代取舍，把该定的事写在「处理」列（例如「采用 needLogin，待团队确认」）。
# 这张表是**正文**，不能剥；但行里既然标了「待确认」，就必须同时登记进清单——
# 否则界面上「待确认问题」容器是空的，而正文里明明挂着待确认（2026-11-19 实测）。
_TABLE_PENDING_MARK = re.compile(r"待确认|待定|待团队确认|待评审确认|待产品确认|需确认")
_TABLE_CELL_SEPARATOR = re.compile(r"^:?-{2,}:?$")


def _extract_table_pending(text: str, *, id_prefix: str) -> list[dict[str, Any]]:
    """从正文末尾的表格里收集「标了待确认」的行，转成清单项（不修改正文）。

    判定保守：只看**后三分之一**（取舍表按惯例在末尾），只看真正的 Markdown 表格行，
    只看含「待确认 / 待定 / 待团队确认…」的格子。一条都认不出就返回空列表。

    Args:
        text: 完整正文。
        id_prefix: 清单项 id 前缀（与 `finalize_items` 一致）。

    Returns:
        已排序、已分配 id 的清单项（可能为空）。
    """
    lines = text.splitlines()
    start_at = len(lines) * 2 // 3
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index in range(start_at, len(lines)):
        line = lines[index].strip()
        if not (line.startswith("|") and line.endswith("|")):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        if all(_TABLE_CELL_SEPARATOR.match(cell.replace(" ", "")) for cell in cells if cell):
            continue  # 表头分隔行
        marked = next((cell for cell in cells[1:] if _TABLE_PENDING_MARK.search(cell)), "")
        if not marked:
            continue
        # 题干要像「一句待确认的问题」而不是表格原样拼接。线上实测：取舍表的行
        # （「`basic`：基础组录入内容…组内字段清单待确认」）被原样搬进清单后，
        # 用户看到的是一条字段说明，不是要他回答的问题。
        topic = cells[0].strip(" ：:;；")
        if len(cells) >= 3:
            question = f"「{topic}」的取舍需要确认：{marked}"
        else:
            question = f"「{topic}」需要确认：{marked}"
        question = question.strip(" ：:;；")
        if not question or question in seen:
            continue
        seen.add(question)
        records.append(
            {
                "question": question[:160],
                "sections": [],
                "decider": "",
                "importance": _LEGACY_IMPORTANCE,
            }
        )
    if not records:
        return []
    logger.warning(
        "正文里用的是**规范取舍表**而不是标记块，已从表格行兜底登记待确认项",
        extra={"event": "pending.table_fallback", "count": len(records)},
    )
    return finalize_items(records, id_prefix=id_prefix)


def extract(
    text: str, *, id_prefix: str = "p"
) -> tuple[str, list[dict[str, Any]]]:
    """从完整正文剥离**所有**待确认标记块，返回（干净正文, 清单列表）。

    设计要点：

    - 正文里可能有 0..N 个标记块（分章节生成时每章一个），逐个剥离。
    - 所有条目先收集，**校验重要性等级**（缺失或不合法 → 丢弃该项并记 warning），
      再按 `importance` **从高到低（P0 → P6）稳定排序**后，才按排好的顺序重新分配
      `id` 为 ``{id_prefix}-{n}``，不信任模型给的 `id`。
      于是「id 序号」天然等于「提问顺序」——越重要的排在越前面、越先被问到。
    - 某个块 JSON 非法 → 跳过该块（正文中不留块体），记 warning；生成不因此失败。
    - 没有任何块 → 返回（原文, []）。

    Args:
        text: 可能含标记块的完整正文。
        id_prefix: 清单项 id 的前缀；分章节生成传 `section.id`，保证跨章节不重号。

    Returns:
        `(clean_text, items)`：`clean_text` 不含任何标记块；`items` 是解析出的清单
        （每项已被加上后端分配的 `id`）。
    """
    if BEGIN_SENTINEL not in text:
        # 模型没按标记块写时（例如写成「## 附：待确认问题」的人读列表），
        # 不再直接放行——那样清单会留在正文里，界面上既看不到「待确认问题」容器，
        # 又会把一段本属于元数据的附录当正文展示（2026-11-18 线上反馈）。
        legacy = _extract_legacy_appendix(text, id_prefix=id_prefix)
        if legacy is not None:
            return legacy
        # 第三种形态：正文末尾的「规范取舍表」（2026-11-19 实测）——表格本身是正文，
        # 不剥，只把「标了待确认」的行登记成清单，否则容器永远是空的。
        return text, _extract_table_pending(text, id_prefix=id_prefix)

    # 先把所有块里的条目原样收集起来，统一排序后才分配 id。
    collected: list[dict[str, Any]] = []
    parts = text.split(BEGIN_SENTINEL)
    out: list[str] = [parts[0]]
    for block in parts[1:]:
        end_idx = block.find(END_SENTINEL)
        if end_idx == -1:
            # 标记块没有闭合：视为抑制失败漏出的半截，正文保留原样（不当 JSON 解析）。
            out.append(block)
            logger.warning(
                "待确认标记块未闭合，已整段保留为正文",
                extra={"event": "pending.unclosed"},
            )
            continue
        raw_json = block[:end_idx]
        tail = block[end_idx + len(END_SENTINEL):]
        parsed = _parse_items(raw_json)
        if parsed is None:
            logger.warning(
                "待确认清单块不是合法 JSON，已忽略",
                extra={"event": "pending.missing", "raw": raw_json[:200]},
            )
        else:
            collected.extend(parsed)
        out.append(tail)

    items = finalize_items(collected, id_prefix=id_prefix)
    if not items:
        # 标记块里是空数组（或者块坏掉被跳过）：正文里的取舍表可能还留着待确认行，
        # 一并兜底，避免「文档写了待确认、容器却是空的」。
        items = _extract_table_pending(text, id_prefix=id_prefix)

    clean = "".join(out)
    # 移除块体后可能留下成片空行：压缩到连续两个换行以内并裁掉首尾空白，
    # 让落库正文与 `part` 预览视觉一致（不影响语义）。
    clean = "\n".join(line for line in re.split(r"\n{3,}", clean.strip()) if line.strip() or True)
    clean = collapse_blank_lines(clean)
    return clean, items


# 清单项的**文案清洗**：模型偶尔不按 `render_spec` 写「一句人话问题」，而是把自己内部的
# 记账字段拼成一行塞进 `question`——线上实见（2026-11-19）：
#   "id: P-01 topic: F-03 格式校验验收标准 context: PRD 中 F-03 标记「待确认」… owner: 产品经理"
# 用户看到的不是问题，是一条开发日志；末尾还残留 ``` （清单块被写进了代码围栏）。
# 这里做**确定性**的还原，不猜语义：剥围栏 → 拆键值段 → 用 `topic` 当题干、`context` 变背景、
# `owner` 落到拍板人。都不成立时原样返回，绝不让清洗把正常题干改坏。
_FENCE_TOKEN = re.compile(r"```(?:json|JSON)?|~~~(?:json|JSON)?")
_KEYED_SEGMENT = re.compile(
    r"(?<![\w-])(id|topic|context|owner|decider|sections|importance)\s*[:：]\s*", re.I
)


def _strip_fences(text: str) -> str:
    """去掉残留的代码围栏记号（清单块被模型写进 ```json 围栏时留下的尾巴）。"""
    return _FENCE_TOKEN.sub("", text).strip()


def _split_keyed(text: str) -> tuple[str, dict[str, str]]:
    """把「id: … topic: … context: … owner: …」拆成（前缀, {键: 值})。

    没有任何键值段时返回 `(原文, {})`——正常题干不受影响。
    """
    matches = list(_KEYED_SEGMENT.finditer(text))
    if not matches:
        return text.strip(), {}
    head = text[: matches[0].start()].strip()
    segments: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        segments[match.group(1).lower()] = text[match.end() : end].strip(" \t;；,，、\n")
    return head, segments


def clean_question_fields(record: dict[str, Any]) -> dict[str, Any]:
    """把清单项的题干还原成**面向用户的一句话**（见上面 `_KEYED_SEGMENT` 的说明）。

    Args:
        record: 原始清单项（`question` 可能是机器拼接串）。

    Returns:
        清洗后的新字典（不改动入参）；题干为空时退回原文。
    """
    raw = _strip_fences(str(record.get("question") or ""))
    head, segments = _split_keyed(raw)
    cleaned = dict(record)

    topic = segments.get("topic") or segments.get("question") or ""
    context = segments.get("context") or ""
    if topic:
        question = f"{topic}（背景：{context}）" if context else topic
    else:
        question = head or raw
    cleaned["question"] = _strip_fences(question).strip(" \t\n;；")

    owner = segments.get("owner") or segments.get("decider") or ""
    if owner and not str(cleaned.get("decider") or "").strip():
        cleaned["decider"] = owner
    return cleaned


def finalize_items(
    records: list[dict[str, Any]], *, id_prefix: str = "p"
) -> list[dict[str, Any]]:
    """把一批**原始**清单项校验、排序并分配 id——这条链路的唯一落点。

    为什么要单独拎出来：分节改写只会重写其中几节，改完要把「改写节的新清单」与
    「未改写节的旧清单」合并后**重新排一次序、重新分配一遍 id**。若各写一份
    「校验 + 排序 + 分配」的循环，两边迟早在「等级怎么兜底」「id 从几开始」上漂移。

    处理顺序（与 `extract` 内的口径完全一致）：

    1. **等级校验**：缺失或非法 → **丢弃该项**（绝不兜底），记 warning；
    2. 按 `importance` 从高到低（P0 → P6）**稳定排序**——同级保持模型原本的书写顺序，
       避免同一等级内顺序跳来跳去；
    3. 按排好的顺序**重新分配 id** 为 ``{id_prefix}-{n}``，不信任模型给的 `id`。
       于是「id 序号」天然等于「提问顺序」：越重要的排在越前面、越先被问到。

    Args:
        records: 原始清单项（可以来自 `extract` 的收集、也可以来自一次合并）。
        id_prefix: id 前缀；分章节生成传 `section.id`，保证跨章节不重号。

    Returns:
        已排好序且带 id 的清单项。
    """
    validated: list[dict[str, Any]] = []
    for record in records:
        level = _normalize_importance(record.get("importance"))
        if level is None:
            logger.warning(
                "待确认项缺少合法的重要性等级，已丢弃该项",
                extra={
                    "event": "pending.bad_importance",
                    "raw": record.get("importance"),
                    "question": str(record.get("question", ""))[:120],
                },
            )
            continue
        validated.append({**clean_question_fields(record), "importance": level})

    validated.sort(key=_importance_rank)
    return [
        {**record, "id": f"{id_prefix}-{index}"}
        for index, record in enumerate(validated, start=1)
    ]


# 人读附录的标题（模型偶尔不按标记块写）。只认「附 / 附录 / 待确认问题清单」这类标题，
# 避免把正文里某个叫「待确认」的正经章节误当附录剥掉。
_LEGACY_HEADING = re.compile(
    # 后缀是**组合**而不是三选一：线上接口文档直接写成「### 待确认项」，PRD 写成
    # 「## 待确认问题清单」——两者都是同一种附录，各自漏掉一个词就整段留在正文里，
    # 用户看到的正是「正文尾部挂着一张问题清单、待确认问题容器却是空的」。
    r"^[\s>*#]*(?:附(?:录)?\s*[：:]?\s*)?待确认(?:问题|事项|清单|项|点|条目)*"
    r"(?:[\s（(][^）)]*[）)])?[\s*#：:]*$"
)

# 一条附录项的起始：「1. 」/「1、」/「- 」/「* 」/「• 」。
_LEGACY_ITEM_START = re.compile(r"^\s*(?:[0-9]+[.、)）]|[-*•·])\s+(.*)$")

# 附录项里的「影响章节：…」「拍板人：…」「需要 X 拍板」等尾注。
_LEGACY_SECTIONS = re.compile(r"影响章节\s*[：:]\s*([^；;]+)")
_LEGACY_DECIDER = re.compile(r"(?:拍板人\s*[：:]\s*|需要\s*)([^；;，,。]*?)(?:拍板|决定|确认)")

# 旧格式没有重要性等级，统一按 **P3（中性档）** 参与澄清：
# 不假装能细分优先级，也不把这些条目整批丢掉（丢掉等于用户彻底看不到它们）。
_LEGACY_IMPORTANCE = "P3"


def _extract_legacy_appendix(
    text: str, *, id_prefix: str
) -> tuple[str, list[dict[str, Any]]] | None:
    """兜底剥离**人读形态**的「附：待确认问题」附录。

    背景：规范格式是 ``<!-- PENDING:BEGIN -->`` 里的 JSON（见 `render_spec()`）。
    但模型有时不照办，直接写成文档末尾的人读列表：

    ```
    ## 附：待确认问题
    1. 产品对外主名称统一为 PRDWright 还是 Vibe Coding 需求工作台 —— 影响章节：1.1、全文；需要产品负责人拍板
    2. 账号体系的具体形态（注册方式、找回密码、会话有效期） —— 影响章节：2.1（F-01）、4；需要产品负责人拍板
    ```

    这种输出以前会**原样留在正文里**：界面上看不到「待确认问题」容器，正文尾部却挂
    一段本该是元数据的附录。这里把附录识别出来 → 从正文剥离 → 解析成清单项。

    判定是保守的，三条同时满足才动手（避免误伤正文）：

    1. 存在一个**附录样式的标题行**（`附：待确认问题` / `待确认问题清单` / `附录：待确认`…）；
    2. 该标题之后至少解析出 **1 条**编号/项目符号条目；
    3. 该标题位于正文的**后半段**（附录按规范就在末尾；出现在开头多半是正经章节）。

    Args:
        text: 完整正文（不含标记块）。
        id_prefix: 清单项 id 前缀。

    Returns:
        `(剥离后的正文, 清单项)`；没有可识别的附录时返回 `None`（交调用方按原文放行）。
    """
    lines = text.splitlines()
    heading_index = None
    for index, line in enumerate(lines):
        if _LEGACY_HEADING.match(line.strip()):
            # 只认后半段出现的标题：防止把开头某个「待确认问题」章节当附录
            if index >= len(lines) // 2:
                heading_index = index
                break
    if heading_index is None:
        return None

    # 逐条收集：新条目开启新项，续行并入上一条（模型常把长条折行）。
    raw_items: list[str] = []
    for line in lines[heading_index + 1:]:
        match = _LEGACY_ITEM_START.match(line)
        if match:
            raw_items.append(match.group(1).strip())
        elif raw_items and line.strip():
            raw_items[-1] = f"{raw_items[-1]} {line.strip()}"
    if not raw_items:
        return None

    records: list[dict[str, Any]] = []
    for raw in raw_items:
        question, sections, decider = _parse_legacy_item(raw)
        if not question:
            continue
        records.append(
            {
                "question": question,
                "sections": sections,
                "decider": decider,
                "importance": _LEGACY_IMPORTANCE,
            }
        )
    if not records:
        return None

    items = finalize_items(records, id_prefix=id_prefix)
    logger.warning(
        "待确认清单是**人读附录**而不是标记块，已按旧格式兜底剥离（建议收紧提示词）",
        extra={"event": "pending.legacy_appendix", "count": len(items)},
    )

    body = "\n".join(lines[:heading_index]).rstrip()
    return collapse_blank_lines(body), items


def _parse_legacy_item(raw: str) -> tuple[str, list[str], str]:
    """把一条人读附录项拆成（问题, 影响章节, 拍板人）。

    尾注可能用破折号引出（`—— 影响章节：…`），也可能被折行后直接以「影响章节：」开头
    （线上那份附录两种都有）。因此先找破折号，找不到就退化为「在第一个尾注关键词处分界」。
    """
    head, separator, tail = raw.partition("——")
    if not separator:
        head, separator, tail = raw.partition("--")
    if not separator:
        # 没有破折号：以第一个尾注关键词为界，前面是问题、后面是元数据
        marker = _LEGACY_SECTIONS.search(raw) or _LEGACY_DECIDER.search(raw)
        if marker is not None:
            head, tail = raw[: marker.start()], raw[marker.start():]
        else:
            head, tail = raw, ""

    question = head.strip().strip("，,。；; —")
    sections: list[str] = []
    section_match = _LEGACY_SECTIONS.search(tail or "")
    if section_match:
        sections = [
            part.strip()
            for part in re.split(r"[、,，/]", section_match.group(1))
            if part.strip()
        ]
    decider = ""
    decider_match = _LEGACY_DECIDER.search(tail or "")
    if decider_match:
        decider = decider_match.group(1).strip()
    return question, sections, decider


def _parse_items(raw: str) -> list[dict[str, Any]] | None:
    """把块内 JSON 解析成清单项列表；任何异常返回 None（交调用方降级处理）。"""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, list):
        return None
    result: list[dict[str, Any]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        cleaned = {
            key: value for key, value in entry.items() if key in _ITEM_FIELDS
        }
        if not str(cleaned.get("question", "")).strip():
            # 没有问题的项没有意义，跳过（模型偶尔会塞一个空对象进来）。
            continue
        result.append(cleaned)
    return result


def collapse_blank_lines(text: str) -> str:
    """把 3 个及以上连续换行压缩成 2 个，并裁掉首尾空白。

    剥离标记块后往往留下成片空行；压缩一下让落库正文与预览视觉一致（不影响语义）。
    澄清提问那套（`clarification_ask`）也用同一件——两处各写一份迟早在阈值上漂移。
    """
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class PendingBlockSuppressor:
    """有状态、逐 chunk 的**哨兵标记块**抑制器。

    模型是流式输出的，标记块可能被任意切成多段。本类保证：**即使抑制逻辑本身有漏，
    前端也永远看不到哨兵与块体**——而因为这些哨兵是 HTML 注释，即便漏出去在 Markdown
    渲染里也不可见，这里额外做一层抑制只是双保险。

    状态机：

    - **块外（OUT）**：扫描起始哨兵；哨兵前的文本原样输出，哨兵之后的内容进入块内态并丢弃。
      若本 chunk 内没找到完整起始哨兵，则尾留至多 `carry_len` 个字符作为
      「可能被切断的哨兵前缀」，供下一轮判断。
    - **块内（IN）**：扫描结束哨兵；块内所有内容（含结束哨兵）丢弃，结束哨兵之后的文本回到 OUT 态输出。
    - **流结束仍在块内**：缓冲的块体**不输出**（不能把半截 JSON 当正文）。

    **哨兵由构造参数给出**（默认即待确认清单那一对）。⚠️ 澄清提问的选项卡已改用
    **原生工具调用**（见 `clarification_ask` 的模块说明，那里不再有标记块），所以本类
    现在只服务待确认清单。参数化仍保留——那段状态机的全部难度在「哨兵被切在 chunk
    边界」，将来任何一条哨兵通道都能直接复用，不必复制这个坑。

    ⚠️ 名字里的 `Pending` 只是它的出身（先为待确认清单而写）：**类本身与块内是什么无关**。
    不为此搬家改名——那要把既有测试的 import 全改一遍，换来的只是名字好看一点。
    """

    def __init__(self, begin: str = BEGIN_SENTINEL, end: str = END_SENTINEL) -> None:
        self._begin = begin
        self._end = end
        # 块外态最多尾留的「可能被切断的哨兵前缀」长度（取两个哨兵的较大者 - 1）。
        # 只留 len-1 是因为：任何大于等于哨兵长度的串都可能是完整哨兵，必须全留进下一轮判断；
        # 小于它的部分绝不可能是完整哨兵，可以放心输出。
        self._carry_len = max(len(begin), len(end)) - 1
        self._in_block = False
        # 块外态下「可能被截断的哨兵前缀」，至多 `carry_len` 个字符。
        self._carry = ""

    def feed(self, chunk: str) -> str:
        """喂入一个增量，返回「应当输出」的部分（可能为空字符串）。"""
        if not chunk:
            return ""
        data = self._carry + chunk
        self._carry = ""
        if self._in_block:
            return self._emit_in(data)
        return self._emit_out(data)

    def _emit_out(self, data: str) -> str:
        idx = data.find(self._begin)
        if idx == -1:
            if len(data) > self._carry_len:
                self._carry = data[-self._carry_len:]
                return data[: -self._carry_len]
            self._carry = data
            return ""
        before = data[:idx]
        self._in_block = True
        rest = data[idx + len(self._begin):]
        return before + self._emit_in(rest)

    def _emit_in(self, data: str) -> str:
        idx = data.find(self._end)
        if idx == -1:
            # 仍在块内：**整段块体内容丢弃**，只暂存末尾可能截断的结束哨兵前缀。
            # 这里绝不能把块体当正文输出——那会把半截 JSON 暴露成「正文」。
            if len(data) > self._carry_len:
                self._carry = data[-self._carry_len:]
                return ""
            self._carry = data
            return ""
        after = data[idx + len(self._end):]
        self._in_block = False
        self._carry = ""
        return self._emit_out(after)

    def flush(self) -> str:
        """流结束时的收尾。

        - OUT 态：把末尾的「可能前缀」原样输出（它不可能是完整哨兵，最多 `carry_len` 字符）。
        - IN 态：块体尚未闭合，**不输出**（半截 JSON 不能当正文）。
        """
        if self._in_block:
            return ""
        out = self._carry
        self._carry = ""
        return out
