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
import re
import string
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import pytest
from conftest import register
from langchain_core.messages import AIMessageChunk, BaseMessage

from backend.services import (
    document_service,
    document_store,
    job_runtime,
    llm_factory,
    prd_section_writer,
    prompts,
    review_defects,
)
from backend.services.document_review import DocumentReviewResult
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


# ---------------------------------------------------------------------------
# 四、类别键名不是软闸：别名归一、旧形状与新输出分开、六类锁定
# ---------------------------------------------------------------------------


def test_category_table_is_locked_to_six_keys_and_fixed_severity():
    """锁六类键名与固定严重度（ADR 0003 决策 2）：改键名或改档位必须红。

    期望值抄自 ADR 0003 的决策表，**不是**从被测常量反算——反算的断言永远为真。
    """
    assert list(review_defects.CATEGORY_SEVERITY.items()) == [
        ("fabrication", "high"),
        ("contradiction", "high"),
        ("structure", "medium"),
        ("vague", "medium"),
        ("unlabeled_inference", "medium"),
        ("missing_pending", "medium"),
    ]
    assert list(review_defects.CATEGORY_LABELS.items()) == [
        ("fabrication", "编造"),
        ("contradiction", "矛盾"),
        ("structure", "结构不符"),
        ("vague", "空泛"),
        ("unlabeled_inference", "推断未标注"),
        ("missing_pending", "清单漏登记"),
    ]


def _prd_header(body: str = "正文内容") -> str:
    """一份最小可审的 PRD 正文（H1 + 第一章），供「需要上游 PRD」的产物准备材料。"""
    heading = prd_section_writer.PRD_SECTIONS[0].heading
    return f"# 演示产品 产品需求文档（PRD）\n\n{heading}\n\n{body}\n"


def test_chinese_category_label_is_normalized_and_still_forces_high(client, monkeypatch):
    """模型把类别写成中文「编造」→ 仍按类别抬成 high 并触发一次改写。

    这是 A1 的核心：类别键名一旦是软闸，「AC4 的抬/压档」与「AC3 的服务端丢弃」
    都能被一个别名绕过。
    """
    register(client)
    solution_id = _create_solution(client)
    _seed_document(client, solution_id, "prd", _prd_header())

    reply = _review_json(
        {
            "where": "## 一、概述",
            "category": "编造",
            "severity": "medium",
            "detail": "凭空补了鉴权方式",
        }
    )
    llm = FakeLLM(
        writers=[_API_DOCS_BODY, _API_DOCS_BODY + "\n## 二、鉴权\n\n已按上游补齐。\n"],
        reviewer=lambda prompt: reply,
    )
    _fake_service(monkeypatch, llm)
    _job_id, events, status = _run_job(
        client, {"kind": "api_docs", "solution_id": solution_id}
    )

    assert status == "completed"
    conclusion = _review_events(events)[0]
    issue = conclusion["issues"][0]
    assert issue["category"] == "fabrication"
    assert issue["severity"] == "high", "中文类别也必须被认出来并按类别抬档"
    assert llm.writer_calls == 2, "认得出类别 = 编造，所以改写一次"


@pytest.mark.parametrize("raw_category", ["missing-pending", "missing pending", "清单漏登记"])
def test_missing_pending_spelling_variants_are_dropped_during_generation(
    client, monkeypatch, raw_category
):
    """换成连字符 / 空格 / 中文标签，生成链路照样丢掉这一条（时点口径不能被绕过）。"""
    register(client)
    solution_id = _create_solution(client)
    _seed_document(client, solution_id, "prd", _prd_header())

    reply = json.dumps(
        {
            "verdict": "revise",
            "issues": [
                {
                    "where": "## 一、概述",
                    "category": raw_category,
                    "severity": "high",
                    "detail": "登录方式该登记未登记",
                }
            ],
            "instruction": "把缺的条目登记进待确认清单",
        },
        ensure_ascii=False,
    )
    llm = FakeLLM(writers=[_API_DOCS_BODY], reviewer=lambda prompt: reply)
    _fake_service(monkeypatch, llm)
    _job_id, events, status = _run_job(
        client, {"kind": "api_docs", "solution_id": solution_id}
    )

    assert status == "completed"
    conclusion = _review_events(events)[0]
    assert conclusion["issues"] == [], "生成过程中报的清单漏登记必须被丢掉"
    assert conclusion["verdict"] == "pass"
    assert conclusion["summary"] != ""
    assert llm.writer_calls == 1, "被丢掉的条目不该触发改写"


