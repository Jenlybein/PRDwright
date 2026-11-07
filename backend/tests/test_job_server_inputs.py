"""生成任务的**上游正文由服务端取**（spec #51 / 票 #53 的「扩」）。

本文件只跨 HTTP 主链路断言：

| 接缝 | 断言的可观察行为 |
| --- | --- |
| `POST /api/jobs` + `GET /{id}/stream` | 只给方案与任务种类就能生成，上游由服务端自己从库里取 |
| 假 LLM 收到的提示词 | 上游是「当前版本全文」/「方案上的录入文本」，不是请求体里那一份 |
| `GET /api/solutions/{id}/documents` | 产出落到对应产物上，正文与 `done.reply` 逐字一致 |

请求体里的旧字段**仍然被接受**（拆契约是下一票 #54 的事），但生成链路不再拿它们当上游：
凡「请求体与库冲突」的用例都在证明同一件事——以库为准。

所有用例离线零 token：真实 LLM 工厂一调用即抛，生成一律注入 `ScriptedLLM`。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import register, users

from backend.core.engine import session_scope
from backend.repositories.chat_turn_repo import ChatTurnRepository
from backend.services import (
    document_service,
    job_runtime,
    llm_factory,
    prd_section_writer,
)
from backend.services.job_bus import job_bus
from fakes import ScriptedLLM

# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_real_llm(monkeypatch):
    """真实 LLM 工厂一被调用就抛，杜绝任何测试触网。"""

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("测试不应触网调用真实 LLM")

    monkeypatch.setattr(llm_factory, "get_llm", _boom)
    monkeypatch.setattr(document_service, "get_llm", _boom)
    monkeypatch.setattr(prd_section_writer, "get_llm", _boom)
    yield


@pytest.fixture(autouse=True)
def clean_job_state():
    """清空进程内运行时与任务集合，避免用例互相污染。"""
    job_runtime._RUNTIMES.clear()  # noqa: SLF001
    job_runtime._TASKS.clear()  # noqa: SLF001
    job_bus._subscribers.clear()  # noqa: SLF001
    yield
    job_runtime._RUNTIMES.clear()  # noqa: SLF001
    job_runtime._TASKS.clear()  # noqa: SLF001
    job_bus._subscribers.clear()  # noqa: SLF001


def _fake_service(monkeypatch, llm: ScriptedLLM) -> None:
    """把模块级文档服务的实例换成注入了假 LLM 的实例。"""
    monkeypatch.setattr(
        document_service,
        "get_service",
        lambda: document_service.DocumentService(llm=llm),
    )


def _create_solution(client, state: dict[str, Any] | None = None) -> int:
    """建一份真实方案（可带状态列），返回 id。"""
    created = client.post(
        "/api/solutions",
        json={
            "title": "演示方案",
            "mode": "structured",
            "stage": "review-prd",
            "state": state or {},
        },
    )
    assert created.status_code == 201, created.text
    return int(created.json()["id"])


def _put_document(client, solution_id: int, kind: str, content: str) -> None:
    """把一份正文写进方案（走文档路由；没有文档时它会顺势建第一版）。"""
    response = client.put(
        f"/api/solutions/{solution_id}/documents/{kind}", json={"content": content}
    )
    assert response.status_code == 200, response.text


def _documents(client, solution_id: int) -> dict[str, dict[str, Any]]:
    """读回方案下的三份产物（按 kind 索引）。"""
    response = client.get(f"/api/solutions/{solution_id}/documents")
    assert response.status_code == 200, response.text
    return {item["kind"]: item for item in response.json()["documents"]}


def _seed_requirement_turn(solution_id: int, content: str) -> None:
    """在需求澄清线程里落一条用户轮次（PRD 生成的上游之一）。

    这只是**准备材料**：生产里这条轮次由对话任务写入同一张表；
    断言仍在 HTTP 主链路上，不去读这张表。用例里注册的用户都是 alice。
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


