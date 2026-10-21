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
import json

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


def _drain(
    llm,
    *,
    document: str = "# 文档",
    outcome: ReviewOutcome | None = None,
    review_llm=None,
    section_titles: str = "",
):
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
                review_llm=review_llm,
                section_titles=section_titles,
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


def test_api_docs_stream_is_one_pass_when_review_disabled() -> None:
    """快捷流程（`review_enabled=False`）：接口文档一遍过，不审也不改。

    顺带守住**不发 `review` 事件**：只跳调用、照样发一条「通过」的结论，会被读成
    「审过了、没问题」——那是假信号，比不报更糟（与 PRD 侧同口径）。
    """
    llm = TwoStageLLM(
        document_pieces=["# 接口文档\n", "\n初稿正文"],
        review_pieces=['{"verdict":"revise","issues":[{"severity":"high","detail":"缺鉴权"}]}'],
        rewrite_pieces=["不该被改写"],
    )
    service = DocumentService(llm=llm)

    events = _drain_events(
        service.generate_api_docs_stream(prd_content="# PRD", review_enabled=False)
    )

    # ① 只调了一次模型（撰写）；审核与改写一次都没发生
    assert len(llm.calls) == 1
    # ② 没有审核 / 改写的阶段与增量，也没有 review 结论
    assert not [event for event in events if event.type == "stage_chunk"]
    assert not [event for event in events if event.type == "review"]
    assert [event.data["stage"] for event in events if event.type == "stage"] == ["writing"]
    # ③ 正文与落稿都是初稿
    body = "".join(event.content for event in events if event.type == "chunk")
    assert "初稿正文" in body
    done = next(event for event in events if event.type == "done")
    assert "初稿正文" in str(done.data["reply"])
    assert "改写" not in str(done.data["reply"])


def test_prompts_stream_is_one_pass_when_review_disabled() -> None:
    """与接口文档同口径：提示词套件在快捷流程里也是一遍过。"""
    llm = TwoStageLLM(
        document_pieces=["# 提示词\n", "\n初稿正文"],
        review_pieces=['{"verdict":"revise","issues":[{"severity":"high","detail":"缺约束"}]}'],
        rewrite_pieces=["不该被改写"],
    )
    service = DocumentService(llm=llm)

    events = _drain_events(
        service.generate_prompts_stream(prd_content="# PRD", review_enabled=False)
    )

    assert len(llm.calls) == 1
    assert not [event for event in events if event.type == "review"]
    done = next(event for event in events if event.type == "done")
    assert "初稿正文" in str(done.data["reply"])


def test_whole_document_review_defaults_to_enabled() -> None:
    """不传 `review_enabled` 时默认**仍审**：漏传字段不能悄悄改变产出的质量口径。"""
    llm = TwoStageLLM(
        document_pieces=["# 接口文档\n", "\n初稿正文"],
        review_pieces=['{"verdict":"pass"}'],
    )
    service = DocumentService(llm=llm)

    events = _drain_events(service.generate_api_docs_stream(prd_content="# PRD"))

    assert len(llm.calls) == 2
    assert [event.type for event in events].count("review") == 1


def test_review_uses_the_smaller_window_instance() -> None:
    """审核走 `review_llm`（小输出窗口），改写仍走撰写用的那个实例。

    审核只吐一个 JSON 批注，用不着撰写级的上限；给它更小的窗口既省时间也顺带
    堵住「模型在审核输出里复述正文」这条浪费路径。
    """
    main = TwoStageLLM(
        review_pieces=[],  # 审核不归它管，一个字都不该从它这儿过
        document_pieces=["# 文档"],
        rewrite_pieces=["## 全文重写\n"],
    )
    auditor = TwoStageLLM(review_pieces=['{"verdict":"pass"}'])

    _drain(main, review_llm=auditor)

    assert len(auditor.calls) == 1
    assert len(main.calls) == 0


def test_section_rewrite_only_replaces_the_hit_section() -> None:
    """分节改写：只重写 `where` 命中的那一节，其余逐字保留，拼回的仍是整篇。"""
    document = "# 头\n\n## A\n\n旧A\n\n## B\n\n旧B\n"
    revise = json.dumps(
        {
            "verdict": "revise",
            "issues": [{"where": "## A", "severity": "high", "detail": "缺少鉴权说明"}],
        },
        ensure_ascii=False,
    )
    llm = TwoStageLLM(review_pieces=[revise], rewrite_pieces=["## A\n\n新A\n"])

    events, outcome = _drain(llm, document=document, section_titles="## A\n## B")

    assert outcome.rewritten is True
    assert "新A" in outcome.final_reply
    assert "旧A" not in outcome.final_reply
    # 未被点名的 B 节逐字保留——改写不该顺手动它
    assert "旧B" in outcome.final_reply
    # 没丢章、也没把标题重复一遍
    assert outcome.final_reply.count("## ") == 2
    # H1 与前言在第一个标题之前，拼回时不能丢
    assert outcome.final_reply.startswith("# 头")

    # 预览是整段覆盖的：`part.text` 必须是完整正文，不能只发被改写的那一节
    part = next(event for event in events if event.type == "part")
    assert part.data["replace"] is True
    assert part.data["text"] == outcome.final_reply


