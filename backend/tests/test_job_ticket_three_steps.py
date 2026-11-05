"""生成任务的「造单 → 登记 → 派发 → 订阅」新三步形状（票 #36）。

本文件只跨公开接缝断言，不探内部状态：

| 接缝 | 断言的可观察行为 |
| --- | --- |
| `build_job_ticket` | 造单是公开正门：不落库、不顶替、不发事件、不查方案；技能包身份在此算好 |
| `register_job_ticket` | 一个事务：当场查方案、顶替双标记、落库投影、写对话输入轮次 |
| `start_registered_job` | 只认后一张单；事件形状、终态、回执与旧形状一致 |
| `open_stream` | 订阅先快照后 live，事件种类与编码不变 |
| `create_job` / `dispatch` | 旧入口仍可用、行为逐项不变（同一 dict 原地补全） |

假环境在造单时传入（生产真值 / 测试假值），不开测试专用私有缝。
不碰 SSE 事件种类与编码、`[DONE]`、单 worker + 进程内运行时、重连先快照后 live、
失败不覆写、被取代与已取消分开、排空顺序与 30 秒、就绪探针、`reap_orphans`、
错误码、前端交互。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import FrozenInstanceError, fields
from typing import Any

import pytest
from conftest import register, users
from dbsupport import connect

from backend.core.engine import session_scope
from backend.core.errors import AppError, ErrorCode
from backend.repositories.chat_turn_repo import ChatTurnRepository
from backend.repositories.generation_job_repo import GenerationJobRepository
from backend.services import conversation_service, document_service, job_service, solution_service
from backend.services.job_bus import job_bus
from fakes import ScriptedLLM


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------


def _alice_id() -> int:
    """取已注册用户 alice 的 id（generation_jobs.owner_id 有外键）。"""
    user = users.get_by_username("alice")
    assert user is not None
    return int(user["id"])


@pytest.fixture
def alice(client):
    """注册 alice 并返回其 id（顺带保证 DB 已初始化）。"""
    register(client)
    return _alice_id()


@pytest.fixture(autouse=True)
def clean_job_state():
    """清空进程内运行时、任务集合与总线订阅，避免用例互相污染。"""
    job_service._RUNTIMES.clear()  # noqa: SLF001
    job_service._TASKS.clear()  # noqa: SLF001
    job_bus._subscribers.clear()  # noqa: SLF001
    yield
    job_service._RUNTIMES.clear()  # noqa: SLF001
    job_service._TASKS.clear()  # noqa: SLF001
    job_bus._subscribers.clear()  # noqa: SLF001


def _use_prd_llm(monkeypatch, pieces: list[str]) -> None:
    """把文档生成链路换成注入假 LLM 的实例（全链路离线、零 token）。"""
    monkeypatch.setattr(
        document_service,
        "get_service",
        lambda: document_service.DocumentService(llm=ScriptedLLM(pieces)),
    )

    def _no_real_llm(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("测试不应触网调用真实 LLM")

    monkeypatch.setattr(document_service, "get_llm", _no_real_llm)
    from backend.services import prd_section_writer

    monkeypatch.setattr(prd_section_writer, "get_llm", _no_real_llm)


def _use_chat_llm(monkeypatch, pieces: list[str]) -> None:
    """把对话服务换成注入假 LLM 的实例。"""
    monkeypatch.setattr(
        conversation_service,
        "get_service",
        lambda: conversation_service.ConversationService(llm=ScriptedLLM(pieces)),
    )


def _raw(job_id: int) -> dict[str, Any]:
    """按 id 直读整行（绕开 ORM，验证真的落盘了）。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM generation_jobs WHERE id = ?", (job_id,)
        ).fetchone()
    assert row is not None
    return dict(row)


def _row_count() -> int:
    """任务表当前行数（登记事务性的可观察证据）。"""
    with connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM generation_jobs").fetchone()[0])


def _seed_provider(*, provider_id: str, name: str, model: str) -> str:
    """落一条供应商配置并设为使用中，返回 id。"""
    from backend.core import runtime_config as rc
    from conftest import model_providers, system_config

    model_providers.create(
        provider_id=provider_id,
        name=name,
        protocol="ark",
        value_token="",
        default_model=model,
    )
    system_config.upsert_many(
        {rc.ACTIVE_PROVIDER_KEY: json.dumps(provider_id)}, updated_by=None
    )
    rc.invalidate()
    return provider_id