def _create_job(client, payload: dict[str, Any]) -> int:
    """建一个任务并返回 id（201 之外的响应直接判失败）。"""
    created = client.post("/api/jobs", json=payload)
    assert created.status_code == 201, created.text
    return int(created.json()["job_id"])


def _drain(client, job_id: int) -> list[dict[str, Any]]:
    """订阅事件流直到任务结束，返回解码后的事件列表。"""
    response = client.get(f"/api/jobs/{job_id}/stream")
    assert response.status_code == 200, response.text
    events: list[dict[str, Any]] = []
    for line in response.text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload or payload == "[DONE]":
            continue
        events.append(json.loads(payload))
    assert events, "订阅应当至少收到一份快照"
    return events


def _run_to_completion(client, payload: dict[str, Any]) -> None:
    """建任务 → 订阅到结束；终态不是 completed 就当场判失败。

    假 LLM 太快，订阅往往晚于收尾：此时首帧快照已是终态，`done` 只在订阅期间才发。
    因此终态判据取 `run_summary`（终态任务会由 `open_stream` 补发一份），
    **正文断言一律读文档接口**——那才是刷新 / 换设备后仍存在的真源。
    """
    job_id = _create_job(client, payload)
    events = _drain(client, job_id)
    summary = next(event for event in events if event["type"] == "run_summary")
    assert summary["data"]["status"] == "completed", summary


def _generation_prompt(llm: ScriptedLLM) -> str:
    """取出**生成那一次**调用收到的提示词正文。

    取第一次而不是全部：接口文档 / 提示词在审一次时还会再调一次模型，
    那次的输入是「待审的产出」而不是上游材料，混进来会让断言失去意义。
    """
    assert llm.calls, "生成没有调用模型"
    return "\n".join(str(getattr(message, "content", "")) for message in llm.calls[0])


def _all_prompts(llm: ScriptedLLM) -> str:
    """把所有调用收到的正文拼成一段（PRD 分章节链路一次生成会调很多次）。"""
    assert llm.calls, "生成没有调用模型"
    return "\n".join(
        str(getattr(message, "content", ""))
        for call in llm.calls
        for message in call
    )


# ---------------------------------------------------------------------------
# 一、接口文档：上游 = PRD 当前版本全文
# ---------------------------------------------------------------------------


def test_api_docs_job_takes_prd_current_version_from_server(client, monkeypatch):
    """只给方案与任务种类：接口文档的上游由服务端从库里的 PRD 当前版本取。

    这条同时是「旧字段不再是必需」的判据——请求体里一份正文都没有，
    生成仍要拿到 PRD 全文，否则模型只对着空白写接口。
    """
    register(client)
    llm = ScriptedLLM(["# 接口文档\n\n## 账本"])
    _fake_service(monkeypatch, llm)
    solution_id = _create_solution(client)
    _put_document(client, solution_id, "prd", "# 库里的 PRD\n\n账本支持多币种结算")

    _run_to_completion(
        client, {"kind": "api_docs", "solution_id": solution_id, "review_enabled": False}
    )

    assert "# 库里的 PRD" in _generation_prompt(llm)
    # 产出落到接口文档上：正文就是这次模型的产出（独立已知字面量，不读事件里的值）
    api_docs = _documents(client, solution_id)["api_docs"]
    assert api_docs["content"] == "# 接口文档\n\n## 账本"