def test_section_rewrite_falls_back_to_whole_document_when_where_unmatched() -> None:
    """`where` 定位不到任何章节 → 退回整篇重写。

    宁可多花一次调用，也不要因为定位不上就干脆不改——界面上会显示「发现 N 个问题」
    却什么都没动，那比慢更难解释。
    """
    document = "## A\n\n旧A\n\n## B\n\n旧B\n"
    revise = json.dumps(
        {
            "verdict": "revise",
            "issues": [{"where": "## 不存在的章节", "severity": "high", "detail": "缺鉴权"}],
        },
        ensure_ascii=False,
    )
    llm = TwoStageLLM(review_pieces=[revise], rewrite_pieces=["## 全文重写\n"])

    _, outcome = _drain(llm, document=document)

    assert outcome.rewritten is True
    assert outcome.final_reply.strip() == "## 全文重写"


def test_section_rewrite_keeps_untouched_pending_items() -> None:
    """只重写 N 节时，未改写章节的待确认项不能被覆盖掉。

    改一节却让清单少一半问题，是最难解释的一类 bug。
    """
    document = "## A\n\n旧A\n\n## B\n\n旧B"
    revise = json.dumps(
        {
            "verdict": "revise",
            "issues": [{"where": "## A", "severity": "high", "detail": "缺鉴权"}],
        },
        ensure_ascii=False,
    )
    new_block = (
        "<!-- PENDING:BEGIN -->\n"
        '[{"question":"新问题","sections":["A"],"decider":"产品负责人","importance":"P0"}]\n'
        "<!-- PENDING:END -->"
    )
    llm = TwoStageLLM(review_pieces=[revise], rewrite_pieces=[f"## A\n\n新A\n\n{new_block}"])
    outcome = ReviewOutcome(
        final_reply=document,
        pending_items=[
            {
                "question": "旧问题",
                "sections": ["B"],
                "decider": "产品负责人",
                "importance": "P1",
                "id": "p-1",
            }
        ],
    )

    _, outcome = _drain(llm, document=document, outcome=outcome)

    # B 节没动，它的待确认项必须还在；按重要性 P0 在前重新排序
    assert [item["question"] for item in outcome.pending_items] == ["新问题", "旧问题"]
    # id 重新分配后连续不重号
    assert [item["id"] for item in outcome.pending_items] == ["p-1", "p-2"]


def test_medium_severity_does_not_trigger_rewrite() -> None:
    """只有未达门槛的 medium 问题时：结论照给，但不自动改写。

    medium 里的「结构不符」「空泛表述」几乎必然命中，按它改写会把
    「审一次 → 改一次」变成常态，时延翻倍而收益极小。
    """
    revise = json.dumps(
        {
            "verdict": "revise",
            "issues": [{"where": "## A", "severity": "medium", "detail": "结构不符"}],
        },
        ensure_ascii=False,
    )
    llm = TwoStageLLM(review_pieces=[revise], rewrite_pieces=["## 全文重写\n"])

    events, outcome = _drain(llm, document="## A\n\n旧A\n")

    assert outcome.rewritten is False
    assert [event.type for event in events if event.type == "part"] == []
    # 结论照样给，且问题一条不少——只是不自动改
    conclusion = next(event for event in events if event.type == "review")
    assert conclusion.data["verdict"] == "revise"
    assert len(conclusion.data["issues"]) == 1


def test_review_input_is_skeletonized_but_generation_sees_full_prd() -> None:
    """审核看骨架、撰写看全文：压缩只发生在**审核侧**。

    审核要核对的是「有没有编造 / 有没有漏」，需要的是事实清单而不是散文。而撰写必须
    拿全文——那是唯一的事实来源，压掉了就会照着骨架编内容。
    """
    verbose = "这是一段没有任何数字的叙述性文字，用来验证骨架化确实把它压掉了。" * 15
    prd = f"# 演示产品\n\n## 1. 功能需求\n\n{verbose}\n\n- F-01 创建订单\n"
    llm = TwoStageLLM(
        document_pieces=["# 接口文档\n", "\n初稿正文"],
        review_pieces=['{"verdict":"pass"}'],
    )
    service = DocumentService(llm=llm)

    _drain_events(service.generate_api_docs_stream(prd_content=prd))

    generate_msg = str(llm.calls[0][-1].content)
    review_msg = str(llm.calls[1][-1].content)

    # 撰写拿全文：需求里的长句一字不少
    assert verbose in generate_msg
    # 审核拿骨架：长句被压掉，但标题与编号锚点都还在
    assert verbose not in review_msg
    assert "## 1. 功能需求" in review_msg
    assert "F-01 创建订单" in review_msg
