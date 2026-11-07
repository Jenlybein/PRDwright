"""审核只报缺陷（ADR 0003 / 票 #63）：类别、固定严重度、两个时点、清单注入。

守的是「审核与待确认清单不得互相代答」这条分工：

- **两个时点两套口径**：生成过程中只判前五类，提示词里既没有「清单漏登记」也没有清单
  （那时清单还没抽出来）；**重审与修复后的重审**才判第六类，并把当前版本的清单整份注入。
- **类别决定严重度**：编造 / 矛盾 = high，其余 = medium；模型自报的 severity 一律按类别
  覆写（服务端强制，不信模型）。
- **旧结论不追溯**：没有类别的条目读出来 `category=None`（界面归「未分类」）。
- **修复不得为缺失编造**：修复提示词写死这条硬约束，缺处仍是「待确认」。

所有用例只跨 HTTP 主接缝：

    PUT  /api/solutions/{id}/documents/{kind}        （准备「当前版本」正文）
    POST /api/jobs（kind=api_docs / review / review_fix）→ GET /api/jobs/{id}/stream
    GET  /api/solutions/{id}/documents

待确认清单**只能由生成产出**（用户不编辑它），所以夹具走一次真实的生成任务来落清单，
不直连 `document_store`。LLM 一律注入离线假模型（真实工厂一被调用就抛）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Callable

import pytest
from conftest import register
from langchain_core.messages import AIMessageChunk, BaseMessage

from backend.services import document_service, job_runtime, llm_factory, prd_section_writer
from backend.services.job_bus import job_bus

# 两套审核人设的系统提示词特征词（与 `test_review_fix` / `test_review_job` 同口径）
_DOC_REVIEW_MARKER = "技术文档评审员"
_PRD_REVIEW_MARKER = "产品文档评审员"

_PASS_JSON = json.dumps({"verdict": "pass", "issues": []})


def _missing_pending_json(where: str = "## 一、概述", detail: str = "登录方式该登记未登记") -> str:
    return json.dumps(
        {
            "verdict": "revise",
            "issues": [
                {
                    "where": where,
                    "category": "missing_pending",
                    "severity": "high",
                    "detail": detail,
                }
            ],
            "instruction": "把缺的条目登记进待确认清单",
        },
        ensure_ascii=False,
    )


def _pending_block(question: str = "确认登录方式", importance: str = "P1") -> str:
    """待确认清单标记块（形状与 `pending_items.render_spec` 一致）。"""
    return (
        "<!-- PENDING:BEGIN -->\n"
        + json.dumps(
            [
                {
                    "question": question,
                    "sections": ["1"],
                    "decider": "产品负责人",
                    "importance": importance,
                }
            ],
            ensure_ascii=False,
        )
        + "\n<!-- PENDING:END -->"
    )


# ---------------------------------------------------------------------------
# 夹具：不触网、任务状态互相隔离
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_real_llm(monkeypatch):
    """真实 LLM 工厂一被调用就抛，杜绝任何测试触网（零 LLM Key）。"""

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("测试不应触网调用真实 LLM")

    monkeypatch.setattr(llm_factory, "get_llm", _boom)
    monkeypatch.setattr(document_service, "get_llm", _boom)
    monkeypatch.setattr(prd_section_writer, "get_llm", _boom)


@pytest.fixture(autouse=True)
def clean_job_state():
    job_runtime.reset_for_tests()
    job_bus.reset_for_tests()
    yield
    job_runtime.reset_for_tests()
    job_bus.reset_for_tests()


class FakeLLM:
    """按系统提示词分流的假模型：审核人设走 `reviewer`，其余（撰写 / 重写）走 `writers`。

    `reviewer` 是一个**按提示词原文作答**的函数——它的用途正是「模型手里有没有那个事实」：
    清单注入没注入，直接体现在它给出的判定上。所有收到的用户消息都留档，供「提示词里
    有没有出现某一类 / 某一份清单」这类断言使用。
    """

    def __init__(
        self,
        *,
        reviewer: Callable[[str], str] | None = None,
        writers: list[str] | None = None,
    ) -> None:
        self._reviewer = reviewer or (lambda _prompt: _PASS_JSON)
        self._writers = list(writers or ["# 文档\n\n正文内容"])
        self.writer_calls = 0
        self.reviewer_calls = 0
        self.writer_prompts: list[str] = []
        self.reviewer_prompts: list[str] = []

    async def astream(self, messages: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
        system = str(messages[0].content)
        prompt = str(messages[-1].content)
        reviewing = _DOC_REVIEW_MARKER in system or _PRD_REVIEW_MARKER in system
        if reviewing:
            self.reviewer_calls += 1
            self.reviewer_prompts.append(prompt)
            reply = self._reviewer(prompt)
        else:
            self.writer_calls += 1
            self.writer_prompts.append(prompt)
            reply = self._writers[min(self.writer_calls - 1, len(self._writers) - 1)]

        # 让出一点真实时间：「建任务 → 订阅」之间任务必须仍在跑，订阅才拿得到 `review` 事件
        await asyncio.sleep(0.15)
        middle = len(reply) // 2
        for piece in (reply[:middle], reply[middle:]):
            if piece:
                await asyncio.sleep(0)
                yield AIMessageChunk(content=piece)


# ---------------------------------------------------------------------------
# 主接缝小工具
# ---------------------------------------------------------------------------


def _events(response) -> list[dict[str, Any]]:  # noqa: ANN001 - TestClient 响应
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
        json={"title": "审核只报缺陷用例", "mode": "structured", "stage": "review-prd", "data": "{}"},
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
    events = _events(stream)

    status = client.get(f"/api/jobs/{job_id}").json()["status"]
    return job_id, events, status


def _review_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event["data"] for event in events if event["type"] == "review"]


def _documents(client, solution_id: int) -> dict[str, dict[str, Any]]:  # noqa: ANN001
    response = client.get(f"/api/solutions/{solution_id}/documents")
    assert response.status_code == 200, response.text
    return {item["kind"]: item for item in response.json()["documents"]}


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
            "title": "审核只报缺陷用例",
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


_API_DOCS_BODY = "# 接口文档\n\n## 一、概述\n\n统一响应结构：`{code, data, message}`\n"

# 正文里的一条推断，已按「推断未标注」的口径标了「（建议）」并给了一句依据——
# 它对应的待确认项登记在清单里（`_pending_block(question="确认登录方式")`）。
_INFERRED_WITH_MARK = (
    "\n鉴权方式（建议）：沿用上游提到的登录体系，请求头带 `Authorization`"
    "（依据：上游只写了「沿用现有登录」，没给具体方案）。\n"
)


def _generate_api_docs_with_pending(client, monkeypatch, solution_id: int) -> None:  # noqa: ANN001
    """跑一次真实的接口文档生成任务，把待确认清单落进当前版本（清单只能由生成产出）。"""
    llm = FakeLLM(writers=[_API_DOCS_BODY + _INFERRED_WITH_MARK + "\n" + _pending_block()])
    _fake_service(monkeypatch, llm)
    _job_id, _events, status = _run_job(
        client, {"kind": "api_docs", "solution_id": solution_id}
    )
    assert status == "completed"
    assert _documents(client, solution_id)["api_docs"]["pending_items"], (
        "夹具没有把待确认清单落进当前版本"
    )


# ---------------------------------------------------------------------------
# 一、两个时点：重审带清单，生成不带
# ---------------------------------------------------------------------------


def test_rereview_sees_the_pending_list_so_a_registered_item_is_not_a_defect(
    client, monkeypatch
):
    """正文里的推断已标「（建议）」、清单也登记了 → 重审**不报**清单漏登记。

    判定落在结论上：假模型只在「提示词里看得到那条清单项」时判通过。因此
    `verdict=pass` 就是「当前版本的清单真的进了重审提示词」的外部证据。
    """
    register(client)
    solution_id = _create_solution(client)
    _seed_document(client, solution_id, "prd", "# 演示产品 产品需求文档（PRD）\n\n## 一、概述\n\n正文\n")
    _generate_api_docs_with_pending(client, monkeypatch, solution_id)

    def reviewer(prompt: str) -> str:
        return _PASS_JSON if "确认登录方式" in prompt else _missing_pending_json()

    llm = FakeLLM(reviewer=reviewer)
    _fake_service(monkeypatch, llm)
    _job_id, events, status = _run_job(client, _review_payload(solution_id, "api_docs"))

    assert status == "completed"
    conclusion = _review_events(events)[0]
    assert conclusion["verdict"] == "pass", "清单里已经登记过的项不该再被报成缺陷"
    assert conclusion["issues"] == []
    # 条目内容与重要性等级都要在（等级是「先问哪一批」的依据）
    assert "[P1]" in llm.reviewer_prompts[0]


def test_rereview_reports_missing_pending_with_where_and_the_missing_entry(
    client, monkeypatch
):
    """正文里有该登记的推断、清单里没有 → 重审报「清单漏登记」（带章节与缺的意思）。"""
    register(client)
    solution_id = _create_solution(client)
    _seed_document(client, solution_id, "prd", "# 演示产品 产品需求文档（PRD）\n\n## 一、概述\n\n正文\n")
    # 同一份正文：有推断、已标「（建议）」，但它没有生成过，当前版本的清单是空的
    _seed_document(client, solution_id, "api_docs", _API_DOCS_BODY + _INFERRED_WITH_MARK)

    llm = FakeLLM(reviewer=lambda prompt: _missing_pending_json())
    _fake_service(monkeypatch, llm)
    _job_id, events, status = _run_job(client, _review_payload(solution_id, "api_docs"))

    assert status == "completed"
    # 重审确实读到了清单（空清单也要显式说明），因此「漏登记」判得出来
    assert "本版没有登记任何待确认项" in llm.reviewer_prompts[0]
    conclusion = _review_events(events)[0]
    assert conclusion["verdict"] == "revise"
    issue = conclusion["issues"][0]
    assert issue["category"] == "missing_pending"
    # 服务端按类别定严重度：漏登记是 medium，模型自报的 high 不算数
    assert issue["severity"] == "medium"
    assert issue["where"] == "## 一、概述"
    assert "登录方式" in issue["detail"]


def test_generation_never_judges_missing_pending(client, monkeypatch):
    """生成过程中（清单尚未抽出）：提示词里没有这一类的判据，也没有清单。

    即便模型自作主张报了一条 `missing_pending`，服务端也会把它丢掉——「只允许前五类」
    是服务端强制的口径，不是提示词里的一句请求。
    """
    register(client)
    solution_id = _create_solution(client)
    _seed_document(client, solution_id, "prd", "# 演示产品 产品需求文档（PRD）\n\n## 一、概述\n\n正文\n")

    # 与重审夹具同一份正文（推断已标「（建议）」），只是这一次它刚被生成出来
    llm = FakeLLM(
        writers=[_API_DOCS_BODY + _INFERRED_WITH_MARK],
        reviewer=lambda prompt: _missing_pending_json(),
    )
    _fake_service(monkeypatch, llm)
    _job_id, events, status = _run_job(
        client, {"kind": "api_docs", "solution_id": solution_id}
    )

    assert status == "completed"
    assert llm.reviewer_prompts, "生成链路应当审了一次"
    generation_prompt = llm.reviewer_prompts[0]
    assert "清单漏登记" not in generation_prompt, "生成时不该出现第六类的判据"
    assert "当前版本的待确认清单" not in generation_prompt, "生成时不该注入清单"
    assert "确认登录方式" not in generation_prompt

    conclusion = _review_events(events)[0]
    assert conclusion["issues"] == [], "生成时报的清单漏登记必须被服务端丢掉"
    assert conclusion["verdict"] == "pass"
    assert llm.writer_calls == 1, "被丢掉的条目不该触发改写"


# ---------------------------------------------------------------------------
# 二、修复：不得为缺失编造
# ---------------------------------------------------------------------------

_FABRICATION_REVISE_JSON = json.dumps(
    {
        "verdict": "revise",
        "issues": [
            {
                "where": "## 一、概述",
                "category": "fabrication",
                "severity": "high",
                "detail": "编造了鉴权方式",
            }
        ],
        "instruction": "删掉编造的鉴权",
    },
    ensure_ascii=False,
)

# 修复稿里「缺」处仍写着「待确认」，并把那一项登记进了清单
_FIXED_API_DOCS = (
    "# 接口文档\n\n## 一、概述\n\n统一响应结构：`{code, data, message}`\n\n"
    "## 二、鉴权\n\n鉴权方式：待确认\n\n"
    + _pending_block(question="确认鉴权方式", importance="P0")
)


def test_fix_prompt_forbids_inventing_and_keeps_the_hole_open(client, monkeypatch):
    """修复提示词写死「不得为缺失编造」；修复后的「缺」处仍是待确认、清单照登记。

    为什么这条必须守：修复会把整篇重写一遍。若它用「像样的默认值」把缺的地方填上，
    用户得到的是一份**看起来更完整、但事实是编的**文档——比留着「待确认」糟得多
    （ADR 0003 决策 6）。
    """
    register(client)
    solution_id = _create_solution(client)
    _seed_document(client, solution_id, "prd", "# 演示产品 产品需求文档（PRD）\n\n## 一、概述\n\n正文\n")
    _seed_document(client, solution_id, "api_docs", _API_DOCS_BODY)

    # 先把它判成「有问题」：修复的唯一起点是那条结论
    _fake_service(monkeypatch, FakeLLM(reviewer=lambda prompt: _FABRICATION_REVISE_JSON))
    _job_id, events, status = _run_job(client, _review_payload(solution_id, "api_docs"))
    assert status == "completed"
    assert _review_events(events)[0]["verdict"] == "revise"

    llm = FakeLLM(writers=[_FIXED_API_DOCS], reviewer=lambda prompt: _PASS_JSON)
    _fake_service(monkeypatch, llm)
    _job_id, _events, status = _run_job(
        client, {"kind": "review_fix", "solution_id": solution_id, "doc_kind": "api_docs"}
    )
    assert status == "completed"
    assert llm.writer_calls == 1

    prompt = llm.writer_prompts[0]
    assert "不得为缺失编造" in prompt, "修复提示词缺了那条硬约束"
    assert "待确认" in prompt, "约束要写清缺处的落点（写「待确认」并登记清单）"

    doc = _documents(client, solution_id)["api_docs"]
    assert "待确认" in doc["content"], "「缺」处不许被填成一个像样的默认值"
    assert [item["question"] for item in doc["pending_items"]] == ["确认鉴权方式"]

    # 修复后的重审是「六类全开」的那个时点：清单已注入，而漏登记仍可报
    assert "确认鉴权方式" in llm.reviewer_prompts[-1]


# ---------------------------------------------------------------------------
# 三、类别定严重度：编造/矛盾 = 高，其余 = 中；中档不触发自动改写
# ---------------------------------------------------------------------------


def _review_json(*issues: dict[str, Any], instruction: str = "按问题清单修正") -> str:
    return json.dumps(
        {"verdict": "revise", "issues": list(issues), "instruction": instruction},
        ensure_ascii=False,
    )


def test_medium_category_is_forced_to_medium_and_never_triggers_rewrite(
    client, monkeypatch
):
    """结构不符被模型标成 high 也按类别改回 medium；中档只报不改（改写门槛不变）。"""
    register(client)
    solution_id = _create_solution(client)
    _seed_document(client, solution_id, "prd", "# 演示产品 产品需求文档（PRD）\n\n## 一、概述\n\n正文\n")

    reply = _review_json(
        {
            "where": "## 一、概述",
            "category": "structure",
            "severity": "high",
            "detail": "表格列与规范不一致",
        }
    )
    llm = FakeLLM(writers=[_API_DOCS_BODY, "# 不该出现的改写稿"], reviewer=lambda prompt: reply)
    _fake_service(monkeypatch, llm)
    _job_id, events, status = _run_job(
        client, {"kind": "api_docs", "solution_id": solution_id}
    )

    assert status == "completed"
    conclusion = _review_events(events)[0]
    issue = conclusion["issues"][0]
    assert issue["category"] == "structure"
    assert issue["severity"] == "medium", "严重度由类别决定，模型写的 high 不算数"
    assert llm.writer_calls == 1, "中档只报不改——生成链路的改写门槛不变"


def test_fabrication_is_forced_to_high_and_triggers_one_rewrite(client, monkeypatch):
    """编造被模型标成 low 也按类别抬回 high，并触发一次改写。"""
    register(client)
    solution_id = _create_solution(client)
    _seed_document(client, solution_id, "prd", "# 演示产品 产品需求文档（PRD）\n\n## 一、概述\n\n正文\n")

    reply = _review_json(
        {
            "where": "## 一、概述",
            "category": "fabrication",
            "severity": "low",
            "detail": "编造了鉴权方式",
        }
    )
    rewritten = _API_DOCS_BODY + "\n## 二、鉴权\n\n已按上游补齐。\n"
    llm = FakeLLM(writers=[_API_DOCS_BODY, rewritten], reviewer=lambda prompt: reply)
    _fake_service(monkeypatch, llm)
    _job_id, events, status = _run_job(
        client, {"kind": "api_docs", "solution_id": solution_id}
    )

    assert status == "completed"
    conclusion = _review_events(events)[0]
    assert conclusion["issues"][0]["severity"] == "high"
    assert llm.writer_calls == 2, "编造是 high，改写一次"


def test_legacy_conclusion_without_a_category_reads_back_as_uncategorized(
    client, monkeypatch
):
    """旧结论（没有类别）读出来是 `category=None`：不推断、不重分类、不追溯。"""
    register(client)
    solution_id = _create_solution(client)
    _seed_document(client, solution_id, "api_docs", _API_DOCS_BODY)

    # 旧口径的审核输出：只有 where / severity / detail
    legacy = _review_json(
        {
            "where": "## 一、概述",
            "severity": "high",
            "detail": "编造了鉴权方式",
        }
    )
    llm = FakeLLM(reviewer=lambda prompt: legacy)
    _fake_service(monkeypatch, llm)
    _job_id, _events, status = _run_job(client, _review_payload(solution_id, "api_docs"))
    assert status == "completed"

    doc = _documents(client, solution_id)["api_docs"]
    issue = doc["review"]["issues"][0]
    assert issue["category"] is None, "没有类别就是没有类别，不替历史猜一个"
    assert issue["severity"] == "high", "旧形状的严重度照旧（不按类别覆写）"


def test_prd_rereview_also_opens_the_sixth_category_with_an_empty_list(
    client, monkeypatch
):
    """PRD 的逐章重审与整篇重审同一套类别：清单为空时照样允许报「清单漏登记」。

    三份产物必须用同一个类别集，否则「同一份判断在 PRD 侧判 medium、在接口文档侧
    判 high」这种分叉会立刻出现（ADR 0003）。
    """
    register(client)
    solution_id = _create_solution(client)
    _seed_requirements(client, solution_id)
    heading = prd_section_writer.PRD_SECTIONS[0].heading
    _seed_document(
        client, solution_id, "prd", f"# 演示产品 产品需求文档（PRD）\n\n{heading}\n\n正文内容\n"
    )

    llm = FakeLLM(reviewer=lambda prompt: _missing_pending_json(where=heading))
    _fake_service(monkeypatch, llm)
    _job_id, events, status = _run_job(client, _review_payload(solution_id, "prd"))

    assert status == "completed"
    prompt = llm.reviewer_prompts[0]
    assert "清单漏登记" in prompt, "重审是六类全开的那个时点"
    assert "本版没有登记任何待确认项" in prompt, "空清单也要显式说明"

    conclusion = _review_events(events)[0]
    issue = conclusion["issues"][0]
    assert issue["category"] == "missing_pending"
    assert issue["severity"] == "medium"
    # 汇总结论按章定位，`section_title` 是章节名（与生成链路的汇总形状同一份）
    assert issue["section_title"] == prd_section_writer.PRD_SECTIONS[0].title