def _seed_solution(client, title: str = "任务单方案") -> int:
    """走既有路由建一份方案，返回 id（不手写表结构）。"""
    created = client.post("/api/solutions", json={"title": title})
    assert created.status_code == 201, created.text
    return int(created.json()["id"])


def _ticket_module():
    """新三步公开动作所在的模块（造单 / 登记 / 派发）。"""
    from backend.services import job_ticket

    return job_ticket


def _ticket(
    owner_id: int,
    request: dict[str, Any],
    *,
    kind: str = "prd",
    solution_id: int | None = None,
    **kwargs: Any,
):
    """走公开正门造一张单①。"""
    return _ticket_module().build_job_ticket(
        owner_id=owner_id,
        solution_id=solution_id,
        kind=kind,
        request=request,
        **kwargs,
    )


def _end_to_end(
    owner_id: int,
    request: dict[str, Any],
    *,
    kind: str = "prd",
    solution_id: int | None = None,
    **kwargs: Any,
):
    """一张最小任务走通「造单 → 登记 → 派发 → 订阅到结束」。"""
    module = _ticket_module()

    async def scenario():
        draft = module.build_job_ticket(
            owner_id=owner_id,
            solution_id=solution_id,
            kind=kind,
            request=request,
            **kwargs,
        )
        registered = module.register_job_ticket(draft)
        module.start_registered_job(registered)
        events = [event async for event in job_service.open_stream(owner_id, registered.job_id)]
        return draft, registered, events

    return asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 一、一张最小任务走通主接缝
# ---------------------------------------------------------------------------


def test_three_steps_run_one_minimal_job_to_completion(alice, monkeypatch):
    """造单 → 登记 → 派发 → 订阅：事件形状、终态与回执与旧形状一致。

    先 snapshot 后 live；`done` 带权威正文；`run_summary` 收尾；DB 行是 completed。
    """
    _use_prd_llm(monkeypatch, ["第一章正文"])
    draft, registered, events = _end_to_end(
        alice, {"requirements": "能跑完", "product_name": "做个最小任务"}
    )

    assert draft is not registered
    assert registered.job_id > 0
    assert [event.type for event in events][0] == "snapshot"
    assert any(event.type == "done" for event in events)
    assert any(event.type == "chunk" for event in events)
    assert events[-1].type == "run_summary"
    assert events[-1].data["status"] == "completed"
    assert events[-1].data["kind"] == "prd"
    assert hasattr(events[0], "model_dump")  # 仍是既有 SSE 契约对象
    assert _raw(registered.job_id)["status"] == "completed"


# ---------------------------------------------------------------------------
# 二、两段单各自不可变；单②是新对象、唯一权威
# ---------------------------------------------------------------------------


def test_both_tickets_are_immutable_and_second_is_new_authority(alice):
    """两张单各自 frozen（连嵌套容器也不可写）；单①无 id；单②带 id 且是另一对象。"""
    draft = _ticket(alice, {"summary": "不可变", "history": [{"role": "user"}]})
    registered = _ticket_module().register_job_ticket(draft)

    assert draft is not registered
    for ticket in (draft, registered):
        with pytest.raises(FrozenInstanceError):
            setattr(ticket, fields(ticket)[0].name, "换个值")
        with pytest.raises(TypeError):
            ticket.request_params["悄悄改"] = True
        with pytest.raises(TypeError):
            ticket.config_snapshot["悄悄改"] = True
        with pytest.raises((TypeError, AttributeError)):
            ticket.skill_refs.append({"name": "后加的包"})
    assert not any(field.name == "job_id" for field in fields(draft))
    assert registered.job_id > 0
    assert registered.request_params == draft.request_params
    assert registered.config_snapshot is draft.config_snapshot
    assert registered.skill_refs is draft.skill_refs


def test_dispatch_only_accepts_second_ticket(alice, monkeypatch):
    """派发只认单②：把单①递进去应当被拒绝，且不产生任何后台任务。"""
    _use_prd_llm(monkeypatch, ["不该跑"])
    draft = _ticket(alice, {"summary": "别被派发"})

    with pytest.raises(TypeError):
        _ticket_module().start_registered_job(draft)  # type: ignore[arg-type]
    assert _row_count() == 0
    assert job_service._RUNTIMES == {}  # noqa: SLF001 - 只断言「没有任务被派发」


