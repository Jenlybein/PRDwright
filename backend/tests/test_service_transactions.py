"""服务层事务收口的原子性用例（v6 / T16.11）。

三条用例分别钉住一处「以前是两次提交、现在必须同生共死」的地方：

1. `document_store.commit_generated`：版本行写了一半就失败 → **一行都不落**；
2. `auth_service` 的登录失败计数：上层抛 `AuthError` 之前账必须已经落定
   （否则攻击者永远攒不到阈值）；
3. `job_service.delete_solution_and_cancel_jobs`：删方案与取消任务在一个事务里
   ——删不掉时任务**不能**被改成 failed。

外加一条总兜底：服务方法返回后**重开一个 session 要能读到**它写的东西。
这条防的是「仓库不再自己 commit」之后最阴的一类回归：忘了包 `session_scope`，
数据静默丢掉，接口照样返回 200。
"""

from __future__ import annotations

import pytest
from conftest import job_create, job_get, register, users

from backend.core.engine import session_scope
from backend.repositories.auth_repo import LoginFailureRepository
from backend.repositories.document_repo import DocumentRepository
from backend.repositories.solution_repo import SolutionRepository
from backend.services import auth_service, document_store, job_service, solution_service


def _owner(client) -> int:
    """注册 alice 并返回她的 id。"""
    register(client)
    return int(users.get_by_username("alice")["id"])  # type: ignore[index]


# ---------------------------------------------------------------------------
# ① commit_generated：写了一半失败 → 零残留
# ---------------------------------------------------------------------------


def test_commit_generated_leaves_nothing_when_it_fails_midway(client, monkeypatch):
    """版本行已经插进去、随后抛异常 —— 整个 `session_scope()` 必须回滚。

    为什么这条能证明「一个事务」：若 `append_version` 与「回填指针」各自提交，
    版本行会留下来，于是文档看起来「有历史但没有当前版本」——
    正是那种不报错、只在某天打开历史列表时才发现的半成品。
    """
    owner = _owner(client)
    solution = solution_service.create_solution(owner, "原子性", "chat", "draft")
    solution_id = int(solution["id"])

    real_append = DocumentRepository.append_version

    def append_then_fail(self, **kwargs):
        # 先真的写进去（flush 过），再让事务失败
        real_append(self, **kwargs)
        raise RuntimeError("考核点：写了一半之后失败")

    monkeypatch.setattr(DocumentRepository, "append_version", append_then_fail)

    with pytest.raises(RuntimeError):
        document_store.commit_generated(owner, solution_id, "prd", "# 半截正文")

    # 重开 session 直接数行：版本、文档都不该留下任何东西
    with session_scope() as session:
        assert DocumentRepository(session).list_versions(owner, solution_id, "prd") == []
    assert document_store.get_document(owner, solution_id, "prd") is None


# ---------------------------------------------------------------------------
# ② 登录失败计数：报错之前账必须落定
# ---------------------------------------------------------------------------


def test_login_failures_are_committed_before_the_error_is_raised(client):
    """连续输错密码 → 每次失败都要真的落库，不能被随后的 `AuthError` 回滚。

    反例（曾真实存在过的写法）：把「计数」和「抛错」塞进同一个事务，
    抛异常把计数一起回滚 → 失败次数永远是 0，渐进延时永远不触发，
    而日志里看起来「限流生效了」。
    """
    owner = _owner(client)
    assert owner > 0

    for _ in range(3):
        with pytest.raises(auth_service.AuthError):
            auth_service.login("alice", "definitely-wrong")

    with session_scope() as session:
        failures = LoginFailureRepository(session).count_since("alice", "1970-01-01")
    assert failures == 3, "失败次数必须独立提交"


def test_successful_login_clears_the_failure_counter(client):
    """登录成功后清账也是独立事务：下一次登录不该继承上一轮的延时。"""
    _owner(client)
    for _ in range(2):
        with pytest.raises(auth_service.AuthError):
            auth_service.login("alice", "definitely-wrong")

    auth_service.login("alice", "password123")

    with session_scope() as session:
        assert LoginFailureRepository(session).count_since("alice", "1970-01-01") == 0


# ---------------------------------------------------------------------------
# ③ 删方案 + 取消任务：同生共死
# ---------------------------------------------------------------------------


def test_delete_solution_cancels_its_jobs(client):
    """正常路径：方案没了，挂在上面的 running 任务同时被收成终态。"""
    owner = _owner(client)
    solution = solution_service.create_solution(owner, "待删", "chat", "draft")
    solution_id = int(solution["id"])
    job_id = job_create(owner, "prd", solution_id)

    assert job_service.delete_solution_and_cancel_jobs(owner, solution_id) is True

    assert solution_service.get_solution(owner, solution_id) is None
    row = job_get(owner, job_id)
    assert row is not None and row["status"] == "failed"
    assert row["error_code"] == "CANCELLED"


def test_delete_solution_does_not_cancel_jobs_when_it_fails(client, monkeypatch):
    """删除失败（方案不属于本人 / 已被删掉）时，**任务必须原样还在 running**。

    这条是「一个事务」的判据：若取消是另一个事务先提交，任务会变成 failed，
    而方案还在——用户会看到「我什么都没删，任务却被停了」。
    """
    owner = _owner(client)
    solution = solution_service.create_solution(owner, "留着", "chat", "draft")
    solution_id = int(solution["id"])
    job_id = job_create(owner, "prd", solution_id)

    # 让「删方案」这一步命中 0 行
    monkeypatch.setattr(SolutionRepository, "delete", lambda self, *a, **k: False)

    assert job_service.delete_solution_and_cancel_jobs(owner, solution_id) is False

    row = job_get(owner, job_id)
    assert row is not None and row["status"] == "running", "删除失败不该顺手停任务"
    assert solution_service.get_solution(owner, solution_id) is not None


# ---------------------------------------------------------------------------
# ④ 总兜底：服务方法返回后重开 session 能读到
# ---------------------------------------------------------------------------


def test_service_writes_are_visible_from_a_new_session(client):
    """「仓库不 commit」之后最容易踩的坑：服务层忘了包 scope → 静默丢数据。

    这条不用 mock，直接用一个**全新的 session** 去读服务刚写的东西。
    接口返回 200 与「数据真的在库里」是两件事，这条只认后者。
    """
    owner = _owner(client)

    created = solution_service.create_solution(owner, "看得见吗", "chat", "draft")
    solution_id = int(created["id"])

    document_store.commit_generated(owner, solution_id, "prd", "# 正文")

    with session_scope() as session:
        assert SolutionRepository(session).get(owner, solution_id) is not None
    assert document_store.get_document(owner, solution_id, "prd") is not None
