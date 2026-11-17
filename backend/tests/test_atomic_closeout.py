"""原子收尾 + 统一版本锁（#81）：成功收尾五件事同事务，正文写入只有一条带版本号的入口。

接缝
----
本票断言跨**三条**接缝：

1. **主接缝**（造单 → 登记 → 派发 → 订阅，`ticket_support`）：真实收尾路径上注入写正文
   失败 / 版本冲突 / 账本写坏，断言外部可观察的终态、库行、账本、版本链与事件回执。
2. **文档服务 `document_store.write_document`**（本票新设的**唯一**正文写入口）：
   版本号对不上必须返回机器判据 `version_conflict`，且一个字都不写。
3. **HTTP 保存接缝**（`PUT /api/solutions/{id}/documents/{kind}`）：手改带过期版本号时
   必须 409 + `CONTENT_CHANGED`，不覆盖更新的正文；前端据此提示「请刷新后重试」。

为什么不直调 `_finalize` / `_write_document`：见 `test_no_internal_positioning.py`（断言
只架在公开边界上）。注入失败也一律换**公开入口**的模块属性（`document_store.write_document`
/ `commit_review` / `JobAttemptRepository.append`），不替换作业层私有函数。
"""

from __future__ import annotations

import json

import pytest
from conftest import job_get, register, users
from dbsupport import connect

from backend.core.contracts import StreamEvent
from backend.core.engine import session_scope
from backend.repositories.job_attempt_repo import (
    OUTCOME_COMPLETED,
    OUTCOME_FAILED,
    JobAttemptRepository,
)
from backend.services import document_store, job_service, solution_service
from ticket_support import run_ticket_events, run_ticket_in_flight

# 版本冲突时给用户看的那句话：后端写进 error_message / 409 的 detail，前端原样展示。
CONFLICT_MESSAGE = "正文已被更新，请刷新后重试"


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------


def _alice_id() -> int:
    user = users.get_by_username("alice")
    assert user is not None
    return int(user["id"])


@pytest.fixture
def alice(client):
    """注册 alice 并返回其 id。"""
    register(client)
    return _alice_id()


