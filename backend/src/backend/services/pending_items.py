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
        "没有待确认项时附 `[]`。清单须与正文中的「待确认」一一对应；不要把这段标记写进章节里。"
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
        return text, []

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

    clean = "".join(out)
    # 移除块体后可能留下成片空行：压缩到连续两个换行以内并裁掉首尾空白，
    # 让落库正文与 `part` 预览视觉一致（不影响语义）。
    clean = "\n".join(line for line in re.split(r"\n{3,}", clean.strip()) if line.strip() or True)
    clean = collapse_blank_lines(clean)
    return clean, items


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
        validated.append({**record, "importance": level})

    validated.sort(key=_importance_rank)
    return [
        {**record, "id": f"{id_prefix}-{index}"}
        for index, record in enumerate(validated, start=1)
    ]


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
