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

from backend.repositories.generation_job_repo import GenerationJobRepository
from backend.services import document_service, job_service, llm_factory, prd_section_writer
from backend.services.job_bus import job_bus
from backend.services.prd_section_writer import PRD_SECTIONS
from fakes import ScriptedLLM


# ---------------------------------------------------------------------------
# 夹具与工具
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


# ---------------------------------------------------------------------------
# 任务编排：与连接解耦、快照补全、契约不变量
# ---------------------------------------------------------------------------


def test_stream_starts_with_snapshot_and_parts_reassemble_into_reply(alice, monkeypatch):
    """端到端：首事件是 snapshot；`part` 按序拼接即 done.reply；任务落库为 completed。"""
    _fake_service(monkeypatch, ScriptedLLM(["章节正文"]))

    async def scenario() -> tuple[int, list]:
        job_id = job_service.create_job(owner_id=alice, solution_id=5, kind="prd")
        job_service.dispatch(
            job_id,
            owner_id=alice,
            solution_id=5,
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
    # 契约：part 拼接 == 最终正文
    assert "".join(part.data["text"] for part in parts).strip() == done.data["reply"]

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
    assert isinstance(data["provider"], str) and data["provider"]
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


def test_reconnect_after_unsubscribe_completes_remaining(alice, monkeypatch):
    """中途退订后重连：先收快照（含已产出正文），再续上 live 直到 done。"""
    _fake_service(monkeypatch, ScriptedLLM(["章节正文"]))

    async def scenario() -> tuple[list, list]:
        job_id = job_service.create_job(owner_id=alice, solution_id=7, kind="prd")
        job_service.dispatch(
            job_id,
            owner_id=alice,
            solution_id=7,
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


def test_resubscribe_after_completion_sends_snapshot_then_summary(alice, monkeypatch):
    """任务已完成后再订阅：发 snapshot 补全正文，**再补一份 run_summary**，随即结束。

    补发是必须的：收尾的 `run_summary` 在任务结束当刻广播，只在当刻连着的人收得到；
    刷新页面或订阅晚于收尾，就会漏掉它——那样「这次花了多少 token」只在没人看的时候存在。
    终态任务的用量已落库，重连时按库补齐即可。
    """
    _fake_service(monkeypatch, ScriptedLLM(["章节正文"]))

    async def scenario() -> list:
        job_id = job_service.create_job(owner_id=alice, solution_id=9, kind="prd")
        job_service.dispatch(
            job_id,
            owner_id=alice,
            solution_id=9,
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
    assert old_row["error_code"] == "INTERNAL_ERROR"
    assert old_row["error_message"] == "被新任务取代"

    # 新建的是 running
    assert GenerationJobRepository.get(alice, new)["status"] == "running"


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