# ---------------------------------------------------------------------------
# 三、造单是公开正门：不落库、不顶替、不发事件、不查方案
# ---------------------------------------------------------------------------


def test_build_has_no_side_effects_and_does_not_check_solution(alice, monkeypatch):
    """造单不查方案是否还在（方案不存在也能造）、不落库、不顶替、不发事件。"""
    module = _ticket_module()
    with session_scope() as session:
        old = GenerationJobRepository(session).create(
            alice, "prd", None, request_params={"summary": "旧任务"}
        )
        old_id = int(old.id or 0)

    called = {"solution_exists": 0, "publish": 0}

    def _forbidden_solution(*args, **kwargs):  # noqa: ANN002, ANN003
        called["solution_exists"] += 1
        raise AssertionError("造单阶段不许查方案是否还在")

    def _count_publish(*args, **kwargs):  # noqa: ANN002, ANN003
        called["publish"] += 1

    # 两条登记期会走到的「查方案」路径一起拦住：造单阶段一条都不该碰。
    from backend.repositories.solution_repo import SolutionRepository
    from backend.services import solution_service

    monkeypatch.setattr(solution_service, "solution_exists", _forbidden_solution)
    monkeypatch.setattr(SolutionRepository, "exists", _forbidden_solution)
    monkeypatch.setattr(job_bus, "publish", _count_publish)

    draft = module.build_job_ticket(
        owner_id=alice,
        solution_id=999_999,  # 不存在的方案：造单阶段不许查、不许拒
        kind="prd",
        request={"summary": "造单无副作用"},
    )

    assert called == {"solution_exists": 0, "publish": 0}
    assert _row_count() == 1  # 只有先前那一行，造单没落库
    assert _raw(old_id)["status"] == "running"  # 没顶替旧任务
    assert draft.solution_id == 999_999


def test_register_checks_solution_on_the_spot(client, alice):
    """「方案是否仍存在」是登记当场查询：造完单再删方案，登记必须失败。"""
    solution_id = _seed_solution(client)
    draft = _ticket(alice, {"summary": "方案会被删"}, solution_id=solution_id)

    with connect() as conn:
        conn.execute("DELETE FROM solutions WHERE id = ?", (solution_id,))
        conn.commit()

    with pytest.raises(AppError) as caught:
        _ticket_module().register_job_ticket(draft)
    assert caught.value.code is ErrorCode.SOLUTION_NOT_FOUND
    assert _row_count() == 0  # 失败没有半截行


# ---------------------------------------------------------------------------
# 四、技能包身份造单时定格（数据，不是能力）
# ---------------------------------------------------------------------------


def test_skill_identity_is_frozen_at_build_time(alice, monkeypatch):
    """技能包清单在造单时读一次；之后换读盘结果，单①/单②/回执都还是定格值。"""
    calls: list[str] = []
    frozen = {"name": "ticket-skill", "version": "3.2.1", "fingerprint": "abcabc"}

    def _describe(kind: str):
        calls.append(kind)
        return dict(frozen)

    # 复用 #34 的技能包身份判断：它在本模块按目标产物查一次注册表。
    monkeypatch.setattr(job_service, "describe_kind", _describe)
    _use_prd_llm(monkeypatch, ["技能包定格"])

    draft, registered, events = _end_to_end(
        alice, {"requirements": "定格", "product_name": "技能包身份"}
    )
    assert calls == ["prd"]  # 只在造单那一次算好，派发不再重算
    assert list(draft.skill_refs) == [frozen]
    assert list(registered.skill_refs) == [frozen]
    assert events[-1].data["injected_skills"] == [frozen]
    assert json.loads(_raw(registered.job_id)["skill_refs"]) == [frozen]


def test_build_is_the_public_door_for_fake_tickets(alice):
    """测试造假任务单走公开正门：造两张单互不影响、都不落库，不开私有缝。"""
    first = _ticket(alice, {"summary": "第一张"})
    second = _ticket(alice, {"summary": "第二张"})
    assert first is not second
    assert first.request_params["summary"] == "第一张"
    assert second.request_params["summary"] == "第二张"
    assert _row_count() == 0


