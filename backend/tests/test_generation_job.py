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

import pytest
from conftest import login, register

from backend.core.contracts import StreamEvent
from backend.repositories.generation_job_repo import GenerationJobRepository
from backend.services import (
    document_service,
    document_store,
    job_service,
    llm_factory,
    prd_section_writer,
    solution_service,
)
from backend.services.job_bus import job_bus
from backend.services.prd_section_writer import PRD_SECTIONS
from fakes import ScriptedLLM


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------


def _seed_provider() -> str:
    """放一条配置并设为使用中，返回 id。

    ⚠️ ADR-051 之后 `.env` 不再是模型配置的来源：一条配置都没有时
    `active_provider` 是 `None`，回执里的 `protocol` / `model` 会是空串。
    而这两个字段正是「这次走了哪条配置」的唯一凭据，所以凡是要断言它们的用例
    都得先把配置落下来——这不是用例啰嗦，是真实前提。
    """
    from backend.core import runtime_config as rc
    from backend.core.migrations import run_migrations
    from backend.repositories.runtime_config_repo import (
        ModelProviderRepository,
        SystemConfigRepository,
    )

    run_migrations()
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
    """每个用例前后清空进程内运行时与任务集合，避免用例互相污染。"""
    job_service._RUNTIMES.clear()  # noqa: SLF001
    job_service._TASKS.clear()  # noqa: SLF001
    job_bus._subscribers.clear()  # noqa: SLF001
    yield
    job_service._RUNTIMES.clear()  # noqa: SLF001
    job_service._TASKS.clear()  # noqa: SLF001
    job_bus._subscribers.clear()  # noqa: SLF001


def _alice_id() -> int:
    """取已注册用户 alice 的 id（generation_jobs.owner_id 有外键，必须是真实用户）。"""
    from backend.services import auth_service

    user = auth_service.UserRepository.get_by_username("alice")
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


def _run_job(owner_id: int, solution_id: int | None, kind: str, request: dict) -> tuple[int, list]:
    """建任务 → 派发 → 一路订阅到结束，返回 (job_id, 全部事件)。"""

    async def scenario() -> tuple[int, list]:
        job_id = job_service.create_job(owner_id=owner_id, solution_id=solution_id, kind=kind)
        job_service.dispatch(
            job_id, owner_id=owner_id, solution_id=solution_id, kind=kind, request=request
        )
        return job_id, [event async for event in job_service.open_stream(owner_id, job_id)]

    return _run(scenario())


# ---------------------------------------------------------------------------
# 任务编排：与连接解耦、快照补全、契约不变量
# ---------------------------------------------------------------------------


def test_stream_starts_with_snapshot_and_parts_reassemble_into_reply(client, alice, monkeypatch):
    """端到端：首事件是 snapshot；`part` 按序拼接即 done.reply；任务落库为 completed。"""
    _fake_service(monkeypatch, ScriptedLLM(["章节正文"]))
    # 方案必须真实存在：收尾会校验「方案还在不在」，挂在不存在方案上的任务不再算成功
    solution_id = _create_solution(client)
    _seed_provider()

    async def scenario() -> tuple[int, list]:
        job_id = job_service.create_job(owner_id=alice, solution_id=solution_id, kind="prd")
        job_service.dispatch(
            job_id,
            owner_id=alice,
            solution_id=solution_id,
            kind="prd",
            request={"requirements": "做一个演示产品", "product_name": "演示产品"},
        )
        events = [event async for event in job_service.open_stream(alice, job_id)]
        return job_id, events

    job_id, events = _run(scenario())

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
    row = GenerationJobRepository.get(alice, job_id)
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
        job_id = job_service.create_job(owner_id=alice, solution_id=solution_id, kind="prd")
        job_service.dispatch(
            job_id,
            owner_id=alice,
            solution_id=solution_id,
            kind="prd",
            request={"requirements": "做一个演示产品", "product_name": "演示产品"},
        )

        first: list = []
        async for event in job_service.open_stream(alice, job_id):
            first.append(event)
            if len(_parts(first)) >= 2:  # 拿到文档头 + 第一章后断线
                break

        # 断线重连：同一协程内立即重订阅，任务尚未跑完，仍能续流
        second = [event async for event in job_service.open_stream(alice, job_id)]
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
        job_id = job_service.create_job(owner_id=alice, solution_id=solution_id, kind="prd")
        job_service.dispatch(
            job_id,
            owner_id=alice,
            solution_id=solution_id,
            kind="prd",
            request={"requirements": "做一个演示产品", "product_name": "演示产品"},
        )
        # 第一次订阅跑到任务结束
        async for _ in job_service.open_stream(alice, job_id):
            pass
        # 第二次订阅：snapshot + run_summary 后返回，不挂起
        return [event async for event in job_service.open_stream(alice, job_id)]

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

    async def scenario() -> tuple[int, list]:
        job_id = job_service.create_job(owner_id=alice, solution_id=None, kind="prd")
        job_service.dispatch(
            job_id,
            owner_id=alice,
            solution_id=None,
            kind="prd",
            request={"requirements": "做一个演示产品", "product_name": "演示产品"},
        )
        return job_id, [event async for event in job_service.open_stream(alice, job_id)]

    job_id, events = _run(scenario())

    error = next(event for event in events if event.type == "error")
    assert error.code == "LLM_UPSTREAM_ERROR"
    assert "生成失败" in error.content

    # 收尾仍发 run_summary（status=failed）
    assert events[-1].type == "run_summary"
    assert events[-1].data["status"] == "failed"

    row = GenerationJobRepository.get(alice, job_id)
    assert row is not None
    assert row["status"] == "failed"
    assert row["error_code"] == "LLM_UPSTREAM_ERROR"
    assert row["error_message"].strip() != ""
    # 半截正文（文档头 + 已流出的第一段）保留
    assert row["draft_content"].strip() != ""


