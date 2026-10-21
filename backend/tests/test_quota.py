"""每日额度的口径与拦截（v4.5 T5-2）。

这一层的用例分三类，重心各不同：

1. **默认零变化**：没配任何额度时必须完全放行。这条一旦破了，升级当天所有人
   都会被拦——而那时页面上还没有任何地方能调。
2. **口径**：次数、token、并发三条统计各自的纳入条件。算错了不会报错，
   数字看着都合理，只是「今天明明没用几次却被拦了」或「开一堆任务都没拦住」。
3. **边界**：日界（跨时区）、三态（-1 / 0 / N）、`chat` 豁免、只告警不拦截。
"""

from __future__ import annotations

import json

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from backend.core import runtime_config as rc
from backend.core.db import connect, utc_now, utc_now_text
from backend.repositories import quota_repo
from backend.repositories.runtime_config_repo import SystemConfigRepository
from backend.services import quota_service
from backend.services.auth_service import User


@pytest.fixture(autouse=True)
def fresh_config():
    """每个用例都从干净的配置快照开始（否则上一个用例的覆盖项会渗过来）。"""
    rc.invalidate()
    yield
    rc.invalidate()


def _user(user_id: int = 1, username: str = "u1") -> User:
    return User(id=user_id, username=username, is_active=True)


def _ensure_user(user_id: int) -> None:
    """`generation_jobs.owner_id` 有外键指向 `users`，造任务前必须先有这个人。

    用 `INSERT OR IGNORE` 而不是查一下再插：用例里同一个 id 会被反复用到
    （多数用例只关心「谁的任务」），重复插入静默跳过最省事。
    """
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (id, username, password_hash, salt, created_at)"
            " VALUES (?, ?, 'hash', X'', ?)",
            (user_id, f"user{user_id}", utc_now_text()),
        )
        conn.commit()


def _job(owner_id: int, *, status: str = "completed", prompt: int = 0, completion: int = 0,
         error_code: str = "", created_at: str | None = None) -> None:
    """直接写一行任务，用于构造统计口径。"""
    _ensure_user(owner_id)
    moment = created_at or utc_now_text()
    with connect() as conn:
        conn.execute(
            "INSERT INTO generation_jobs (owner_id, kind, status, error_code, prompt_tokens,"
            " completion_tokens, created_at, updated_at) VALUES (?, 'prd', ?, ?, ?, ?, ?, ?)",
            (owner_id, status, error_code, prompt, completion, moment, moment),
        )
        conn.commit()


def _set_global(**values: int | bool) -> None:
    """写全局额度配置并让缓存立刻失效。

    ⚠️ 必须 `json.dumps` 而不是 `str`：`system_config.value` 存的是 **JSON 编码**
    的标量（生产路径 `admin_config_service` 也是这么写的），读的时候走
    `json.loads`。用 `str(False)` 会写出 `"False"` —— 那不是合法 JSON，
    解析失败后静默退回默认值 True，于是「只告警不拦截」的用例验的是个假象。
    """
    SystemConfigRepository.upsert_many(
        {k: json.dumps(v) for k, v in values.items()}, updated_by=1
    )
    rc.invalidate()


# ---------------------------------------------------------------------------
# 默认零变化
# ---------------------------------------------------------------------------


def test_default_is_unlimited_and_never_blocks(isolated_db):  # noqa: ARG001
    """一条任务都没有、也没配额度时，任何生成都必须放行。

    这是「升级后行为不变」的落点：三个默认值全是 0（不限），
    所以 `check` 在读用量之前就该返回。
    """
    quota_service.check(_user(), "prd")
    quota_service.check(_user(), "api_docs")
    quota_service.check(_user(), "prompts")


def test_chat_is_never_limited(isolated_db):  # noqa: ARG001
    """对话不受配额管——它是生成的前置。

    若连对话都限，用户额度用完时连「为什么被拒」都问不了，而澄清对话本来
    就是生成流程的一部分（改需求、确认清单）。
    """
    _set_global(quota_daily_task_limit=0)
    # 造一条已收尾任务，并用「次数上限 = 当前已用」把额度耗光
    _job(1, prompt=10, completion=5)
    _set_global(quota_daily_task_limit=1)

    with pytest.raises(Exception):
        quota_service.check(_user(), "prd")

    # 同一时刻，chat 必须照常通过
    quota_service.check(_user(), "chat")


