"""整篇审核 / 一次改写：解析口径、失败降级与**流式输出**。

守住四条：
1. 合法 JSON 能识别出 `revise` 与逐条问题；
2. 非法 JSON / 调用失败一律按「通过」处理，不能把任务判失败；
3. 改写提示词必须同时带上「问题清单」与「上一版全文」；
4. 审核与改写的模型输出必须**实时流出去**（`stage_chunk`），否则用户在整篇量级的
   调用里只能干等，看不出任务到底卡没卡死。
"""

from __future__ import annotations

import asyncio

from langchain_core.messages import BaseMessage
from langchain_core.messages.ai import AIMessageChunk

from backend.services.document_review import (
    DocumentReviewResult,
    ReviewOutcome,
    review_whole_document,
    rewrite_whole_document_messages,
    stream_review_and_rewrite,
)
from backend.services.document_service import DocumentService
from fakes import ScriptedLLM


def _run(coro):
    return asyncio.run(coro)


class TwoStageLLM:
    """按**系统提示词**分流的假模型：审核人设吐 JSON，改写人设吐正文。

    不按「第 N 次调用」编排：这两个角色本来就是两套人设，用提示词分流既贴合事实，
    也不会因为将来审核多调一次就整组用例错位。

    给 `document_pieces` 时再多认一种人设（既不是评审员也不是改写者）——那条分支
    用来跑完整的「生成 → 审核 → 改写」链路。
    """

    def __init__(
        self,
        *,
        review_pieces: list[str],
        rewrite_pieces: list[str] | None = None,
        document_pieces: list[str] | None = None,
    ) -> None:
        self._review = list(review_pieces)
        self._rewrite = list(rewrite_pieces or [])
        self._document = list(document_pieces or [])
        # 收到的用户消息（供「改写提示词里带没带问题清单」这类断言）
        self.calls: list[list[BaseMessage]] = []

    async def astream(self, messages: list[BaseMessage]):  # noqa: ANN201 - 协议方法
        self.calls.append(list(messages))
        system = str(messages[0].content)
        if "技术文档评审员" in system:
            pieces = self._review
        elif "技术文档撰写者" in system:
            pieces = self._rewrite
        else:
            pieces = self._document
        for piece in pieces:
            await asyncio.sleep(0)
            yield AIMessageChunk(content=piece)


def _drain(llm, *, document: str = "# 文档", outcome: ReviewOutcome | None = None):
    """跑完一次「审核 + 改写」，返回（事件列表, 回填后的 outcome）。"""

    async def go():
        holder = outcome or ReviewOutcome(final_reply=document)
        events = [
            event
            async for event in stream_review_and_rewrite(
                llm,
                document=document,
                writing_rules="规范",
                requirements="PRD",
                doc_type="api_doc",
                kind="api_docs",
                label="接口文档",
                outcome=holder,
            )
        ]
        return events, holder

    return _run(go())


def _drain_events(events):  # noqa: ANN001, ANN201 - 测试内小工具
    """把任意异步事件流收成列表（生成器不能直接丢给 `asyncio.run`）。"""

    async def go():
        return [event async for event in events]

    return _run(go())


def test_review_parses_revise_json() -> None:
    llm = ScriptedLLM(
        ['{"verdict":"revise","issues":[{"severity":"high","detail":"编造了接口"}],"instruction":"删掉"}']
    )

    result = _run(
        review_whole_document(
            llm, document="# 文档", writing_rules="规范", requirements="PRD"
        )
    )

    assert result is not None
    assert result.needs_revision is True
    assert "编造了接口" in result.issues_text()


def test_review_invalid_json_falls_back_to_pass() -> None:
    llm = ScriptedLLM(["这不是 JSON"])

    result = _run(
        review_whole_document(llm, document="# 文档", writing_rules="", requirements="")
    )

    assert result is not None
    assert result.verdict == "pass"
    assert result.needs_revision is False


def test_review_failure_returns_none() -> None:
    llm = ScriptedLLM([], raise_at=0)

    assert (
        _run(
            review_whole_document(
                llm, document="# 文档", writing_rules="", requirements=""
            )
        )
        is None
    )


def test_empty_document_is_not_reviewed() -> None:
    llm = ScriptedLLM(["不该被调用"])

    assert (
        _run(
            review_whole_document(
                llm, document="   ", writing_rules="", requirements=""
            )
        )
        is None
    )


def test_rewrite_messages_include_issues_and_document() -> None:
    review = DocumentReviewResult(
        verdict="revise",
        issues=[{"severity": "high", "detail": "缺接口"}],
        instruction="补上",
    )

    messages = rewrite_whole_document_messages(
        document="# 原文档", review=review, writing_rules="规范"
    )
    text = "\n".join(str(message.content) for message in messages)

    assert "缺接口" in text
    assert "# 原文档" in text
    assert "补上" in text


