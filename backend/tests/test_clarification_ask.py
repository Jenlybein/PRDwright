"""澄清提问「结构化选择题」的用例。

盯三件事：**工具 schema 与指令文本够不够模型照着做**、**工具参数校验够不够严**
（坏数据一律丢项、不抛异常），以及**从 tool_calls 提取题目的口径**（label/id 由后端
分配、multiple 严格布尔、题目带来源调用的 `tool_call_id` 供作答回灌配对）。
"""

from __future__ import annotations

from backend.services.clarification_ask import (
    ASK_TOOL_NAME,
    MAX_ASK_OPTIONS,
    MAX_ASK_QUESTIONS,
    MAX_OPTION_DESC_CHARS,
    MAX_OPTION_TITLE_CHARS,
    MIN_ASK_OPTIONS,
    asks_from_tool_calls,
    build_ask_tool,
    render_ask_tool_instruction,
)
from backend.services.prompts import CLARIFICATION_ASK_REMINDER


def _call(questions: object, *, name: str = ASK_TOOL_NAME, as_json: bool = False) -> dict[str, object]:
    """构造一条 tool_call（`_merge_tool_calls` 的输出形状）；`as_json` 时 args 为 JSON 串。"""
    import json

    args: object = {"questions": questions}
    if as_json:
        args = json.dumps(args, ensure_ascii=False)
    return {"name": name, "id": "call-1", "args": args}


