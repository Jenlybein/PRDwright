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
from conftest import login, register, users
from langchain_core.messages import AIMessageChunk, BaseMessage

from backend.core.engine import session_scope
from backend.repositories.chat_turn_repo import ChatTurnRepository
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

    def __init__(self, replies: list[str | BaseException], *, delay: float = 0.2) -> None:
        self._replies = list(replies)
        self._delay = delay
        self.calls = 0

    async def astream(self, messages: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
        assert _DOC_REVIEW_MARKER in str(messages[0].content)
        self.calls += 1
        # 让出一点真实时间：真实模型不是瞬时返回的，而「建任务 → 订阅」这两步之间
        # 任务必须仍在跑，否则本次订阅拿不到 `review` 事件（它按设计不重放），
        # 用例也就无从断言「订阅期间即时看到结论」（与 `test_review_persistence` 同口径）。
        await asyncio.sleep(self._delay)
        reply = self._replies[min(self.calls - 1, len(self._replies) - 1)]
        if isinstance(reply, BaseException):
            raise reply
        middle = len(reply) // 2
        for piece in (reply[:middle], reply[middle:]):
            if piece:
                await asyncio.sleep(0)
                yield AIMessageChunk(content=piece)


class SectionReviewLLM:
    """PRD 单章审核的假模型：指定章节判 revise，其余判 pass。

    同时记下每次调用收到的用户消息：重审提示词里到底装了什么，只能从这里看。
    """

    def __init__(self, *, revise_heading: str) -> None:
        self._revise_heading = revise_heading
        self.calls = 0
        self.prompts: list[str] = []

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
        self.prompts.append(str(messages[-1].content))
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


def _seed_requirement_turn(solution_id: int, content: str) -> None:
    """在需求澄清线程里落一条用户轮次（PRD 生成 / 重审共用的上游之一）。

    这只是**准备材料**：生产里这条轮次由对话任务写入同一张表；
    断言仍走 HTTP 主链路。用例里注册的用户都是 alice。
    """
    user = users.get_by_username("alice")
    assert user is not None
    with session_scope() as session:
        ChatTurnRepository(session).create(
            int(user["id"]),
            solution_id,
            stage="requirement",
            role="user",
            content=content,
        )


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
# 三、需求澄清线程进重审提示词：与生成侧同一份内容口径
# ---------------------------------------------------------------------------


def test_rereview_of_prd_feeds_the_requirement_thread_into_the_prompt(client, monkeypatch):
    """重审提示词里要带上需求澄清线程——它是「这一段有据还是编造」的另一半事实来源。

    只给摘要 / 自由文本时，用户在对话里拍板的细节（本例：支付方式）在模型眼里就是
    「需求输入里查不到」，于是正文里如实写了也会被判成编造。生成侧一直带着这条线程，
    重审不带，同一份正文会因为「是生成时审的还是重审的」得到不同结论。

    断言用的是**正文里没有、只在线程里出现过**的那句话，否则「它本来就在章节正文里」
    会让这条用例永远通过。
    """
    register(client)
    solution_id = _create_solution(client)
    heading = prd_section_writer.PRD_SECTIONS[0].heading
    body = f"# 演示产品 产品需求文档（PRD）\n\n{heading}\n\n1.1 产品名称：演示产品\n"
    _seed_requirements(client, solution_id)
    _seed_requirement_turn(solution_id, "支付方式确定为只做微信支付，不做支付宝")
    _seed_document(client, solution_id, "prd", body)

    llm = SectionReviewLLM(revise_heading="## 不在正文里的一章")
    _fake_service(monkeypatch, llm)
    _job_id, _events, status = _run_job(client, _review_payload(solution_id, "prd"))

    assert status == "completed"
    assert llm.prompts, "重审应当至少调用一次模型"
    for prompt in llm.prompts:
        assert "只做微信支付" in prompt, "需求澄清线程要进重审提示词"


# ---------------------------------------------------------------------------
# 四、没审成 ≠ 通过：任务判失败，且不写结论
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


def test_rereview_fails_when_the_body_changed_midway(client, monkeypatch):
    """审到一半正文被改 → 任务失败、结论不写、旧结论原样保留（B3）。

    为什么必须失败：重审的正文是**建任务那一刻**的快照，结论却要写到「此刻的当前版本」
    上。两者之间用户手改一次，结论就落在一份**没审过的正文**上——它看起来完全正常，
    却是错的。宁可判失败、让用户重审一次，也不能写一条指向别的正文的结论。
    """
    register(client)
    solution_id = _create_solution(client)
    _seed_document(client, solution_id, "api_docs", _API_DOCS_BODY)

    # 先有一条真结论（「通过」）：B3 要求不一致时**不覆盖**已有结论
    _fake_service(monkeypatch, WholeDocReviewLLM([_PASS_JSON]))
    _job_id, _first_events, status = _run_job(client, _review_payload(solution_id, "api_docs"))
    assert status == "completed"
    baseline = _documents(client, solution_id)["api_docs"]["review"]
    assert baseline["verdict"] == "pass"

    # 第二次重审：建任务（快照）之后、收尾之前，用户把正文改了
    _fake_service(monkeypatch, WholeDocReviewLLM([_REVISE_JSON], delay=0.6))
    created = client.post("/api/jobs", json=_review_payload(solution_id, "api_docs"))
    assert created.status_code == 201, created.text
    job_id = int(created.json()["job_id"])

    edited = _API_DOCS_BODY + "\n## 二、鉴权\n\n手改追加的一节。\n"
    saved = client.put(
        f"/api/solutions/{solution_id}/documents/api_docs", json={"content": edited}
    )
    assert saved.status_code == 200, saved.text

    stream = client.get(f"/api/jobs/{job_id}/stream")
    assert stream.status_code == 200, stream.text
    events = _events(stream)
    # 订阅者还连着：失败原因要能当场读到（否则前端只看到「流读完、任务却失败」）
    errors = [event for event in events if event["type"] == "error"]
    assert errors, "正文已变更的失败必须发一条 error 事件"
    assert "正文已变更" in errors[-1]["content"]

    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "failed", "被审的正文变了，本次结论无处可写"
    assert "正文已变更" in job["error_message"]
    assert "重新审核" in job["error_message"]

    doc = _documents(client, solution_id)["api_docs"]
    assert doc["content"] == edited, "手改的正文照旧保留"
    assert doc["review"] == baseline, "结论不写到没审过的正文上，也不覆盖已有结论"


# ---------------------------------------------------------------------------
# 五、入口校验：目标产物、方案与被审正文都是必填
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


def test_review_job_requires_the_document_body(client):
    """这一份还没有正文时 400，而不是建一个注定空跑的任务。

    「没有正文」也没有可写结论的位置（结论写在**当前版本**上），因此这是一次
    货真价实的请求错误，和「缺上游」同一条口径（C-R3：材料服务端取，缺了就明说）。
    """
    register(client)
    solution_id = _create_solution(client)

    response = client.post("/api/jobs", json=_review_payload(solution_id, "api_docs"))

    assert response.status_code == 400, response.text
    assert "还没有正文" in response.json()["detail"]


def test_rereview_of_prd_tolerates_missing_requirement_input(client, monkeypatch):
    """**旧方案也能补审**：PRD 有正文、却没有需求输入时，重审照样跑（按降级核对）。

    与生成不同：生成缺需求直接 400（没有事实来源就写不出东西），而重审的产出是
    「这一版被审成什么样」——上游只是对照材料，缺了只是判定的把握变小。
    """
    register(client)
    solution_id = _create_solution(client)
    heading = prd_section_writer.PRD_SECTIONS[0].heading
    body = f"# 演示产品 产品需求文档（PRD）\n\n{heading}\n\n1.1 产品名称：演示产品\n"
    _seed_document(client, solution_id, "prd", body)

    _fake_service(monkeypatch, SectionReviewLLM(revise_heading="## 不在正文里的一章"))
    _job_id, events, status = _run_job(client, _review_payload(solution_id, "prd"))

    assert status == "completed"
    conclusion = _review_events(events)[0]
    assert conclusion["verdict"] == "pass"
    assert _documents(client, solution_id)["prd"]["review"] == conclusion
