"""全链路用例改走主接缝后，失败分支仍由「造单时传入的假环境」造出（票 #38）。

这张票本身是「替换测试」，红→绿形态：先把新形状写成用例（旧样板还没迁移时
「全链路文件里不再临时替换内部零件」这条守卫会红），再逐文件把样板换成新三步、
让全绿。这里同时把「出错 / 半截 / 分章乱序 / 技能包读不出 / 预算超顶 /
配置中途不改」六条分支在新接缝上各留一条可观察行为的用例，行为与旧口径一致。

只跨公开接缝断言：事件序列、终态、回执、正文、`aborted` 语义；不断言内部
状态或内部摆位。假环境在造单时传入，生产真值 / 测试假值分工明确。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from conftest import register, users, write_skill_package
from dbsupport import connect

from backend.core import runtime_config as rc
from backend.core.config import get_settings
from backend.core.contracts import StreamEvent
from backend.core.engine import session_scope
from backend.core.errors import AppError, ErrorCode
from backend.core.providers import ProviderSpec
from backend.repositories.chat_turn_repo import ChatTurnRepository
from backend.services import (
    conversation_service,
    document_service,
    job_service,
    llm_factory,
    prd_section_writer,
)
from backend.services.job_bus import job_bus
from backend.services.job_ticket import (
    build_job_ticket,
    register_job_ticket,
    start_registered_job,
)
from fakes import ScriptedLLM
from ticket_support import run_ticket, run_ticket_async, run_ticket_to_completion

_TESTS_DIR = Path(__file__).resolve().parent

# 需要迁移的全链路样板文件：迁移完成后这些文件里不应再出现「临时替换内部零件」。
_FULL_CHAIN_FILES = (
    "test_generation_job.py",
    "test_chat_job.py",
    "test_auto_clarify.py",
    "test_clarify.py",
    "test_main_chain_logging.py",
    "test_config_effect.py",
    "test_job_immutable_projection.py",
)


@pytest.fixture(autouse=True)
def clean_job_state():
    """清空进程内运行时、任务集合与总线订阅，避免用例互相污染。"""
    job_service._RUNTIMES.clear()  # noqa: SLF001 - 夹具清理，非行为断言
    job_service._TASKS.clear()  # noqa: SLF001
    job_bus._subscribers.clear()  # noqa: SLF001
    yield
    job_service._RUNTIMES.clear()  # noqa: SLF001
    job_service._TASKS.clear()  # noqa: SLF001
    job_bus._subscribers.clear()  # noqa: SLF001


@pytest.fixture(autouse=True)
def no_real_llm(monkeypatch):
    """硬约束：真实 LLM 工厂一被调用就抛，杜绝任何测试触网。"""

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("测试不应触网调用真实 LLM")

    monkeypatch.setattr(llm_factory, "get_llm", _boom)
    monkeypatch.setattr(document_service, "get_llm", _boom)
    monkeypatch.setattr(prd_section_writer, "get_llm", _boom)


@pytest.fixture
def alice(client):
    """注册 alice 并返回其 id（顺带保证 DB 已初始化）。"""
    register(client)
    user = users.get_by_username("alice")
    assert user is not None
    return int(user["id"])


def _fake_service(monkeypatch, pieces: list[str]) -> None:
    """模型边界换注入假 LLM 的实例（既有离线假环境形态，零 token）。"""
    monkeypatch.setattr(
        document_service,
        "get_service",
        lambda: document_service.DocumentService(llm=ScriptedLLM(pieces)),
    )


def _fake_config(*, config_id: str, model: str, name: str = "假配置") -> rc.RuntimeConfig:
    """造单时传入的假配置快照：不查真库、不触网。"""
    return rc.RuntimeConfig(
        settings=get_settings(),
        active_provider=ProviderSpec(
            id=config_id,
            name=name,
            protocol="ark",
            base_url="",
            value_token="",
            model=model,
            max_tokens=0,
        ),
        source="fake",
        revision=f"fake-revision-{config_id}",
    )


def _row(job_id: int) -> dict:
    """按 id 直读任务行，断言外部可观察的终态与正文。"""
    with connect() as conn:
        result = conn.execute(
            "SELECT * FROM generation_jobs WHERE id = ?", (job_id,)
        ).fetchone()
    assert result is not None
    return dict(result)


def _seed_solution(client) -> int:
    """走既有路由建一份方案，返回 id。"""
    created = client.post("/api/solutions", json={"title": "主接缝方案"})
    assert created.status_code == 201, created.text
    return int(created.json()["id"])


# ---------------------------------------------------------------------------
# 〇、守卫：全链路样板不再临时替换内部零件（红 → 绿的驱动用例）
# ---------------------------------------------------------------------------


def test_full_chain_samples_do_not_replace_internal_parts():
    """全链路样板里不再有 `monkeypatch.setattr(job_service, ...)` 这类内部替换。

    假环境必须在造单时传入；`describe_kind` / `_build_event_stream` / `JobRuntime`
    这些内部零件不该被测试临时换掉。其余文件（#39 的裸运行时摆位用例、
    #35 的反向守卫）不在本票范围内，不扫。
    """
    offenders: list[str] = []
    for name in _FULL_CHAIN_FILES:
        path = _TESTS_DIR / name
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "monkeypatch.setattr(job_service," in line or "monkeypatch.setattr(js," in line:
                offenders.append(f"{name}:{number}: {line.strip()}")
    assert offenders == [], "全链路样板仍在临时替换内部零件：" + "；".join(offenders)


# ---------------------------------------------------------------------------
# 一、造单时传入的假事件流：出错 / 半截 / 分章乱序
# ---------------------------------------------------------------------------


def test_fake_stream_error_keeps_failed_terminal_and_partial_text(alice):
    """假事件流中途抛错：error 事件 + failed 终态 + 半截正文，与旧口径一致。"""

    async def broken_stream(kind, request):
        yield StreamEvent(type="chunk", content="半截正文", data={})
        raise RuntimeError("假环境：生成链路出错")

    registered, events = run_ticket(
        owner_id=alice,
        solution_id=None,
        kind="prd",
        request={"requirements": "造错", "product_name": "假流"},
        event_stream_source=broken_stream,
    )

    error = next(event for event in events if event.type == "error")
    assert error.code == "LLM_UPSTREAM_ERROR"
    assert events[-1].type == "run_summary"
    assert events[-1].data["status"] == "failed"
    row = _row(registered.job_id)
    assert row["status"] == "failed"
    assert row["error_code"] == "LLM_UPSTREAM_ERROR"
    assert "半截正文" in row["draft_content"]


def test_fake_stream_app_error_keeps_budget_exceeded_code(alice):
    """预算超顶（AppError/INVALID_REQUEST）：错误码不被笼统改成上游故障。"""

    async def over_budget(kind, request):
        raise AppError(ErrorCode.INVALID_REQUEST, "输入超出模型窗口")
        yield  # pragma: no cover - 有 yield 才是 async generator

    registered, events = run_ticket(
        owner_id=alice,
        solution_id=None,
        kind="prd",
        request={"requirements": "超顶"},
        event_stream_source=over_budget,
    )

    error = next(event for event in events if event.type == "error")
    assert error.code == "INVALID_REQUEST"
    row = _row(registered.job_id)
    assert row["status"] == "failed"
    assert row["error_code"] == "INVALID_REQUEST"
    assert row["error_message"] == "输入超出模型窗口"
    assert events[-1].data["status"] == "failed"


def test_fake_stream_out_of_order_parts_reassemble_by_index(alice):
    """分章乱序：part 按 index 装配成权威正文，终态与落库正文同源。"""

    async def out_of_order(kind, request):
        # 故意乱序：第 2 章先到、文档头其次、第 1 章最后。
        yield StreamEvent(
            type="part", content="", data={"text": "第2章\n", "index": 1, "section_id": "features"}
        )
        yield StreamEvent(type="part", content="", data={"text": "# 文档头\n"})
        yield StreamEvent(
            type="part", content="", data={"text": "第1章\n", "index": 0, "section_id": "overview"}
        )
        yield StreamEvent(
            type="done", content="", data={"reply": "# 文档头\n第1章\n第2章\n", "partial": False}
        )

    registered, events = run_ticket(
        owner_id=alice,
        solution_id=None,
        kind="prd",
        request={"requirements": "乱序", "product_name": "装配"},
        event_stream_source=out_of_order,
    )

    done = next(event for event in events if event.type == "done")
    assert done.data["reply"] == "# 文档头\n第1章\n第2章\n"
    assert events[-1].data["status"] == "completed"
    row = _row(registered.job_id)
    assert row["status"] == "completed"
    # 最终落库正文按 index 交错归并，不按到达顺序。
    assert row["draft_content"] == "# 文档头\n第1章\n第2章\n"


def test_fake_stream_partial_done_marks_receipt_and_keeps_text(alice):
    """半截任务（done 带 partial）：回执标部分完成、正文保留、终态完成。"""

    async def partial_stream(kind, request):
        yield StreamEvent(type="chunk", content="只写了一半", data={})
        yield StreamEvent(
            type="done", content="", data={"reply": "只写了一半", "partial": True}
        )

    registered, events = run_ticket(
        owner_id=alice,
        solution_id=None,
        kind="prd",
        request={"requirements": "半截"},
        event_stream_source=partial_stream,
    )

    assert any(event.type == "chunk" for event in events)
    assert events[-1].type == "run_summary"
    assert events[-1].data["partial"] is True
    assert events[-1].data["status"] == "completed"
    row = _row(registered.job_id)
    assert row["status"] == "completed"
    assert "只写了一半" in row["draft_content"]


def test_cancel_through_public_action_keeps_aborted_semantics(alice):
    """按停止走公开动作 `cancel_job`：落 `failed + CANCELLED`，不是「生成失败」。

    被中止与真出错要分开：订阅不该收到红色 `error` 事件，回执仍是 failed。
    假流在造单时传入并停在一个可放行的位置，用公开动作而不是内部标志位来取消。
    """
    gate = asyncio.Event()

    async def slow_stream(kind, request):
        yield StreamEvent(type="chunk", content="已经说出口的话", data={})
        await gate.wait()
        yield StreamEvent(type="done", content="", data={"reply": "完整正文"})

    async def drive():
        cancel_target = [0]
        seen: list[StreamEvent] = []

        def on_event(event):
            seen.append(event)
            # 收到第一段增量就按停止：走公开动作，不探内部登记表。
            if event.type == "chunk" and not any(e.type == "run_summary" for e in seen):
                assert job_service.cancel_job(alice, cancel_target[0]) is True
                # 放行假流：协作式取消会在下一个事件点跳出循环，不必挂住订阅者。
                gate.set()

        return await run_ticket_async(
            owner_id=alice,
            solution_id=None,
            kind="prd",
            request={"requirements": "按停止"},
            event_stream_source=slow_stream,
            before_open=lambda job_id: cancel_target.__setitem__(0, job_id),
            on_event=on_event,
        )

    registered, events = asyncio.run(drive())

    assert not any(event.type == "error" for event in events)
    assert events[-1].type == "run_summary"
    assert events[-1].data["status"] == "failed"
    row = _row(registered.job_id)
    assert row["status"] == "failed"
    assert row["error_code"] == "CANCELLED"
    assert "已经说出口的话" in row["draft_content"]


# ---------------------------------------------------------------------------
# 二、技能包读不出：造单时算好的数据如实为空，任务照常跑完
# ---------------------------------------------------------------------------


def test_unreadable_skill_package_degrades_visibility_not_the_job(
    client, alice, monkeypatch, temp_skills_root
):
    """技能包资源读不出：回执里 injected_skills 如实为空，任务仍 completed。

    用临时技能目录造假环境（包在位但资源缺失），不替换 `describe_kind` 内部零件。
    """
    solution_id = _seed_solution(client)
    write_skill_package(temp_skills_root, "prd-generator")
    (temp_skills_root / "prd-generator" / "references" / "prd-template.md").unlink()
    _fake_service(monkeypatch, ["章节正文"])

    job_id, events = run_ticket_to_completion(
        alice,
        solution_id,
        "prd",
        {"requirements": "技能包读不出", "product_name": "降级"},
    )

    assert events[-1].type == "run_summary"
    assert events[-1].data["status"] == "completed"
    assert events[-1].data["injected_skills"] == []
    assert _row(job_id)["status"] == "completed"


# ---------------------------------------------------------------------------
# 三、配置冻结：假配置在造单时定格，生成中途改后台参数不漂移
# ---------------------------------------------------------------------------


def test_fake_config_is_frozen_at_build_time_and_does_not_drift(alice):
    """造单传假配置 A，随后换后台真配置 B：回执与 DB 行仍是 A。"""
    from conftest import model_providers, system_config

    fake = _fake_config(config_id="cfg_seam_a", model="model-A")

    async def plain_stream(kind, request):
        yield StreamEvent(type="chunk", content="配置不漂移", data={})
        yield StreamEvent(
            type="done", content="", data={"reply": "配置不漂移", "partial": False}
        )

    async def scenario():
        draft = build_job_ticket(
            owner_id=alice,
            solution_id=None,
            kind="prd",
            request={"requirements": "配置冻结"},
            event_stream_source=plain_stream,
            config=fake,
        )
        frozen = dict(draft.config_snapshot["active_provider"])
        assert frozen["provider_config_id"] == "cfg_seam_a"
        assert frozen["provider_model"] == "model-A"

        # 造单之后、登记/派发之前改后台配置：这一刻起进程内读到的是 B。
        model_providers.create(
            provider_id="cfg_seam_b",
            name="配置 B",
            protocol="ark",
            value_token="",
            default_model="model-B",
        )
        system_config.upsert_many(
            {rc.ACTIVE_PROVIDER_KEY: json.dumps("cfg_seam_b")}, updated_by=None
        )
        rc.invalidate()
        assert rc.get_runtime_config().active_provider.model == "model-B"

        registered = register_job_ticket(draft)
        start_registered_job(registered)
        events = [
            event
            async for event in job_service.open_stream(alice, registered.job_id)
        ]
        return frozen, registered, events

    frozen, registered, events = asyncio.run(scenario())
    summary = events[-1].data
    assert summary["status"] == "completed"
    assert summary["provider_config_id"] == "cfg_seam_a"
    assert summary["model"] == "model-A"
    row = _row(registered.job_id)
    assert row["provider_config_id"] == "cfg_seam_a"
    assert row["provider_model"] == "model-A"
    assert json.loads(row["config_snapshot"])["active_provider"] == frozen


# ---------------------------------------------------------------------------
# 四、chat 派生字段与输入轮次唯一约束，走主接缝成立
# ---------------------------------------------------------------------------


def test_chat_derived_fields_and_unique_input_turn(client, alice, monkeypatch):
    """四个派生字段明写在单②；同一 (job_id, role) 只写一条输入轮次。"""
    solution_id = _seed_solution(client)
    monkeypatch.setattr(
        conversation_service,
        "get_service",
        lambda: conversation_service.ConversationService(llm=ScriptedLLM(["收到"])),
    )
    request = {
        "stage": "requirement",
        "chat_mode": "continue",
        "content": "一句用户输入",
        "form_data": {},
    }

    registered, events = run_ticket(
        owner_id=alice,
        solution_id=solution_id,
        kind="chat",
        request=dict(request),
    )

    assert registered.job_id > 0
    assert registered.input_turn_id > 0
    assert registered.owner_id == alice
    assert registered.solution_id == solution_id
    assert request == {
        "stage": "requirement",
        "chat_mode": "continue",
        "content": "一句用户输入",
        "form_data": {},
    }
    assert any(event.type == "done" for event in events)
    assert events[-1].data["status"] == "completed"

    with session_scope() as session:
        repo = ChatTurnRepository(session)
        again = repo.create(
            alice,
            solution_id,
            stage="requirement",
            role="user",
            content="重复写入",
            job_id=registered.job_id,
        )
        turns = repo.list_thread(alice, solution_id, "requirement")
    assert again == registered.input_turn_id
    assert sum(turn.role == "user" for turn in turns) == 1