def test_unknown_category_in_new_output_is_kept_as_medium_and_never_rewrites(
    client, monkeypatch
):
    """新输出有 `category` 键但归不到六类 → 条目留着、severity 强制 medium、不触发改写。

    理由：认不出的类别按最低可改写档处理，宁可只报不改；绝不采信模型自报的 high。
    """
    register(client)
    solution_id = _create_solution(client)
    _seed_document(client, solution_id, "prd", _prd_header())

    reply = _review_json(
        {
            "where": "## 一、概述",
            "category": "coverage_gap",
            "severity": "high",
            "detail": "少了一节验收标准",
        }
    )
    llm = FakeLLM(writers=[_API_DOCS_BODY], reviewer=lambda prompt: reply)
    _fake_service(monkeypatch, llm)
    _job_id, events, status = _run_job(
        client, {"kind": "api_docs", "solution_id": solution_id}
    )

    assert status == "completed"
    conclusion = _review_events(events)[0]
    issue = conclusion["issues"][0]
    assert issue["category"] is None
    assert issue["severity"] == "medium", "认不出的新输出不采信模型自报的 high"
    assert conclusion["verdict"] == "revise", "条目仍然保留，判定不因它升级"
    assert llm.writer_calls == 1, "medium 只报不改"


def test_legacy_item_with_empty_severity_is_medium_on_both_products(client, monkeypatch):
    """同一份旧形状输出（无类别、无 severity）在两份产物里严重度相同：都是 medium。

    从前接口文档存空串（界面空徽标、排序垫底），PRD 经 `section_issue` 补 medium——
    两条路的归一必须同源，否则「同一份模型输出」在两份产物里读出不同档位。
    """
    legacy = json.dumps(
        {
            "verdict": "revise",
            "issues": [{"where": "## 一、概述", "detail": "凭空补了鉴权方式"}],
            "instruction": "删掉编造的鉴权",
        },
        ensure_ascii=False,
    )

    # ① 接口文档（整篇审核，生成链路）
    register(client)
    solution_id = _create_solution(client)
    _seed_document(client, solution_id, "prd", _prd_header())
    llm = FakeLLM(writers=[_API_DOCS_BODY], reviewer=lambda prompt: legacy)
    _fake_service(monkeypatch, llm)
    _job_id, events, status = _run_job(
        client, {"kind": "api_docs", "solution_id": solution_id}
    )
    assert status == "completed"
    whole_issue = _review_events(events)[0]["issues"][0]
    assert whole_issue["severity"] == "medium", "空 severity 不许原样存成空徽标"

    # ② PRD（逐章重审，汇总链路经 `section_issue`）
    solution_id = _create_solution(client)
    _seed_requirements(client, solution_id)
    _seed_document(client, solution_id, "prd", _prd_header())
    llm = FakeLLM(reviewer=lambda prompt: legacy)
    _fake_service(monkeypatch, llm)
    _job_id, events, status = _run_job(client, _review_payload(solution_id, "prd"))
    assert status == "completed"
    prd_issue = _review_events(events)[0]["issues"][0]
    assert prd_issue["severity"] == whole_issue["severity"], (
        "同一份模型输出在两份产物里严重度必须相同"
    )
    assert prd_issue["severity"] == "medium"


# ---------------------------------------------------------------------------
# 五、第六类判据要含定位；三份产物同一套类别；提示词产物也要守
# ---------------------------------------------------------------------------


