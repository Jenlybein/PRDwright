"""「AI 自行澄清」（`auto_clarify`）任务的契约测试。

与 `clarify` 的差别只有一个：**谁出面回答**。`clarify` 复述用户这一轮聊出来的结论，
`auto_clarify` 替用户按「最保守的行业默认值」逐条拍板、直接把结论写进正文。

因此它守的契约与 `clarify` 同构：

1. **写回哪一份靠 `doc_kind`**：缺了就 400，不猜默认值（猜错是静默的数据损坏）。
2. **清单按改写稿重算**：拍板掉的条目随块消失，真需要用户提供事实的才留着。
3. **不牵动下游**：就地改写同一份文档，接口文档与提示词仍是同一份上游推出的产物。

所有用例都在离线零 token 下运行：真实 LLM 工厂被替换成一调用即抛，生成一律注入
`ScriptedLLM`。
"""

from __future__ import annotations

import asyncio

import pytest
from conftest import register

from backend.services import document_service, document_store, job_service, llm_factory, prd_section_writer
from backend.services.job_bus import job_bus
from backend.services.pending_items import BEGIN_SENTINEL, END_SENTINEL
from fakes import ScriptedLLM

# ---------------------------------------------------------------------------
# 夹具：不触网、任务状态互相隔离（与 test_clarify.py 同款）
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

    user = auth_service.UserRepository.get_by_username("alice")
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


# 「AI 该拍板的两条」：一条是纯默认值（分页上限），一条是必须有事实依据的（响应时间）
_ITEMS = [
    {
        "id": "p-1",
        "question": "分页上限取多少？",
        "sections": ["1"],
        "decider": "产品负责人",
        "importance": "P0",
    },
    {
        "id": "p-2",
        "question": "响应时间的要求是什么？",
        "sections": ["2"],
        "decider": "",
        "importance": "P2",
    },
]

_ORIGINAL_PRD = """# 演示产品 PRD

## 1. 功能需求

分页上限：待确认

## 2. 非功能需求

响应时间：待确认
"""

# 模型拍板后的稿子：分页上限按默认值定了；响应时间真需要用户提供，留在清单里
_DECIDED_PRD = f"""# 演示产品 PRD

## 1. 功能需求

分页上限：50 条（行业常规默认值，上线前复核）

## 2. 非功能需求

响应时间：待确认

{_pending_block("响应时间的要求是什么？", ["2"])}
"""


# ---------------------------------------------------------------------------
# 改写：正文就地落库 + 清单重算
# ---------------------------------------------------------------------------


def test_auto_clarify_writes_the_body_and_recomputes_pending(client, monkeypatch):
    """拍板成功后：正文按 `doc_kind` 就地写回，清单**重算**为改写稿末尾那一份。

    「重算」在数据上表现为：原来的 2 条只剩 1 条**不同的**（关于响应时间）——
    被拍板掉的条目随块一起消失，真答不了的才留着。
    """
    register(client)
    uid = _alice_id()
    solution_id = _create_solution(client)
    document_store.commit_generated(
        uid,
        solution_id,
        "prd",
        _ORIGINAL_PRD,
        pending_items=list(_ITEMS),
    )

    _fake_service(monkeypatch, ScriptedLLM([_DECIDED_PRD]))
    _job_id, events = _run_job(
        uid,
        solution_id,
        "auto_clarify",
        {"document": _ORIGINAL_PRD, "pending_items": list(_ITEMS), "doc_kind": "prd"},
    )

    assert events[-1].data["status"] == "completed"
    done = next(event for event in events if event.type == "done")

    doc = document_store.get_document(uid, solution_id, "prd")
    assert doc is not None
    # 正文与 done.reply 逐字一致，且不含任何标记块
    assert doc["content"] == done.data["reply"]
    assert "分页上限：50 条" in doc["content"]
    assert BEGIN_SENTINEL not in doc["content"] and END_SENTINEL not in doc["content"]

    assert [item["question"] for item in doc["pending_items"]] == ["响应时间的要求是什么？"]
    assert done.data["pending_items"] == doc["pending_items"]


