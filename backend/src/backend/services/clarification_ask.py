"""澄清提问的结构化选择题：工具 schema、参数校验、提示词说明。

澄清对话里 AI 每次提问，正文照常写「问题 + 背景 + 每个选项的简要解释」，并**调用
`ask_clarification_questions` 工具**输出结构化题目（不再在正文里藏哨兵标记块）；
后端从工具调用参数里取出题目、校验、分配 id/label，随 SSE 的 `done` 事件下发，前端据此
把那道题的选项渲染成**可点的卡片**（用户也能走「都不合适、我自己写」）。

为什么从「哨兵块」改成「工具调用」
--------------------------------
旧机制让模型在正文里附 `<!-- ASK:BEGIN -->…<!-- ASK:END -->` 块，后端剥离解析。长对话里
模型会模仿自己历史中「只写 A/B/C 文字、不附块」的写法而把块丢掉，导致选项卡静默不弹。
改成原生 function calling 后，题目走**独立的工具调用通道**（schema 强约束），不再依赖模型
在自由文本里「记得附标记」，从根上消除这类退化。对齐 Claude Code `AskUserQuestion` /
Codex `request_user_input`。

与待确认清单（`pending_items`）的关系
------------------------------------
形状同源、用途不同：`pending_items` 跟着**生成的文档**落库、进快照、按 P0~P6 排序，是
「文档产物的第一类元数据」，仍走哨兵块（不改）；本模块只是一次**提问的展示规格**，只随
SSE 的 `done` 事件下发、不落库、不进快照，走工具调用。故 `PendingBlockSuppressor` 保留给
`pending_items`，本模块不再用它。
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# 工具名：模型据此调用「输出结构化选择题」
ASK_TOOL_NAME = "ask_clarification_questions"

# 每道题最多几个选项。超过就截断——选项太多反而难选，而且卡片会撑得很长。
MAX_ASK_OPTIONS = 6

# 每道题至少几个选项。**少于它的题会被整项丢弃**：一个选项不构成「选择题」，
# 而没有选项的分组在界面上既没法点、也没地方显示答案（自由填写那条路本来就有）。
MIN_ASK_OPTIONS = 2

# 选项卡片上那行标题的字数上限。卡片只放**选项的名字**，解释归正文——
# 「名字：一句解释」这种写法会让卡片与正文重复啰嗦（用户明确反馈过）。
MAX_OPTION_TITLE_CHARS = 16

# 每轮最多几道题。题目太多用户答不过来，卡片也会很长。
MAX_ASK_QUESTIONS = 2

# 选项标号，由**后端**按下标分配（与 `pending_items` 不信任模型的 `id` 同理：
# 让模型自己编号，迟早出现重号或跳号）。长度必须 >= MAX_ASK_OPTIONS。
_OPTION_LABELS = "ABCDEF"


def build_ask_tool() -> dict[str, Any]:
    """返回 `ask_clarification_questions` 工具描述（OpenAI / 方舟兼容的 function 形状）。

    工具参数的 `questions` 数组形状与前端 `ClarificationAsk` 一一对应：
    `[{question, background, multiple, options: [{text}]}]`。`id` 与 `label` **不由模型给**，
    后端在 `asks_from_tool_calls` → `_validate_records` 里分配。
    """
    return {
        "type": "function",
        "function": {
            "name": ASK_TOOL_NAME,
            "description": (
                "本轮向用户提出澄清用的结构化选择题。仅在确实需要用户拍板时调用；"
                "若本轮只是复述确认、或信息已足够（例如提示可以生成 PRD），不要调用本工具。"
                "一次调用带本轮全部（1~2 道）题。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "questions": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MAX_ASK_QUESTIONS,
                        "items": {
                            "type": "object",
                            "properties": {
                                "question": {
                                    "type": "string",
                                    "description": "题干，必填，一句话把要用户拍板的事问清楚",
                                },
                                "background": {
                                    "type": "string",
                                    "description": "一句背景（为什么问这个），可空",
                                },
                                "multiple": {
                                    "type": "boolean",
                                    "description": "是否多选（缺省 false 为单选）",
                                },
                                "options": {
                                    "type": "array",
                                    "minItems": MIN_ASK_OPTIONS,
                                    "maxItems": MAX_ASK_OPTIONS,
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "text": {
                                                "type": "string",
                                                "description": (
                                                    f"选项的短标题，不超过 {MAX_OPTION_TITLE_CHARS} 个字，"
                                                    "只写选项的名字（如「想法快速录入」）；"
                                                    "不要冒号、不要写解释——解释写在正文里"
                                                ),
                                                "maxLength": MAX_OPTION_TITLE_CHARS,
                                            }
                                        },
                                        "required": ["text"],
                                        "additionalProperties": False,
                                    },
                                },
                            },
                            "required": ["question", "options"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["questions"],
                "additionalProperties": False,
            },
        },
    }


def render_ask_tool_instruction() -> str:
    """渲染「提问时正文怎么写 + 调用工具输出结构化题目」的指令，供提示词引用。

    正文仍写「问题 + 背景 + 逐条解释每个选项（冠以 A/B/C）」——单题卡片不显示题干、
    靠正文交代；结构化 payload 通过调用 `ask_clarification_questions` 工具输出。
    """
    return (
        "本轮若向用户**提问**，就**调用 `ask_clarification_questions` 工具**输出结构化选择题；"
        "正文里不要再写任何标记块：\n"
        "- 正文里照常把问题讲清楚——先写问题、再写背景（为什么问），然后**逐条简要解释每个选项**；"
        "解释时请冠以与工具参数里 `options` 顺序一致的 `A` / `B` / `C`，用户才能对照着看。\n"
        "- 工具参数 `options` 里每项 `text` **只写选项的名字**（不超过 "
        f"{MAX_OPTION_TITLE_CHARS} 个字），前端会把它渲染成一张可点的选项卡卡片：\n"
        "  - **不要写冒号、不要把正文里的解释再抄一遍**——卡片上只有这一行，长了就又挤又啰嗦；\n"
        "  - 正例：「想法快速录入」「结构化引导填空」「版本历史与对比」；\n"
        "  - 反例：「想法快速录入：几句话/要点输入，自动识别关键字段」——冒号后面那半段"
        "属于正文该写的解释，卡片上再出现一次就是重复。\n"
        f"- 每道题给 {MIN_ASK_OPTIONS} ~ {MAX_ASK_OPTIONS} 个选项："
        f"**少于 {MIN_ASK_OPTIONS} 个选项的题会被直接丢弃**，"
        f"超过 {MAX_ASK_OPTIONS} 个只保留前 {MAX_ASK_OPTIONS} 个。\n"
        "- 确实没有可枚举方向的开放题，也请给出两三个最常见的起点，让用户可以直接点选，"
        "或者走「都不合适、我自己写」那条路；不要为了凑格式硬编造离谱的选项。\n"
        "- 这一轮**没有任何提问**时（例如只是复述确认、或提示可以生成 PRD）**不要调用该工具**。"
    )


def asks_from_tool_calls(
    tool_calls: list[dict[str, Any]] | None, *, id_prefix: str = "ask"
) -> list[dict[str, Any]]:
    """从累积的 tool_calls 里提取 `ask_clarification_questions` 的题目并归一化。

    - 只认 `ASK_TOOL_NAME` 的调用，其它工具忽略；
    - `args` 可能是已解析的 dict，也可能是 JSON 字符串，两种都兼容；非法 JSON 记 warning 后跳过；
    - 多个 tool_call 的 questions 拼接（正常 `parallel_tool_calls=False` 只有一个）；
    - 提取/校验失败一律**降级为空**（生成不因此失败）：「没调工具」就是纯文本提问，
      是常态而不是异常。

    Returns:
        题目列表。每项已带后端分配的 `id`（`ask-1`、`ask-2`…）与各选项的 `label`；
        来源工具调用带 `id` 时，每项再挂一个 `tool_call_id`（作答回灌靠它配对那次调用）。
    """
    collected: list[tuple[str, dict[str, Any]]] = []
    for call in tool_calls or []:
        if not isinstance(call, dict) or call.get("name") != ASK_TOOL_NAME:
            continue
        raw_id = call.get("id")
        call_id = raw_id if isinstance(raw_id, str) else ""
        args = call.get("args")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (ValueError, TypeError):
                logger.warning(
                    "澄清工具调用参数不是合法 JSON，已忽略",
                    extra={"event": "ask.bad_args", "raw": args[:200]},
                )
                continue
        if not isinstance(args, dict):
            continue
        questions = args.get("questions")
        if isinstance(questions, list):
            collected.extend((call_id, record) for record in _validate_records(questions))

    asks: list[dict[str, Any]] = []
    for index, (call_id, record) in enumerate(collected, start=1):
        options = [
            {"label": _OPTION_LABELS[position], "text": option["text"]}
            for position, option in enumerate(record["options"])
        ]
        item: dict[str, Any] = {**record, "id": f"{id_prefix}-{index}", "options": options}
        # 只在真的拿到调用 id 时才挂：作答回灌要靠它把答案对回这次调用，
        # 缺 id 就不带这个键（前端据此降级成纯文本作答）
        if call_id:
            item["tool_call_id"] = call_id
        asks.append(item)
    return asks


def _validate_records(data: list[Any]) -> list[dict[str, Any]]:
    """逐项校验工具参数的 questions 数组；单项不过校验就丢单项。

    校验口径与旧 `_parse_asks` 一致：没有题干整项丢弃、选项不足整项丢弃、选项没有正文丢该选项、
    `multiple` 严格 `is True`（模型给 "true"/1 一律按单选）。
    """
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
    """把 `options` 解析成 `[{"text": …}]`；坏值丢弃、超量截断、标题过长则收敛。"""
    if not isinstance(raw, list):
        return []
    options: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = _tidy_option_title(_as_text(item.get("text")))
        if not text:
            continue
        options.append({"text": text})
        if len(options) >= MAX_ASK_OPTIONS:
            break
    return options


def _tidy_option_title(text: str) -> str:
    """把选项文本收敛成**卡片上的短标题**；收敛不了就原样返回。

    卡片上只要「名字」，解释归正文——模型最常见的越界写法是「名字：一句解释」
    （那段解释正文里已经写过一遍），卡片上再来一次就是重复啰嗦。这里把冒号后面
    那半段去掉；没有冒号却仍然超长时，截到上限并加省略号。

    与「选项超过 6 个截断」同一路风格：**不信任模型给的格式，但绝不因此让这一轮失败**。
    """
    if len(text) <= MAX_OPTION_TITLE_CHARS and "：" not in text and ":" not in text:
        return text

    head = text
    for separator in ("：", ":"):
        if separator in head:
            head = head.split(separator, 1)[0].strip()
            break
    # 只在冒号前面确实是个像样的短名字时才截；否则宁可原样留着（比如「A/B 两种写法」）
    if 2 <= len(head) <= MAX_OPTION_TITLE_CHARS:
        logger.info(
            "澄清选择题的选项标题已收敛",
            extra={"event": "ask.option_trimmed", "reason": "colon", "raw": text[:120]},
        )
        return head

    if len(text) > MAX_OPTION_TITLE_CHARS:
        logger.info(
            "澄清选择题的选项标题过长，已截断",
            extra={"event": "ask.option_trimmed", "reason": "too_long", "raw": text[:120]},
        )
        return text[:MAX_OPTION_TITLE_CHARS].rstrip() + "…"
    return text


def _as_text(value: Any) -> str:
    """只接受字符串（去掉首尾空白）；其它类型一律当空值。

    不做 `str(value)` 转换：模型的 JSON 里出现 `{"text": {"a": 1}}` 时，`str()` 会把
    一个字典塞进界面，不如当它是坏数据丢掉。
    """
    return value.strip() if isinstance(value, str) else ""