def test_review_deltas_are_streamed_before_the_conclusion() -> None:
    """审核的原始输出要**逐片**下发，而不是攒完 JSON 才给结论。"""
    llm = TwoStageLLM(review_pieces=['{"verdict":', '"pass"}'])

    events, outcome = _drain(llm)
    types = [event.type for event in events]

    # 第一条就是阶段事件：进度条要在审核开始的那一刻亮起，不能等模型返回
    assert types[0] == "stage"
    assert events[0].data["stage"] == "reviewing"
    assert types[-1] == "review"

    chunks = [event for event in events if event.type == "stage_chunk"]
    assert len(chunks) == 2
    assert all(event.data["stage"] == "reviewing" for event in chunks)
    assert "".join(event.content for event in chunks) == '{"verdict":"pass"}'

    # 审核通过：不改写、不覆盖正文、清单仍是调用方给的那份
    assert not [event for event in events if event.type == "part"]
    assert outcome.rewritten is False
    assert outcome.final_reply == "# 文档"


def test_rewrite_is_streamed_and_replaces_body_with_clean_text() -> None:
    """审核要求改写时：改写稿同样流式下发，定稿整段覆盖，且标记块不外泄。"""
    block = (
        "<!-- PENDING:BEGIN -->\n"
        '[{"question": "确认登录方式", "sections": ["2.1"], "decider": "产品负责人", '
        '"importance": "P1"}]\n'
        "<!-- PENDING:END -->"
    )
    llm = TwoStageLLM(
        review_pieces=['{"verdict":"revise","issues":[{"severity":"high","detail":"编造了接口"}]}'],
        rewrite_pieces=["# 改写稿\n", "正文\n", block],
    )

    events, outcome = _drain(llm)
    stages = [event.data["stage"] for event in events if event.type == "stage"]
    assert stages == ["reviewing", "rewriting"]

    rewrite_chunks = [
        event for event in events if event.type == "stage_chunk" and event.data["stage"] == "rewriting"
    ]
    assert rewrite_chunks, "改写稿必须流式下发，否则用户只能盯着不动的进度条"
    streamed = "".join(event.content for event in rewrite_chunks)
    # 待确认标记块在实时增量里就被抑制器拦住，别把哨兵漏到界面上
    assert "PENDING" not in streamed
    assert "改写稿" in streamed

    parts = [event for event in events if event.type == "part"]
    assert len(parts) == 1
    assert parts[0].data["replace"] is True
    # 初稿已经用 chunk 流出去过，改写稿必须整段覆盖，否则预览里会叠字
    assert parts[0].data["text"] == outcome.final_reply
    assert "PENDING" not in outcome.final_reply
    assert "# 改写稿" in outcome.final_reply

    assert outcome.rewritten is True
    assert outcome.pending_items[0]["question"] == "确认登录方式"

    review = events[-1]
    assert review.type == "review"
    assert review.data["rewritten"] is True


def test_review_failure_is_streamed_as_pass() -> None:
    """审核调用挂掉：按「通过」给结论，绝不因此丢掉已生成好的正文。"""
    events, outcome = _drain(ScriptedLLM([], raise_at=0))

    assert events[-1].type == "review"
    assert events[-1].data["verdict"] == "pass"
    assert not [event for event in events if event.type == "part"]
    assert outcome.final_reply == "# 文档"


def test_empty_document_only_yields_conclusion() -> None:
    """正文为空时没有可审的东西：只给一条结论，别空跑一次模型调用。"""
    llm = TwoStageLLM(review_pieces=['{"verdict":"pass"}'])

    events, _ = _drain(llm, document="   ")

    assert [event.type for event in events] == ["review"]
    assert llm.calls == []


def test_api_docs_stream_announces_writing_first_and_streams_review() -> None:
    """接口文档整条链路：阶段条立刻有位、审核/改写的字进阶段面板、正文不被污染。

    这是用户反馈的那条：「进度条一开始不显示、审核等很久又不知道在干什么」。
    守两层——首事件必须是 `stage(writing)`；审核与改写的输出只走 `stage_chunk`，
    绝不混进正文 `chunk`。
    """
    llm = TwoStageLLM(
        document_pieces=["# 接口文档\n", "\n初稿正文"],
        review_pieces=['{"verdict":"revise","issues":[{"severity":"high","detail":"缺鉴权"}]}'],
        rewrite_pieces=["# 接口文档\n", "\n改写后的正文"],
    )
    service = DocumentService(llm=llm)

    events = _drain_events(service.generate_api_docs_stream(prd_content="# PRD"))

    # ① 进度条：第一步是 writing，而且必须在整篇生成之前就发出来
    assert events[0].type == "stage"
    assert events[0].data["stage"] == "writing"
    assert events[0].data["doc_type"] == "api_doc"

    # ② 审核与改写都有增量，且各自带阶段标签
    review_deltas = [
        event for event in events if event.type == "stage_chunk" and event.data["stage"] == "reviewing"
    ]
    rewrite_deltas = [
        event for event in events if event.type == "stage_chunk" and event.data["stage"] == "rewriting"
    ]
    assert review_deltas and rewrite_deltas

    # ③ 正文通道干净：审核 JSON 没有混进正文 chunk
    body = "".join(event.content for event in events if event.type == "chunk")
    assert "verdict" not in body
    assert "初稿正文" in body

    # ④ 改写稿整段覆盖，落进 done
    done = next(event for event in events if event.type == "done")
    assert "改写后的正文" in str(done.data["reply"])