def test_sixth_category_rule_demands_the_section_and_the_missing_class(client, monkeypatch):
    """第六类判据必须要求写清「在哪一节、缺的是哪一类」；服务端拿到的条目里带定位。

    假模型只能证明「服务端收得下这个形状」，所以同时断言提示词规则本身要求了它——
    否则模型没有理由给出定位，这条 AC 就只靠夹具撑着。
    """
    register(client)
    solution_id = _create_solution(client)
    _seed_document(client, solution_id, "prd", _prd_header())
    _seed_document(client, solution_id, "api_docs", _API_DOCS_BODY + _INFERRED_WITH_MARK)

    reply = _missing_pending_json(where="## 一、概述", detail="登录方式未登记")
    llm = FakeLLM(reviewer=lambda prompt: reply)
    _fake_service(monkeypatch, llm)
    _job_id, events, status = _run_job(client, _review_payload(solution_id, "api_docs"))

    assert status == "completed"
    prompt = llm.reviewer_prompts[0]
    assert "在哪一节" in prompt and "哪一类" in prompt, "判据要含定位与缺项类别"
    assert "40 字" in prompt, "detail 仍 ≤40 字，允许 where 承担定位"

    issue = _review_events(events)[0]["issues"][0]
    assert issue["where"] == "## 一、概述"
    assert "登录方式" in issue["detail"]


_PROMPTS_BODY = "## 一、角色设定\n\n你是一位资深产品经理。\n"


def test_prompts_rereview_opens_all_six_categories_and_injects_the_list(
    client, monkeypatch
):
    """提示词产物的重审：六类同样生效、清单漏登记可报、清单被注入。"""
    register(client)
    solution_id = _create_solution(client)
    _seed_document(client, solution_id, "prd", _prd_header())
    _seed_document(client, solution_id, "prompts", _PROMPTS_BODY)

    reply = _missing_pending_json(where="## 一、角色设定")
    llm = FakeLLM(reviewer=lambda prompt: reply)
    _fake_service(monkeypatch, llm)
    _job_id, events, status = _run_job(client, _review_payload(solution_id, "prompts"))

    assert status == "completed"
    prompt = llm.reviewer_prompts[0]
    for label in review_defects.CATEGORY_LABELS.values():
        assert label in prompt, f"提示词产物的重审提示词缺了「{label}」这一类"
    assert "本版没有登记任何待确认项" in prompt, "空清单也要显式注入"

    issue = _review_events(events)[0]["issues"][0]
    assert issue["category"] == "missing_pending"
    assert issue["severity"] == "medium"


# ---------------------------------------------------------------------------
# 六、PRD 的汇总结论按类别计数（不再出现「待处理问题」）
# ---------------------------------------------------------------------------


def test_prd_summary_counts_defects_by_category(client, monkeypatch):
    """PRD 的 summary 是「N 章已审，缺陷 M 处（类别计数）」——术语表禁用「问题」于此。"""
    register(client)
    solution_id = _create_solution(client)
    _seed_requirements(client, solution_id)
    first = prd_section_writer.PRD_SECTIONS[0].heading
    second = prd_section_writer.PRD_SECTIONS[1].heading
    _seed_document(
        client,
        solution_id,
        "prd",
        f"# 演示产品 产品需求文档（PRD）\n\n{first}\n\n正文内容\n\n{second}\n\n正文内容\n",
    )

    reply = _review_json(
        {
            "where": "",
            "category": "fabrication",
            "severity": "high",
            "detail": "凭空补了鉴权方式",
        }
    )
    llm = FakeLLM(reviewer=lambda prompt: reply)
    _fake_service(monkeypatch, llm)
    _job_id, events, status = _run_job(client, _review_payload(solution_id, "prd"))

    assert status == "completed"
    summary = _review_events(events)[0]["summary"]
    assert summary == "2 章已审，缺陷 2 处（编造 2）"
    assert "待处理问题" not in summary


# ---------------------------------------------------------------------------
# 七、五类表只有一个生成点（两条产物链都从 `review_defects` 取）
# ---------------------------------------------------------------------------


def _rendered(template: str, **extra: str) -> str:
    """把模板里所有占位符填成可辨认的值，用于断言某一节真的进了最终提示词。"""
    names = {field for _, field, _, _ in string.Formatter().parse(template) if field}
    values = {name: f"<{name}>" for name in names} | extra
    return template.format(**values)