# ---------------------------------------------------------------------------
# 口径：次数
# ---------------------------------------------------------------------------


def test_failed_and_cancelled_do_not_consume_quota(isolated_db):  # noqa: ARG001
    """失败与被取消的任务不吃次数。

    否则一次上游故障（或用户自己点了停止）就会把当天的机会扣掉，
    而用户完全无从申诉——他没做错任何事。
    """
    _job(1, status="completed")
    _job(1, status="failed", error_code="LLM_UPSTREAM_ERROR")
    _job(1, status="failed", error_code="CANCELLED")
    _job(1, status="failed", error_code="SUPERSEDED")

    usage = quota_service.usage_today(1)
    assert usage.tasks == 1, "只有 completed 该计入"


def test_running_counts_but_does_not_double_count_as_finished(isolated_db):  # noqa: ARG001
    """在飞任务占用次数——否则连发 N 个请求每个都「还没满」全通过。"""
    _job(1, status="running")
    assert quota_service.usage_today(1).tasks == 1


def test_token_counts_every_status(isolated_db):  # noqa: ARG001
    """token 算全部状态的实际消耗：跑了就跑掉了，失败的任务同样花钱。

    这条与「次数」刻意不同，是本模块最容易顺手写错的地方。
    """
    _job(1, status="completed", prompt=100, completion=50)
    _job(1, status="failed", error_code="LLM_UPSTREAM_ERROR", prompt=30, completion=10)
    _job(1, status="failed", error_code="CANCELLED", prompt=20, completion=5)

    assert quota_service.usage_today(1).tokens == 215


def test_usage_is_per_user(isolated_db):  # noqa: ARG001
    """别人的用量不算到我头上。"""
    _job(1, prompt=100)
    _job(2, prompt=999)
    assert quota_service.usage_today(1).tokens == 100
    assert quota_service.usage_today(2).tokens == 999


def test_running_ignores_day_boundary(isolated_db):  # noqa: ARG001
    """在飞任务跨日仍占用并发名额。

    长文档生成确实会跨日。若并发检查也跟着日界过滤，昨天发起、现在还在跑的任务
    就会从并发上限里消失，于是「昨天那批 + 今天这批」能同时开。
    """
    old = (utc_now() - timedelta(days=2)).isoformat()
    _job(1, status="running", created_at=old)
    assert quota_service.usage_today(1).running == 1
    # 但它不该占用今天的「次数」
    assert quota_service.usage_today(1).tasks == 0


# ---------------------------------------------------------------------------
# 三态与拦截
# ---------------------------------------------------------------------------


def test_user_zero_overrides_global_limit(isolated_db):  # noqa: ARG001
    """用户级 `0` 必须能盖掉全局上限——那是「给可信账号开口子」的唯一手段。"""
    _set_global(quota_daily_task_limit=1)
    _job(1, status="completed")
    assert quota_repo.set_quota(1, daily_task_limit=0, daily_token_limit=-1, max_running=-1)

    # 已用 1 次、全局上限 1，但该用户显式不限 → 放行
    quota_service.check(_user(), "prd")
    assert quota_service.resolve_limits(1).daily_tasks == 0
    assert quota_service.source_of(0) == "off"


def test_minus_one_follows_global(isolated_db):  # noqa: ARG001
    """`-1` 跟随全局：全局改了，用户的值跟着动。"""
    _set_global(quota_daily_task_limit=7)
    quota_repo.set_quota(1, daily_task_limit=-1, daily_token_limit=-1, max_running=-1)
    assert quota_service.resolve_limits(1).daily_tasks == 7
    assert quota_service.source_of(-1) == "global"


def test_exceeding_task_limit_raises_429(isolated_db):  # noqa: ARG001
    """超限抛 429，且文案能直接给用户看。"""
    _set_global(quota_daily_task_limit=2)
    _job(1, status="completed")
    _job(1, status="completed")

    with pytest.raises(Exception) as excinfo:
        quota_service.check(_user(), "prd")
    error = excinfo.value
    assert getattr(error, "status_code", 0) == 429
    assert "今日生成次数已用完" in str(error)


def test_one_below_limit_passes(isolated_db):  # noqa: ARG001
    """差一个没满时必须放行——边界差一（off-by-one）是这类代码的经典错法。"""
    _set_global(quota_daily_task_limit=2)
    _job(1, status="completed")
    quota_service.check(_user(), "prd")


