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

    def __init__(
        self,
        section_replies: list[str | BaseException],
        review_replies: list[str | BaseException] | None = None,
    ) -> None:
        self._sections = list(section_replies)
        self._reviews = list(review_replies or [])
        self.section_calls = 0
        self.review_calls = 0
        # 每次调用的**用户消息**：供「改写提示词里装了什么 / 没装什么」这类断言。
        # 按角色分开记，用例不必再靠「提示词里有没有某个章节标题」来筛消息。
        self.user_messages: list[str] = []
        self.write_user_messages: list[str] = []
        self.review_user_messages: list[str] = []

    def _pick(self, pool: list[str | BaseException], count: int) -> str | BaseException:
        """取第 `count` 条回复；预设用完后一直复用最后一条。"""
        if not pool:
            return ""
        return pool[min(count, len(pool) - 1)]

    async def astream(self, messages: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
        system = str(messages[0].content)
        self.user_messages.append(str(messages[-1].content))
        if REVIEW_MARKER in system:
            self.review_user_messages.append(str(messages[-1].content))
            text = self._pick(self._reviews, self.review_calls)
            self.review_calls += 1
        else:
            self.write_user_messages.append(str(messages[-1].content))
            text = self._pick(self._sections, self.section_calls)
            self.section_calls += 1
        # 预设成异常时在**吐第一个字之前**失败：这正是「调用失败」分支的形态
        if isinstance(text, BaseException):
            raise text

        middle = len(text) // 2
        for piece in (text[:middle], text[middle:]):
            if piece:
                yield AIMessageChunk(content=piece)


def _collect(writer: PrdSectionWriter, **request: Any) -> list[StreamEvent]:
    """同步跑完一次生成，收集全部事件。"""
    request.setdefault("requirements", "做一个演示产品")
    request.setdefault("product_name", "演示产品")

    async def drain() -> list[StreamEvent]:
        return [event async for event in writer.stream(**request)]

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
    # 审核每章**恰好一次**：改写后不再重审（与整篇链路「审一次 → 改一次，不重审」同口径）。
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


def test_serial_chunks_also_carry_the_section_tag() -> None:
    """串行链路也必须带章节归属：两条链路共用一套事件契约。

    「带上归属」不是为了并行才需要的——前端只有一份按章渲染的逻辑。
    串行链路若不带，切到并发 1 的部署（`PRD_PIPELINE_CONCURRENCY=1` 是回滚路径）
    就会立刻退化成「没有过程字」。两条链路必须同形。
    """
    events = _collect(
        PrdSectionWriter(llm=FakeLLM(section_replies=["正文内容"]), review_enabled=False)
    )

    chunks = [event for event in events if event.type == "chunk"]
    assert chunks
    for chunk in chunks:
        assert chunk.data.get("section_id")
        assert isinstance(chunk.data.get("index"), int)


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


def _create_solution_with_requirements(client, requirements: str) -> int:  # noqa: ANN001
    """建一份带自由文本需求的方案（旧通道的需求输入同样由服务端按方案取）。

    路由只收 `solution_id`（spec #51 / C-R3）：需求住在方案上，请求体里没有它。
    """
    created = client.post(
        "/api/solutions",
        json={
            "title": "旧通道用例",
            "mode": "structured",
            "stage": "review-prd",
            "state": {"quick_requirements": requirements},
        },
    )
    assert created.status_code == 201, created.text
    return int(created.json()["id"])


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
    solution_id = _create_solution_with_requirements(client, "做一个演示产品")
    response = client.post(
        "/api/conversation/generate-prd-from-summary-stream",
        json={"solution_id": solution_id},
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
    """high 触发一次改写，且**改完不再重审**。

    第二条 `{"verdict":"pass"}` 永远不会被消费——重审被去掉了。
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
    # 审核每章恰好一次：改写后不重审（与整篇链路同口径）
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


# 结构化摘要里带两个「只可能来自需求输入」的事实：产品名与技术约束。
# 下面两条用例靠它们断言审核与撰写**各自**拿到的是什么。
_SUMMARY: dict[str, Any] = {
    "product_name": "问卷星Plus",
    "product_goal": "让产品团队自助收集需求问卷",
    "target_users": ["产品经理", "运营"],
    "platform": "Web",
    "mvp_features": [],
    "v2_features": ["导出为 Excel"],
    "ui_pages": [{"name": "问卷列表", "modules": ["筛选"], "interactions": ["点击新建"]}],
    "technical_constraints": ["后端用 Python", "前端用 Vue", "部署在云服务器"],
}


class FactAwareReviewLLM:
    """审核**只看手里的事实**：两条需求事实都在就判通过，缺一条就判「有问题」。

    这样「审核有没有拿到完整需求」从**提示词原文断言**变成**结论断言**：提示词怎么措辞、
    章节标题怎么写都不影响这条用例，而「审核看到的材料被骨架化成了空壳」必然让它变红。

    撰写 / 审核按调用**奇偶**分流：串行链路里每章都是「先撰写、再审核」，因此偶数次是
    撰写、奇数次是审核。不读提示词里的任何原文。
    """

    def __init__(self) -> None:
        self.calls = 0
        self.review_calls = 0

    async def astream(self, messages: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
        self.calls += 1
        if self.calls % 2 == 1:
            yield AIMessageChunk(content="本章正文内容")
            return
        self.review_calls += 1
        user = str(messages[-1].content)
        has_facts = "问卷星Plus" in user and "后端用 Python" in user
        if has_facts:
            yield AIMessageChunk(content=json.dumps({"verdict": "pass", "issues": []}))
            return
        yield AIMessageChunk(
            content=json.dumps(
                {"verdict": "revise", "issues": [{"severity": "high", "detail": "输入里没有"}]},
                ensure_ascii=False,
            )
        )


def _collect_summary(writer: PrdSectionWriter) -> list[StreamEvent]:
    """用结构化摘要跑一次（`_collect` 走的是自由文本那条路，碰不到 JSON 代码块）。"""

    async def drain() -> list[StreamEvent]:
        return [event async for event in writer.stream(summary=_SUMMARY)]

    return asyncio.run(drain())


def test_review_sees_the_whole_requirements_not_a_skeleton() -> None:
    """审核的唯一事实来源必须是**完整**的需求输入，不能是骨架。

    骨架器（`doc_skeleton.skeletonize`）是为 Markdown 正文设计的：遇到代码块只留围栏
    首行、块体整块丢。而结构化摘要整篇就是一个 ```json 块，压完只剩 ```` ```json ````。
    那时审核对着空材料判定，凡正文里出现具体内容都符合「输入里没有的技术栈」这条
    high 定义——真实存在的事实被整片误判成编造。

    提示词那边已经写了「先取证再判定」，但取证的前提是**手里真有材料**。断言落在
    **结论**上：假模型只在两条事实都到手时判通过，因此 verdict=pass 就是外部证据。
    """

    async def drain() -> tuple[list[StreamEvent], FactAwareReviewLLM]:
        llm = FactAwareReviewLLM()
        writer = PrdSectionWriter(llm=llm, review_enabled=True)
        return [event async for event in writer.stream(summary=_SUMMARY)], llm

    events, llm = asyncio.run(drain())
    conclusions = [event for event in events if event.type == "review"]
    assert len(conclusions) == 1, "PRD 的结论是汇总的一条"
    assert llm.review_calls == len(PRD_SECTIONS), "每章都审了一次"
    assert conclusions[0].data["verdict"] == "pass", (
        "审核没拿到需求输入里的事实（产品名 / 技术约束），把有据的内容判成了编造"
    )

    # 对照：骨架化会把这些事实全部丢掉——写出来是为了让这条用例在退化时必然变红
    from backend.services.doc_skeleton import skeletonize
    from backend.services.prd_skill import format_summary_as_requirements

    skeleton = skeletonize(format_summary_as_requirements(_SUMMARY))
    assert "问卷星Plus" not in skeleton and "后端用 Python" not in skeleton


def test_generation_prompt_carries_compacted_field_contract() -> None:
    """撰写侧带的是**压过的字段清单**，不是 8.6KB 的整份 JSON Schema。

    压掉的必须是校验细节、不能是语义：字段名、必填性、枚举取值都要留着。
    """
    llm = FakeLLM(section_replies=["本章正文内容"])
    _collect_summary(PrdSectionWriter(llm=llm, review_enabled=False))

    write_messages = llm.write_user_messages
    assert write_messages
    for message in write_messages:
        assert "必填（缺失即按「缺失信息处理」标注待确认）" in message
        assert "product_name" in message
        assert "priority 取值 P0/P1/P2" in message
        # 整份 JSON Schema 的校验细节不该再出现
        assert '"$schema"' not in message
        assert "x_normalization_rules" not in message


# ---------------------------------------------------------------------------
# 分支补全：串行链路里此前没有用例走到的失败 / 降级 / 边界分支
# ---------------------------------------------------------------------------
#
# 这些用例是重写前的**行为基线**：结构会变，事件序列与降级口径不能变。


def test_serial_rejects_empty_input_and_missing_skill_assets(monkeypatch) -> None:
    """两处「根本没有可写的东西」：输入为空 → `ValueError`；技能资源缺失 → `RuntimeError`。

    两者都必须**明确失败**而不是产出一份空文档——空文档比报错更难被发现。
    """
    with pytest.raises(ValueError):
        _collect(PrdSectionWriter(llm=FakeLLM([]), review_enabled=False), requirements="")

    from backend.services import prd_section_writer as module

    monkeypatch.setattr(module, "load_prd_skill_artifacts", lambda: None)
    with pytest.raises(RuntimeError):
        _collect(PrdSectionWriter(llm=FakeLLM([]), review_enabled=False))


def test_serial_write_failure_emits_error_and_stops() -> None:
    """串行链路里单章撰写失败是**致命**的：发 `error` 即止，绝不发 `done`。

    这是与并行链路「单章失败不终止全篇」的分野，也是最容易被重写顺手抹掉的差异。
    """
    events = _collect(PrdSectionWriter(llm=FakeLLM([ValueError("撰写失败")]), review_enabled=False))

    assert any(event.type == "error" for event in events)
    assert not any(event.type == "done" for event in events)
    # 有问题的章没有 `part`：正文永远不会带着「半截草稿」定稿
    assert not [part for part in _parts(events) if part.data.get("section_id")]


def test_serial_rewrite_failure_emits_error_and_stops() -> None:
    """改写失败同样是致命的：此时该章还没有 `part`，不能把初稿当定稿发出去。"""
    revise = json.dumps(
        {"verdict": "revise", "issues": [{"severity": "high", "detail": "编造了指标"}]},
        ensure_ascii=False,
    )
    llm = FakeLLM(section_replies=["初稿内容", ValueError("改写失败")], review_replies=[revise])
    events = _collect(PrdSectionWriter(llm=llm, review_enabled=True, max_rewrites=1))

    assert "rewriting" in [event.data["stage"] for event in events if event.type == "stage"]
    assert any(event.type == "error" for event in events)
    assert not any(event.type == "done" for event in events)
    assert not [part for part in _parts(events) if part.data.get("section_id")]


def test_serial_review_failure_degrades_to_pass() -> None:
    """审核调用失败只降级不致命：正文照发、`done` 照发，不产生 `error`。

    审核是锦上添花的一步，它自己挂了不该把整篇生成拖垮。
    """
    llm = FakeLLM(section_replies=["本章正文"], review_replies=[ValueError("审核挂了")])
    events = _collect(PrdSectionWriter(llm=llm, review_enabled=True, max_rewrites=1))

    assert not any(event.type == "error" for event in events)
    done = next(event for event in events if event.type == "done")
    for section in PRD_SECTIONS:
        assert section.heading in done.data["reply"]
    # 每章都真的走了审核（失败也计数），改写一次都没发生
    assert llm.review_calls == len(PRD_SECTIONS)
    assert "rewriting" not in [event.data["stage"] for event in events if event.type == "stage"]


def test_serial_skips_part_when_the_model_returns_nothing() -> None:
    """模型一字未吐时**不发该章的 `part`**：空串补上标题就成了假完成。"""
    events = _collect(PrdSectionWriter(llm=FakeLLM([""]), review_enabled=False))

    parts = _parts(events)
    assert len(parts) == 1, "只有文档头这一个 part"
    done = next(event for event in events if event.type == "done")
    assert done.data["reply"] == parts[0].data["text"].strip()


def test_serial_part_keeps_the_heading_the_model_wrote_once() -> None:
    """模型自己写了标题时不再重复补一遍——`_ensure_heading` 必须幂等。"""
    heading = PRD_SECTIONS[0].heading
    events = _collect(
        PrdSectionWriter(llm=FakeLLM([f"{heading}\n\n正文内容"]), review_enabled=False)
    )

    first = next(part for part in _parts(events) if part.data.get("section_id"))
    assert first.data["text"].startswith(heading)
    assert first.data["text"].count(heading) == 1


def test_serial_pending_block_is_stripped_and_collected_per_section() -> None:
    """待确认块只进 `done.pending_items`，绝不进 `part`（正文与过程字不能混）。"""
    item = json.dumps(
        {
            "question": "MVP 功能清单未提供",
            "sections": ["2.1"],
            "decider": "产品负责人",
            "importance": "P0",
        },
        ensure_ascii=False,
    )
    block = f"<!-- PENDING:BEGIN -->\n[{item}]\n<!-- PENDING:END -->"
    events = _collect(
        PrdSectionWriter(
            llm=FakeLLM([f"{PRD_SECTIONS[0].heading}\n\n正文内容\n\n{block}"]),
            review_enabled=False,
        )
    )

    first = next(part for part in _parts(events) if part.data.get("section_id"))
    assert "PENDING" not in first.data["text"]
    assert first.data["text"] == f"{PRD_SECTIONS[0].heading}\n\n正文内容\n\n"
    done = next(event for event in events if event.type == "done")
    assert [item["id"] for item in done.data["pending_items"]] == [
        f"{section.id}-1" for section in PRD_SECTIONS
    ]


def test_revise_with_instruction_only_still_triggers_rewrite() -> None:
    """只给修改指令、不给逐条问题时也要动笔，且改写提示词里有占位说明。

    「没有 severity 就无从按门槛筛选」按原口径放行；`issues_text()` 空清单时
    给一句明确说明，而不是留白让模型以为漏读了问题。
    """
    review = json.dumps({"verdict": "revise", "instruction": "删掉编造的指标"}, ensure_ascii=False)
    rewritten = "改后稿：已删掉编造的指标与竞品数据，只保留输入里给出的事实。"
    llm = FakeLLM(section_replies=["初稿内容", rewritten], review_replies=[review])
    events = _collect(PrdSectionWriter(llm=llm, review_enabled=True, max_rewrites=1))

    assert "rewriting" in [event.data["stage"] for event in events if event.type == "stage"]
    rewrite_message = next(msg for msg in llm.user_messages if "审核发现的问题" in msg)
    assert "删掉编造的指标" in rewrite_message
    assert "（审核未给出具体问题）" in rewrite_message
    # 改写稿同样逐字流出去过（串行也是「过程字 + 权威段」两条轨）
    streamed = "".join(event.content for event in events if event.type == "chunk")
    assert "改后稿：已删掉编造的指标" in streamed
    first = next(part for part in _parts(events) if part.data.get("section_id"))
    assert "改后稿" in first.data["text"]


def test_unknown_verdict_is_inferred_from_issues() -> None:
    """判定词不合法时按「有没有问题」推断：一个词毁掉整段审核输出不值得。"""
    review = json.dumps(
        {
            "verdict": "later",
            "issues": [{"severity": "high", "detail": "缺少关键内容"}],
        },
        ensure_ascii=False,
    )
    llm = FakeLLM(section_replies=["初稿内容", "改后稿"], review_replies=[review])
    events = _collect(PrdSectionWriter(llm=llm, review_enabled=True, max_rewrites=1))

    assert "rewriting" in [event.data["stage"] for event in events if event.type == "stage"]


def test_is_available_is_false_without_assets_or_matching_headings(monkeypatch) -> None:
    """资源缺失、或模板里找不到全部一级标题时，`is_available()` 必须为假（由上层降级）。"""
    from backend.services import prd_section_writer as module

    monkeypatch.setattr(module, "load_prd_skill_artifacts", lambda: None)
    assert module.is_available() is False

    # 模板只剩一个一级标题：其余章节切不出来，分章节生成会产残章
    monkeypatch.setattr(
        module,
        "load_prd_skill_artifacts",
        lambda: {"prd_template": f"{PRD_SECTIONS[0].heading}\n\n正文"},
    )
    assert module.is_available() is False