def test_open_stream_rejects_other_owner(alice, monkeypatch):
    """owner 不符时不泄内容：退化为失败/未命中的 404 语义（流内不返回任何事件）。"""

    async def scenario() -> list:
        job_id = job_service.create_job(owner_id=alice, solution_id=None, kind="prompts")
        # 用另一个不存在的 owner 订阅：既非内存运行时归属，DB 也查不到
        return [event async for event in job_service.open_stream(alice + 999, job_id)]

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
        job_id = job_service.create_job(owner_id=alice, solution_id=None, kind="prd")
        job_service.dispatch(
            job_id,
            owner_id=alice,
            solution_id=None,
            kind="prd",
            request={"requirements": "做一个演示产品"},
        )
        async for _ in job_service.open_stream(alice, job_id):
            pass
        return job_id

    job_id = _run(scenario())
    assert calls["count"] == 0
    row = GenerationJobRepository.get(alice, job_id)
    assert row is not None and row["status"] == "completed"


# ---------------------------------------------------------------------------
# 仓储：supersede / reap / 归属
# ---------------------------------------------------------------------------


def test_new_job_supersedes_previous_running(alice):
    """同一 owner + solution + kind 至多一个 running：新建前旧 running 被取代。"""
    old = job_service.create_job(owner_id=alice, solution_id=3, kind="prd")
    new = job_service.create_job(owner_id=alice, solution_id=3, kind="prd")

    assert old != new
    old_row = GenerationJobRepository.get(alice, old)
    assert old_row is not None
    assert old_row["status"] == "failed"
    # 有明确原因，不是笼统的 INTERNAL_ERROR：这个任务没出错，是被人接手了
    assert old_row["error_code"] == "SUPERSEDED"
    assert old_row["error_message"] == "被新任务取代"

    # 新建的是 running
    assert GenerationJobRepository.get(alice, new)["status"] == "running"


def test_superseded_job_does_not_write_document(client, alice, monkeypatch):
    """被顶替的旧任务收尾时**不写文档**，并落 `failed + SUPERSEDED`。

    只把库里那一行改成 failed 是不够的：内存里的旧运行时并不知道自己被取代，
    它收尾时照样 `_write_document`，把旧稿盖在新稿上——用户看到「新任务跑完了，
    正文却是上一版的」，不报错、只是内容对不上。因此新建任务时两处都要标记。
    """
    monkeypatch.setattr(job_service.solution_service, "solution_exists", lambda *a, **k: True)
    solution_id = _create_solution(client)

    old = job_service.create_job(owner_id=alice, solution_id=solution_id, kind="prd")
    runtime = job_service.JobRuntime(
        job_id=old, owner_id=alice, solution_id=solution_id, kind="prd"
    )
    runtime.started_at = 0.0
    runtime.final_reply = "# 旧稿"
    runtime.finalized_parts = ["# 旧稿"]
    job_service._RUNTIMES[old] = runtime  # noqa: SLF001 - 直接摆一支在途运行时

    # 新任务接手：库里的旧行与内存里的旧运行时都要被标记
    job_service.create_job(owner_id=alice, solution_id=solution_id, kind="prd")
    assert runtime.superseded is True

    job_service._finalize(runtime, "completed")  # noqa: SLF001 - 收尾是待测行为

    row = GenerationJobRepository.get(alice, old)
    assert row is not None
    assert row["status"] == "failed"
    assert row["error_code"] == "SUPERSEDED"
    # 关键：正文没有被写进 documents（这一份是别人要写的位置）
    assert document_store.get_document(alice, solution_id, "prd") is None


