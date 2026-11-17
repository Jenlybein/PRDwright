"""部分完成版本（票 #85）：失败时把「已生成的内容」落成一版带标记的正文。

接缝
----
本票只断言**公开可观察的后果**，跨三条接缝：

1. **主接缝**（造单 → 登记 → 派发 → 订阅，`ticket_support`）：真实失败收尾路径上，
   注入「中途失败 / 重试耗尽 / 用户取消 / 成功但部分完成」四种结局，断言库行、版本链、
   任务行标记与事件回执。
2. **HTTP 读接缝**：`GET /api/solutions/{id}/documents` 与 `GET /api/jobs/{id}` ——
   前端「能看到、能下载、能修」全靠这两条，因此标记必须从这两条读得到。
3. **文档服务接缝**：`document_store.write_document` / `list_versions` 是正文与版本的
   唯一写入口 / 读入口（#81 收口），标记的写入与读回都走它。

为什么不直调 `_finalize` / `_write_partial_document`：见 `test_no_internal_positioning.py`
（断言只架在公开边界上）。失败也是造单时注入假事件流（`error` 事件），不替换作业层私有函数。

口径
----
- 「部分完成」标记**挂在版本上**（`document_versions.partial`），任务行上留一份
  （`generation_jobs.partial`）——同一件事的两份投影，用同一次收尾事务写。
- 那一版的审核区必须是**空的**：`review` 与 `review_failed_at` 都是 NULL。旧结论属于
  别的正文，摆在新版本上会被读成本次结果（spec「不显示旧结论」）。
- 标记**不阻塞**下载 / 修复 / 重生成：它只是「这份正文不完整」的说明，不是禁用理由。
"""

from __future__ import annotations

import pytest
from conftest import job_get, register, users
from dbsupport import connect

from backend.core.contracts import StreamEvent
from backend.services import document_store, job_inputs, job_service, solution_service
from ticket_support import run_ticket_events, run_ticket_in_flight

# 一条「上游连续失败、重试耗尽」的错误事件：失败收尾的唯一入口是 `_finalize` 的失败分支。
_EXHAUSTED = StreamEvent(
    type="error",
    content="生成失败：上游连续失败，重试已耗尽",
    data={"error_type": "JobFailed"},
    code="LLM_UPSTREAM_ERROR",
)
# 「写了一半的正文」：chunk 进实时缓冲，`_draft()` 据此还原出可用正文。
_HALF = StreamEvent(type="chunk", content="# 半截正文\n\n第一段已经写完。")


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


def _create_solution(owner_id: int) -> int:
    """建一份真实方案：落文档要求 `documents.solution_id` 指向本人方案。"""
    created = solution_service.create_solution(owner_id, "部分完成", "chat", "draft")
    return int(created["id"])


