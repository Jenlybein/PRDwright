"""澄清提问「结构化选择题」的用例。

盯三件事：**指令文本够不够模型照着做**（含字面量哨兵与硬约束）、**剥离与校验够不够严**
（坏数据一律丢项、不抛异常），以及**抑制器换了哨兵之后仍然一个字都不漏**。
"""

from __future__ import annotations

import json

from backend.services.clarification_ask import (
    ASK_BEGIN_SENTINEL,
    ASK_END_SENTINEL,
    MAX_ASK_OPTIONS,
    MIN_ASK_OPTIONS,
    extract_asks,
    make_ask_suppressor,
    render_ask_spec,
)
from backend.services.pending_items import (
    BEGIN_SENTINEL,
    END_SENTINEL,
    PendingBlockSuppressor,
)


def _block(payload: object) -> str:
    """把一段 JSON 包成 ASK 标记块。"""
    return f"{ASK_BEGIN_SENTINEL}\n{json.dumps(payload, ensure_ascii=False)}\n{ASK_END_SENTINEL}"


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
# render_ask_spec：给模型的指令
# ---------------------------------------------------------------------------


def test_spec_carries_the_literal_sentinels_and_shape() -> None:
    """指令里必须是**字面量**哨兵与可照抄的示例，否则模型只会产出格式漂移的块。"""
    spec = render_ask_spec()

    assert ASK_BEGIN_SENTINEL in spec
    assert ASK_END_SENTINEL in spec
    for field in ("question", "background", "multiple", "options", "text"):
        assert f"`{field}`" in spec
    assert json.loads(spec.split(ASK_BEGIN_SENTINEL)[1].split(ASK_END_SENTINEL)[0].strip())


def test_spec_states_the_hard_limits() -> None:
    """两条硬约束要写进指令：选项太少会被丢弃、太多会被截断。

    这与「待确认清单缺等级就丢弃」同一口径——把后果提前告诉模型，而不是等它写完了再静默丢掉。
    """
    spec = render_ask_spec()

    assert f"少于 {MIN_ASK_OPTIONS} 个选项的题会被直接丢弃" in spec
    assert f"保留前 {MAX_ASK_OPTIONS} 个" in spec


def test_spec_tells_the_model_to_explain_options_in_the_body() -> None:
    """正文逐条解释、块里只放短标题——这是界面不重复显示同一段话的前提。"""
    spec = render_ask_spec()

    assert "逐条简要解释每个选项" in spec
    assert "只放短标题" in spec
    assert "不要重复正文的解释文字" in spec


def test_spec_allows_a_turn_without_questions() -> None:
    """没有提问的那一轮不要附块，否则会渲染出一组空卡片。"""
    assert "没有任何提问" in render_ask_spec()


# ---------------------------------------------------------------------------
# extract_asks：剥离与校验
# ---------------------------------------------------------------------------


def test_extract_returns_clean_text_and_labelled_options() -> None:
    """正常块：正文被剥干净，选项的 label 由后端按顺序分配。"""
    text = f"你更倾向哪种？\n\n{_block([_ask()])}\n"

    clean, asks = extract_asks(text)

    assert ASK_BEGIN_SENTINEL not in clean
    assert ASK_END_SENTINEL not in clean
    assert clean == "你更倾向哪种？"
    assert len(asks) == 1
    assert asks[0]["id"] == "ask-1"
    assert asks[0]["question"] == "第一版要不要做离线同步？"
    assert asks[0]["background"] == "你提到用户常在弱网环境使用。"
    assert asks[0]["multiple"] is False
    assert [(option["label"], option["text"]) for option in asks[0]["options"]] == [
        ("A", "第一版就做离线同步"),
        ("B", "第一版只做在线"),
    ]


def test_extract_without_block_is_a_noop() -> None:
    """没有块时原样返回——没产块是常态，不是异常。"""
    text = "信息已经足够，可以点击生成 PRD"

    clean, asks = extract_asks(text)

    assert clean == text
    assert asks == []