def test_both_review_prompts_share_one_generated_category_table():
    """五类表只许有一份生成点：模板里不得再手写类别标识，两链都从同一个函数取。"""
    table = review_defects.render_category_rules()
    assert table.strip(), "共用的类别表不能是空串"
    assert "fabrication" in table and "unlabeled_inference" in table

    assert table in _rendered(prompts.PRD_SECTION_REVIEW_PROMPT, category_rules=table)
    assert table in _rendered(prompts.DOC_REVIEW_PROMPT, category_rules=table)

    # 模板里再手写一份类别表就会与生成点漂移：这里直接把它排除
    for template in (prompts.PRD_SECTION_REVIEW_PROMPT, prompts.DOC_REVIEW_PROMPT):
        for identifier in review_defects.CATEGORY_SEVERITY:
            assert identifier not in template, (
                f"提示词模板里还写着类别标识 `{identifier}`——类别表必须只有一个生成点"
            )


# ---------------------------------------------------------------------------
# 八、跨端标签对拍（后端为准）
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FRONTEND_REVIEW_TS = _REPO_ROOT / "frontend" / "src" / "utils" / "review.ts"

_LABELS_BLOCK = re.compile(
    r"export const DEFECT_CATEGORY_LABELS[^{]*\{(?P<body>[^}]*)\}"
)
_LABEL_ENTRY = re.compile(
    r"^\s*(?P<key>[a-z_]+)\s*:\s*'(?P<label>[^']*)'", re.MULTILINE
)
_CATEGORIES_BLOCK = re.compile(
    r"export const DEFECT_CATEGORIES = \[(?P<body>[^\]]*)\]"
)
_CATEGORY_ENTRY = re.compile(r"^\s*'(?P<key>[a-z_]+)'", re.MULTILINE)


def test_frontend_defect_labels_match_the_backend_one_to_one():
    """前端类别标签与后端 `CATEGORY_LABELS` 逐项相等，键序也相等（后端为准）。

    跨端的取值域只能有一份：前端漏改一个标签，用户就会在「文档缺陷」面板里读到
    两个名字指同一类，而没有任何一处会报错（仓库对错误码已有同款对拍守卫）。
    """
    if not _FRONTEND_REVIEW_TS.exists():
        pytest.skip("前端源码目录不存在，跳过跨端守卫检查")

    source = _FRONTEND_REVIEW_TS.read_text(encoding="utf-8")

    labels_block = _LABELS_BLOCK.search(source)
    assert labels_block is not None, "没在 review.ts 里找到 DEFECT_CATEGORY_LABELS 常量表"
    entries = [
        (entry["key"], entry["label"])
        for entry in _LABEL_ENTRY.finditer(labels_block.group("body"))
    ]
    assert entries, "常量表解析出 0 个条目——正则需要跟着表结构一起更新"
    assert entries == list(review_defects.CATEGORY_LABELS.items()), (
        "前端类别标签与后端 CATEGORY_LABELS 不一致（键名、文案或键序）"
    )

    categories_block = _CATEGORIES_BLOCK.search(source)
    assert categories_block is not None, "没在 review.ts 里找到 DEFECT_CATEGORIES 常量表"
    categories = [
        entry["key"] for entry in _CATEGORY_ENTRY.finditer(categories_block.group("body"))
    ]
    assert categories == list(review_defects.CATEGORY_SEVERITY), (
        "前端类别顺序与后端 CATEGORY_SEVERITY 的键序不一致"
    )


# ---------------------------------------------------------------------------
# 九、回退三处蔓延（B1 / B2 / B3）
# ---------------------------------------------------------------------------


def test_prd_rewrite_prompt_has_no_fix_only_constraint_but_fix_prompt_keeps_it():
    """「不得为缺失编造」只留修复提示词（ADR 0003 决策 6），不蔓延到生成期改写。"""
    assert "不得为缺失编造" in prompts.DOC_FIX_PROMPT
    assert "不得为缺失编造" not in prompts.PRD_SECTION_REWRITE_PROMPT


