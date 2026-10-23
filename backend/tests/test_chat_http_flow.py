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


def test_stream_response_carries_the_sse_headers(client):
    """SSE 三件套必须挂在**真实响应**上：media type、禁缓存、禁 Nginx 缓冲。

    这是「本地一切正常、生产必炸」的典型缺口，三条各有各的坏法：

    - `Content-Type` 不是 `text/event-stream` → 浏览器不把它当事件流，`EventSource`
      直接报错（前端表现为「对话没有任何回复」，而服务端日志一切正常）；
    - 缺 `Cache-Control: no-cache` → 中间层把整段响应缓存起来，用户看到的是
      「等很久，然后一次全出来」，看起来像流式坏了；
    - 缺 `X-Accel-Buffering: no` → Nginx 默认缓冲上游响应，同样是「不流式」，
      而**本地没有 Nginx**，所以这一条永远不可能在本地被测出来。

    用户把集成测试整条推迟了，这三行是那批缺口里唯一保留的断言点，因此钉在
    真实响应头（而不是 `SSE_HEADERS` 这个常量）上——常量对了、路由忘了用它，
    才是真正会发生的错。
    """
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
        },
    )
    assert created.status_code == 201, created.text
    job_id = int(created.json()["job_id"])

    with client.stream("GET", f"/api/jobs/{job_id}/stream") as response:
        assert response.status_code == 200
        headers = {key.lower(): value for key, value in response.headers.items()}
        assert headers["content-type"].startswith("text/event-stream")
        assert headers["cache-control"] == "no-cache"
        assert headers["x-accel-buffering"] == "no"
        # 把流读完，别在生成器还开着的时候就断言完关连接（会让服务端报任务被取消）
        for _ in response.iter_text():
            pass


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
