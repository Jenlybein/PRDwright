"""澄清提问的结构化选择题：工具 schema、参数校验、提示词说明。

澄清对话里 AI 每次提问，就**调用 `ask_clarification_questions` 工具**输出结构化题目
（不再在正文里藏哨兵标记块）；后端从工具调用参数里取出题目、校验、分配 id/label，随 SSE
的 `done` 事件下发，前端据此把那道题的**题干 + 选项 + 每个选项的一句说明**渲染成可点的卡片
（用户也能走「都不合适、我自己写」）。

题干与选项说明为什么必须跟着结构化载荷走
--------------------------------------
旧口径是「卡片只放选项名，题干和逐条解释交给正文」。实测翻车：模型正文只写一句
「P0 级还有一个关键流程点需要确认：」，题干和解释都没写，卡片上就只剩四个短语
（`A 填完点按钮一次性生成` …），用户根本不知道自己在答什么、几个选项差在哪。
凡是「要模型在正文里记得再写一遍」的东西都会这样退化——这与本模块当初从哨兵块改
工具调用是同一个教训。现在题干、背景、每个选项的说明**全在载荷里**，卡片自足。

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

# 选项卡片上那行标题的字数上限。卡片现在**标题一行、说明一行**，所以标题必须短：
# 两行都写长句，一张卡片就撑成一段话，反而更难扫。
MAX_OPTION_TITLE_CHARS = 16

# 选项说明的字数上限。它要回答的是「选它意味着什么 / 代价是什么」，
# 一句话足够；写长了两行拼起来比正文还臃肿。
MAX_OPTION_DESC_CHARS = 40

# 每轮最多几道题。题目太多用户答不过来，卡片也会很长。
MAX_ASK_QUESTIONS = 2

# 选项标号，由**后端**按下标分配（与 `pending_items` 不信任模型的 `id` 同理：
# 让模型自己编号，迟早出现重号或跳号）。长度必须 >= MAX_ASK_OPTIONS。
_OPTION_LABELS = "ABCDEF"


def build_ask_tool() -> dict[str, Any]:
    """返回 `ask_clarification_questions` 工具描述（OpenAI / 方舟兼容的 function 形状）。

    工具参数的 `questions` 数组形状与前端 `ClarificationAsk` 一一对应：
    `[{question, background, multiple, options: [{text, description}]}]`。`id` 与 `label`
    **不由模型给**，后端在 `asks_from_tool_calls` → `_validate_records` 里分配。

    选项为什么有 `description`：只给「填完点按钮一次性生成」这种标题，用户看不出几个选项
    差在哪，等于没有选择。这句说明是用户做决定的依据，所以写进 schema（模型看得见），
    并且**必填**——让它可选就等于允许模型省掉。
    """
    return {
        "type": "function",
        "function": {
            "name": ASK_TOOL_NAME,
            "description": (
                "本轮向用户提出澄清用的结构化选择题。仅在确实需要用户拍板时调用；"
                "若本轮只是复述确认、或信息已足够（例如提示可以生成 PRD），不要调用本工具。"
                "一次调用带本轮全部（1~2 道）题。题干、背景、每个选项的说明都要在这里给全，"
                "前端会把它们渲染成自足的卡片——不要在正文里再重复列一遍。"
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
                                    "description": (
                                        "一句背景（为什么要问这件事、它卡住了后续哪一步），"
                                        "可空；会显示在题干下方"
                                    ),
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
                                                    "不要冒号、不要把说明塞进来"
                                                ),
                                                "maxLength": MAX_OPTION_TITLE_CHARS,
                                            },
                                            "description": {
                                                "type": "string",
                                                "description": (
                                                    "这个选项意味着什么 / 代价是什么，一句话、"
                                                    f"不超过 {MAX_OPTION_DESC_CHARS} 个字"
                                                    "（如「几句话就能开始，字段由 AI 推断」）。"
                                                    "必填：用户靠它区分选项。"
                                                    "标题若是档位词（轻量版 / 标准版 / 详尽版这类），"
                                                    "名字本身不携带信息，必须写清覆盖范围：含什么、不含什么"
                                                ),
                                                "maxLength": MAX_OPTION_DESC_CHARS,
                                            },
                                        },
                                        "required": ["text", "description"],
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

    分工是**载荷管内容、正文管过渡**：题干、背景、每个选项的一句说明全在工具参数里，
    前端据此渲染成自足的卡片；正文只写一两句「为什么现在要问这件事」，不再逐条列 A/B/C。
    """
    return (
        "本轮若向用户**提问**，就**调用 `ask_clarification_questions` 工具**输出结构化选择题；"
        "正文里不要再写任何标记块：\n"
        "- **题干、背景、每个选项的说明都由卡片承载**，正文只需要一两句话交代"
        "「为什么现在要问这件事」（它卡住了后续哪一步）。\n"
        "- **正文不要再逐条列 A / B / C**：选项连同各自的说明已经在卡片上，"
        "再列一遍就是同一段话印两次，用户还得来回对照。\n"
        "- **正文不要描述界面形态**（不要写「我列了几个选项」「请点选」这类话）："
        "卡片长什么样不由正文负责，正文一旦这么承诺，和实际渲染出来的东西就对不上。\n"
        "- 工具参数里每道题给全三样：`question`（题干）、`background`（一句背景，可空）、"
        "`options`。每个选项两个字段：\n"
        f"  - `text`：**短标题**，不超过 {MAX_OPTION_TITLE_CHARS} 个字，只写名字；\n"
        f"  - `description`：**一句话说清选它意味着什么 / 代价是什么**，"
        f"不超过 {MAX_OPTION_DESC_CHARS} 个字。这一句是用户做决定的**唯一依据，不许省**——"
        "只给标题（如「填完点按钮一次性生成」）而没有任何说明，用户根本看不出几个选项差在哪。\n"
        "  - 正例：`text` = 「想法快速录入」，`description` = 「几句话就能开头，字段由 AI 推断」；\n"
        "  - 反例：`text` = 「想法快速录入：几句话就能开头」，`description` 空着——"
        "说明塞进标题会让标题行挤成一段话，卡片上还只剩一个长句。\n"
        "  - ⚠️ 标题是**档位 / 程度 / 策略名**时（「轻量版」「标准版」「详尽版」「P0 优先」「渐进式」），"
        "名称本身不携带任何信息：`description` 必须写成**覆盖范围**——含什么、不含什么，"
        "尽量落到可比较的量（章节数 / 字段数 / 是否含接口与数据模型）。\n"
        "    - 反例：`text` = 「标准版」，`description` = 「适合大多数项目」——等于没说，"
        "用户仍然不知道它和轻量版差在哪；\n"
        "    - 正例：`text` = 「标准版」，`description` = 「6~8 节，含接口清单与核心流程，"
        "不含数据模型与权限细则」。\n"
        "  - 一组档位题的完整写法（照这个颗粒度给）：\n"
        "    `question` = 「第一版 PRD 的完整度要到什么程度？」\n"
        "    A「轻量版」=「3~4 节，只含目标用户与核心流程；不含接口、数据模型与权限」\n"
        "    B「标准版」=「6~8 节，含接口清单与核心流程；不含数据模型与权限细则」\n"
        "    C「详尽版」=「10 节以上，含全量字段契约与接口细节；适合多人协作交付」\n"
        "  - 调用工具前**逐题自查**：有没有哪个选项的 `description` 还空着、"
        "或者只是「适合大多数项目」这种废话？先把它的覆盖范围想清楚再调用；"
        "实在定义不出来的选项就**删掉**——留一个裸标题，比少一个选项更糟。\n"
        f"- 每道题给 {MIN_ASK_OPTIONS} ~ {MAX_ASK_OPTIONS} 个选项："
        f"**少于 {MIN_ASK_OPTIONS} 个选项的题会被直接丢弃**，"
        f"超过 {MAX_ASK_OPTIONS} 个只保留前 {MAX_ASK_OPTIONS} 个。\n"
        "- 选项之间要**真的互斥**：各自代表一种明确做法；别用「都可以」「看情况」这种没有信息量的项。\n"
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
            {
                "label": _OPTION_LABELS[position],
                "text": option["text"],
                "description": option["description"],
            }
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
    """把 `options` 解析成 `[{"text": …, "description": …}]`；坏值丢弃、超量截断、超长收敛。"""
    if not isinstance(raw, list):
        return []
    options: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = _as_text(item.get("text"))
        if not text:
            continue
        title, description = _split_option(text, _as_text(item.get("description")))
        if not description:
            # 这正是用户反馈的那个毛病：卡片上只有标题，几个选项看不出差别。
            # 留一行告警，便于事后统计「模型省说明」的发生率，据此决定要不要再加约束。
            logger.warning(
                "澄清选择题的选项没有说明，用户看不出选项差别",
                extra={"event": "ask.option_without_description", "option": title[:60]},
            )
        options.append({"text": title, "description": description})
        if len(options) >= MAX_ASK_OPTIONS:
            break
    return options


def _split_option(text: str, description: str) -> tuple[str, str]:
    """把模型给的选项收敛成「短标题 + 一句说明」两行。

    两种写法都收：

    - `description` 有值 → 它就是说明，`text` 只留名字（带冒号的话去掉尾巴，避免重复）；
    - `description` 空、而 `text` 是「名字：一句解释」 → **把冒号后面那半段挪进说明**。

    ⚠️ 第二种曾经是「直接把尾巴删掉」——那条规矩的前提是「解释归正文」，可正文未必写
    （实测模型只写一句过渡话就调工具），于是说明两头落空，用户只剩几个短语可看。
    现在卡片自带说明位，这段文字要搬过去而不是丢掉。
    """
    head, tail = _split_at_colon(text)
    if description:
        return _clip_title(head if tail else text), _clip_desc(description)
    if tail and 2 <= len(head) <= MAX_OPTION_TITLE_CHARS:
        logger.info(
            "澄清选择题的说明原先塞在标题里，已挪到说明位",
            extra={"event": "ask.option_split", "raw": text[:120]},
        )
        return head, _clip_desc(tail)
    return _clip_title(text), ""


def _split_at_colon(text: str) -> tuple[str, str]:
    """在第一个全角/半角冒号处切开，返回 `(冒号前, 冒号后)`；没有冒号时后半段为空串。"""
    for separator in ("：", ":"):
        if separator in text:
            head, _, tail = text.partition(separator)
            return head.strip(), tail.strip()
    return text.strip(), ""


def _clip_title(text: str) -> str:
    """标题超长则截到上限并加省略号（没有冒号可切时的兜底）。"""
    stripped = text.strip()
    if len(stripped) <= MAX_OPTION_TITLE_CHARS:
        return stripped
    logger.info(
        "澄清选择题的选项标题过长，已截断",
        extra={"event": "ask.option_trimmed", "reason": "too_long", "raw": stripped[:120]},
    )
    return stripped[:MAX_OPTION_TITLE_CHARS].rstrip() + "…"


def _clip_desc(text: str) -> str:
    """说明超长则截到上限并加省略号：一句话说不完，说明本来就没想清楚。"""
    stripped = text.strip()
    if len(stripped) <= MAX_OPTION_DESC_CHARS:
        return stripped
    return stripped[:MAX_OPTION_DESC_CHARS].rstrip() + "…"


def _as_text(value: Any) -> str:
    """只接受字符串（去掉首尾空白）；其它类型一律当空值。

    不做 `str(value)` 转换：模型的 JSON 里出现 `{"text": {"a": 1}}` 时，`str()` 会把
    一个字典塞进界面，不如当它是坏数据丢掉。
    """
    return value.strip() if isinstance(value, str) else ""
