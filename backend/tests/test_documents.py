"""文档（documents）实体与旧快照回填的测试。

两组关注点：

1. **迁移**：旧快照里的三份正文要能搬进新表，并且守住三条承诺——只复制不删除、
   坏行不拦启动、重复跑不出重复行。
2. **越权**：文档是方案的子资源，别人的方案在我的接口里必须**等同于不存在**。

迁移用例**刻意不使用 `client` 夹具**：那个夹具体内会跑一次应用启动（含迁移），
而这里要的正是「迁移尚未发生」的旧库状态，因此手动插旧数据再手动跑迁移。
"""

from __future__ import annotations

import json

from backend.core.db import connect, utc_now_text
from backend.core.migrations import MIGRATIONS, run_migrations
from backend.services import document_store
from conftest import register


# ---------------------------------------------------------------------------
# 造旧数据（迁移前）
# ---------------------------------------------------------------------------


def _seed_user(username: str = "legacy") -> int:
    """直接插一个用户行——迁移用例不关心认证，只需要一个归属。"""
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, salt, created_at) VALUES (?, ?, ?, ?)",
            (username, "hash", b"salt", utc_now_text()),
        )
        conn.commit()
        return int(cur.lastrowid or 0)


def _seed_solution(owner_id: int, data: str) -> int:
    """插入一份方案，`data` 即前端提交的 `SessionDataV2` JSON 文本。"""
    now = utc_now_text()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO solutions (user_id, title, mode, stage, data, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (owner_id, "旧方案", "structured", "review-prompts", data, now, now),
        )
        conn.commit()
        return int(cur.lastrowid or 0)


def _legacy_snapshot(**overrides) -> str:
    """一份含三份正文与三份用量回执的旧快照（camelCase，与前端字段一致）。"""
    snapshot: dict = {
        "viewState": "review-prompts",
        "prdContent": "# PRD 正文\n\n内容",
        "apiDocsContent": "# 接口文档\n\n内容",
        "promptsContent": "# 提示词\n\n内容",
        "prdRunSummary": {"kind": "prd", "durationMs": 1234},
        "apiDocsRunSummary": {"kind": "api_docs", "durationMs": 99},
        "promptsRunSummary": {"kind": "prompts", "durationMs": 55},
    }
    snapshot.update(overrides)
    return json.dumps(snapshot, ensure_ascii=False)


def _documents(solution_id: int) -> dict[str, dict]:
    """按产物类型取出某方案下的文档行，并带上**当前版本**的正文元数据。

    版本模型下正文住在 `document_versions`，`documents` 只剩身份 + 当前版本指针，
    因此这里 JOIN 当前版本取回 `content / content_sha / pending_items / last_run_summary`，
    让断言仍能从 `docs[kind]["content"]` 取到正文。
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT d.kind, v.content, v.content_sha, v.pending_items, v.last_run_summary "
            "FROM documents d "
            "LEFT JOIN document_versions v ON v.id = d.current_version_id "
            "WHERE d.solution_id = ?",
            (solution_id,),
        ).fetchall()
    return {row["kind"]: dict(row) for row in rows}


def _versions(solution_id: int, kind: str) -> list[dict]:
    """取某方案某产物的全部版本行（从新到旧），供版本语义断言使用。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT v.* FROM document_versions v "
            "JOIN documents d ON d.id = v.document_id "
            "WHERE d.solution_id = ? AND d.kind = ? "
            "ORDER BY v.version_no DESC",
            (solution_id, kind),
        ).fetchall()
    return [dict(row) for row in rows]


def _user_id(username: str) -> int:
    with connect() as conn:
        row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    return int(row["id"])


# ---------------------------------------------------------------------------
# 迁移
# ---------------------------------------------------------------------------


def test_real_plan_creates_documents_table() -> None:
    """真实的迁移清单跑完后，`documents` 与 `document_versions` 表就位（防止清单与建表语句脱节）。"""
    # 与清单长度比而不是写死数字：新增一步迁移不该顺手改坏这两个用例，
    # 而「清单没跑完」这件事仍然会被抓住
    assert run_migrations() == len(MIGRATIONS)

    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'documents'"
        ).fetchone()
        versions = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'document_versions'"
        ).fetchone()
    assert row is not None
    assert versions is not None


