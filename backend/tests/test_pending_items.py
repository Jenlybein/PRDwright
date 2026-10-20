"""待确认清单：标记块剥离、解析、流式抑制与规格渲染。

这些逻辑是「文档所有权后端化」之后新增的第一类产物元数据，必须**只在这一个文件里
实现一份**：前端不持有任何提示词、不解析清单的语义，只负责把后端返回的 `pending_items`
原样展示。因此这里的剥离与抑制是「正文里永远不含哨兵」的唯一保证。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

import pytest
from fakes import ScriptedLLM

from backend.api.schemas import StreamEvent
from backend.services.document_service import DocumentService
from backend.services.pending_items import (
    BEGIN_SENTINEL,
    END_SENTINEL,
    PendingBlockSuppressor,
    extract,
    format_pending_items,
    render_spec,
)


# ---------------------------------------------------------------------------
# render_spec
# ---------------------------------------------------------------------------


def test_render_spec_includes_literal_sentinels() -> None:
    """提示词里必须给出字面量哨兵，模型才能产出可被精确剥离的块。"""
    spec = render_spec()
    assert BEGIN_SENTINEL in spec
    assert END_SENTINEL in spec
    assert "question" in spec


def test_render_spec_mentions_importance_levels() -> None:
    """生成指令里必须给出**完整的 P0~P6**，并说明「缺了等级会被丢弃」。

    前端 relay 的澄清顺序完全依赖这个字段；等级集少写一档，模型就可能标出后端不认的
    值，那一条会在校验时被丢掉——用测试把指令与校验口径钉在一起。
    """
    spec = render_spec()
    assert "importance" in spec
    for level in ("P0", "P1", "P2", "P3", "P4", "P5", "P6"):
        assert level in spec, level
    # 没有兜底等级：指令必须把「缺失/超范围会被丢弃」讲明白
    assert "丢弃" in spec


# ---------------------------------------------------------------------------
# extract：剥离 + 解析 + id 分配
# ---------------------------------------------------------------------------


def test_extract_returns_clean_text_and_items() -> None:
    """正常块：正文被剥离，清单被解析，id 由后端分配。"""
    doc = (
        "# 产品需求文档\n\n## 1. 概述\n正文。\n\n"
        f"{BEGIN_SENTINEL}\n"
        '[{"question": "是否要做海外版？", "sections": ["2.1"], "decider": "产品负责人", '
        '"importance": "P0"}]\n'
        f"{END_SENTINEL}\n"
    )
    clean, items = extract(doc)
    assert BEGIN_SENTINEL not in clean
    assert END_SENTINEL not in clean
    assert "正文。" in clean
    assert len(items) == 1
    assert items[0]["id"] == "p-1"
    assert items[0]["question"] == "是否要做海外版？"
    assert items[0]["sections"] == ["2.1"]
    assert items[0]["decider"] == "产品负责人"
    assert items[0]["importance"] == "P0"


def test_extract_without_block_returns_text_untouched() -> None:
    """没有标记块：声明（原文, []），正文一字不改。"""
    doc = "# 标题\n\n正文。\n"
    clean, items = extract(doc)
    assert clean == doc
    assert items == []


def test_extract_strips_model_supplied_id() -> None:
    """不信任模型给的 id：即便块里带了 id，也要被后端分配的同前缀 id 覆盖。"""
    doc = (
        "正文。\n"
        f"{BEGIN_SENTINEL}\n"
        '[{"id": "EVIL", "question": "问题？", "importance": "P2"}]\n'
        f"{END_SENTINEL}\n"
    )
    _, items = extract(doc)
    assert items[0]["id"] == "p-1"
    assert "id" not in items[0] or items[0]["id"] == "p-1"


def test_extract_assigns_prefix_based_ids() -> None:
    """分章节生成传 section.id 作前缀，跨章节不重号。"""
    doc1 = f"a\n{BEGIN_SENTINEL}\n[{{\"question\": \"q1\", \"importance\": \"P1\"}}]\n{END_SENTINEL}\n"
    doc2 = f"b\n{BEGIN_SENTINEL}\n[{{\"question\": \"q2\", \"importance\": \"P1\"}}]\n{END_SENTINEL}\n"
    _, items1 = extract(doc1, id_prefix="features")
    _, items2 = extract(doc2, id_prefix="scope")
    assert items1[0]["id"] == "features-1"
    assert items2[0]["id"] == "scope-1"


def test_extract_multiple_blocks_accumulate() -> None:
    """一份正文里多个块：全部剥离，id 按出现顺序连续分配。"""
    doc = (
        f"{BEGIN_SENTINEL}\n[{{\"question\": \"q1\", \"importance\": \"P1\"}}]\n{END_SENTINEL}\n"
        "中段。\n"
        f"{BEGIN_SENTINEL}\n"
        '[{"question": "q2", "importance": "P0"}, {"question": "q3", "importance": "P2"}]\n'
        f"{END_SENTINEL}\n"
    )
    clean, items = extract(doc)
    assert "中段。" in clean
    assert len(items) == 3
    assert [item["id"] for item in items] == ["p-1", "p-2", "p-3"]


def test_extract_empty_array_block() -> None:
    """块内是 `[]`：干净正文返回，清单为空。"""
    doc = f"正文。\n{BEGIN_SENTINEL}\n[]\n{END_SENTINEL}\n"
    clean, items = extract(doc)
    assert "正文。" in clean
    assert items == []


def test_extract_bad_json_block_is_ignored() -> None:
    """块内 JSON 非法：该块跳过、正文保留、清单为空、不抛异常。"""
    doc = f"正文。\n{BEGIN_SENTINEL}\n不是 json\n{END_SENTINEL}\n"
    clean, items = extract(doc)
    assert "正文。" in clean
    assert items == []


def test_extract_whitelist_keeps_only_known_fields() -> None:
    """只保留 question/sections/decider/importance；模型夹带的其它字段被丢弃。

    顺带覆盖等级的大小写规整：模型写小写 `p3` 也认。
    """
    doc = (
        f"{BEGIN_SENTINEL}\n"
        '[{"question": "q", "sections": [], "decider": "x", "secret": "drop-me", '
        '"importance": "p3"}]\n'
        f"{END_SENTINEL}\n"
    )
    _, items = extract(doc)
    assert items[0] == {
        "id": "p-1",
        "question": "q",
        "sections": [],
        "decider": "x",
        "importance": "P3",
    }


def test_extract_sorts_by_importance_descending() -> None:
    """清单按重要性从高到低排序（P0 → P6），id 按排好的顺序连续重分配。

    这是「越重要越先提问」的地基——模型的书写顺序不代表重要程度，
    顺序只由 `importance` 决定。
    """
    doc = (
        "正文。\n"
        f"{BEGIN_SENTINEL}\n"
        "[\n"
        '  {"question": "最后再问的细节？", "importance": "P6"},\n'
        '  {"question": "最要紧的前提？", "importance": "P0"},\n'
        '  {"question": "中间某档？", "importance": "P3"}\n'
        "]\n"
        f"{END_SENTINEL}\n"
    )
    _, items = extract(doc)
    assert [item["question"] for item in items] == [
        "最要紧的前提？",
        "中间某档？",
        "最后再问的细节？",
    ]
    assert [item["importance"] for item in items] == ["P0", "P3", "P6"]
    # id 顺序即提问顺序
    assert [item["id"] for item in items] == ["p-1", "p-2", "p-3"]


def test_extract_drops_items_without_a_valid_importance() -> None:
    """没给等级 / 给了不认识的等级 / 超出 P0~P6 → 该项**不通过校验、被丢弃**。

    这里刻意**不兜底**：猜一个默认等级会打乱「越重要越先问」的顺序，还会把一条
    没标级的问题伪装成已分级；丢弃只影响这一项，合法的项照常保留。
    """
    doc = (
        "正文。\n"
        f"{BEGIN_SENTINEL}\n"
        "[\n"
        '  {"question": "没标等级？"},\n'
        '  {"question": "乱标等级？", "importance": "critical"},\n'
        '  {"question": "超范围？", "importance": "P7"},\n'
        '  {"question": "要紧的？", "importance": "P0"}\n'
        "]\n"
        f"{END_SENTINEL}\n"
    )
    _, items = extract(doc)
    assert [item["question"] for item in items] == ["要紧的？"]
    assert items[0]["importance"] == "P0"
    # 丢弃不占号：留下的仍然是连续的 p-1
    assert items[0]["id"] == "p-1"


def test_extract_keeps_stable_order_within_same_importance() -> None:
    """同一等级内保持模型原本的书写顺序（稳定排序），避免顺序忽上忽下。"""
    doc = (
        "正文。\n"
        f"{BEGIN_SENTINEL}\n"
        "[\n"
        '  {"question": "第一条 P1？", "importance": "P1"},\n'
        '  {"question": "第二条 P1？", "importance": "P1"}\n'
        "]\n"
        f"{END_SENTINEL}\n"
    )
    _, items = extract(doc)
    assert [item["question"] for item in items] == ["第一条 P1？", "第二条 P1？"]
    assert [item["id"] for item in items] == ["p-1", "p-2"]


def test_extract_sorts_across_multiple_blocks() -> None:
    """分章节生成时每章一个块：跨块的条目也要一起按重要性重排并统一编号。"""
    doc = (
        "一章。\n"
        f"{BEGIN_SENTINEL}\n"
        '[{"question": "次要的？", "importance": "P2"}]\n'
        f"{END_SENTINEL}\n"
        "二章。\n"
        f"{BEGIN_SENTINEL}\n"
        '[{"question": "要紧的？", "importance": "P0"}]\n'
        f"{END_SENTINEL}\n"
    )
    clean, items = extract(doc)
    assert "一章。" in clean
    assert "二章。" in clean
    assert [item["importance"] for item in items] == ["P0", "P2"]
    assert [item["id"] for item in items] == ["p-1", "p-2"]


def test_extract_unclosed_block_retained_as_text() -> None:
    """块没有结束哨兵：视为漏出的半截，块内 JSON 当正文保留、不当 JSON 解析。"""
    doc = f"正文。\n{BEGIN_SENTINEL}\n[{{\"question\": \"q\"}}]\n尾部"
    clean, items = extract(doc)
    # 起始哨兵在 split 时被移除；但块内的原始 JSON 文本仍随正文保留。
    assert END_SENTINEL not in clean
    assert '"question"' in clean
    assert "q" in clean
    assert "尾部" in clean
    assert items == []


# ---------------------------------------------------------------------------
# PendingBlockSuppressor：流式逐 chunk 抑制
# ---------------------------------------------------------------------------


def _feed_all(suppressor: PendingBlockSuppressor, chunks: list[str]) -> str:
    """把若干 chunk 依次喂给抑制器并拼接输出。"""
    out: list[str] = []
    for chunk in chunks:
        out.append(suppressor.feed(chunk))
    out.append(suppressor.flush())
    return "".join(out)


def test_suppressor_passes_plain_text() -> None:
    """纯正文：原样输出，没有任何损耗。"""
    assert _feed_all(PendingBlockSuppressor(), ["普通", "正文。"]) == "普通正文。"


def test_suppressor_hides_block_in_single_chunk() -> None:
    """标记块在一个 chunk 内：哨兵与块体完全不输出，只保留块外的真实文本。"""
    chunk = (
        "前面。\n"
        f"{BEGIN_SENTINEL}\n[{{\"question\": \"q\"}}]\n{END_SENTINEL}\n"
        "后面。"
    )
    out = _feed_all(PendingBlockSuppressor(), [chunk])
    assert BEGIN_SENTINEL not in out
    assert END_SENTINEL not in out
    assert "q" not in out
    assert "前面。" in out
    assert "后面。" in out


def test_suppressor_hides_block_split_across_chunks() -> None:
    """标记块被切成多段：任意切法都不应漏出哨兵或块体。"""
    block = (
        f"{BEGIN_SENTINEL}\n[{{\"question\": \"待确认点\"}}]\n{END_SENTINEL}"
    )
    for split in range(1, len(block)):
        left, right = block[:split], block[split:]
        out = _feed_all(
            PendingBlockSuppressor(),
            ["前置文本。", left, right, "后置文本。"],
        )
        assert BEGIN_SENTINEL not in out, f"切点在 {split} 时漏出了起始哨兵"
        assert END_SENTINEL not in out, f"切点在 {split} 时漏出了结束哨兵"
        assert "待确认点" not in out, f"切点在 {split} 时漏出了块体"
        assert out == "前置文本。后置文本。"


def test_suppressor_drops_unclosed_block_at_end() -> None:
    """流结束仍在块内：半截块体不输出（不能当正文）。"""
    chunks = ["前面。", f"{BEGIN_SENTINEL}\n[{{\"question\": \"半截"]
    out = _feed_all(PendingBlockSuppressor(), chunks)
    assert "半截" not in out
    assert out == "前面。"


def test_suppressor_carries_cross_chunk_prefix() -> None:
    """哨兵被切断在 chunk 边界：前缀暂存到下一轮，仍不漏出。"""
    # 起始哨兵被从中间切开，前半在 chunk1、后半在 chunk2
    split = 5
    sentinel_head, sentinel_tail = BEGIN_SENTINEL[:split], BEGIN_SENTINEL[split:]
    out = _feed_all(
        PendingBlockSuppressor(),
        [f"文本A{sentinel_head}", f"{sentinel_tail}\n[{{}}]\n{END_SENTINEL}文本B"],
    )
    assert BEGIN_SENTINEL not in out
    assert out == "文本A文本B"


# ---------------------------------------------------------------------------
# 端到端：三条生成线都产出「不含哨兵的正文 + 剥离出来的清单」
# ---------------------------------------------------------------------------


def _pending_block(question: str) -> str:
    """拼一个合法的标记块，模拟模型在正文末尾附的清单。

    带 `importance` 是**必须**的：等级缺失或不合法时 `extract` 会把该项直接丢弃，
    这里若省掉，整条链路的用例都会变成「清单为空」。
    """
    payload = json.dumps(
        [
            {
                "question": question,
                "sections": ["2.1"],
                "decider": "产品负责人",
                "importance": "P1",
            }
        ],
        ensure_ascii=False,
    )
    return f"{BEGIN_SENTINEL}\n{payload}\n{END_SENTINEL}"


def _in_two_chunks(text: str) -> list[str]:
    """把文本切成两块吐出：覆盖「标记块被切在 chunk 边界」这条真实路径。"""
    middle = len(text) // 2
    return [text[:middle], text[middle:]]


async def _drain(events: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    """同步跑完一条生成流，收集全部事件。"""
    return [event async for event in events]


def _assert_no_sentinel(*texts: str) -> None:
    """哨兵一律不得出现在任何一份「会给用户看 / 会落库」的文本里。"""
    for text in texts:
        assert BEGIN_SENTINEL not in text
        assert END_SENTINEL not in text


def test_prd_line_strips_block_from_chunks_parts_and_reply() -> None:
    """PRD 分章线：`chunk` / `part` / `done.reply` 三处都不含哨兵，清单随 `done` 带出。

    每章都会产出自己的块，因此清单条数等于章节数，且 id 按「章节 + 序号」分配——
    前端拿到后可直接定位是哪一章有待确认项。
    """
    from backend.services.prd_section_writer import PRD_SECTIONS, PrdSectionWriter

    body = "章节正文\n\n" + _pending_block("这一章的指标口径谁拍板？")
    writer = PrdSectionWriter(
        llm=ScriptedLLM(_in_two_chunks(body)), review_enabled=False
    )
    events = asyncio.run(
        _drain(writer.stream(requirements="做一个演示产品", product_name="演示产品"))
    )

    chunks = "".join(event.content for event in events if event.type == "chunk")
    parts = "".join(
        event.data["text"] for event in events if event.type == "part"
    )
    done = next(event for event in events if event.type == "done")
    reply = str(done.data["reply"])

    _assert_no_sentinel(chunks, parts, reply)
    # 正文保住了，问题只留在清单里
    assert "章节正文" in reply
    assert "指标口径" not in reply

    items = done.data["pending_items"]
    assert len(items) == len(PRD_SECTIONS)
    assert [item["id"] for item in items] == [f"{s.id}-1" for s in PRD_SECTIONS]
    assert all(item["question"] == "这一章的指标口径谁拍板？" for item in items)


def test_api_docs_line_strips_block_from_chunks_and_reply() -> None:
    """接口文档线：实时 `chunk` 与 `done.reply` 都不含哨兵，清单在 `done.data` 里。"""
    body = "# 接口文档\n\n接口正文\n\n" + _pending_block("分页上限取多少？")
    service = DocumentService(llm=ScriptedLLM(_in_two_chunks(body)))
    events = asyncio.run(_drain(service.generate_api_docs_stream(prd_content="# PRD")))

    chunks = "".join(event.content for event in events if event.type == "chunk")
    done = next(event for event in events if event.type == "done")
    reply = str(done.data["reply"])

    _assert_no_sentinel(chunks, reply)
    assert "接口正文" in reply
    assert "分页上限" not in reply
    assert done.data["pending_items"] == [
        {
            "question": "分页上限取多少？",
            "sections": ["2.1"],
            "decider": "产品负责人",
            "importance": "P1",
            "id": "p-1",
        }
    ]


def test_prompts_line_strips_block_from_chunks_and_reply() -> None:
    """提示词线：与接口文档同款——提示词套件也是「生成的第一类产物」，清单同样剥离。"""
    body = "# 提示词套件\n\n提示词正文\n\n" + _pending_block("目标模型定哪个？")
    service = DocumentService(llm=ScriptedLLM(_in_two_chunks(body)))
    events = asyncio.run(_drain(service.generate_prompts_stream(prd_content="# PRD")))

    chunks = "".join(event.content for event in events if event.type == "chunk")
    done = next(event for event in events if event.type == "done")
    reply = str(done.data["reply"])

    _assert_no_sentinel(chunks, reply)
    assert "提示词正文" in reply
    assert "目标模型" not in reply
    assert [item["question"] for item in done.data["pending_items"]] == [
        "目标模型定哪个？"
    ]


def test_prompt_templates_carry_the_pending_spec() -> None:
    """三条生成线的提示词都必须给出 `{pending_spec}` 占位符，否则模型不会产出块。

    技能包资源优先于内置兜底；两条路都要带上，缺一条就是「某条线永远拿不到清单」。
    """
    from backend.services import prompts as prompts_module
    from backend.services.skill_registry import load_skill_artifacts

    assert "{pending_spec}" in prompts_module.PROMPTS_GENERATION_PROMPT
    assert "{pending_spec}" in prompts_module.API_DOCS_GENERATION_PROMPT
    assert "{pending_spec}" in prompts_module.PRD_SECTION_GENERATION_PROMPT

    guide = (load_skill_artifacts("prompts") or {}).get("prompts_guide", "")
    assert "{pending_spec}" in guide


def test_line_without_block_yields_empty_list() -> None:
    """模型没附块（或块坏了）：正文原样保留、清单为空——生成不因此失败。"""
    service = DocumentService(llm=ScriptedLLM(["# 接口文档\n\n接口正文"]))
    events = asyncio.run(_drain(service.generate_api_docs_stream(prd_content="# PRD")))

    done = next(event for event in events if event.type == "done")
    assert done.data["reply"].strip() == "# 接口文档\n\n接口正文"
    assert done.data["pending_items"] == []

# ---------------------------------------------------------------------------
# format_pending_items：把**已有**清单渲染进提示词
#
# 与 `render_spec` 是一对：那个教模型「怎么产出」清单，这个把「已有的」念给它听。
# 只有「AI 自行澄清」用得上——它要照单逐条拍板，少一条就漏一条。
# ---------------------------------------------------------------------------


def test_format_pending_items_numbers_and_tags_every_item() -> None:
    """序号、等级、影响章节、原定拍板人都要出现：这些是模型判断该不该拍板的依据。"""
    text = format_pending_items(
        [
            {
                "id": "p-1",
                "question": "分页上限取多少？",
                "sections": ["4.3"],
                "decider": "产品负责人",
                "importance": "P0",
            },
            {
                "id": "p-2",
                "question": "要不要支持导出？",
                "sections": [],
                "decider": "",
                "importance": "P2",
            },
        ]
    )

    lines = text.split("\n")
    assert lines[0] == "1. [P0] 分页上限取多少？（影响章节：4.3）（原定拍板人：产品负责人）"
    # 空 `sections` / 空 `decider` 不留半截空括号
    assert lines[1] == "2. [P2] 要不要支持导出？"


def test_format_pending_items_says_so_when_empty() -> None:
    """空清单也要给一句话：直接留白会让提示词里出现一个没有下文的章节标题。"""
    assert format_pending_items([]) == "（本次没有待确认问题）"


def test_format_pending_items_tolerates_missing_fields() -> None:
    """模型/旧数据里缺字段是常态，渲染不能抛：缺什么就退化成可读占位。"""
    text = format_pending_items([{"id": "p-1"}])

    assert text == "1. [未标注] （未描述）"


def test_format_pending_items_joins_multiple_sections() -> None:
    """多个影响章节用「、」连起来，不是一个列表字面量。"""
    text = format_pending_items(
        [{"question": "容量按多少规划？", "sections": ["4.3", "5.1"], "importance": "P1"}]
    )

    assert "（影响章节：4.3、5.1）" in text