def test_build_accepts_fake_config_environment(alice, monkeypatch):
    """「配置/供应商快照」是造单时传入的环境：假配置不查真库也能定格随单走。"""
    from backend.core import runtime_config as rc
    from backend.core.providers import ProviderSpec
    from backend.services import job_ticket

    fake_config = rc.RuntimeConfig(
        settings=rc.get_settings(),
        active_provider=ProviderSpec(
            id="cfg_fake",
            name="假配置",
            protocol="ark",
            base_url="",
            value_token="",
            model="model-fake",
            max_tokens=0,
        ),
        source="fake",
        revision="fake-revision",
    )

    def _no_real_config():
        raise AssertionError("传入假配置时造单不应再读进程内配置")

    monkeypatch.setattr(rc, "get_runtime_config", _no_real_config)
    draft = job_ticket.build_job_ticket(
        owner_id=alice,
        solution_id=None,
        kind="prd",
        request={"requirements": "假配置"},
        config=fake_config,
    )
    frozen = draft.config_snapshot["active_provider"]
    assert frozen["provider_config_id"] == "cfg_fake"
    assert frozen["provider_model"] == "model-fake"
    assert draft.config_snapshot["revision"] == "fake-revision"
    assert draft.runtime_config is fake_config
    assert _row_count() == 0


# ---------------------------------------------------------------------------
# 五、配置/供应商快照一次定格；生成中途改后台参数不漂移
# ---------------------------------------------------------------------------


def test_config_freeze_does_not_drift_when_admin_changes_config(alice, monkeypatch):
    """造单定格配置 A，随后后台切到 B：派发侧的计量器、回执与 DB 行仍是 A。

    若派发再自行 `get_runtime_config()`，四格就会跟着 B 漂移；这里跨公开接缝断言
    三个出口（计量器 / `run_summary` / DB 投影）与单①定格的四格同源。
    """
    import backend.core.runtime_config as rc
    from conftest import system_config

    _seed_provider(provider_id="cfg_ticket_a", name="配置 A", model="model-A")
    _use_prd_llm(monkeypatch, ["配置不漂移"])
    module = _ticket_module()

    async def scenario():
        draft = module.build_job_ticket(
            owner_id=alice,
            solution_id=None,
            kind="prd",
            request={"requirements": "配置冻结"},
        )
        frozen = draft.config_snapshot["active_provider"]
        assert frozen["provider_config_id"] == "cfg_ticket_a"
        assert frozen["provider_model"] == "model-A"

        # 造单之后、派发之前改后台配置：这一刻起进程内读到的应是 B。
        _seed_provider(provider_id="cfg_ticket_b", name="配置 B", model="model-B")
        assert rc.get_runtime_config().active_provider.model == "model-B"

        registered = module.register_job_ticket(draft)
        module.start_registered_job(registered)
        runtime = job_service._RUNTIMES[registered.job_id]  # noqa: SLF001 - 断言定格值
        assert runtime.meter.protocol == frozen["provider_protocol"]
        assert runtime.meter.config_id == frozen["provider_config_id"]
        assert runtime.meter.name == frozen["provider_name"]
        assert runtime.meter.model == frozen["provider_model"]

        events = [event async for event in job_service.open_stream(alice, registered.job_id)]
        return frozen, registered, events

    frozen, registered, events = asyncio.run(scenario())
    assert events[-1].data["protocol"] == frozen["provider_protocol"]
    assert events[-1].data["model"] == frozen["provider_model"]
    assert events[-1].data["provider_config_id"] == frozen["provider_config_id"]
    row = _raw(registered.job_id)
    assert json.loads(row["config_snapshot"])["active_provider"] == dict(frozen)
    assert row["provider_config_id"] == frozen["provider_config_id"]
    assert row["provider_model"] == frozen["provider_model"]
    # 后台确实切过配置（用例不是恒真）：B 现在是生效值。
    assert system_config.get_all().get(rc.ACTIVE_PROVIDER_KEY) == json.dumps("cfg_ticket_b")