def test_backfill_moves_three_documents() -> None:
    """旧快照里的三份正文各建一行，正文、指纹、清单占位与回执都到位。"""
    owner = _seed_user()
    solution_id = _seed_solution(owner, _legacy_snapshot())

    run_migrations()

    docs = _documents(solution_id)
    assert set(docs) == {"prd", "api_docs", "prompts"}

    assert docs["prd"]["content"] == "# PRD 正文\n\n内容"
    assert len(docs["prd"]["content_sha"]) == 12
    # 旧文档没有清单机制，一律是空清单
    assert json.loads(docs["prd"]["pending_items"]) == []
    # 旧回执原样搬过来（不解析、不改形状）
    assert json.loads(docs["prd"]["last_run_summary"]) == {"kind": "prd", "durationMs": 1234}


def test_backfill_keeps_legacy_snapshot_intact() -> None:
    """回填是「复制」不是「搬走」：旧字段仍留在快照里，随时能回退。"""
    owner = _seed_user()
    solution_id = _seed_solution(owner, _legacy_snapshot())

    run_migrations()

    with connect() as conn:
        row = conn.execute("SELECT data FROM solutions WHERE id = ?", (solution_id,)).fetchone()
    assert json.loads(row["data"])["prdContent"] == "# PRD 正文\n\n内容"


def test_backfill_skips_corrupt_row_and_continues() -> None:
    """某一行 JSON 损坏只跳过它，不拦死迁移，也不丢原文。"""
    owner = _seed_user()
    broken = _seed_solution(owner, "{这不是合法 JSON")
    good = _seed_solution(owner, _legacy_snapshot())

    # 与清单长度比而不是写死数字：新增一步迁移不该顺手改坏这两个用例，
    # 而「清单没跑完」这件事仍然会被抓住
    assert run_migrations() == len(MIGRATIONS)

    assert _documents(broken) == {}
    assert set(_documents(good)) == {"prd", "api_docs", "prompts"}

    with connect() as conn:
        row = conn.execute("SELECT data FROM solutions WHERE id = ?", (broken,)).fetchone()
    assert row["data"] == "{这不是合法 JSON"


def test_backfill_skips_empty_and_non_string_content() -> None:
    """空串、纯空白、非字符串一律不建行——不为「没有文档」凭空造文档。"""
    owner = _seed_user()
    solution_id = _seed_solution(
        owner,
        _legacy_snapshot(prdContent="   ", apiDocsContent=None, promptsContent=""),
    )

    run_migrations()

    assert _documents(solution_id) == {}


def test_backfill_is_idempotent() -> None:
    """迁移跑第二次不会写重复行（版本号已推进，整步跳过）。"""
    owner = _seed_user()
    solution_id = _seed_solution(owner, _legacy_snapshot())

    run_migrations()
    run_migrations()

    assert len(_documents(solution_id)) == 3


# ---------------------------------------------------------------------------
# 接口：读取
# ---------------------------------------------------------------------------


