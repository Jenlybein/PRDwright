"""PRD 整篇生成的事件契约（票 #92：分章退役）。

PRD 曾经走「分六章串行/并行撰写 → 逐章审核 → 逐章改写」那条专门链路，事件形状
与接口文档 / 提示词套件**不一样**：`chunk` 带 `section_id` + `index`、`part` 按章
追加、`review` 是按章汇总。分章退役后三份产物共用同一条生成路径，本文件把新契约
钉成不变量：

- `chunk` **不带**章节归属（`section_id` / `index` 一律不存在），只表示「正文在长」；
- 阶段条只按**阶段**推进（outlining → writing → reviewing → rewriting），不看章节；
- 审核是**整篇一次**，改写是**整篇一次**（仅 high 严重度触发，改完不重审）；
- 重新审核 / 按结论修复同样走整篇（PRD 不再有逐章路径）。

`part` 只剩「整段覆盖」一种语义（`replace: true`），与另两份一致。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

import pytest
from conftest import register, users
from langchain_core.messages import AIMessageChunk, BaseMessage

from backend.api.schemas import StreamEvent
from backend.services import document_store, job_runtime
from backend.services.document_service import DocumentService
from backend.services.job_bus import job_bus

# 审核人设（`DOC_REVIEW_SYSTEM_PROMPT`）里的特征词：用它区分「审核调用」
REVIEW_MARKER = "技术文档评审员"
# 改写人设（`document_review._REWRITE_SYSTEM_PROMPT`）里的特征词
REWRITE_MARKER = "技术文档撰写者"

_PASS = json.dumps({"verdict": "pass", "issues": []}, ensure_ascii=False)
_REVISE_HIGH = json.dumps(
    {
        "verdict": "revise",
        "issues": [{"severity": "high", "detail": "出现了输入中没有的性能指标"}],
    },
    ensure_ascii=False,
)

# 结构化摘要：里面有两处「只可能来自需求输入」的事实，用来证明审核 / 修复拿到的
# 是**完整需求**而不是骨架。
_SUMMARY: dict[str, Any] = {
    "product_name": "问卷星Plus",
    "product_goal": "让产品团队自助收集需求问卷",
    "target_users": ["产品经理", "运营"],
    "platform": "Web",
    "mvp_features": [
        {"name": "问卷编辑", "module": "问卷", "description": "拖拽式编辑问卷"},
    ],
    "v2_features": ["导出为 Excel"],
    "ui_pages": [{"name": "问卷列表", "modules": ["筛选"], "interactions": ["点击新建"]}],
    "technical_constraints": ["后端用 Python", "前端用 Vue", "部署在云服务器"],
}

_INITIAL_DOC = "# 问卷星Plus 产品需求文档（PRD）\n\n## 1. 产品概述\n\n正文\n\n## 2. 功能需求\n\n正文\n"
_REWRITTEN_DOC = "# 问卷星Plus 产品需求文档（PRD）\n\n## 1. 产品概述\n\n修正后的正文\n"


class RoutingLLM:
    """按系统提示词分流的三段式假 LLM：撰写 / 审核 / 改写各一份预设回复。

    与分章时代的 `FakeLLM` 不同：这里不再有「按章数调用 N 次」的编排，三份产物
    整篇链路的总调用次数是 1（撰写）或 2（+审核）或 3（+改写），因此按**角色**
    分流即可，调用次数本身就是被断言的契约。
    """

    def __init__(
        self,
        *,
        write: str = "",
        review: str = _PASS,
        rewrite: str = "",
    ) -> None:
        self._write = write
        self._review = review
        self._rewrite = rewrite
        self.write_calls = 0
        self.review_calls = 0
        self.rewrite_calls = 0
        self.user_messages: list[str] = []
        self.review_user_messages: list[str] = []
        self.rewrite_user_messages: list[str] = []

    async def astream(self, messages: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
        system = str(messages[0].content)
        user = str(messages[-1].content)
        self.user_messages.append(user)
        if REVIEW_MARKER in system:
            self.review_calls += 1
            self.review_user_messages.append(user)
            text = self._review
        elif REWRITE_MARKER in system:
            self.rewrite_calls += 1
            self.rewrite_user_messages.append(user)
            text = self._rewrite
        else:
            self.write_calls += 1
            text = self._write
        # 切两块吐出：覆盖「增量拼接」这条路径
        middle = len(text) // 2
        for piece in (text[:middle], text[middle:]):
            if piece:
                yield AIMessageChunk(content=piece)


def _drain(coro) -> list[StreamEvent]:
    return asyncio.run(_aiter(coro))


async def _aiter(agen) -> list[StreamEvent]:
    return [event async for event in agen]


def _stages(events: list[StreamEvent]) -> list[str]:
    return [str(event.data.get("stage")) for event in events if event.type == "stage"]


def _assert_no_section_attribution(events: list[StreamEvent]) -> None:
    """整篇路径的事件里不许再出现章节归属（`section_id` / `index`）。"""
    for event in events:
        assert "section_id" not in event.data, f"{event.type} 事件仍带 section_id"
        assert "index" not in event.data, f"{event.type} 事件仍带 index"


@pytest.fixture(autouse=True)
def _clean_job_state():
    job_runtime.reset_for_tests()
    job_bus.reset_for_tests()
    yield
    job_runtime.reset_for_tests()
    job_bus.reset_for_tests()


def test_prd_generation_is_one_whole_document_write() -> None:
    """PRD 生成不再分章：一次撰写、chunk 无归属、done.reply 即整篇正文。"""
    llm = RoutingLLM(write=_INITIAL_DOC)
    service = DocumentService(llm=llm)
    events = _drain(
        service.generate_prd_from_summary_stream(summary=_SUMMARY, review_enabled=False)
    )

    stages = _stages(events)
    # 阶段顺序与另两份一致：大纲 → 撰写（无审核时到此为止）
    assert "outlining" in stages
    assert "writing" in stages
    assert stages.index("outlining") < stages.index("writing")
    # 撰写只调用了一次：不再按章逐次调用
    assert llm.write_calls == 1
    assert llm.review_calls == 0

    _assert_no_section_attribution(events)
    chunks = [event for event in events if event.type == "chunk"]
    assert chunks
    assert "".join(chunk.content for chunk in chunks).strip() == _INITIAL_DOC.strip()

    done = next(event for event in events if event.type == "done")
    assert done.data["reply"].strip() == _INITIAL_DOC.strip()
    # 快捷流程不审不改：连 `review` 都不发（发一条「通过」会被读成「审过了」）
    assert not [event for event in events if event.type == "review"]
    assert not [event for event in events if event.type == "part"]


def test_prd_outline_extracts_structure_from_requirements() -> None:
    """大纲阶段仍是 PRD 的：从需求输入抽取，且骨架就是模板章节。"""
    llm = RoutingLLM(write=_INITIAL_DOC)
    events = _drain(
        DocumentService(llm=llm).generate_prd_from_summary_stream(
            summary=_SUMMARY, review_enabled=False
        )
    )

    outline_text = "".join(
        event.content
        for event in events
        if event.type == "stage_chunk" and event.data.get("stage") == "outlining"
    )
    assert "PRD大纲" in outline_text
    # 模板章节就是大纲骨架（抽取而非发明）
    assert "产品概述" in outline_text


def test_prd_review_is_one_whole_document_call() -> None:
    """整篇审核一次：不存在「每章审一次」的 stage / 调用。"""
    llm = RoutingLLM(write=_INITIAL_DOC)
    events = _drain(
        DocumentService(llm=llm).generate_prd_from_summary_stream(summary=_SUMMARY)
    )

    assert llm.write_calls == 1
    assert llm.review_calls == 1, "整篇审核只该发一次调用（分章时代是六次）"
    stages = _stages(events)
    assert stages.index("writing") < stages.index("reviewing")
    assert "rewriting" not in stages

    _assert_no_section_attribution(events)
    review_events = [event for event in events if event.type == "review"]
    assert len(review_events) == 1
    assert review_events[0].data["kind"] == "prd"
    assert review_events[0].data["verdict"] == "pass"
    done = next(event for event in events if event.type == "done")
    assert done.data["reply"].strip() == _INITIAL_DOC.strip()


def test_high_severity_rewrites_the_whole_document_once_without_re_review() -> None:
    """high 触发**整篇**改写一次，改完不再重审（与另两份同口径）。"""
    rewrite_doc = _REWRITTEN_DOC
    llm = RoutingLLM(write=_INITIAL_DOC, review=_REVISE_HIGH, rewrite=rewrite_doc)
    events = _drain(
        DocumentService(llm=llm).generate_prd_from_summary_stream(summary=_SUMMARY)
    )

    assert llm.review_calls == 1, "改写后不再重审"
    assert llm.rewrite_calls == 1
    stages = _stages(events)
    assert "rewriting" in stages
    rewriting = next(
        event for event in events if event.type == "stage" and event.data.get("stage") == "rewriting"
    )
    # P0 既有信息不回退：改写原因仍要带给前端
    assert rewriting.data.get("note")

    _assert_no_section_attribution(events)
    parts = [event for event in events if event.type == "part"]
    assert len(parts) == 1, "整篇改写只覆盖一次，不再按章追加"
    assert parts[0].data["replace"] is True
    assert parts[0].data["text"].strip() == rewrite_doc.strip()

    review_event = next(event for event in events if event.type == "review")
    assert review_event.data["rewritten"] is True
    done = next(event for event in events if event.type == "done")
    assert done.data["reply"].strip() == rewrite_doc.strip()
    assert llm.rewrite_user_messages, "改写提示词要带上审核发现的问题"
    assert "出现了输入中没有的性能指标" in llm.rewrite_user_messages[0]


def test_review_sees_the_whole_requirements_not_a_skeleton() -> None:
    """审核的唯一事实来源是**完整需求**，不能是骨架（结构化摘要被压成空壳）。"""
    llm = RoutingLLM(write=_INITIAL_DOC)
    _drain(DocumentService(llm=llm).generate_prd_from_summary_stream(summary=_SUMMARY))

    review_prompt = llm.review_user_messages[0]
    assert "问卷星Plus" in review_prompt
    assert "后端用 Python" in review_prompt


def test_review_document_stream_is_whole_document_for_prd() -> None:
    """重新审核：整篇一次、只产结论，不再逐章审。"""
    llm = RoutingLLM(review=_PASS)
    events = _drain(
        DocumentService(llm=llm).review_document_stream(
            doc_kind="prd", document=_INITIAL_DOC, summary=_SUMMARY
        )
    )

    assert llm.review_calls == 1
    assert _stages(events) == ["reviewing"]
    _assert_no_section_attribution(events)
    review = next(event for event in events if event.type == "review")
    assert review.data["kind"] == "prd"
    assert review.data["verdict"] == "pass"
    # 只审不改：不产正文
    assert not [event for event in events if event.type == "part"]


def test_fix_document_stream_rewrites_prd_as_a_whole() -> None:
    """按结论修复：整篇重写一次 → 只审一次，PRD 也走通用修复路径。"""
    llm = RoutingLLM(review=_PASS, rewrite=_REWRITTEN_DOC)
    conclusion = {
        "kind": "prd",
        "verdict": "revise",
        "summary": "编造了性能指标",
        "issues": [{"severity": "high", "detail": "出现了输入中没有的性能指标"}],
    }
    events = _drain(
        DocumentService(llm=llm).fix_document_stream(
            doc_kind="prd",
            document=_INITIAL_DOC,
            conclusion=conclusion,
            summary=_SUMMARY,
        )
    )

    stages = _stages(events)
    assert stages[0] == "writing"
    assert "reviewing" in stages
    assert llm.rewrite_calls == 1
    assert llm.review_calls == 1

    _assert_no_section_attribution(events)
    parts = [event for event in events if event.type == "part"]
    assert len(parts) == 1 and parts[0].data["replace"] is True
    done = next(event for event in events if event.type == "done")
    assert done.data["reply"].strip() == _REWRITTEN_DOC.strip()
    review = next(event for event in events if event.type == "review")
    assert review.data["kind"] == "prd"
    # 修复看的是上游**全文**：需求事实一个都不能少
    assert "问卷星Plus" in llm.rewrite_user_messages[0]
    assert "后端用 Python" in llm.rewrite_user_messages[0]


# ---------------------------------------------------------------------------
# 端到端：提交 → 大纲 → 撰写 → 审核 → 定稿 → 落版本
# ---------------------------------------------------------------------------

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
    """建一份带自由文本需求的方案（需求输入由服务端按方案取）。"""
    created = client.post(
        "/api/solutions",
        json={
            "title": "任务通道用例",
            "mode": "structured",
            "stage": "review-prd",
            "state": {"entry_mode": "prd-quick", "quick_requirements": requirements},
        },
    )
    assert created.status_code == 201, created.text
    return int(created.json()["id"])


class _PacedService:
    """把文档服务包一层：**第一个事件之前**先等一会，让订阅者来得及挂上来。"""

    def __init__(self, inner: DocumentService) -> None:
        self._inner = inner

    async def generate_prd_from_summary_stream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        await asyncio.sleep(0.1)
        async for event in self._inner.generate_prd_from_summary_stream(**kwargs):
            yield event


def test_sse_end_to_end_prd_generation_lands_a_version(client, monkeypatch) -> None:
    """端到端：提交任务 → 大纲 → 撰写 → 审核 → 定稿，正文落进 `documents` 与版本链。

    接缝是任务通道（`POST /api/jobs` → `GET /{id}/stream`）；只替换 LLM，
    鉴权 / 路由 / SSE 分帧 / 收尾落库全走真实实现。
    """
    from backend.services import document_service

    monkeypatch.setattr(
        document_service,
        "get_service",
        lambda: _PacedService(
            DocumentService(llm=RoutingLLM(write=_INITIAL_DOC, review=_PASS))
        ),
    )

    assert register(client).status_code == 200
    uid = int(users.get_by_username("alice")["id"])  # type: ignore[index]
    solution_id = _create_solution_with_requirements(client, "做一个演示产品")
    created = client.post("/api/jobs", json={"kind": "prd", "solution_id": solution_id})
    assert created.status_code == 201, created.text
    stream = client.get(f"/api/jobs/{created.json()['job_id']}/stream")
    assert stream.status_code == 200

    events = _parse_sse(stream.text)
    types = [event.get("type") for event in events]
    for expected in ("stage", "chunk", "review", "done"):
        assert expected in types, f"SSE 里缺了 {expected}：{types}"

    chunks = [event for event in events if event.get("type") == "chunk"]
    assert chunks, "正文必须流式可见（P0：按阶段看进度、看得见在写）"
    assert all("section_id" not in chunk["data"] for chunk in chunks)
    reply = next(event["data"]["reply"] for event in events if event.get("type") == "done")
    assert "".join(chunk["content"] for chunk in chunks).strip() == reply.strip()

    # 落版本：报告里的「PRD 仍能正常生成并落版本」由这一条守
    versions = document_store.list_versions(uid, solution_id, "prd")
    assert [item["version_no"] for item in versions] == [1]
    assert versions[0]["source"] == "prd"
    doc = document_store.get_document(uid, solution_id, "prd")
    assert doc is not None
    assert doc["content"].strip() == reply.strip()
