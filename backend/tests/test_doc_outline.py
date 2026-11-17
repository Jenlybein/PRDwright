"""三份产物的大纲：只从上游抽取、抽不到标「待确认」、失败就降级直接写正文。

票 #91「大纲扩到三份」。这一组守三件事：

1. **大纲条目必须能追溯到上游材料**——每条都是从上游原文里摘出来的字符串。
   这不是靠提示词「请勿编造」，而是靠**结构上没有创作这一步**（纯字符串抽取，零 LLM
   调用 + 一次后校验）：上游里没有的章节，大纲里不出现；抽不到就标「待确认」。
2. **降级不卡链路**——上游材料抽不出结构时，大纲阶段直接让路，正文照常写，
   且**不发 `error`**（后端不报错、前端阶段条不报错）。
3. **大纲不进交付物**——它只走 `stage_chunk` 这条过程通道，绝不混进 `chunk` 正文与
   `done.reply`；交付齐备判据仍只看三份正文，与大纲无关。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from fakes import ScriptedLLM

from backend.services.doc_outline import (
    PENDING_MARK,
    build_outline,
    is_traceable,
    render_outline,
    verify_outline,
)
from backend.services.document_service import DocumentService

# 一份「像样」的 PRD：接口清单、错误码、数据模型、分页约定都在
PRD_WITH_INTERFACES = """# 账单管理产品需求文档（PRD）

## 1. 产品概述

- 产品名称：账单管理
- 目标用户：独立开发者
- 平台：网页应用

## 2. 功能需求

### 2.1 MVP 功能（第一版必须交付）

- F-01 用户登录：支持邮箱密码登录
- F-02 账单列表：按月份筛选并分页展示
- F-03 账单导出：导出 CSV

## 3. 页面设计

- 登录页
- 账单列表页

## 4. 技术规格

- 鉴权：JWT
- 存储：PostgreSQL

## 5. 非功能需求

- 列表首屏加载不超过 1 秒

## 6. 项目范围

- 在范围内：账单增删改查、导出
- 不在范围内：多租户
"""

# 一份信息不全的 PRD：**没有**任何错误码 / 数据模型口径
PRD_WITHOUT_ERROR_CODES = """# 极简产品需求文档

## 1. 产品概述

- 产品名称：极简记账
- 目标用户：个人用户

## 2. 功能需求

