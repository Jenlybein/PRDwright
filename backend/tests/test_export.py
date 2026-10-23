"""交付包导出：三份 md + README，无 manifest；越权 / 空交付的边界。

导出不是「完整性门禁」：只有一两份文档时仍然出包，由 README 如实列出缺失项。
全空时 400——导出一个只有 README 的包没有意义。
"""

from __future__ import annotations

import io
import zipfile

from dbsupport import connect
from backend.services import document_store
from conftest import login, register


def _new_solution(client, title: str = "交付测试") -> int:
    response = client.post(
        "/api/solutions",
        json={"title": title, "mode": "structured", "stage": "review-prompts", "data": "{}"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _user_id(username: str) -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE username = ?", (username,)
        ).fetchone()
    assert row is not None
    return int(row["id"])


def _export(client, solution_id: int):
    return client.get(f"/api/solutions/{solution_id}/export")


def test_export_requires_login(client) -> None:
    assert client.get("/api/solutions/1/export").status_code == 401


def test_export_returns_zip_with_docs_and_readme(client) -> None:
    register(client, username="alice")
    solution_id = _new_solution(client)
    owner = _user_id("alice")
    document_store.commit_generated(owner, solution_id, "prd", "# PRD 正文")
    document_store.commit_generated(owner, solution_id, "api_docs", "# 接口正文")
    document_store.commit_generated(owner, solution_id, "prompts", "# 提示词正文")

    response = _export(client, solution_id)

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "filename*=UTF-8''" in response.headers["content-disposition"]

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert names == {"README.md", "PRD.md", "API-Docs.md", "Prompts.md"}
        assert "manifest.json" not in names
        readme = archive.read("README.md").decode("utf-8")
        assert "阅读顺序" in readme
        assert "由 PRDwright 导出" in readme
        assert "未生成" not in readme


def test_export_partial_package_lists_missing(client) -> None:
    register(client, username="alice")
    solution_id = _new_solution(client)
    owner = _user_id("alice")
    document_store.commit_generated(owner, solution_id, "prd", "# 只有 PRD")

    response = _export(client, solution_id)

    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert "PRD.md" in names
        assert "API-Docs.md" not in names
        assert "Prompts.md" not in names
        readme = archive.read("README.md").decode("utf-8")
        assert "接口文档：未生成" in readme
        assert "提示词套件：未生成" in readme


def test_export_empty_is_400(client) -> None:
    register(client, username="alice")
    solution_id = _new_solution(client)

    response = _export(client, solution_id)

    assert response.status_code == 400
    assert "暂无可交付的文档" in response.json()["detail"]


def test_export_other_user_is_404(client) -> None:
    register(client, username="alice")
    solution_id = _new_solution(client)
    owner = _user_id("alice")
    document_store.commit_generated(owner, solution_id, "prd", "# PRD")

    register(client, username="bob")
    login(client, username="bob")

    assert _export(client, solution_id).status_code == 404