def test_api_docs_job_uses_the_prd_after_it_was_edited(client, monkeypatch):
    """PRD 改过之后再生成接口文档，用的是**新的**那一版（后端自己取当前版本）。

    这条是「双真源不复活」的核心判据：解析若发生在建任务那一刻并留在了任务单上，
    或者前端那份副本被当成上游，第二份接口文档就会继续对着旧 PRD 写，且没人能发现。
    """
    register(client)
    solution_id = _create_solution(client)
    payload = {"kind": "api_docs", "solution_id": solution_id, "review_enabled": False}

    _put_document(client, solution_id, "prd", "# 第一版 PRD\n\n只有邮箱登录")
    first_llm = ScriptedLLM(["# 接口文档 v1"])
    _fake_service(monkeypatch, first_llm)
    _run_to_completion(client, payload)
    assert "第一版 PRD" in _generation_prompt(first_llm)

    _put_document(client, solution_id, "prd", "# 第二版 PRD\n\n改成手机号登录")
    second_llm = ScriptedLLM(["# 接口文档 v2"])
    _fake_service(monkeypatch, second_llm)
    _run_to_completion(client, payload)

    prompt = _generation_prompt(second_llm)
    assert "第二版 PRD" in prompt
    assert "第一版 PRD" not in prompt


def test_api_docs_job_ignores_a_stale_prd_in_the_request_body(client, monkeypatch):
    """请求体里仍可就地给上游（旧前端），但**库里那一份说了算**。

    这正是「删字段之前先让行为不变」的那一半：旧前端照发，产出的仍是最新 PRD；
    下一票删掉字段时，行为不需要再变一次。
    """
    register(client)
    llm = ScriptedLLM(["# 接口文档"])
    _fake_service(monkeypatch, llm)
    solution_id = _create_solution(client)
    _put_document(client, solution_id, "prd", "# 库里的新版 PRD")

    _run_to_completion(
        client,
        {
            "kind": "api_docs",
            "solution_id": solution_id,
            "prd_content": "# 前端手里的旧 PRD",
            "review_enabled": False,
        },
    )

    prompt = _generation_prompt(llm)
    assert "# 库里的新版 PRD" in prompt
    assert "前端手里的旧 PRD" not in prompt


# ---------------------------------------------------------------------------
# 二、提示词：上游 = PRD 与接口文档两份当前版本
# ---------------------------------------------------------------------------


def test_prompts_job_takes_both_current_versions_from_server(client, monkeypatch):
    """只给方案与任务种类：提示词同时拿到 PRD 与接口文档的当前版本。"""
    register(client)
    llm = ScriptedLLM(["# 提示词套件"])
    _fake_service(monkeypatch, llm)
    solution_id = _create_solution(client)
    _put_document(client, solution_id, "prd", "# 库里的 PRD\n\n账本支持多币种")
    _put_document(client, solution_id, "api_docs", "# 库里的接口文档\n\nPOST /api/ledgers")

    _run_to_completion(
        client, {"kind": "prompts", "solution_id": solution_id, "review_enabled": False}
    )

    prompt = _generation_prompt(llm)
    assert "# 库里的 PRD" in prompt
    assert "# 库里的接口文档" in prompt
    # 产出落到提示词上
    assert _documents(client, solution_id)["prompts"]["content"] == "# 提示词套件"

    # 改过接口文档后再生成提示词，第二份跟着新版本走（两份上游各自都是「当前」）
    _put_document(client, solution_id, "api_docs", "# 改过的接口文档\n\nPOST /api/books")
    second = ScriptedLLM(["# 提示词套件 v2"])
    _fake_service(monkeypatch, second)
    _run_to_completion(
        client, {"kind": "prompts", "solution_id": solution_id, "review_enabled": False}
    )
    second_prompt = _generation_prompt(second)
    assert "# 改过的接口文档" in second_prompt
    assert "# 库里的接口文档" not in second_prompt


# ---------------------------------------------------------------------------
# 三、导入入口：上游 = 方案上保存的录入文本
# ---------------------------------------------------------------------------


def test_import_entry_prompts_use_the_saved_draft_prd(client, monkeypatch):
    """「我有 PRD」入口：上游是方案行上存下来的那份草稿，不是请求体。

    这个入口没有库内 PRD（用户自带的那份刻意不进文档 store），
    所以「服务端取」对它只有一条路——读方案上的录入文本列。
    """
    register(client)
    llm = ScriptedLLM(["# 提示词套件"])
    _fake_service(monkeypatch, llm)
    solution_id = _create_solution(
        client,
        {
            "entry_mode": "prompt-debug",
            "draft_prd_text": "# 贴入的 PRD\n\n账本要支持导入",
        },
    )

    _run_to_completion(
        client,
        {
            "kind": "prompts",
            "solution_id": solution_id,
            "prd_content": "# 请求体里的 PRD",
            "review_enabled": False,
        },
    )

    prompt = _generation_prompt(llm)
    assert "# 贴入的 PRD" in prompt
    assert "请求体里的 PRD" not in prompt


