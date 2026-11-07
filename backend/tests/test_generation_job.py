"""生成任务（generation_jobs）的契约测试。

覆盖两件事：
1. **任务与连接解耦**：后台任务如何产出、广播、持久化、收尾；刷新重连如何先收
   `snapshot` 补全正文再接 live。
2. **HTTP 面**：创建 / 查询 / 兜底列表 / 订阅的归属与鉴权（越权一律 404，未登录 401）。

最要紧的一条不变量是 `''.join(part.text) == done.reply`——前端预览与最终落库正文
必须同源，这在前端「刷新后按快照补全」时同样成立。

所有用例都在**离线零 token** 下运行：真实 LLM 工厂被替换成一调用即抛，
生成一律注入 `ScriptedLLM`，因此不会触网。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import pytest
from conftest import login, register, users

from backend.core.contracts import StreamEvent
from backend.core.errors import AppError, ErrorCode
from conftest import (
    job_cancel_running,
    job_create,
    job_fail,
    job_find_active,
    job_finish,
    job_get,
    job_list_by_owner,
    job_persist_progress,
    write_skill_package,
)
from backend.services import (
    document_service,
    document_store,
    job_drain,
    job_service,
    job_runtime,
    llm_factory,
    prd_section_writer,
    solution_service,
)
from backend.services.job_bus import job_bus
from backend.services.job_snapshot import parse_usage
from backend.services.job_ticket import start_registered_job
from backend.services.prd_section_writer import PRD_SECTIONS
from fakes import ScriptedLLM
from ticket_support import (
    build_and_register,
    first_snapshot_event,
    run_ticket_async,
    run_ticket_events,
    run_ticket_snapshot,
    run_ticket_to_completion,
)


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------


def _seed_provider() -> str:
    """放一条配置并设为使用中，返回 id。

    ⚠️ `.env` 不再是模型配置的来源：一条配置都没有时
    `active_provider` 是 `None`，回执里的 `protocol` / `model` 会是空串。
    而这两个字段正是「这次走了哪条配置」的唯一凭据，所以凡是要断言它们的用例
    都得先把配置落下来——这不是用例啰嗦，是真实前提。
    """
    from backend.core import runtime_config as rc
    from conftest import model_providers as ModelProviderRepository, system_config as SystemConfigRepository
    provider_id = "cfg_jobtest01"
    ModelProviderRepository.create(
        provider_id=provider_id,
        name="用例配置",
        protocol="ark",
        value_token="",
        default_model="doubao-test",
    )
    SystemConfigRepository.upsert_many(
        {rc.ACTIVE_PROVIDER_KEY: json.dumps(provider_id)}, updated_by=None
    )
    rc.invalidate()
    return provider_id



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
    """每个用例前后清空进程内运行时与任务集合，避免用例互相污染。

    排空标志一并复位：它是**进程内单例**，上一个用例排空完之后若不复位，
    下一个用例的就绪探针会继承那个 503——那是「测试顺序污染」，不是被测行为。
    """
    job_runtime._RUNTIMES.clear()  # noqa: SLF001
    job_runtime._TASKS.clear()  # noqa: SLF001
    job_bus._subscribers.clear()  # noqa: SLF001
    job_drain.drain_controller.reset()
    yield
    job_runtime._RUNTIMES.clear()  # noqa: SLF001
    job_runtime._TASKS.clear()  # noqa: SLF001
    job_bus._subscribers.clear()  # noqa: SLF001
    job_drain.drain_controller.reset()


def _alice_id() -> int:
    """取已注册用户 alice 的 id（generation_jobs.owner_id 有外键，必须是真实用户）。"""
    from backend.services import auth_service

    user = users.get_by_username("alice")
    assert user is not None
    return int(user["id"])


@pytest.fixture
def alice(client):
    """注册 alice 并返回其 id（顺带保证 DB 已初始化、外键可用）。"""
    register(client)
    return _alice_id()


def _fake_service(monkeypatch, llm) -> None:
    """把模块级文档服务的实例换成注入了假 LLM 的实例。"""
    monkeypatch.setattr(
        document_service,
        "get_service",
        lambda: document_service.DocumentService(llm=llm),
    )


def _run(coro):
    """在新事件循环里跑一段协程。"""
    return asyncio.run(coro)


def _parts(events):
    return [event for event in events if event.type == "part"]


def _create_solution(client) -> int:
    """建一份真实方案并返回 id。

    落文档要求 `documents.solution_id` 指向一个**真实且属于本人**的方案
    （仓储靠 `JOIN solutions` 判归属），所以涉及落库的用例不能像早期那样随手
    写一个不存在的 `solution_id=5`。
    """
    response = client.post(
        "/api/solutions",
        json={"title": "演示方案", "mode": "structured", "stage": "review-prd", "data": "{}"},
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _run_job(
    owner_id: int,
    solution_id: int | None,
    kind: str,
    request: dict,
    resolved_inputs: dict | None = None,
) -> tuple[int, list]:
    """造单 → 登记 → 派发 → 一路订阅到结束，返回 (job_id, 全部事件)。

    `request` 只放「调用方发了什么」；上游正文 / 需求正文放 `resolved_inputs`
    （生产由路由按方案解析，票 #54）。假环境（生成事件流来源、配置快照）在造单时
    传入；这里用生产真值，模型边界仍用既有的离线假 LLM（零 token）。
    """
    return run_ticket_to_completion(
        owner_id, solution_id, kind, request, resolved_inputs=resolved_inputs
    )


def _create_solution_with_requirements(client) -> int:
    """建一份带需求输入的方案：PRD 任务的上游由服务端从这一份取。"""
    response = client.post(
        "/api/solutions",
        json={
            "title": "演示方案",
            "mode": "structured",
            "stage": "review-prd",
            "state": {
                "entry_mode": "prd-quick",
                "quick_product_name": "演示产品",
                "quick_requirements": "做一个演示产品",
            },
        },
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


# ---------------------------------------------------------------------------
# 任务编排：与连接解耦、快照补全、契约不变量
# ---------------------------------------------------------------------------


def test_stream_starts_with_snapshot_and_parts_reassemble_into_reply(client, alice, monkeypatch):
    """端到端：首事件是 snapshot；`part` 按序拼接即 done.reply；任务落库为 completed。"""
    _fake_service(monkeypatch, ScriptedLLM(["章节正文"]))
    # 方案必须真实存在：收尾会校验「方案还在不在」，挂在不存在方案上的任务不再算成功
    solution_id = _create_solution(client)
    _seed_provider()

    job_id, events = _run_job(
        alice,
        solution_id,
        "prd",
        {},
        resolved_inputs={"requirements": "做一个演示产品", "product_name": "演示产品"},
    )

    # 首事件必为 snapshot（重连补全正文的落点）
    assert events[0].type == "snapshot"
    assert events[0].data["status"] == "running"

    parts = _parts(events)
    # 文档头 1 段 + 每章 1 段
    assert len(parts) == len(PRD_SECTIONS) + 1
    done = next(event for event in events if event.type == "done")
    # 契约：按 index 排序后拼接 == 最终正文（并行时 part 可以乱序到达）
    ordered = sorted(parts, key=lambda part: part.data.get("index", -1))
    assert "".join(part.data["text"] for part in ordered).strip() == done.data["reply"]

    # 收尾：run_summary 的完整契约（用量汇总与任务状态并作一份）
    summary = events[-1]
    assert summary.type == "run_summary"
    data = summary.data
    assert data["kind"] == "prd"
    assert data["status"] == "completed"
    assert isinstance(data["duration_ms"], int)
    # 假 LLM 不带 usage_metadata，token 按零计；但调用次数与耗时照记（不是「没计量」）
    assert data["total_calls"] >= 1
    assert data["prompt_tokens"] == 0
    assert data["completion_tokens"] == 0
    # PRD 生成必须在回执上留下技能身份：这次用的是技能包还是内置兜底提示词，
    # 这个差别以往是静默的（债务 D8），现在随 run_summary 事件与 usage 列一起可见。
    skills = data["injected_skills"]
    assert len(skills) == 1
    assert skills[0]["name"] == "prd-generator"
    assert skills[0]["version"]
    # 指纹由三份资源内容算出，长度固定；改了模板它就变（不必依赖人工改版本号）
    assert len(skills[0]["fingerprint"]) == 12
    # 供应商身份拆成四个字段：协议查限值、配置 id 定位到具体一条、名字给人读、型号记账。
    # 合成一个字符串就解释不了「同一协议两条配置到底走的哪条」。
    assert isinstance(data["protocol"], str) and data["protocol"]
    assert isinstance(data["provider_config_id"], str)
    assert isinstance(data["provider_name"], str)
    assert isinstance(data["model"], str) and data["model"]
    # 直连服务、没有 HTTP 请求上下文，request_id 为空（不抛异常）
    assert data["request_id"] == ""
    # steps 覆盖流水线的多个阶段，字段与 StepUsage 一致；步骤名由 kind + 阶段推出
    assert data["steps"]
    for step in data["steps"]:
        assert set(step) == {
            "step",
            "calls",
            "failed_calls",
            "prompt_tokens",
            "completion_tokens",
            "duration_ms",
        }
        assert step["calls"] >= 1
        assert step["step"].startswith("prd")
    # 分步调用次数之和 == 总调用次数
    assert sum(step["calls"] for step in data["steps"]) == data["total_calls"]

    # 落库：完成态 + 正文与 part 拼接一致
    row = job_get(alice, job_id)
    assert row is not None
    assert row["status"] == "completed"
    assert row["draft_content"] == "".join(part.data["text"] for part in parts)
    # 用量与事件同源：落库的 usage 就是流里那份 run_summary，前端「看到的」与「重连读到的」一致
    assert row["usage"] is not None
    assert json.loads(row["usage"]) == data


def test_reconnect_after_unsubscribe_completes_remaining(client, alice, monkeypatch):
    """中途退订后重连：先收快照（含已产出正文），再续上 live 直到 done。"""
    _fake_service(monkeypatch, ScriptedLLM(["章节正文"]))
    # 方案必须真实存在：收尾要确认「方案还在」，否则任务会被判成取消态
    # （那样本用例就会在不知情的情况下走「方案已消失」那条分支）
    solution_id = _create_solution(client)

    async def scenario() -> tuple[list, list]:
        registered = build_and_register(
            owner_id=alice,
            solution_id=solution_id,
            kind="prd",
            request={"requirements": "做一个演示产品", "product_name": "演示产品"},
        )
        start_registered_job(registered)

        first: list = []
        async for event in job_service.open_stream(alice, registered.job_id):
            first.append(event)
            if len(_parts(first)) >= 2:  # 拿到文档头 + 第一章后断线
                break

        # 断线重连：同一协程内立即重订阅，任务尚未跑完，仍能续流
        second = [event async for event in job_service.open_stream(alice, registered.job_id)]
        return first, second

    first, second = _run(scenario())

    assert first[0].type == "snapshot"
    assert len(_parts(first)) >= 2

    # 重连的首事件同样是 snapshot，且带上了断线前已产出的定稿正文
    assert second[0].type == "snapshot"
    assert second[0].data["finalized_text"].strip() != ""
    # 续流直到 done，正文完整
    done = next(event for event in second if event.type == "done")
    assert done.data["reply"].startswith("# 演示产品 产品需求文档")


def test_resubscribe_after_completion_sends_snapshot_then_summary(client, alice, monkeypatch):
    """任务已完成后再订阅：发 snapshot 补全正文，**再补一份 run_summary**，随即结束。

    补发是必须的：收尾的 `run_summary` 在任务结束当刻广播，只在当刻连着的人收得到；
    刷新页面或订阅晚于收尾，就会漏掉它——那样「这次花了多少 token」只在没人看的时候存在。
    终态任务的用量已落库，重连时按库补齐即可。
    """
    _fake_service(monkeypatch, ScriptedLLM(["章节正文"]))
    # 方案必须真实存在：收尾要确认「方案还在」，否则任务会被判成取消态
    solution_id = _create_solution(client)

    async def scenario() -> list:
        registered = build_and_register(
            owner_id=alice,
            solution_id=solution_id,
            kind="prd",
            request={"requirements": "做一个演示产品", "product_name": "演示产品"},
        )
        start_registered_job(registered)
        # 第一次订阅跑到任务结束
        async for _ in job_service.open_stream(alice, registered.job_id):
            pass
        # 第二次订阅：snapshot + run_summary 后返回，不挂起
        return [
            event
            async for event in job_service.open_stream(alice, registered.job_id)
        ]

    events = _run(scenario())

    assert [event.type for event in events] == ["snapshot", "run_summary"]
    assert events[0].data["status"] == "completed"
    assert events[0].data["finalized_text"].startswith("# 演示产品")
    # 重连补发的汇总与落库同源
    assert events[1].data["status"] == "completed"
    assert events[1].data["kind"] == "prd"
    assert events[1].data["total_calls"] >= 1


def test_failure_event_marks_job_failed_and_keeps_partial(alice, monkeypatch):
    """中途断流：任务置 failed，error_code/error_message 到位，已产出的半截正文保留。"""
    # 第一段吐完即断
    _fake_service(monkeypatch, ScriptedLLM(["第一段正文", "第二段"], raise_at=1))

    job_id, events = _run_job(
        alice,
        None,
        "prd",
        {},
        resolved_inputs={"requirements": "做一个演示产品", "product_name": "演示产品"},
    )

    error = next(event for event in events if event.type == "error")
    assert error.code == "LLM_UPSTREAM_ERROR"
    assert "生成失败" in error.content

    # 收尾仍发 run_summary（status=failed）
    assert events[-1].type == "run_summary"
    assert events[-1].data["status"] == "failed"

    row = job_get(alice, job_id)
    assert row is not None
    assert row["status"] == "failed"
    assert row["error_code"] == "LLM_UPSTREAM_ERROR"
    assert row["error_message"].strip() != ""
    # 半截正文（文档头 + 已流出的第一段）保留
    assert row["draft_content"].strip() != ""


def test_late_subscriber_gets_error_when_job_already_failed(alice, monkeypatch):
    """订阅晚于失败：也必须收到 `error` 事件。

    真实场景：没有模型配置时 `get_llm()` 在建任务后**立刻**抛错，任务往往在浏览器
    来得及订阅之前就已失败。收尾那一刻广播的 `error` 没有听众；若不再补发一条，
    前端只会看到「流正常结束、正文为空」，把它当成生成成功（空内容）——这正是
    「只显示成功、没有任何生成内容」那个 bug。`open_stream` 因此在快照后补发原因。
    """

    async def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("还没有任何模型配置。请先在管理后台「模型配置」页新增一条")
        yield  # pragma: no cover - 有 yield 才是 async generator，@ 第一次迭代即抛

    async def scenario() -> list:
        # 假事件流在造单时传入：无模型配置那种「一进流就抛」的失败分支。
        registered = build_and_register(
            owner_id=alice,
            solution_id=None,
            kind="prd",
            request={"requirements": "随便写点", "product_name": "演示"},
            event_stream_source=lambda kind, request: _boom(),
        )
        start_registered_job(registered)
        # 等任务彻底收尾：用公开查询的终态作判据，不探内部登记表。
        for _ in range(200):
            row = job_get(alice, registered.job_id)
            if row is not None and row["status"] != "running":
                break
            await asyncio.sleep(0.005)
        return [
            event
            async for event in job_service.open_stream(alice, registered.job_id)
        ]

    events = _run(scenario())

    assert events[0].type == "snapshot"
    assert events[0].data["status"] == "failed"
    error = next(event for event in events if event.type == "error")
    assert "模型配置" in error.content
    # 收尾汇总照旧补发
    assert events[-1].type == "run_summary"
    assert events[-1].data["status"] == "failed"


def test_open_stream_rejects_other_owner(alice, monkeypatch):
    """owner 不符时不泄内容：退化为失败/未命中的 404 语义（流内不返回任何事件）。"""

    async def scenario() -> list:
        registered = build_and_register(
            owner_id=alice, solution_id=None, kind="prompts", request={}
        )
        # 用另一个不存在的 owner 订阅：既非内存运行时归属，DB 也查不到
        return [
            event
            async for event in job_service.open_stream(alice + 999, registered.job_id)
        ]

    assert _run(scenario()) == []


def test_offline_zero_token(alice, monkeypatch):
    """硬约束：整个生成链路在真实 LLM 工厂一调用即抛的前提下仍能跑完。"""
    calls = {"count": 0}

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        calls["count"] += 1
        raise AssertionError("触网")

    monkeypatch.setattr(document_service, "get_llm", _boom)
    monkeypatch.setattr(prd_section_writer, "get_llm", _boom)
    monkeypatch.setattr(llm_factory, "get_llm", _boom)
    _fake_service(monkeypatch, ScriptedLLM(["正文"]))

    async def scenario() -> int:
        registered, _ = await run_ticket_async(
            owner_id=alice,
            solution_id=None,
            kind="prd",
            request={"requirements": "做一个演示产品"},
        )
        return registered.job_id

    job_id = _run(scenario())
    assert calls["count"] == 0
    row = job_get(alice, job_id)
    assert row is not None and row["status"] == "completed"


# ---------------------------------------------------------------------------
# 仓储：登记顶替 / 目标产物落列 / reap / 归属
# ---------------------------------------------------------------------------


def test_new_job_supersedes_previous_running(alice):
    """登记：同一 owner + solution + kind 至多一个 running：登记前旧 running 被取代。

    跨公开动作断言「顶替库行」：两次 `build_and_register` 之后旧行 `failed +
    SUPERSEDED`、新行 running。这条语义由「登记」承担，不再有第二个建任务入口。
    """
    old = build_and_register(owner_id=alice, solution_id=None, kind="prd", request={}).job_id
    new = build_and_register(owner_id=alice, solution_id=None, kind="prd", request={}).job_id

    assert old != new
    old_row = job_get(alice, old)
    assert old_row is not None
    assert old_row["status"] == "failed"
    # 有明确原因，不是笼统的 INTERNAL_ERROR：这个任务没出错，是被人接手了
    assert old_row["error_code"] == "SUPERSEDED"
    assert old_row["error_message"] == "被新任务取代"

    # 新登记的是 running
    assert job_get(alice, new)["status"] == "running"


def test_register_supersedes_old_job_in_db_and_memory(client, alice):
    """登记顶替旧任务：库行 `failed + SUPERSEDED`，且旧任务收尾不写文档。

    只把库里那一行改成 failed 是不够的：内存里的旧运行时并不知道自己被取代，
    它收尾时照样写文档，把旧稿盖在新稿上——用户看到「新任务跑完了，
    正文却是上一版的」，不报错、只是内容对不上。本用例跨公开动作断言「双标记」：
    旧任务在**新任务登记那一刻**被标记，随后收尾落 `SUPERSEDED` 且不写文档。
    """
    from backend.core.contracts import StreamEvent

    solution_id = _create_solution(client)
    hold = asyncio.Event()
    first_seen = asyncio.Event()

    async def stream(kind, request):
        yield StreamEvent(type="chunk", content="旧稿", data={})
        first_seen.set()
        await hold.wait()
        yield StreamEvent(type="done", content="", data={"reply": "# 旧稿"})

    async def scenario() -> tuple[int, int, list]:
        old = build_and_register(
            owner_id=alice,
            solution_id=solution_id,
            kind="prd",
            request={"requirements": "旧任务"},
            event_stream_source=stream,
        )
        start_registered_job(old)
        await asyncio.wait_for(first_seen.wait(), timeout=5)

        # 新任务登记接手：库里的旧行与内存里的旧运行时都要被标记。
        new = build_and_register(
            owner_id=alice, solution_id=solution_id, kind="prd", request={"requirements": "新任务"}
        ).job_id
        hold.set()
        events = [event async for event in job_service.open_stream(alice, old.job_id)]
        return old.job_id, new, events

    old_id, new_id, events = _run(scenario())

    assert old_id != new_id
    assert any(event.type == "run_summary" for event in events)
    row = job_get(alice, old_id)
    assert row is not None
    assert row["status"] == "failed"
    assert row["error_code"] == "SUPERSEDED"
    # 关键：正文没有被写进 documents（这一份是别人要写的位置）
    assert document_store.get_document(alice, solution_id, "prd") is None


def test_find_active_ignores_other_solution_and_kind(client, alice):
    """`find_active` 精确匹配 owner + solution + kind，且只认 running。

    登记语义：目标产物落列与活跃行都由「登记」这一个事务产生。
    """
    first = _create_solution(client)
    second = _create_solution(client)
    build_and_register(
        owner_id=alice, solution_id=first, kind="prd", request={}
    )
    assert job_find_active(alice, solution_id=first, kind="prd") is not None
    assert job_find_active(alice, solution_id=second, kind="prd") is None
    assert job_find_active(alice, solution_id=first, kind="prompts") is None
    # solution_id=None 的匹配
    none_job = build_and_register(
        owner_id=alice, solution_id=None, kind="prompts", request={}
    ).job_id
    active = job_find_active(alice, solution_id=None, kind="prompts")
    assert active is not None and active["id"] == none_job


def test_reap_orphans_marks_running_as_failed(alice):
    """启动回收：上次残留的 running 全部置 failed。"""
    job_id = job_create(alice, "prd", None)
    assert job_get(alice, job_id)["status"] == "running"

    job_service.reap_orphans()

    row = job_get(alice, job_id)
    assert row["status"] == "failed"
    assert row["error_code"] == "INTERNAL_ERROR"
    assert row["error_message"] == "服务重启，任务已中断"


def test_owner_isolation_in_repository(alice):
    """仓储层归属隔离：别人的任务等同不存在。"""
    job_id = job_create(alice, "prd", None)
    assert job_get(alice + 999, job_id) is None
    assert job_list_by_owner(alice + 999) == []


# ---------------------------------------------------------------------------
# HTTP 面
# ---------------------------------------------------------------------------


def test_create_job_returns_201_and_persists(client, monkeypatch):
    """POST /api/jobs 返回 201 与 job_id，并在库里落一行。"""
    register(client)
    uid = _alice_id()
    _fake_service(monkeypatch, ScriptedLLM(["正文"]))
    solution_id = _create_solution_with_requirements(client)

    response = client.post(
        "/api/jobs",
        json={"kind": "prd", "solution_id": solution_id},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["job_id"] > 0
    assert body["status"] == "running"

    row = job_get(uid, body["job_id"])
    assert row is not None
    assert row["kind"] == "prd"
    # prd 任务会计算并落输入指纹（供后续续传 / 缓存判断输入是否变化）
    assert row["input_fingerprint"]


def test_create_job_validates_required_input_by_kind(client):
    """按 kind 校验必填：缺输入返回 400 INVALID_REQUEST（不是 422）。

    文档类任务的前两条现在都栽在**缺方案**上（输入由服务端按方案取），
    后两条仍缺动作自己的输入。缺材料（方案存在、库里没有上游）的 400
    由 `test_job_contract.py` 逐个种类覆盖。
    """
    register(client)

    for payload in (
        {"kind": "prd"},  # 缺方案（需求输入也由服务端取）
        {"kind": "api_docs"},  # 缺方案（上游 PRD 也由服务端取）
        {"kind": "prompts"},  # 缺方案
        {"kind": "optimize", "document": "x"},  # 缺 instruction
        {"kind": "optimize", "instruction": "改一下"},  # 缺 document
    ):
        response = client.post("/api/jobs", json=payload)
        assert response.status_code == 400, payload
        assert response.json()["code"] == "INVALID_REQUEST"


def test_create_prompts_job_accepts_optional_api_docs(client, monkeypatch):
    """接口文档是**可选**材料：方案上存了两份就都用，只有 PRD 也能建任务。

    「我有 PRD + 接口文档」入口比「我有 PRD」多一份材料，两者走的是同一个 `prompts`
    任务类型。材料由服务端从方案上取（票 #54），请求体里不再有它们。
    """
    register(client)
    _fake_service(monkeypatch, ScriptedLLM(["# 提示词套件"]))
    solution_id = client.post(
        "/api/solutions",
        json={
            "title": "导入方案",
            "state": {
                "entry_mode": "prd-and-api-paste",
                "paste_prd_text": "# PRD 正文",
                "paste_api_docs_text": "# 接口文档正文",
            },
        },
    )
    assert solution_id.status_code == 201, solution_id.text

    response = client.post(
        "/api/jobs",
        json={"kind": "prompts", "solution_id": int(solution_id.json()["id"])},
    )

    assert response.status_code == 201, response.text
    assert response.json()["job_id"] > 0


def test_get_job_status(client):
    """GET /api/jobs/{id} 返回状态摘要，且不含 draft_content。"""
    register(client)
    uid = _alice_id()
    job_id = job_create(uid, "prompts", None)

    response = client.get(f"/api/jobs/{job_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == job_id
    assert body["kind"] == "prompts"
    assert body["status"] == "running"
    assert "draft_content" not in body


def test_missing_and_cross_owner_jobs_return_404(client):
    """越权与不存在一律 404，两者不可区分。"""
    register(client, username="alice")
    uid = _alice_id()
    alice_job = job_create(uid, "prd", None)
    client.post("/api/auth/logout")

    register(client, username="bob")

    assert client.get(f"/api/jobs/{alice_job}").status_code == 404
    assert client.get(f"/api/jobs/{alice_job}/stream").status_code == 404
    assert client.get("/api/jobs/999999").status_code == 404
    assert client.get("/api/jobs/999999/stream").status_code == 404


def test_list_jobs_filters_by_status_and_kind(client):
    """GET /api/jobs 兜底找回：按 status / kind 过滤，命中 running。"""
    register(client)
    uid = _alice_id()

    running_prd = job_create(uid, "prd", None)
    running_prompts = job_create(uid, "prompts", None)
    finished = job_create(uid, "prd", None)
    job_finish(
        uid, finished, draft_content="正文", completed_sections=[]
    )

    running = client.get("/api/jobs", params={"status": "running"})
    assert running.status_code == 200
    running_ids = [item["id"] for item in running.json()["jobs"]]
    assert running_prd in running_ids
    assert running_prompts in running_ids
    assert finished not in running_ids  # 已完成的不算 running

    only_prompts = client.get("/api/jobs", params={"kind": "prompts", "status": "running"})
    ids = [item["id"] for item in only_prompts.json()["jobs"]]
    assert ids == [running_prompts]


def test_create_job_without_solution_id_is_rejected(client):
    """`solution_id` 不再是可空的逃生口：文档类任务缺方案就是 400（票 #54）。

    从前它可空，靠请求体里的正文照样能跑；输入搬回服务端之后没有这条路了。
    「不留下空跑任务」由 `test_document_job_without_solution_is_not_created` 守。
    """
    register(client)

    response = client.post("/api/jobs", json={"kind": "prompts", "prd_content": "# PRD"})
    assert response.status_code == 400
    assert "方案" in response.json()["detail"]


def test_stream_endpoint_emits_snapshot_first(client, monkeypatch):
    """端到端：GET /api/jobs/{id}/stream 首事件是 snapshot（SSE 分帧走真实实现）。"""
    register(client)
    _fake_service(monkeypatch, ScriptedLLM(["正文"]))
    solution_id = _create_solution_with_requirements(client)

    created = client.post(
        "/api/jobs",
        json={"kind": "prd", "solution_id": solution_id},
    )
    job_id = created.json()["job_id"]

    response = client.get(f"/api/jobs/{job_id}/stream")
    assert response.status_code == 200

    payloads = [
        line[len("data:") :].strip()
        for line in response.text.splitlines()
        if line.startswith("data:")
    ]
    # 首个非 [DONE] 的数据帧是 snapshot
    first = next(payload for payload in payloads if payload and payload != "[DONE]")
    import json

    assert json.loads(first)["type"] == "snapshot"


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("get", "/api/jobs", None),
        ("post", "/api/jobs", {"kind": "prompts", "prd_content": "x"}),
        ("get", "/api/jobs/1", None),
        ("get", "/api/jobs/1/stream", None),
    ],
)
def test_jobs_endpoints_require_login(client, method, path, body):
    """四个任务接口都不在白名单里，未登录一律 401。"""
    call = getattr(client, method)
    response = call(path, json=body) if body is not None else call(path)
    assert response.status_code == 401
    assert response.json()["detail"] == "请先登录后再使用该功能"
    assert response.json()["code"] == "UNAUTHORIZED"


# ---------------------------------------------------------------------------
# 文档落库：任务成功收尾时把权威正文写进 `documents`
#
# 「文档所有权在后端」的核心断言：**前端不需要自己拼一份正文存回去**，
# 只要读 `documents` 就能拿到与 `done.reply` 逐字一致的内容。
# ---------------------------------------------------------------------------


def test_completed_prd_job_writes_document(client, monkeypatch):
    """PRD 生成成功后：`documents` 里出现一份与 `done.reply` 逐字一致的正文。"""
    register(client)
    uid = _alice_id()
    solution_id = _create_solution(client)
    _fake_service(monkeypatch, ScriptedLLM(["章节正文"]))

    _job_id, events = _run_job(
        uid,
        solution_id,
        "prd",
        {"requirements": "做一个演示产品", "product_name": "演示产品"},
    )
    done = next(event for event in events if event.type == "done")

    doc = document_store.get_document(uid, solution_id, "prd")
    assert doc is not None
    # 逐字一致：落库正文直接取自 done.reply，不是前端再拼一遍
    assert doc["content"] == done.data["reply"]
    # 回执与正文同批写入：打开方案就能看到「上次生成花了多少」
    assert doc["last_run_summary"] is not None
    assert doc["last_run_summary"]["kind"] == "prd"
    assert doc["updated_at"]


def test_completed_api_docs_job_writes_its_own_kind(client, monkeypatch):
    """接口文档任务写进 `api_docs`，不会串到 `prd` 那一份。"""
    register(client)
    uid = _alice_id()
    solution_id = _create_solution(client)
    _fake_service(monkeypatch, ScriptedLLM(["接口正文"]))

    _run_job(
        uid,
        solution_id,
        "api_docs",
        {},
        resolved_inputs={"prd_content": "# 演示产品 PRD"},
    )

    assert document_store.get_document(uid, solution_id, "api_docs") is not None
    assert document_store.get_document(uid, solution_id, "prd") is None


def test_failed_job_keeps_previous_document(client, monkeypatch):
    """失败的任务**不覆写**已有文档：半截输出不该顶掉上一版可用正文。"""
    register(client)
    uid = _alice_id()
    solution_id = _create_solution(client)
    document_store.save_document(uid, solution_id, "prd", "# 上一版 PRD")

    # 第一段吐完即断
    _fake_service(monkeypatch, ScriptedLLM(["第一段正文", "第二段"], raise_at=1))
    _job_id, events = _run_job(
        uid,
        solution_id,
        "prd",
        {},
        resolved_inputs={"requirements": "做一个演示产品"},
    )

    assert events[-1].data["status"] == "failed"
    doc = document_store.get_document(uid, solution_id, "prd")
    assert doc is not None
    assert doc["content"] == "# 上一版 PRD"


def test_document_job_without_solution_is_not_created(client, monkeypatch):
    """没有方案 → 400，而且**不留下一个空跑的任务**（票 #54 收口）。

    从前 `solution_id=None` 是「逃生口」：靠请求体里的正文照样能跑一趟生成。
    输入搬回服务端之后这条路没有了——方案是取上游的前提。HTTP 面上是 400、
    任务列表里一条记录都不留；同一断言在 `test_job_contract.py` 里按种类逐个守过。
    """
    register(client)
    uid = _alice_id()
    _fake_service(monkeypatch, ScriptedLLM(["正文"]))

    response = client.post("/api/jobs", json={"kind": "prompts"})

    assert response.status_code == 400
    assert "方案" in response.json()["detail"]
    assert job_list_by_owner(uid) == []


def test_regenerating_prd_keeps_downstream_documents(client, monkeypatch):
    """重新生成 PRD 后下游**保留**：它们是旧上游的产物，但不再被静默删除。

    旧行为是「PRD 换版 → 删下游」，那样会把旧内容与澄清线程一起抹掉。现在过期与否
    交给读取侧的 `stale` 标记（比较 `derived_from` 记录的内容指纹），用户可以自己
    决定重新生成还是继续用——删除会让旧内容与澄清线程一起消失。
    """
    register(client)
    uid = _alice_id()
    solution_id = _create_solution(client)
    document_store.save_document(uid, solution_id, "prd", "# 旧 PRD")
    document_store.save_document(uid, solution_id, "api_docs", "# 旧接口文档")
    document_store.save_document(uid, solution_id, "prompts", "# 旧提示词")

    _fake_service(monkeypatch, ScriptedLLM(["章节正文"]))
    _run_job(
        uid,
        solution_id,
        "prd",
        {},
        resolved_inputs={"requirements": "做一个演示产品", "product_name": "演示产品"},
    )

    kinds = {doc["kind"] for doc in document_store.list_documents(uid, solution_id)}
    assert kinds == {"prd", "api_docs", "prompts"}


def test_regenerating_api_docs_keeps_all_documents(client, monkeypatch):
    """重新生成接口文档同样不删除提示词；PRD 是它的上游，也不受影响。"""
    register(client)
    uid = _alice_id()
    solution_id = _create_solution(client)
    document_store.save_document(uid, solution_id, "prd", "# PRD")
    document_store.save_document(uid, solution_id, "api_docs", "# 旧接口文档")
    document_store.save_document(uid, solution_id, "prompts", "# 旧提示词")

    _fake_service(monkeypatch, ScriptedLLM(["接口正文"]))
    _run_job(uid, solution_id, "api_docs", {}, resolved_inputs={"prd_content": "# PRD"})

    kinds = {doc["kind"] for doc in document_store.list_documents(uid, solution_id)}
    assert kinds == {"prd", "api_docs", "prompts"}


def test_optimize_writes_to_the_document_named_by_doc_kind(client, monkeypatch):
    """就地优化按 `doc_kind` 决定写进哪一份——优化接口文档不该动 PRD。"""
    register(client)
    uid = _alice_id()
    solution_id = _create_solution(client)
    document_store.save_document(uid, solution_id, "prd", "# 原 PRD")
    _fake_service(monkeypatch, ScriptedLLM(["优化后的接口文档"]))

    _run_job(
        uid,
        solution_id,
        "optimize",
        {"document": "# 旧接口文档", "instruction": "补充分页说明", "doc_kind": "api_docs"},
    )

    api_docs = document_store.get_document(uid, solution_id, "api_docs")
    assert api_docs is not None
    assert api_docs["content"] == "优化后的接口文档"
    # PRD 原样保留：优化是就地重写，不牵动别的文档
    prd = document_store.get_document(uid, solution_id, "prd")
    assert prd is not None and prd["content"] == "# 原 PRD"


def test_optimize_without_doc_kind_writes_nothing(client, monkeypatch):
    """`optimize` 未说明改的是哪一份时**不落库**：宁可不写，也不猜一个默认值写错位置。"""
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

    assert events[-1].data["status"] == "completed"
    assert document_store.list_documents(uid, solution_id) == []


def test_create_optimize_job_via_http_carries_doc_kind(client, monkeypatch):
    """HTTP 面：`doc_kind` 从请求体一路透传到落库（防止「字段加了但没接上」）。"""
    register(client)
    uid = _alice_id()
    solution_id = _create_solution(client)
    _fake_service(monkeypatch, ScriptedLLM(["优化结果"]))

    created = client.post(
        "/api/jobs",
        json={
            "kind": "optimize",
            "solution_id": solution_id,
            "document": "# 旧提示词",
            "instruction": "改一下",
            "doc_kind": "prompts",
        },
    )
    assert created.status_code == 201, created.text
    client.get(f"/api/jobs/{created.json()['job_id']}/stream")

    doc = document_store.get_document(uid, solution_id, "prompts")
    assert doc is not None and doc["content"] == "优化结果"

    # 同一个结论也要能从**任务记录**读到：前端重连时靠它定位「改的是哪一份」，
    # 而不是靠「当前停在哪个审阅页」去猜。
    status = client.get(f"/api/jobs/{created.json()['job_id']}")
    assert status.status_code == 200
    assert status.json()["doc_kind"] == "prompts"


def test_registration_persists_doc_kind_from_request(client, alice):
    """登记在**落库那一刻**就把 doc_kind 算出来落列（不是收尾时才算）。

    登记时能算是因为它是纯请求推导，不依赖入口模式或前端快照。
    晚算的话，进程重启后 DB 兜底快照与 `GET /api/jobs` 就永远答不出「改的是哪一份」。
    """
    optimize = build_and_register(
        owner_id=alice,
        solution_id=None,
        kind="optimize",
        request={"doc_kind": "api_docs"},
    )
    row = job_get(alice, optimize.job_id)
    assert row is not None
    assert row["doc_kind"] == "api_docs"

    # 整篇生成的产物由 kind 直接决定，不依赖请求体
    prd = build_and_register(
        owner_id=alice, solution_id=None, kind="prd", request={"requirements": "x"}
    )
    assert job_get(alice, prd.job_id)["doc_kind"] == "prd"

    # 「不猜成 prd」：optimize 漏传 doc_kind 时，DB 兜底快照读回的是 None。
    unknown = build_and_register(
        owner_id=alice, solution_id=None, kind="optimize", request={"document": "x"}
    )
    assert first_snapshot_event(alice, unknown.job_id).data["doc_kind"] is None


def test_in_flight_snapshot_carries_doc_kind(client, alice):
    """在途内存快照带 `doc_kind`；取不到时是 `None`，**不猜成 prd**。

    「不猜」是硬口径：猜错等于把优化接口文档的结果写进 PRD 正文，属于静默的数据损坏。
    跨「造单 → 登记 → 派发 → 订阅」读首份快照，取代直调 `_snapshot_event`。
    """
    from backend.core.contracts import StreamEvent

    _, optimize_snapshot, _ = run_ticket_snapshot(
        alice,
        None,
        "optimize",
        {"doc_kind": "api_docs", "document": "x"},
        StreamEvent(type="chunk", content="在途", data={}),
    )
    assert optimize_snapshot.data["doc_kind"] == "api_docs"

    _, prd_snapshot, _ = run_ticket_snapshot(
        alice,
        None,
        "prd",
        {"requirements": "x"},
        StreamEvent(type="chunk", content="在途", data={}),
    )
    assert prd_snapshot.data["doc_kind"] == "prd"

    _, unknown_snapshot, _ = run_ticket_snapshot(
        alice,
        None,
        "optimize",
        {"document": "x"},
        StreamEvent(type="chunk", content="在途", data={}),
    )
    assert unknown_snapshot.data["doc_kind"] is None


def test_db_snapshot_event_carries_doc_kind(alice):
    """DB 兜底快照同样带 `doc_kind`；旧行（空串）退化成 `None`。

    走「登记（不派发）→ `open_stream`」：登记表里没有这支运行时时读 DB 兜底；
    这正是进程重启后前端重连的真实路径，取代直调 `_db_snapshot_event`。
    """
    prompts = build_and_register(
        owner_id=alice, solution_id=None, kind="prompts", request={}
    )
    assert first_snapshot_event(alice, prompts.job_id).data["doc_kind"] == "prompts"

    legacy = build_and_register(
        owner_id=alice, solution_id=None, kind="optimize", request={}
    )
    assert first_snapshot_event(alice, legacy.job_id).data["doc_kind"] is None


def test_doc_kind_rejects_unknown_value(client):
    """`doc_kind` 只接受三种产物；写错的取值应被 422 挡在门口，而不是悄悄落库。"""
    register(client)
    _create_solution(client)

    response = client.post(
        "/api/jobs",
        json={"kind": "optimize", "document": "x", "instruction": "y", "doc_kind": "nope"},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# 文档落库：版本语义（整篇生成追加版本；优化 / 澄清就地改当前版本）
# ---------------------------------------------------------------------------


def _versions(uid: int, solution_id: int, kind: str) -> list[dict]:
    """取某方案某产物的版本摘要列表（新→旧）。"""
    return document_store.list_versions(uid, solution_id, kind)


def test_completed_generation_appends_a_version(client, monkeypatch):
    """整篇生成成功 = 追加一条新版本，来源标为该产物类型，且它就是当前版本。"""
    register(client)
    uid = _alice_id()
    solution_id = _create_solution(client)
    _fake_service(monkeypatch, ScriptedLLM(["章节正文"]))

    _run_job(
        uid,
        solution_id,
        "prd",
        {},
        resolved_inputs={"requirements": "做一个演示产品", "product_name": "演示产品"},
    )

    versions = _versions(uid, solution_id, "prd")
    assert [v["version_no"] for v in versions] == [1]
    assert versions[0]["source"] == "prd"
    assert versions[0]["is_current"] is True


def test_regenerating_prd_appends_version_and_keeps_downstream_versions(client, monkeypatch):
    """重新生成 PRD：PRD 追加一条新版本，下游文档与它们的版本都**保留**。"""
    register(client)
    uid = _alice_id()
    solution_id = _create_solution(client)
    document_store.save_document(uid, solution_id, "prd", "# 旧 PRD")
    document_store.save_document(uid, solution_id, "prompts", "# 旧提示词")
    assert len(_versions(uid, solution_id, "prompts")) == 1

    _fake_service(monkeypatch, ScriptedLLM(["章节正文"]))
    _run_job(
        uid,
        solution_id,
        "prd",
        {},
        resolved_inputs={"requirements": "做一个演示产品", "product_name": "演示产品"},
    )

    assert [v["version_no"] for v in _versions(uid, solution_id, "prd")] == [2, 1]
    # 下游保留：不再是空列表，旧版本仍在（是否过期由 stale 标记）
    assert len(_versions(uid, solution_id, "prompts")) == 1


def test_optimize_rewrites_current_version_without_new_entry(client, monkeypatch):
    """就地优化**不新增版本条目**、不动下游：版本数不变，当前版本正文被改写。"""
    register(client)
    uid = _alice_id()
    solution_id = _create_solution(client)
    document_store.save_document(uid, solution_id, "prd", "# 原 PRD")
    document_store.save_document(uid, solution_id, "prompts", "# 原提示词")
    before = len(_versions(uid, solution_id, "prd"))

    _fake_service(monkeypatch, ScriptedLLM(["优化后的 PRD"]))
    _run_job(
        uid,
        solution_id,
        "optimize",
        {"document": "# 原 PRD", "instruction": "补一段", "doc_kind": "prd"},
    )

    assert len(_versions(uid, solution_id, "prd")) == before  # 条目数不增
    assert document_store.get_document(uid, solution_id, "prd")["content"] == "优化后的 PRD"
    # 下游不被顺手删掉
    assert {d["kind"] for d in document_store.list_documents(uid, solution_id)} == {
        "prd",
        "prompts",
    }


def test_clarify_rewrites_current_version_without_new_entry(client, monkeypatch):
    """按澄清改写同样就地改当前版本：不新增条目、不删下游。"""
    register(client)
    uid = _alice_id()
    solution_id = _create_solution(client)
    document_store.save_document(uid, solution_id, "prd", "# 原 PRD")
    before = len(_versions(uid, solution_id, "prd"))

    _fake_service(monkeypatch, ScriptedLLM(["按澄清改写后的 PRD"]))
    _run_job(
        uid,
        solution_id,
        "clarify",
        {"document": "# 原 PRD", "doc_kind": "prd"},
        # 对话由服务端从那条文档线程取（生产路径），这里直接给它同样的形状
        resolved_inputs={"history": [{"role": "user", "content": "登录方式确定为手机号"}]},
    )

    assert len(_versions(uid, solution_id, "prd")) == before
    assert (
        document_store.get_document(uid, solution_id, "prd")["content"]
        == "按澄清改写后的 PRD"
    )


# ---------------------------------------------------------------------------
# P3-b：待确认清单贯穿 generation_jobs（迁移 + 仓储三方法 + 两路快照）
# ---------------------------------------------------------------------------


def _sample_items() -> list[dict[str, Any]]:
    """一份典型的待确认清单（与 `pending_items.extract` 产出的形状一致）。"""
    return [
        {"id": "p-1", "question": "确认登录方式", "sections": ["2.1"], "decider": "产品负责人"},
        {"id": "p-2", "question": "分页大小是否固定为 20", "sections": ["4.3"], "decider": ""},
    ]


def test_persist_progress_writes_pending_items(alice):
    """合并写把待确认清单落进 generation_jobs.pending_items。"""
    items = _sample_items()
    job_id = job_create(alice, "api_docs", None)
    job_persist_progress(
        alice,
        job_id,
        draft_content="正文",
        completed_sections=[],
        phase="writing",
        pending_items=items,
    )
    row = job_get(alice, job_id)
    assert json.loads(row["pending_items"]) == items


def test_finish_writes_pending_items(alice):
    """成功收尾把待确认清单与终态一并落库。"""
    items = _sample_items()
    job_id = job_create(alice, "prompts", None)
    job_finish(
        alice,
        job_id,
        draft_content="正文",
        completed_sections=[],
        pending_items=items,
    )
    row = job_get(alice, job_id)
    assert row["status"] == "completed"
    assert json.loads(row["pending_items"]) == items


def test_fail_writes_pending_items(alice):
    """失败收尾同样落清单：失败的任务也产出了待确认项，不该丢掉。"""
    items = _sample_items()
    job_id = job_create(alice, "prompts", None)
    job_fail(
        alice,
        job_id,
        error_code="LLM_UPSTREAM_ERROR",
        error_message="上游中断",
        draft_content="半截正文",
        completed_sections=[],
        pending_items=items,
    )
    row = job_get(alice, job_id)
    assert row["status"] == "failed"
    assert json.loads(row["pending_items"]) == items


def test_db_snapshot_event_carries_pending_items(alice):
    """DB 兜底快照携带待确认清单，刷新重连后前端直接展示。

    清单由 `done` 随收尾落到任务行；登记（不派发）之后 `open_stream` 走 DB 兜底，
    读回的是库里那一份，取代直调 `_db_snapshot_event`。
    """
    items = _sample_items()
    registered, _ = run_ticket_events(
        alice, None, "api_docs", {}, done_data={"reply": "正文", "pending_items": items}
    )
    # 收尾已把任务摘出登记表：此刻订阅读的是 DB 兜底路径。
    snapshot = first_snapshot_event(alice, registered.job_id)
    assert snapshot.data["pending_items"] == items


def test_in_flight_snapshot_carries_pending_items(alice):
    """在途内存快照携带待确认清单：刷新重连后前端不用另发请求。"""
    items = _sample_items()
    _, snapshot, _ = run_ticket_snapshot(
        alice,
        None,
        "api_docs",
        {},
        StreamEvent(
            type="done", content="", data={"reply": "正文", "pending_items": items}
        ),
    )
    assert snapshot.data["pending_items"] == items


def test_in_flight_snapshot_carries_stage_output(alice):
    """在途快照带上**当前阶段的原始输出**：刷新重连后审核面板不会空着。

    这条守的是「刷新后要重新接回流式输出」：阶段输出从前只活在前端内存里，
    一刷新就没了；现在它由任务运行时持有，随 `snapshot.stage_text` 一道回来。
    """
    stage_events = [
        StreamEvent(type="stage", content="", data={"stage": "reviewing"}),
        *[
            StreamEvent(type="stage_chunk", content=piece, data={"stage": "reviewing"})
            for piece in ('{"verdict"', ':"revise"}', "")
        ],
    ]
    _, snapshot, _ = run_ticket_snapshot(alice, None, "api_docs", {}, *stage_events)

    assert snapshot.data["phase"] == "reviewing"
    assert snapshot.data["stage_text"] == '{"verdict":"revise"}'


def test_stage_change_clears_previous_stage_output(alice):
    """换阶段清空上一阶段的输出：审核意见不该留在改写阶段的面板里。"""
    _, snapshot, _ = run_ticket_snapshot(
        alice,
        None,
        "api_docs",
        {},
        StreamEvent(type="stage", content="", data={"stage": "reviewing"}),
        StreamEvent(type="stage_chunk", content="审核意见", data={"stage": "reviewing"}),
        StreamEvent(type="stage", content="", data={"stage": "rewriting"}),
        StreamEvent(type="stage_chunk", content="改写稿", data={"stage": "rewriting"}),
    )
    assert snapshot.data["phase"] == "rewriting"
    assert snapshot.data["stage_text"] == "改写稿"


def test_stage_chunk_never_leaks_into_draft_text(alice):
    """阶段输出与正文缓冲**严格分道**：审核 JSON 不能出现在会落库的正文里。"""
    _, snapshot, _ = run_ticket_snapshot(
        alice,
        None,
        "api_docs",
        {},
        StreamEvent(
            type="stage_chunk",
            content='{"verdict":"pass"}',
            data={"stage": "reviewing"},
        ),
    )
    assert snapshot.data["live_buffer"] == ""
    assert snapshot.data["finalized_text"] == ""


def test_db_snapshot_event_has_empty_stage_text(alice):
    """DB 兜底快照（进程重启后）没有阶段输出：显式给空串，不让前端拿到 `undefined`。"""
    registered = build_and_register(
        owner_id=alice, solution_id=None, kind="api_docs", request={}
    )
    assert first_snapshot_event(alice, registered.job_id).data["stage_text"] == ""


def test_generation_flows_pending_items_into_job_and_document(client, monkeypatch):
    """端到端：一次生成产出的待确认清单随 `done` 落到任务记录与文档，三者同源。

    用 `api_docs` 线（比 PRD 的分章节线简单）：注入的假 LLM 在正文里带上标记块，
    生成器在 `done` 时剥离块、产出清单，经 `job_service` 同一次收尾原子落库。
    这条断言守的是 P3-b 与 P3-c 的交界：「换正文却忘重算清单」没有结构落脚点。
    """
    register(client)
    uid = _alice_id()
    solution_id = _create_solution(client)
    block = (
        "<!-- PENDING:BEGIN -->\n"
        '[{"question": "确认登录方式", "sections": ["2.1"], "decider": "产品负责人", '
        '"importance": "P1"}]\n'
        "<!-- PENDING:END -->"
    )
    _fake_service(monkeypatch, ScriptedLLM([f"接口文档正文\n{block}"]))

    job_id, events = _run_job(uid, solution_id, "api_docs", {"prd_content": "# PRD"})
    done = next(event for event in events if event.type == "done")
    items = done.data["pending_items"]

    # 标记块被剥离、清单按出现顺序分配 id
    assert items == [
        {
            "id": "p-1",
            "question": "确认登录方式",
            "sections": ["2.1"],
            "decider": "产品负责人",
            "importance": "P1",
        }
    ]
    # 正文干净：标记块不应出现在落库正文里
    assert "<!-- PENDING" not in done.data["reply"]

    # 任务记录与文档都存了同一份清单（同一次收尾写入）
    row = job_get(uid, job_id)
    assert json.loads(row["pending_items"]) == items
    doc = document_store.get_document(uid, solution_id, "api_docs")
    assert doc is not None
    assert doc["pending_items"] == items


# ---------------------------------------------------------------------------
# 删除方案 → 取消挂在它上面的任务
#
# 没有这一组，「生成途中删掉方案」会留下一条很隐蔽的尾巴：任务照跑、收尾照写文档，
# 而方案已经不在了。前端那边迟到的流回调则会把刚被清空的会话重新填满，
# 自动保存据此新建出一份「未命名方案」。
# ---------------------------------------------------------------------------


def test_cancel_running_only_targets_owner_and_solution(client, alice):
    """`cancel_running` 只动该 owner + 方案下的 running，不波及别的方案与已终态任务。"""
    target = job_create(alice, "prd", 11)
    other_solution = job_create(alice, "prd", 12)
    finished = job_create(alice, "prd", 11)
    job_finish(alice, finished, draft_content="正文", completed_sections=[])

    # 另一个 owner 恰好也有一个挂在同一个 solution_id 上的任务
    register(client, username="bob")
    from backend.services import auth_service

    bob = users.get_by_username("bob")
    assert bob is not None
    bob_job = job_create(int(bob["id"]), "prd", 11)

    affected = job_cancel_running(alice, solution_id=11)

    assert affected == 1
    cancelled = job_get(alice, target)
    assert cancelled["status"] == "failed"
    assert cancelled["error_code"] == "CANCELLED"
    assert cancelled["error_message"] == "方案已删除，任务已取消"

    # 别的方案、别的 owner、已经结束的任务都不动
    assert job_get(alice, other_solution)["status"] == "running"
    assert job_get(int(bob["id"]), bob_job)["status"] == "running"
    assert job_get(alice, finished)["status"] == "completed"


def test_finalize_skips_document_when_solution_gone(client, monkeypatch):
    """方案已删除时收尾**不写文档**，并把任务落成失败态。

    跨公开动作复现「删除发生在标记生效之前」的时间窗：造单 → 登记 → 派发，
    假事件流吐出第一段后卡住；此时用 `solution_service.delete_solution` 删方案
    （**不是** `delete_solution_and_cancel_jobs`，后者会顺手置内存取消标记），
    再放开流让 `done` 抵达收尾。收尾只剩「方案还在不在」这一条事实可查。
    断言的是外部可观察的终态、写文档动作、以及回执，不探任何内部标志位。
    """
    register(client)
    uid = _alice_id()
    solution_id = _create_solution(client)
    first_seen = asyncio.Event()
    release = asyncio.Event()

    async def stream(kind, request):
        yield StreamEvent(type="chunk", content="半截正文", data={})
        first_seen.set()
        await release.wait()
        yield StreamEvent(type="done", content="", data={"reply": "# 已经生成好的正文"})

    written: list = []
    monkeypatch.setattr(
        document_store, "commit_generated", lambda *a, **k: written.append((a, k))
    )

    async def scenario() -> tuple[int, list]:
        registered, events = await run_ticket_async(
            owner_id=uid,
            solution_id=solution_id,
            kind="prd",
            request={"requirements": "方案会被删"},
            event_stream_source=stream,
            before_open=delete_solution_after_first_chunk,
        )
        return registered.job_id, events

    async def delete_solution_after_first_chunk(job_id: int) -> None:
        await asyncio.wait_for(first_seen.wait(), timeout=5)
        assert solution_service.delete_solution(uid, solution_id) is True
        release.set()

    job_id, events = _run(scenario())

    assert written == [], "方案已删除时不该再往 documents 里写"
    assert any(event.type == "run_summary" and event.data["status"] == "failed" for event in events)
    row = job_get(uid, job_id)
    assert row is not None
    assert row["status"] == "failed"
    assert row["error_code"] == "CANCELLED"
    assert document_store.list_documents(uid, solution_id) == []


def test_run_job_stops_consuming_events_after_cancel(client, alice):
    """方案中途被删：订阅只收到第一段增量，done 不再消费，正文不变成「完整正文」。

    假事件流在造单时传入；「方案被删」走公开动作 `delete_solution_and_cancel_jobs`
    （等同 HTTP 删除方案那条路），断言外部可观察的事件序列、终态与落库正文，
    不再构造裸运行时、也不读内部标志位。
    """
    solution_id = _create_solution(client)
    produced: list[str] = []
    consumed: list[StreamEvent] = []

    async def stream(kind, request):
        produced.append("第一段")
        yield StreamEvent(type="chunk", content="第一段", data={})
        # 第一个事件处理完之后，方案被删除（公开动作会标记在途任务）。
        job_service.delete_solution_and_cancel_jobs(alice, solution_id)
        produced.append("第二段")
        yield StreamEvent(type="chunk", content="第二段", data={})
        produced.append("done")
        yield StreamEvent(type="done", content="", data={"reply": "完整正文"})

    async def scenario() -> tuple[int, list]:
        registered = build_and_register(
            owner_id=alice,
            solution_id=solution_id,
            kind="prd",
            request={"requirements": "取消分支", "product_name": "假流"},
            event_stream_source=stream,
        )
        start_registered_job(registered)
        events = []
        async for event in job_service.open_stream(alice, registered.job_id):
            events.append(event)
            consumed.append(event)
        return registered.job_id, events

    job_id, events = _run(scenario())

    # 只取走了两个事件就 break：done 从未被消费，正文不可能变成「完整正文」
    assert produced == ["第一段", "第二段"]
    assert "第二段" not in [event.content for event in events if event.type == "chunk"]
    assert not any(event.type == "done" for event in events)

    row = job_get(alice, job_id)
    assert row is not None
    assert row["status"] == "failed"
    assert row["error_code"] == "CANCELLED"
    assert row["draft_content"] == "第一段"


def test_delete_solution_endpoint_cancels_its_jobs(client):
    """HTTP 面：删除方案会连带取消它的 running 任务（路由层的编排没漏接）。"""
    register(client)
    uid = _alice_id()
    solution_id = _create_solution(client)
    other_id = _create_solution(client)
    doomed = job_create(uid, "prd", solution_id)
    untouched = job_create(uid, "api_docs", other_id)

    assert client.delete(f"/api/solutions/{solution_id}").status_code == 200

    row = job_get(uid, doomed)
    assert row is not None
    assert row["status"] == "failed"
    assert row["error_code"] == "CANCELLED"
    assert job_get(uid, untouched)["status"] == "running"


# ---------------------------------------------------------------------------
# 分章节的按章缓冲：多章同时逐字流，但各存各的
#
# 这一组原先直接构造裸运行时、往 `_apply_event` 喂事件。归位后一律把同样的事件
# 在**造单时**作为假事件流传入，经「登记 → 派发 → 订阅」读在途快照：
# `live_by_section` / `completed_sections` / `finalized_text` 都是前端重连时
# 真实拿得到的公开字段，内部实现怎么重排都不影响这些断言。
# ---------------------------------------------------------------------------


def _header_part(text: str) -> StreamEvent:
    """文档头：没有 `index`、也没有 `section_id`（它不属于任何一章）。"""
    return StreamEvent(type="part", content="", data={"text": text})


def _chunk(text: str, section_id: str, index: int) -> StreamEvent:
    return StreamEvent(
        type="chunk", content=text, data={"section_id": section_id, "index": index}
    )


def _part(text: str, section_id: str, index: int) -> StreamEvent:
    return StreamEvent(
        type="part", content="", data={"text": text, "section_id": section_id, "index": index}
    )


def _stage(name: str, section_id: str, index: int) -> StreamEvent:
    return StreamEvent(
        type="stage",
        content="",
        data={"stage": name, "section_id": section_id, "index": index},
    )


def _section_snapshot(alice: int, *events: StreamEvent) -> StreamEvent:
    """把一组分章节事件走主接缝，返回在途内存快照的第一份。"""
    _, snapshot, _ = run_ticket_snapshot(alice, None, "prd", {}, *events)
    return snapshot


def test_section_chunks_are_buffered_per_section(alice):
    """每章的增量各进各的格子。

    混在一个 `live_buffer` 里就再也分不开了——前端只能整批丢弃，
    那正是「PRD 只能等整章定稿才蹦出来」的成因。
    """
    snapshot = _section_snapshot(
        alice,
        _chunk("第一章", "overview", 0),
        _chunk("第二章", "features", 1),
        _chunk("续写", "overview", 0),
    )
    live = snapshot.data["live_by_section"]

    assert [item["section_id"] for item in live] == ["overview", "features"]
    assert live[0]["text"] == "第一章续写"
    assert live[1]["text"] == "第二章"
    # 无归属的整篇缓冲保持干净：它只服务「不分章」的链路（文档 / 对话）
    assert snapshot.data["live_buffer"] == ""


def test_part_clears_only_its_own_section(alice):
    """一章定稿只清这一章：并行时别的章还在写，清全场会把它们的字一起抹掉。"""
    snapshot = _section_snapshot(
        alice,
        _chunk("甲", "overview", 0),
        _chunk("乙", "features", 1),
        _part("## 1. 甲\n\n", "overview", 0),
    )
    live = snapshot.data["live_by_section"]

    assert [item["index"] for item in live] == [1], "定稿的章半成品该被清掉"
    assert live[0]["text"] == "乙", "别的章一个字都不能丢"
    assert snapshot.data["finalized_text"] == "## 1. 甲\n\n"
    assert [item["section_id"] for item in snapshot.data["completed_sections"]] == ["overview"]


def test_rewriting_clears_the_section_so_draft_and_rewrite_do_not_stack(alice):
    """改写必须清掉该章半成品：否则初稿与改写稿会在同一张卡片里叠着长。"""
    snapshot = _section_snapshot(
        alice,
        _chunk("初稿", "overview", 0),
        _stage("rewriting", "overview", 0),
        _chunk("改后", "overview", 0),
    )
    live = snapshot.data["live_by_section"]

    assert live[0]["text"] == "改后"
    assert live[0]["phase"] == "rewriting"


def test_retry_reannounced_writing_resets_the_section(alice):
    """单章重试会重报 `writing`：那是「从头重来」的信号，必须清该章。"""
    snapshot = _section_snapshot(
        alice,
        _chunk("第一轮的一半", "overview", 0),
        _stage("writing", "overview", 0),
        _chunk("第二轮", "overview", 0),
    )
    live = snapshot.data["live_by_section"]

    assert live[0]["text"] == "第二轮"


def test_draft_interleaves_finalized_and_live_by_index(alice):
    """正文按 index 交错归并，不能「先定稿、后半成品」。

    并行时第 1 章可能还在写、第 2 章已经定稿；简单拼接会把正文排成 2、3、1，
    而「已定稿 + 各章半成品」是**进度落库**与「重连兜底」的正文来源——
    顺序错了刷新后就是错版。快照把两份分开给，前端按 index 拼回去。
    """
    snapshot = _section_snapshot(
        alice,
        _header_part("# 文档头\n\n"),
        _chunk("第1章写到一半", "overview", 0),
        _part("第2章定稿\n\n", "features", 1),
    )

    assert snapshot.data["finalized_text"] == "# 文档头\n\n第2章定稿\n\n"
    assert [item["index"] for item in snapshot.data["live_by_section"]] == [0]
    assert [item["index"] for item in snapshot.data["completed_sections"]] == [1]


def test_snapshot_carries_live_by_section_but_not_the_finalized_ones(alice):
    """快照要带上「正在写的几章」，且不能与已定稿章节重复。

    重复的后果是刷新后同一段正文出现两次；只发定稿章节的后果则是
    「正在写的那几章刷新后一片空白，直到定稿才整段出现」。
    """
    snapshot = _section_snapshot(
        alice,
        _chunk("甲半截", "overview", 0),
        _part("乙定稿\n\n", "features", 1),
        _chunk("丙半截", "pages", 2),
    )
    live = snapshot.data["live_by_section"]

    assert [item["section_id"] for item in live] == ["overview", "pages"]
    assert [item["index"] for item in live] == [0, 2]
    assert live[0]["text"] == "甲半截"
    assert all(item["section_id"] != "features" for item in live)
    # 定稿章节照旧在 `completed_sections` 里，各走各的字段
    assert [item["section_id"] for item in snapshot.data["completed_sections"]] == ["features"]


def test_db_snapshot_has_no_live_by_section(alice):
    """DB 兜底快照显式给空数组：过程字不落库（同 `stage_text`）。

    登记（不派发）之后 `open_stream` 走 DB 兜底——正是进程重启后前端重建看板的路径。
    """
    registered = build_and_register(
        owner_id=alice, solution_id=None, kind="prd", request={}
    )
    assert first_snapshot_event(alice, registered.job_id).data["live_by_section"] == []

# ---------------------------------------------------------------------------
# 收尾与终态：被取代 / 已取消的三条路、以及「落库失败不等于任务失败」
#
# 这一组盯的是「一次任务走到终点时，哪些情况算失败、哪些只是不写」。
# 原先只覆盖了「方案已删」与「被顶替」两条；取消/落库异常几条分支没有用例。
# ---------------------------------------------------------------------------


def _collect(owner_id: int, job_id: int) -> list:
    """订阅一个任务直到流结束，返回全部事件。"""

    async def scenario() -> list:
        return [event async for event in job_service.open_stream(owner_id, job_id)]

    return _run(scenario())


def test_late_subscriber_replays_failure_snapshot_then_error_and_summary(alice):
    """失败任务重连：快照补全状态，`error` 说明原因，`run_summary` 补发用量。

    少这条，重连的订阅者会挂在队列上等一个永远不会再来的事件（任务已经跑完），
    而「为什么失败」也就漏掉了。跨主接缝复现：假事件流在造单时吐一个 `error`，
    任务收尾后重订阅，读到的事件序列是 DB 兜底那一路补出来的。
    """
    registered, _ = run_ticket_events(
        alice,
        None,
        "prd",
        {},
        StreamEvent(
            type="error",
            content="上游中断",
            code="LLM_UPSTREAM_ERROR",
            data={"error_type": "JobFailed"},
        ),
    )

    events = _collect(alice, registered.job_id)

    assert [event.type for event in events] == ["snapshot", "error", "run_summary"]
    assert events[0].data["status"] == "failed"
    assert events[1].code == "LLM_UPSTREAM_ERROR"
    assert "上游中断" in events[1].content
    assert events[2].data["status"] == "failed"


def test_late_subscriber_does_not_replay_an_abort_as_an_error(alice):
    """取消 / 被顶替同样落 failed，但补发时**不**发 `error`：那不是错误。

    否则用户自己按的停止会被渲染成红色的「生成失败」。跨主接缝复现：派发后
    用公开动作 `cancel_job` 停止在途任务，收尾落 `failed + CANCELLED`；
    重订阅只读到快照与回执。
    """
    release = asyncio.Event()

    async def stream(kind, request):
        yield StreamEvent(type="chunk", content="第一段", data={})
        await release.wait()
        yield StreamEvent(type="done", content="", data={"reply": "完整正文"})

    target: list[int] = []
    seen: list[StreamEvent] = []

    def stop_on_first_snapshot(event: StreamEvent) -> None:
        seen.append(event)
        if event.type == "snapshot":
            job_service.cancel_job(alice, target[0])
            release.set()

    async def scenario() -> int:
        registered, _ = await run_ticket_async(
            owner_id=alice,
            solution_id=None,
            kind="prd",
            request={},
            event_stream_source=stream,
            before_open=lambda job_id: target.append(job_id),
            on_event=stop_on_first_snapshot,
        )
        return registered.job_id

    job_id = _run(scenario())
    events = _collect(alice, job_id)

    assert [event.type for event in events] == ["snapshot", "run_summary"]
    assert events[0].data["error_code"] == "CANCELLED"
    assert events[1].data["status"] == "failed"


def test_delete_solution_marks_in_flight_runtimes_cancelled(client, alice):
    """删方案时**内存里的在途任务也要停**：只改库的话协程照旧烧 token 直到跑完。

    这是「协作式取消」的全部机制：库里那一行只是事后记录，真正让它停下的是
    内存标记。跨公开动作断言两支在途任务的结局：
    - 被删方案那支：`delete_solution_and_cancel_jobs` 之后不再消费后续事件，
      落成 `failed + CANCELLED`，半截正文停在「第一段」；
    - 别的方案那支：照常把「第二段」消费完、落 `completed`。
    """
    doomed_solution = _create_solution(client)
    other_solution = _create_solution(client)
    doomed_seen, other_seen = asyncio.Event(), asyncio.Event()
    doomed_hold, other_hold = asyncio.Event(), asyncio.Event()

    async def stream(kind, request, seen, hold, tag):
        yield StreamEvent(type="chunk", content=f"第一段-{tag}", data={})
        seen.set()
        await hold.wait()
        yield StreamEvent(type="chunk", content=f"第二段-{tag}", data={})
        yield StreamEvent(type="done", content="", data={"reply": f"完整-{tag}"})

    async def scenario() -> tuple[int, int]:
        doomed = build_and_register(
            owner_id=alice,
            solution_id=doomed_solution,
            kind="prd",
            request={"requirements": "会被取消"},
            event_stream_source=lambda k, r: stream(k, r, doomed_seen, doomed_hold, "doomed"),
        )
        other = build_and_register(
            owner_id=alice,
            solution_id=other_solution,
            kind="prd",
            request={"requirements": "不该被波及"},
            event_stream_source=lambda k, r: stream(k, r, other_seen, other_hold, "other"),
        )
        start_registered_job(doomed)
        start_registered_job(other)
        await asyncio.wait_for(doomed_seen.wait(), timeout=5)
        await asyncio.wait_for(other_seen.wait(), timeout=5)

        assert job_service.delete_solution_and_cancel_jobs(alice, doomed_solution) is True
        doomed_hold.set()
        other_hold.set()

        # 用公开查询的终态作判据，等两支后台任务各自收尾完毕。
        for _ in range(200):
            rows = [job_get(alice, doomed.job_id), job_get(alice, other.job_id)]
            if all(row is not None and row["status"] != "running" for row in rows):
                break
            await asyncio.sleep(0.005)
        return doomed.job_id, other.job_id

    doomed_id, other_id = _run(scenario())

    doomed_row = job_get(alice, doomed_id)
    assert doomed_row is not None
    assert doomed_row["status"] == "failed"
    assert doomed_row["error_code"] == "CANCELLED"
    assert "第二段-doomed" not in doomed_row["draft_content"], "被取消的任务不该继续消费事件"

    other_row = job_get(alice, other_id)
    assert other_row is not None
    assert other_row["status"] == "completed"
    assert "第二段-other" in other_row["draft_content"], "别的方案上的任务一个字都不该丢"


def test_skill_registry_failure_degrades_visibility_not_the_job(
    client, alice, monkeypatch, temp_skills_root
):
    """技能包身份读不出来时：回执里**如实为空**，任务照常跑完。

    降级要看得见（债务 D8）：读盘出意外属于「该起的自检没拦住」，不能让一次具体的
    生成请求无缘无故失败，也不能报一个与实际注入不符的包。假环境用临时技能目录
    里「元数据损坏」的包，不替换 `describe_kind` 内部零件。
    """
    skill_dir = temp_skills_root / "prd-generator"
    skill_dir.mkdir()
    (skill_dir / "skill.yaml").write_text("这不是合法的 YAML 映射：[\n", encoding="utf-8")
    _fake_service(monkeypatch, ScriptedLLM(["章节正文"]))
    solution_id = _create_solution(client)

    job_id, events = _run_job(
        alice,
        solution_id,
        "prd",
        {},
        resolved_inputs={"requirements": "做一个演示产品", "product_name": "演示产品"},
    )

    summary = events[-1].data
    assert summary["status"] == "completed"
    assert summary["injected_skills"] == []
    assert job_get(alice, job_id)["status"] == "completed"


def test_pipeline_app_error_keeps_its_own_code(alice, monkeypatch):
    """管线自己抛的 `AppError`：用它自带的码，不笼统记成「上游故障」。

    预算超顶是 `INVALID_REQUEST`（重试也没用）；记成 `LLM_UPSTREAM_ERROR` 会让前端
    去做无意义的重试，把一次确定的超顶放大成一串调用。
    """

    async def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AppError(ErrorCode.INVALID_REQUEST, "输入超出模型窗口")
        yield  # pragma: no cover - 有 yield 才是 async generator

    monkeypatch.setattr(document_service, "generate_prd_from_summary_stream", _boom)

    job_id, events = _run_job(
        alice, None, "prd", {}, resolved_inputs={"requirements": "做一个演示产品"}
    )

    row = job_get(alice, job_id)
    assert row is not None
    assert row["status"] == "failed"
    assert row["error_code"] == "INVALID_REQUEST"
    assert row["error_message"] == "输入超出模型窗口"
    error = next(event for event in events if event.type == "error")
    assert error.code == "INVALID_REQUEST"


def test_replace_part_wipes_every_section_buffer(alice):
    """`part{replace}` 是**整篇覆盖**：所有章节的半成品一并作废。

    若不清，改写稿会与初稿的半截字并排长在同一张卡片里——两条轨（过程字 / 权威正文）
    混起来正是「PRD 不流式」与「重复正文」两类事故的同一个根。归位后读在途快照：
    覆盖之后只剩新版全文，既无按章半成品、也无旧定稿章节。
    """
    snapshot = _section_snapshot(
        alice,
        _header_part("# 文档头\n\n"),
        _chunk("甲半截", "overview", 0),
        _chunk("乙半截", "features", 1),
        StreamEvent(type="part", content="", data={"text": "# 新版全文", "replace": True}),
    )

    assert snapshot.data["finalized_text"] == "# 新版全文"
    assert snapshot.data["live_by_section"] == []
    assert snapshot.data["live_buffer"] == ""
    assert snapshot.data["completed_sections"] == []


def test_write_document_skips_an_empty_reply(client, alice, monkeypatch):
    """定稿正文为空时不写文档：宁可这次不落库，也不要拿空稿顶掉上一版。

    跨主接缝：`done.reply` 是空白，`commit_generated` 是唯一写文档的公开落点，
    用记录器断言它一次都没被调；任务本身照常成功（不写 ≠ 失败）。
    """
    solution_id = _create_solution(client)
    written: list = []
    monkeypatch.setattr(
        document_store, "commit_generated", lambda *a, **k: written.append((a, k))
    )

    registered, events = run_ticket_events(
        alice, solution_id, "prd", {}, reply="   \n\t"
    )

    assert written == []
    assert events[-1].data["status"] == "completed"
    assert job_get(alice, registered.job_id)["status"] == "completed"
    assert document_store.get_document(alice, solution_id, "prd") is None


def test_document_write_failure_keeps_the_job_successful(client, alice, monkeypatch):
    """写 documents 时库出错：任务**仍然算成功**，正文留在 `draft_content` 里。

    正文已经在任务记录里有一份；把整个任务判失败，等于让一次成功的生成看起来像失败。
    跨主接缝：假事件流给出 `part` 定稿与 `done.reply`，`commit_generated` 抛错，
    断言可观察的事件回执、库行终态与正文。
    """
    solution_id = _create_solution(client)

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("考核点：写 documents 时库出错")

    monkeypatch.setattr(document_store, "commit_generated", _boom)

    registered, events = run_ticket_events(
        alice,
        solution_id,
        "prd",
        {},
        StreamEvent(type="part", content="", data={"text": "# 正文"}),
        reply="# 正文",
    )

    assert events[-1].data["status"] == "completed"
    row = job_get(alice, registered.job_id)
    assert row is not None
    assert row["status"] == "completed"
    assert row["draft_content"] == "# 正文"
    assert document_store.get_document(alice, solution_id, "prd") is None


def test_document_write_skip_is_not_a_failure(client, alice, monkeypatch):
    """`commit_generated` 返回 `None`（方案不可见 / 归属不符）也不算失败：只是没地方落。"""
    solution_id = _create_solution(client)
    monkeypatch.setattr(document_store, "commit_generated", lambda *a, **k: None)

    registered, events = run_ticket_events(
        alice, solution_id, "prd", {}, reply="# 正文"
    )

    assert events[-1].data["status"] == "completed"
    row = job_get(alice, registered.job_id)
    assert row is not None and row["status"] == "completed"
    assert document_store.get_document(alice, solution_id, "prd") is None


def test_prompts_version_records_both_upstreams(client, monkeypatch):
    """提示词是 PRD + 接口文档的下游：两条锚点都要记，否则「哪一份上游改过」查不出来。

    `derived_from` 是读取侧 `stale` 的唯一判据；少记一条，那份上游换了版也不会亮黄条，
    用户会拿着一份基于旧接口文档的提示词继续干活。
    """
    register(client)
    uid = _alice_id()
    solution_id = _create_solution(client)
    document_store.save_document(uid, solution_id, "prd", "# PRD 正文")
    document_store.save_document(uid, solution_id, "api_docs", "# 接口文档正文")
    _fake_service(monkeypatch, ScriptedLLM(["提示词正文"]))

    _run_job(
        uid,
        solution_id,
        "prompts",
        {},
        resolved_inputs={"prd_content": "# PRD 正文", "api_docs_content": "# 接口文档正文"},
    )

    doc = document_store.get_document(uid, solution_id, "prompts")
    assert doc is not None
    derived = doc["derived_from"]
    assert set(derived) == {"prd", "api_docs"}
    assert derived["prd"]["content_sha"]
    assert derived["api_docs"]["version_no"] == 1


# ---------------------------------------------------------------------------
# 读旧数据：坏 JSON / 未知类型都不许炸
# ---------------------------------------------------------------------------


def test_usage_parsing_tolerates_legacy_and_broken_values():
    """`generation_jobs.usage` 的容错口径：只认对象，读不出来返回 `None`。

    这一列早年恒为 `NULL`、将来也可能改结构；读不出来时正确反应是「这条任务没有
    用量可展示」，而不是让整个任务列表 500。
    """
    assert parse_usage(None) is None
    assert parse_usage("") is None
    assert parse_usage({"total_calls": 3}) == {"total_calls": 3}
    assert parse_usage('{"total_calls": 4}') == {"total_calls": 4}
    assert parse_usage("[1, 2]") is None
    assert parse_usage("{坏掉的 JSON") is None


def test_db_snapshot_tolerates_corrupt_columns(alice):
    """DB 兜底快照遇到坏 JSON / 旧行：逐字段退化为空值，不整条炸掉。

    坏数据由登记落库后的真实任务行回填（不走任何内部摆位），再走 `open_stream`
    读 DB 兜底路径：前端刷新重连时拿到的仍是形状完整的快照。
    """
    from dbsupport import connect

    registered = build_and_register(
        owner_id=alice, solution_id=None, kind="prd", request={}
    )
    with connect() as conn:
        conn.execute(
            "UPDATE generation_jobs SET status = 'failed', error_code = 'LLM_UPSTREAM_ERROR', "
            "draft_content = '半截正文', completed_sections = '{坏掉的 JSON', "
            "usage = '{坏掉的 JSON', pending_items = '{坏掉的 JSON' WHERE id = ?",
            (registered.job_id,),
        )
        conn.commit()

    snapshot = first_snapshot_event(alice, registered.job_id)

    assert snapshot.data["completed_sections"] == []
    assert snapshot.data["pending_items"] == []
    assert snapshot.data["partial"] is False
    assert snapshot.data["failed_sections"] == []


def test_unknown_kind_fails_loudly_through_the_public_seam(alice):
    """未知任务类型不许被当成「生成成功但没内容」：订阅必须收到 error、任务落 failed。

    造单用生产默认的事件流来源（不传假环境）；未知 kind 在事件流构造那一刻抛错，
    `run_job` 兜底把它变成外部可观察的失败回执。只断言事件序列与终态。
    """
    job_id, events = _run_job(alice, None, "nope", {})

    error = next((event for event in events if event.type == "error"), None)
    assert error is not None, "未知类型被静默当成空内容成功"
    assert "未知的生成任务类型" in error.content
    assert events[-1].type == "run_summary"
    assert events[-1].data["status"] == "failed"
    row = job_get(alice, job_id)
    assert row is not None
    assert row["status"] == "failed"


# ---------------------------------------------------------------------------
# 排空（issue #13）：摘流 → 等上限 → 超时取消落终态
# ---------------------------------------------------------------------------
#
# 排空是一个**可以直接调用的单元**（`job_drain.drain_controller`）：用例直接调它，
# 不起子进程、不发真信号。信号只是这个单元的薄适配层（`main.py` 的 lifespan 里挂），
# 因此下面每一条都在毫秒级完成，且断言的是外部可观察的事实——就绪探针的 503、
# 任务行落成什么终态、订阅者收到哪个事件——而不是内部的哪个标志位、哪次循环。


def test_drain_returns_at_once_when_nothing_is_in_flight(client):
    """无在途任务：排空立刻完成，**不空等**到上限。

    这条守的是「等待是上限不是必等」：30 秒是天花板，不是睡眠时长。写成
    `await asyncio.sleep(30)` 也能让别的用例全绿，只是每次重启白等半分钟。
    """
    assert job_drain.drain_controller.is_draining() is False

    async def scenario() -> tuple[job_drain.DrainReport, float]:
        started = time.monotonic()
        report = await job_drain.drain_controller.drain(timeout=30)
        return report, time.monotonic() - started

    report, elapsed = _run(scenario())

    assert report.in_flight == 0
    assert report.cancelled == 0
    assert report.hit_deadline is False
    assert elapsed < 1.0, f"没有在途任务时不该等满窗口（实测等了 {elapsed:.2f} 秒）"
    # 排空一开始就摘流：就绪探针在这之后必须说「没好」（见下一条用例）
    assert job_drain.drain_controller.is_draining() is True


def test_drain_waits_for_a_job_that_finishes_within_the_window(client, alice, monkeypatch):
    """有在途任务且能在窗口内跑完：等它跑完，任务落**正常终态**，一个都不取消。

    反面写法（曾经很诱人）：收到信号就把所有在途任务一律取消。它同样「留不下
    `running` 尸体」，但用户看到的是「生成到一半被打断」——明明再等两秒就能拿到
    完整的 PRD。排空的价值恰恰在前半句：**先让能跑完的跑完**。
    """
    release = asyncio.Event()

    first_chunk_seen = asyncio.Event()

    async def stream(kind, request):
        yield StreamEvent(type="chunk", content="第一段", data={})
        first_chunk_seen.set()
        await release.wait()  # 模拟「模型还在吐字」：任务确实在途，不是还没开始
        yield StreamEvent(type="done", content="", data={"reply": "完整正文"})

    async def scenario() -> tuple[int, job_drain.DrainReport]:
        registered = build_and_register(
            owner_id=alice,
            solution_id=None,
            kind="prd",
            request={},
            event_stream_source=stream,
        )
        start_registered_job(registered)
        # 等第一个 chunk 被产出：断言的是「排空在等一个真的在跑的任务」
        await asyncio.wait_for(first_chunk_seen.wait(), timeout=5)

        draining = asyncio.create_task(job_drain.drain_controller.drain(timeout=5))
        await asyncio.sleep(0.05)
        assert not draining.done(), "任务还在跑，排空不该提前结束"
        release.set()
        return registered.job_id, await draining

    job_id, report = _run(scenario())

    assert report.in_flight == 1
    assert report.finished == 1, "在窗口内跑完的任务不该被算作取消"
    assert report.cancelled == 0
    assert report.hit_deadline is False
    row = job_get(alice, job_id)
    assert row is not None
    assert row["status"] == "completed", "排空只负责等；终态仍由任务自己收尾"
    assert row["error_code"] == ""


def test_drain_cancels_a_job_that_outlives_the_window(client, alice, monkeypatch):
    """超过窗口仍在跑的任务：**标成已取消、落终态**，订阅者收到 error 事件。

    这条是整张票的承重点。三件事必须同时成立，缺一条用户就没有确定结论：

    1. 库里那一行是终态（`failed` + `CANCELLED`），不是 `running`——否则下次启动
       的 `reap_orphans()` 会把它再收一次失败，用户看到两次「生成失败」；
    2. 订阅者收到**错误**事件（还带原因），不是静默结束、也不是「完成」——
       「流正常结束 + 正文为空」在前端会被当成一次成功（历史 bug 的同一个坑）；
    3. 订阅者不会永远挂着：收哨兵必须有，否则前端停在一个再也不会更新的转圈上。
    """
    never = asyncio.Event()  # 永不 set：任务卡在「模型调用」里，自己走不出来

    async def stream(kind, request):
        yield StreamEvent(type="chunk", content="第一段", data={})
        await never.wait()
        yield StreamEvent(type="done", content="", data={"reply": "完整正文"})

    async def scenario() -> tuple[int, job_drain.DrainReport, list]:
        registered = build_and_register(
            owner_id=alice,
            solution_id=None,
            kind="prd",
            request={},
            event_stream_source=stream,
        )
        start_registered_job(registered)

        # 订阅者先挂上：真实场景是「浏览器正连着这个任务，然后运维重启了容器」
        events: list = []

        async def consume() -> None:
            async for event in job_service.open_stream(alice, registered.job_id):
                events.append(event)

        watcher = asyncio.create_task(consume())
        for _ in range(200):
            if any(event.type == "chunk" for event in events):
                break
            await asyncio.sleep(0.005)

        # 窗口给 0.2 秒：用例不该为了验证「等 30 秒」真的等 30 秒
        report = await job_drain.drain_controller.drain(timeout=0.2)
        # 订阅者必须能自己读完（哨兵），不是被用例硬 cancel 掉
        await asyncio.wait_for(watcher, timeout=5)
        return registered.job_id, report, events

    job_id, report, events = _run(scenario())

    assert report.in_flight == 1
    assert report.cancelled == 1, "超过窗口的任务必须被取消"
    assert report.finished == 0
    assert report.hit_deadline is True
    assert report.waited_seconds >= 0.15, "至少要让窗口走完，不能提前取消"

    row = job_get(alice, job_id)
    assert row is not None
    assert row["status"] == "failed", "超时任务必须落终态，不能留 running"
    assert row["error_code"] == "CANCELLED"
    assert "重启" in row["error_message"]

    error = next((event for event in events if event.type == "error"), None)
    assert error is not None, "订阅者必须收到 error 事件，否则「流结束 + 空正文」会被当成成功"
    assert error.code == "CANCELLED"
    assert "重启" in error.content

    summary = [event for event in events if event.type == "run_summary"]
    assert summary and summary[-1].data["status"] == "failed"
    assert not [
        event
        for event in events
        if event.type == "run_summary" and event.data.get("status") == "completed"
    ], "被取消的任务不能以「完成」收尾"


def test_readyz_returns_503_while_draining(client, temp_skills_root):
    """排空期间就绪探针返回 503——这是编排器/反代「摘流」的唯一信号。

    为什么不满足于「日志里写一句在排空」：编排器不读日志，它只看探针。探针在此期间
    仍说 200，反代会继续把新请求送进来，于是「停止接收新任务」这句承诺当场失效。

    同时钉住另一面：`/healthz` 照旧 200。排空中的进程**是活着的**，让生死探针失败
    会招来一次重启——那正好把这次体面的排空变成一次硬杀。
    """
    write_skill_package(temp_skills_root, "prd-generator")
    assert client.get("/readyz").status_code == 200, "先确认它在排空之前是就绪的"

    job_drain.drain_controller.begin()

    response = client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    # 失败原因要能指出来：只说「没好」的探针，人和编排器都无从判断
    assert body["checks"]["draining"]["ok"] is False
    assert "重启" in body["checks"]["draining"]["detail"]
    assert client.get("/healthz").status_code == 200, "排空中的进程还活着，生死探针不该失败"
    # 详情端点契约不变（它恒 200，是给人看的体检报告）
    assert client.get("/health").status_code == 200


def test_drain_logs_how_many_jobs_it_cancelled(client, alice, monkeypatch, caplog):
    """汇总日志里写明「本次排空取消了几个任务」。

    运维在 `docker compose logs` 里要能一眼看出影响面，而不是去翻每一条任务的终态。
    断言分两层：结构化字段（供日志检索/告警）与渲染后的正文（供人眼），
    两者都指向同一个数——只测其中一层，另一层写错了不会有人发现。
    """
    never = asyncio.Event()

    first_chunk_seen = asyncio.Event()

    async def stream(kind, request):
        yield StreamEvent(type="chunk", content="第一段", data={})
        first_chunk_seen.set()
        await never.wait()

    async def scenario() -> job_drain.DrainReport:
        registered = build_and_register(
            owner_id=alice,
            solution_id=None,
            kind="prd",
            request={},
            event_stream_source=stream,
        )
        start_registered_job(registered)
        await asyncio.wait_for(first_chunk_seen.wait(), timeout=5)
        return await job_drain.drain_controller.drain(timeout=0.1)

    with caplog.at_level(logging.WARNING, logger="backend.services.job_drain"):
        report = _run(scenario())

    assert report.cancelled == 1
    finished = [r for r in caplog.records if getattr(r, "event", "") == "service.drain_finished"]
    assert finished, "排空必须落一行汇总（事件名 service.drain_finished）"
    record = finished[-1]
    assert record.cancelled_jobs == 1
    assert record.in_flight == 1
    assert "取消 1 个" in record.getMessage(), record.getMessage()


def test_shutdown_returns_a_success_exit_code_after_draining(client, alice, monkeypatch):
    """排空结束后进程以**成功**状态退出：退出码 0，不是被信号打断的 128+15。

    为什么值得单独钉：`docker compose restart` 之后容器若以非零码退出，编排器会把一次
    **计划内**的重启记成崩溃（重启计数、告警、自动回滚的判据都会跟着错）。用例不启动
    子进程、不发真信号，所以断言的是那个单元交出的退出码——`main.py` 的信号处理拿到
    它之后才结束进程（`os._exit(code)`，见那里的说明）。

    即便这次排空不得不取消一个任务，退出码仍然是 0：取消是**排空成功的一部分**，
    不是排空失败。
    """
    never = asyncio.Event()

    first_chunk_seen = asyncio.Event()

    async def stream(kind, request):
        yield StreamEvent(type="chunk", content="第一段", data={})
        first_chunk_seen.set()
        await never.wait()

    async def scenario() -> int:
        registered = build_and_register(
            owner_id=alice,
            solution_id=None,
            kind="prd",
            request={},
            event_stream_source=stream,
        )
        start_registered_job(registered)
        await asyncio.wait_for(first_chunk_seen.wait(), timeout=5)
        return await job_drain.drain_controller.shutdown(timeout=0.1)

    code = _run(scenario())

    assert code == 0, "排空走完是正常收尾，退出码必须是 0"
    assert isinstance(code, int)
    assert job_drain.drain_controller.is_draining() is True


def test_signal_handler_installation_never_breaks_startup(client):
    """装不上信号处理器时安静地返回 False，**不许把启动搞崩**。

    两个真实场景：Windows 的 Proactor 事件循环没有 `add_signal_handler`
    （直接抛 `NotImplementedError`），以及应用跑在非主线程（`ValueError`，用例里的
    TestClient 正是这样）。排空本身不依赖信号——它与信号是两个单元，信号只是调用方；
    所以在这些环境里，兜底是 lifespan 关闭阶段的那次排空。
    """
    # 同步用例里没有运行中的事件循环：这一句必须安静地返回 False，而不是抛异常
    assert job_drain.drain_controller.install_signal_handlers() is False


def test_draining_refuses_new_jobs(client):
    """排空期间创建任务返回 503：请求不能被排进一个马上要死的队列。

    只靠就绪探针不够：反代的摘流有几秒延迟，而生成请求是用户点出来的。此时把它
    塞进队列，得到的是「任务起来了、进程随后死了、下次启动才告诉它失败」——
    用户等了三分钟才看到一句「生成失败」。直接说不收，用户立刻知道该重试。
    """
    register(client)
    job_drain.drain_controller.begin()

    response = client.post(
        "/api/jobs",
        json={"kind": "prd", "requirements": "做一个演示产品", "product_name": "演示"},
    )

    assert response.status_code == 503, response.text
    body = response.json()
    assert "重启" in body["detail"]
    # 不新增错误码：错误码是前后端共享契约（前端有一份逐字镜像的枚举），
    # 这里用既有的 INTERNAL_ERROR 承载「服务端暂时不可用」，靠状态码 503 表达「稍后重试」。
    assert body["code"] == "INTERNAL_ERROR"


def test_drain_window_comes_from_the_environment(client, alice, monkeypatch):
    """等待上限是**可配的环境变量**（`PRDWRIGHT_DRAIN_TIMEOUT`，默认 30 秒）。

    为什么不做成常量：30 秒是给容器 `stop_grace_period` 留余量的经验值，而部署形态、
    任务长度、宿主差异都会让这个经验值需要调。做成常量的话，调它就得改代码、重打镜像；
    做成配置则部署与用例各取所需——**用例正因此能在毫秒级验证超时那条路径**。

    这条不只是读一遍配置：它把环境变量调到 0.05 秒，然后**不传 timeout** 去排空一个
    卡住的任务，证明 `drain()` 真的按配置走（而不是只在文档里可配）。
    """
    from backend.core.config import get_settings

    assert get_settings().drain_timeout_seconds == 30.0, "默认值必须是 30 秒"

    monkeypatch.setenv("PRDWRIGHT_DRAIN_TIMEOUT", "0.05")
    get_settings.cache_clear()
    assert get_settings().drain_timeout_seconds == 0.05

    never = asyncio.Event()

    first_chunk_seen = asyncio.Event()

    async def stream(kind, request):
        yield StreamEvent(type="chunk", content="第一段", data={})
        first_chunk_seen.set()
        await never.wait()

    async def scenario() -> job_drain.DrainReport:
        registered = build_and_register(
            owner_id=alice,
            solution_id=None,
            kind="prd",
            request={},
            event_stream_source=stream,
        )
        start_registered_job(registered)
        await asyncio.wait_for(first_chunk_seen.wait(), timeout=5)
        # 不传 timeout：窗口必须来自环境变量
        return await job_drain.drain_controller.drain()

    report = _run(scenario())

    assert report.cancelled == 1
    assert report.waited_seconds < 5, "窗口应当从配置读（0.05 秒），不是写死的 30 秒"
