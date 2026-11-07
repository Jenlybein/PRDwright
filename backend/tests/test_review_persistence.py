"""审核结论随文档版本保存（票 #52）的契约测试。

守的是**读取面与写入面的同一份事实**：生成收尾时把本次审核结论写进被审的那一版
`document_versions.review`；刷新、换一个会话打开同一方案，读文档列表 / 版本详情
拿到的仍是同一条；**审核没跑成就不写**（没有结论 ≠ 通过）。

所有用例只跨 HTTP 主接缝：

    POST /api/jobs → GET /api/jobs/{id}/stream（订阅到 run_summary）
    → GET /api/solutions/{id}/documents → GET .../{kind}/versions/{vid}

不在用例里构造运行态、不直连仓储断言入库，也不复用 SSE 之外的内部形状。
生成链路一律注入离线假 LLM（零 token），真实 LLM 工厂一被调用就抛。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

import pytest
from conftest import login, register
from langchain_core.messages import AIMessageChunk, BaseMessage

from backend.core import runtime_config
from backend.core.config import get_settings
from backend.services import (
    document_service,
    job_runtime,
    llm_factory,
    prd_section_writer,
)
from backend.services.job_bus import job_bus

# 审核 / 改写人设提示词里的特征词：用它区分三类模型调用（与 `test_document_review.py` 同口径）
_REVIEW_MARKER = "技术文档评审员"
_REWRITE_MARKER = "技术文档撰写者"
_PRD_REVIEW_MARKER = "产品文档评审员"


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
# 假模型：按人设分流（审核吐 JSON、改写吐正文、其余当撰写）
# ---------------------------------------------------------------------------


class RoutingDocLLM:
    """整篇文档链路的三类调用：撰写 / 审核 / 改写，各按系统提示词分流。

    每个池子里的条目是「一次调用的完整回复」；池子用完后复用最后一条。
    条目为异常时在**吐第一个字之前**抛出——那正是「调用失败」的真实形态。
    """

    def __init__(
        self,
        *,
        document: list[str | BaseException],
        review: list[str | BaseException],
        rewrite: list[str | BaseException] | None = None,
    ) -> None:
        self._pools: dict[str, list[str | BaseException]] = {
            "document": list(document),
            "review": list(review),
            "rewrite": list(rewrite or []),
        }
        self._cursors = {"document": 0, "review": 0, "rewrite": 0}

    def _role(self, messages: list[BaseMessage]) -> str:
        system = str(messages[0].content)
        if _REVIEW_MARKER in system:
            return "review"
        if _REWRITE_MARKER in system:
            return "rewrite"
        return "document"

    async def astream(self, messages: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
        role = self._role(messages)
        pool = self._pools[role]
        cursor = self._cursors[role]
        self._cursors[role] = cursor + 1
        if not pool:
            return
        reply = pool[min(cursor, len(pool) - 1)]
        # 每次调用先让出一点时间：真实模型不是瞬时返回的，而这里还要保证
        # 「建任务 → 订阅」这两步之间任务仍在跑，否则本次订阅拿不到 `review` 事件
        # （它按设计不重放），用例也就无从比较「订阅看到的」与「落库那条」。
        await asyncio.sleep(0.05)
        if isinstance(reply, BaseException):
            raise reply
        # 切成两片：顺带覆盖「增量拼接」这条路径
        middle = len(reply) // 2
        for piece in (reply[:middle], reply[middle:]):
            if piece:
                await asyncio.sleep(0)
                yield AIMessageChunk(content=piece)


class RoutingPrdLLM:
    """PRD 三类调用：撰写 / 审核 / 改写，按人设与「审核发现的问题」分流。

    并行链路里章节完成顺序不确定，因此**不能按「第 N 次调用」编排**——
    一律按提示词里的章节标题路由（与 `test_prd_section_parallel.py` 同口径）。
    """

    def __init__(self, *, revise_heading: str | None = None) -> None:
        self._revise_heading = revise_heading
        self.calls = 0

    @staticmethod
    def _heading(text: str) -> str:
        """取**最后一次**出现的章节标题（生成提示词里带着整份模板）。"""
        candidates = [
            (text.rfind(section.heading), section.heading)
            for section in prd_section_writer.PRD_SECTIONS
        ]
        position, heading = max(candidates)
        return heading if position >= 0 else ""

    async def astream(self, messages: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
        self.calls += 1
        await asyncio.sleep(0.005)
        system = str(messages[0].content)
        user = str(messages[-1].content)
        heading = self._heading(user)

        if _PRD_REVIEW_MARKER in system:
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
            else:
                yield AIMessageChunk(content='{"verdict": "pass"}')
            return

        # 改写与撰写同一套人设，只能靠用户消息区分
        if "审核发现的问题" in user:
            yield AIMessageChunk(content=f"\n\n改后稿：{heading}\n\n")
            return

        yield AIMessageChunk(content=f"\n\n{heading}\n\n初稿：{heading}\n\n")


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
    """建一份真实方案并返回 id（落文档要求归属可校验）。"""
    response = client.post(
        "/api/solutions",
        json={"title": "审核结论用例", "mode": "structured", "stage": "review-prd", "data": "{}"},
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _run_job(client, payload: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:  # noqa: ANN001
    """建任务 → 订阅到结束，返回 (job_id, 全部事件)。"""
    created = client.post("/api/jobs", json=payload)
    assert created.status_code == 201, created.text
    job_id = int(created.json()["job_id"])

    stream = client.get(f"/api/jobs/{job_id}/stream")
    assert stream.status_code == 200, stream.text
    events = _events(stream)

    status = client.get(f"/api/jobs/{job_id}").json()["status"]
    assert status == "completed", f"任务没有跑完：{status}，事件：{[item['type'] for item in events]}"
    return job_id, events


def _review_event(events: list[dict[str, Any]]) -> dict[str, Any]:
    """取本次订阅里那条 `review` 结论（缺失说明审核这一环没被触发）。"""
    matches = [event for event in events if event["type"] == "review"]
    assert len(matches) == 1, f"应当恰好一条 review 事件，实际 {len(matches)} 条"
    return matches[0]["data"]


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


_REVISED_DOC = "## 概览\n\n改写后的正文"
_DRAFT_DOC = "## 概览\n\n初稿正文"
_REVISE_JSON = json.dumps(
    {
        "verdict": "revise",
        "issues": [{"severity": "high", "detail": "编造了鉴权方式"}],
        "instruction": "删掉编造的鉴权",
    },
    ensure_ascii=False,
)


def _seed_prd(client, solution_id: int) -> None:  # noqa: ANN001
    """经公开接口把一份 PRD 正文放进方案（生成接口文档的上游由服务端取，见 C-R3）。

    不走请求体传上游：那是 #53 起已经收口的旧路，用例也照新口径准备材料。
    """
    saved = client.put(
        f"/api/solutions/{solution_id}/documents/prd",
        json={"content": "# 演示产品\n\n## 1. 功能需求\n\n- 创建订单\n"},
    )
    assert saved.status_code == 200, saved.text


def _seed_requirements(client, solution_id: int) -> None:  # noqa: ANN001
    """把需求输入写进方案（PRD 的上游同样由服务端取，见 C-R3）。

    不用请求体带需求：那是 #53 起已经收口的旧路。这条也让用例在 #54 删掉
    「请求体兜底」之后仍然成立。
    """
    saved = client.put(
        f"/api/solutions/{solution_id}",
        json={
            "title": "审核结论用例",
            "mode": "structured",
            "stage": "review-prd",
            "state": {
                "quick_product_name": "演示产品",
                "quick_requirements": "做一个演示产品",
            },
        },
    )
    assert saved.status_code == 200, saved.text


def _api_docs_payload(solution_id: int, *, review_enabled: bool | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": "api_docs",
        "solution_id": solution_id,
    }
    if review_enabled is not None:
        payload["review_enabled"] = review_enabled
    return payload


def _use_prd_pipeline(monkeypatch, concurrency: str) -> None:  # noqa: ANN001
    """把 PRD 链路的并发度定死（并让运行时配置快照立刻反映新环境变量）。"""
    monkeypatch.setenv("PRD_PIPELINE_CONCURRENCY", concurrency)
    monkeypatch.setenv("PRD_SECTION_MAX_RETRIES", "0")
    get_settings.cache_clear()
    runtime_config.invalidate()


# ---------------------------------------------------------------------------
# 一、整篇生成的结论随当前版本保存，且与订阅期间看到的一致
# ---------------------------------------------------------------------------


def test_api_docs_conclusion_is_read_back_with_the_current_version(client, monkeypatch):
    """生成完读文档列表：当前版本带结论，字段与订阅期间收到的 `review` 事件一致。

    这里刻意**不读**任何运行态：结论只能是落库那份。`reviewed_at` 也在事件里，
    两处逐字段相等才算「同一条」。
    """
    register(client)
    solution_id = _create_solution(client)
    _seed_prd(client, solution_id)
    _fake_service(
        monkeypatch,
        RoutingDocLLM(
            document=[_DRAFT_DOC],
            review=[_REVISE_JSON],
            rewrite=[_REVISED_DOC],
        ),
    )

    _job_id, events = _run_job(client, _api_docs_payload(solution_id))
    conclusion = _review_event(events)
    assert conclusion["kind"] == "api_docs"
    assert conclusion["verdict"] == "revise"
    assert conclusion["rewritten"] is True
    assert conclusion["reviewed_at"]

    doc = _documents(client, solution_id)["api_docs"]
    assert "改写后的正文" in doc["content"]
    assert doc["review"] == conclusion

    # 版本详情带同一条：它是「哪一份正文被审成什么样」的可查记录
    versions = _versions(client, solution_id, "api_docs")
    assert len(versions) == 1
    detail = _version_detail(client, solution_id, "api_docs", versions[0]["version_id"])
    assert detail["review"] == conclusion


def test_conclusion_is_still_there_in_a_new_session(client, monkeypatch):
    """换一个会话（重新登录）打开同一方案：结论仍在，且不依赖任何一次订阅。"""
    register(client)
    solution_id = _create_solution(client)
    _seed_prd(client, solution_id)
    _fake_service(
        monkeypatch,
        RoutingDocLLM(
            document=[_DRAFT_DOC],
            review=[json.dumps({"verdict": "pass", "issues": []})],
        ),
    )
    _run_job(client, _api_docs_payload(solution_id))
    first = _documents(client, solution_id)["api_docs"]["review"]
    assert first["verdict"] == "pass"

    client.cookies.clear()
    login(client)
    again = _documents(client, solution_id)["api_docs"]["review"]

    assert again == first


def test_failed_review_leaves_the_version_unreviewed(client, monkeypatch):
    """审核调用挂掉：正文照旧落库，但**没有结论**（不是「通过」）。

    订阅期间那条 `review` 事件仍要说明「没审成」（判定为空串），界面据此标
    「审核失败」；落库那一列必须为空，读出来是「未审核」。
    """
    register(client)
    solution_id = _create_solution(client)
    _seed_prd(client, solution_id)
    _fake_service(
        monkeypatch,
        RoutingDocLLM(
            document=[_DRAFT_DOC],
            review=[ConnectionError("审核上游挂了")],
        ),
    )

    _job_id, events = _run_job(client, _api_docs_payload(solution_id))
    conclusion = _review_event(events)
    assert conclusion["verdict"] == "", "审核失败不能报成通过"
    assert conclusion["issues"] == []

    doc = _documents(client, solution_id)["api_docs"]
    assert "初稿正文" in doc["content"]
    assert doc["review"] is None

    versions = _versions(client, solution_id, "api_docs")
    assert len(versions) == 1
    detail = _version_detail(client, solution_id, "api_docs", versions[0]["version_id"])
    assert detail["review"] is None


def test_old_versions_without_a_review_read_as_unreviewed(client, monkeypatch):
    """旧版本（当时没审）读作「未审核」，当前版本仍是它自己那条结论。"""
    register(client)
    solution_id = _create_solution(client)
    _seed_prd(client, solution_id)

    # 第一版：审核关闭（快捷流程口径），因此没有任何结论
    _fake_service(
        monkeypatch,
        RoutingDocLLM(document=[_DRAFT_DOC], review=[]),
    )
    _run_job(client, _api_docs_payload(solution_id, review_enabled=False))

    # 第二版：正常审核并给出结论
    second_conclusion = json.dumps({"verdict": "pass", "issues": []})
    _fake_service(
        monkeypatch,
        RoutingDocLLM(document=[_DRAFT_DOC], review=[second_conclusion]),
    )
    _run_job(client, _api_docs_payload(solution_id))

    versions = _versions(client, solution_id, "api_docs")
    assert [item["version_no"] for item in versions] == [2, 1]
    old = next(item for item in versions if item["version_no"] == 1)
    current = next(item for item in versions if item["version_no"] == 2)

    assert _version_detail(client, solution_id, "api_docs", old["version_id"])["review"] is None
    current_review = _version_detail(
        client, solution_id, "api_docs", current["version_id"]
    )["review"]
    assert current_review["verdict"] == "pass"
    assert _documents(client, solution_id)["api_docs"]["review"] == current_review


# ---------------------------------------------------------------------------
# 二、PRD 的汇总结论：串行与并行两条链路同形，`rewritten` 如实汇总
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("concurrency", ["1", "3"])
@pytest.mark.parametrize("revise", [True, False])
def test_prd_conclusion_is_aggregated_in_both_links(
    client, monkeypatch, concurrency, revise
):  # noqa: ANN001
    """并发度 1（串行回滚路径）与 >1（并行）都得到**一条**汇总结论。

    `rewritten` 必须如实反映「这次到底改没改」，不能是恒假——否则「PRD 能不能修复」
    会随并发度配置漂。结论与当前版本一起落库，读文档列表即可拿到。
    """
    _use_prd_pipeline(monkeypatch, concurrency)
    register(client)
    solution_id = _create_solution(client)

    target = prd_section_writer.PRD_SECTIONS[0]
    _seed_requirements(client, solution_id)
    _fake_service(
        monkeypatch,
        RoutingPrdLLM(revise_heading=target.heading if revise else None),
    )

    _job_id, events = _run_job(
        client,
        {
            "kind": "prd",
            "solution_id": solution_id,
        },
    )

    conclusion = _review_event(events)
    assert conclusion["kind"] == "prd"
    assert conclusion["verdict"] == ("revise" if revise else "pass")
    assert conclusion["rewritten"] is revise
    assert bool(conclusion["issues"]) is revise
    assert conclusion["failed_sections"] == []
    if revise:
        assert conclusion["issues"][0]["severity"] == "high"
        assert conclusion["issues"][0]["section_id"] == target.id

    doc = _documents(client, solution_id)["prd"]
    assert doc["review"] == conclusion

    versions = _versions(client, solution_id, "prd")
    detail = _version_detail(client, solution_id, "prd", versions[0]["version_id"])
    assert detail["review"] == conclusion