def test_extract_drops_invalid_json_block_but_keeps_the_body() -> None:
    """块内 JSON 非法：剥掉块体、记 warning，正文照常返回，不抛异常。"""
    text = f"正文第一段。\n{ASK_BEGIN_SENTINEL}\n[{{这不是合法 JSON\n{ASK_END_SENTINEL}\n正文第二段。"

    clean, asks = extract_asks(text)

    assert asks == []
    assert ASK_BEGIN_SENTINEL not in clean
    assert "这不是合法 JSON" not in clean
    assert "正文第一段。" in clean
    assert "正文第二段。" in clean


def test_extract_keeps_unclosed_block_as_body() -> None:
    """块没闭合：多半是被截断的输出，整段保留为正文（哨兵是注释，渲染不可见）。"""
    text = f"正文。\n{ASK_BEGIN_SENTINEL}\n[{{\"question\": \"半截"

    clean, asks = extract_asks(text)

    assert asks == []
    assert "正文。" in clean
    assert "半截" in clean


def test_extract_drops_items_with_too_few_options() -> None:
    """选项 0 个或 1 个的题一律丢弃——一个选项不构成选择题，界面上也没地方安放答案。"""
    text = _block(
        [
            _ask(options=[]),
            _ask(question="只有一个选项的题", options=[{"text": "唯一方案"}]),
            _ask(question="这道题是好的"),
        ]
    )

    _, asks = extract_asks(text)

    assert [item["question"] for item in asks] == ["这道题是好的"]
    # id 按**留下**的顺序重排，不因为中间丢了项就跳号
    assert asks[0]["id"] == "ask-1"


def test_extract_truncates_extra_options() -> None:
    """选项超过上限：截断保留前 N 个，而不是把整道题丢掉。"""
    options = [{"text": f"方案 {index}"} for index in range(1, MAX_ASK_OPTIONS + 3)]

    _, asks = extract_asks(_block([_ask(options=options)]))

    assert len(asks[0]["options"]) == MAX_ASK_OPTIONS
    # 末位标号与实现里的标号表同源（"ABCDEF" 的第 MAX_ASK_OPTIONS 位）
    assert asks[0]["options"][-1]["label"] == "ABCDEF"[MAX_ASK_OPTIONS - 1]


def test_extract_drops_items_without_question() -> None:
    """没有题干的项丢掉，同一块里其它题不受影响。"""
    text = _block([{"options": [{"text": "甲"}, {"text": "乙"}]}, _ask(question="保留我")])

    _, asks = extract_asks(text)

    assert [item["question"] for item in asks] == ["保留我"]


def test_extract_drops_options_without_text() -> None:
    """选项没有正文就丢该选项；丢到不足两个时整题作废。"""
    text = _block(
        [
            _ask(question="丢一个还剩够", options=[{"text": "甲"}, {"text": "   "}, {"text": "乙"}]),
            _ask(question="丢完不够了", options=[{"text": "甲"}, {"text": 42}]),
        ]
    )

    _, asks = extract_asks(text)

    assert [item["question"] for item in asks] == ["丢一个还剩够"]
    assert [option["text"] for option in asks[0]["options"]] == ["甲", "乙"]


def test_extract_ignores_model_supplied_ids_and_labels() -> None:
    """模型自己写的 id / label 一律不认：让模型编号迟早出现重号或跳号。"""
    text = _block(
        [
            _ask(
                id="模型编的-id",
                options=[{"label": "Z", "text": "甲"}, {"label": "Y", "text": "乙"}],
            )
        ]
    )

    _, asks = extract_asks(text)

    assert asks[0]["id"] == "ask-1"
    assert [option["label"] for option in asks[0]["options"]] == ["A", "B"]


def test_extract_requires_a_real_boolean_for_multiple() -> None:
    """`multiple` 只在**真的是布尔真**时算多选：字符串 "true" 会把单选题渲染成多选。"""
    text = _block(
        [
            _ask(question="真布尔", multiple=True),
            _ask(question="字符串假值", multiple="true"),
        ]
    )

    _, asks = extract_asks(text)

    assert asks[0]["multiple"] is True
    assert asks[1]["multiple"] is False


