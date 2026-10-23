"""文档澄清（`clarify`）与就地改写收尾的契约测试。

澄清工作流分两半：**对话**（走 `/api/conversation/start-stream` 的 `doc-clarification`
档案，见 `test_conversation_prompt.py`）与**改写**（走 `/api/jobs` 的 `clarify` 任务，
本文件）。改写这一半要守住三条：

1. **只动该动的**：其余章节逐字保留（这条靠提示词约束，但「改完的正文被完整落库、
   没有被后端二次加工」是代码能保证的）。
2. **清单必须重算**：改写稿末尾的块就是新的清单，而不是沿用上一轮的——这正是
   「换了正文却忘了重算清单」在这条路径上不存在的原因。
3. **不许静默丢东西**：`clarify` / `optimize` 是就地改写，不牵动下游产物；缺 `doc_kind`
   时宁可不落库也不猜；回执里的技能包身份必须与实际注入的一致。

所有用例都在离线零 token 下运行：真实 LLM 工厂被替换成一调用即抛，生成一律注入
`ScriptedLLM`。
"""

from __future__ import annotations

import asyncio

import pytest
from conftest import register, users

from backend.services import document_service, document_store, job_service, llm_factory, prd_section_writer
from backend.services.job_bus import job_bus
from backend.services.pending_items import BEGIN_SENTINEL, END_SENTINEL
from fakes import ScriptedLLM

# ---------------------------------------------------------------------------
# 夹具（与 test_generation_job.py 同款：不触网、任务状态互相隔离）
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_real_llm(monkeypatch):
    """硬约束：真实 LLM 工厂一被调用就抛，杜绝任何测试触网。"""

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("测试不应触网调用真实 LLM")

    monkeypatch.setattr(llm_factory, "get_llm", _boom)
    monkeypatch.setattr(document_service, "get_llm", _boom)
    monkeypatch.setattr(prd_section_writer, "get_llm", _boom)
    yield


@pytest.fixture(autouse=True)
def clean_job_state():
    """每个用例前后清空进程内运行时与任务集合，避免用例互相污染。"""
    job_service._RUNTIMES.clear()  # noqa: SLF001
    job_service._TASKS.clear()  # noqa: SLF001
    job_bus._subscribers.clear()  # noqa: SLF001
    yield
    job_service._RUNTIMES.clear()  # noqa: SLF001
    job_service._TASKS.clear()  # noqa: SLF001
    job_bus._subscribers.clear()  # noqa: SLF001


def _alice_id() -> int:
    """取已注册用户 alice 的 id（`generation_jobs.owner_id` 有外键）。"""
    from backend.services import auth_service

    user = users.get_by_username("alice")
    assert user is not None
    return int(user["id"])


def _fake_service(monkeypatch, llm) -> None:
    """把模块级文档服务的实例换成注入了假 LLM 的实例。"""
    monkeypatch.setattr(
        document_service,
        "get_service",
        lambda: document_service.DocumentService(llm=llm),
    )


def _run_job(owner_id: int, solution_id: int | None, kind: str, request: dict) -> tuple[int, list]:
    """建任务 → 派发 → 一路订阅到结束，返回 (job_id, 全部事件)。"""

    async def scenario() -> tuple[int, list]:
        job_id = job_service.create_job(owner_id=owner_id, solution_id=solution_id, kind=kind)
        job_service.dispatch(
            job_id, owner_id=owner_id, solution_id=solution_id, kind=kind, request=request
        )
        return job_id, [event async for event in job_service.open_stream(owner_id, job_id)]

    return asyncio.run(scenario())


