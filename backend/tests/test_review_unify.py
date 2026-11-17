"""审核 / 改写 / 修复统一为整篇（票 #94，spec #75 的收口票）。

三份产物（PRD / 接口文档 / 提示词套件）自 #92 / #93 起共用同一条整篇管线。本文件把
票面的五条 AC 钉成不变量，**尽量逐条参数化三份产物**——「只有一份产物满足」正是这条
管线最容易退回去的样子（分章时代 PRD 就走在另一条路上）。

五条 AC 与其守的接缝：

1. **一版一条、三份同形**：结论住在 `document_versions.review` 上；重审覆盖同一条、
   不追加版本；三份产物读出来的结论键集完全相同（`_CONCLUSION_KEYS`）。
2. **只有 high 触发改写，改写后不重审**：一次生成里审核调用恰好一次、改写调用至多一次，
   且改写发生在审核之后、之后不再回到 `reviewing`。
3. **修复整篇重写、但追加新版本**：走 #81 的唯一写入入口
   `document_store.write_document(mode="append")`——不是「重生成文档」，旧版本原样保留。
4. **重审只更新当前版本结论、不改正文**：条件写 `expected_content_sha`；正文变了就
   `CONTENT_CHANGED`、原结论与正文都不动。
5. **审核失败不写结论**：界面照实显示「审核失败」。「没审」与「审失败」靠显式
   `attempted` 标记区分，**不判空串**。

另外守一条清理结论：`failed_sections` / `completed_sections` 这些分章遗留形状随 #94
收口（详见 `test_chapter_leftover_fields_are_gone`）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

import pytest
from conftest import login, register
from langchain_core.messages import AIMessageChunk, BaseMessage

from backend.core.contracts import RunSummary
from backend.core.models import GenerationJob
from backend.services import (
    document_review,
    document_service,
    document_store,
    job_runtime,
    llm_factory,
)
from backend.services.document_service import DocumentService
from backend.services.job_bus import job_bus

# 两类调用靠系统提示词区分（口径与 `test_review_job.py` / `test_prd_whole_stream.py` 一致）
_REVIEW_MARKER = "技术文档评审员"
_REWRITE_MARKER = "技术文档撰写者"

# 一份「有问题」的审核判定：**故意不给 `where`**，让改写走整篇那条（与产物无关、可复现）
_REVISE_HIGH = json.dumps(
    {
        "verdict": "revise",
        "issues": [{"severity": "high", "detail": "功能清单里出现了需求中没有的模块"}],
        "instruction": "删掉编造的模块",
    },
    ensure_ascii=False,
)
# 三条 medium：只报不改（改写门槛是 high）
_REVISE_MEDIUM = json.dumps(
    {
        "verdict": "revise",
        "issues": [
            {"severity": "medium", "detail": "表述偏空泛"},
            {"severity": "medium", "detail": "标题层级与模板略有出入"},
        ],
    },
    ensure_ascii=False,
)
_PASS = json.dumps({"verdict": "pass", "issues": []}, ensure_ascii=False)

# `review` 结论的稳定形状（事件与落库共用 `document_review.review_data` 一处）。
# `failed_sections` 已随分章退役（#94），列在下面就是回归。
_CONCLUSION_KEYS = {
    "attempted",
    "issues",
    "kind",
    "reviewed_at",
    "rewritten",
    "summary",
    "verdict",
}

# ---------------------------------------------------------------------------
# 夹具数据：三份产物各一份正文
# ---------------------------------------------------------------------------

_PRD_BODY = (
    "# 演示产品 产品需求文档（PRD）\n\n"
    "## 一、产品概述\n\n1.1 产品名称：演示产品\n\n"
    "## 二、功能清单\n\n- 待办清单：勾选完成\n"
)
_API_DOCS_BODY = "# 接口文档\n\n## 一、概述\n\n统一响应结构：`{code, data, message}`\n"
_PROMPTS_BODY = "# 提示词套件\n\n## 一、角色设定\n\n你是一位资深产品经理。\n"

# 修复稿：落库时会剥掉尾随空白（`pending_items.extract`），期望值因此不带结尾换行
_FIXED_PRD = (
    "# 演示产品 产品需求文档（PRD）\n\n"
    "## 一、产品概述\n\n1.1 产品名称：演示产品\n\n"
    "## 二、功能清单\n\n- 待办清单：勾选完成（已删掉编造的模块）。"
)
_FIXED_API_DOCS = (
    "# 接口文档\n\n## 一、概述\n\n统一响应结构：`{code, data, message}`\n\n"
    "## 二、鉴权\n\n按需求补齐鉴权说明后重写的整篇。"
)
_FIXED_PROMPTS = "# 提示词套件\n\n## 一、角色设定\n\n你是一位资深产品经理，按 PRD 的接口约定回答。"

_PRODUCT_ROWS: list[tuple[str, str, str]] = [
    ("prd", _PRD_BODY, _FIXED_PRD),
    ("api_docs", _API_DOCS_BODY, _FIXED_API_DOCS),
    ("prompts", _PROMPTS_BODY, _FIXED_PROMPTS),
]
_PRODUCTS = [pytest.param(*row, id=row[0]) for row in _PRODUCT_ROWS]


# ---------------------------------------------------------------------------
# 夹具：不触网、任务状态互相隔离
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_real_llm(monkeypatch):
    """真实 LLM 工厂一被调用就抛，杜绝任何用例触网。"""

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("测试不应触网调用真实 LLM")

    monkeypatch.setattr(llm_factory, "get_llm", _boom)
    monkeypatch.setattr(document_service, "get_llm", _boom)


@pytest.fixture(autouse=True)
def clean_job_state():
    job_runtime.reset_for_tests()
    job_bus.reset_for_tests()
    yield
    job_runtime.reset_for_tests()
    job_bus.reset_for_tests()


class PoolLLM:
    """按系统提示词分流的假模型：审核池 / 写作与改写池各自按调用次序取条目。

    条目是异常时在**吐第一个字之前**抛出——那是「调用失败」的真实形态。
    `delay` 让出真实时间，好让用例在「建任务之后、收尾之前」去改正文（AC4 需要）。
    """

    def __init__(
        self,
        *,
        writers: list[str | BaseException] | None = None,
        reviewers: list[str | BaseException] | None = None,
        delay: float = 0.15,
    ) -> None:
        self._writers = list(writers or [])
        self._reviewers = list(reviewers or [])
        self.writer_calls = 0
        self.review_calls = 0
        self._delay = delay

    @staticmethod
    def _pick(pool: list[str | BaseException], calls: int) -> str | BaseException:
        assert pool, "假模型没有配置可用的回复"
        return pool[min(calls - 1, len(pool) - 1)]

    async def astream(self, messages: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
        system = str(messages[0].content)
        if _REVIEW_MARKER in system:
            self.review_calls += 1
            reply = self._pick(self._reviewers, self.review_calls)
        else:
            self.writer_calls += 1
            reply = self._pick(self._writers, self.writer_calls)
        if self._delay:
            await asyncio.sleep(self._delay)
        if isinstance(reply, BaseException):
            raise reply
        middle = len(reply) // 2
        for piece in (reply[:middle], reply[middle:]):
            if piece:
                yield AIMessageChunk(content=piece)


class RoutingLLM(PoolLLM):
    """在 `PoolLLM` 之上再分开「撰写」与「改写」：改写用的是另一枚人设提示词。

    三份产物的整篇链路总调用次数是 1（撰写）/ 2（+审核）/ 3（+改写），因此按**角色**
    分流即可——调用次数本身就是被断言的契约。
    """

    def __init__(self, *, write: str = "", review: str = _PASS, rewrite: str = "") -> None:
        super().__init__(reviewers=[review])
        self._write = write
        self._rewrite = rewrite
        self.write_calls = 0
        self.review_calls = 0
        self.rewrite_calls = 0

    async def astream(self, messages: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
        system = str(messages[0].content)
        if _REVIEW_MARKER in system:
            self.review_calls += 1
            text: str | BaseException = self._reviewers[0]
        elif _REWRITE_MARKER in system:
            self.rewrite_calls += 1
            text = self._rewrite
        else:
            self.write_calls += 1
            text = self._write
        if isinstance(text, BaseException):
            raise text
        middle = len(text) // 2
        for piece in (text[:middle], text[middle:]):
            if piece:
                yield AIMessageChunk(content=piece)


# ---------------------------------------------------------------------------
# 主接缝小工具（只跨 HTTP：建任务 → 订阅 → 读回文档 / 版本）
# ---------------------------------------------------------------------------


def _parse_events(response) -> list[dict[str, Any]]:  # noqa: ANN001 - TestClient 响应
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
    response = client.post(
        "/api/solutions",
        json={"title": "整篇审核收口用例", "mode": "structured", "stage": "review-prd", "data": "{}"},
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _run_job(
    client, payload: dict[str, Any]  # noqa: ANN001
) -> tuple[int, list[dict[str, Any]], str]:
    created = client.post("/api/jobs", json=payload)
    assert created.status_code == 201, created.text
    job_id = int(created.json()["job_id"])
    stream = client.get(f"/api/jobs/{job_id}/stream")
    assert stream.status_code == 200, stream.text
    events = _parse_events(stream)
    status = client.get(f"/api/jobs/{job_id}").json()["status"]
    return job_id, events, status


def _review_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event["data"] for event in events if event["type"] == "review"]


def _documents(client, solution_id: int) -> dict[str, dict[str, Any]]:  # noqa: ANN001
    response = client.get(f"/api/solutions/{solution_id}/documents")
    assert response.status_code == 200, response.text
    return {item["kind"]: item for item in response.json()["documents"]}


def _versions(client, solution_id: int, kind: str) -> list[dict[str, Any]]:  # noqa: ANN001
    response = client.get(f"/api/solutions/{solution_id}/documents/{kind}/versions")
    assert response.status_code == 200, response.text
    return response.json()["versions"]


def _version_detail(  # noqa: ANN001
    client, solution_id: int, kind: str, version_id: int
) -> dict[str, Any]:
    response = client.get(
        f"/api/solutions/{solution_id}/documents/{kind}/versions/{version_id}"
    )
    assert response.status_code == 200, response.text
    return response.json()


def _fake_service(monkeypatch, llm) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        document_service,
        "get_service",
        lambda: document_service.DocumentService(llm=llm),
    )


def _seed_document(client, solution_id: int, kind: str, content: str) -> None:  # noqa: ANN001
    saved = client.put(
        f"/api/solutions/{solution_id}/documents/{kind}",
        json={"content": content},
    )
    assert saved.status_code == 200, saved.text


def _seed_requirements(client, solution_id: int) -> None:  # noqa: ANN001
    saved = client.put(
        f"/api/solutions/{solution_id}",
        json={
            "title": "整篇审核收口用例",
            "mode": "structured",
            "stage": "review-prd",
            "state": {
                "quick_product_name": "演示产品",
                "quick_requirements": "做一个给团队写周报的工具",
            },
        },
    )
    assert saved.status_code == 200, saved.text


def _review_payload(solution_id: int, doc_kind: str) -> dict[str, Any]:
    return {"kind": "review", "solution_id": solution_id, "doc_kind": doc_kind}


def _fix_payload(solution_id: int, doc_kind: str) -> dict[str, Any]:
    return {"kind": "review_fix", "solution_id": solution_id, "doc_kind": doc_kind}


def _establish_revise(client, monkeypatch, solution_id: int, doc_kind: str) -> dict[str, Any]:  # noqa: ANN001
    """先用一次「重新审核」把当前版本判成「有问题」——修复只对它有入口。"""
    _fake_service(monkeypatch, PoolLLM(reviewers=[_REVISE_HIGH]))
    _job_id, events, status = _run_job(client, _review_payload(solution_id, doc_kind))
    assert status == "succeeded"
    conclusion = _review_events(events)[0]
    assert conclusion["verdict"] == "revise"
    return conclusion


# ---------------------------------------------------------------------------
# AC1：三份产物的审核结论形状一致，一版一条
# ---------------------------------------------------------------------------


def test_conclusion_has_one_shape_for_all_three_products(client, monkeypatch):
    """三份产物重审读回来的结论**键集完全相同**，且就是 `_CONCLUSION_KEYS`。

    为什么按「键集」而不是「字段值」：形状一致是 AC1 的原话，也是前端只有一套
    `parseReview` / 一个面板的前提。形状一旦分叉，界面就得按产物分叉。
    """
    register(client)
    solution_id = _create_solution(client)
    shapes: dict[str, set[str]] = {}
    issue_shapes: dict[str, set[str]] = {}

    for kind, body, _fixed in _PRODUCT_ROWS:
        _seed_document(client, solution_id, kind, body)
        _fake_service(monkeypatch, PoolLLM(reviewers=[_REVISE_MEDIUM]))
        _job_id, events, status = _run_job(client, _review_payload(solution_id, kind))
        assert status == "succeeded", f"{kind} 重审失败"

        conclusion = _review_events(events)[0]
        shapes[kind] = set(conclusion)
        issue_shapes[kind] = set(conclusion["issues"][0])

    assert set(shapes) == {"prd", "api_docs", "prompts"}
    for kind, shape in shapes.items():
        assert shape == _CONCLUSION_KEYS, f"{kind} 的结论形状与 2026 口径不一致：{shape}"
    assert len({frozenset(shape) for shape in shapes.values()}) == 1, "三份结论形状分叉了"
    assert len({frozenset(shape) for shape in issue_shapes.values()}) == 1, "逐条问题的形状分叉了"


@pytest.mark.parametrize(("doc_kind", "body", "_fixed"), _PRODUCTS)
def test_one_conclusion_per_version_and_rereview_overwrites_it(
    client, monkeypatch, doc_kind, body, _fixed
):
    """重审覆盖**同一条**结论：正文逐字不变、版本数不变，刷新后读到的就是新那条。

    三份产物都参数化：一版一条不是「PRD 特例」，它是三份共同的语义。
    """
    register(client)
    solution_id = _create_solution(client)
    _seed_requirements(client, solution_id)
    _seed_document(client, solution_id, doc_kind, body)

    _fake_service(monkeypatch, PoolLLM(reviewers=[_PASS]))
    _job_id, events, status = _run_job(client, _review_payload(solution_id, doc_kind))
    assert status == "succeeded"
    first = _review_events(events)[0]
    assert first["verdict"] == "pass"
    assert first["rewritten"] is False, "重审只审不改"

    _fake_service(monkeypatch, PoolLLM(reviewers=[_REVISE_MEDIUM]))
    _job_id, events, status = _run_job(client, _review_payload(solution_id, doc_kind))
    assert status == "succeeded"
    second = _review_events(events)[0]
    assert second["verdict"] == "revise"

    doc = _documents(client, solution_id)[doc_kind]
    assert doc["content"] == body, "重审不改正文一个字节"
    assert doc["review"] == second, "重审覆盖同一条结论"

    versions = _versions(client, solution_id, doc_kind)
    assert [item["version_no"] for item in versions] == [1], "重审不追加版本"
    detail = _version_detail(client, solution_id, doc_kind, versions[0]["version_id"])
    assert detail["review"] == second, "结论随那一版正文落库"

    # 「刷新 / 换设备」= 换一个会话重读：落库那条才是唯一来源
    client.cookies.clear()
    login(client)
    assert _documents(client, solution_id)[doc_kind]["review"] == second


# ---------------------------------------------------------------------------
# AC2：只有高严重度触发改写，改写后不重审（三份产物同一条路）
# ---------------------------------------------------------------------------

_GENERATION_DOCS = [
    pytest.param("prd", _PRD_BODY, id="prd"),
    pytest.param("api_docs", _API_DOCS_BODY, id="api_docs"),
    pytest.param("prompts", _PROMPTS_BODY, id="prompts"),
]


def _generate_events(llm, doc_kind: str):  # noqa: ANN001, ANN202
    """按产物驱动各自的整篇生成入口，返回事件列表。"""
    service = DocumentService(llm=llm)

    async def _drain() -> list[Any]:
        if doc_kind == "prd":
            stream = service.generate_prd_from_summary_stream(
                summary={"product_name": "演示产品", "mvp_features": [{"name": "待办清单"}]},
                review_enabled=True,
            )
        elif doc_kind == "api_docs":
            stream = service.generate_api_docs_stream(prd_content=_PRD_BODY, review_enabled=True)
        else:
            stream = service.generate_prompts_stream(
                prd_content=_PRD_BODY, api_docs_content=_API_DOCS_BODY, review_enabled=True
            )
        return [event async for event in stream]

    return asyncio.run(_drain())


@pytest.mark.parametrize(("doc_kind", "_body"), _GENERATION_DOCS)
def test_only_high_severity_triggers_one_rewrite_and_never_rereviews(doc_kind, _body):
    """审核调用恰好一次；high 触发**一次**改写后不再回到审核，medium 一次都不改。

    三份产物共用 `document_review.stream_review_and_rewrite`，所以这条对三份同时成立；
    参数化是为了钉住「退化回某个产物专用链路」这件事（分章时代 PRD 就这么分叉过）。
    """
    high_llm = RoutingLLM(write=_body, review=_REVISE_HIGH, rewrite=_body + "\n\n已按问题修正。")
    high_events = _generate_events(high_llm, doc_kind)
    assert high_llm.review_calls == 1, f"{doc_kind} 的改写之后不该再重审"
    assert high_llm.rewrite_calls == 1, f"{doc_kind} 的 high 问题只该改写一次"

    stages = [str(event.data.get("stage")) for event in high_events if event.type == "stage"]
    assert stages.count("reviewing") == 1, "审核只该亮一次"
    assert stages.count("rewriting") == 1, "改写只该亮一次"
    assert stages.index("reviewing") < stages.index("rewriting")
    assert "reviewing" not in stages[stages.index("rewriting") :], "改写之后不许回头审"

    review = next(event for event in high_events if event.type == "review")
    assert review.data["rewritten"] is True
    parts = [event for event in high_events if event.type == "part"]
    assert len(parts) == 1 and parts[0].data["replace"] is True, "改写稿整段覆盖初稿"

    medium_llm = RoutingLLM(write=_body, review=_REVISE_MEDIUM, rewrite="不该出现的改写稿")
    medium_events = _generate_events(medium_llm, doc_kind)
    assert medium_llm.review_calls == 1
    assert medium_llm.rewrite_calls == 0, "只有 high 才动笔；medium 照实报但不改"
    assert "rewriting" not in [
        str(event.data.get("stage")) for event in medium_events if event.type == "stage"
    ]
    medium_review = next(event for event in medium_events if event.type == "review")
    assert medium_review.data["verdict"] == "revise", "判定照实报，只是不动笔"
    assert medium_review.data["rewritten"] is False


# ---------------------------------------------------------------------------
# AC3：修复一律整篇重写，但仍是**追加新版本**（不是重生成文档）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("doc_kind", "body", "fixed"), _PRODUCTS)
def test_fix_rewrites_whole_and_appends_a_version(client, monkeypatch, doc_kind, body, fixed):
    """修复走唯一写入入口的 `append`：版本 +1、来源记 `review_fix`、旧版原样保留。

    「整篇重写」与「追加版本」必须同时成立：前者是修复的语义（一条结论描述整篇），
    后者是版本链的语义（历史只增不改）。只满足一半的两种写法都真实出现过——
    就地改当前版本会把用户手里那份正文换掉，重生成文档会让正文「没有历史」。
    """
    register(client)
    solution_id = _create_solution(client)
    _seed_requirements(client, solution_id)
    if doc_kind != "prd":
        _seed_document(client, solution_id, "prd", _PRD_BODY)
    if doc_kind == "prompts":
        _seed_document(client, solution_id, "api_docs", _API_DOCS_BODY)
    _seed_document(client, solution_id, doc_kind, body)
    first = _establish_revise(client, monkeypatch, solution_id, doc_kind)

    modes: list[str | None] = []
    real_write = document_store.write_document

    def _spy(*args, **kwargs):  # noqa: ANN002, ANN003
        modes.append(kwargs.get("mode"))
        return real_write(*args, **kwargs)

    monkeypatch.setattr(document_store, "write_document", _spy)

    llm = PoolLLM(writers=[fixed], reviewers=[_PASS])
    _fake_service(monkeypatch, llm)
    _job_id, events, status = _run_job(client, _fix_payload(solution_id, doc_kind))

    assert status == "succeeded"
    assert llm.writer_calls == 1, "修复只重写一次（整篇，不分节）"
    assert llm.review_calls == 1, "修复后只重审一次，不再改写"
    assert "append" in modes, f"修复必须走统一写入入口的 append 模式，实际：{modes}"
    assert "replace" not in modes, "修复不是就地改当前版本"

    parts = [event for event in events if event["type"] == "part"]
    assert len(parts) == 1 and parts[0]["data"]["replace"] is True, "整篇改写稿整段覆盖"

    doc = _documents(client, solution_id)[doc_kind]
    assert doc["content"] == fixed
    assert doc["review"]["verdict"] == "pass"

    versions = _versions(client, solution_id, doc_kind)
    assert [item["version_no"] for item in versions] == [2, 1], "追加一版，旧版仍在"
    newest, oldest = versions
    assert newest["source"] == "review_fix", "新版本来源记修复"
    oldest_detail = _version_detail(client, solution_id, doc_kind, oldest["version_id"])
    assert oldest_detail["content"] == body, "旧正文仍在版本历史里"
    assert oldest_detail["review"] == first, "旧结论留在它描述的那一版上"


# ---------------------------------------------------------------------------
# AC4：重审只更新当前版本结论、不改正文（条件写 + CONTENT_CHANGED）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("doc_kind", "body", "_fixed"), _PRODUCTS)
def test_rereview_keeps_the_old_conclusion_when_the_body_changed(
    client, monkeypatch, doc_kind, body, _fixed
):
    """审到一半正文被改 → 任务失败（`CONTENT_CHANGED`）、原结论与正文都不动。

    重审的正文是**建任务那一刻**的快照，结论要写到「此刻的当前版本」上。
    中间用户手改一次，结论就会落在没审过的正文上——它看起来完全正常，却是错的。
    """
    register(client)
    solution_id = _create_solution(client)
    _seed_requirements(client, solution_id)
    _seed_document(client, solution_id, doc_kind, body)

    _fake_service(monkeypatch, PoolLLM(reviewers=[_PASS]))
    _job_id, _events, status = _run_job(client, _review_payload(solution_id, doc_kind))
    assert status == "succeeded"
    baseline = _documents(client, solution_id)[doc_kind]["review"]
    assert baseline is not None and baseline["verdict"] == "pass"

    # 第二次重审：建任务（快照）之后、收尾之前，用户把正文改了
    _fake_service(monkeypatch, PoolLLM(reviewers=[_REVISE_HIGH], delay=0.6))
    created = client.post("/api/jobs", json=_review_payload(solution_id, doc_kind))
    assert created.status_code == 201, created.text
    job_id = int(created.json()["job_id"])

    edited = body + "\n## 手改追加的一节\n\n这一段没被审过。\n"
    _seed_document(client, solution_id, doc_kind, edited)

    stream = client.get(f"/api/jobs/{job_id}/stream")
    assert stream.status_code == 200, stream.text
    events = _parse_events(stream)
    errors = [event for event in events if event["type"] == "error"]
    assert errors, "正文已变更的失败必须发一条 error 事件"
    assert "正文已变更" in errors[-1]["content"]

    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "failed"
    assert job["error_code"] == "CONTENT_CHANGED"

    doc = _documents(client, solution_id)[doc_kind]
    assert doc["content"] == edited, "手改的正文照旧保留"
    assert doc["review"] == baseline, "结论不写到没审过的正文上，也不覆盖已有结论"
    assert [item["version_no"] for item in _versions(client, solution_id, doc_kind)] == [1]


# ---------------------------------------------------------------------------
# AC5：审核失败不写结论；「没审」与「审失败」靠显式 attempted 标记区分
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("doc_kind", "body", "_fixed"), _PRODUCTS)
def test_failed_rereview_writes_no_conclusion_and_marks_the_failure(
    client, monkeypatch, doc_kind, body, _fixed
):
    """审失败 → 不加版本、不写结论、写失败时间；界面据此显示「审核失败」而不是「未审核」。"""
    register(client)
    solution_id = _create_solution(client)
    _seed_requirements(client, solution_id)
    _seed_document(client, solution_id, doc_kind, body)

    _fake_service(monkeypatch, PoolLLM(reviewers=[ConnectionError("审核上游挂了")]))
    _job_id, events, status = _run_job(client, _review_payload(solution_id, doc_kind))

    assert status == "failed", "重审的唯一产出就是判定，没跑成就是失败"
    assert any(event["type"] == "error" for event in events), "失败要有可读的原因"
    assert _review_events(events) == [], "没审成不该给出一条结论"

    client.cookies.clear()
    login(client)
    doc = _documents(client, solution_id)[doc_kind]
    assert doc["review"] is None, "审核失败不写结论（没有结论 ≠ 通过）"
    assert doc["review_failed_at"], "失败时间必须落库，否则刷新后退化成「未审核」"
    assert doc["content"] == body
    assert [item["version_no"] for item in _versions(client, solution_id, doc_kind)] == [1]

    # 补审一次：拿到结论的同时清掉失败时间（两者描述同一版，不能并存）
    _fake_service(monkeypatch, PoolLLM(reviewers=[_PASS]))
    _job_id, _events, status = _run_job(client, _review_payload(solution_id, doc_kind))
    assert status == "succeeded"
    refreshed = _documents(client, solution_id)[doc_kind]
    assert refreshed["review"]["verdict"] == "pass"
    assert refreshed["review_failed_at"] is None


def test_attempted_marker_separates_not_reviewed_from_failed():
    """「没审」与「审失败」只由 `attempted` 区分，**不判空串**。

    三条路径都是「没有结论」，但只有第二条算失败：
    - 显式关闭审核（快捷流程）：连事件都不发 → 不写失败时间；
    - 试过但没成：`attempted=True` → 收尾写 `review_failed_at`；
    - 没有正文可审：同样没结论，但它**不是失败**，因此 `attempted=False`。
    """
    failed = document_review.review_event("prd", None)
    assert failed.data["verdict"] == "", "没审成不许报成 pass"
    assert failed.data["attempted"] is True, "试过但没成：这是「审失败」"

    not_attempted = document_review.review_event("prd", None, attempted=False)
    assert not_attempted.data["attempted"] is False, "没试过：这是「没审」，不是失败"

    # 两条的判定都是空串——所以收尾**只能**看 attempted，看空串分不开它们
    assert failed.data["verdict"] == not_attempted.data["verdict"] == ""
    assert not_attempted.data["summary"] == "审核未完成"


# ---------------------------------------------------------------------------
# 清理结论：分章遗留形状不许长回来（#94 收口）
# ---------------------------------------------------------------------------


def test_chapter_leftover_fields_are_gone():
    """`failed_sections` / `completed_sections` 已随分章退役，不许再出现在契约里。

    这两个字段在 #92 退役分章之后**恒为空**，却仍在四处流转（结论 / 回执 / 快照 /
    任务行）：读代码的人会以为「哪天会有值」。删掉之后这条用例是唯一的报警点——
    只删实现不删契约，字段会以「读起来像接口」的样子回来。
    """
    assert "failed_sections" not in RunSummary.model_fields, "回执里的 failed_sections 该删了"
    assert "completed_sections" not in GenerationJob.model_fields, "任务行不再有分章清单"

    conclusion = document_review.review_data("prd", verdict="pass", summary="未发现缺陷")
    assert set(conclusion) == _CONCLUSION_KEYS

    event = document_review.review_event("prd", None)
    assert "failed_sections" not in event.data