def _new_solution(client, data: str = '{"viewState":"entry"}') -> int:
    """经接口建一份方案，返回 id。"""
    response = client.post(
        "/api/solutions",
        json={"title": "文档测试", "mode": "structured", "stage": "entry", "data": data},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_list_documents_is_empty_for_a_new_solution(client) -> None:
    """还没生成过文档时，返回空列表而不是 404。"""
    register(client)
    solution_id = _new_solution(client)

    response = client.get(f"/api/solutions/{solution_id}/documents")

    assert response.status_code == 200
    assert response.json() == {"documents": []}


def test_list_documents_requires_login(client) -> None:
    """未登录不返回任何方案内容。"""
    assert client.get("/api/solutions/1/documents").status_code == 401


def test_documents_404_for_unknown_solution(client) -> None:
    register(client)
    assert client.get("/api/solutions/99999/documents").status_code == 404


def test_documents_are_invisible_to_other_users(client) -> None:
    """别人的方案在我这里等同于不存在：读、写都 404。"""
    register(client, username="alice")
    solution_id = _new_solution(client)

    client.cookies.clear()
    register(client, username="bob")

    assert client.get(f"/api/solutions/{solution_id}/documents").status_code == 404
    saved = client.put(f"/api/solutions/{solution_id}/documents/prd", json={"content": "篡改"})
    assert saved.status_code == 404


# ---------------------------------------------------------------------------
# 接口：写入
# ---------------------------------------------------------------------------


def test_save_document_upserts_without_duplicating(client) -> None:
    """同一产物反复保存只有一行，内容以后写的为准。"""
    register(client)
    solution_id = _new_solution(client)

    first = client.put(f"/api/solutions/{solution_id}/documents/prd", json={"content": "# 一版"})
    assert first.status_code == 200, first.text
    assert first.json()["kind"] == "prd"
    assert first.json()["content"] == "# 一版"

    second = client.put(f"/api/solutions/{solution_id}/documents/prd", json={"content": "# 二版"})
    assert second.json()["content"] == "# 二版"

    listed = client.get(f"/api/solutions/{solution_id}/documents").json()["documents"]
    assert len(listed) == 1


def test_save_document_keeps_pending_items_and_run_summary(client) -> None:
    """手改只动正文：清单与回执属于「上一次生成」，不该被顺手清掉。"""
    register(client, username="alice")
    solution_id = _new_solution(client)
    owner = _user_id("alice")
    item = {"id": "features-1", "question": "MVP 功能清单是否需要调整？"}
    document_store.commit_generated(
        owner,
        solution_id,
        "prd",
        "# 生成版",
        pending_items=[item],
        run_summary={"kind": "prd", "duration_ms": 4321},
    )

    body = client.put(
        f"/api/solutions/{solution_id}/documents/prd", json={"content": "# 手改版"}
    ).json()

    assert body["content"] == "# 手改版"
    assert body["pending_items"] == [item]
    assert body["last_run_summary"] == {"kind": "prd", "duration_ms": 4321}


def test_invalid_kind_is_rejected(client) -> None:
    """产物类型是闭集合：`optimize` 之类的取值不该被当成一种文档存下来。"""
    register(client)
    solution_id = _new_solution(client)

    response = client.put(
        f"/api/solutions/{solution_id}/documents/optimize", json={"content": "x"}
    )

    assert response.status_code == 422


def test_commit_generated_replaces_all_three_fields(client) -> None:
    """生成收尾是一次整批替换：正文、清单、回执三者同批更新。"""
    register(client, username="alice")
    solution_id = _new_solution(client)
    owner = _user_id("alice")

    document_store.commit_generated(
        owner, solution_id, "prd", "第一版", pending_items=[{"id": "a"}], run_summary={"kind": "prd"}
    )
    item = document_store.commit_generated(
        owner,
        solution_id,
        "prd",
        "第二版",
        pending_items=[],
        run_summary={"kind": "prd", "duration_ms": 2},
    )

    assert item is not None
    assert item["content"] == "第二版"
    assert item["pending_items"] == []
    assert item["last_run_summary"] == {"kind": "prd", "duration_ms": 2}


def test_deleting_solution_cascades_to_documents(client) -> None:
    """删方案时文档跟着走，不留孤儿行。"""
    register(client, username="alice")
    solution_id = _new_solution(client)
    owner = _user_id("alice")
    document_store.commit_generated(owner, solution_id, "prd", "# x")
    assert len(_documents(solution_id)) == 1

    assert client.delete(f"/api/solutions/{solution_id}").status_code == 200

    with connect() as conn:
        left = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE solution_id = ?", (solution_id,)
        ).fetchone()[0]
    assert left == 0


# ---------------------------------------------------------------------------
# 版本链：写入语义（生成追加、手改/就地改不追加、checkpoint/restore 追加）
# ---------------------------------------------------------------------------


