"""对话任务（`kind='chat'`）的契约测试。

盯的是「对话与文档哪里不一样」——那正是这次改造的全部风险所在：

1. **只追加、不覆盖**：一个任务产出两条轮次（进去的一条 + 出来的一条），
   幂等靠唯一索引，不靠调用方自觉；
2. **历史由后端拼**：前端回传的那份不再是权威，否则两台设备各持一份就没有裁决者；
3. **停止 ≠ 退订**：用户按停止要真停，且已说出口的话保留（标 `interrupted`）；
4. **单写者**：同一线程同时只允许一轮，第二条请求被拒而不是顶替前一条。

所有用例零 token：真实 LLM 工厂不参与，对话服务被替换成注入了脚本模型的实例。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from conftest import register

from backend.core.errors import AppError
from backend.repositories.chat_turn_repo import ChatTurnRepository
from backend.services import conversation_service, job_service
from fakes import ScriptedLLM


@pytest.fixture(autouse=True)
def clean_job_state():
    """清空进程内运行时与任务集合，避免用例互相污染。"""
    job_service._RUNTIMES.clear()  # noqa: SLF001
    job_service._TASKS.clear()  # noqa: SLF001
    yield
    job_service._RUNTIMES.clear()  # noqa: SLF001
    job_service._TASKS.clear()  # noqa: SLF001


@pytest.fixture
def alice(client):
    """注册 alice 并返回 (owner_id, solution_id)。"""
    register(client)
    from backend.services import auth_service

    user = auth_service.UserRepository.get_by_username("alice")
    assert user is not None
    created = client.post("/api/solutions", json={"title": "对话方案"})
    assert created.status_code == 201, created.text
    return int(user["id"]), int(created.json()["id"])


def _use_llm(monkeypatch, llm) -> None:
    """把对话服务换成注入了脚本模型的实例。

    换实例而不是换 `get_llm`：`ConversationService` 的 llm 是惰性 property，
    已经建好的单例会一直用它自己那份缓存，打补丁打不到。
    """
    monkeypatch.setattr(
        conversation_service,
        "get_service",
        lambda: conversation_service.ConversationService(llm=llm),
    )


def _create(owner_id: int, solution_id: int, request: dict) -> int:
    """建一条对话任务（不派发），返回 job_id。"""
    return job_service.create_job(
        owner_id=owner_id,
        solution_id=solution_id,
        kind="chat",
        request=dict(request),
    )


def _run(owner_id: int, solution_id: int, request: dict) -> tuple[int, list, dict]:
    """建任务、派发、一路订阅到结束。返回 (job_id, 事件, 被 create_job 补全后的 request)。"""

    async def scenario() -> tuple[int, list, dict]:
        payload = dict(request)
        job_id = job_service.create_job(
            owner_id=owner_id,
            solution_id=solution_id,
            kind="chat",
            request=payload,
        )
        job_service.dispatch(
            job_id,
            owner_id=owner_id,
            solution_id=solution_id,
            kind="chat",
            request=payload,
        )
        events = [event async for event in job_service.open_stream(owner_id, job_id)]
        return job_id, events, payload

    return asyncio.run(scenario())


def _turns(owner_id: int, solution_id: int, stage: str = "requirement") -> list[dict]:
    return ChatTurnRepository.list_thread(owner_id, solution_id, stage)


# ---------------------------------------------------------------------------
# 一轮对话 = 两条轮次
# ---------------------------------------------------------------------------


def test_start_writes_opening_and_assistant_turns(client, alice, monkeypatch):
    """开新会话：开场白存成 system 轮次，回复存成 assistant 轮次。"""
    owner_id, solution_id = alice
    _use_llm(monkeypatch, ScriptedLLM(["你好，", "我先问三个问题。"]))

    _, events, _ = _run(
        owner_id,
        solution_id,
        {"stage": "requirement", "chat_mode": "start", "form_data": {"product_name": "周报"}},
    )

    assert any(event.type == "done" for event in events)
    turns = _turns(owner_id, solution_id)
    assert [turn["role"] for turn in turns] == ["system", "assistant"]
    # 开场白是「喂给模型但不展示给用户」的那部分上下文：不存它，第二轮模型就只看到
    # 自己上一句话，完全不知道用户提交过什么
    assert turns[0]["content"].strip() != ""
    assert turns[1]["content"] == "你好，我先问三个问题。"


def test_continue_appends_user_and_assistant_turns(client, alice, monkeypatch):
    """续聊：追加 user + assistant 两条，顺序由 id 决定。"""
    owner_id, solution_id = alice
    _use_llm(monkeypatch, ScriptedLLM(["第一次回复"]))
    _run(
        owner_id,
        solution_id,
        {"stage": "requirement", "chat_mode": "start", "form_data": {}},
    )

    _use_llm(monkeypatch, ScriptedLLM(["第二次回复"]))
    _, events, request = _run(
        owner_id,
        solution_id,
        {"stage": "requirement", "chat_mode": "continue", "content": "再说细一点"},
    )

    assert any(event.type == "done" for event in events)
    turns = _turns(owner_id, solution_id)
    assert [turn["role"] for turn in turns] == ["system", "assistant", "user", "assistant"]
    assert turns[-2]["content"] == "再说细一点"
    assert turns[-2]["job_id"] == request["job_id"]


def test_history_comes_from_turns_not_from_request(client, alice, monkeypatch):
    """续聊时后端自己拼历史：模型能看到上一轮的回复。"""
    owner_id, solution_id = alice
    llm = ScriptedLLM(["上一轮我说过的话"])
    _use_llm(monkeypatch, llm)
    _run(
        owner_id,
        solution_id,
        {"stage": "requirement", "chat_mode": "start", "form_data": {}},
    )

    llm2 = ScriptedLLM(["好"])
    _use_llm(monkeypatch, llm2)
    _run(
        owner_id,
        solution_id,
        {"stage": "requirement", "chat_mode": "continue", "content": "继续"},
    )

    # 第二次调用收到的消息里必须既有上一轮的 assistant，也有本轮的用户输入
    joined = "\n".join(str(message.content) for message in llm2.calls[0])
    assert "上一轮我说过的话" in joined
    assert "继续" in joined


def test_threads_do_not_leak_into_each_other(client, alice, monkeypatch):
    """PRD 澄清线程看不到需求澄清的对话——四条线程各拼各的历史。"""
    owner_id, solution_id = alice
    _use_llm(monkeypatch, ScriptedLLM(["需求线程的回复"]))
    _run(
        owner_id,
        solution_id,
        {"stage": "requirement", "chat_mode": "start", "form_data": {}},
    )

    llm = ScriptedLLM(["文档线程的回复"])
    _use_llm(monkeypatch, llm)
    _run(
        owner_id,
        solution_id,
        {
            "stage": "prd-clarify",
            "chat_mode": "start",
            "form_data": {"document": "# PRD 正文"},
        },
    )

    joined = "\n".join(str(message.content) for message in llm.calls[0])
    assert "需求线程的回复" not in joined
    assert "PRD 正文" in joined


# ---------------------------------------------------------------------------
# 单写者：同一线程同时只允许一轮
# ---------------------------------------------------------------------------


def test_second_turn_on_same_thread_is_rejected(client, alice):
    """同线程已有 running 任务时，新发起被拒（不是顶替）。"""
    owner_id, solution_id = alice
    _create(
        owner_id,
        solution_id,
        {"stage": "requirement", "chat_mode": "continue", "content": "第一句"},
    )

    with pytest.raises(AppError) as raised:
        _create(
            owner_id,
            solution_id,
            {"stage": "requirement", "chat_mode": "continue", "content": "第二句"},
        )
    assert "还在生成中" in str(raised.value)


def test_other_thread_is_not_blocked(client, alice):
    """不同线程互不相干：一条在跑，不该拦住另一条。"""
    owner_id, solution_id = alice
    _create(
        owner_id,
        solution_id,
        {"stage": "requirement", "chat_mode": "continue", "content": "第一句"},
    )
    # 不抛异常即为通过
    _create(
        owner_id,
        solution_id,
        {"stage": "prd-clarify", "chat_mode": "continue", "content": "另一条线程"},
    )


def test_unknown_stage_is_rejected(client, alice):
    """线程标识必须合法：猜一个默认值等于把对话记到别人头上。"""
    owner_id, solution_id = alice
    with pytest.raises(AppError) as raised:
        _create(owner_id, solution_id, {"stage": "nope", "chat_mode": "continue", "content": "x"})
    assert "未知的对话线程" in str(raised.value)


# ---------------------------------------------------------------------------
# 停止：真停、且保留已生成的部分
# ---------------------------------------------------------------------------


def test_cancel_stops_and_keeps_partial_reply(client, alice, monkeypatch):
    """用户按停止：轮次照样写、打上 interrupted，正文是已经流出的那部分。"""
    owner_id, solution_id = alice
    llm = ScriptedLLM(["很长的一段", "还在继续说", "第三段"])
    _use_llm(monkeypatch, llm)

    async def scenario() -> tuple[int, list]:
        payload = {
            "stage": "requirement",
            "chat_mode": "continue",
            "content": "讲个长故事",
        }
        job_id = job_service.create_job(
            owner_id=owner_id, solution_id=solution_id, kind="chat", request=payload
        )
        job_service.dispatch(
            job_id, owner_id=owner_id, solution_id=solution_id, kind="chat", request=payload
        )
        events = []
        async for event in job_service.open_stream(owner_id, job_id):
            events.append(event)
            # 收到第一段增量就按停止：模拟「说到一半用户点了停止」
            if event.type == "chunk" and not any(e.type == "run_summary" for e in events):
                job_service.cancel_job(owner_id, job_id)
        return job_id, events

    job_id, _ = asyncio.run(scenario())

    turns = [turn for turn in _turns(owner_id, solution_id) if turn["role"] == "assistant"]
    assert len(turns) == 1
    assert turns[0]["interrupted"] == 1
    # 已经说出口的话要留下——用户按停止通常是「够了」，不是「不要了」
    assert "很长的一段" in turns[0]["content"]
    assert turns[0]["job_id"] == job_id


def test_cancel_finished_job_reports_false(client, alice, monkeypatch):
    """任务已经结束再点停止：返回「没取消到」，而不是报错。"""
    owner_id, solution_id = alice
    _use_llm(monkeypatch, ScriptedLLM(["说完了"]))
    job_id, _, _ = _run(
        owner_id,
        solution_id,
        {"stage": "requirement", "chat_mode": "continue", "content": "你好"},
    )

    assert job_service.cancel_job(owner_id, job_id) is False


# ---------------------------------------------------------------------------
# 存储形状 ≠ 展示形状
# ---------------------------------------------------------------------------


def test_tool_answer_is_stored_as_tool_but_served_as_user(client, alice, monkeypatch):
    """作答轮：库里存 `tool`（上游协议要求），接口投影成用户气泡。"""
    owner_id, solution_id = alice
    _use_llm(monkeypatch, ScriptedLLM(["收到"]))
    _run(
        owner_id,
        solution_id,
        {
            "stage": "requirement",
            "chat_mode": "continue",
            "content": "第 1 题：A",
            "tool_result": {
                "tool_call_id": "call-1",
                "answers": [{"question": "目标用户", "texts": ["运营"]}],
            },
        },
    )

    stored = _turns(owner_id, solution_id)
    assert stored[0]["role"] == "tool"
    assert stored[0]["tool_call_id"] == "call-1"

    response = client.get(
        f"/api/solutions/{solution_id}/turns", params={"stage": "requirement"}
    )
    assert response.status_code == 200
    served = response.json()["turns"]
    # 前端类型里没有 `tool`：让它流过去会在复建时被白名单整条丢掉
    assert [turn["role"] for turn in served] == ["user", "assistant"]
    assert served[0]["answers"] == [{"question": "目标用户", "texts": ["运营"]}]


def test_turns_endpoint_rejects_unknown_stage(client, alice):
    """未知线程返回 400，而不是悄悄返回需求澄清的对话。"""
    _, solution_id = alice
    response = client.get(f"/api/solutions/{solution_id}/turns", params={"stage": "nope"})
    assert response.status_code == 400


def test_turns_endpoint_hides_other_users_threads(client, alice):
    """越权读轮次返回 404（不是空列表：空列表是「这条线程还没聊过」）。"""
    _, solution_id = alice
    client.post("/api/auth/logout")
    register(client, username="bob", invite_code="test-invite")

    response = client.get(
        f"/api/solutions/{solution_id}/turns", params={"stage": "requirement"}
    )
    assert response.status_code == 404


def test_done_carries_clarification_asks_into_the_turn(monkeypatch, client, alice):
    """`done` 里的选择题必须落进轮次，否则选项卡永远不出现。

    这条守的是一个**键名**：对话服务下发的字段叫 `clarification_asks`。
    若收尾时读成 `asks`，不会报任何错，只会每轮都存下空数组——
    界面表现为「选项卡再也没出现过」，而日志里 `asks_count` 明明是 3。
    """
    owner_id, solution_id = alice
    _use_llm(monkeypatch, ScriptedLLM(["我先问几个问题。"]))

    from backend.core.contracts import StreamEvent

    turns_before = _turns(owner_id, solution_id)
    assert turns_before == []

    # 直接走「建任务 + 手动收尾」，绕开模型：这里只验证事件 → 轮次的映射
    from backend.services import job_service as js

    payload = {
        "stage": "requirement",
        "chat_mode": "continue",
        "content": "开始吧",
        "owner_id": owner_id,
        "solution_id": solution_id,
    }
    job_id = js.create_job(
        owner_id=owner_id, solution_id=solution_id, kind="chat", request=payload
    )
    runtime = js.JobRuntime(
        job_id=job_id,
        owner_id=owner_id,
        solution_id=solution_id,
        kind="chat",
        request=payload,
        chat_stage="requirement",
    )
    js._apply_event(  # noqa: SLF001 - 这一条测的正是事件到运行时的映射
        runtime,
        StreamEvent(
            type="done",
            content="",
            data={
                "reply": "我先问几个问题。",
                "clarification_asks": [
                    {
                        "id": "q1",
                        "question": "目标用户是谁？",
                        "options": [{"label": "A", "title": "运营"}],
                    }
                ],
                "tool_calls": [{"id": "call-1", "name": "ask_clarification_questions", "args": {}}],
            },
        ),
    )
    js._write_chat_turn(runtime)  # noqa: SLF001

    assistant = [t for t in _turns(owner_id, solution_id) if t["role"] == "assistant"]
    assert len(assistant) == 1
    stored = json.loads(assistant[0]["asks"])
    assert len(stored) == 1
    assert stored[0]["question"] == "目标用户是谁？"