def test_extract_normalizes_background() -> None:
    """`background` 缺失或不是字符串时退化为空串，而不是把字典塞进界面。"""
    text = _block(
        [
            _ask(question="没有背景", background=None),
            _ask(question="背景是字典", background={"a": 1}),
        ]
    )

    _, asks = extract_asks(text)

    assert asks[0]["background"] == ""
    assert asks[1]["background"] == ""


def test_extract_accumulates_multiple_blocks() -> None:
    """块可以有多个（模型偶尔会分几段写），全部累加、id 连续。"""
    first = _block([_ask(question="第一题")])
    second = _block([_ask(question="第二题")])

    clean, asks = extract_asks(f"开头。{first}中间。{second}结尾。")

    assert [item["id"] for item in asks] == ["ask-1", "ask-2"]
    assert [item["question"] for item in asks] == ["第一题", "第二题"]
    assert clean == "开头。中间。结尾。"


def test_extract_honours_id_prefix() -> None:
    """前缀可注入：将来若要按线程区分 id，不必改实现。"""
    _, asks = extract_asks(_block([_ask()]), id_prefix="prd")

    assert asks[0]["id"] == "prd-1"


# ---------------------------------------------------------------------------
# 抑制器：换了哨兵之后仍然一个字都不漏
# ---------------------------------------------------------------------------


def _feed_all(suppressor: PendingBlockSuppressor, chunks: list[str]) -> str:
    """把若干增量喂给抑制器，返回最终真正输出给用户的文本。"""
    out = "".join(suppressor.feed(chunk) for chunk in chunks)
    return out + suppressor.flush()


def test_ask_suppressor_passes_plain_text() -> None:
    """没有块时纯正文原样通过。"""
    assert _feed_all(make_ask_suppressor(), ["你更倾向", "哪种？"]) == "你更倾向哪种？"


def test_ask_suppressor_hides_block_split_across_chunks() -> None:
    """块被切成多段：任意切法都不该漏出哨兵或块体。

    这条是抑制器存在的全部理由——模型是流式输出的，块一定会在某个位置被切开。
    """
    block = f"{ASK_BEGIN_SENTINEL}\n{json.dumps([_ask()], ensure_ascii=False)}\n{ASK_END_SENTINEL}"
    for split in range(1, len(block)):
        left, right = block[:split], block[split:]
        out = _feed_all(make_ask_suppressor(), ["先看这句。", left, right, "收尾。"])
        assert ASK_BEGIN_SENTINEL not in out, f"切点在 {split} 时漏出了起始哨兵"
        assert ASK_END_SENTINEL not in out, f"切点在 {split} 时漏出了结束哨兵"
        assert "离线同步" not in out, f"切点在 {split} 时漏出了块体"
        assert out == "先看这句。收尾。"


def test_ask_suppressor_drops_unclosed_block_at_end() -> None:
    """流结束仍在块内：半截 JSON 绝不能当正文输出。"""
    out = _feed_all(make_ask_suppressor(), ["前面。", f"{ASK_BEGIN_SENTINEL}\n[{{\"question\": \"半截"])

    assert out == "前面。"
    assert "半截" not in out


def test_ask_suppressor_leaves_pending_blocks_alone() -> None:
    """两种哨兵互不干扰：ASK 抑制器不认识 PENDING 块，反之亦然。

    分开的哨兵是刻意的——澄清对话里不会出现待确认块，但两套机制将来若被拼在同一条流里，
    也不能互相吃掉对方的内容。
    """
    pending_block = f"{BEGIN_SENTINEL}\n[]\n{END_SENTINEL}"
    out = _feed_all(make_ask_suppressor(), [f"正文。{pending_block}"])

    assert BEGIN_SENTINEL in out


def test_parameterizing_sentinels_kept_the_pending_defaults() -> None:
    """参数化之后的回归守卫：无参构造仍按**待确认清单**的哨兵工作。

    这条盯着「加参数时别把默认值改错」——那是会静默放行块体、把半截 JSON 送到前端的改动。
    """
    block = f"{BEGIN_SENTINEL}\n[{{\"question\": \"待确认点\"}}]\n{END_SENTINEL}"
    out = _feed_all(PendingBlockSuppressor(), ["前。", block, "后。"])

    assert out == "前。后。"
    assert "待确认点" not in out