def test_build_accepts_fake_event_stream_environment(alice, monkeypatch):
    """「生成事件流来源」是造单时传入的环境：假流走公开正门也能派发完。"""
    from backend.core.contracts import StreamEvent

    async def fake_stream(kind, request):
        yield StreamEvent(type="chunk", content="假流正文")
        yield StreamEvent(
            type="done", content="", data={"reply": "假流正文", "partial": False}
        )

    draft, registered, events = _end_to_end(
        alice, {"summary": "假事件流"}, event_stream_source=fake_stream
    )
    assert any(event.type == "done" for event in events)
    assert events[-1].type == "run_summary"
    assert _raw(registered.job_id)["status"] == "completed"
    assert draft is not registered


# ---------------------------------------------------------------------------
# 六、chat 四个派生字段明写在单②；输入轮次唯一约束仍生效
# ---------------------------------------------------------------------------


def test_chat_four_derived_fields_are_written_on_second_ticket(client, alice, monkeypatch):
    """任务 id、输入轮次 id、归属用户、方案四个字段明写在单②，且不改调用方 dict。"""
    solution_id = _seed_solution(client)
    _use_chat_llm(monkeypatch, ["你好，这是回复。"])
    request = {
        "stage": "requirement",
        "chat_mode": "continue",
        "content": "请继续",
        "form_data": {},
    }
    original = dict(request)

    draft, registered, events = _end_to_end(
        alice, request, kind="chat", solution_id=solution_id
    )

    named = {field.name for field in fields(registered)}
    assert {"job_id", "input_turn_id", "owner_id", "solution_id"} <= named
    assert registered.job_id > 0
    assert registered.input_turn_id > 0
    assert registered.owner_id == alice
    assert registered.solution_id == solution_id
    assert not any(field.name == "job_id" for field in fields(draft))
    assert request == original  # 不再原地改写调用方传入的字典
    assert any(event.type == "done" for event in events)
    assert events[-1].type == "run_summary"

    with session_scope() as session:
        turns = ChatTurnRepository(session).list_thread(
            alice, solution_id, "requirement"
        )
    assert [turn.role for turn in turns] == ["user", "assistant"]
    assert turns[0].job_id == registered.job_id
    assert turns[0].id == registered.input_turn_id
    assert turns[1].job_id == registered.job_id


def test_chat_input_turn_unique_constraint_still_holds(client, alice, monkeypatch):
    """同一任务同一角色只写一条轮次：重连/收尾撞车都不多写。"""
    solution_id = _seed_solution(client)
    _use_chat_llm(monkeypatch, ["回复一次"])
    draft, registered, _ = _end_to_end(
        alice,
        {"stage": "requirement", "chat_mode": "continue", "content": "一句用户输入"},
        kind="chat",
        solution_id=solution_id,
    )
    # 再写一次同 (job_id, role) 的输入轮次，仓储应回查到同一条 id。
    with session_scope() as session:
        again = ChatTurnRepository(session).create(
            alice,
            solution_id,
            stage="requirement",
            role="user",
            content="重复写入",
            job_id=registered.job_id,
        )
    assert again == registered.input_turn_id
    with session_scope() as session:
        turns = ChatTurnRepository(session).list_thread(
            alice, solution_id, "requirement"
        )
    assert sum(turn.role == "user" for turn in turns) == 1
    assert draft is not registered


# ---------------------------------------------------------------------------
# 七、登记是一个事务：中途失败无半截行、无单②、不派发
# ---------------------------------------------------------------------------


def test_registration_failure_leaves_no_half_running_row(client, alice, monkeypatch):
    """落库之后、写输入轮次之前抛错：整笔事务回滚，不留 running 行、不派发。"""
    from backend.services import job_registration

    solution_id = _seed_solution(client)
    draft = _ticket(
        alice,
        {"stage": "requirement", "chat_mode": "continue", "content": "会被打断"},
        kind="chat",
        solution_id=solution_id,
    )

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("登记中途失败")

    monkeypatch.setattr(job_registration.ChatTurnRepository, "create", _boom)

    with pytest.raises(RuntimeError, match="登记中途失败"):
        _ticket_module().register_job_ticket(draft)

    assert _row_count() == 0  # 没有半截 running 行
    assert job_service._RUNTIMES == {}  # noqa: SLF001 - 没有任务被派发
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM chat_turns").fetchone()[0] == 0