def test_generation_appends_version_and_manual_edit_reuses_it(client) -> None:
    """整篇生成 = 追加新版本；手改 = 就地改当前版本、不新增条目。"""
    register(client, username="alice")
    solution_id = _new_solution(client)
    owner = _user_id("alice")

    document_store.commit_generated(owner, solution_id, "prd", "第一版")
    assert [v["version_no"] for v in _versions(solution_id, "prd")] == [1]

    body = client.put(
        f"/api/solutions/{solution_id}/documents/prd", json={"content": "手改一版"}
    ).json()

    assert body["content"] == "手改一版"
    versions = _versions(solution_id, "prd")
    assert [v["version_no"] for v in versions] == [1]  # 条目数不变
    assert versions[0]["content"] == "手改一版"  # 就地改了当前版本


def test_in_place_rewrite_replaces_all_three_fields_without_new_version(client) -> None:
    """`optimize` / `clarify` 的就地改写不新增版本，但正文 + 清单 + 回执同批替换。"""
    register(client, username="alice")
    solution_id = _new_solution(client)
    owner = _user_id("alice")

    document_store.commit_generated(
        owner, solution_id, "prd", "生成版", pending_items=[{"id": "a"}], run_summary={"kind": "prd"}
    )
    document_store.commit_generated(
        owner,
        solution_id,
        "prd",
        "改写版",
        pending_items=[{"id": "b"}],
        run_summary={"kind": "prd", "duration_ms": 9},
        append_version=False,
        source="optimize",
    )

    versions = _versions(solution_id, "prd")
    assert [v["version_no"] for v in versions] == [1]
    assert versions[0]["content"] == "改写版"
    assert json.loads(versions[0]["pending_items"]) == [{"id": "b"}]
    assert json.loads(versions[0]["last_run_summary"]) == {"kind": "prd", "duration_ms": 9}


def test_regenerating_appends_version_and_keeps_downstream(client) -> None:
    """整篇重生成：追加新版本；下游**保留**，过期由 `stale` 标记提示。"""
    register(client, username="alice")
    solution_id = _new_solution(client)
    owner = _user_id("alice")

    document_store.commit_generated(owner, solution_id, "prd", "PRD v1")
    prd_meta = document_store.current_version_meta(owner, solution_id, "prd")
    document_store.commit_generated(
        owner,
        solution_id,
        "api_docs",
        "接口 v1",
        metadata={"derived_from": {"prd": prd_meta}},
    )
    document_store.commit_generated(
        owner,
        solution_id,
        "prompts",
        "提示词 v1",
        metadata={"derived_from": {"prd": prd_meta}},
    )

    document_store.commit_generated(owner, solution_id, "prd", "PRD v2")

    docs = {doc["kind"]: doc for doc in document_store.list_documents(owner, solution_id)}
    assert set(docs) == {"prd", "api_docs", "prompts"}
    assert [v["version_no"] for v in _versions(solution_id, "prd")] == [2, 1]
    # 下游不再删除；上游换版后按内容指纹标成「可能已过期」
    assert docs["api_docs"]["stale"] is True
    assert docs["prompts"]["stale"] is True


def test_downstream_goes_stale_after_upstream_hand_edit(client) -> None:
    """手改上游不升版本号，但内容指纹变了 → 下游同样应被标记过期。"""
    register(client, username="alice")
    solution_id = _new_solution(client)
    owner = _user_id("alice")

    document_store.commit_generated(owner, solution_id, "prd", "PRD v1")
    prd_meta = document_store.current_version_meta(owner, solution_id, "prd")
    document_store.commit_generated(
        owner,
        solution_id,
        "api_docs",
        "接口 v1",
        metadata={"derived_from": {"prd": prd_meta}},
    )
    assert document_store.get_document(owner, solution_id, "api_docs")["stale"] is False

    document_store.save_document(owner, solution_id, "prd", "PRD v1（手改一句）")

    assert document_store.get_document(owner, solution_id, "api_docs")["stale"] is True


