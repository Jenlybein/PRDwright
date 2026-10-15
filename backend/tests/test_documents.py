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
from backend.core.migrations import run_migrations
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
    """按产物类型取出某方案下的文档行。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM documents WHERE solution_id = ?", (solution_id,)
        ).fetchall()
    return {row["kind"]: dict(row) for row in rows}


def _user_id(username: str) -> int:
    with connect() as conn:
        row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    return int(row["id"])


# ---------------------------------------------------------------------------
# 迁移
# ---------------------------------------------------------------------------


def test_real_plan_creates_documents_table() -> None:
    """真实的迁移清单跑完后，`documents` 表就位（防止清单与建表语句脱节）。"""
    assert run_migrations() == 2

    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'documents'"
        ).fetchone()
    assert row is not None


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

    assert run_migrations() == 2

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