def test_find_active_ignores_other_solution_and_kind(alice):
    """`find_active` 精确匹配 owner + solution + kind，且只认 running。"""
    job_service.create_job(owner_id=alice, solution_id=1, kind="prd")
    assert GenerationJobRepository.find_active(alice, solution_id=1, kind="prd") is not None
    assert GenerationJobRepository.find_active(alice, solution_id=2, kind="prd") is None
    assert GenerationJobRepository.find_active(alice, solution_id=1, kind="prompts") is None
    # solution_id=None 的匹配
    none_job = job_service.create_job(owner_id=alice, solution_id=None, kind="prompts")
    active = GenerationJobRepository.find_active(alice, solution_id=None, kind="prompts")
    assert active is not None and active["id"] == none_job


def test_reap_orphans_marks_running_as_failed(alice):
    """启动回收：上次残留的 running 全部置 failed。"""
    job_id = GenerationJobRepository.create(alice, "prd", None)
    assert GenerationJobRepository.get(alice, job_id)["status"] == "running"

    job_service.reap_orphans()

    row = GenerationJobRepository.get(alice, job_id)
    assert row["status"] == "failed"
    assert row["error_code"] == "INTERNAL_ERROR"
    assert row["error_message"] == "服务重启，任务已中断"


def test_owner_isolation_in_repository(alice):
    """仓储层归属隔离：别人的任务等同不存在。"""
    job_id = GenerationJobRepository.create(alice, "prd", None)
    assert GenerationJobRepository.get(alice + 999, job_id) is None
    assert GenerationJobRepository.list_by_owner(alice + 999) == []


# ---------------------------------------------------------------------------
# HTTP 面
# ---------------------------------------------------------------------------


