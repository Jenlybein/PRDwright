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
from backend.services import document_service, document_store, job_runtime, llm_factory, prd_section_writer
from backend.services.job_bus import job_bus

# 审核的假模型只按「这次有没有拿到某个事实」作答，不认提示词里的任何原文：
# 认提示词内文（人设词、章节小标题）会把用例焊死在提示词措辞上——改一句文案就红，
# 而行为其实没变。这里断言的是**结论**（verdict / 逐条问题），那是外部可观察的。


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
    """每个用例前后清空进程内运行时、任务集合与总线订阅（走公开的重置口）。"""
    job_runtime.reset_for_tests()
    job_bus.reset_for_tests()
    yield
    job_runtime.reset_for_tests()
    job_bus.reset_for_tests()


# ---------------------------------------------------------------------------
# 假模型：整篇审核一个池子、单章审核按「有没有拿到某个事实」作答
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
    """PRD 单章审核的假模型：**按「手里有没有那个事实」作答**，不认提示词原文。

    - `fact is None`：每一章都判「有问题」（用来验证汇总口径）；
    - 给了 `fact`：提示词里找得到这个事实就判「通过」，找不到就判「有问题」。

    于是「事实有没有真的进提示词」这件事，从**提示词原文断言**变成**结论断言**：
    用例只看 `verdict`，提示词措辞怎么改都不影响它。
    """

    def __init__(self, fact: str | None = None) -> None:
        self._fact = fact
        self.calls = 0

    async def astream(self, messages: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
        self.calls += 1
        user = str(messages[-1].content)
        await asyncio.sleep(0.2)
        if self._fact is not None and self._fact in user:
            yield AIMessageChunk(content='{"verdict": "pass"}')
            return
        yield AIMessageChunk(
            content=json.dumps(
                {
                    "verdict": "revise",
                    "issues": [{"severity": "high", "detail": "缺少关键内容"}],
                },
                ensure_ascii=False,
            )
        )


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
    """PRD 的结论由逐章重审汇总而来：一章一条问题，章节定位取自**当前正文**。

    用例刻意准备一份**手写**的正文（不是生成出来的）：重审读的是库里当前版本，
    与「这一版是谁产出的」无关。正文里两章（模板第一章 + 一个模板外的自定义章），
    因此应当有两次审核调用、两条按章定位的问题。
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

    llm = SectionReviewLLM()
    _fake_service(monkeypatch, llm)
    _job_id, events, status = _run_job(client, _review_payload(solution_id, "prd"))
    assert status == "completed"
    assert llm.calls == 2, "两章各审一次"

    conclusion = _review_events(events)[0]
    assert conclusion["kind"] == "prd"
    assert conclusion["verdict"] == "revise"
    assert conclusion["rewritten"] is False
    assert conclusion["failed_sections"] == []
    # 两章各一条：定位来自当前正文（自定义章也在内），不是模板
    assert len(conclusion["issues"]) == 2
    first, second = conclusion["issues"]
    assert first["section_id"] == prd_section_writer.PRD_SECTIONS[0].id
    assert first["section_title"] == prd_section_writer.PRD_SECTIONS[0].title
    assert first["severity"] == "high"
    assert second["section_title"] == "自定义章节"

    doc = _documents(client, solution_id)["prd"]
    assert doc["content"] == body, "按章重审不改正文"
    assert doc["review"] == conclusion
    assert len(_versions(client, solution_id, "prd")) == 1


# ---------------------------------------------------------------------------
# 三、需求澄清线程进重审提示词：与生成侧同一份内容口径
# ---------------------------------------------------------------------------


def test_rereview_of_prd_feeds_the_requirement_thread_into_the_prompt(client, monkeypatch):
    """重审要看得见需求澄清线程——它是「这一段有据还是编造」的另一半事实来源。

    只给摘要 / 自由文本时，用户在对话里拍板的细节（本例：支付方式）在模型眼里就是
    「需求输入里查不到」，于是正文里如实写了也会被判成编造。生成侧一直带着这条线程，
    重审不带，同一份正文会因为「是生成时审的还是重审的」得到不同结论。

    断言落在**结论**上：假模型只在「拿得到那个事实」时判通过，因此 verdict=pass
    就是「线程真的进了上下文」的外部证据；提示词原文一个字都不比。
    """
    register(client)
    solution_id = _create_solution(client)
    heading = prd_section_writer.PRD_SECTIONS[0].heading
    body = f"# 演示产品 产品需求文档（PRD）\n\n{heading}\n\n1.1 产品名称：演示产品\n"
    _seed_requirements(client, solution_id)
    _seed_requirement_turn(solution_id, "支付方式确定为只做微信支付，不做支付宝")
    _seed_document(client, solution_id, "prd", body)

    llm = SectionReviewLLM(fact="只做微信支付")
    _fake_service(monkeypatch, llm)
    _job_id, events, status = _run_job(client, _review_payload(solution_id, "prd"))

    assert status == "completed"
    assert llm.calls == 1, "这一版只有一章"
    conclusion = _review_events(events)[0]
    assert conclusion["verdict"] == "pass", "重审没有看到需求澄清线程里的拍板细节"


# ---------------------------------------------------------------------------
# 四、没审成 ≠ 通过：任务判失败，且不写结论
# ---------------------------------------------------------------------------

def test_failed_rereview_is_persisted_on_the_version(client, monkeypatch):
    """重审没跑成 → 失败时间落到**被审的那一版**上，刷新后仍读得到（US3）。

    为什么失败也要落库：它只说在会话内存里的话，刷新之后界面会退化成「未审核」，
    用户再也看不出这份正文被审过、且没审成。拿到新结论时清空——两者描述同一版，
    不能并存。
    """
    register(client)
    solution_id = _create_solution(client)
    _seed_document(client, solution_id, "api_docs", _API_DOCS_BODY)

    _fake_service(monkeypatch, WholeDocReviewLLM([ConnectionError("审核上游挂了")]))
    _job_id, _events, status = _run_job(client, _review_payload(solution_id, "api_docs"))
    assert status == "failed"

    # 「刷新」= 换一个会话重新读；失败是库里的事实，不是内存里的记忆
    client.cookies.clear()
    login(client)
    doc = _documents(client, solution_id)["api_docs"]
    assert doc["review"] is None, "失败不写结论（没有结论 ≠ 通过）"
    assert doc["review_failed_at"], "失败时间必须落库，否则刷新后退化成「未审核」"
    version_id = _versions(client, solution_id, "api_docs")[0]["version_id"]
    detail = _version_detail(client, solution_id, "api_docs", version_id)
    assert detail["review_failed_at"] == doc["review_failed_at"], "失败时间挂在被审的那一版上"

    # 补审一次：拿到结论的同时清掉失败时间
    _fake_service(monkeypatch, WholeDocReviewLLM([_PASS_JSON]))
    _job_id, _events, status = _run_job(client, _review_payload(solution_id, "api_docs"))
    assert status == "completed"
    refreshed = _documents(client, solution_id)["api_docs"]
    assert refreshed["review"]["verdict"] == "pass"
    assert refreshed["review_failed_at"] is None


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


@pytest.mark.parametrize(
    ("outcome", "code", "message"),
    [
        ("content_changed", "CONTENT_CHANGED", "正文已变更，本次审核结论未写入，请重新审核"),
        ("missing", "REVIEW_TARGET_MISSING", "已不存在"),
        ("raise", "INTERNAL_ERROR", "写入失败"),
    ],
    ids=["条件写未命中", "目标缺失", "写入抛异常"],
)
def test_second_write_gate_failure_fails_the_job(
    client, monkeypatch, outcome, code, message
):
    """写入 SQL 那道闸门失败时，任务必须失败并当场报出原因（规格 (c)2）。

    为什么单个「事前比较」不够：结论写到哪一版，是**写入那一刻**才定下的。事前比较与
    真正写入之间还有一段时间窗，条件写就是用来关掉它的。可它一旦真的拒写，收尾若只记
    一条日志、任务照样 `completed`，用户看到的是一份「成功但结论没变」的记录——比报错
    更糟：他没有任何线索该重审。

    这里模拟的正是**第二道闸门**：事前比较已经放行，写入那一步失败。
    """
    register(client)
    solution_id = _create_solution(client)
    _seed_document(client, solution_id, "api_docs", _API_DOCS_BODY)

    # 先有一条真结论：失败时它必须原样保留（不覆盖、也不清空）
    _fake_service(monkeypatch, WholeDocReviewLLM([_PASS_JSON]))
    _job_id, _events, status = _run_job(client, _review_payload(solution_id, "api_docs"))
    assert status == "completed"
    baseline = _documents(client, solution_id)["api_docs"]["review"]
    assert baseline["verdict"] == "pass"

    def _second_gate(*args, **kwargs):  # noqa: ANN002, ANN003
        if outcome == "raise":
            raise RuntimeError("写入失败")
        return outcome

    monkeypatch.setattr(document_store, "commit_review", _second_gate)

    _fake_service(monkeypatch, WholeDocReviewLLM([_REVISE_JSON]))
    job_id, events, status = _run_job(client, _review_payload(solution_id, "api_docs"))

    assert status == "failed", "第二道闸门拒写，任务不能报成功"
    errors = [event for event in events if event["type"] == "error"]
    assert errors, "订阅者还连着：失败原因必须当场发一条 error 事件"
    assert errors[-1]["code"] == code
    assert message in errors[-1]["content"]

    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["error_code"] == code
    assert message in job["error_message"]

    doc = _documents(client, solution_id)["api_docs"]
    assert doc["content"] == _API_DOCS_BODY, "拒写不该动正文"
    assert doc["review"] == baseline, "已有结论原样保留"
    assert len(_versions(client, solution_id, "api_docs")) == 1, "拒写不该追加版本"


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

    _fake_service(monkeypatch, SectionReviewLLM())
    _job_id, events, status = _run_job(client, _review_payload(solution_id, "prd"))

    assert status == "completed"
    conclusion = _review_events(events)[0]
    assert conclusion["verdict"] == "revise", "假模型每章都判有问题，汇总也应是「有问题」"
    assert _documents(client, solution_id)["prd"]["review"] == conclusion
