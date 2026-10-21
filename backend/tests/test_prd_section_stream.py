"""PRD 分章节生成的事件契约测试。

背景：前端实时预览曾按 `chunk` 逐字拼接，而最终正文是后端先 `_ensure_heading()`
补标题、章间补空行，再拼成 `done.reply`——两者不是同一份字符串。于是预览里出现过
「某一章的标题消失」「标题与上一段粘成一行」，生成完成后又自行「愈合」。

修法是让后端做正文的唯一来源：每段定稿后发一个 `part` 事件。本文件把这条契约钉成
不变量——**全部 `part.text` 按序拼接即为最终正文**。
"""

import asyncio
import json
from typing import Any, AsyncIterator

import pytest
from conftest import register
from langchain_core.messages import AIMessageChunk, BaseMessage

from backend.api.schemas import StreamEvent
from backend.core.config import get_settings
from backend.services.prd_section_writer import PRD_SECTIONS, PrdSectionWriter

# 审核人设提示词里的特征词：用它区分「审核调用」与「撰写调用」
REVIEW_MARKER = "产品文档评审员"


@pytest.fixture(autouse=True)
def _serial_pipeline(monkeypatch):
    """本文件守的是**串行**链路的契约。

    并行链路有自己的一组用例（`test_prd_section_parallel.py`）。这里显式把并发度设为 1，
    否则默认值 3 会让这些「按调用顺序编排假 LLM」的用例变成不确定行为。
    """
    monkeypatch.setenv("PRD_PIPELINE_CONCURRENCY", "1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeLLM:
    """按预设文本作答的假 LLM（满足 `StreamingChatModel` 协议）。

    撰写与审核共用 `astream`，因此按系统提示词分流。文本统一切成两块吐出，
    顺带覆盖「增量拼接」这条路径。
    """

    def __init__(self, section_replies: list[str], review_replies: list[str] | None = None) -> None:
        self._sections = list(section_replies)
        self._reviews = list(review_replies or [])
        self.section_calls = 0
        self.review_calls = 0
        # 每次调用的**用户消息**：供「改写提示词里装了什么 / 没装什么」这类断言
        self.user_messages: list[str] = []

    def _pick(self, pool: list[str], count: int) -> str:
        """取第 `count` 条回复；预设用完后一直复用最后一条。"""
        if not pool:
            return ""
        return pool[min(count, len(pool) - 1)]

    async def astream(self, messages: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
        system = str(messages[0].content)
        self.user_messages.append(str(messages[-1].content))
        if REVIEW_MARKER in system:
            text = self._pick(self._reviews, self.review_calls)
            self.review_calls += 1
        else:
            text = self._pick(self._sections, self.section_calls)
            self.section_calls += 1

        middle = len(text) // 2
        for piece in (text[:middle], text[middle:]):
            if piece:
                yield AIMessageChunk(content=piece)


def _collect(writer: PrdSectionWriter) -> list[StreamEvent]:
    """同步跑完一次生成，收集全部事件。"""

    async def drain() -> list[StreamEvent]:
        return [
            event
            async for event in writer.stream(
                requirements="做一个演示产品", product_name="演示产品"
            )
        ]

    return asyncio.run(drain())


def _parts(events: list[StreamEvent]) -> list[StreamEvent]:
    """取出全部 `part` 事件。"""
    return [event for event in events if event.type == "part"]


def test_part_events_reassemble_into_the_final_reply() -> None:
    """不变量：`part.text` 按序拼接后即最终正文（预览与落库正文同源）。"""
    writer = PrdSectionWriter(llm=FakeLLM(["本章正文内容"]), review_enabled=False)
    events = _collect(writer)

    parts = _parts(events)
    # 文档头一次 + 每一章一次
    assert len(parts) == len(PRD_SECTIONS) + 1

    reply = next(event for event in events if event.type == "done").data["reply"]
    assert "".join(part.data["text"] for part in parts).strip() == reply


def test_part_carries_the_heading_the_model_forgot() -> None:
    """模型漏写标题行时 `part` 里必须已经补上——前端预览直接用它，看不到原始 chunk。"""
    writer = PrdSectionWriter(llm=FakeLLM(["正文没有标题行"]), review_enabled=False)
    events = _collect(writer)

    first = next(
        event for event in _parts(events) if event.data.get("section_id")
    )
    assert first.data["section_id"] == PRD_SECTIONS[0].id
    assert first.data["section_title"] == PRD_SECTIONS[0].title
    assert first.data["text"].startswith(PRD_SECTIONS[0].heading)
    # 章间分隔空行由后端补好，前端拿到即可直接追加、无需再做拼接判断
    assert first.data["text"].endswith("\n\n")


def test_part_is_emitted_only_after_rewriting_finishes() -> None:
    """判定为 revise 时必须先改写再定稿：`part` 带的是重写稿，初稿只以 chunk 流出去过。"""
    revise = json.dumps(
        {
            "verdict": "revise",
            "issues": [{"severity": "high", "detail": "出现了输入中没有的性能指标"}],
            "instruction": "删掉编造的指标",
        },
        ensure_ascii=False,
    )
    llm = FakeLLM(
        section_replies=["初稿内容", "重写后的内容"],
        review_replies=[revise, '{"verdict": "pass"}'],
    )
    writer = PrdSectionWriter(llm=llm, review_enabled=True, max_rewrites=1)
    events = _collect(writer)

    stages = [event.data["stage"] for event in events if event.type == "stage"]
    assert "rewriting" in stages

    # 每章撰写一次 + 首章额外改写一次
    assert llm.section_calls == len(PRD_SECTIONS) + 1
    # 审核每章**恰好一次**：改写后不再复审（与整篇链路「审一次 → 改一次，不复审」同口径）。
    # 从前首章会多审一次，那条多出来的调用价值很低——改完大概率过，却要多等一整轮。
    assert llm.review_calls == len(PRD_SECTIONS)

    first_section_part = next(
        event for event in _parts(events) if event.data.get("section_id")
    )
    assert "重写后的内容" in first_section_part.data["text"]
    assert "初稿内容" not in first_section_part.data["text"]

    # 初稿仍然以 chunk 形式流出过：它是前端的「实时缓冲」，改写时由前端自行丢弃
    chunk_text = "".join(event.content for event in events if event.type == "chunk")
    assert "初稿内容" in chunk_text


def test_rewriting_stage_carries_the_review_reason() -> None:
    """改写事件要带原因，否则前端只能显示「正在重写」，用户不知道为什么要重写。"""
    revise = json.dumps(
        {"verdict": "revise", "issues": [{"severity": "high", "detail": "编造了竞品数据"}]},
        ensure_ascii=False,
    )
    llm = FakeLLM(section_replies=["初稿", "改后稿"], review_replies=[revise, '{"verdict": "pass"}'])
    writer = PrdSectionWriter(llm=llm, review_enabled=True, max_rewrites=1)
    events = _collect(writer)

    rewriting = next(
        event
        for event in events
        if event.type == "stage" and event.data["stage"] == "rewriting"
    )
    assert rewriting.data["section_id"] == PRD_SECTIONS[0].id
    assert rewriting.data["note"] == "编造了竞品数据"


def _parse_sse(text: str) -> list[dict[str, Any]]:
    """从 SSE 文本里取出各事件的 JSON 体（忽略 `[DONE]` 结束标记）。"""
    events: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        for line in block.split("\n"):
            if not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if payload and payload != "[DONE]":
                events.append(json.loads(payload))
    return events


def test_sse_endpoint_emits_parts_that_reassemble_into_reply(client, monkeypatch) -> None:
    """端到端：真实路由 + 真实 SSE 编码下，`part` 按序拼接即 `done.reply`。

    只替换 LLM，鉴权、路由、SSE 分帧、schema 序列化全部走真实实现——单元测试守的是
    「内部怎么算」，这条守的是「前端实际收到什么」。
    """
    from backend.services import document_service

    monkeypatch.setattr(
        document_service,
        "get_service",
        lambda: document_service.DocumentService(llm=FakeLLM(["章节正文"])),
    )

    assert register(client).status_code == 200
    response = client.post(
        "/api/conversation/generate-prd-from-summary-stream",
        json={"requirements": "做一个演示产品"},
    )
    assert response.status_code == 200

    events = _parse_sse(response.text)
    parts = [event for event in events if event.get("type") == "part"]
    reply = next(event["data"]["reply"] for event in events if event.get("type") == "done")

    # 文档头一次 + 每一章一次
    assert len(parts) == len(PRD_SECTIONS) + 1
    # 契约：前端把 part 按序拼起来就得到最终正文，无需再做任何加工
    assert "".join(part["data"]["text"] for part in parts).strip() == reply

    # 文档头也是 part：否则前端会把它当成第一章的实时缓冲、在第一章开始时清掉
    assert parts[0]["data"]["text"].startswith("# ")
    assert "section_id" not in parts[0]["data"]
    assert parts[1]["data"]["section_id"] == PRD_SECTIONS[0].id


def test_medium_severity_does_not_trigger_rewrite() -> None:
    """只报 medium 时不自动改写：结论照给，但不动笔。

    medium 里的第 5 条口径是「空泛表述（出现『用户管理』『数据看板』这类描述）」，
    几乎必然命中。按它改写等于每章都要整章重吐一遍，时延翻倍而收益极小。
    """
    revise = json.dumps(
        {
            "verdict": "revise",
            "issues": [{"severity": "medium", "detail": "出现空泛表述"}],
        },
        ensure_ascii=False,
    )
    llm = FakeLLM(section_replies=["初稿内容"], review_replies=[revise])
    events = _collect(PrdSectionWriter(llm=llm, review_enabled=True, max_rewrites=1))

    stages = [event.data["stage"] for event in events if event.type == "stage"]
    assert "rewriting" not in stages
    # 每章撰写一次，没有额外的改写调用；审核照做（串行链路不发 `review` 事件，
    # 结论只在并行链路汇总，因此这里用调用次数证明「审过了」）
    assert llm.section_calls == len(PRD_SECTIONS)
    assert llm.review_calls == len(PRD_SECTIONS)
    # 正文是初稿：没有被动过
    first_part = next(event for event in _parts(events) if event.data.get("section_id"))
    assert "初稿内容" in first_part.data["text"]


def test_high_severity_rewrites_once_without_re_review() -> None:
    """high 触发一次改写，且**改完不再复审**。

    第二条 `{"verdict":"pass"}` 永远不会被消费——复审被去掉了。
    """
    revise = json.dumps(
        {
            "verdict": "revise",
            "issues": [{"severity": "high", "detail": "编造了性能指标"}],
        },
        ensure_ascii=False,
    )
    llm = FakeLLM(
        section_replies=["初稿内容", "改后稿"], review_replies=[revise, '{"verdict": "pass"}']
    )
    events = _collect(PrdSectionWriter(llm=llm, review_enabled=True, max_rewrites=1))

    stages = [event.data["stage"] for event in events if event.type == "stage"]
    assert "rewriting" in stages
    # 每章撰写一次 + 首章改写一次
    assert llm.section_calls == len(PRD_SECTIONS) + 1
    # 审核每章恰好一次：改写后不复审（与整篇链路同口径）
    assert llm.review_calls == len(PRD_SECTIONS)

    first_part = next(event for event in _parts(events) if event.data.get("section_id"))
    assert "改后稿" in first_part.data["text"]


def test_rewrite_prompt_is_slim_but_still_carries_pending_spec() -> None:
    """改写不再重发完整撰写提示词：没有字段契约、没有模板占位符。

    从前改写是把 `PRD_SECTION_GENERATION_PROMPT` 整份重发（含 6000 字符的字段契约、
    带 `{{字段名}}` 占位符的模板骨架、对话记录）再追加一段附录——改写一次的输入
    比初次撰写还大。而模板里的占位符会诱导模型重新渲染一遍骨架，反而破坏原文。
    """
    revise = json.dumps(
        {
            "verdict": "revise",
            "issues": [{"severity": "high", "detail": "编造了性能指标"}],
        },
        ensure_ascii=False,
    )
    llm = FakeLLM(section_replies=["初稿内容", "改后稿"], review_replies=[revise])
    _collect(PrdSectionWriter(llm=llm, review_enabled=True, max_rewrites=1))

    rewrite_msg = next(
        message for message in llm.user_messages if "审核发现的问题" in message
    )
    # 撰写提示词专属的两节不该出现（用完整的小节标题匹配：光匹配「输入字段契约」
    # 会误伤 `render_spec()` 里提到这个词的那句说明）
    assert "## 三、输入字段契约" not in rewrite_msg
    assert "## 二、补充对话记录" not in rewrite_msg
    # 带 `{{字段名}}` 占位符的模板骨架也不该出现——它会诱导模型重新渲染一遍骨架，
    # 反而破坏原文（用这一节的标题匹配：直接搜 `{{` 会误伤 `render_spec()` 的 JSON 示例）
    assert "全文模板" not in rewrite_msg
    # 该有的三样：要改的章节、待确认清单规范、写作规范
    assert PRD_SECTIONS[0].heading in rewrite_msg
    assert "PENDING:BEGIN" in rewrite_msg
    assert "写作规范" in rewrite_msg


def test_prior_sections_are_skeletonized() -> None:
    """第 k 章不再带前 k−1 章全文：总输入从 O(N²) 降到 O(N)。

    骨架保留标题行与含数字的行，因此跨章一致性真正要核对的东西（产品名、功能编号）
    不会丢，但纯叙述性的长句会被压掉。
    """
    verbose = "这是一段没有任何数字的叙述性文字，用来验证骨架化确实把它压掉了。" * 20
    llm = FakeLLM(section_replies=[verbose])
    _collect(PrdSectionWriter(llm=llm, review_enabled=False))

    # 第 0 条是撰写第 1 章（那时还没有上游章节），第 1 条起才带 prior_sections
    second_chapter_msg = llm.user_messages[1]
    assert verbose not in second_chapter_msg
    # 但章节标题必须还在，否则编号与口径就无从核对
    assert PRD_SECTIONS[0].heading in second_chapter_msg