def _create_solution(client) -> int:
    """建一份真实方案：落文档要求 `documents.solution_id` 指向本人方案。"""
    response = client.post(
        "/api/solutions",
        json={"title": "原子收尾", "mode": "structured", "stage": "review-prd", "data": "{}"},
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _attempts(owner_id: int, job_id: int) -> list[dict]:
    with session_scope() as session:
        rows = JobAttemptRepository(session).list_by_job(owner_id, job_id)
        return [row.model_dump() for row in rows]


def _version_rows(solution_id: int, kind: str) -> list[dict]:
    """直接读版本行（含正文与结论），用于「一个字都没写」这类精确断言。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT v.version_no, v.content, v.review, v.review_failed_at "
            "FROM document_versions v JOIN documents d ON d.id = v.document_id "
            "WHERE d.solution_id = %s AND d.kind = %s ORDER BY v.version_no DESC",
            (solution_id, kind),
        ).fetchall()
    return [dict(row) for row in rows]


def _summaries(events: list[StreamEvent]) -> list[StreamEvent]:
    return [event for event in events if event.type == "run_summary"]


# ---------------------------------------------------------------------------
# 一、AC1 / AC5：写正文失败 → 整批回滚，任务停在执行中，正文与版本都不变
# ---------------------------------------------------------------------------


def test_body_write_failure_rolls_back_and_keeps_the_job_running(alice, monkeypatch):
    """注入「写正文」失败：任务**不能**进入已定稿，库行仍是执行中，账本与正文都不落。

    旧实现把 `commit_generated` 的异常吞成一条 warning，任务照旧 completed——那就是
    「任务表说已定稿、文档表里却没有这一版」的假成功。本票把它改成硬失败：整个收尾
    事务回滚，任务停在执行中（由恢复机制接手），而不是报成功。

    三条判据全部来自公开接缝：库行状态、事件回执（没有结束事件）、账本行数。
    """

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("考核点：写正文时库出错")

    monkeypatch.setattr(document_store, "write_document", _boom)

    solution_id = _create_solution_via_service(alice)
    registered, events = run_ticket_events(
        alice,
        solution_id,
        "prd",
        {},
        StreamEvent(type="part", content="", data={"text": "# 正文"}),
        reply="# 正文",
    )

    row = job_get(alice, registered.job_id)
    assert row is not None
    assert row["status"] == "running", "写正文失败不许把任务报成已定稿"
    assert _attempts(alice, registered.job_id) == [], "账本必须跟着整批回滚"
    assert _summaries(events) == [], "没有收尾就没有结束事件（不能谎报完成）"


def test_body_write_failure_leaves_the_previous_version_untouched(alice, monkeypatch):
    """已有上一版正文时注入写正文失败：旧版与版本链一个字都不变。

    这是 AC1 的另一面：不只是「新版本没写」，还要证明**旧版本没被顺手改坏**。
    """
    solution_id = _create_solution_via_service(alice)
    document_store.write_document(alice, solution_id, "prd", "# 旧稿", mode="append")

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("考核点：写正文时库出错")

    monkeypatch.setattr(document_store, "write_document", _boom)

    registered, _events = run_ticket_events(
        alice, solution_id, "prd", {}, StreamEvent(type="part", content="", data={"text": "# 新稿"}),
        reply="# 新稿",
    )

    assert job_get(alice, registered.job_id)["status"] == "running"
    doc = document_store.get_document(alice, solution_id, "prd")
    assert doc["content"] == "# 旧稿", "旧版本不许被改写"
    assert doc["version_no"] == 1
    assert len(_version_rows(solution_id, "prd")) == 1, "不许追加新版本"


def test_write_document_rejects_a_stale_version_with_a_machine_verdict(alice):
    """`write_document(expected_version_no=...)` 对不上旧版号 → `version_conflict`，零写入。

    场景就是真实事故：用户在版本 1 上编辑，生成任务期间追加了版本 2；用户那边的自动
    保存带着「我以为当前是第 1 版」发出去。判据必须是**机器可读的结果码**，不是
    「0 行 / None」——把 0 行当成功正是要根除的口径；调用方要能据此提示
    「请刷新后重试」，而不是把失败当成功或当不存在。
    """
    solution_id = _create_solution_via_service(alice)
    document_store.write_document(
        alice, solution_id, "prd", "# 第一版", mode="append"
    )
    document_store.write_document(
        alice, solution_id, "prd", "# 第二版（生成）", mode="append"
    )

    result = document_store.write_document(
        alice,
        solution_id,
        "prd",
        "# 用户基于第一版改的稿子",
        mode="manual",
        expected_version_no=1,
    )

    assert result.outcome == "version_conflict"
    doc = document_store.get_document(alice, solution_id, "prd")
    assert doc is not None and doc["content"] == "# 第二版（生成）", "不许覆盖更新的正文"
    assert doc["version_no"] == 2
    assert len(_version_rows(solution_id, "prd")) == 2, "冲突不得追加第三条版本"


def _create_solution_via_service(owner_id: int) -> int:
    """经服务层建一份方案（本文件里不经过 HTTP 的用例用它）。"""
    created = solution_service.create_solution(owner_id, "原子收尾", "chat", "draft")
    return int(created["id"])


# ---------------------------------------------------------------------------
# 二、AC2：HTTP 手改带过期版本号 → 409 + CONTENT_CHANGED，不覆盖
# ---------------------------------------------------------------------------


def test_http_save_with_a_stale_version_returns_409_and_keeps_the_newer_body(alice, client):
    """手改与生成赛跑：PUT 带过期 `expected_version_no` → 409，正文与版本都不动。

    前端拿到 409 + `CONTENT_CHANGED` 后要提示「正文已被更新，请刷新后重试」，并**停止**
    把旧内容重试回去（见前端 `stores/documents.ts`）。后端这一步是唯一能真正拦住覆盖的地方。
    """
    solution_id = _create_solution(client)
    document_store.write_document(alice, solution_id, "prd", "# 第一版", mode="append")
    document_store.write_document(alice, solution_id, "prd", "# 第二版（生成）", mode="append")

    response = client.put(
        f"/api/solutions/{solution_id}/documents/prd",
        json={"content": "# 旧稿上的手改", "expected_version_no": 1},
    )

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "CONTENT_CHANGED"
    assert body["detail"] == CONFLICT_MESSAGE

    assert document_store.get_document(alice, solution_id, "prd")["content"] == "# 第二版（生成）"
    assert len(_version_rows(solution_id, "prd")) == 2


def test_http_save_with_the_current_version_still_succeeds(alice, client):
    """版本号对得上时正常保存：就地改当前版本、不追加版本，body 仍是用户的新内容。"""
    solution_id = _create_solution(client)
    document_store.write_document(alice, solution_id, "prd", "# 第一版", mode="append")

    response = client.put(
        f"/api/solutions/{solution_id}/documents/prd",
        json={"content": "# 手改后的正文", "expected_version_no": 1},
    )

    assert response.status_code == 200, response.text
    doc = document_store.get_document(alice, solution_id, "prd")
    assert doc["content"] == "# 手改后的正文"
    assert doc["version_no"] == 1, "手改是就地改当前版本，不追加版本"


# ---------------------------------------------------------------------------
# 三、AC3：五条写入路径全部走同一个入口
# ---------------------------------------------------------------------------


def test_the_five_write_paths_share_one_entry(alice, monkeypatch):
    """手改 / 整篇生成 / 就地改写 / 存为新版本 / 恢复：五条路径都打同一个入口。

    「实现只有一份」的证伪方式：把统一入口换成记录器（仍转发真实写入），五条对外
    动作各跑一次——只要任何一条走了自己的私有实现，它的调用就不会出现在记录里。
    记录里还断言 `session` 为空（非收尾调用方用不带 session 的便捷封装）。
    """
    real = document_store.write_document
    calls: list[dict] = []

    def spy(*args, **kwargs):  # noqa: ANN002, ANN003
        calls.append({"args": args, "kwargs": kwargs})
        return real(*args, **kwargs)

    monkeypatch.setattr(document_store, "write_document", spy)

    solution_id = _create_solution_via_service(alice)

    document_store.save_document(alice, solution_id, "prd", "# 手改")
    document_store.commit_generated(alice, solution_id, "prd", "# 整篇生成")
    document_store.commit_generated(
        alice, solution_id, "prd", "# 就地改写", append_version=False, source="optimize"
    )
    document_store.checkpoint(alice, solution_id, "prd", "# 存为新版本", "定稿")
    first_version_id = document_store.list_versions(alice, solution_id, "prd")[-1]["version_id"]
    document_store.restore(alice, solution_id, "prd", int(first_version_id))

    assert len(calls) == 5, f"五条写入路径应当都经过统一入口，实际 {len(calls)} 次"
    modes = [call["kwargs"]["mode"] for call in calls]
    assert modes == ["manual", "append", "replace", "append", "append"]
    # 前三笔是「自己开事务」的便捷形态；存为新版本 / 恢复在读源版本时已经开了事务，
    # 复用同一个 session（读与写在同一个事务里，才不会被中间写入插队）。
    assert [call["kwargs"].get("session") is None for call in calls] == [
        True,
        True,
        True,
        False,
        False,
    ]


def test_generation_and_in_place_jobs_write_through_the_entry(alice, monkeypatch):
    """收尾侧同样只有一条入口：整篇生成用 `append`、就地改写用 `replace`（且带版本号）。"""
    real = document_store.write_document
    calls: list[dict] = []

    def spy(*args, **kwargs):  # noqa: ANN002, ANN003
        calls.append({"args": args, "kwargs": kwargs})
        return real(*args, **kwargs)

    monkeypatch.setattr(document_store, "write_document", spy)

    solution_id = _create_solution_via_service(alice)
    # 整篇生成：从零写第一版
    run_ticket_events(
        alice, solution_id, "prd", {}, StreamEvent(type="done", data={"reply": "# 生成稿"})
    )
    assert [call["kwargs"]["mode"] for call in calls] == ["append"]
    assert calls[0]["kwargs"]["session"] is not None, "收尾由 _finalize 持事务，必须传 session"

    # 就地改写：版本号随任务单定格（前端手边那一版）
    calls.clear()
    run_ticket_events(
        alice,
        solution_id,
        "optimize",
        {"document": "# 生成稿", "doc_kind": "prd", "expected_version_no": 1},
        reply="# 优化稿",
    )
    assert [call["kwargs"]["mode"] for call in calls] == ["replace"]
    assert calls[0]["kwargs"]["expected_version_no"] == 1
    assert calls[0]["kwargs"]["session"] is not None


# ---------------------------------------------------------------------------
# 四、AC2：就地改写的版本号对不上 → 任务失败、正文保持新生成那一版
# ---------------------------------------------------------------------------


def test_in_place_rewrite_with_a_stale_version_fails_the_job(alice):
    """就地改写跑在版本 1 上，期间生成追加了版本 2 → 改写被拒、任务失败、正文不动。

    就地改写是**唯一会覆盖当前正文**的生成动作；版本锁要拦的正是它——否则优化稿会把
    用户刚生成的那一版悄悄盖掉，而任务还报成功。
    """
    solution_id = _create_solution_via_service(alice)
    document_store.write_document(alice, solution_id, "prd", "# 第一版", mode="append")

    def append_new_version() -> None:
        document_store.write_document(alice, solution_id, "prd", "# 第二版（生成）", mode="append")

    registered, events = run_ticket_in_flight(
        alice,
        solution_id,
        "optimize",
        {"document": "# 第一版", "doc_kind": "prd", "expected_version_no": 1},
        append_new_version,
        StreamEvent(type="part", content="", data={"text": "# 优化稿"}),
        reply="# 优化稿",
    )

    row = job_get(alice, registered.job_id)
    assert row["status"] == "failed"
    assert row["error_code"] == "CONTENT_CHANGED"
    assert CONFLICT_MESSAGE in row["error_message"]
    assert any(
        event.type == "error" and CONFLICT_MESSAGE in event.content for event in events
    ), "订阅者还连着：冲突原因必须当场可见"

    doc = document_store.get_document(alice, solution_id, "prd")
    assert doc["content"] == "# 第二版（生成）", "被拒的改写不许覆盖更新的正文"
    assert doc["version_no"] == 2
    assert len(_version_rows(solution_id, "prd")) == 2

    attempts = _attempts(alice, registered.job_id)
    assert len(attempts) == 1 and attempts[0]["outcome"] == OUTCOME_FAILED
    assert attempts[0]["error_code"] == "CONTENT_CHANGED"


# ---------------------------------------------------------------------------
# 五、AC4：账本、正文、审核结论属于同一版本 / 同一事务
# ---------------------------------------------------------------------------


def test_closeout_ledger_body_and_conclusion_belong_to_one_version(alice):
    """一次成功收尾之后：账本一行、正文一版、结论挂在**那一版**上，三处同源。

    判据刻意用两边独立可读的数字对拍：账本的 token 与耗时取自收尾载荷，正文版本的
    `last_run_summary` 是同一份载荷落库，任务行的 `usage` 又是落库的那一列——
    三处只要有一处走了「另算一遍」，数字就会对不上。
    """
    solution_id = _create_solution_via_service(alice)
    review = {
        "kind": "prd",
        "verdict": "pass",
        "summary": "无缺陷",
        "issues": [],
        "rewritten": False,
        "reviewed_at": "2026-01-01T00:00:00",
        "attempted": True,
    }
    pending = [{"id": "p1", "title": "支付方式", "detail": "待确认"}]

    registered, _events = run_ticket_events(
        alice,
        solution_id,
        "prd",
        {},
        StreamEvent(type="part", content="", data={"text": "# 正文"}),
        StreamEvent(type="review", content="", data=review),
        reply="# 正文",
        done_data={"pending_items": pending},
    )

    row = job_get(alice, registered.job_id)
    assert row["status"] == "succeeded"
    usage = json.loads(row["usage"])

    versions = _version_rows(solution_id, "prd")
    assert len(versions) == 1
    version = versions[0]
    assert version["content"] == "# 正文"
    assert json.loads(version["review"]) == review, "结论挂在正文那一版上"
    assert version["review_failed_at"] is None

    doc = document_store.get_document(alice, solution_id, "prd")
    assert doc["pending_items"] == pending
    assert doc["last_run_summary"]["prompt_tokens"] == usage["prompt_tokens"]

    attempts = _attempts(alice, registered.job_id)
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == OUTCOME_COMPLETED
    assert attempts[0]["prompt_tokens"] == int(usage["prompt_tokens"])
    assert attempts[0]["duration_ms"] == int(usage["duration_ms"])


def test_ledger_failure_keeps_the_closeout_but_loses_only_the_ledger_row(alice, monkeypatch):
    """账本写坏：状态 / 正文 / 结论照常提交，只丢这一行账并留 warning（#80 口径）。

    这是本票对 #80「保存点隔离」取舍的正面证据：正文与状态是权威、账本是审计。
    硬事务把**写正文失败**整批回滚（见上面两条），但审计写失败不许把一个真实成功的
    生成判死——所以账本那一笔仍套在 SAVEPOINT 里。
    """
    import logging

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("账本写不进去")

    monkeypatch.setattr("backend.services.job_state.JobAttemptRepository.append", _boom)

    solution_id = _create_solution_via_service(alice)
    registered, events = run_ticket_events(
        alice, solution_id, "prd", {}, reply="# 正文"
    )

    row = job_get(alice, registered.job_id)
    assert row["status"] == "succeeded", "审计写失败不许把结论判死"
    assert _attempts(alice, registered.job_id) == []
    assert document_store.get_document(alice, solution_id, "prd")["content"] == "# 正文"
    assert _summaries(events)[-1].data["status"] == "succeeded"
    # 账本没写成必须可见；用 caplog 的 handler 拿不到被 monkeypatch 的 logger 记录，
    # 这里只断「不影响结论」的正面事实（可见性由 #80 的用例负责）。


def test_review_conclusion_write_failure_rolls_back_state_ledger_and_conclusion(
    alice, monkeypatch, caplog
):
    """重审结论「写进去之后抛错」：状态 / 账本 / 结论一起回滚，任务改判失败。

    这是全票据最硬的一条证据：结论、任务终态、账本同在一个事务里。若结论走自己的短事务
    （旧结构），结论会留在版本上、账本还会多一行 `completed` 指向一个 failed 的任务。
    """
    import logging

    solution_id = _create_solution_via_service(alice)
    body = "# 接口文档正文"
    document_store.write_document(alice, solution_id, "api_docs", body, mode="append")

    real = document_store.commit_review

    def write_then_boom(*args, **kwargs):  # noqa: ANN002, ANN003
        real(*args, **kwargs)
        raise RuntimeError("结论写进去之后抛错")

    monkeypatch.setattr(document_store, "commit_review", write_then_boom)

    review = {"kind": "api_docs", "verdict": "pass", "summary": "无缺陷", "issues": [], "rewritten": False, "reviewed_at": "2026-01-01T00:00:00"}

    with caplog.at_level(logging.WARNING):
        registered, events = run_ticket_events(
            alice,
            solution_id,
            "review",
            {"doc_kind": "api_docs"},
            StreamEvent(type="review", content="", data=review),
            resolved_inputs={"document": body},
        )

    row = job_get(alice, registered.job_id)
    assert row["status"] == "failed"
    assert row["error_code"] == "INTERNAL_ERROR"

    # 结论一个字都没留下（成功那笔事务整批回滚了）
    versions = _version_rows(solution_id, "api_docs")
    assert len(versions) == 1
    assert versions[0]["review"] is None
    assert versions[0]["review_failed_at"]

    # 账本只有失败那一行：成功后写的那行 `completed` 跟着回滚了
    attempts = _attempts(alice, registered.job_id)
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == OUTCOME_FAILED
    assert attempts[0]["error_code"] == "INTERNAL_ERROR"
    assert [event.type for event in events if event.type == "error"], "失败原因必须当场可见"


# ---------------------------------------------------------------------------
# 六、AC5：回滚之后由恢复机制接手（#82 的租约 + #83 的恢复器）
# ---------------------------------------------------------------------------


def test_a_rolled_back_closeout_is_reaped_as_a_failure_later(alice, monkeypatch):
    """回滚留下的 `running` 行不是死尸：启动回收（恢复器）会把它判死并记账。

    #82 的租约恢复会接走在线上的在途任务；本票能证明的是回滚语义没有把任务卡成
    「永久 running 却无人知晓」——现有恢复机制仍能观察到它。**#83 之后**这条行的
    死因是确定的：它还持有执行者的租约，所以恢复器按「执行者进程没了、租约到期」
    判失败（`LEASE_EXPIRED`），而不是笼统的内部错误——台账里也就有了真正的超时原因。
    """

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("考核点：写正文时库出错")

    monkeypatch.setattr(document_store, "write_document", _boom)
    solution_id = _create_solution_via_service(alice)
    registered, _events = run_ticket_events(
        alice, solution_id, "prd", {}, reply="# 正文"
    )

    row = job_get(alice, registered.job_id)
    assert row["status"] == "running", "回滚语义：任务仍停在执行中"
    assert row["lease_token"] != "", "回滚的任务仍持有执行者的租约（#82）"

    job_service.reap_orphans()

    row = job_get(alice, registered.job_id)
    assert row["status"] == "failed"
    assert row["error_code"] == "LEASE_EXPIRED", "启动回收给的是确定的超时原因，不是笼统内部错误"
    assert (row["lease_owner"], row["lease_token"], row["lease_expire_at"]) == ("", "", "")
    attempts = _attempts(alice, registered.job_id)
    assert len(attempts) == 1 and attempts[0]["outcome"] == OUTCOME_FAILED
    assert attempts[0]["error_code"] == "LEASE_EXPIRED"