def _ask(**overrides: object) -> dict[str, object]:
    """一条最小可用的题目，按需覆盖字段。"""
    base: dict[str, object] = {
        "question": "第一版要不要做离线同步？",
        "background": "你提到用户常在弱网环境使用。",
        "options": [{"text": "第一版就做离线同步"}, {"text": "第一版只做在线"}],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# build_ask_tool：工具 schema
# ---------------------------------------------------------------------------


def test_tool_schema_shape() -> None:
    """工具是 function 形状，名字对、parameters 收口到 questions。"""
    tool = build_ask_tool()

    assert tool["type"] == "function"
    assert tool["function"]["name"] == ASK_TOOL_NAME
    params = tool["function"]["parameters"]
    assert params["type"] == "object"
    assert "questions" in params["properties"]
    assert params["required"] == ["questions"]


def test_tool_schema_declares_the_limits() -> None:
    """schema 里的硬约束与常量同源：题目数、选项数都写进 schema，模型照着受约束。"""
    questions = build_ask_tool()["function"]["parameters"]["properties"]["questions"]
    options = questions["items"]["properties"]["options"]

    assert questions["minItems"] == 1
    assert questions["maxItems"] == MAX_ASK_QUESTIONS
    assert options["minItems"] == MIN_ASK_OPTIONS
    assert options["maxItems"] == MAX_ASK_OPTIONS
    assert options["items"]["required"] == ["text", "description"]


def test_tool_schema_item_requires_question_and_options() -> None:
    """每道题必填 question + options，其余可选。"""
    item = build_ask_tool()["function"]["parameters"]["properties"]["questions"]["items"]
    assert set(item["required"]) == {"question", "options"}


def test_option_text_declares_a_length_cap() -> None:
    """选项标题的字数上限要写进 schema：模型看得见上限，才不会把解释一起塞进来。"""
    options = build_ask_tool()["function"]["parameters"]["properties"]["questions"]["items"]["properties"]["options"]
    assert options["items"]["properties"]["text"]["maxLength"] == MAX_OPTION_TITLE_CHARS


# ---------------------------------------------------------------------------
# render_ask_tool_instruction：给模型的指令
# ---------------------------------------------------------------------------


def test_instruction_names_the_tool() -> None:
    """指令必须点名工具，模型才知道要调它。"""
    assert ASK_TOOL_NAME in render_ask_tool_instruction()


def test_instruction_states_the_hard_limits() -> None:
    """两条硬约束要写进指令：选项太少会被丢弃、太多会被截断。"""
    spec = render_ask_tool_instruction()

    assert f"少于 {MIN_ASK_OPTIONS} 个选项的题会被直接丢弃" in spec
    assert f"保留前 {MAX_ASK_OPTIONS} 个" in spec


def test_instruction_moves_the_option_explanation_onto_the_card() -> None:
    """说明归卡片、正文只写过渡——这是「用户看不清选项差别」那条反馈的落点。

    旧口径让正文逐条解释、卡片只放名字。实测模型正文往往只写一句过渡话就调工具，
    于是题干与说明两头落空：卡片上只剩「A 填完点按钮一次性生成」这类短语。
    """
    spec = render_ask_tool_instruction()

    # 说明必须写进 description，且点明它是用户做决定的唯一依据
    assert "description" in spec
    assert "不许省" in spec
    assert "唯一依据" in spec
    assert f"不超过 {MAX_OPTION_DESC_CHARS} 个字" in spec

    # 正文不再逐条列 A/B/C，题干也由卡片承载
    assert "题干、背景、每个选项的说明都由卡片承载" in spec
    assert "不要再逐条列" in spec

    # 「只讲原则」不够：模型照样会把说明塞进标题，必须给出正例与反例
    assert "正例" in spec and "反例" in spec
    assert "想法快速录入：" in spec  # 反例就是真实世界里它写出来的那种


def test_instruction_forces_coverage_for_scale_titles() -> None:
    """档位类标题（轻量版 / 标准版 / 详尽版）必须写清覆盖范围。

    实测这一类的说明最容易被省掉：模型觉得「标准版」这个名字已经说明了一切，
    于是 `description` 空着或写成「适合大多数项目」——用户仍然看不出三档差在哪。
    """
    spec = render_ask_tool_instruction()

    assert "档位" in spec
    for title in ("轻量版", "标准版", "详尽版"):
        assert title in spec
    # 判据要具体到「含什么、不含什么」，而不是「说清楚代价」这种形容词
    assert "覆盖范围" in spec
    assert "不含" in spec
    # 反例必须点出「适合大多数项目」这种正确但没信息量的写法
    assert "适合大多数项目" in spec
    # 抽象规则不够：档位题要给一整组可模仿的示例（模型模仿整组示例比照规则写稳）
    assert "第一版 PRD 的完整度" in spec
    assert "含全量字段契约" in spec


def test_instruction_asks_for_a_self_check_before_calling() -> None:
    """调用工具前要自查每个选项是否都有说明；定义不出来的选项宁可删掉。"""
    spec = render_ask_tool_instruction()

    assert "自查" in spec
    assert "删掉" in spec


def test_instruction_keeps_the_body_out_of_ui_wording() -> None:
    """正文不描述界面形态：「我列了几个选项」「请点选」这类话不该由正文说。

    卡片是照着工具参数渲染的，正文一旦承诺「下面有选项」，两者就对不上了。
    """
    spec = render_ask_tool_instruction()

    assert "不要描述界面形态" in spec
    assert "请点选" in spec


def test_option_description_field_mentions_scale_titles() -> None:
    """字段说明也要点破档位词：模型写这个字段时看的就是这段。"""
    options = build_ask_tool()["function"]["parameters"]["properties"]["questions"]["items"]["properties"]["options"]
    field = options["items"]["properties"]["description"]["description"]

    assert "档位" in field
    assert "含什么" in field


def test_reminder_repeats_the_scale_rule() -> None:
    """续聊轮次的格式重申也要带上档位规则——长对话里最先退化的就是这一句。"""
    assert "档位" in CLARIFICATION_ASK_REMINDER


def test_instruction_warns_against_non_exclusive_options() -> None:
    """选项要互斥：否则用户点哪个都一样，等于没得选。"""
    assert "互斥" in render_ask_tool_instruction()


def test_instruction_allows_a_turn_without_questions() -> None:
    """没有提问的那一轮不要调工具，否则会渲染出一组空卡片。"""
    assert "没有任何提问" in render_ask_tool_instruction()


# ---------------------------------------------------------------------------
# asks_from_tool_calls：提取与校验
# ---------------------------------------------------------------------------


def test_extract_returns_labelled_options_and_ids() -> None:
    """正常工具调用：选项的 label、题目 id 由后端按顺序分配。"""
    asks = asks_from_tool_calls([_call([_ask()])])

    assert len(asks) == 1
    assert asks[0]["id"] == "ask-1"
    assert asks[0]["question"] == "第一版要不要做离线同步？"
    assert asks[0]["background"] == "你提到用户常在弱网环境使用。"
    assert asks[0]["multiple"] is False
    assert [(option["label"], option["text"]) for option in asks[0]["options"]] == [
        ("A", "第一版就做离线同步"),
        ("B", "第一版只做在线"),
    ]


def test_extract_accepts_args_as_json_string() -> None:
    """`args` 可能是 JSON 串（未解析）或 dict（LangChain 已解析），两种都要能取。"""
    asks = asks_from_tool_calls([_call([_ask()], as_json=True)])
    assert len(asks) == 1


def test_extract_without_tool_call_is_a_noop() -> None:
    """没有工具调用时返回空——没调工具是常态，不是异常。"""
    assert asks_from_tool_calls([]) == []
    assert asks_from_tool_calls(None) == []


def test_extract_ignores_other_tools() -> None:
    """只认 `ask_clarification_questions`，其它工具忽略。"""
    assert asks_from_tool_calls([_call([_ask()], name="别的工具")]) == []


def test_extract_bad_args_json_degrades_to_empty() -> None:
    """args 是非法 JSON：降级为空，不抛异常（生成不因此失败）。"""
    assert asks_from_tool_calls([{"name": ASK_TOOL_NAME, "args": "{不是合法 JSON"}]) == []


def test_extract_drops_items_with_too_few_options() -> None:
    """选项 0 个或 1 个的题一律丢弃——一个选项不构成选择题。"""
    asks = asks_from_tool_calls(
        [
            _call(
                [
                    _ask(options=[]),
                    _ask(question="只有一个选项的题", options=[{"text": "唯一方案"}]),
                    _ask(question="这道题是好的"),
                ]
            )
        ]
    )

    assert [item["question"] for item in asks] == ["这道题是好的"]
    # id 按**留下**的顺序重排，不因为中间丢了项就跳号
    assert asks[0]["id"] == "ask-1"


def test_extract_truncates_extra_options() -> None:
    """选项超过上限：截断保留前 N 个，而不是把整道题丢掉。"""
    options = [{"text": f"方案 {index}"} for index in range(1, MAX_ASK_OPTIONS + 3)]

    asks = asks_from_tool_calls([_call([_ask(options=options)])])

    assert len(asks[0]["options"]) == MAX_ASK_OPTIONS
    assert asks[0]["options"][-1]["label"] == "ABCDEF"[MAX_ASK_OPTIONS - 1]


def test_extract_drops_items_without_question() -> None:
    """没有题干的项丢掉，同一 tool_call 里其它题不受影响。"""
    asks = asks_from_tool_calls(
        [_call([{"options": [{"text": "甲"}, {"text": "乙"}]}, _ask(question="保留我")])]
    )

    assert [item["question"] for item in asks] == ["保留我"]


def test_extract_drops_options_without_text() -> None:
    """选项没有正文就丢该选项；丢到不足两个时整题作废。"""
    asks = asks_from_tool_calls(
        [
            _call(
                [
                    _ask(question="丢一个还剩够", options=[{"text": "甲"}, {"text": "   "}, {"text": "乙"}]),
                    _ask(question="丢完不够了", options=[{"text": "甲"}, {"text": 42}]),
                ]
            )
        ]
    )

    assert [item["question"] for item in asks] == ["丢一个还剩够"]
    assert [option["text"] for option in asks[0]["options"]] == ["甲", "乙"]


def test_extract_ignores_model_supplied_ids_and_labels() -> None:
    """模型自己写的 id / label 一律不认：让模型编号迟早出现重号或跳号。"""
    asks = asks_from_tool_calls(
        [
            _call(
                [
                    _ask(
                        id="模型编的-id",
                        options=[{"label": "Z", "text": "甲"}, {"label": "Y", "text": "乙"}],
                    )
                ]
            )
        ]
    )

    assert asks[0]["id"] == "ask-1"
    assert [option["label"] for option in asks[0]["options"]] == ["A", "B"]


def test_extract_requires_a_real_boolean_for_multiple() -> None:
    """`multiple` 只在**真的是布尔真**时算多选：字符串 "true" 会把单选题渲染成多选。"""
    asks = asks_from_tool_calls(
        [
            _call(
                [
                    _ask(question="真布尔", multiple=True),
                    _ask(question="字符串假值", multiple="true"),
                ]
            )
        ]
    )

    assert asks[0]["multiple"] is True
    assert asks[1]["multiple"] is False


def test_extract_normalizes_background() -> None:
    """`background` 缺失或不是字符串时退化为空串，而不是把字典塞进界面。"""
    asks = asks_from_tool_calls(
        [
            _call(
                [
                    _ask(question="没有背景", background=None),
                    _ask(question="背景是字典", background={"a": 1}),
                ]
            )
        ]
    )

    assert asks[0]["background"] == ""
    assert asks[1]["background"] == ""


def test_extract_accumulates_multiple_tool_calls() -> None:
    """多个 tool_call 的题目拼接、id 连续（正常只有一个，但要能兜住并行调用）。"""
    asks = asks_from_tool_calls(
        [
            _call([_ask(question="第一题")]),
            _call([_ask(question="第二题")]),
        ]
    )

    assert [item["id"] for item in asks] == ["ask-1", "ask-2"]
    assert [item["question"] for item in asks] == ["第一题", "第二题"]


def test_extract_honours_id_prefix() -> None:
    """前缀可注入：将来若要按线程区分 id，不必改实现。"""
    asks = asks_from_tool_calls([_call([_ask()])], id_prefix="prd")

    assert asks[0]["id"] == "prd-1"


def test_extract_carries_tool_call_id() -> None:
    """题目带回发起调用的 tool_call_id：作答轮靠它把答案对回这次调用。"""
    asks = asks_from_tool_calls([_call([_ask()])])

    assert asks[0]["tool_call_id"] == "call-1"


def test_extract_carries_source_call_id_per_item() -> None:
    """多调用拼接时每道题记各自的调用 id，不能都挂到第一个上。"""
    first = _call([_ask(question="第一题")])
    second = {"name": ASK_TOOL_NAME, "id": "call-2", "args": {"questions": [_ask(question="第二题")]}}

    asks = asks_from_tool_calls([first, second])

    assert asks[0]["tool_call_id"] == "call-1"
    assert asks[1]["tool_call_id"] == "call-2"


def test_option_description_is_carried_through() -> None:
    """每个选项自带一句说明：卡片靠它让用户看出差别，后端不许吞掉。"""
    asks = asks_from_tool_calls(
        [
            _call(
                [
                    _ask(
                        options=[
                            {"text": "想法快速录入", "description": "几句话就能开头"},
                            {"text": "结构化引导填空", "description": "按 PRD 框架分步填"},
                        ]
                    )
                ]
            )
        ]
    )

    assert [(option["label"], option["text"], option["description"]) for option in asks[0]["options"]] == [
        ("A", "想法快速录入", "几句话就能开头"),
        ("B", "结构化引导填空", "按 PRD 框架分步填"),
    ]


def test_option_without_description_degrades_to_empty_string() -> None:
    """模型漏了说明也不让这一轮失败：说明位留空串，选项照常可点。"""
    asks = asks_from_tool_calls(
        [_call([_ask(options=[{"text": "第一版就做离线同步"}, {"text": "第一版只做在线"}])])]
    )

    assert [option["description"] for option in asks[0]["options"]] == ["", ""]


def test_overlong_option_description_is_clipped() -> None:
    """说明超长截断：一句话说不完，说明本来就没想清楚。"""
    long = "选这个的话要先做数据模型设计、再逐字段对齐、最后补校验规则，第一版很可能拖很久才能交付"
    asks = asks_from_tool_calls(
        [_call([_ask(options=[{"text": "结构化引导填空", "description": long}, {"text": "想法快速录入"}])])]
    )

    clipped = asks[0]["options"][0]["description"]
    assert clipped.endswith("…")
    assert len(clipped) <= MAX_OPTION_DESC_CHARS + 1


def test_option_title_is_trimmed_at_the_colon_but_the_tail_becomes_description() -> None:
    """「名字：一句解释」里的后半段**挪进说明位**，不是丢掉。

    这是用户反馈「看不到选项差别」的直接成因：从前这里把冒号后面整段删掉，
    而它的前提「解释归正文」未必成立——模型常常不写，于是说明两头落空。
    """
    asks = asks_from_tool_calls(
        [
            _call(
                [
                    _ask(
                        options=[
                            {"text": "想法快速录入：几句话/要点输入，自动识别关键字段"},
                            {"text": "结构化引导填空：按 PRD 框架分步填写，带提示示例"},
                        ]
                    )
                ]
            )
        ]
    )

    assert [option["text"] for option in asks[0]["options"]] == [
        "想法快速录入",
        "结构化引导填空",
    ]
    assert [option["description"] for option in asks[0]["options"]] == [
        "几句话/要点输入，自动识别关键字段",
        "按 PRD 框架分步填写，带提示示例",
    ]


def test_explicit_description_wins_over_the_colon_tail() -> None:
    """两个字段都给时以 `description` 为准，标题里那截尾巴只当重复去掉。"""
    asks = asks_from_tool_calls(
        [_call([_ask(options=[{"text": "想法快速录入：旧的说明", "description": "新的说明"}, {"text": "另一个"}])])]
    )

    option = asks[0]["options"][0]
    assert option["text"] == "想法快速录入"
    assert option["description"] == "新的说明"


def test_overlong_option_title_is_truncated() -> None:
    """没有冒号却仍然超长：截到上限并加省略号，别让一张卡片撑成一段话。"""
    long = "一键生成排版好的 PRD 并支持导出为 Markdown 与 PDF 等多种格式"
    asks = asks_from_tool_calls([_call([_ask(options=[{"text": long}, {"text": "版本历史与对比"}])])])

    trimmed = asks[0]["options"][0]["text"]
    assert trimmed.endswith("…")
    assert len(trimmed) <= MAX_OPTION_TITLE_CHARS + 1


def test_short_option_title_is_left_alone() -> None:
    """本来就是短标题的，一个字都不许动。"""
    asks = asks_from_tool_calls(
        [_call([_ask(options=[{"text": "第一版就做离线同步"}, {"text": "第一版只做在线"}])])]
    )
    assert [option["text"] for option in asks[0]["options"]] == [
        "第一版就做离线同步",
        "第一版只做在线",
    ]


def test_colon_is_not_trimmed_when_head_is_not_a_name() -> None:
    """冒号前面不像个名字时（例如只有一个字母），宁可原样留着，别切出个废标题。"""
    asks = asks_from_tool_calls([_call([_ask(options=[{"text": "X：某个选项"}, {"text": "另一个选项"}])])])

    assert asks[0]["options"][0]["text"] == "X：某个选项"


def test_extract_omits_tool_call_id_when_source_has_none() -> None:
    """源调用没有 id 就不带这个键——空壳 id 只会让作答回灌配错对。"""
    call = _call([_ask()])
    del call["id"]

    asks = asks_from_tool_calls([call])

    assert "tool_call_id" not in asks[0]
