"""重新审核任务（只审不改，票 #55）的契约测试。

守的是审阅页「审核结论」面板四态背后的那条事实：

- 重审**只更新当前版本的结论**，不碰正文、不追加版本（C-R2）；
- 目标产物由 `doc_kind` 决定，缺了就是 400（与 `clarify` 同口径）；
- 审核侧真的没审成（调用失败 / 回的不是 JSON）时**任务判失败**，绝不写一条
  「通过」占位（C-R1：审核失败不写结论，没有结论 ≠ 通过）；
- PRD 按章重审当前正文，汇总成一条与生成时同形的结论。

所有用例只跨 HTTP 主接缝：

    PUT  /api/solutions/{id}/documents/{kind}        （准备「当前版本」正文）
    POST /api/jobs（kind=review）→ GET /api/jobs/{id}/stream（订阅到结束）
    GET  /api/solutions/{id}/documents → GET .../{kind}/versions → .../versions/{vid}

用例里不构造运行态、不直连仓储断言入库，也不复用 SSE 之外的内部形状；
LLM 一律注入离线假模型（真实工厂一被调用就抛）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

import pytest
from conftest import login, register
from langchain_core.messages import AIMessageChunk, BaseMessage

from backend.services import document_service, job_runtime, llm_factory, prd_section_writer
from backend.services.job_bus import job_bus

# 审核人设提示词里的特征词：用它区分「整篇审核」与「单章审核」两类调用
# （与 `test_review_persistence.py` / `test_document_review.py` 同口径）。
_DOC_REVIEW_MARKER = "技术文档评审员"
_PRD_REVIEW_MARKER = "产品文档评审员"
# 单章审核提示词里「待审章节」那一节的标题行前缀：假模型靠它认出这一章是谁
_PRD_SECTION_PREFIX = "## 四、待审章节："


# ---------------------------------------------------------------------------
# 夹具：不触网、任务状态互相隔离
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_real_llm(monkeypatch):
    """真实 LLM 工厂一被调用就抛，杜绝任何测试触网。"""

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("测试不应触网调用真实 LLM")

    monkeypatch.setattr(llm_factory, "get_llm", _boom)
    monkeypatch.setattr(document_service, "get_llm", _boom)
    monkeypatch.setattr(prd_section_writer, "get_llm", _boom)


@pytest.fixture(autouse=True)
def clean_job_state():
    """每个用例前后清空进程内运行时、任务集合与总线订阅。"""
    job_runtime._RUNTIMES.clear()  # noqa: SLF001
    job_runtime._TASKS.clear()  # noqa: SLF001
    job_bus._subscribers.clear()  # noqa: SLF001
    yield
    job_runtime._RUNTIMES.clear()  # noqa: SLF001
    job_runtime._TASKS.clear()  # noqa: SLF001
    job_bus._subscribers.clear()  # noqa: SLF001


# ---------------------------------------------------------------------------
# 假模型：整篇审核一个池子、单章审核按「待审章节」路由
# ---------------------------------------------------------------------------


class WholeDocReviewLLM:
    """整篇审核的假模型：每次调用取池子里的下一条（用完复用最后一条）。

    条目为异常时在**吐第一个字之前**抛出——那正是「调用失败」的真实形态。
    """

    def __init__(self, replies: list[str | BaseException]) -> None:
        self._replies = list(replies)
        self.calls = 0

    async def astream(self, messages: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
        assert _DOC_REVIEW_MARKER in str(messages[0].content)
        self.calls += 1
        # 让出一点真实时间：真实模型不是瞬时返回的，而「建任务 → 订阅」这两步之间
        # 任务必须仍在跑，否则本次订阅拿不到 `review` 事件（它按设计不重放），
        # 用例也就无从断言「订阅期间即时看到结论」（与 `test_review_persistence` 同口径）。
        await asyncio.sleep(0.2)
        reply = self._replies[min(self.calls - 1, len(self._replies) - 1)]
        if isinstance(reply, BaseException):
            raise reply
        middle = len(reply) // 2
        for piece in (reply[:middle], reply[middle:]):
            if piece:
                await asyncio.sleep(0)
                yield AIMessageChunk(content=piece)


class SectionReviewLLM:
    """PRD 单章审核的假模型：指定章节判 revise，其余判 pass。"""

    def __init__(self, *, revise_heading: str) -> None:
        self._revise_heading = revise_heading
        self.calls = 0

    @staticmethod
    def _heading(user: str) -> str:
        """取提示词里「待审章节」那一行给出的标题行。"""
        for line in user.splitlines():
            if line.startswith(_PRD_SECTION_PREFIX):
                return line[len(_PRD_SECTION_PREFIX) :].strip()
        return ""

    async def astream(self, messages: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
        assert _PRD_REVIEW_MARKER in str(messages[0].content)
        self.calls += 1
        await asyncio.sleep(0.2)
        heading = self._heading(str(messages[-1].content))
        if heading == self._revise_heading:
            yield AIMessageChunk(
                content=json.dumps(
                    {
                        "verdict": "revise",
                        "issues": [
                            {"where": heading, "severity": "high", "detail": "缺少关键内容"}
                        ],
                    },
                    ensure_ascii=False,
                )
            )
            return
        yield AIMessageChunk(content='{"verdict": "pass"}')


# ---------------------------------------------------------------------------
# 主接缝小工具
# ---------------------------------------------------------------------------


def _events(response) -> list[dict[str, Any]]:  # noqa: ANN001 - TestClient 响应
    """把 SSE 响应解析成事件 dict 列表（`data:` 行即 `StreamEvent` 的 JSON）。"""
    payloads: list[dict[str, Any]] = []
    for line in response.text.splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[len("data:") :].strip()
        if not raw or raw == "[DONE]":
            continue
        payloads.append(json.loads(raw))
    return payloads


def _create_solution(client) -> int:  # noqa: ANN001
    """建一份真实方案并返回 id（重审要求归属可校验）。"""
    response = client.post(
        "/api/solutions",
        json={"title": "重新审核用例", "mode": "structured", "stage": "review-prd", "data": "{}"},
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _run_job(
    client, payload: dict[str, Any]  # noqa: ANN001
) -> tuple[int, list[dict[str, Any]], str]:
    """建任务 → 订阅到结束，返回 (job_id, 全部事件, 终态)。"""
    created = client.post("/api/jobs", json=payload)
    assert created.status_code == 201, created.text
    job_id = int(created.json()["job_id"])

    stream = client.get(f"/api/jobs/{job_id}/stream")
    assert stream.status_code == 200, stream.text
    events = _events(stream)

    status = client.get(f"/api/jobs/{job_id}").json()["status"]
    return job_id, events, status


def _review_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event["data"] for event in events if event["type"] == "review"]


def _documents(client, solution_id: int) -> dict[str, dict[str, Any]]:  # noqa: ANN001
    """读文档列表，按产物类型索引。"""
    response = client.get(f"/api/solutions/{solution_id}/documents")
    assert response.status_code == 200, response.text
    return {item["kind"]: item for item in response.json()["documents"]}


def _versions(client, solution_id: int, kind: str) -> list[dict[str, Any]]:  # noqa: ANN001
    response = client.get(f"/api/solutions/{solution_id}/documents/{kind}/versions")
    assert response.status_code == 200, response.text
    return response.json()["versions"]


def _version_detail(client, solution_id: int, kind: str, version_id: int) -> dict[str, Any]:  # noqa: ANN001
    response = client.get(
        f"/api/solutions/{solution_id}/documents/{kind}/versions/{version_id}"
    )
    assert response.status_code == 200, response.text
    return response.json()


def _fake_service(monkeypatch, llm) -> None:  # noqa: ANN001
    """把模块级文档服务换成注入假 LLM 的实例。"""
    monkeypatch.setattr(
        document_service,
        "get_service",
        lambda: document_service.DocumentService(llm=llm),
    )


def _seed_document(client, solution_id: int, kind: str, content: str) -> None:  # noqa: ANN001
    """经公开接口准备一份「当前版本」正文（重审的输入只从库里取，见 C-R3）。"""
    saved = client.put(
        f"/api/solutions/{solution_id}/documents/{kind}",
        json={"content": content},
    )
    assert saved.status_code == 200, saved.text


def _seed_requirements(client, solution_id: int) -> None:  # noqa: ANN001
    """把需求输入写进方案（PRD 的上游同样由服务端取，见 C-R3）。"""
    saved = client.put(
        f"/api/solutions/{solution_id}",
        json={
            "title": "重新审核用例",
            "mode": "structured",
            "stage": "review-prd",
            "state": {
                "quick_product_name": "演示产品",
                "quick_requirements": "做一个演示产品",
            },
        },
    )
    assert saved.status_code == 200, saved.text


def _review_payload(solution_id: int, doc_kind: str) -> dict[str, Any]:
    return {"kind": "review", "solution_id": solution_id, "doc_kind": doc_kind}


_API_DOCS_BODY = "## 一、概述\n\n统一响应结构：`{code, data, message}`\n"
_PROMPTS_BODY = "## 一、角色设定\n\n你是一位资深产品经理。\n"
_PASS_JSON = json.dumps({"verdict": "pass", "issues": []})
_REVISE_JSON = json.dumps(
    {
        "verdict": "revise",
        "issues": [{"severity": "high", "detail": "编造了鉴权方式"}],
        "instruction": "删掉编造的鉴权",
    },
    ensure_ascii=False,
)


# ---------------------------------------------------------------------------
# 一、重审只更新结论：正文逐字不变、版本数不变
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("doc_kind", "body"),
    [("api_docs", _API_DOCS_BODY), ("prompts", _PROMPTS_BODY)],
)
def test_rereview_updates_the_conclusion_in_place(client, monkeypatch, doc_kind, body):
    """两份「整篇审核」的产物：首次重审补上结论，再次重审覆盖同一条，正文与版本都不动。

    两轮都走同一条 HTTP 主接缝，因此这条用例同时守住三件事：从未审过的旧文档能补审、
    重审不追加版本、重审结果刷新（换会话）后仍在。
    """
    register(client)
    solution_id = _create_solution(client)
    _seed_document(client, solution_id, doc_kind, body)

    # 第一轮：从未审核 → 通过
    _fake_service(monkeypatch, WholeDocReviewLLM([_PASS_JSON]))
    _job_id, events, status = _run_job(client, _review_payload(solution_id, doc_kind))
    assert status == "completed"
    conclusions = _review_events(events)
    assert len(conclusions) == 1, "一次重审只该给出一条结论"
    first = conclusions[0]
    assert first["kind"] == doc_kind
    assert first["verdict"] == "pass"
    assert first["rewritten"] is False, "重审只审不改"
    assert first["reviewed_at"]

    after_first = _documents(client, solution_id)[doc_kind]
    assert after_first["content"] == body, "重审不改正文"
    assert after_first["review"] == first

    # 第二轮：通过 → 有问题（覆盖同一条，不追加版本）
    _fake_service(monkeypatch, WholeDocReviewLLM([_REVISE_JSON]))
    _job_id, events, status = _run_job(client, _review_payload(solution_id, doc_kind))
    assert status == "completed"
    second = _review_events(events)[0]
    assert second["verdict"] == "revise"
    assert second["issues"][0]["severity"] == "high"

    after_second = _documents(client, solution_id)[doc_kind]
    assert after_second["content"] == body, "重审不该动正文一个字节"
    assert after_second["review"] == second

    versions = _versions(client, solution_id, doc_kind)
    assert [item["version_no"] for item in versions] == [1], "重审不追加版本"
    detail = _version_detail(client, solution_id, doc_kind, versions[0]["version_id"])
    assert detail["review"] == second
    assert detail["content"] == body

    # 刷新 / 换会话后读到的仍是重审那一刻的结论（落库那条才是唯一来源）
    client.cookies.clear()
    login(client)
    assert _documents(client, solution_id)[doc_kind]["review"] == second


# ---------------------------------------------------------------------------
# 二、PRD 按章重审当前正文，汇总成一条与生成时同形的结论
# ---------------------------------------------------------------------------


def test_rereview_of_prd_reviews_the_current_body_section_by_section(client, monkeypatch):
    """PRD 的结论由逐章重审汇总而来：`section_id` / `section_title` 与生成时同一套口径。

    用例刻意准备一份**手写**的正文（不是生成出来的）：重审读的是库里当前版本，
    与「这一版是谁产出的」无关。
    """
    register(client)
    solution_id = _create_solution(client)
    _seed_requirements(client, solution_id)
    revise_heading = prd_section_writer.PRD_SECTIONS[0].heading
    body = (
        "# 演示产品 产品需求文档（PRD）\n\n"
        f"{revise_heading}\n\n1.1 产品名称：演示产品\n\n"
        "## 自定义章节\n\n这一章不在模板里\n"
    )
    _seed_document(client, solution_id, "prd", body)

    llm = SectionReviewLLM(revise_heading=revise_heading)
    _fake_service(monkeypatch, llm)
    _job_id, events, status = _run_job(client, _review_payload(solution_id, "prd"))
    assert status == "completed"
    assert llm.calls == 2, "两章各审一次"

    conclusion = _review_events(events)[0]
    assert conclusion["kind"] == "prd"
    assert conclusion["verdict"] == "revise"
    assert conclusion["rewritten"] is False
    assert conclusion["failed_sections"] == []
    assert len(conclusion["issues"]) == 1
    issue = conclusion["issues"][0]
    assert issue["section_id"] == prd_section_writer.PRD_SECTIONS[0].id
    assert issue["section_title"] == prd_section_writer.PRD_SECTIONS[0].title
    assert issue["severity"] == "high"

    doc = _documents(client, solution_id)["prd"]
    assert doc["content"] == body, "按章重审不改正文"
    assert doc["review"] == conclusion
    assert len(_versions(client, solution_id, "prd")) == 1


# ---------------------------------------------------------------------------
# 三、没审成 ≠ 通过：任务判失败，且不写结论
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [ConnectionError("审核上游挂了"), "这不是 JSON"],
    ids=["调用失败", "回的不是 JSON"],
)
def test_failed_rereview_writes_no_conclusion(client, monkeypatch, reply):
    """重审自己失败时任务必须失败——既不能把上一版结论算作本次结果，
    也不能写一条「通过」占位（那是最误导人的一条假记录）。"""
    register(client)
    solution_id = _create_solution(client)
    _seed_document(client, solution_id, "api_docs", _API_DOCS_BODY)

    _fake_service(monkeypatch, WholeDocReviewLLM([reply]))
    _job_id, events, status = _run_job(client, _review_payload(solution_id, "api_docs"))

    assert status == "failed"
    assert any(event["type"] == "error" for event in events), "失败要有可读的原因"
    assert _review_events(events) == [], "没审成不该给出一条结论"

    doc = _documents(client, solution_id)["api_docs"]
    assert doc["review"] is None, "审核失败不写结论（没有结论 ≠ 通过）"
    assert doc["content"] == _API_DOCS_BODY
    assert len(_versions(client, solution_id, "api_docs")) == 1


# ---------------------------------------------------------------------------
# 四、入口校验：目标产物与方案都是必填
# ---------------------------------------------------------------------------


def test_review_job_requires_doc_kind(client):
    """`review` 的 `kind` 只说「做了一件什么事」，产物类型必须由 `doc_kind` 给出。"""
    register(client)
    solution_id = _create_solution(client)

    response = client.post("/api/jobs", json={"kind": "review", "solution_id": solution_id})

    assert response.status_code == 400, response.text
    assert "doc_kind" in response.json()["detail"]


def test_review_job_requires_a_solution(client):
    """没有方案就没有可读的正文，也没有可写结论的位置——门口直接拒掉。"""
    register(client)

    response = client.post("/api/jobs", json={"kind": "review", "doc_kind": "prd"})

    assert response.status_code == 400, response.text
