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
    MAX_OPTION_TITLE_CHARS,
    MIN_ASK_OPTIONS,
    asks_from_tool_calls,
    build_ask_tool,
    render_ask_tool_instruction,
)


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
    assert options["items"]["required"] == ["text"]


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


def test_instruction_tells_the_model_to_explain_options_in_the_body() -> None:
    """正文逐条解释、卡片只放选项名——这是界面不重复显示同一段话的前提。"""
    spec = render_ask_tool_instruction()

    assert "逐条简要解释每个选项" in spec
    assert f"不超过 {MAX_OPTION_TITLE_CHARS} 个字" in spec
    assert "不要写冒号" in spec

    # 「只讲原则」不够：模型照样会写成「名字：一句解释」，必须给出正例与反例
    assert "正例" in spec and "反例" in spec
    assert "想法快速录入：" in spec  # 反例就是真实世界里它写出来的那种


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


def test_option_title_is_trimmed_at_the_colon() -> None:
    """卡片上只留「名字」：「名字：一句解释」里冒号后面那半段是正文该写的。

    这是用户明确反馈过的毛病——卡片与正文重复啰嗦。模型不听话时由后端兜住。
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