def test_running_limit_reports_concurrency_not_quota(isolated_db):  # noqa: ARG001
    """并发满了说「等一个跑完」，而不是「明天再来」。

    两者的下一步动作完全不同：前者有明确可做的事，后者只能等到明天。
    """
    _set_global(quota_max_running=1, quota_daily_task_limit=10)
    _job(1, status="running")

    with pytest.raises(Exception) as excinfo:
        quota_service.check(_user(), "prd")
    assert "同时运行的任务已达上限" in str(excinfo.value)


def test_token_limit_raised_when_tasks_not_exhausted(isolated_db):  # noqa: ARG001
    """token 超限时说的是 token，而不是次数。"""
    _set_global(quota_daily_token_limit=100)
    _job(1, status="completed", prompt=60, completion=40)

    with pytest.raises(Exception) as excinfo:
        quota_service.check(_user(), "prd")
    assert "Token 用量已达上限" in str(excinfo.value)


def test_enforce_off_logs_but_allows(isolated_db, caplog):  # noqa: ARG001
    """`quota_enforce=False` 时只留日志、不拒绝。

    这是「先观察再收紧」的开关：部署方想先看真实用量分布，不想立刻拒绝用户。
    """
    _set_global(quota_daily_task_limit=1, quota_enforce=False)
    _job(1, status="completed")

    with caplog.at_level("WARNING"):
        quota_service.check(_user(), "prd")  # 不抛

    assert any("quota.exceeded" == getattr(r, "event", "") for r in caplog.records)


# ---------------------------------------------------------------------------
# 日界
# ---------------------------------------------------------------------------


def test_day_boundary_follows_configured_timezone(isolated_db):  # noqa: ARG001
    """「今天」按配置时区算，不按 UTC。

    东八区的今天从 UTC 前一天 16:00 开始。若按 UTC 分组，北京时间凌晨 0–8 点
    生成的任务会被算进「昨天」，额度提前恢复。
    """
    _set_global(quota_timezone_offset_minutes=480)
    start = quota_service.day_start_utc()
    # 日界应当是 UTC 的整点 16:00（东八区 00:00）
    assert start.endswith("T16:00:00"), f"日界算错了：{start}"

    # 日界之前一分钟的任务不算今天
    before = (utc_now() - timedelta(days=1)).isoformat()
    _job(1, status="completed", created_at=before)
    assert quota_service.usage_today(1).tasks == 0


def test_day_boundary_respects_offset_change(isolated_db):  # noqa: ARG001
    """改时区偏移立刻改变日界（它是热生效配置，不是启动参数）。"""
    _set_global(quota_timezone_offset_minutes=0)
    utc_start = quota_service.day_start_utc()
    assert utc_start.endswith("T00:00:00")

    _set_global(quota_timezone_offset_minutes=480)
    assert quota_service.day_start_utc() != utc_start


# ---------------------------------------------------------------------------
# 接口层：拦截发生在建任务之前
# ---------------------------------------------------------------------------


def test_api_rejects_without_creating_a_job_row(client: TestClient, isolated_db):  # noqa: ARG001
    """超限时返回 429，且**不留任务行**。

    这条比「返回 429」更重要：若检查放在建任务之后，被拒的请求会在
    `generation_jobs` 里留下一行 `failed`，污染失败率统计、也会出现在用户的
    任务列表里（「我什么都没生成，却多了一条失败记录」）。
    """
    from conftest import login, register

    register(client, "limited")
    login(client, "limited")
    me = client.get("/api/auth/me").json()
    user_id = int(me["id"])

    # 额度设成 0 次（显式不限是 0，所以用「上限 1 且有 1 条已收尾」来卡）
    quota_repo.set_quota(user_id, daily_task_limit=1, daily_token_limit=-1, max_running=-1)
    _job(user_id, status="completed")

    before = client.get("/api/jobs").json()["jobs"]
    response = client.post(
        "/api/jobs",
        json={"kind": "prd", "requirements": "做一个演示产品", "product_name": "演示"},
    )
    assert response.status_code == 429
    # 文案要能直接给用户看（前端 describeJobError 会原样显示字符串 detail）
    assert "额度" in response.json()["detail"]

    after = client.get("/api/jobs").json()["jobs"]
    assert len(after) == len(before), "被拒的请求不该留下任务行"