- F-01 记一笔：记录金额与分类
"""

# 结构化需求摘要（完整流程的输入形态）
SUMMARY = {
    "product_name": "演示产品",
    "product_goal": "让产品经理 10 分钟写出一份 PRD",
    "target_users": ["独立开发者", "3-10 人小团队的产品经理"],
    "platform": "网页应用",
    "mvp_features": [
        {"name": "结构化录入", "description": "按字段录入需求"},
        {"name": "流式生成 PRD", "description": "逐段展示生成过程"},
    ],
    "ui_pages": [{"name": "录入页"}, {"name": "审阅页"}],
    "scope": {"in_scope": ["结构化录入", "流式生成"], "out_of_scope": ["多用户协作"]},
}

PRD_SECTIONS = ("1. 产品概述", "2. 功能需求", "3. 页面设计", "4. 技术规格", "5. 非功能需求", "6. 项目范围")


def _drain(events: AsyncIterator) -> list:
    """同步跑完一条生成流，收集全部事件。"""

    async def go() -> list:
        return [event async for event in events]

    return asyncio.run(go())


def _stages(events: list) -> list[str]:
    return [str(event.data.get("stage", "")) for event in events if event.type == "stage"]


def _stage_chunks(events: list, stage: str) -> str:
    return "".join(
        event.content
        for event in events
        if event.type == "stage_chunk" and event.data.get("stage") == stage
    )


# ---------------------------------------------------------------------------
# 来源一：接口文档的大纲来自 PRD 的接口清单
# ---------------------------------------------------------------------------


def test_api_docs_outline_takes_structures_from_the_prd() -> None:
    """接口文档大纲的条目逐字摘自 PRD：功能编号、概述、字段都带出处。

    「能追溯到上游」是可验证的：每个锚点在 PRD 原文里 `is_traceable` 为真。
    """
    outline = build_outline("api_docs", upstream=PRD_WITH_INTERFACES)
    assert outline is not None, "上游材料足够时不该降级"
    assert outline.doc_kind == "api_docs"

    rendered = render_outline(outline)
    # 接口清单里的功能编号必须出现（这就是「PRD 的接口清单」）
    assert "F-01" in rendered and "F-02" in rendered
    # 概述类的上游事实也要带出来
    assert "产品名称：账单管理" in rendered

    for entry in outline.entries:
        for anchor in entry.anchors:
            assert is_traceable(anchor, PRD_WITH_INTERFACES), f"锚点不可追溯：{anchor}"


def test_outline_marks_unextractable_sections_as_pending() -> None:
    """抽不到的结构标「待确认」，且不编造上游没有的内容。

    给一份没有错误码 / 数据模型口径的 PRD：那几节只能是待确认，绝不许出现
    「错误码 40000 参数校验失败」这类上游根本没有的条目。
    """
    outline = build_outline("api_docs", upstream=PRD_WITHOUT_ERROR_CODES)
    assert outline is not None

    pending = [entry for entry in outline.entries if entry.is_pending]
    assert pending, "上游没给错误码，这一节必须落到待确认"
    assert any("错误码" in entry.heading for entry in pending)
    for entry in pending:
        assert entry.reason, "待确认项要写清为什么抽不出"

    rendered = render_outline(outline)
    assert PENDING_MARK in rendered
    # 编造的证据：上游没有的东西一个字都不许出现在大纲里
    assert "40000" not in rendered
    assert "数据模型：账单" not in rendered


def test_verify_outline_drops_fabricated_anchors() -> None:
    """后校验：上游里找不到的锚点一律作废，条目随之降级为待确认。

    这是「只抽取不发明」的第二道闸——即便有人绕过抽取直接塞了一条大纲条目，
    只要它的锚点在材料里查不到，就不会以「已定结构」的形态出现。
    """
    from backend.services.doc_outline import OutlineEntry

    upstream = "- F-01 用户登录：支持邮箱密码登录"
    entries = (
        OutlineEntry(heading="接口列表", anchors=("F-01 用户登录：支持邮箱密码登录",)),
        OutlineEntry(heading="错误码约定", anchors=("错误码 40000 参数校验失败",)),
    )
    verified = verify_outline(entries, upstream)

    assert verified[0].anchors == ("F-01 用户登录：支持邮箱密码登录",)
    assert not verified[0].is_pending
    assert verified[1].is_pending, "查不到出处的条目必须降级为待确认"
    assert verified[1].anchors == ()

    # 直接用同一份上游重建，编造的条目不会出现在大纲文本里
    outline = build_outline("api_docs", upstream=upstream)
    assert outline is not None
    text = render_outline(outline)
    assert "40000" not in text


def test_outline_is_none_when_upstream_has_nothing_to_extract() -> None:
    """上游抽不出东西就返回 `None`（交给调用方降级），不产出全空大纲。"""
    assert build_outline("api_docs", upstream="") is None
    assert build_outline("api_docs", upstream="  \n\t ") is None
    # 模板结构缺失同样是「抽不出」：没有骨架就没有大纲
    assert build_outline("prd", upstream="- 想做个小工具", structure=()) is None


# ---------------------------------------------------------------------------
# 来源二 / 三：提示词的大纲来自 PRD + 接口文档
# ---------------------------------------------------------------------------


def test_prompts_outline_reads_both_upstreams() -> None:
    """提示词大纲同时从 PRD 与接口文档抽：两份材料的条目都能看到。"""
    api_docs = """# 接口文档

## 1. 概述

- 接口风格：RESTful
- 基础路径：/api/v1

## 2. 接口列表

