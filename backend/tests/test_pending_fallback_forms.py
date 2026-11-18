"""待确认清单的另外两种**跑偏形态**：机器拼接的题干、正文里的规范取舍表。

背景（2026-11-19 端到端走查，方案 4 的真实产物）：

1. 提示词的清单项长这样——
   ``id: P-01 topic: F-03 格式校验验收标准 context: PRD 中 F-03 标注「待确认」… owner: 产品经理``
   用户看到的不是问题，是一条开发日志；最后一条尾部还残留 ``` （标记块被写进了代码围栏）。
2. 接口文档压根没写清单块，而是写了一整张「团队规范取舍表」，表里才有「待团队确认」——
   界面上「待确认问题」容器因此是空的（库里 `pending_items = "[]"`）。

这两条都钉在这里：题干必须还原成人话，表格行里的待确认必须被登记，而**正文不能被改动**。
"""

from __future__ import annotations

from backend.services.pending_items import BEGIN_SENTINEL, END_SENTINEL, extract

# 线上真实形态（节选自方案 4 的提示词套件）
_JUNK_QUESTION = (
    'id: P-01 topic: F-03 格式校验验收标准 context: PRD 中 F-03 验收标准标注「待确认」，'
    'B-1 校验规则草案需待其确认后定稿 owner: 产品经理'
)


def test_machine_joined_question_is_restored_to_a_sentence() -> None:
    """`id / topic / context / owner` 拼成的题干要还原成「问题（背景：…）」+ 拍板人。"""
    doc = (
        "# 演示文档\n\n正文若干。\n\n"
        f"{BEGIN_SENTINEL}\n"
        '[{"question": "'
        f"{_JUNK_QUESTION}"
        '", "sections": [], "decider": "", "importance": "P3"}]\n'
        f"{END_SENTINEL}\n"
    )
    clean, items = extract(doc)

    assert len(items) == 1
    question = items[0]["question"]
    # 题干里不能再出现控制字段
    assert "id:" not in question and "topic:" not in question and "owner:" not in question
    # 用 topic 当题干、context 变成背景
    assert question.startswith("F-03 格式校验验收标准")
    assert "背景：" in question
    # owner 落到拍板人，而不是留在题干里
    assert items[0]["decider"] == "产品经理"
    assert "产品经理" not in question


def test_fence_residue_in_question_is_stripped() -> None:
    """题干尾部残留的反引号要剥掉（标记块被写进代码围栏时会出现）。"""
    doc = (
        "# 演示文档\n\n正文。\n\n"
        f"{BEGIN_SENTINEL}\n"
        '[{"question": "错误态文案的 errorType 全集 owner: 后端 / 前端 ```", '
        '"sections": [], "decider": "", "importance": "P3"}]\n'
        f"{END_SENTINEL}\n"
    )
    _clean, items = extract(doc)

    # 题干 = 围栏之前的那句话；反引号与 owner 都不能留在题干里
    assert items[0]["question"] == "错误态文案的 errorType 全集"
    assert "```" not in items[0]["question"], "题干里不能残留围栏"


def _trade_off_table_doc() -> str:
    body = "# 接口文档\n\n" + ("正文段落。\n\n" * 40)
    return body + (
        "## 5. 团队规范取舍\n\n"
        "| 冲突点 | 处理 |\n"
        "| --- | --- |\n"
        "| 第 3 条 全站 Bearer 鉴权 | PRD 免登录 | 以 PRD 为准，不设计鉴权接口 |\n"
        "| 第 1 条 布尔字段 `is_`/`has_` 前缀 | 对外字段 lowerCamelCase | "
        "采用 `needLogin`，待团队确认 |\n"
        "| 第 8 条 创建类 POST 幂等 | 不持久化用户输入 | 不要求 `Idempotency-Key` |\n"
    )


def test_trade_off_table_rows_become_pending_items() -> None:
    """取舍表里标了「待团队确认」的行要登记成清单项，正文保持原样。"""
    doc = _trade_off_table_doc()
    clean, items = extract(doc)

    assert len(items) == 1, "只有那一行带「待团队确认」，其余行不该被收进来"
    assert "布尔字段" in items[0]["question"]
    assert "待团队确认" in items[0]["question"]
    assert items[0]["importance"] == "P3"
    # 表格是正文，不能剥
    assert clean == doc
    assert "团队规范取舍" in clean


def test_table_without_pending_marks_adds_nothing() -> None:
    """没有待确认字样的表格不能被误收。"""
    doc = "# 接口文档\n\n" + ("正文段落。\n\n" * 40) + (
        "| 字段 | 类型 |\n"
        "| --- | --- |\n"
        "| `code` | integer |\n"
        "| `message` | string |\n"
    )
    _clean, items = extract(doc)
    assert items == []