def _version_rows(solution_id: int, kind: str = "prd") -> list[dict]:
    """直接读版本行（含标记与审核两列），用于「这一版到底长什么样」的精确断言。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT v.version_no, v.content, v.review, v.review_failed_at, v.partial "
            "FROM document_versions v JOIN documents d ON d.id = v.document_id "
            "WHERE d.solution_id = %s AND d.kind = %s ORDER BY v.version_no DESC",
            (solution_id, kind),
        ).fetchall()
    return [dict(row) for row in rows]


def _current_version(solution_id: int, kind: str = "prd") -> dict:
    """当前版本行（含标记与审核两列）。"""
    rows = _version_rows(solution_id, kind)
    assert rows, "当前版本应当存在"
    return rows[0]


def _fail_mid_stream(owner_id: int, solution_id: int, *, events):  # noqa: ANN001
    """跑一支「已产出内容、随后失败」的生成任务，返回 (单②, 全部事件)。"""
    return run_ticket_events(owner_id, solution_id, "prd", {}, *events, reply="")


# ---------------------------------------------------------------------------
# 一、AC1：失败时把已生成内容落成一版**带标记**的正文
# ---------------------------------------------------------------------------


def test_exhausted_retries_land_a_partial_version(alice):
    """重试耗尽（中途失败）→ 已生成的内容变成一版新正文，并标「部分完成」。

    旧口径是「失败只保留 `draft_content`，不写文档版本」，于是用户手里什么都没有；
    本票把这半截正文升格成**正式版本**——它可读、可下载、可拿去修。
    """
    solution_id = _create_solution(alice)
    registered, _events = _fail_mid_stream(alice, solution_id, events=(_HALF, _EXHAUSTED))

    row = job_get(alice, registered.job_id)
    assert row["status"] == "failed"

    version = _current_version(solution_id)
    assert version["content"].strip() != "", "已生成的内容不许丢"
    assert "第一段已经写完" in version["content"]
    assert version["partial"] is True, "这一版必须标成部分完成"
    assert version["version_no"] == 1


def test_partial_version_is_appended_not_a_rewrite_of_the_previous_body(alice):
    """已有完整版时失败：部分完成是**追加**的一版，旧版一个字都不动。

    部分完成不是「就地改写当前版本」——那会把用户手里那份完整正文换成半截稿，
    比失败本身更糟。它只进版本链，旧版继续可恢复。
    """
    solution_id = _create_solution(alice)
    document_store.write_document(alice, solution_id, "prd", "# 完整旧稿", mode="append")

    _fail_mid_stream(alice, solution_id, events=(_HALF, _EXHAUSTED))

    rows = _version_rows(solution_id)
    assert [row["version_no"] for row in rows] == [2, 1], "新的一版在前，旧版仍在"
    assert rows[1]["content"] == "# 完整旧稿", "旧版正文不许被改写"
    assert rows[1]["partial"] is False
    assert rows[0]["partial"] is True
    doc = document_store.get_document(alice, solution_id, "prd")
    assert doc is not None and doc["version_no"] == 2, "当前版本指针前移到部分完成那一版"


def test_completed_but_partial_task_marks_the_version_it_wrote(alice):
    """批次耗尽但管线仍收尾（`done.partial=true`）：这一版正文可用但不完整。

    这是「部分完成」的另一条来源：章节重试用尽后落的是失败占位，管线照常收尾。
    标记同样必须落到版本上——否则界面会把「有占位符的正文」当成完整交付。
    """
    solution_id = _create_solution(alice)
    _registered, _events = run_ticket_events(
        alice,
        solution_id,
        "prd",
        {},
        reply="# 正文\n\n## 1. 概述\n正文\n\n## 2. 功能\n（本章生成失败）\n",
        done_data={
            "partial": True,
            "failed_sections": [{"section_id": "features", "title": "功能"}],
        },
    )

    version = _current_version(solution_id)
    assert version["partial"] is True
    assert "（本章生成失败）" in version["content"]


def test_failure_without_any_content_leaves_no_version(alice):
    """一个字都没写出来就失败：**不**产生空版本，任务也不标部分完成。

    空正文升格成版本只会给用户一个「点开是空的」的假产物；没有内容就没有「可用」可言。
    """
    solution_id = _create_solution(alice)
    registered, _events = _fail_mid_stream(alice, solution_id, events=(_EXHAUSTED,))

    assert _version_rows(solution_id) == [], "没有内容就不许落版本"
    row = job_get(alice, registered.job_id)
    assert row["status"] == "failed"
    assert row["partial"] is False, "没有可用内容就不标部分完成"


# ---------------------------------------------------------------------------
# 二、AC2：那一版的审核结论与审核失败时间**都是空**
# ---------------------------------------------------------------------------


def test_partial_version_carries_no_review_and_no_review_failed_at(alice):
    """部分完成那一版的审核两列都是 NULL——旧结论属于**别的正文**，不能搬过来。

    旧实现（以及最容易被忽略的写法）会把当前版本那条结论顺手带过去；它看起来完全正常，
    却是对另一份正文的判断。这里用「上一版有通过结论 + 审核失败时间」来证明它不会跟过来。
    """
    solution_id = _create_solution(alice)
    document_store.write_document(
        alice,
        solution_id,
        "prd",
        "# 完整旧稿",
        mode="append",
        review={
            "kind": "prd",
            "verdict": "pass",
            "summary": "旧版的结论",
            "issues": [],
            "rewritten": False,
            "reviewed_at": "2026-01-01T00:00:00",
            "attempted": True,
            "failed_sections": [],
        },
        review_failed_at="2026-01-02T00:00:00",
    )

    _fail_mid_stream(alice, solution_id, events=(_HALF, _EXHAUSTED))

    version = _current_version(solution_id)
    assert version["review"] is None, "新版本不许带旧结论"
    assert version["review_failed_at"] is None, "新版本也不许带旧的审核失败时间"

    rows = _version_rows(solution_id)
    assert rows[1]["review"] is not None, "旧版自己的结论原样保留（它描述的是旧正文）"


# ---------------------------------------------------------------------------
# 三、AC3：任务状态是失败，但「有内容可用」在任务行上也留了一份
# ---------------------------------------------------------------------------


def test_job_row_marks_partial_while_status_stays_failed(alice):
    """任务行：`status=failed` + `partial=true` —— 两件事同时成立。

    「失败」回答「这次跑成了没有」，「partial」回答「有没有东西能用」。合成一个状态
    会让界面二选一：要么把半截内容藏起来，要么把失败说成成功。
    """
    solution_id = _create_solution(alice)
    registered, _events = _fail_mid_stream(alice, solution_id, events=(_HALF, _EXHAUSTED))

    row = job_get(alice, registered.job_id)
    assert row["status"] == "failed"
    assert row["partial"] is True


def test_jobs_api_exposes_the_partial_marker(alice, client):
    """`GET /api/jobs/{id}` 读得到 `partial`：界面靠它区分「失败」与「失败但有内容」。"""
    solution_id = _create_solution(alice)
    registered, _events = _fail_mid_stream(alice, solution_id, events=(_HALF, _EXHAUSTED))

    response = client.get(f"/api/jobs/{registered.job_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "failed"
    assert body["partial"] is True


def test_version_list_exposes_the_partial_marker(alice):
    """版本列表读得到每条版本的 `partial`：界面据此把部分完成与完整版区分开。"""
    solution_id = _create_solution(alice)
    document_store.write_document(alice, solution_id, "prd", "# 完整旧稿", mode="append")
    _fail_mid_stream(alice, solution_id, events=(_HALF, _EXHAUSTED))

    versions = document_store.list_versions(alice, solution_id, "prd")
    assert [item["version_no"] for item in versions] == [2, 1]
    assert versions[0]["partial"] is True
    assert versions[1]["partial"] is False
    assert versions[0]["is_current"] is True


# ---------------------------------------------------------------------------
# 四、AC4：下载与修复对部分完成版本可用（不因标记禁用）
# ---------------------------------------------------------------------------


def test_documents_api_serves_the_partial_version_for_download(alice, client):
    """审阅页 / 下载读的是同一份文档：正文在那、标记在那、审核区是空的（未审核）。

    下载走的是 `documents` 的正文与 `version_no`，因此这条读接缝就是「能下载」的后端面。
    """
    solution_id = _create_solution(alice)
    _fail_mid_stream(alice, solution_id, events=(_HALF, _EXHAUSTED))

    response = client.get(f"/api/solutions/{solution_id}/documents")
    assert response.status_code == 200, response.text
    items = [item for item in response.json()["documents"] if item["kind"] == "prd"]
    assert len(items) == 1
    item = items[0]
    assert "第一段已经写完" in item["content"], "下载拿到的必须是那半截正文"
    assert item["version_no"] == 1
    assert item["partial"] is True
    assert item["review"] is None, "审核面板据此显示「未审核」"
    assert item["review_failed_at"] is None, "且不显示上一次的审核失败时间"


def test_fix_path_accepts_the_partial_version(alice):
    """修复（`review_fix`）对部分完成版本可用：只要它被审出问题，就能按结论重写。

    标记不是禁用理由——后端**不**拿 `partial` 拦修复；拦住它的是「有没有结论」这条既有
    口径（没有结论 ≠ 通过，也就无从修复）。所以先审一次（重审只写结论、不动正文），
    再解析修复输入即可通过。
    """
    solution_id = _create_solution(alice)
    _fail_mid_stream(alice, solution_id, events=(_HALF, _EXHAUSTED))

    document_store.commit_review(
        alice,
        solution_id,
        "prd",
        {
            "kind": "prd",
            "verdict": "revise",
            "summary": "半截正文里有问题",
            "issues": [
                {"severity": "high", "detail": "缺章节", "category": "structure"}
            ],
            "rewritten": False,
            "reviewed_at": "2026-01-03T00:00:00",
            "attempted": True,
            "failed_sections": [],
        },
    )

    inputs = job_inputs.resolve_runtime_inputs("review_fix", alice, solution_id, "prd")
    assert "第一段已经写完" in inputs["document"], "修复拿到的就是部分完成那一版正文"
    # 同一份也被重生成 / 重新审核可达：两者都不看 `partial`，这里以「读得到正文」为准。
    doc = document_store.get_document(alice, solution_id, "prd")
    assert doc is not None and doc["partial"] is True


# ---------------------------------------------------------------------------
# 五、AC5：标记挂在版本上，任务上留一份（同一次收尾事务写）
# ---------------------------------------------------------------------------


def test_marker_is_on_the_version_and_mirrored_on_the_job(alice):
    """同一件事的两份投影：版本行与任务行都写着 `partial`。

    「挂在版本上」回答「哪一份正文不完整」，「任务上也留一份」回答「这次生成算不算有货」。
    两处必须同源——只写一处，刷新之后就会有一边说得对、另一边说得错。
    """
    solution_id = _create_solution(alice)
    registered, _events = _fail_mid_stream(alice, solution_id, events=(_HALF, _EXHAUSTED))

    version = _current_version(solution_id)
    job = job_get(alice, registered.job_id)
    assert version["partial"] is True
    assert job["partial"] is True
    assert job["status"] == "failed"


def test_partial_version_and_job_marker_come_from_one_closeout(alice, monkeypatch):
    """标记与任务终态同一次收尾：写标记时抛错 → 整批回滚，任务仍在执行中。

    收尾是**一个事务**（#81）。若两次分别提交，一次「任务已失败、正文没落」就会留下
    「界面说有内容可用、库里却没有这一版」的谎报——正是本票要消灭的那类不一致。
    """

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("考核点：落部分完成版本时库出错")

    monkeypatch.setattr(document_store, "write_document", _boom)

    solution_id = _create_solution(alice)
    registered, events = _fail_mid_stream(alice, solution_id, events=(_HALF, _EXHAUSTED))

    row = job_get(alice, registered.job_id)
    assert row["status"] == "running", "落正文失败时不许先把任务判死"
    assert row["partial"] is False, "标记同样跟着回滚，不许单独留下"
    assert _version_rows(solution_id) == []
    assert [event.type for event in events if event.type == "run_summary"] == []


# ---------------------------------------------------------------------------
# 六、用户主动取消：也落部分完成版本（内容不丢、由用户处置）
# ---------------------------------------------------------------------------


def test_user_cancel_also_lands_a_partial_version(alice):
    """用户按「停止」也落一版：取消常常意味着「够了」，已生成的正文不该被丢掉。

    取消走的正是 `CONFIRM_CANCEL` 那条「补一次载荷」的条件写（票 #84）；取消不是失败，
    部分完成版本与它同一个事务落库，因此「停止之后界面有内容可用」也成立。
    """
    solution_id = _create_solution(alice)

    def cancel_in_flight() -> None:
        rows = job_service.list_jobs(alice, status="running")
        assert len(rows) == 1
        job_service.cancel_job(alice, int(rows[0]["id"]))

    registered, _events = run_ticket_in_flight(
        alice,
        solution_id,
        "prd",
        {},
        cancel_in_flight,
        StreamEvent(type="chunk", content="# 停止前写出来的正文"),
    )

    row = job_get(alice, registered.job_id)
    assert row["status"] == "cancelled"
    assert row["error_code"] == ""
    assert row["partial"] is True

    version = _current_version(solution_id)
    assert "停止前写出来的正文" in version["content"]
    assert version["partial"] is True
    assert version["review"] is None
    assert version["review_failed_at"] is None


def test_review_job_failure_does_not_land_a_partial_version(alice):
    """重审任务**不**产正文：失败时不该凭空多出一个「部分完成版本」。

    重审的唯一产出是结论（失败时由 `review_failed_at` 记事实）；对话任务同属这一类
    （它的产物是一条轮次，`_document_kind` 根本返回 None）。把它们的失败也升格成正文
    版本，等于给用户一份不存在的产物。
    """
    solution_id = _create_solution(alice)
    document_store.write_document(alice, solution_id, "prd", "# 旧稿", mode="append")

    _registered, _events = run_ticket_events(
        alice,
        solution_id,
        "review",
        {"doc_kind": "prd"},
        StreamEvent(type="error", content="审核失败", data={}, code="LLM_UPSTREAM_ERROR"),
    )

    rows = _version_rows(solution_id)
    assert len(rows) == 1, "重审失败不许追加版本"
    assert rows[0]["content"] == "# 旧稿"
    assert rows[0]["partial"] is False


def test_partial_row_survives_reading_it_back_with_a_fresh_session(alice):
    """标记是**落库事实**：换一个 session 读回来仍在（不是只在提交那一刻的变量里）。

    这条守的是「写进了库」而不是「写进了模型对象」——刷新 / 换设备之后界面仍要能说出
    「这份正文不完整」。`list_versions` / `get_version` 各自开一次 `session_scope`，
    因此读到的是刚提交的那一行，而不是收尾事务里那个对象。
    """
    solution_id = _create_solution(alice)
    _fail_mid_stream(alice, solution_id, events=(_HALF, _EXHAUSTED))

    version_id = int(document_store.list_versions(alice, solution_id, "prd")[0]["version_id"])
    detail = document_store.get_version(alice, solution_id, "prd", version_id)
    assert detail is not None
    assert detail["partial"] is True
    doc = document_store.get_document(alice, solution_id, "prd")
    assert doc is not None and doc["partial"] is True