def test_registration_supersedes_old_job_in_db_and_memory(alice, monkeypatch):
    """登记顶替旧任务：库行 failed+SUPERSEDED，内存运行时也打 `superseded` 标记。"""
    from backend.services.llm_meter import RunMeter

    _use_prd_llm(monkeypatch, ["旧任务不该落文档"])
    # 造一个在跑的旧运行时（不经派发，避免后台任务自己收尾）。
    old_runtime = job_service.JobRuntime(
        job_id=7001,
        owner_id=alice,
        solution_id=None,
        kind="prd",
        meter=RunMeter(kind="prd"),
    )
    job_service._RUNTIMES[7001] = old_runtime  # noqa: SLF001
    with session_scope() as session:
        old = GenerationJobRepository(session).create(
            alice, "prd", None, request_params={"summary": "旧任务"}
        )
        old_id = int(old.id or 0)

    registered = _ticket_module().register_job_ticket(
        _ticket(alice, {"requirements": "新任务"})
    )

    assert registered.job_id != old_id
    old_row = _raw(old_id)
    assert old_row["status"] == "failed"
    assert old_row["error_code"] == "SUPERSEDED"
    assert old_runtime.superseded is True  # 双标记：内存那一半也在
    assert _raw(registered.job_id)["status"] == "running"


def test_registration_is_one_transaction_for_supersede_and_create(alice, monkeypatch):
    """顶替旧任务与落库在同一事务：中途失败时旧任务不被误标、新任务不留行。"""
    from backend.services import job_registration

    with session_scope() as session:
        old = GenerationJobRepository(session).create(
            alice, "prd", None, request_params={"summary": "旧任务"}
        )
        old_id = int(old.id or 0)

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("落库前失败")

    monkeypatch.setattr(job_registration.GenerationJobRepository, "create", _boom)
    draft = _ticket(alice, {"summary": "新任务"})
    with pytest.raises(RuntimeError, match="落库前失败"):
        _ticket_module().register_job_ticket(draft)

    assert _raw(old_id)["status"] == "running"  # 旧任务没被顶替
    assert _row_count() == 1


# ---------------------------------------------------------------------------
# 八、旧入口行为逐项不变
# ---------------------------------------------------------------------------


def test_legacy_entry_still_mutates_shared_request_dict(client, alice, monkeypatch):
    """旧 `create_job` + `dispatch` 仍可用：chat 的四个派生键照样原地注入共享 dict。"""
    solution_id = _seed_solution(client)
    _use_chat_llm(monkeypatch, ["旧入口照跑"])
    payload = {
        "stage": "requirement",
        "chat_mode": "continue",
        "content": "旧入口",
        "form_data": {},
    }

    async def scenario():
        job_id = job_service.create_job(
            owner_id=alice, solution_id=solution_id, kind="chat", request=payload
        )
        job_service.dispatch(
            job_id,
            owner_id=alice,
            solution_id=solution_id,
            kind="chat",
            request=payload,
        )
        return job_id, [event async for event in job_service.open_stream(alice, job_id)]

    job_id, events = asyncio.run(scenario())
    assert {"input_turn_id", "job_id", "owner_id", "solution_id"} <= payload.keys()
    assert payload["job_id"] == job_id
    assert payload["owner_id"] == alice
    assert payload["solution_id"] == solution_id
    assert payload["input_turn_id"] > 0
    assert any(event.type == "done" for event in events)
    assert events[-1].type == "run_summary"
    assert _raw(job_id)["status"] == "completed"


def test_legacy_document_entry_matches_new_shape_events(alice, monkeypatch):
    """同一最小任务分别走旧入口与新三步，事件类型序列与终态逐项相同。"""
    _use_prd_llm(monkeypatch, ["同一段正文"])

    async def legacy():
        request = {"requirements": "逐项相同", "product_name": "对照"}
        job_id = job_service.create_job(
            owner_id=alice, solution_id=None, kind="prd", request=request
        )
        job_service.dispatch(
            job_id,
            owner_id=alice,
            solution_id=None,
            kind="prd",
            request=request,
        )
        return [
            event.type
            async for event in job_service.open_stream(alice, job_id)
        ]

    legacy_types = asyncio.run(legacy())
    job_service._RUNTIMES.clear()  # noqa: SLF001
    _use_prd_llm(monkeypatch, ["同一段正文"])
    _, _, new_events = _end_to_end(alice, {"requirements": "逐项相同", "product_name": "对照"})

    assert [event.type for event in new_events] == legacy_types
    assert new_events[-1].data["status"] == "completed"
