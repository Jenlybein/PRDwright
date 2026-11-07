"""按审核结论修复（`review_fix`，票 #56）的契约测试。

守的是审阅页「修复」按钮背后的那条事实（SPEC #51 → 票 #56）：

- 修复按**当前版本的审核结论**重写正文，**追加新版本**（正文与结论同批写入，
  版本来源记为修复），旧正文仍在版本历史里（C-R2）；
- 同一任务内**只审一次、不再改写**：点一次修复，正文只变一次；
- 上下文窗口由目标产物决定，且是**全量注入**（不是审核侧那份骨架）：
  接口文档看 PRD 当前版本全文、提示词看 PRD + 接口文档全文、PRD 看需求输入与两条澄清线程；
- 改写失败 → 正文与版本都不动；改写成功但复审失败 → 正文照落库、结论留空（C-R1）。

所有用例只跨 HTTP 主接缝：

    PUT  /api/solutions/{id}/documents/{kind}        （准备「当前版本」正文）
    POST /api/jobs（kind=review / review_fix）→ GET /api/jobs/{id}/stream（订阅到结束）
    GET  /api/solutions/{id}/documents → GET .../{kind}/versions → .../versions/{vid}

用例里不构造运行态、不直连仓储断言入库；LLM 一律注入离线假模型（真实工厂一被调用就抛）。
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

# 两类调用靠系统提示词区分：审核人设提示词里的特征词、与撰写人设（重写）的。
# 口径与 `test_review_job.py` / `test_review_persistence.py` 一致。
_DOC_REVIEW_MARKER = "技术文档评审员"
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
# 假模型：写作池 / 审核池分开
# ---------------------------------------------------------------------------


class FakeLLM:
    """按系统提示词分流：审核人设 → 审核池，其余（生成 / 重写）→ 写作池。

    两个池各自按调用次序取条目（用完复用最后一条），条目是异常时在**吐第一个字之前**
    抛出——那正是「调用失败」的真实形态。写作池的提示词正文留档，供「上下文窗口」
    类断言检查（修复必须看得见上游全文）。
    """

    def __init__(
        self,
        *,
        writers: list[str | BaseException] | None = None,
        reviewers: list[str | BaseException] | None = None,
    ) -> None:
        self._writers = list(writers or [])
        self._reviewers = list(reviewers or [])
        self.writer_calls = 0
        self.reviewer_calls = 0
        self.writer_prompts: list[str] = []

    @staticmethod
    def _pick(pool: list[str | BaseException], calls: int) -> str | BaseException:
        assert pool, "假模型没有配置可用的回复"
        return pool[min(calls - 1, len(pool) - 1)]

    async def astream(self, messages: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
        system = str(messages[0].content)
        reviewing = _DOC_REVIEW_MARKER in system or _PRD_REVIEW_MARKER in system
        if reviewing:
            self.reviewer_calls += 1
            reply = self._pick(self._reviewers, self.reviewer_calls)
        else:
            self.writer_calls += 1
            self.writer_prompts.append(str(messages[-1].content))
            reply = self._pick(self._writers, self.writer_calls)

        # 让出一点真实时间：「建任务 → 订阅」之间任务必须仍在跑，
        # 订阅才拿得到 `review` 事件（它按设计不重放，见 `test_review_job` 同款说明）。
        await asyncio.sleep(0.15)
        if isinstance(reply, BaseException):
            raise reply
        middle = len(reply) // 2
        for piece in (reply[:middle], reply[middle:]):
            if piece:
                await asyncio.sleep(0)
                yield AIMessageChunk(content=piece)


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
    response = client.post(
        "/api/solutions",
        json={"title": "按结论修复用例", "mode": "structured", "stage": "review-prd", "data": "{}"},
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
    monkeypatch.setattr(
        document_service,
        "get_service",
        lambda: document_service.DocumentService(llm=llm),
    )


def _seed_document(client, solution_id: int, kind: str, content: str) -> None:  # noqa: ANN001
    """经公开接口准备一份「当前版本」正文（修复的输入只从库里取，见 C-R3）。"""
    saved = client.put(
        f"/api/solutions/{solution_id}/documents/{kind}",
        json={"content": content},
    )
    assert saved.status_code == 200, saved.text


def _seed_requirements(client, solution_id: int, *, product_name: str, requirements: str) -> None:  # noqa: ANN001
    """把需求输入写进方案（PRD 修复的上游同样由服务端取，见 C-R3）。"""
    saved = client.put(
        f"/api/solutions/{solution_id}",
        json={
            "title": "按结论修复用例",
            "mode": "structured",
            "stage": "review-prd",
            "state": {
                "quick_product_name": product_name,
                "quick_requirements": requirements,
            },
        },
    )
    assert saved.status_code == 200, saved.text


def _seed_turn(solution_id: int, stage: str, content: str) -> None:
    """在某条澄清线程里落一条用户轮次（与 `test_job_server_inputs` 同款准备材料）。

    生产里这条轮次由对话任务写入同一张表；这里只是**准备上游材料**，
    断言仍然只走 HTTP 主链路。用例里注册的用户都是 alice。
    """
    user = users.get_by_username("alice")
    assert user is not None
    with session_scope() as session:
        ChatTurnRepository(session).create(
            int(user["id"]),
            solution_id,
            stage=stage,
            role="user",
            content=content,
        )


def _review_payload(solution_id: int, doc_kind: str) -> dict[str, Any]:
    return {"kind": "review", "solution_id": solution_id, "doc_kind": doc_kind}


def _fix_payload(solution_id: int, doc_kind: str) -> dict[str, Any]:
    return {"kind": "review_fix", "solution_id": solution_id, "doc_kind": doc_kind}


def _establish_revise(client, monkeypatch, solution_id: int, doc_kind: str) -> dict[str, Any]:
    """先用一次「重新审核」把当前版本判成「有问题」——修复只对它有入口。"""
    _fake_service(monkeypatch, FakeLLM(reviewers=[_REVISE_JSON]))
    _job_id, events, status = _run_job(client, _review_payload(solution_id, doc_kind))
    assert status == "completed"
    conclusion = _review_events(events)[0]
    assert conclusion["verdict"] == "revise"
    return conclusion


# ---------------------------------------------------------------------------
# 夹具数据
# ---------------------------------------------------------------------------

_PRD_BODY = (
    "# 演示产品 产品需求文档（PRD）\n\n"
    "## 一、产品概述\n\n1.1 产品名称：演示产品\n\n"
    "## 二、页面与交互\n\n页面清单见下。\n"
)
_API_DOCS_BODY = "# 接口文档\n\n## 一、概述\n\n统一响应结构：`{code, data, message}`\n"
_PROMPTS_BODY = "# 提示词套件\n\n## 一、角色设定\n\n你是一位资深产品经理。\n"

# 修复稿：正文落库时会剥掉尾随空白（`pending_items.extract`），因此期望值不带结尾换行。
_FIXED_API_DOCS = (
    "# 接口文档\n\n## 一、概述\n\n统一响应结构：`{code, data, message}`\n\n"
    "## 二、鉴权\n\n已按需求补齐鉴权说明。"
)
_FIXED_PROMPTS = "# 提示词套件\n\n## 一、角色设定\n\n你是一位资深产品经理，按 PRD 的接口约定回答。"
_FIXED_PRD = (
    "# 演示产品 产品需求文档（PRD）\n\n"
    "## 一、产品概述\n\n1.1 产品名称：演示产品\n\n"
    "## 二、页面与交互\n\n页面清单与跳转规则见下（已补齐）。"
)

_PASS_JSON = json.dumps({"verdict": "pass", "issues": []})
_REVISE_JSON = json.dumps(
    {
        "verdict": "revise",
        "issues": [{"where": "## 一、概述", "severity": "high", "detail": "编造了鉴权方式"}],
        "instruction": "删掉编造的鉴权",
    },
    ensure_ascii=False,
)
_PRD_REVISE_JSON = json.dumps(
    {
        "verdict": "revise",
        "issues": [{"severity": "high", "detail": "页面跳转规则缺失"}],
    },
    ensure_ascii=False,
)


# ---------------------------------------------------------------------------
# 一、修复追加新版本：正文与结论同批写入，旧正文仍在历史里
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("doc_kind", "body", "fixed"),
    [("api_docs", _API_DOCS_BODY, _FIXED_API_DOCS), ("prompts", _PROMPTS_BODY, _FIXED_PROMPTS)],
)
def test_fix_appends_a_version_carrying_the_new_conclusion(
    client, monkeypatch, doc_kind, body, fixed
):
    """修复后版本数 +1、新版本带新结论，旧正文仍在版本历史、旧结论也留在旧版本上。"""
    register(client)
    solution_id = _create_solution(client)
    _seed_document(client, solution_id, "prd", _PRD_BODY)
    _seed_document(client, solution_id, doc_kind, body)
    first_conclusion = _establish_revise(client, monkeypatch, solution_id, doc_kind)

    llm = FakeLLM(writers=[fixed], reviewers=[_PASS_JSON])
    _fake_service(monkeypatch, llm)
    _job_id, events, status = _run_job(client, _fix_payload(solution_id, doc_kind))

    assert status == "completed"
    assert llm.writer_calls == 1, "修复只重写一次"
    assert llm.reviewer_calls == 1, "修复后只复审一次"

    second_conclusion = _review_events(events)[0]
    assert second_conclusion["kind"] == doc_kind
    assert second_conclusion["verdict"] == "pass"

    doc = _documents(client, solution_id)[doc_kind]
    assert doc["content"] == fixed, "正文换成了修复稿"
    assert doc["review"] == second_conclusion, "新版本带的是修复后复审的那条结论"

    versions = _versions(client, solution_id, doc_kind)
    assert [item["version_no"] for item in versions] == [2, 1], "修复追加一版，旧版仍在"
    newest, oldest = versions
    assert newest["source"] == "review_fix", "版本来源记为修复"
    assert newest["is_current"] is True

    newest_detail = _version_detail(client, solution_id, doc_kind, newest["version_id"])
    assert newest_detail["content"] == fixed
    assert newest_detail["review"] == second_conclusion

    oldest_detail = _version_detail(client, solution_id, doc_kind, oldest["version_id"])
    assert oldest_detail["content"] == body, "旧正文仍在版本历史里"
    assert oldest_detail["review"] == first_conclusion, "旧结论留在它描述的那一版上"


# ---------------------------------------------------------------------------
# 二、上下文窗口：全量注入当前上游（不是审核侧那份骨架）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("doc_kind", ["api_docs", "prompts"])
def test_fix_injects_the_current_upstream_in_full(client, monkeypatch, doc_kind):
    """修复接口文档注入 PRD 全文；修复提示词注入 PRD 与接口文档全文。

    「当前版本」与「全文」两条都要证明：

    - 先写入旧 PRD、再改成新字符串 → 提示词里必须有**新**那句（改上游后修复可验证）；
    - 新那句是一句**不含数字的普通段落**——审核侧 `skeletonize` 会把这类行丢掉，
      因此它出现在修复提示词里，就说明修复这一侧是整段注入而不是骨架。
    """
    register(client)
    solution_id = _create_solution(client)
    _seed_document(client, solution_id, "prd", "# 旧 PRD\n\n这一版的上游内容。\n")
    updated_prd = (
        "# 演示产品 产品需求文档（PRD）\n\n"
        "## 一、产品概述\n\n这一句没有数字，但它必须原样出现在修复提示词里。\n"
    )
    _seed_document(client, solution_id, "prd", updated_prd)
    _seed_document(client, solution_id, "api_docs", _API_DOCS_BODY)
    if doc_kind == "prompts":
        _seed_document(client, solution_id, "prompts", _PROMPTS_BODY)
    _establish_revise(client, monkeypatch, solution_id, doc_kind)

    llm = FakeLLM(writers=[_FIXED_PROMPTS], reviewers=[_PASS_JSON])
    _fake_service(monkeypatch, llm)
    _job_id, _events, status = _run_job(client, _fix_payload(solution_id, doc_kind))

    assert status == "completed"
    prompt = llm.writer_prompts[0]
    assert "这一句没有数字，但它必须原样出现在修复提示词里。" in prompt, (
        "修复必须看得见 PRD 当前版本全文"
    )
    assert "这一版的上游内容。" not in prompt, "拿的必须是当前版本，不是上一版"
    if doc_kind == "prompts":
        assert "统一响应结构：`{code, data, message}`" in prompt, (
            "修复提示词同样要看得见接口文档全文"
        )


def test_fix_of_prd_injects_summary_and_both_clarification_threads(client, monkeypatch):
    """PRD 的修复上下文 = 需求摘要 + `requirement` 与 `prd-clarify` 两条线程 + 当前正文。"""
    register(client)
    solution_id = _create_solution(client)
    _seed_requirements(
        client,
        solution_id,
        product_name="演示产品",
        requirements="做一个给团队写周报的工具",
    )
    _seed_turn(solution_id, "requirement", "需求澄清里定下的：只支持 Web 端")
    _seed_turn(solution_id, "prd-clarify", "PRD 澄清里定下的：页面清单按三栏布局")
    _seed_document(client, solution_id, "prd", _PRD_BODY)

    _fake_service(monkeypatch, FakeLLM(reviewers=[_PRD_REVISE_JSON]))
    _job_id, events, status = _run_job(client, _review_payload(solution_id, "prd"))
    assert status == "completed"
    assert _review_events(events)[0]["verdict"] == "revise"

    llm = FakeLLM(writers=[_FIXED_PRD], reviewers=[_PASS_JSON])
    _fake_service(monkeypatch, llm)
    _job_id, events, status = _run_job(client, _fix_payload(solution_id, "prd"))

    assert status == "completed"
    prompt = llm.writer_prompts[0]
    assert "做一个给团队写周报的工具" in prompt, "需求输入要进修复上下文"
    assert "只支持 Web 端" in prompt, "需求澄清线程要进修复上下文"
    assert "页面清单按三栏布局" in prompt, "PRD 澄清线程也要进修复上下文"
    assert "页面清单见下。" in prompt, "当前正文要进修复上下文"
    assert "页面跳转规则缺失" in prompt, "修复依据是当前版本的审核结论"

    doc = _documents(client, solution_id)["prd"]
    assert doc["content"] == _FIXED_PRD
    assert doc["review"]["verdict"] == "pass"
    assert [item["version_no"] for item in _versions(client, solution_id, "prd")] == [2, 1]


# ---------------------------------------------------------------------------
# 三、修复内的复审只审不改：正文只变一次
# ---------------------------------------------------------------------------


def test_fix_reviews_without_rewriting_again(client, monkeypatch):
    """复审即使仍判「有问题」，也**不再触发改写**——正文只变一次（C-R2）。

    假模型准备了两份重写稿：真被改两轮的话，落在库里的会是第二份。
    """
    register(client)
    solution_id = _create_solution(client)
    _seed_document(client, solution_id, "prd", _PRD_BODY)
    _seed_document(client, solution_id, "api_docs", _API_DOCS_BODY)
    _establish_revise(client, monkeypatch, solution_id, "api_docs")

    second_rewrite = "# 接口文档\n\n## 一、概述\n\n这是第二轮才该出现的稿子。\n"
    llm = FakeLLM(writers=[_FIXED_API_DOCS, second_rewrite], reviewers=[_REVISE_JSON])
    _fake_service(monkeypatch, llm)
    _job_id, events, status = _run_job(client, _fix_payload(solution_id, "api_docs"))

    assert status == "completed"
    assert llm.writer_calls == 1, "修复内的复审不得再触发改写"
    doc = _documents(client, solution_id)["api_docs"]
    assert doc["content"] == _FIXED_API_DOCS
    assert "第二轮才该出现" not in doc["content"]
    # 复审照实报「仍有问题」：结论是复审那条，不是「修完就自动通过」
    assert doc["review"]["verdict"] == "revise"
    assert doc["review"]["rewritten"] is False, "复审不产生改写"
    assert _review_events(events)[0]["verdict"] == "revise"


# ---------------------------------------------------------------------------
# 四、失败边界：改写失败不动库；复审失败只留空结论
# ---------------------------------------------------------------------------


def test_fix_rewrite_failure_keeps_body_and_versions(client, monkeypatch):
    """改写失败 → 正文、结论与版本数一个都不动。"""
    register(client)
    solution_id = _create_solution(client)
    _seed_document(client, solution_id, "prd", _PRD_BODY)
    _seed_document(client, solution_id, "api_docs", _API_DOCS_BODY)
    first_conclusion = _establish_revise(client, monkeypatch, solution_id, "api_docs")

    llm = FakeLLM(writers=[ConnectionError("重写上游挂了")], reviewers=[_PASS_JSON])
    _fake_service(monkeypatch, llm)
    _job_id, events, status = _run_job(client, _fix_payload(solution_id, "api_docs"))

    assert status == "failed"
    assert any(event["type"] == "error" for event in events), "失败要有可读的原因"

    doc = _documents(client, solution_id)["api_docs"]
    assert doc["content"] == _API_DOCS_BODY, "改写失败不覆写正文"
    assert doc["review"] == first_conclusion, "改写失败也不动结论"
    assert [item["version_no"] for item in _versions(client, solution_id, "api_docs")] == [1]


def test_fix_keeps_the_new_body_when_the_rereview_fails(client, monkeypatch):
    """改写成功但复审失败 → 正文照落库、结论留空；随后还能再点「重新审核」补上。"""
    register(client)
    solution_id = _create_solution(client)
    _seed_document(client, solution_id, "prd", _PRD_BODY)
    _seed_document(client, solution_id, "api_docs", _API_DOCS_BODY)
    _establish_revise(client, monkeypatch, solution_id, "api_docs")

    llm = FakeLLM(writers=[_FIXED_API_DOCS], reviewers=[ConnectionError("审核上游挂了")])
    _fake_service(monkeypatch, llm)
    _job_id, events, status = _run_job(client, _fix_payload(solution_id, "api_docs"))

    assert status == "completed", "复审失败不该把已经落下的新正文判成失败"
    # 订阅期间给出的是一条「没审成」的结论（判定空串），界面据此显示「审核失败」
    conclusions = _review_events(events)
    assert conclusions, "复审失败也要有一条 review 事件，否则界面无从显示「审核失败」"
    assert conclusions[-1]["verdict"] == ""

    doc = _documents(client, solution_id)["api_docs"]
    assert doc["content"] == _FIXED_API_DOCS, "新正文照落库"
    assert doc["review"] is None, "没有结论 ≠ 通过：复审失败不写占位"
    versions = _versions(client, solution_id, "api_docs")
    assert [item["version_no"] for item in versions] == [2, 1]

    # 「可再点重新审核」：补审一次就能把这一版的结论填回去
    _fake_service(monkeypatch, FakeLLM(reviewers=[_PASS_JSON]))
    _job_id, _events, status = _run_job(client, _review_payload(solution_id, "api_docs"))
    assert status == "completed"
    assert _documents(client, solution_id)["api_docs"]["review"]["verdict"] == "pass"


# ---------------------------------------------------------------------------
# 五、入口条件：只对「有问题」的当前版本开放
# ---------------------------------------------------------------------------


def test_fix_requires_a_revise_conclusion(client, monkeypatch):
    """没有结论、或结论不是「有问题」时明确 400——修复的唯一起点就是那条结论。"""
    register(client)
    solution_id = _create_solution(client)
    _seed_document(client, solution_id, "api_docs", _API_DOCS_BODY)

    # ① 从未审过
    untouched = client.post("/api/jobs", json=_fix_payload(solution_id, "api_docs"))
    assert untouched.status_code == 400, untouched.text
    assert "有问题" in untouched.json()["detail"]

    # ② 审过、且判了通过：修复没有起点
    _fake_service(monkeypatch, FakeLLM(reviewers=[_PASS_JSON]))
    _job_id, _events, status = _run_job(client, _review_payload(solution_id, "api_docs"))
    assert status == "completed"
    passed = client.post("/api/jobs", json=_fix_payload(solution_id, "api_docs"))
    assert passed.status_code == 400, passed.text
    assert "有问题" in passed.json()["detail"]


def test_fix_requires_doc_kind_and_a_body(client):
    """`doc_kind` 决定注入哪份上游；没有正文就没有可修的东西——两者都在门口拒掉。"""
    register(client)
    solution_id = _create_solution(client)

    no_kind = client.post(
        "/api/jobs", json={"kind": "review_fix", "solution_id": solution_id}
    )
    assert no_kind.status_code == 400, no_kind.text
    assert "doc_kind" in no_kind.json()["detail"]

    no_body = client.post("/api/jobs", json=_fix_payload(solution_id, "api_docs"))
    assert no_body.status_code == 400, no_body.text
    assert "还没有正文" in no_body.json()["detail"]


# ---------------------------------------------------------------------------
# 六、修复 PRD 也算「换了一版上游」：下游按既有规则亮过期黄条
# ---------------------------------------------------------------------------


def test_fix_of_prd_marks_downstream_stale(client, monkeypatch):
    """修复追加的是**新版本**：下游的 `derived_from` 于是对不上，亮过期黄条。"""
    register(client)
    solution_id = _create_solution(client)
    _seed_document(client, solution_id, "prd", _PRD_BODY)

    # 用整篇生成把接口文档写入库：这样它才带 `derived_from` 的上游锚点
    _fake_service(monkeypatch, FakeLLM(writers=[_API_DOCS_BODY], reviewers=[_PASS_JSON]))
    _job_id, _events, status = _run_job(
        client, {"kind": "api_docs", "solution_id": solution_id}
    )
    assert status == "completed"
    assert _documents(client, solution_id)["api_docs"]["stale"] is False

    # PRD 被判「有问题」，随后按结论修复
    _fake_service(monkeypatch, FakeLLM(reviewers=[_PRD_REVISE_JSON]))
    _job_id, _events, status = _run_job(client, _review_payload(solution_id, "prd"))
    assert status == "completed"

    _fake_service(monkeypatch, FakeLLM(writers=[_FIXED_PRD], reviewers=[_PASS_JSON]))
    _job_id, _events, status = _run_job(client, _fix_payload(solution_id, "prd"))
    assert status == "completed"

    documents = _documents(client, solution_id)
    assert documents["prd"]["content"] == _FIXED_PRD
    assert documents["api_docs"]["stale"] is True, "上游换了一版，下游要亮过期黄条"
    # 黄条只是提示：下游正文与版本都不动（正文由整篇生成写入，尾随空白已被剥离）
    assert documents["api_docs"]["content"] == _API_DOCS_BODY.strip()
    assert [item["version_no"] for item in _versions(client, solution_id, "api_docs")] == [1]


# ---------------------------------------------------------------------------
# 七、刷新 / 换会话后仍在
# ---------------------------------------------------------------------------


def test_fix_result_survives_a_new_session(client, monkeypatch):
    """结论与版本都落了库：换一个会话（重新登录）读到的仍是同一份。"""
    register(client)
    solution_id = _create_solution(client)
    _seed_document(client, solution_id, "prd", _PRD_BODY)
    _seed_document(client, solution_id, "prompts", _PROMPTS_BODY)
    _establish_revise(client, monkeypatch, solution_id, "prompts")

    _fake_service(monkeypatch, FakeLLM(writers=[_FIXED_PROMPTS], reviewers=[_PASS_JSON]))
    _job_id, _events, status = _run_job(client, _fix_payload(solution_id, "prompts"))
    assert status == "completed"

    client.cookies.clear()
    login(client)
    doc = _documents(client, solution_id)["prompts"]
    assert doc["content"] == _FIXED_PROMPTS
    assert doc["review"]["verdict"] == "pass"
    assert len(_versions(client, solution_id, "prompts")) == 2
