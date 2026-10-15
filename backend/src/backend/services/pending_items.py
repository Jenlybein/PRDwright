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
# 流式抑制时，块外态最多尾留的「可能被切断的哨兵前缀」长度（取两个哨兵的较大者 - 1）。
# 只留 len-1 是因为：任何大于等于哨兵长度的串都可能是完整哨兵，必须全留进下一轮判断；
# 小于它的部分绝不可能是完整哨兵，可以放心输出。
_CARRY_LEN = max(len(BEGIN_SENTINEL), len(END_SENTINEL)) - 1

# 清单项允许从模型输出里保留的字段（其余一律丢弃；尤其是不信任模型给的 `id`）。
_ITEM_FIELDS = ("question", "sections", "decider", "importance")

# 待确认项的「重要性等级」，数字越小越重要。
# 三个等级由模型在生成正文时随清单一起给出；澄清阶段据此逐级推进，
# 保证「越重要越先问」。缺失或非法取值一律兜底为 _DEFAULT_IMPORTANCE，
# 这样即便模型偷懒没标等级，排序顺序依然成立（全部同级 → 保持原有顺序）。
IMPORTANCE_ORDER: dict[str, int] = {"P0": 0, "P1": 1, "P2": 2}
_DEFAULT_IMPORTANCE = "P1"


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
        "`importance` 必须是 `P0` / `P1` / `P2` 三者之一，按「不问就没法开工」的程度排序："
        "`P0` 最高（不确认会导致整份文档写错或无法开工）、`P1` 次之、`P2` 是可以后置的细节。"
        "拿不准就标 `P1`。\n"
        "没有待确认项时附 `[]`。清单须与正文中的「待确认」一一对应；不要把这段标记写进章节里。"
    )


def _normalize_importance(raw: Any) -> str:
    """把模型给的 importance 规范成 P0/P1/P2；缺失或不认识的值一律兜底为 P1。

    Args:
        raw: 模型输出里该项的 `importance` 原值，可能是任意类型。

    Returns:
        `IMPORTANCE_ORDER` 里的三个等级之一。
    """
    if isinstance(raw, str):
        value = raw.strip().upper()
        if value in IMPORTANCE_ORDER:
            return value
    return _DEFAULT_IMPORTANCE


def _importance_rank(item: dict[str, Any]) -> int:
    """清单项的排序键：重要性数字越小越靠前（P0 最优先）。"""
    return IMPORTANCE_ORDER.get(
        str(item.get("importance", "")), IMPORTANCE_ORDER[_DEFAULT_IMPORTANCE]
    )


def extract(
    text: str, *, id_prefix: str = "p"
) -> tuple[str, list[dict[str, Any]]]:
    """从完整正文剥离**所有**待确认标记块，返回（干净正文, 清单列表）。

    设计要点：

    - 正文里可能有 0..N 个标记块（分章节生成时每章一个），逐个剥离。
    - 所有条目先收集，按 `importance` **从高到低（P0 → P1 → P2）稳定排序**后，
      才按排好的顺序重新分配 `id` 为 ``{id_prefix}-{n}``，不信任模型给的 `id`。
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

    # 规范等级（缺失/非法 → P1），再按重要性从高到低稳定排序：
    # 同级保持模型原本的书写顺序，避免同一等级内顺序跳来跳去。
    for record in collected:
        record["importance"] = _normalize_importance(record.get("importance"))
    collected.sort(key=_importance_rank)
    items = [
        {**record, "id": f"{id_prefix}-{index}"}
        for index, record in enumerate(collected, start=1)
    ]

    clean = "".join(out)
    # 移除块体后可能留下成片空行：压缩到连续两个换行以内并裁掉首尾空白，
    # 让落库正文与 `part` 预览视觉一致（不影响语义）。
    clean = "\n".join(line for line in re.split(r"\n{3,}", clean.strip()) if line.strip() or True)
    clean = _collapse_blank_lines(clean)
    return clean, items


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


def _collapse_blank_lines(text: str) -> str:
    """把 3 个及以上连续换行压缩成 2 个，并裁掉首尾空白。"""
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class PendingBlockSuppressor:
    """有状态、逐 chunk 的标记块抑制器。

    模型是流式输出的，标记块可能被任意切成多段。本类保证：**即使抑制逻辑本身有漏，
    前端也永远看不到哨兵与块体**——而因为这些哨兵是 HTML 注释，即便漏出去在 Markdown
    渲染里也不可见，这里额外做一层抑制只是双保险。

    状态机：

    - **块外（OUT）**：扫描起始哨兵；哨兵前的文本原样输出，哨兵之后的内容进入块内态并丢弃。
      若本 chunk 内没找到完整起始哨兵，则尾留至多 `_CARRY_LEN` 个字符作为
      「可能被切断的哨兵前缀」，供下一轮判断。
    - **块内（IN）**：扫描结束哨兵；块内所有内容（含结束哨兵）丢弃，结束哨兵之后的文本回到 OUT 态输出。
    - **流结束仍在块内**：缓冲的块体**不输出**（不能把半截 JSON 当正文）。
    """

    def __init__(self) -> None:
        self._in_block = False
        # 块外态下「可能被截断的哨兵前缀」，至多 _CARRY_LEN 个字符。
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
        idx = data.find(BEGIN_SENTINEL)
        if idx == -1:
            if len(data) > _CARRY_LEN:
                self._carry = data[-_CARRY_LEN:]
                return data[: -_CARRY_LEN]
            self._carry = data
            return ""
        before = data[:idx]
        self._in_block = True
        rest = data[idx + len(BEGIN_SENTINEL):]
        return before + self._emit_in(rest)

    def _emit_in(self, data: str) -> str:
        idx = data.find(END_SENTINEL)
        if idx == -1:
            # 仍在块内：**整段块体内容丢弃**，只暂存末尾可能截断的结束哨兵前缀。
            # 这里绝不能把块体当正文输出——那会把半截 JSON 暴露成「正文」。
            if len(data) > _CARRY_LEN:
                self._carry = data[-_CARRY_LEN:]
                return ""
            self._carry = data
            return ""
        after = data[idx + len(END_SENTINEL):]
        self._in_block = False
        self._carry = ""
        return self._emit_out(after)

    def flush(self) -> str:
        """流结束时的收尾。

        - OUT 态：把末尾的「可能前缀」原样输出（它不可能是完整哨兵，最多 _CARRY_LEN 字符）。
        - IN 态：块体尚未闭合，**不输出**（半截 JSON 不能当正文）。
        """
        if self._in_block:
            return ""
        out = self._carry
        self._carry = ""
        return out
