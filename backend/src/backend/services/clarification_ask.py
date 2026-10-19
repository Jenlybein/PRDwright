"""澄清提问的结构化选择题：标记块规范、剥离、流式抑制、提示词说明。

澄清对话里 AI 每次提问，正文照常写「问题 + 背景 + 每个选项的简要解释」，再在末尾附一个
结构化块；前端据此把那道题的选项渲染成**可点的卡片**（用户也能走「都不合适、我自己写」）。

与待确认清单（`pending_items`）的关系
------------------------------------
**形状同源、用途不同**，因此共用一套机制、各留各的校验：

- `pending_items`：跟着**生成的文档**一起产出、随正文落库、进快照与文档表，还要按 P0~P6
  排序——它是「文档产物的第一类元数据」；
- 本模块：是一次**提问的展示规格**，只随 SSE 的 `done` 事件下发，**不落库、不进快照**。

生命周期不同，所以不复用 `pending_items.extract`（那会把两套正交的校验揉成一团）；但流式抑制
直接复用 `PendingBlockSuppressor`——那段状态机的全部难度在「哨兵被切在 chunk 边界」，
复制一份等于把这个坑复制一份。

为什么用 HTML 注释式哨兵
------------------------
与待确认清单同一个理由：即使抑制逻辑有漏、块体漏到了前端，Markdown 渲染里也**不可见**，
不会把半截 JSON 当成「正文」闪现出来（「降级不污染产物」）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from backend.services.pending_items import PendingBlockSuppressor, collapse_blank_lines

logger = logging.getLogger(__name__)

# 标记块哨兵：用 HTML 注释包裹，渲染时不可见。
ASK_BEGIN_SENTINEL = "<!-- ASK:BEGIN -->"
ASK_END_SENTINEL = "<!-- ASK:END -->"

# 每道题最多几个选项。超过就截断——选项太多反而难选，而且卡片会撑得很长。
MAX_ASK_OPTIONS = 6

# 每道题至少几个选项。**少于它的题会被整项丢弃**：一个选项不构成「选择题」，
# 而没有选项的分组在界面上既没法点、也没地方显示答案（自由填写那条路本来就有）。
MIN_ASK_OPTIONS = 2

# 选项标号，由**后端**按下标分配（与 `pending_items` 不信任模型的 `id` 同理：
# 让模型自己编号，迟早出现重号或跳号）。长度必须 >= MAX_ASK_OPTIONS。
_OPTION_LABELS = "ABCDEF"


def render_ask_spec() -> str:
    """渲染「在提问末尾附结构化选择题块」的指令文本，供提示词引用。

    返回的段落既说明「附在哪、正文该怎么写」，又给出**字面量**哨兵与示例，模型据此产出
    可被 `extract_asks` 精确剥离的结构化选项。

    Returns:
        可直接拼进系统提示词（提问规则）的说明段落。
    """
    return (
        "本轮若向你**提问**，就在正文末尾附一个「结构化选择题」块，用如下 HTML 注释标记块包裹；"
        "块内是 JSON 数组，每项含 `question`（必填，题干）、`background`（可空，一句背景）、"
        "`multiple`（是否多选，缺省 false）、`options`（必填，选项数组，每项只含 `text`）：\n"
        f"{ASK_BEGIN_SENTINEL}\n"
        # ⚠️ 这两行是**普通字符串**（没有 f 前缀），所以花括号**不能**按 f-string 的规矩写成
        # `{{`——那会原样渲染出双括号，示例就成了非法 JSON，模型照着抄必然失败。
        '[{"question": "第一版要不要做离线同步？", "background": "你提到用户常在弱网环境使用。", '
        '"multiple": false, "options": [{"text": "第一版就做离线同步"}, '
        '{"text": "第一版只做在线"}]}]\n'
        f"{ASK_END_SENTINEL}\n"
        "配合这套块的写法：\n"
        "- 正文里照常把问题讲清楚——先写问题、再写背景（为什么问），然后**逐条简要解释每个选项**；"
        "解释时请冠以与块内一致的 `A` / `B` / `C`，用户才能对照着看。\n"
        "- **块里只放短标题**（选项的一句话名字，如「第一版只做在线」），不要重复正文的解释文字"
        "——前端会把它们渲染成可点的选项卡，卡片上只显示标题。\n"
        f"- 每道题给 {MIN_ASK_OPTIONS} ~ {MAX_ASK_OPTIONS} 个选项："
        f"**少于 {MIN_ASK_OPTIONS} 个选项的题会被直接丢弃**，"
        f"超过 {MAX_ASK_OPTIONS} 个只保留前 {MAX_ASK_OPTIONS} 个。\n"
        "- 确实没有可枚举方向的开放题，也请给出两三个最常见的起点，让用户可以直接点选，"
        "或者走「都不合适、我自己写」那条路；不要为了凑格式硬编造离谱的选项。\n"
        "- 这一轮**没有任何提问**时（例如只是复述确认、或提示可以生成 PRD）不要附这个块。"
    )


def make_ask_suppressor() -> PendingBlockSuppressor:
    """按 ASK 哨兵构造一个流式抑制器（复用待确认清单那套状态机）。"""
    return PendingBlockSuppressor(ASK_BEGIN_SENTINEL, ASK_END_SENTINEL)


def extract_asks(
    text: str, *, id_prefix: str = "ask"
) -> tuple[str, list[dict[str, Any]]]:
    """从完整回复里剥离**所有**结构化选择题块，返回（干净正文, 题目列表）。

    设计要点（与 `pending_items.extract` 同口径的地方就不再重复）：

    - 回复里可能有 0..N 个块，逐个剥离；某个块 JSON 非法 → 跳过该块（正文不留块体）并记 warning，
      生成**不因此失败**。
    - **没闭合的块保留为正文**：那说明它多半是被截断的模型输出，把它当 JSON 解析只会丢掉用户
      真正看到的那段话；而这些哨兵是 HTML 注释，留在正文里渲染也不可见。
    - 每项**逐条校验**，不过就丢该项（没有题干、选项不足、选项没有正文）。
    - `label` 与 `id` 由本函数分配，**不信任模型给的值**。

    Args:
        text: 可能含标记块的完整回复。
        id_prefix: 题目 id 的前缀。

    Returns:
        `(clean_text, asks)`：`clean_text` 不含任何标记块；`asks` 每项已带后端分配的
        `id` 与各选项的 `label`。
    """
    if ASK_BEGIN_SENTINEL not in text:
        # 观测盲点：模型没产块是「功能静默退化」的最常见形态，但若不记一条日志，
        # 就无法把「没产块」和「前端刷新丢了 asks」区分开。这里补上明确信号。
        logger.info(
            "澄清回复未包含选择题块（模型未产块，按纯文本提问降级）",
            extra={"event": "ask.no_block"},
        )
        return text, []

    collected: list[dict[str, Any]] = []
    parts = text.split(ASK_BEGIN_SENTINEL)
    out: list[str] = [parts[0]]
    for block in parts[1:]:
        end_idx = block.find(ASK_END_SENTINEL)
        if end_idx == -1:
            # 未闭合：多半是被截断的输出，整段原样保留（哨兵是 HTML 注释，渲染不可见）
            out.append(block)
            logger.warning(
                "澄清选择题标记块未闭合，已整段保留为正文",
                extra={"event": "ask.unclosed"},
            )
            continue
        raw_json = block[:end_idx]
        tail = block[end_idx + len(ASK_END_SENTINEL):]
        parsed = _parse_asks(raw_json)
        if parsed is None:
            logger.warning(
                "澄清选择题块不是合法 JSON，已忽略",
                extra={"event": "ask.missing", "raw": raw_json[:200]},
            )
        else:
            collected.extend(parsed)
        out.append(tail)

    asks: list[dict[str, Any]] = []
    for index, record in enumerate(collected, start=1):
        options = [
            {"label": _OPTION_LABELS[position], "text": option["text"]}
            for position, option in enumerate(record["options"])
        ]
        asks.append({**record, "id": f"{id_prefix}-{index}", "options": options})

    return collapse_blank_lines("".join(out)), asks


def _parse_asks(raw: str) -> list[dict[str, Any]] | None:
    """把块内 JSON 解析成题目列表；整块不可解析时返回 `None`（交调用方降级处理）。

    单项不过校验就丢单项，不影响同一块里其它题目。
    """
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
        question = _as_text(entry.get("question"))
        if not question:
            # 没有题干的分组既没标题可渲染，也没地方安放答案，整项丢弃
            continue
        options = _parse_options(entry.get("options"))
        if len(options) < MIN_ASK_OPTIONS:
            logger.warning(
                "澄清选择题的选项不足，已丢弃该项",
                extra={
                    "event": "ask.too_few_options",
                    "question": question[:120],
                    "count": len(options),
                },
            )
            continue
        result.append(
            {
                "question": question,
                "background": _as_text(entry.get("background")),
                # 严格 `is True`：模型给 "true" / 1 这类值一律按单选处理，
                # 免得把一个字符串当真值、把单选题渲染成多选题
                "multiple": entry.get("multiple") is True,
                "options": options,
            }
        )
    return result


def _parse_options(raw: Any) -> list[dict[str, str]]:
    """把 `options` 解析成 `[{"text": …}]`；坏值丢弃，超量截断。"""
    if not isinstance(raw, list):
        return []
    options: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = _as_text(item.get("text"))
        if not text:
            continue
        options.append({"text": text})
        if len(options) >= MAX_ASK_OPTIONS:
            break
    return options


def _as_text(value: Any) -> str:
    """只接受字符串（去掉首尾空白）；其它类型一律当空值。

    不做 `str(value)` 转换：模型的 JSON 里出现 `{"text": {"a": 1}}` 时，`str()` 会把
    一个字典塞进界面，不如当它是坏数据丢掉。
    """
    return value.strip() if isinstance(value, str) else ""