- POST /api/v1/bills：创建账单
- GET /api/v1/bills：账单列表
"""
    outline = build_outline("prompts", upstream=PRD_WITH_INTERFACES, secondary=api_docs)
    assert outline is not None

    rendered = render_outline(outline)
    assert "F-01 用户登录" in rendered, "PRD 的场景/功能要进大纲"
    assert "/api/v1/bills" in rendered, "接口文档的条目也要进大纲"

    for entry in outline.entries:
        for anchor in entry.anchors:
            assert is_traceable(anchor, PRD_WITH_INTERFACES) or is_traceable(
                anchor, api_docs
            ), f"锚点不可追溯：{anchor}"


def test_prompts_outline_without_api_docs_still_works() -> None:
    """「我有 PRD」入口没有接口文档：大纲照样出，只是少一份来源。"""
    outline = build_outline("prompts", upstream=PRD_WITH_INTERFACES, secondary="")
    assert outline is not None
    rendered = render_outline(outline)
    assert "F-01" in rendered
    assert "接口文档" in rendered, "来源说明要如实写「本次未提供接口文档」"


# ---------------------------------------------------------------------------
# 来源三：PRD 的大纲来自需求输入 + 技能包模板章节
# ---------------------------------------------------------------------------


def test_prd_outline_follows_the_template_sections() -> None:
    """PRD 大纲的骨架 = 即将写入的模板章节，内容从需求输入抽取（结构化摘要）。"""
    outline = build_outline(
        "prd",
        upstream=json.dumps(SUMMARY, ensure_ascii=False, indent=2),
        structure=PRD_SECTIONS,
    )
    assert outline is not None
    # 骨架顺序与模板逐字一致：大纲说的就是接下来要写的章节
    assert [entry.heading for entry in outline.entries] == list(PRD_SECTIONS)

    rendered = render_outline(outline)
    assert "结构化录入" in rendered
    assert "三个必填字段" not in rendered  # 上游没有的内容不许出现


def test_api_docs_outline_structure_matches_the_generation_prompt() -> None:
    """骨架不许另立一套：接口文档大纲的五节必须就是生成提示词里「输出要求」的五节。

    这条守卫防的是「提示词改了、大纲还在预报旧结构」——那种漂移不报错，
    只会让大纲与实际产物对不上，而大纲正是用来预告产物的。
    """
    from backend.services.doc_outline import API_DOCS_STRUCTURE
    from backend.services.prompts import API_DOCS_GENERATION_PROMPT

    for section in API_DOCS_STRUCTURE:
        assert section in API_DOCS_GENERATION_PROMPT, f"生成提示词里没有「{section}」这一节"


def test_prompts_outline_structure_matches_the_skill_guide() -> None:
    """提示词套件的骨架来自技能包的产出要求，同理不许另立一套。"""
    from backend.services.doc_outline import PROMPTS_STRUCTURE
    from backend.services.skill_registry import load_skill_artifacts

    guide = (load_skill_artifacts("prompts") or {}).get("prompts_guide", "")
    assert guide, "prompts-generator 技能包没加载到，这条守卫就白守了"
    for section in PROMPTS_STRUCTURE:
        assert section in guide, f"技能包产出要求里没有「{section}」这一节"


# ---------------------------------------------------------------------------
# 三份产物都接进阶段事件（AC1 / AC4）
# ---------------------------------------------------------------------------


def test_api_docs_stream_announces_outline_then_writing() -> None:
    """接口文档流：先 `stage(outlining)` + `stage_chunk`（大纲进了阶段输出面板），再撰写。"""
    llm = ScriptedLLM(["# 接口文档\n\n正文"])
    service = DocumentService(llm=llm)

    events = _drain(service.generate_api_docs_stream(prd_content=PRD_WITH_INTERFACES))

    stages = _stages(events)
    assert stages[:2] == ["outlining", "writing"], f"阶段顺序不对：{stages}"
    outline_text = _stage_chunks(events, "outlining")
    assert outline_text.strip(), "大纲内容必须经 stage_chunk 下发，阶段面板才看得到"
    assert "接口列表" in outline_text
    assert PENDING_MARK in outline_text

    # 大纲是过程产物：绝不混进正文 chunk（与审核意见同一条规矩）
    body = "".join(event.content for event in events if event.type == "chunk")
    assert "接口列表" not in body

    done = next(event for event in events if event.type == "done")
    assert done.data["reply"].strip() == "# 接口文档\n\n正文"


def test_prompts_stream_announces_outline_then_writing() -> None:
    """提示词流与接口文档同构：三份产物阶段顺序一致（大纲 → 撰写 → …）。"""
    llm = ScriptedLLM(["# 提示词套件\n\n正文"])
    service = DocumentService(llm=llm)

    events = _drain(
        service.generate_prompts_stream(
            prd_content=PRD_WITH_INTERFACES,
            api_docs_content="# 接口文档\n\n- GET /api/v1/bills：账单列表",
            review_enabled=False,
        )
    )

    stages = _stages(events)
    assert stages[:2] == ["outlining", "writing"]
    outline_text = _stage_chunks(events, "outlining")
    assert PENDING_MARK in outline_text or "提示词清单" in outline_text
    assert "提示词清单" in outline_text


def test_outline_stage_carries_doc_type() -> None:
    """阶段事件对外形状不变：仍是 `stage` / `stage_chunk`，只多一个取值 `outlining`。"""
    llm = ScriptedLLM(["# 文档"])
    service = DocumentService(llm=llm)
    events = _drain(service.generate_api_docs_stream(prd_content=PRD_WITH_INTERFACES))

    outline_stage = next(event for event in events if event.type == "stage")
    assert outline_stage.data["stage"] == "outlining"
    assert outline_stage.data["doc_type"] == "api_doc"
    assert set(outline_stage.data) == {"stage", "doc_type"}, "事件形状不变"


# ---------------------------------------------------------------------------
# 降级：抽不出结构时直接写正文，不报错（AC3）
# ---------------------------------------------------------------------------


def test_blank_upstream_degrades_to_plain_writing() -> None:
    """上游空白 → 抽不出大纲 → 直接撰写；**不发 `error`**，链路不卡死。"""
    llm = ScriptedLLM(["# 接口文档\n\n正文"])
    service = DocumentService(llm=llm)

    events = _drain(service.generate_api_docs_stream(prd_content="   "))

    errors = [event for event in events if event.type == "error"]
    assert errors == [], "大纲降级不该被当成任务失败"
    assert "writing" in _stages(events)
    assert _stage_chunks(events, "outlining") == "", "降级时没有大纲内容，也就没有阶段输出"
    done = next(event for event in events if event.type == "done")
    assert done.data["reply"].strip() == "# 接口文档\n\n正文"
    assert len(llm.calls) >= 1, "降级只是不出大纲，正文照样要写"


def test_degrade_is_reported_honestly_in_the_stage_note() -> None:
    """降级要在阶段事件里说清楚（不静默、更不假装有大纲），但仍然不是错误。"""
    llm = ScriptedLLM(["# 文档"])
    service = DocumentService(llm=llm)
    events = _drain(service.generate_api_docs_stream(prd_content=""))

    stage = next(event for event in events if event.type == "stage")
    assert stage.data["stage"] == "outlining"
    assert "直接" in stage.content or "降级" in stage.content


def test_prompts_stream_degrades_too() -> None:
    """提示词线同口径：抽不出大纲也照常写正文。"""
    llm = ScriptedLLM(["# 提示词套件"])
    service = DocumentService(llm=llm)
    events = _drain(service.generate_prompts_stream(prd_content=""))

    assert not [event for event in events if event.type == "error"]
    assert "writing" in _stages(events)
    done = next(event for event in events if event.type == "done")
    assert done.data["reply"].strip() == "# 提示词套件"


def test_prd_stream_shows_outline_before_the_first_section() -> None:
    """PRD 也是三份之一：大纲在第一章之前出现，骨架就是模板章节。"""
    from backend.services.prd_section_writer import PrdSectionWriter

    class _WriterLLM:
        """按照角色分流的假模型：撰写与审核共用，这里只回答问题、记录调用。"""

        def __init__(self) -> None:
            self.calls = 0

        async def astream(self, messages: list[Any]) -> AsyncIterator[Any]:  # noqa: ANN401
            from langchain_core.messages import AIMessageChunk

            self.calls += 1
            yield AIMessageChunk(content="## 章节正文\n\n内容")

    writer = PrdSectionWriter(llm=_WriterLLM(), review_enabled=False)
    events = _drain(
        writer.stream(
            summary=SUMMARY,
            requirements="",
            product_name="演示产品",
        )
    )

    stages = _stages(events)
    assert stages[0] == "outlining", f"PRD 的第一格必须是大纲：{stages}"
    outline_text = _stage_chunks(events, "outlining")
    assert "1. 产品概述" in outline_text
    assert "2. 功能需求" in outline_text
    # 预报的章节集合与真正会写出的章节集合一致
    written_headings = [
        str(event.data.get("section_heading", "")) for event in events if event.type == "part"
    ]
    assert any("产品概述" in heading for heading in written_headings)
    # 大纲里报的六章与真正写出的六章是同一批（不预报、也不漏报）
    assert len(written_headings) == len(PRD_SECTIONS) + 1  # 文档头 + 六章


# ---------------------------------------------------------------------------
# AC5：大纲不影响交付判据
# ---------------------------------------------------------------------------


def test_outline_never_leaks_into_the_deliverable_body() -> None:
    """大纲不改变交付物：`done.reply` 仍是正文，待确认清单也不由大纲生成。

    交付齐备判据只看三份正文有没有内容（前端 `requiredDocsReady`），而大纲只活在
    `stage_chunk` 这条过程通道上——它多一条、少一条，都不该让一份「只有大纲」的
    产物看起来像正文已完成。
    """
    llm = ScriptedLLM(["# 接口文档\n\n正文"])
    service = DocumentService(llm=llm)
    events = _drain(service.generate_api_docs_stream(prd_content=PRD_WITH_INTERFACES))

    done = next(event for event in events if event.type == "done")
    assert done.data["reply"] == "# 接口文档\n\n正文"
    assert done.data["pending_items"] == []
    outline_text = _stage_chunks(events, "outlining")
    assert outline_text.splitlines()[1] not in done.data["reply"]