def _create_solution(client) -> int:
    """建一份真实方案并返回 id（落文档要求归属可校验）。"""
    response = client.post(
        "/api/solutions",
        json={"title": "演示方案", "mode": "structured", "stage": "review-prd", "data": "{}"},
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _pending_block(question: str, sections: list[str] | None = None) -> str:
    """拼一个合法的待确认标记块。

    `importance` 不能省：等级缺失或不合法时 `extract` 会把该项直接丢弃。
    """
    import json

    payload = json.dumps(
        [
            {
                "question": question,
                "sections": sections or [],
                "decider": "",
                "importance": "P1",
            }
        ],
        ensure_ascii=False,
    )
    return f"{BEGIN_SENTINEL}\n{payload}\n{END_SENTINEL}"


# ---------------------------------------------------------------------------
# 改写：只改澄清涉及的章节 + 重算清单
# ---------------------------------------------------------------------------

# 一份「带两处待确认」的 PRD：1. 分页上限（本轮会答）；2. 响应时间（本轮没答）
_ORIGINAL_PRD = """# 演示产品 PRD

## 1. 功能需求

分页上限：待确认

## 2. 非功能需求

响应时间：待确认
"""

# 模型改写后的稿子：答掉分页上限、保留响应时间待确认，末尾只留一条新清单
_REWRITTEN_PRD = f"""# 演示产品 PRD

## 1. 功能需求

分页上限：50 条

## 2. 非功能需求

响应时间：待确认

{_pending_block("响应时间的要求是什么？", ["2"])}
"""

_HISTORY = [
    {"role": "assistant", "content": "分页上限取多少？"},
    {"role": "user", "content": "就定 50 条。"},
]


def test_clarify_writes_the_rewritten_body_and_recomputes_pending(client, monkeypatch):
    """改写成功后：正文按 `doc_kind` 落库，清单**重算**为改写稿末尾的那一份。

    「重算」在数据上表现为：原文档有 1 条清单（关于分页上限），改写后只剩 1 条**不同的**
    清单（关于响应时间）——已解答项随块一起消失，未解答项留着。
    """
    register(client)
    uid = _alice_id()
    solution_id = _create_solution(client)
    # 先放一份「上一轮生成」的文档：正文 + 旧清单
    document_store.commit_generated(
        uid,
        solution_id,
        "prd",
        _ORIGINAL_PRD,
        pending_items=[{"id": "p-1", "question": "分页上限取多少？", "sections": ["1"], "decider": ""}],
    )

    _fake_service(monkeypatch, ScriptedLLM([_REWRITTEN_PRD]))
    _job_id, events = _run_job(
        uid,
        solution_id,
        "clarify",
        {"document": _ORIGINAL_PRD, "history": _HISTORY, "doc_kind": "prd"},
    )

    assert events[-1].data["status"] == "completed"
    done = next(event for event in events if event.type == "done")

    doc = document_store.get_document(uid, solution_id, "prd")
    assert doc is not None
    # 正文与 done.reply 逐字一致，且不含任何标记块
    assert doc["content"] == done.data["reply"]
    assert "分页上限：50 条" in doc["content"]
    assert "响应时间：待确认" in doc["content"]
    assert BEGIN_SENTINEL not in doc["content"] and END_SENTINEL not in doc["content"]

    # 清单被重算：只有改写稿末尾那一条，旧的「分页上限」随块一起消失
    assert [item["question"] for item in doc["pending_items"]] == ["响应时间的要求是什么？"]
    assert done.data["pending_items"] == doc["pending_items"]


def test_clarify_prompt_carries_conversation_and_original_document(client, monkeypatch):
    """提示词里必须同时有「原文档」与「本轮对话」——否则模型无从知道答了什么。"""
    register(client)
    uid = _alice_id()
    solution_id = _create_solution(client)
    llm = ScriptedLLM([_REWRITTEN_PRD])
    _fake_service(monkeypatch, llm)

    _run_job(
        uid,
        solution_id,
        "clarify",
        {"document": _ORIGINAL_PRD, "history": _HISTORY, "doc_kind": "prd"},
    )

    prompt = str(llm.calls[0][-1].content)
    assert _ORIGINAL_PRD.strip() in prompt
    assert "就定 50 条。" in prompt
    # 改写规则里必须明确「其余章节逐字保留」，否则模型会顺手润色全文
    assert "逐字保留" in prompt


def test_clarify_injects_the_target_product_conventions(client, monkeypatch):
    """`doc_kind` 决定注入哪一套产出规范——整段注入，不是挑几条。

    「优化接口文档」时注入的是接口规范，不是 PRD 的写作规范。这条以前是**没有**的：
    回执上写着技能包身份，提示词里却什么都没有，属于静默不一致。
    """
    register(client)
    uid = _alice_id()
    solution_id = _create_solution(client)
    llm = ScriptedLLM(["# 接口文档\n\n正文"])
    _fake_service(monkeypatch, llm)

    _run_job(
        uid,
        solution_id,
        "clarify",
        {"document": "# 接口文档\n\n分页：待确认", "history": _HISTORY, "doc_kind": "api_docs"},
    )

    prompt = str(llm.calls[0][-1].content)
    # 取自 skills/api-docs-generator/references/api-specs.md 的小标题
    assert "统一响应结构与字段命名" in prompt


def test_clarify_without_doc_kind_is_rejected(client):
    """缺 `doc_kind` → 400：不知道写回哪一份，这次澄清等于白做。"""
    register(client)
    _create_solution(client)

    response = client.post(
        "/api/jobs",
        json={"kind": "clarify", "document": "# PRD", "history": _HISTORY},
    )
    assert response.status_code == 400


def test_clarify_without_history_is_rejected(client):
    """缺对话记录 → 400：没有「用户答了什么」，无从改写。"""
    register(client)
    _create_solution(client)

    response = client.post(
        "/api/jobs",
        json={"kind": "clarify", "document": "# PRD", "doc_kind": "prd"},
    )
    assert response.status_code == 400


def test_clarify_without_document_is_rejected(client):
    """缺正文 → 400。"""
    register(client)
    _create_solution(client)

    response = client.post(
        "/api/jobs",
        json={"kind": "clarify", "history": _HISTORY, "doc_kind": "prd"},
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# 就地改写不牵动下游：`clarify` / `optimize` 都只动同一份文档
# ---------------------------------------------------------------------------


def test_clarify_does_not_invalidate_downstream(client, monkeypatch):
    """按澄清改写 PRD **不会**删掉接口文档与提示词——它们是同一份 PRD 的产物。

    `DOWNSTREAM_KINDS` 只在「整篇重生成上游」时才生效；就地改写不是重生成。
    """
    register(client)
    uid = _alice_id()
    solution_id = _create_solution(client)
    document_store.save_document(uid, solution_id, "prd", _ORIGINAL_PRD)
    document_store.save_document(uid, solution_id, "api_docs", "# 接口文档")
    document_store.save_document(uid, solution_id, "prompts", "# 提示词")

    _fake_service(monkeypatch, ScriptedLLM([_REWRITTEN_PRD]))
    _run_job(
        uid,
        solution_id,
        "clarify",
        {"document": _ORIGINAL_PRD, "history": _HISTORY, "doc_kind": "prd"},
    )

    kinds = {doc["kind"] for doc in document_store.list_documents(uid, solution_id)}
    assert kinds == {"prd", "api_docs", "prompts"}


def test_optimize_does_not_invalidate_downstream(client, monkeypatch):
    """按指令就地优化接口文档，**不会**把基于它生成的提示词删掉。

    这条是回归测试：原先收尾一律按「整篇重生成」处理，于是「优化接口文档」会静默删掉
    提示词——用户没有任何提示，只是下次打开方案时东西不见了。
    """
    register(client)
    uid = _alice_id()
    solution_id = _create_solution(client)
    document_store.save_document(uid, solution_id, "prd", "# PRD")
    document_store.save_document(uid, solution_id, "api_docs", "# 旧接口文档")
    document_store.save_document(uid, solution_id, "prompts", "# 提示词")

    _fake_service(monkeypatch, ScriptedLLM(["# 优化后的接口文档"]))
    _run_job(
        uid,
        solution_id,
        "optimize",
        {"document": "# 旧接口文档", "instruction": "补充分页说明", "doc_kind": "api_docs"},
    )

    kinds = {doc["kind"] for doc in document_store.list_documents(uid, solution_id)}
    assert kinds == {"prd", "api_docs", "prompts"}


# 「整篇重生成 PRD 不删下游」那条不变式在 `test_generation_job.py::test_regenerating_prd_keeps_downstream_documents`，
# 与它的姊妹用例（重生成接口文档）放在一起。此处不再重复一份：同一个断言写两遍，
# 改的时候只改一处，另一处就会静默地继续验证旧行为。


# ---------------------------------------------------------------------------
# 回执归桶：按 `doc_kind` 写进对应的那一份，且技能包身份与实际注入一致
# ---------------------------------------------------------------------------


def test_clarify_run_summary_lands_on_the_target_document(client, monkeypatch):
    """回执按 `doc_kind` 归桶：改写接口文档，回执就写在接口文档那一行上。"""
    register(client)
    uid = _alice_id()
    solution_id = _create_solution(client)
    document_store.save_document(uid, solution_id, "prd", "# PRD")
    document_store.save_document(uid, solution_id, "api_docs", "# 旧接口文档")

    _fake_service(monkeypatch, ScriptedLLM(["# 接口文档\n\n正文"]))
    _run_job(
        uid,
        solution_id,
        "clarify",
        {"document": "# 旧接口文档", "history": _HISTORY, "doc_kind": "api_docs"},
    )

    api_docs = document_store.get_document(uid, solution_id, "api_docs")
    prd = document_store.get_document(uid, solution_id, "prd")
    assert api_docs is not None and api_docs["last_run_summary"] is not None
    # PRD 那一行没被写回执（它这次没被碰过）
    assert prd is not None and prd["last_run_summary"] is None


def test_skill_refs_follow_the_target_product(client, monkeypatch):
    """回执里的技能包身份按**目标产物**报，而不是按「做了一件什么事」。

    没有这一条，回执会给「改写接口文档」标上 PRD 那个包——报出的身份与实际注入的规范
    不符，下一次排查就会从错误的方向开始。
    """
    register(client)
    uid = _alice_id()
    solution_id = _create_solution(client)
    _fake_service(monkeypatch, ScriptedLLM(["# 接口文档\n\n正文"]))

    _job_id, events = _run_job(
        uid,
        solution_id,
        "clarify",
        {"document": "# 旧接口文档", "history": _HISTORY, "doc_kind": "api_docs"},
    )

    summary = events[-1].data
    assert summary["kind"] == "clarify"
    names = [skill["name"] for skill in summary["injected_skills"]]
    assert names == ["api-docs-generator"]


def test_skill_refs_are_empty_when_the_target_is_unknown(client, monkeypatch):
    """取不到目标产物时回执**不报**技能包——报一个没注入的包比不报更糟。"""
    register(client)
    uid = _alice_id()
    solution_id = _create_solution(client)
    _fake_service(monkeypatch, ScriptedLLM(["优化结果"]))

    _job_id, events = _run_job(
        uid,
        solution_id,
        "optimize",
        {"document": "# 旧接口文档", "instruction": "改一下"},
    )

    assert events[-1].data["injected_skills"] == []