def test_import_entry_prompts_use_both_saved_paste_columns(client, monkeypatch):
    """「我有 PRD + 接口文档」入口：两列录入文本各自进提示词，缺一列也不串。"""
    register(client)
    llm = ScriptedLLM(["# 提示词套件"])
    _fake_service(monkeypatch, llm)
    solution_id = _create_solution(
        client,
        {
            "entry_mode": "prd-and-api-paste",
            "paste_prd_text": "# 双粘贴的 PRD\n\n账本要支持共享",
            "paste_api_docs_text": "# 双粘贴的接口文档\n\nGET /api/ledgers",
        },
    )

    _run_to_completion(
        client,
        {"kind": "prompts", "solution_id": solution_id, "review_enabled": False},
    )

    prompt = _generation_prompt(llm)
    assert "# 双粘贴的 PRD" in prompt
    assert "# 双粘贴的接口文档" in prompt


# ---------------------------------------------------------------------------
# 四、PRD：上游 = 结构化摘要 + 自由文本需求 + 需求澄清历史
# ---------------------------------------------------------------------------


def test_prd_job_takes_summary_and_requirement_history_from_server(client, monkeypatch):
    """只给方案与任务种类：PRD 的上游是方案上的摘要与需求澄清线程。

    完整流程的两个上游缺一不可——摘要决定写什么，澄清历史决定「已经拍过的结论」。
    请求体里照旧可以带一份 requirements（旧前端），但它不再是上游。
    """
    register(client)
    llm = ScriptedLLM(["章节正文"])
    _fake_service(monkeypatch, llm)
    solution_id = _create_solution(
        client,
        {
            "summary": {
                "product_name": "记账助手",
                "problem": "手工记账太麻烦",
                "target_users": "有两地生活的上班族",
            }
        },
    )
    _seed_requirement_turn(solution_id, "登录方式确定为手机号验证码")

    _run_to_completion(
        client,
        {
            "kind": "prd",
            "solution_id": solution_id,
            "requirements": "请求体里的需求",
            "review_enabled": False,
        },
    )

    prompts = _all_prompts(llm)
    assert "手工记账太麻烦" in prompts
    assert "登录方式确定为手机号验证码" in prompts
    assert "请求体里的需求" not in prompts
    # 产品名也从摘要里取：文档头由代码生成，取错名字一眼能看出来
    prd = _documents(client, solution_id)["prd"]
    assert prd["content"].startswith("# 记账助手 产品需求文档")


def test_quick_flow_prd_takes_free_text_requirements_from_server(client, monkeypatch):
    """快捷流程（一段话需求）：上游是方案上的自由文本需求那一列。

    这条入口没有结构化摘要，也没有需求澄清对话——若服务端只认摘要，
    快捷流程在拆契约之后就会变成「没有任何输入」，所以它单独守一条。
    """
    register(client)
    llm = ScriptedLLM(["章节正文"])
    _fake_service(monkeypatch, llm)
    solution_id = _create_solution(
        client,
        {
            "entry_mode": "prd-quick",
            "quick_product_name": "共享账本",
            "quick_requirements": "一句话需求：做一个多人共享的账本",
        },
    )

    _run_to_completion(
        client, {"kind": "prd", "solution_id": solution_id, "review_enabled": False}
    )

    assert "一句话需求：做一个多人共享的账本" in _all_prompts(llm)
    prd = _documents(client, solution_id)["prd"]
    assert prd["content"].startswith("# 共享账本 产品需求文档")