def test_rewrite_prompt_gets_plain_severity_not_the_chinese_category_label(
    client, monkeypatch
):
    """改写提示词里的问题行回到 `[severity] where：detail`——类别只留给结论 JSON / 界面。"""
    register(client)
    solution_id = _create_solution(client)
    _seed_document(client, solution_id, "prd", _prd_header())

    reply = _review_json(
        {
            "where": "## 一、概述",
            "category": "fabrication",
            "severity": "high",
            "detail": "凭空补了鉴权方式",
        }
    )
    llm = FakeLLM(writers=[_API_DOCS_BODY, _API_DOCS_BODY], reviewer=lambda prompt: reply)
    _fake_service(monkeypatch, llm)
    _job_id, _events, status = _run_job(
        client, {"kind": "api_docs", "solution_id": solution_id}
    )

    assert status == "completed"
    rewrite_prompt = llm.writer_prompts[1]
    assert "[high] ## 一、概述：" in rewrite_prompt
    assert "编造" not in rewrite_prompt, "中文类别标签不该进改写提示词"
    assert "high·" not in rewrite_prompt


def test_verdict_falls_back_to_pass_only_when_filtering_emptied_the_list():
    """`finalize_verdict` 只为「条目被时点口径丢光」这一件事服务（AC3）。"""
    assert review_defects.finalize_verdict(
        "revise", issues=[], instruction="把缺的条目登记进待确认清单", dropped=1
    ) == ("pass", "")

    # 还有条目 → 判定与指令都不动（哪怕有别的条目被丢掉了）
    kept = [{"severity": "high", "detail": "凭空补了鉴权方式"}]
    assert review_defects.finalize_verdict(
        "revise", issues=kept, instruction="删掉编造的鉴权", dropped=1
    ) == ("revise", "删掉编造的鉴权")

    # 没有丢弃 → 只给了指令的结论照样保留（不顺手清空无关的 instruction）
    assert review_defects.finalize_verdict(
        "revise", issues=[], instruction="按指令逐条修正", dropped=0
    ) == ("revise", "按指令逐条修正")


# ---------------------------------------------------------------------------
# 十、坏味：两个 `issues_text` 薄壳共用同一份实现（C2）
# ---------------------------------------------------------------------------


def test_issues_text_keeps_placeholder_and_clipping_at_both_call_sites():
    """两处薄壳各自只有「占位句」与「是否截断」不同，实现只有一份。"""
    long_detail = "很长" * 40

    whole = DocumentReviewResult(
        verdict="revise",
        issues=[{"severity": "high", "where": "## 一、概述", "detail": long_detail}],
    )
    text = whole.issues_text()
    assert text.startswith("1. [high] ## 一、概述：")
    assert text.endswith("…"), "整篇链路的 detail 仍按 60 字截断（口径不变）"
    assert long_detail not in text

    section = prd_section_writer.SectionReview(
        verdict="revise",
        issues=[{"severity": "low", "detail": long_detail}],
    )
    text = section.issues_text()
    assert long_detail in text, "PRD 章节链路的 detail 不做截断（口径不变）"

    assert DocumentReviewResult(verdict="pass").issues_text() == "（审核未给出逐条问题）"
    assert prd_section_writer.SectionReview(verdict="pass").issues_text() == "（审核未给出具体问题）"


# ---------------------------------------------------------------------------
# 十一、坏味：重审输入不再重扫同一张表（C1）
# ---------------------------------------------------------------------------


def test_review_job_reads_the_document_rows_once(client, monkeypatch):
    """重审的输入解析只读一次文档行：清单就从同一批行里取，不再多扫一张表。"""
    register(client)
    solution_id = _create_solution(client)
    _seed_document(client, solution_id, "prd", _prd_header())
    _seed_document(client, solution_id, "api_docs", _API_DOCS_BODY)

    calls: list[tuple[int, int]] = []
    original = document_store.list_documents

    def counting(owner_id: int, solution_id_arg: int):  # noqa: ANN202
        calls.append((owner_id, solution_id_arg))
        return original(owner_id, solution_id_arg)

    monkeypatch.setattr(document_store, "list_documents", counting)

    llm = FakeLLM(reviewer=lambda prompt: _PASS_JSON)
    _fake_service(monkeypatch, llm)
    _job_id, _events, status = _run_job(client, _review_payload(solution_id, "api_docs"))

    assert status == "completed"
    assert len(calls) == 1, f"重审输入解析读了 {len(calls)} 次文档行（应只读 1 次）"