def test_create_job_returns_201_and_persists(client, monkeypatch):
    """POST /api/jobs 返回 201 与 job_id，并在库里落一行。"""
    register(client)
    uid = _alice_id()
    _fake_service(monkeypatch, ScriptedLLM(["正文"]))

    response = client.post(
        "/api/jobs",
        json={"kind": "prd", "requirements": "做一个演示产品", "product_name": "演示"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["job_id"] > 0
    assert body["status"] == "running"

    row = GenerationJobRepository.get(uid, body["job_id"])
    assert row is not None
    assert row["kind"] == "prd"
    # prd 任务会计算并落输入指纹（供后续续传 / 缓存判断输入是否变化）
    assert row["input_fingerprint"]


def test_create_job_validates_required_input_by_kind(client):
    """按 kind 校验必填：缺输入返回 400 INVALID_REQUEST（不是 422）。"""
    register(client)

    for payload in (
        {"kind": "prd"},  # summary 与 requirements 皆空
        {"kind": "api_docs"},  # 缺 prd_content
        {"kind": "prompts"},  # 缺 prd_content
        {"kind": "optimize", "document": "x"},  # 缺 instruction
        {"kind": "optimize", "instruction": "改一下"},  # 缺 document
    ):
        response = client.post("/api/jobs", json=payload)
        assert response.status_code == 400, payload
        assert response.json()["code"] == "INVALID_REQUEST"


def test_create_prompts_job_accepts_optional_api_docs(client, monkeypatch):
    """`api_docs_content` 是**可选**入参：带上它不该被门口挡掉。

    「我有 PRD + 接口文档」入口比「我有 PRD」多带一份材料，两者走的是同一个 `prompts`
    任务类型。这里只守「带上了也能建任务」这一件事——材料怎么走到模型面前由
    `test_prompts_input.py` 单独覆盖。
    """
    register(client)
    _fake_service(monkeypatch, ScriptedLLM(["# 提示词套件"]))

    response = client.post(
        "/api/jobs",
        json={
            "kind": "prompts",
            "prd_content": "# PRD 正文",
            "api_docs_content": "# 接口文档正文",
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["job_id"] > 0


def test_get_job_status(client):
    """GET /api/jobs/{id} 返回状态摘要，且不含 draft_content。"""
    register(client)
    uid = _alice_id()
    job_id = GenerationJobRepository.create(uid, "prompts", None)

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
    alice_job = GenerationJobRepository.create(uid, "prd", None)
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

    running_prd = GenerationJobRepository.create(uid, "prd", None)
    running_prompts = GenerationJobRepository.create(uid, "prompts", None)
    finished = GenerationJobRepository.create(uid, "prd", None)
    GenerationJobRepository.finish(
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


def test_create_job_without_solution_id_is_allowed(client):
    """solution_id 可空（快捷入口逃生口），能建且落库为 NULL。"""
    register(client)
    uid = _alice_id()

    response = client.post("/api/jobs", json={"kind": "prompts", "prd_content": "# PRD"})
    assert response.status_code == 201
    row = GenerationJobRepository.get(uid, response.json()["job_id"])
    assert row is not None
    assert row["solution_id"] is None


def test_stream_endpoint_emits_snapshot_first(client, monkeypatch):
    """端到端：GET /api/jobs/{id}/stream 首事件是 snapshot（SSE 分帧走真实实现）。"""
    register(client)
    _fake_service(monkeypatch, ScriptedLLM(["正文"]))

    created = client.post(
        "/api/jobs",
        json={"kind": "prd", "requirements": "做一个演示产品", "product_name": "演示产品"},
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

    _run_job(uid, solution_id, "api_docs", {"prd_content": "# 演示产品 PRD"})

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
    _job_id, events = _run_job(uid, solution_id, "prd", {"requirements": "做一个演示产品"})

    assert events[-1].data["status"] == "failed"
    doc = document_store.get_document(uid, solution_id, "prd")
    assert doc is not None
    assert doc["content"] == "# 上一版 PRD"


def test_job_without_solution_does_not_write_document(client, monkeypatch):
    """没有方案可挂（`solution_id=None`）时任务照常成功，只是没有文档可写。"""
    register(client)
    uid = _alice_id()
    _fake_service(monkeypatch, ScriptedLLM(["正文"]))

    job_id, events = _run_job(uid, None, "prompts", {"prd_content": "# PRD"})

    assert events[-1].data["status"] == "completed"
    assert GenerationJobRepository.get(uid, job_id)["status"] == "completed"


def test_regenerating_prd_keeps_downstream_documents(client, monkeypatch):
    """重新生成 PRD 后下游**保留**：它们是旧上游的产物，但不再被静默删除。

    过期与否交给读取侧的 `stale` 标记（比较 `derived_from` 记录的内容指纹），
    用户可以自己决定重新生成还是继续用——删除会让旧内容与澄清线程一起消失。
    """
    register(client)
    uid = _alice_id()
    solution_id = _create_solution(client)
    document_store.save_document(uid, solution_id, "prd", "# 旧 PRD")
    document_store.save_document(uid, solution_id, "api_docs", "# 旧接口文档")
    document_store.save_document(uid, solution_id, "prompts", "# 旧提示词")

    _fake_service(monkeypatch, ScriptedLLM(["章节正文"]))
    _run_job(uid, solution_id, "prd", {"requirements": "做一个演示产品", "product_name": "演示产品"})

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
    _run_job(uid, solution_id, "api_docs", {"prd_content": "# PRD"})

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


def test_create_job_persists_doc_kind_from_request(alice):
    """`create_job` 在**创建时**就把 doc_kind 算出来落列（不是收尾时才算）。

    创建时能算是因为它是纯请求推导（`_target_product_kind`），不依赖入口模式或前端快照。
    晚算的话，进程重启后 DB 兜底快照与 `GET /api/jobs` 就永远答不出「改的是哪一份」。
    """
    job_id = job_service.create_job(
        owner_id=alice,
        solution_id=None,
        kind="optimize",
        request={"doc_kind": "api_docs"},
    )
    row = GenerationJobRepository.get(alice, job_id)
    assert row is not None
    assert row["doc_kind"] == "api_docs"

    # 整篇生成的产物由 kind 直接决定，不依赖请求体
    prd_id = job_service.create_job(
        owner_id=alice, solution_id=None, kind="prd", request={"requirements": "x"}
    )
    assert GenerationJobRepository.get(alice, prd_id)["doc_kind"] == "prd"


def test_snapshot_events_carry_doc_kind():
    """内存快照带 `doc_kind`；取不到时是 `None`，**不猜成 prd**。

    「不猜」是硬口径：猜错等于把优化接口文档的结果写进 PRD 正文，属于静默的数据损坏。
    """
    optimize = job_service.JobRuntime(
        job_id=1,
        owner_id=1,
        solution_id=None,
        kind="optimize",
        request={"doc_kind": "api_docs"},
    )
    assert job_service._snapshot_event(optimize).data["doc_kind"] == "api_docs"  # noqa: SLF001

    prd = job_service.JobRuntime(job_id=2, owner_id=1, solution_id=None, kind="prd")
    assert job_service._snapshot_event(prd).data["doc_kind"] == "prd"  # noqa: SLF001

    unknown = job_service.JobRuntime(job_id=3, owner_id=1, solution_id=None, kind="optimize")
    assert job_service._snapshot_event(unknown).data["doc_kind"] is None  # noqa: SLF001


def test_db_snapshot_event_carries_doc_kind(alice):
    """DB 兜底快照同样带 `doc_kind`；旧行（空串）退化成 `None`。"""
    job_id = GenerationJobRepository.create(alice, "prompts", None, doc_kind="prompts")
    row = GenerationJobRepository.get(alice, job_id)
    assert row is not None
    assert job_service._db_snapshot_event(row).data["doc_kind"] == "prompts"  # noqa: SLF001

    legacy = GenerationJobRepository.create(alice, "optimize", None)
    row = GenerationJobRepository.get(alice, legacy)
    assert row is not None
    assert job_service._db_snapshot_event(row).data["doc_kind"] is None  # noqa: SLF001


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

    _run_job(uid, solution_id, "prd", {"requirements": "做一个演示产品", "product_name": "演示产品"})

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
    _run_job(uid, solution_id, "prd", {"requirements": "做一个演示产品", "product_name": "演示产品"})

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
        {
            "document": "# 原 PRD",
            "history": [{"role": "user", "content": "登录方式确定为手机号"}],
            "doc_kind": "prd",
        },
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
    job_id = GenerationJobRepository.create(alice, "api_docs", None)
    GenerationJobRepository.persist_progress(
        alice,
        job_id,
        draft_content="正文",
        completed_sections=[],
        phase="writing",
        pending_items=items,
    )
    row = GenerationJobRepository.get(alice, job_id)
    assert json.loads(row["pending_items"]) == items


def test_finish_writes_pending_items(alice):
    """成功收尾把待确认清单与终态一并落库。"""
    items = _sample_items()
    job_id = GenerationJobRepository.create(alice, "prompts", None)
    GenerationJobRepository.finish(
        alice,
        job_id,
        draft_content="正文",
        completed_sections=[],
        pending_items=items,
    )
    row = GenerationJobRepository.get(alice, job_id)
    assert row["status"] == "completed"
    assert json.loads(row["pending_items"]) == items


def test_fail_writes_pending_items(alice):
    """失败收尾同样落清单：失败的任务也产出了待确认项，不该丢掉。"""
    items = _sample_items()
    job_id = GenerationJobRepository.create(alice, "prompts", None)
    GenerationJobRepository.fail(
        alice,
        job_id,
        error_code="LLM_UPSTREAM_ERROR",
        error_message="上游中断",
        draft_content="半截正文",
        completed_sections=[],
        pending_items=items,
    )
    row = GenerationJobRepository.get(alice, job_id)
    assert row["status"] == "failed"
    assert json.loads(row["pending_items"]) == items


def test_db_snapshot_event_carries_pending_items(alice):
    """DB 兜底快照（`_db_snapshot_event`）携带待确认清单，刷新重连后前端直接展示。"""
    items = _sample_items()
    job_id = GenerationJobRepository.create(alice, "api_docs", None)
    GenerationJobRepository.persist_progress(
        alice,
        job_id,
        draft_content="正文",
        completed_sections=[{"section_id": "x", "heading": "小节", "text": "t"}],
        phase="writing",
        pending_items=items,
    )
    row = GenerationJobRepository.get(alice, job_id)
    snapshot = job_service._db_snapshot_event(row)  # noqa: SLF001 - 测内部快照构造
    assert snapshot.data["pending_items"] == items


def test_snapshot_event_carries_pending_items():
    """内存运行时快照（`_snapshot_event`）携带待确认清单。"""
    items = _sample_items()
    runtime = job_service.JobRuntime(
        job_id=1, owner_id=1, solution_id=None, kind="api_docs", pending_items=items
    )
    snapshot = job_service._snapshot_event(runtime)  # noqa: SLF001 - 测内部快照构造
    assert snapshot.data["pending_items"] == items


def test_snapshot_event_carries_stage_output():
    """内存运行时快照带上**当前阶段的原始输出**：刷新重连后审核面板不会空着。

    这条守的是「刷新后要重新接回流式输出」：阶段输出从前只活在前端内存里，
    一刷新就没了；现在它由任务运行时持有，随 `snapshot.stage_text` 一道回来。
    """
    runtime = job_service.JobRuntime(job_id=1, owner_id=1, solution_id=None, kind="api_docs")
    job_service._apply_event(  # noqa: SLF001 - 测内部事件归并
        runtime, StreamEvent(type="stage", content="", data={"stage": "reviewing"})
    )
    for piece in ('{"verdict"', ':"revise"}', ""):
        job_service._apply_event(  # noqa: SLF001
            runtime,
            StreamEvent(type="stage_chunk", content=piece, data={"stage": "reviewing"}),
        )

    snapshot = job_service._snapshot_event(runtime)  # noqa: SLF001

    assert snapshot.data["phase"] == "reviewing"
    assert snapshot.data["stage_text"] == '{"verdict":"revise"}'


def test_stage_change_clears_previous_stage_output():
    """换阶段清空上一阶段的输出：审核意见不该留在改写阶段的面板里。"""
    runtime = job_service.JobRuntime(job_id=1, owner_id=1, solution_id=None, kind="api_docs")
    job_service._apply_event(  # noqa: SLF001
        runtime, StreamEvent(type="stage", content="", data={"stage": "reviewing"})
    )
    job_service._apply_event(  # noqa: SLF001
        runtime, StreamEvent(type="stage_chunk", content="审核意见", data={"stage": "reviewing"})
    )
    job_service._apply_event(  # noqa: SLF001
        runtime, StreamEvent(type="stage", content="", data={"stage": "rewriting"})
    )
    assert runtime.stage_text == ""

    job_service._apply_event(  # noqa: SLF001
        runtime, StreamEvent(type="stage_chunk", content="改写稿", data={"stage": "rewriting"})
    )
    assert runtime.stage_text == "改写稿"


def test_stage_chunk_never_leaks_into_draft_text():
    """阶段输出与正文缓冲**严格分道**：审核 JSON 不能出现在会落库的正文里。"""
    runtime = job_service.JobRuntime(job_id=1, owner_id=1, solution_id=None, kind="api_docs")
    job_service._apply_event(  # noqa: SLF001
        runtime, StreamEvent(type="stage_chunk", content='{"verdict":"pass"}', data={"stage": "reviewing"})
    )

    assert runtime.live_buffer == ""
    assert job_service._draft(runtime) == ""  # noqa: SLF001 - 测内部草稿拼装


def test_db_snapshot_event_has_empty_stage_text(alice):
    """DB 兜底快照（进程重启后）没有阶段输出：显式给空串，不让前端拿到 `undefined`。"""
    job_id = GenerationJobRepository.create(alice, "api_docs", None)
    row = GenerationJobRepository.get(alice, job_id)
    snapshot = job_service._db_snapshot_event(row)  # noqa: SLF001 - 测内部快照构造
    assert snapshot.data["stage_text"] == ""


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
    row = GenerationJobRepository.get(uid, job_id)
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
    target = GenerationJobRepository.create(alice, "prd", 11)
    other_solution = GenerationJobRepository.create(alice, "prd", 12)
    finished = GenerationJobRepository.create(alice, "prd", 11)
    GenerationJobRepository.finish(alice, finished, draft_content="正文", completed_sections=[])

    # 另一个 owner 恰好也有一个挂在同一个 solution_id 上的任务
    register(client, username="bob")
    from backend.services import auth_service

    bob = auth_service.UserRepository.get_by_username("bob")
    assert bob is not None
    bob_job = GenerationJobRepository.create(int(bob["id"]), "prd", 11)

    affected = GenerationJobRepository.cancel_running(alice, solution_id=11)

    assert affected == 1
    cancelled = GenerationJobRepository.get(alice, target)
    assert cancelled["status"] == "failed"
    assert cancelled["error_code"] == "CANCELLED"
    assert cancelled["error_message"] == "方案已删除，任务已取消"

    # 别的方案、别的 owner、已经结束的任务都不动
    assert GenerationJobRepository.get(alice, other_solution)["status"] == "running"
    assert GenerationJobRepository.get(int(bob["id"]), bob_job)["status"] == "running"
    assert GenerationJobRepository.get(alice, finished)["status"] == "completed"


def test_finalize_skips_document_when_solution_gone(client, monkeypatch):
    """方案已删除时收尾**不写文档**，并把任务落成失败态。

    这里刻意不置 `runtime.cancelled`：模拟「删除发生在标记生效之前」的那个时间窗——
    运行时不在了 / 标记没赶上，只剩「方案还在不在」这一条事实可查。
    """
    register(client)
    uid = _alice_id()
    solution_id = _create_solution(client)
    job_id = GenerationJobRepository.create(uid, "prd", solution_id)
    runtime = job_service.JobRuntime(
        job_id=job_id,
        owner_id=uid,
        solution_id=solution_id,
        kind="prd",
        final_reply="# 已经生成好的正文",
    )

    assert solution_service.delete_solution(uid, solution_id) is True
    assert runtime.cancelled is False

    written: list = []
    monkeypatch.setattr(
        document_store, "commit_generated", lambda *a, **k: written.append((a, k))
    )

    job_service._finalize(runtime, "completed")  # noqa: SLF001 - 直接测收尾分支

    assert written == [], "方案已删除时不该再往 documents 里写"
    assert runtime.status == "failed"
    row = GenerationJobRepository.get(uid, job_id)
    assert row is not None
    assert row["status"] == "failed"
    assert row["error_code"] == "CANCELLED"
    assert document_store.list_documents(uid, solution_id) == []


def test_run_job_stops_consuming_events_after_cancel(alice, monkeypatch):
    """运行时被标记取消后，下一个事件就不再消费（假事件流证明后续事件没被取走）。"""
    job_id = GenerationJobRepository.create(alice, "prd", None)
    runtime = job_service.JobRuntime(
        job_id=job_id, owner_id=alice, solution_id=None, kind="prd"
    )
    produced: list[str] = []

    async def stream():
        produced.append("第一段")
        yield StreamEvent(type="chunk", content="第一段", data={})
        # 第一个事件处理完之后，用户删除了方案（`cancel_for_solution` 会置这一位）
        runtime.cancelled = True
        produced.append("第二段")
        yield StreamEvent(type="chunk", content="第二段", data={})
        produced.append("done")
        yield StreamEvent(type="done", content="", data={"reply": "完整正文"})

    monkeypatch.setattr(job_service, "_build_event_stream", lambda kind, request: stream())

    _run(job_service.run_job(runtime))

    # 只取走了两个事件就 break：第三个（done）从未被请求，正文不可能是「完整正文」
    assert produced == ["第一段", "第二段"]
    assert runtime.live_buffer == "第一段"
    assert runtime.final_reply == ""

    row = GenerationJobRepository.get(alice, job_id)
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
    doomed = GenerationJobRepository.create(uid, "prd", solution_id)
    untouched = GenerationJobRepository.create(uid, "api_docs", other_id)

    assert client.delete(f"/api/solutions/{solution_id}").status_code == 200

    row = GenerationJobRepository.get(uid, doomed)
    assert row is not None
    assert row["status"] == "failed"
    assert row["error_code"] == "CANCELLED"
    assert GenerationJobRepository.get(uid, untouched)["status"] == "running"


# ---------------------------------------------------------------------------
# 分章节的按章缓冲：多章同时逐字流，但各存各的
# ---------------------------------------------------------------------------


def _runtime_with_sections() -> job_service.JobRuntime:
    """一支 PRD 运行时（不需要真建任务：这一组测的是事件到运行时的映射）。"""
    runtime = job_service.JobRuntime(
        job_id=1, owner_id=1, solution_id=1, kind="prd"
    )
    runtime.started_at = 0.0
    return runtime


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


def test_section_chunks_are_buffered_per_section():
    """每章的增量各进各的格子。

    混在一个 `live_buffer` 里就再也分不开了——前端只能整批丢弃，
    那正是「PRD 只能等整章定稿才蹦出来」的成因。
    """
    runtime = _runtime_with_sections()

    job_service._apply_event(runtime, _chunk("第一章", "overview", 0))
    job_service._apply_event(runtime, _chunk("第二章", "features", 1))
    job_service._apply_event(runtime, _chunk("续写", "overview", 0))

    assert runtime.live_by_index[0].section_id == "overview"
    assert runtime.live_by_index[0].text == "第一章续写"
    assert runtime.live_by_index[1].text == "第二章"
    # 无归属的整篇缓冲保持干净：它只服务「不分章」的链路（文档 / 对话）
    assert runtime.live_buffer == ""


def test_part_clears_only_its_own_section():
    """一章定稿只清这一章：并行时别的章还在写，清全场会把它们的字一起抹掉。"""
    runtime = _runtime_with_sections()
    job_service._apply_event(runtime, _chunk("甲", "overview", 0))
    job_service._apply_event(runtime, _chunk("乙", "features", 1))

    job_service._apply_event(runtime, _part("## 1. 甲\n\n", "overview", 0))

    assert 0 not in runtime.live_by_index, "定稿的章半成品该被清掉"
    assert runtime.live_by_index[1].text == "乙", "别的章一个字都不能丢"
    assert runtime.parts_by_index[0] == "## 1. 甲\n\n"


def test_rewriting_clears_the_section_so_draft_and_rewrite_do_not_stack():
    """改写必须清掉该章半成品：否则初稿与改写稿会在同一张卡片里叠着长。"""
    runtime = _runtime_with_sections()
    job_service._apply_event(runtime, _chunk("初稿", "overview", 0))

    job_service._apply_event(runtime, _stage("rewriting", "overview", 0))
    job_service._apply_event(runtime, _chunk("改后", "overview", 0))

    assert runtime.live_by_index[0].text == "改后"
    assert runtime.live_by_index[0].phase == "rewriting"


def test_retry_reannounced_writing_resets_the_section():
    """单章重试会重报 `writing`：那是「从头重来」的信号，必须清该章。"""
    runtime = _runtime_with_sections()
    job_service._apply_event(runtime, _chunk("第一轮的一半", "overview", 0))

    job_service._apply_event(runtime, _stage("writing", "overview", 0))
    assert runtime.live_by_index[0].text == ""

    job_service._apply_event(runtime, _chunk("第二轮", "overview", 0))
    assert runtime.live_by_index[0].text == "第二轮"


def test_draft_interleaves_finalized_and_live_by_index():
    """`_draft` 必须按 index 交错归并，不能「先定稿、后半成品」。

    并行时第 1 章可能还在写、第 2 章已经定稿；简单拼接会把正文排成 2、3、1，
    而 `_draft` 是**进度落库**与「重连兜底」的正文来源——顺序错了刷新后就是错版。
    """
    runtime = _runtime_with_sections()
    job_service._apply_event(runtime, _header_part("# 文档头\n\n"))
    job_service._apply_event(runtime, _chunk("第1章写到一半", "overview", 0))
    job_service._apply_event(runtime, _part("第2章定稿\n\n", "features", 1))

    assert job_service._draft(runtime) == "# 文档头\n\n第1章写到一半第2章定稿\n\n"


def test_snapshot_carries_live_by_section_but_not_the_finalized_ones():
    """快照要带上「正在写的几章」，且不能与已定稿章节重复。

    重复的后果是刷新后同一段正文出现两次；只发定稿章节的后果则是
    「正在写的那几章刷新后一片空白，直到定稿才整段出现」。
    """
    runtime = _runtime_with_sections()
    job_service._apply_event(runtime, _chunk("甲半截", "overview", 0))
    job_service._apply_event(runtime, _part("乙定稿\n\n", "features", 1))
    job_service._apply_event(runtime, _chunk("丙半截", "pages", 2))

    snapshot = job_service._snapshot_event(runtime)  # noqa: SLF001 - 待测行为
    live = snapshot.data["live_by_section"]

    assert [item["section_id"] for item in live] == ["overview", "pages"]
    assert [item["index"] for item in live] == [0, 2]
    assert live[0]["text"] == "甲半截"
    assert all(item["section_id"] != "features" for item in live)
    # 定稿章节照旧在 `completed_sections` 里，各走各的字段
    assert [item["section_id"] for item in snapshot.data["completed_sections"]] == ["features"]


def test_db_snapshot_has_no_live_by_section():
    """DB 兜底快照显式给空数组：过程字不落库（同 `stage_text`）。

    进程重启后拿到的就是这份快照——它只有已定稿章节，前端据此重建看板即可。
    """
    row = {
        "id": 1,
        "kind": "prd",
        "doc_kind": "prd",
        "status": "running",
        "phase": "writing",
        "draft_content": "# 文档头\n\n",
        "completed_sections": "[]",
        "solution_id": 1,
    }
    snapshot = job_service._db_snapshot_event(row)  # noqa: SLF001 - 待测行为

    assert snapshot.data["live_by_section"] == []