def test_checkpoint_appends_version_and_inherits_meta(client) -> None:
    """「存为新版本」追加一条 checkpoint 版本，清单 / 回执从当前版本继承。"""
    register(client, username="alice")
    solution_id = _new_solution(client)
    owner = _user_id("alice")
    item = {"id": "x", "question": "?"}
    document_store.commit_generated(
        owner, solution_id, "prd", "生成版", pending_items=[item], run_summary={"kind": "prd"}
    )

    saved = document_store.checkpoint(owner, solution_id, "prd", "手工存的一版", "满意")

    assert saved is not None
    assert saved["content"] == "手工存的一版"
    versions = _versions(solution_id, "prd")
    assert [v["version_no"] for v in versions] == [2, 1]
    assert versions[0]["source"] == "checkpoint"
    assert versions[0]["note"] == "满意"
    assert json.loads(versions[0]["pending_items"]) == [item]
    assert json.loads(versions[0]["last_run_summary"]) == {"kind": "prd"}


def test_restore_appends_new_version_copying_source(client) -> None:
    """恢复历史版本 = 追加一条新版本（source='restore'），正文 / 清单取源版本。"""
    register(client, username="alice")
    solution_id = _new_solution(client)
    owner = _user_id("alice")

    document_store.commit_generated(
        owner, solution_id, "prd", "第一版", pending_items=[{"id": "v1"}]
    )
    document_store.commit_generated(
        owner, solution_id, "prd", "第二版", pending_items=[{"id": "v2"}]
    )
    first = [v for v in _versions(solution_id, "prd") if v["version_no"] == 1][0]

    restored = document_store.restore(owner, solution_id, "prd", int(first["id"]))

    assert restored is not None
    assert restored["content"] == "第一版"
    assert restored["pending_items"] == [{"id": "v1"}]
    versions = _versions(solution_id, "prd")
    assert [v["version_no"] for v in versions] == [3, 2, 1]  # 历史只增不改
    assert versions[0]["source"] == "restore"
    assert versions[0]["content"] == "第一版"


def test_restore_unknown_version_returns_none(client) -> None:
    """恢复一个不存在的版本：返回 None（路由翻 404），不改动当前文档。"""
    register(client, username="alice")
    solution_id = _new_solution(client)
    owner = _user_id("alice")
    document_store.commit_generated(owner, solution_id, "prd", "当前版")

    assert document_store.restore(owner, solution_id, "prd", 99999) is None
    assert [v["version_no"] for v in _versions(solution_id, "prd")] == [1]


# ---------------------------------------------------------------------------
# 版本链：读取
# ---------------------------------------------------------------------------


def test_list_versions_summary_marks_current(client) -> None:
    """版本摘要按新→旧排序，标出当前版本，且**不带正文**。"""
    register(client, username="alice")
    solution_id = _new_solution(client)
    owner = _user_id("alice")
    document_store.commit_generated(owner, solution_id, "prd", "第一版")
    document_store.commit_generated(owner, solution_id, "prd", "第二版")

    versions = document_store.list_versions(owner, solution_id, "prd")

    assert [v["version_no"] for v in versions] == [2, 1]
    assert versions[0]["is_current"] is True
    assert versions[1]["is_current"] is False
    # 摘要是列表用的，不含正文
    assert "content" not in versions[0]
    assert versions[0]["content_length"] == len("第二版")


def test_get_version_returns_full_content(client) -> None:
    """版本详情含正文 / 清单 / 回执；不存在时返回 None。"""
    register(client, username="alice")
    solution_id = _new_solution(client)
    owner = _user_id("alice")
    document_store.commit_generated(
        owner, solution_id, "prd", "第一版", pending_items=[{"id": "a"}], run_summary={"kind": "prd"}
    )
    version_id = int(_versions(solution_id, "prd")[0]["id"])

    detail = document_store.get_version(owner, solution_id, "prd", version_id)
    assert detail is not None
    assert detail["content"] == "第一版"
    assert detail["pending_items"] == [{"id": "a"}]
    assert detail["last_run_summary"] == {"kind": "prd"}
    assert detail["is_current"] is True

    assert document_store.get_version(owner, solution_id, "prd", 99999) is None


def test_document_outward_shape_is_unchanged(client) -> None:
    """回归护栏：`GET documents` 的对外结构不因版本化而增删字段。"""
    register(client, username="alice")
    solution_id = _new_solution(client)
    owner = _user_id("alice")
    document_store.commit_generated(owner, solution_id, "prd", "# 正文")

    body = client.get(f"/api/solutions/{solution_id}/documents").json()

    assert set(body) == {"documents"}
    assert set(body["documents"][0]) == {
        "kind",
        "content",
        "pending_items",
        "last_run_summary",
        "updated_at",
        "stale",
        "derived_from",
    }