def test_auto_clarify_prompt_carries_document_and_pending_list(client, monkeypatch):
    """提示词里必须同时有「原文档」与「渲染成人话的清单」——它要照单逐条拍板。

    清单是**结构化数据**，渲染由后端负责（`format_pending_items`）：前端不该拼好一段
    提示词递过来（ADR-010）。因此这里断言的是「后端把结构渲染成了什么」。
    """
    register(client)
    uid = _alice_id()
    solution_id = _create_solution(client)
    llm = ScriptedLLM([_DECIDED_PRD])
    _fake_service(monkeypatch, llm)

    _run_job(
        uid,
        solution_id,
        "auto_clarify",
        {"document": _ORIGINAL_PRD, "pending_items": list(_ITEMS), "doc_kind": "prd"},
    )

    prompt = str(llm.calls[0][-1].content)
    assert _ORIGINAL_PRD.strip() in prompt
    # 等级、影响章节、原定拍板人都要带上：这是模型判断「该不该替用户拍板」的依据
    assert "1. [P0] 分页上限取多少？（影响章节：1）（原定拍板人：产品负责人）" in prompt
    assert "2. [P2] 响应时间的要求是什么？（影响章节：2）" in prompt
    # 拍板规则要在提示词里说死：不反问、取保守默认值、只改受影响的地方
    assert "不要反问" in prompt
    assert "逐字保留" in prompt


def test_auto_clarify_injects_the_target_product_conventions(client, monkeypatch):
    """`doc_kind` 决定注入哪一套产出规范——整段注入，不是挑几条。"""
    register(client)
    uid = _alice_id()
    solution_id = _create_solution(client)
    llm = ScriptedLLM(["# 接口文档\n\n正文"])
    _fake_service(monkeypatch, llm)

    _run_job(
        uid,
        solution_id,
        "auto_clarify",
        {
            "document": "# 接口文档\n\n分页：待确认",
            "pending_items": list(_ITEMS),
            "doc_kind": "api_docs",
        },
    )

    prompt = str(llm.calls[0][-1].content)
    # 取自 skills/api-docs-generator/references/api-specs.md 的小标题
    assert "统一响应结构与字段命名" in prompt


def test_auto_clarify_does_not_invalidate_downstream(client, monkeypatch):
    """拍板是**就地改写**，不删接口文档与提示词——它们仍由同一份 PRD 推导而来。"""
    register(client)
    uid = _alice_id()
    solution_id = _create_solution(client)
    document_store.save_document(uid, solution_id, "prd", _ORIGINAL_PRD)
    document_store.save_document(uid, solution_id, "api_docs", "# 接口文档")
    document_store.save_document(uid, solution_id, "prompts", "# 提示词")

    _fake_service(monkeypatch, ScriptedLLM([_DECIDED_PRD]))
    _run_job(
        uid,
        solution_id,
        "auto_clarify",
        {"document": _ORIGINAL_PRD, "pending_items": list(_ITEMS), "doc_kind": "prd"},
    )

    assert document_store.get_document(uid, solution_id, "api_docs") is not None
    assert document_store.get_document(uid, solution_id, "prompts") is not None


# ---------------------------------------------------------------------------
# 参数校验：三样缺一不可，一律 400（不猜默认值）
# ---------------------------------------------------------------------------


def test_auto_clarify_without_doc_kind_is_rejected(client):
    """缺 `doc_kind` → 400：不知道写回哪一份，拍板结果只能丢。"""
    register(client)
    _create_solution(client)

    response = client.post(
        "/api/jobs",
        json={"kind": "auto_clarify", "document": "# PRD", "pending_items": list(_ITEMS)},
    )
    assert response.status_code == 400


def test_auto_clarify_without_pending_items_is_rejected(client):
    """空清单 → 400：没有可拍板的东西，跑一次只会把正文原样吐回来还烧 token。"""
    register(client)
    _create_solution(client)

    response = client.post(
        "/api/jobs",
        json={"kind": "auto_clarify", "document": "# PRD", "doc_kind": "prd", "pending_items": []},
    )
    assert response.status_code == 400


def test_auto_clarify_without_document_is_rejected(client):
    """缺正文 → 400。"""
    register(client)
    _create_solution(client)

    response = client.post(
        "/api/jobs",
        json={"kind": "auto_clarify", "doc_kind": "prd", "pending_items": list(_ITEMS)},
    )
    assert response.status_code == 400
