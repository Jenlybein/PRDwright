"""PRD 三车道并行：顺序、失败降级、并发上限。

这些用例与 `test_prd_section_stream.py` 分工：
- 那边守**串行**链路的契约（并发度显式设为 1）；
- 这边守**并行**链路：`part` 可乱序但按 index 装配、单章失败不拖垮全篇、
  所有模型调用受同一个信号量约束。
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from langchain_core.messages import AIMessageChunk, BaseMessage

from backend.core.config import get_settings
from backend.services.prd_section_writer import PRD_SECTIONS, PrdSectionWriter

REVIEW_MARKER = "产品文档评审员"


@pytest.fixture(autouse=True)
def _parallel(monkeypatch):
    """并发 3、重试 1：既验证并行，又让失败用例跑得快。"""
    monkeypatch.setenv("PRD_PIPELINE_CONCURRENCY", "3")
    monkeypatch.setenv("PRD_SECTION_MAX_RETRIES", "1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class RoutingLLM:
    """按「用户消息里的章节标题」路由，并记录同时在飞的调用数。

    不能按「第 N 次调用」编排：并行下车道的完成顺序不确定，那种脚本会随机失败。
    """

    def __init__(
        self,
        *,
        fail_headings: set[str] | None = None,
        review_delays: dict[str, float] | None = None,
    ) -> None:
        self._fail = set(fail_headings or set())
        self._review_delays = review_delays or {}
        self.calls: list[str] = []
        self.max_in_flight = 0
        self._in_flight = 0

    def _heading(self, text: str) -> str:
        """取**最后一次**出现的章节标题。

        生成提示词里包含了整份模板（所有章节标题），只取「第一个命中」会永远得到第 1 章；
        本次要写的章节标题在提示词末尾（第六节），因此用 `rfind` 取最靠后的那个。
        """
        candidates = [(text.rfind(section.heading), section.heading) for section in PRD_SECTIONS]
        position, heading = max(candidates)
        return heading if position >= 0 else ""

    async def astream(self, messages: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            system = str(messages[0].content)
            user = str(messages[-1].content)
            heading = self._heading(user)

            if REVIEW_MARKER in system:
                delay = self._review_delays.get(heading, 0.0)
                if delay:
                    await asyncio.sleep(delay)
                yield AIMessageChunk(content='{"verdict": "pass"}')
                return

            self.calls.append(heading)
            if heading in self._fail:
                raise ConnectionError("章节生成失败")
            # 让出控制权，制造真实的并发窗口
            await asyncio.sleep(0.01)
            yield AIMessageChunk(content=f"\n\n正文-{heading}\n\n")
        finally:
            self._in_flight -= 1


def _collect(writer: PrdSectionWriter):
    async def drain():
        return [
            event
            async for event in writer.stream(
                requirements="做一个演示产品", product_name="演示产品"
            )
        ]

    return asyncio.run(drain())


def test_parallel_parts_are_indexed_and_reply_is_assembled_in_order() -> None:
    llm = RoutingLLM()
    events = _collect(PrdSectionWriter(llm=llm, review_enabled=True, max_rewrites=1))

    parts = [event for event in events if event.type == "part"]
    assert len(parts) == len(PRD_SECTIONS) + 1
    # 并行链路刻意不逐字流：多章 chunk 会串字，也让整章重试不安全
    assert not [event for event in events if event.type == "chunk"]

    reply = next(event for event in events if event.type == "done").data["reply"]
    ordered = sorted(parts, key=lambda part: part.data.get("index", -1))
    assert "".join(part.data["text"] for part in ordered).strip() == reply

    # 每段都带 index（文档头除外），前端据此归位
    for part in parts:
        if part.data.get("section_id"):
            assert isinstance(part.data.get("index"), int)


def test_parts_may_arrive_out_of_order() -> None:
    """第 1 章审核拖慢时，后面的章可以先定稿；`done.reply` 仍按章节顺序装配。"""
    slow = PRD_SECTIONS[0].heading
    llm = RoutingLLM(review_delays={slow: 0.5})
    events = _collect(PrdSectionWriter(llm=llm, review_enabled=True, max_rewrites=1))

    section_parts = [
        event
        for event in events
        if event.type == "part" and event.data.get("section_id")
    ]
    # 到达顺序：第 1 章最后（它被审核拖住了）
    assert section_parts[-1].data["section_id"] == PRD_SECTIONS[0].id

    reply = next(event for event in events if event.type == "done").data["reply"]
    ordered = sorted(
        [event for event in events if event.type == "part"],
        key=lambda part: part.data.get("index", -1),
    )
    assert "".join(part.data["text"] for part in ordered).strip() == reply


def test_single_failed_section_still_completes_with_placeholder() -> None:
    failed = PRD_SECTIONS[3]
    llm = RoutingLLM(fail_headings={failed.heading})
    events = _collect(PrdSectionWriter(llm=llm, review_enabled=True, max_rewrites=1))

    done = next(event for event in events if event.type == "done")
    assert done.data["partial"] is True
    assert done.data["failed_sections"][0]["section_id"] == failed.id

    failed_part = next(
        event
        for event in events
        if event.type == "part" and event.data.get("section_id") == failed.id
    )
    assert failed_part.data.get("failed") is True
    assert failed.heading in failed_part.data["text"]
    # 占位也算进最终正文，保证章节结构完整
    assert failed.heading in done.data["reply"]

    review = next(event for event in events if event.type == "review")
    assert review.data["kind"] == "prd"
    assert review.data["failed_sections"][0]["section_id"] == failed.id


def test_all_sections_failed_emits_error_without_done() -> None:
    llm = RoutingLLM(fail_headings={section.heading for section in PRD_SECTIONS})
    events = _collect(PrdSectionWriter(llm=llm, review_enabled=False))

    assert any(event.type == "error" for event in events)
    assert not any(event.type == "done" for event in events)


def test_concurrency_cap_is_respected() -> None:
    llm = RoutingLLM()
    _collect(PrdSectionWriter(llm=llm, review_enabled=True, max_rewrites=1))

    assert llm.max_in_flight <= 3