# ---------------------------------------------------------------------------
# 接口：版本链（列表 / 详情 / 存为新版本 / 恢复）
# ---------------------------------------------------------------------------


def test_version_endpoints_require_login(client) -> None:
    """未登录时四个版本端点都拒绝，不泄露任何方案内容。"""
    assert client.get("/api/solutions/1/documents/prd/versions").status_code == 401
    assert client.get("/api/solutions/1/documents/prd/versions/1").status_code == 401
    assert (
        client.post(
            "/api/solutions/1/documents/prd/checkpoint", json={"content": "x"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/solutions/1/documents/prd/restore", json={"version_id": 1}
        ).status_code
        == 401
    )


def test_version_list_is_empty_for_new_solution(client) -> None:
    """方案可见但还没有任何版本时，返回 200 + 空列表（不是 404）。"""
    register(client)
    solution_id = _new_solution(client)

    response = client.get(f"/api/solutions/{solution_id}/documents/prd/versions")

    assert response.status_code == 200
    assert response.json() == {"versions": []}


def test_version_list_and_detail_via_http(client) -> None:
    """列表给摘要（不含正文 / 标出当前版本）；详情给完整正文与清单。"""
    register(client, username="alice")
    solution_id = _new_solution(client)
    owner = _user_id("alice")
    document_store.commit_generated(
        owner,
        solution_id,
        "prd",
        "# 第一版",
        pending_items=[{"id": "a"}],
        run_summary={"kind": "prd"},
    )

    listed = client.get(f"/api/solutions/{solution_id}/documents/prd/versions").json()
    assert len(listed["versions"]) == 1
    item = listed["versions"][0]
    assert item["version_no"] == 1
    assert item["is_current"] is True
    assert item["source"] == "prd"
    assert "content" not in item  # 摘要不带正文

    detail = client.get(
        f"/api/solutions/{solution_id}/documents/prd/versions/{item['version_id']}"
    ).json()
    assert detail["content"] == "# 第一版"
    assert detail["pending_items"] == [{"id": "a"}]
    assert detail["last_run_summary"] == {"kind": "prd"}


def test_checkpoint_via_http_appends_version(client) -> None:
    """「存为新版本」把给定正文追加为新版本，并把当前文档切到它。"""
    register(client, username="alice")
    solution_id = _new_solution(client)
    owner = _user_id("alice")
    document_store.commit_generated(owner, solution_id, "prd", "# 生成版")

    response = client.post(
        f"/api/solutions/{solution_id}/documents/prd/checkpoint",
        json={"content": "# 存档版", "note": "满意"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["content"] == "# 存档版"
    listed = client.get(f"/api/solutions/{solution_id}/documents/prd/versions").json()
    assert [v["version_no"] for v in listed["versions"]] == [2, 1]
    assert listed["versions"][0]["source"] == "checkpoint"
    assert listed["versions"][0]["note"] == "满意"


def test_checkpoint_requires_content(client) -> None:
    """`content` 是必填：缺了它无法确定「要留档的是哪一版」，应被 422 挡住。"""
    register(client)
    solution_id = _new_solution(client)

    response = client.post(
        f"/api/solutions/{solution_id}/documents/prd/checkpoint", json={"note": "x"}
    )

    assert response.status_code == 422


def test_restore_via_http_switches_current_back(client) -> None:
    """恢复历史版本后，读到的当前文档就是被恢复的那一版。"""
    register(client, username="alice")
    solution_id = _new_solution(client)
    owner = _user_id("alice")
    document_store.commit_generated(owner, solution_id, "prd", "# 第一版")
    document_store.commit_generated(owner, solution_id, "prd", "# 第二版")
    first = [v for v in _versions(solution_id, "prd") if v["version_no"] == 1][0]

    restored = client.post(
        f"/api/solutions/{solution_id}/documents/prd/restore",
        json={"version_id": int(first["id"])},
    )

    assert restored.status_code == 200, restored.text
    assert restored.json()["content"] == "# 第一版"
    # 读「当前文档」应看到恢复后的那一版
    listed_docs = client.get(f"/api/solutions/{solution_id}/documents").json()["documents"]
    current = next(d for d in listed_docs if d["kind"] == "prd")
    assert current["content"] == "# 第一版"
    listed = client.get(f"/api/solutions/{solution_id}/documents/prd/versions").json()
    assert [v["version_no"] for v in listed["versions"]] == [3, 2, 1]
    assert listed["versions"][0]["is_current"] is True


def test_unknown_version_returns_404(client) -> None:
    """版本不存在：列表外的 id 在详情与恢复上都回 404。"""
    register(client, username="alice")
    solution_id = _new_solution(client)
    owner = _user_id("alice")
    document_store.commit_generated(owner, solution_id, "prd", "# x")

    assert (
        client.get(f"/api/solutions/{solution_id}/documents/prd/versions/99999").status_code
        == 404
    )
    assert (
        client.post(
            f"/api/solutions/{solution_id}/documents/prd/restore", json={"version_id": 99999}
        ).status_code
        == 404
    )


def test_versions_are_invisible_to_other_users(client) -> None:
    """别人的方案：读版本、存版本、恢复都等同不存在（404）。"""
    register(client, username="alice")
    solution_id = _new_solution(client)
    owner = _user_id("alice")
    document_store.commit_generated(owner, solution_id, "prd", "# 私有")
    version_id = int(_versions(solution_id, "prd")[0]["id"])

    client.cookies.clear()
    register(client, username="bob")

    assert client.get(f"/api/solutions/{solution_id}/documents/prd/versions").status_code == 404
    assert (
        client.get(
            f"/api/solutions/{solution_id}/documents/prd/versions/{version_id}"
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/solutions/{solution_id}/documents/prd/checkpoint", json={"content": "篡改"}
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/solutions/{solution_id}/documents/prd/restore",
            json={"version_id": version_id},
        ).status_code
        == 404
    )


def test_version_routes_reject_invalid_kind(client) -> None:
    """`kind` 在版本路由上同样是闭集合：非法取值 422。"""
    register(client)
    solution_id = _new_solution(client)

    assert (
        client.get(f"/api/solutions/{solution_id}/documents/nope/versions").status_code == 422
    )


# ---------------------------------------------------------------------------
# 接口：删除历史版本
#
# 「删一条不满意的旧版」与「换掉当前在用的那一版」是两件事：前者只动历史、不碰正文，
# 后者请用「恢复」（追加而非删除）。这批用例把这条边界钉死。
# ---------------------------------------------------------------------------


def test_delete_version_removes_history_and_keeps_current(client) -> None:
    """删历史版本：列表少一条、当前正文**一点不变**。"""
    register(client, username="alice")
    solution_id = _new_solution(client)
    owner = _user_id("alice")
    document_store.commit_generated(owner, solution_id, "prd", "# 第一版")
    document_store.commit_generated(owner, solution_id, "prd", "# 第二版")
    first = [v for v in _versions(solution_id, "prd") if v["version_no"] == 1][0]

    response = client.delete(
        f"/api/solutions/{solution_id}/documents/prd/versions/{int(first['id'])}"
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "version_id": int(first["id"])}
    assert [v["version_no"] for v in _versions(solution_id, "prd")] == [2]
    # 当前正文不受影响：删的是历史，不是「换一版」
    assert document_store.get_document(owner, solution_id, "prd")["content"] == "# 第二版"


def test_deleting_current_version_is_rejected(client) -> None:
    """当前版本不给删：它是这份文档正在用的正文。"""
    register(client, username="alice")
    solution_id = _new_solution(client)
    owner = _user_id("alice")
    document_store.commit_generated(owner, solution_id, "prd", "# 第一版")
    current = _versions(solution_id, "prd")[0]

    response = client.delete(
        f"/api/solutions/{solution_id}/documents/prd/versions/{int(current['id'])}"
    )

    assert response.status_code == 400, response.text
    assert response.json()["code"] == "INVALID_REQUEST"
    assert "当前版本不能删除" in response.json()["detail"]
    # 原样保留
    assert [v["version_no"] for v in _versions(solution_id, "prd")] == [1]


def test_cannot_delete_the_only_remaining_version(client) -> None:
    """删到只剩当前版本为止——最后一份正文不会凭空消失。"""
    register(client, username="alice")
    solution_id = _new_solution(client)
    owner = _user_id("alice")
    document_store.commit_generated(owner, solution_id, "prd", "# 第一版")
    document_store.commit_generated(owner, solution_id, "prd", "# 第二版")
    document_store.commit_generated(owner, solution_id, "prd", "# 第三版")
    by_no = {v["version_no"]: int(v["id"]) for v in _versions(solution_id, "prd")}

    # v1、v2 都能删
    assert client.delete(f"/api/solutions/{solution_id}/documents/prd/versions/{by_no[1]}").status_code == 200
    assert client.delete(f"/api/solutions/{solution_id}/documents/prd/versions/{by_no[2]}").status_code == 200

    # 只剩 v3（当前版本）——再删就是 400
    assert client.delete(f"/api/solutions/{solution_id}/documents/prd/versions/{by_no[3]}").status_code == 400
    assert [v["version_no"] for v in _versions(solution_id, "prd")] == [3]


def test_delete_unknown_version_returns_404(client) -> None:
    """不存在的版本：404，且不动已有版本。"""
    register(client, username="alice")
    solution_id = _new_solution(client)
    owner = _user_id("alice")
    document_store.commit_generated(owner, solution_id, "prd", "# x")

    response = client.delete(f"/api/solutions/{solution_id}/documents/prd/versions/99999")

    assert response.status_code == 404
    assert [v["version_no"] for v in _versions(solution_id, "prd")] == [1]


def test_delete_version_requires_login(client) -> None:
    """未登录不返回任何方案内容。"""
    assert client.delete("/api/solutions/1/documents/prd/versions/1").status_code == 401


def test_delete_version_invisible_to_other_users(client) -> None:
    """别人的方案：等同不存在（404），且那条版本仍在。"""
    register(client, username="alice")
    solution_id = _new_solution(client)
    owner = _user_id("alice")
    document_store.commit_generated(owner, solution_id, "prd", "# 第一版")
    document_store.commit_generated(owner, solution_id, "prd", "# 第二版")
    first = [v for v in _versions(solution_id, "prd") if v["version_no"] == 1][0]

    client.cookies.clear()
    register(client, username="bob")

    response = client.delete(
        f"/api/solutions/{solution_id}/documents/prd/versions/{int(first['id'])}"
    )

    assert response.status_code == 404
    assert [v["version_no"] for v in _versions(solution_id, "prd")] == [2, 1]


def test_delete_version_rejects_invalid_kind(client) -> None:
    """`kind` 在删除路由上同样是闭集合：非法取值 422。"""
    register(client)
    solution_id = _new_solution(client)

    assert (
        client.delete(f"/api/solutions/{solution_id}/documents/nope/versions/1").status_code
        == 422
    )


def test_delete_version_service_outcomes(client) -> None:
    """服务层把三种结果分得很清楚（路由据此翻状态码，不拿文案判断）。"""
    register(client, username="alice")
    solution_id = _new_solution(client)
    owner = _user_id("alice")
    document_store.commit_generated(owner, solution_id, "prd", "# 第一版")
    document_store.commit_generated(owner, solution_id, "prd", "# 第二版")
    by_no = {v["version_no"]: int(v["id"]) for v in _versions(solution_id, "prd")}

    # 不存在的版本 → missing
    assert document_store.delete_version(owner, solution_id, "prd", 99999) == "missing"
    # 当前版本 → is_current
    assert document_store.delete_version(owner, solution_id, "prd", by_no[2]) == "is_current"
    # 历史版本 → ok
    assert document_store.delete_version(owner, solution_id, "prd", by_no[1]) == "ok"
    # 已删掉的再删 → missing
    assert document_store.delete_version(owner, solution_id, "prd", by_no[1]) == "missing"
