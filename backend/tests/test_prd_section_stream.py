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

from conftest import register
from langchain_core.messages import AIMessageChunk, BaseMessage

from backend.api.schemas import StreamEvent
from backend.services.prd_section_writer import PRD_SECTIONS, PrdSectionWriter

# 审核人设提示词里的特征词：用它区分「审核调用」与「撰写调用」
REVIEW_MARKER = "产品文档评审员"


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

    def _pick(self, pool: list[str], count: int) -> str:
        """取第 `count` 条回复；预设用完后一直复用最后一条。"""
        if not pool:
            return ""
        return pool[min(count, len(pool) - 1)]

    async def astream(self, messages: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
        system = str(messages[0].content)
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
    # 审核则每章至少一次，首章因改写后再审多一次
    assert llm.review_calls == len(PRD_SECTIONS) + 1

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
