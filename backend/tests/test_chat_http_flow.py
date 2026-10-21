"""对话任务的 HTTP 全链路：建方案 → 建任务 → 订阅 → 读轮次。

为什么单独一条：`test_chat_job.py` 走的是服务层（`create_job` + `dispatch`），
**绕过了接口层的字段命名、必填校验与响应形状**。而前端只看得见接口层——
「前端拿到的对话是空的」这类现象，十有八九出在这两层之间的缝隙里，
所以这里按前端的**真实调用顺序**走一遍，一步都不简化。
"""

from __future__ import annotations

import json

import pytest
from conftest import register

from backend.services import conversation_service
from fakes import ScriptedLLM


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch):
    """对话服务换成脚本模型：不触网、输出确定。"""
    monkeypatch.setattr(
        conversation_service,
        "get_service",
        lambda: conversation_service.ConversationService(llm=ScriptedLLM(["你好，", "我先问三个问题。"])),
    )


def _create_solution(client) -> int:
    """按前端的形状建一份方案（结构化状态，不再是 JSON 文本）。"""
    response = client.post(
        "/api/solutions",
        json={
            "title": "周报",
            "mode": "structured",
            "stage": "form",
            "state": {
                "view_state": "form",
                "entry_mode": "structured",
                "chat_tab": "requirement",
                "summary": {"product_name": "周报"},
            },
            "content_changed": True,
        },
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _read_stream(client, job_id: int) -> str:
    """把 SSE 全文读成一段文本（测试里只需要断言关键字）。"""
    with client.stream("GET", f"/api/jobs/{job_id}/stream") as response:
        return "".join(chunk for chunk in response.iter_text())


def test_start_turn_survives_the_whole_http_chain(client):
    """开新会话：流里有回复，轮次接口能读到它，且角色是前端认识的形状。"""
    register(client)
    solution_id = _create_solution(client)

    created = client.post(
        "/api/jobs",
        json={
            "kind": "chat",
            "solution_id": solution_id,
            "stage": "requirement",
            "chat_mode": "start",
            "form_data": {"product_name": "周报"},
            "content": "",
            "tool_result": None,
            "profile_key": "",
        },
    )
    assert created.status_code == 201, created.text
    job_id = int(created.json()["job_id"])

    text = _read_stream(client, job_id)
    # 脚本模型跑得比订阅还快：事件可能全在 `snapshot` 里（真实 LLM 会逐字出 chunk）。
    # 因此这里只断言「订阅结束时能拿到完整回复」，不断言事件种类。
    assert "我先问三个问题。" in text

    turns = client.get(f"/api/solutions/{solution_id}/turns")
    assert turns.status_code == 200, turns.text
    items = turns.json()["turns"]
    # 开场白是 system（不展示），回复是 assistant；前端拿到的角色里**没有 tool**
    assert [item["role"] for item in items] == ["system", "assistant"]
    assert "我先问三个问题。" in items[1]["content"]
    assert items[1]["stage"] == "requirement"
    assert isinstance(items[1]["id"], int)


def test_continue_round_replays_the_thread(client):
    """续聊：服务端把这一轮也落成轮次，第二轮能接着上一轮的上下文。"""
    register(client)
    solution_id = _create_solution(client)

    first = client.post(
        "/api/jobs",
        json={
            "kind": "chat",
            "solution_id": solution_id,
            "stage": "requirement",
            "chat_mode": "start",
            "form_data": {"product_name": "周报"},
        },
    )
    assert first.status_code == 201, first.text
    _read_stream(client, int(first.json()["job_id"]))

    second = client.post(
        "/api/jobs",
        json={
            "kind": "chat",
            "solution_id": solution_id,
            "stage": "requirement",
            "chat_mode": "continue",
            "content": "第一版要不要做离线同步？",
        },
    )
    assert second.status_code == 201, second.text
    _read_stream(client, int(second.json()["job_id"]))

    turns = client.get(
        f"/api/solutions/{solution_id}/turns", params={"stage": "requirement"}
    ).json()["turns"]
    assert [item["role"] for item in turns] == ["system", "assistant", "user", "assistant"]
    assert turns[2]["content"] == "第一版要不要做离线同步？"


def test_missing_stage_is_rejected_before_any_job_is_created(client):
    """线程标识缺失必须当场 400：猜一个默认值等于把对话记到别人头上。"""
    register(client)
    solution_id = _create_solution(client)

    response = client.post(
        "/api/jobs",
        json={
            "kind": "chat",
            "solution_id": solution_id,
            "chat_mode": "continue",
            "content": "你好",
        },
    )
    assert response.status_code == 400
    assert "stage" in json.dumps(response.json(), ensure_ascii=False)


def test_turns_of_a_new_solution_are_empty_not_404(client):
    """还没聊过的方案返回空列表（不是 404）：两种情况对前端意义完全不同。"""
    register(client)
    solution_id = _create_solution(client)

    response = client.get(f"/api/solutions/{solution_id}/turns")
    assert response.status_code == 200
    assert response.json()["turns"] == []
