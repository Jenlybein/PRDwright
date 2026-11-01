"""PRD 三车道并行：顺序、失败降级、并发上限。

这些用例与 `test_prd_section_stream.py` 分工：
- 那边守**串行**链路的契约（并发度显式设为 1）；
- 这边守**并行**链路：`part` 可乱序但按 index 装配、单章失败不拖垮全篇、
  所有模型调用受同一个信号量约束。
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

import pytest
from langchain_core.messages import AIMessageChunk, BaseMessage

from backend.core.config import get_settings
from backend.core.errors import AppError, ErrorCode
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
        revise_headings: set[str] | None = None,
        revise_severity: str = "high",
        review_errors: dict[str, BaseException] | None = None,
        write_errors: dict[str, BaseException] | None = None,
        empty_headings: set[str] | None = None,
        rewrite_errors: dict[str, BaseException] | None = None,
        blank_rewrites: set[str] | None = None,
        trailing: dict[str, str] | None = None,
    ) -> None:
        self._fail = set(fail_headings or set())
        self._review_delays = review_delays or {}
        # 这些章节的审核返回 `revise`（带 `where`），从而把改写路径走起来
        self._revise = set(revise_headings or set())
        # 触发改写时报告的严重度：用来验证「只有 high 才改」
        self._severity = revise_severity
        # 各阶段的失败注入：审核抛错、撰写抛错、改写抛错
        self._review_errors = dict(review_errors or {})
        self._write_errors = dict(write_errors or {})
        self._empty = set(empty_headings or set())
        self._rewrite_errors = dict(rewrite_errors or {})
        # 改写吐空白：用来覆盖「改写结果为空 → 保留改写前版本」
        self._blank_rewrites = set(blank_rewrites or set())
        # 撰写结尾追加的尾巴（用来验证抑制器在流结束时把「疑似哨兵前缀」吐回来）
        self._trailing = dict(trailing or {})
        self.calls: list[str] = []
        self.review_calls: list[str] = []
        self.rewrite_calls: list[str] = []
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
                self.review_calls.append(heading)
                delay = self._review_delays.get(heading, 0.0)
                if delay:
                    await asyncio.sleep(delay)
                error = self._review_errors.get(heading)
                if error is not None:
                    raise error
                if heading in self._revise:
                    yield AIMessageChunk(
                        content=json.dumps(
                            {
                                "verdict": "revise",
                                "issues": [
                                    {
                                        "where": heading,
                                        "severity": self._severity,
                                        "detail": "缺少关键内容",
                                    }
                                ],
                            },
                            ensure_ascii=False,
                        )
                    )
                else:
                    yield AIMessageChunk(content='{"verdict": "pass"}')
                return

            # 改写调用与撰写调用的人设相同，只能靠用户消息区分
            if "审核发现的问题" in user:
                self.rewrite_calls.append(heading)
                error = self._rewrite_errors.get(heading)
                if error is not None:
                    raise error
                await asyncio.sleep(0.01)
                if heading in self._blank_rewrites:
                    return
                yield AIMessageChunk(content=f"\n\n改后-{heading}\n\n")
                return

            self.calls.append(heading)
            error = self._write_errors.get(heading)
            if error is not None:
                raise error
            if heading in self._fail:
                raise ConnectionError("章节生成失败")
            # 让出控制权，制造真实的并发窗口
            await asyncio.sleep(0.01)
            if heading in self._empty:
                # 只有空白：章节层的 `strip()` 之后就是空正文
                yield AIMessageChunk(content="   ")
                return
            yield AIMessageChunk(content=f"\n\n正文-{heading}\n\n{self._trailing.get(heading, '')}")
        finally:
            self._in_flight -= 1


def _collect(writer: PrdSectionWriter, **request):
    request.setdefault("requirements", "做一个演示产品")
    request.setdefault("product_name", "演示产品")

    async def drain():
        return [event async for event in writer.stream(**request)]

    return asyncio.run(drain())


def test_parallel_parts_are_indexed_and_reply_is_assembled_in_order() -> None:
    llm = RoutingLLM()
    events = _collect(PrdSectionWriter(llm=llm, review_enabled=True, max_rewrites=1))

    parts = [event for event in events if event.type == "part"]
    assert len(parts) == len(PRD_SECTIONS) + 1
    # 并行链路逐字流，但每一片都必须带章节归属：多章同时吐字，丢了 section_id
    # 前端就只能整批丢弃（那正是「PRD 不流式、只能等整章蹦出来」的历史成因）
    chunks = [event for event in events if event.type == "chunk"]
    assert chunks
    for chunk in chunks:
        assert chunk.data.get("section_id")
        assert isinstance(chunk.data.get("index"), int)

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


def test_review_disabled_emits_no_review_event() -> None:
    """关掉逐章审核时**不发** `review` 结论事件，但正文照样齐备。

    摘要里那句「共 0 个待处理问题」在没审的情况下是假的——会被读成「审过了、没问题」。
    因此省掉审核车道就得连结论一起省掉。快捷流程用的就是这条路径。
    """
    events = _collect(PrdSectionWriter(llm=RoutingLLM(), review_enabled=False))

    assert not [event for event in events if event.type == "review"]
    done = next(event for event in events if event.type == "done")
    # 少的是审核，不是撰写：每章正文都在
    for section in PRD_SECTIONS:
        assert section.heading in done.data["reply"]
    assert done.data["partial"] is False


def test_parallel_rewrite_replaces_only_the_hit_section() -> None:
    """并行的改写路径：只重写被点名的那一章，其余章逐字保留。

    这条路径此前**零覆盖**——假模型的审核分支恒返回 `pass`，从不进改写，
    于是「并行链路的改写」从未被任何用例走通过。
    """
    target = PRD_SECTIONS[1]
    llm = RoutingLLM(revise_headings={target.heading})
    events = _collect(PrdSectionWriter(llm=llm, review_enabled=True, max_rewrites=1))

    assert "rewriting" in [event.data["stage"] for event in events if event.type == "stage"]
    # 只改写被点名的那一章，且只改一次
    assert llm.rewrite_calls == [target.heading]

    parts = {
        event.data["section_id"]: event.data["text"]
        for event in events
        if event.type == "part" and event.data.get("section_id")
    }
    assert f"改后-{target.heading}" in parts[target.id]

    # 未被点名的章照旧，没被顺手动过
    other = PRD_SECTIONS[2]
    assert f"正文-{other.heading}" in parts[other.id]
    assert "改后-" not in parts[other.id]


def test_parallel_medium_severity_does_not_rewrite() -> None:
    """并行链路同样遵守改写门槛：medium 只报不改。"""
    target = PRD_SECTIONS[1]
    llm = RoutingLLM(revise_headings={target.heading}, revise_severity="medium")
    events = _collect(PrdSectionWriter(llm=llm, review_enabled=True, max_rewrites=1))

    assert "rewriting" not in [event.data["stage"] for event in events if event.type == "stage"]
    assert llm.rewrite_calls == []


def test_chunks_are_tagged_so_parallel_sections_do_not_mix() -> None:
    """每片增量都带章节归属：并行时各章各长各的，绝不串到第一章上。

    这条守的是「PRD 也要流式」的核心前提。归属一旦丢了，前端只能整批丢弃增量
    （那正是历史行为），用户看到的就是「等一整章才蹦出来」。
    """
    events = _collect(PrdSectionWriter(llm=RoutingLLM(), review_enabled=False))

    grouped: dict[str, str] = {}
    for event in events:
        if event.type != "chunk":
            continue
        section_id = event.data["section_id"]
        grouped[section_id] = grouped.get(section_id, "") + event.content

    # 六章都在流，而不是只有第一章有字
    assert len(grouped) == len(PRD_SECTIONS)
    for section in PRD_SECTIONS:
        assert grouped[section.id] == f"\n\n正文-{section.heading}\n\n"


def test_rewrite_streams_under_the_same_section_tag() -> None:
    """改写稿同样逐字下发，且仍落在该章名下（前端靠 `rewriting` 清旧缓冲）。"""
    target = PRD_SECTIONS[1]
    events = _collect(
        PrdSectionWriter(
            llm=RoutingLLM(revise_headings={target.heading}),
            review_enabled=True,
            max_rewrites=1,
        )
    )

    rewriting = [
        event
        for event in events
        if event.type == "stage" and event.data.get("stage") == "rewriting"
    ]
    assert [event.data["section_id"] for event in rewriting] == [target.id]

    streamed = "".join(
        event.content
        for event in events
        if event.type == "chunk" and event.data.get("section_id") == target.id
    )
    # 初稿与改写稿都在这一章名下；「哪一份作数」由随后的 part 定，不由流决定
    assert f"正文-{target.heading}" in streamed
    assert f"改后-{target.heading}" in streamed


class FailOnceLLM(RoutingLLM):
    """指定章节第一次撰写失败、第二次成功——用来覆盖「重试」路径。"""

    def __init__(self, heading: str) -> None:
        super().__init__()
        self._once = heading
        self._failed = False

    async def astream(self, messages: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
        system = str(messages[0].content)
        user = str(messages[-1].content)
        if (
            REVIEW_MARKER not in system
            and self._heading(user) == self._once
            and not self._failed
        ):
            self._failed = True
            raise ConnectionError("第一次撰写失败")
        async for chunk in super().astream(messages):
            yield chunk


def test_retry_reannounces_writing_so_the_half_written_text_is_dropped(monkeypatch) -> None:
    """单章重试必须重报 `writing`：前端收到它才清掉上一轮已吐出的半截。

    少了这一次重报，两轮的字会在同一张卡片里叠着长（重试越多次叠得越厚），
    而且不报任何错——那种 bug 只能靠肉眼在页面上发现。

    ⚠️ 必须关掉传输层的自动重试（`LLM_MAX_RETRIES=0`）：`astream_text` 自己会
    把可重试的失败吞掉重来一次，章节层根本看不到失败，也就没有「重报」可言。
    """
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    get_settings.cache_clear()

    target = PRD_SECTIONS[2]
    events = _collect(PrdSectionWriter(llm=FailOnceLLM(target.heading), review_enabled=False))

    writing = [
        event
        for event in events
        if event.type == "stage"
        and event.data.get("stage") == "writing"
        and event.data.get("section_id") == target.id
    ]
    assert len(writing) == 2

    # 其余章节各只报一次：别把「重试重报」做成「每章都重报」
    others = [
        event
        for event in events
        if event.type == "stage"
        and event.data.get("stage") == "writing"
        and event.data.get("section_id") != target.id
    ]
    assert len(others) == len(PRD_SECTIONS) - 1


# ---------------------------------------------------------------------------
# 分支补全：并行链路里此前没有用例走到的失败 / 降级 / 边界分支
# ---------------------------------------------------------------------------
#
# 这些用例是重写前的**行为基线**：结构会变（376 行的方法要拆开），
# 「单章失败不终止全篇」「审核/改写失败各自怎么降级」不能变。


def test_parallel_rejects_empty_input_and_missing_skill_assets(monkeypatch) -> None:
    """并行链路的两种「没有可写的东西」：输入为空 → `ValueError`；资源缺失 → `RuntimeError`。"""
    with pytest.raises(ValueError):
        _collect(PrdSectionWriter(llm=RoutingLLM(), review_enabled=False), requirements="")

    from backend.services import prd_section_writer as module

    monkeypatch.setattr(module, "load_prd_skill_artifacts", lambda: None)
    with pytest.raises(RuntimeError):
        _collect(PrdSectionWriter(llm=RoutingLLM(), review_enabled=False))


def test_parallel_summary_input_takes_the_product_name_from_the_summary() -> None:
    """并行链路的**结构化摘要**入口（此前只有串行链路有摘要用例）。

    摘要里的产品名优先于调用方传入的 `product_name`——两条入口共用同一套口径。
    """
    summary = {
        "product_name": "问卷星Plus",
        "product_goal": "让产品团队自助收集需求问卷",
        "target_users": ["产品经理"],
        "platform": "Web",
        "mvp_features": [],
        "v2_features": [],
        "ui_pages": [],
    }
    events = _collect(
        PrdSectionWriter(llm=RoutingLLM(), review_enabled=False),
        requirements="",
        product_name="另一个名字",
        summary=summary,
    )

    header = next(event for event in events if event.type == "part")
    assert "问卷星Plus" in header.data["text"]
    assert "另一个名字" not in header.data["text"]
    done = next(event for event in events if event.type == "done")
    for section in PRD_SECTIONS:
        assert section.heading in done.data["reply"]


def test_review_retry_exhaustion_keeps_the_body() -> None:
    """审核连续失败用尽重试后**保留正文**，该章不判失败、整篇不中断。"""
    target = PRD_SECTIONS[1]
    llm = RoutingLLM(review_errors={target.heading: ValueError("审核一直失败")})
    events = _collect(PrdSectionWriter(llm=llm, review_enabled=True, max_rewrites=1))

    done = next(event for event in events if event.type == "done")
    assert done.data["partial"] is False
    target_part = next(
        event
        for event in events
        if event.type == "part" and event.data.get("section_id") == target.id
    )
    assert f"正文-{target.heading}" in target_part.data["text"]
    # 审核层重试 max_retries+1 次后放弃：1 + 1
    assert llm.review_calls.count(target.heading) == 2
    # 没开「审出问题」的章节仍照常汇总结论（这章按通过处理）
    review = next(event for event in events if event.type == "review")
    assert review.data["verdict"] == "pass"


def test_review_app_error_is_fatal() -> None:
    """审核抛出 `AppError`（预算超顶 / 墙钟硬顶）不重试，直接走致命路径。

    这条同时守着一个**曾经存在的竞态**：章节任务若在 `gather` 之前就失败并被摘出
    任务集，异常就无人回收（`Task exception was never retrieved`），整篇会在没有
    任何告警的情况下产出一份缺章的文档。
    """
    target = PRD_SECTIONS[1]
    llm = RoutingLLM(
        review_errors={target.heading: AppError(ErrorCode.INVALID_REQUEST, "预算超顶")}
    )
    events = _collect(PrdSectionWriter(llm=llm, review_enabled=True, max_rewrites=1))

    assert any(event.type == "error" for event in events)
    assert not any(event.type == "done" for event in events)


def test_write_app_error_is_fatal() -> None:
    """撰写阶段的 `AppError` 同样不重试、不写占位：重来多少次结果都一样。"""
    target = PRD_SECTIONS[1]
    llm = RoutingLLM(write_errors={target.heading: AppError(ErrorCode.INVALID_REQUEST, "预算超顶")})
    events = _collect(PrdSectionWriter(llm=llm, review_enabled=False))

    assert any(event.type == "error" for event in events)
    assert not any(event.type == "done" for event in events)
    assert not [
        event
        for event in events
        if event.type == "part" and event.data.get("section_id") == target.id
    ]


def test_empty_draft_fails_only_that_section() -> None:
    """模型吐了空白：该章落失败占位，其余章照常完成，`partial=true`。"""
    target = PRD_SECTIONS[3]
    llm = RoutingLLM(empty_headings={target.heading})
    events = _collect(PrdSectionWriter(llm=llm, review_enabled=False))

    done = next(event for event in events if event.type == "done")
    assert done.data["partial"] is True
    assert done.data["failed_sections"][0]["reason"] == "撰写结果为空"

    failed_part = next(
        event
        for event in events
        if event.type == "part" and event.data.get("section_id") == target.id
    )
    assert failed_part.data.get("failed") is True
    assert failed_part.data["text"].startswith(target.heading)
    assert "本章生成失败" in failed_part.data["text"]
    # 占位仍按 index 归位，且只有这一章是占位
    assert failed_part.data["index"] == PRD_SECTIONS.index(target)
    assert sum(1 for event in events if event.type == "part" and event.data.get("failed")) == 1


def test_rewrite_failure_and_blank_rewrite_keep_the_pre_rewrite_draft() -> None:
    """改写抛错或只吐空白时保留**改写前版本**，不判失败、不丢正文。"""
    raised, blank = PRD_SECTIONS[1], PRD_SECTIONS[2]
    llm = RoutingLLM(
        revise_headings={raised.heading, blank.heading},
        rewrite_errors={raised.heading: ValueError("改写挂了")},
        blank_rewrites={blank.heading},
    )
    events = _collect(PrdSectionWriter(llm=llm, review_enabled=True, max_rewrites=1))

    parts = {
        event.data["section_id"]: event.data
        for event in events
        if event.type == "part" and event.data.get("section_id")
    }
    # 抛错的那章：仍是初稿，且没被标成失败
    assert f"正文-{raised.heading}" in parts[raised.id]["text"]
    assert "改后-" not in parts[raised.id]["text"]
    assert parts[raised.id].get("failed") is not True
    # 吐空白的那章：同样保留初稿
    assert f"正文-{blank.heading}" in parts[blank.id]["text"]
    assert parts[blank.id].get("failed") is not True
    assert llm.rewrite_calls == [raised.heading, blank.heading]


def test_rewrite_app_error_is_fatal() -> None:
    """改写阶段的 `AppError` 与审核同口径：不吞、不保留旧稿，直接走致命路径。"""
    target = PRD_SECTIONS[1]
    llm = RoutingLLM(
        revise_headings={target.heading},
        rewrite_errors={target.heading: AppError(ErrorCode.INVALID_REQUEST, "预算超顶")},
    )
    events = _collect(PrdSectionWriter(llm=llm, review_enabled=True, max_rewrites=1))

    assert any(event.type == "error" for event in events)
    assert not any(event.type == "done" for event in events)


def test_trailing_looks_like_a_sentinel_is_flushed_not_dropped() -> None:
    """流结束时若尾部像「被切断的哨兵前缀」，抑制器要把暂存原样吐回来。

    这条守的是「一个字都不能吞」：抑制器为了不把半截哨兵漏给用户，会把末尾
    `carry_len` 个字符先扣住；流结束时它必须原样交还有效正文，否则正文尾部
    会被静默吃掉（而且只在「结尾恰好像哨兵」时才发生，极难复现）。
    """
    target = PRD_SECTIONS[0]
    tail = "<!-- PENDING:BEG"
    llm = RoutingLLM(trailing={target.heading: tail})
    events = _collect(PrdSectionWriter(llm=llm, review_enabled=False))

    streamed = "".join(
        event.content
        for event in events
        if event.type == "chunk" and event.data.get("section_id") == target.id
    )
    # 扣住的尾部最终仍然流了出去（没有静默截断）
    assert streamed == f"\n\n正文-{target.heading}\n\n{tail}"
    assert any(
        event.type == "chunk"
        and event.data.get("section_id") == target.id
        and "PENDING:BEG" in event.content
        for event in events
    )


def test_closing_the_stream_mid_flight_cancels_the_lane() -> None:
    """消费者提前断开（生成器被 `aclose()`）时，车道与派生任务必须被取消、不报错。

    这是「客户端滑走」的路径：不清理就会留下后台还在烧 token 的任务。
    """

    async def scenario():
        writer = PrdSectionWriter(llm=RoutingLLM(), review_enabled=False)
        stream = writer.stream(requirements="做一个演示产品")
        first = await stream.__anext__()
        # 继续取到「车道已经开跑、且已有章节在流」之后再看断开：此时 `finally` 里有活要收
        for _ in range(3):
            await stream.__anext__()
        await stream.aclose()
        return first

    first = asyncio.run(scenario())
    assert first.type == "status"

