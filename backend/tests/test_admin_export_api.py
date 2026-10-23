"""账号数据导出（v4.5 T5-4）：ZIP 的内容、边界与三处「静默错」的防线。

这一组用例守的不是「有没有返回一个文件」，而是四个**错了也不会报错**的地方：

1. **密码材料外泄**。导出包会被转发、拷进网盘、发给审核方。`password_hash` 一旦
   进包，离线爆破的成本就只剩算力——而这件事在功能测试里完全看不出来。
2. **路径穿越**。方案标题是用户输入的，含 `../` 时若直接当目录名，解压方（可能是
   另一台机器上的审核人）的文件会被写到目标目录之外。
3. **选择题丢失**。题干只存在 `asks` 字段里，正文不重复。只导 `content` 的话，
   用户会看到「请确认以下两点」后面空着，像是数据丢了。
4. **CSV 中文乱码**。不带 BOM 的 UTF-8 在 Excel 里是乱码，而这份 CSV 的用途就是
   给运营/审核用表格看。
"""

from __future__ import annotations

import io
import json
import os
import zipfile

from conftest import login, register, users

from backend.core.clock import utc_now
from dbsupport import connect
from backend.core.engine import session_scope
from backend.repositories.chat_turn_repo import ChatTurnRepository
from backend.services import auth_service, document_store


def _create_turn(*args, **kwargs) -> int:
    """写一条对话轮次（仓库不再自己提交，事务由这里显式开）。"""
    with session_scope() as session:
        return ChatTurnRepository(session).create(*args, **kwargs)


def _as_admin(client, username: str = "root") -> None:
    """注册并任命管理员（顺带让 client 处于管理员登录态）。"""
    register(client, username)
    auth_service.set_admin(username, True)
    assert client.get("/api/admin/me").status_code == 200


def _user_id(username: str) -> int:
    row = users.get_by_username(username)
    assert row is not None
    return int(row["id"])


def _new_solution(client, title: str = "支付方案") -> int:
    """经接口建一份方案（当前登录者即为 owner）。"""
    response = client.post(
        "/api/solutions",
        json={"title": title, "mode": "structured", "stage": "entry", "data": "{}"},
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _job(owner_id: int, *, status: str = "completed", prompt_tokens: int = 10) -> None:
    """直接插一条任务记录（绕开仓储的 running-only 建法）。"""
    moment = utc_now().isoformat()
    with connect() as conn:
        conn.execute(
            "INSERT INTO generation_jobs "
            "(owner_id, solution_id, kind, doc_kind, chat_stage, status, phase, "
            " draft_content, completed_sections, input_fingerprint, usage, "
            " max_provider_switches, error_code, error_message, "
            " prompt_tokens, completion_tokens, created_at, updated_at) "
            "VALUES (?, NULL, 'prd', '', '', ?, '', '', '[]', NULL, NULL, 2, '', '', ?, 7, ?, ?)",
            (owner_id, status, prompt_tokens, moment, moment),
        )
        conn.commit()


def _unzip(payload: bytes) -> dict[str, str]:
    """把 ZIP 解成 `{成员名: 文本}`（只用于断言，忽略二进制差异）。"""
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return {
            name: archive.read(name).decode("utf-8", errors="replace")
            for name in archive.namelist()
        }


def _seed_alice(client) -> tuple[int, int]:
    """造一个「有方案、有文档、有对话、有任务」的账号，返回 `(user_id, solution_id)`。"""
    register(client, "alice")
    solution_id = _new_solution(client, "支付方案")
    owner_id = _user_id("alice")
    document_store.commit_generated(owner_id, solution_id, "prd", "# 支付 PRD\n\n正文内容")
    document_store.commit_generated(owner_id, solution_id, "prompts", "第一步：建表")
    _create_turn(
        owner_id, solution_id, stage="chat", role="user", content="这个方案要不要支持退款？"
    )
    _create_turn(
        owner_id, solution_id, stage="chat", role="assistant", content="建议支持，理由如下……"
    )
    _job(owner_id)
    return owner_id, solution_id


def test_export_zip_contains_expected_files(client) -> None:
    """包里有清单、账号、方案摘要、正文、对话与任务 CSV。"""
    _seed_alice(client)
    _as_admin(client)

    response = client.get(f"/api/admin/users/{_user_id('alice')}/export")

    assert response.status_code == 200, response.text
    files = _unzip(response.content)
    assert {"manifest.json", "account.json", "solutions.json", "jobs.csv"} <= set(files)
    assert any(name.startswith("documents/") and name.endswith("prd.md") for name in files)
    assert any(name.startswith("documents/") and name.endswith("prompts.md") for name in files)
    assert any(name.startswith("chats/") for name in files)

    # 正文原样进包，不做任何加工
    prd = next(text for name, text in files.items() if name.endswith("prd.md"))
    assert "# 支付 PRD" in prd

    manifest = json.loads(files["manifest.json"])
    assert manifest["user"]["username"] == "alice"
    assert manifest["counts"]["solutions"] == 1
    assert manifest["counts"]["documents"] == 2
    assert manifest["counts"]["chat_turns"] == 2
    assert manifest["counts"]["jobs"] == 1
    assert manifest["exported_by"] == "root"


def test_export_never_contains_password_material(client) -> None:
    """密码哈希与 salt 绝不进包——包会离开这台机器。

    断言**取键而不是搜文本**：`account.json` 里有一句给人看的说明
    「不含密码哈希与 salt」，按字符串搜会命中这句话本身，用例就永远红。
    """
    _seed_alice(client)
    _as_admin(client)

    with connect() as conn:
        row = conn.execute(
            "SELECT password_hash, salt FROM users WHERE username = 'alice'"
        ).fetchone()
    password_hash = str(row["password_hash"])

    files = _unzip(client.get(f"/api/admin/users/{_user_id('alice')}/export").content)
    account = json.loads(files["account.json"])

    assert "password_hash" not in account
    assert "salt" not in account
    # 哈希原文不能出现在包内任何文件里（防止将来有人「顺手」把它塞进别的字段）
    assert password_hash not in "\n".join(files.values())


def test_export_sanitizes_user_supplied_titles(client) -> None:
    """方案标题里的 `../` 与路径分隔符不能变成 ZIP 内的目录穿越。

    ⚠️ 每份方案都**必须带一份文档**：目录名只有在该方案有文件落盘时才会出现在
    包里的路径上。只建方案不建文档的话，断言会在一个不含标题的成员集合上跑，
    用例恒绿——「消毒逻辑被删掉也不会红」的假通过就是这么来的。
    """
    register(client, "alice")
    owner_id = _user_id("alice")
    # 标题同时含穿越序列、路径分隔符与控制字符
    evil = _new_solution(client, "../../etc/passwd")
    messy = _new_solution(client, "a/b\\c:d*e?f")
    document_store.commit_generated(owner_id, evil, "prd", "# 恶意标题")
    document_store.commit_generated(owner_id, messy, "prd", "# 奇怪标题")
    _as_admin(client)

    files = _unzip(client.get(f"/api/admin/users/{owner_id}/export").content)
    doc_paths = [name for name in files if name.startswith("documents/")]

    assert len(doc_paths) == 2, "每份方案都该有一个文档成员（否则下面等于没测）"
    for name in files:
        assert ".." not in name, f"包内路径不应出现 `..`：{name}"
        assert not name.startswith("/"), f"不应是绝对路径：{name}"
        assert "\\" not in name, f"不应含反斜杠：{name}"
        # 目录层级只允许这两段前缀
        assert name.count("/") <= 2


def test_export_renders_clarification_asks_with_choice(client) -> None:
    """选择题与当时的选择要能在对话转录里读出来。"""
    register(client, "alice")
    solution_id = _new_solution(client, "支付方案")
    owner_id = _user_id("alice")
    asks = [
        {
            "id": "ask-1",
            "question": "退款要走审批流吗？",
            "background": "影响接口数量",
            "multiple": False,
            "options": [
                {"label": "A", "text": "走审批", "description": "多一个审批接口"},
                {"label": "B", "text": "不审批", "description": "退款直接成功"},
            ],
        }
    ]
    answers = [{"ask_id": "ask-1", "labels": ["B"], "question": "退款要走审批流吗？", "texts": ["不审批"]}]
    _create_turn(
        owner_id,
        solution_id,
        stage="chat",
        role="assistant",
        content="请确认以下问题",
        asks=asks,
        answers=answers,
    )
    _as_admin(client)

    files = _unzip(client.get(f"/api/admin/users/{_user_id('alice')}/export").content)
    chat = next(text for name, text in files.items() if name.startswith("chats/"))

    assert "退款要走审批流吗？" in chat
    assert "**请你确认：**" in chat
    # 选中的那一项打勾，没选的不打
    assert "A. 走审批（多一个审批接口）" in chat
    assert "B. 不审批（退款直接成功） ✓" in chat


def test_export_jobs_csv_has_bom_for_excel(client) -> None:
    """CSV 带 UTF-8 BOM：不带的话 Excel 打开中文列头是乱码。"""
    _seed_alice(client)
    _as_admin(client)

    files = _unzip(client.get(f"/api/admin/users/{_user_id('alice')}/export").content)

    assert files["jobs.csv"].startswith("\ufeff")
    assert "prompt_tokens" in files["jobs.csv"]
    assert "completed" in files["jobs.csv"]


def test_export_headers_are_download_friendly(client) -> None:
    """响应头要能让浏览器直接存成文件，且不留缓存。"""
    _seed_alice(client)
    _as_admin(client)

    response = client.get(f"/api/admin/users/{_user_id('alice')}/export")

    assert response.headers["content-type"].startswith("application/zip")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert ".zip" in disposition
    assert "filename*=UTF-8''" in disposition
    # 个人数据不该被任何中间层缓存
    assert response.headers["cache-control"] == "no-store"


def test_export_is_audited(client) -> None:
    """读别人的数据也要留痕：事后能回答「谁在什么时候导走了谁的数据」。"""
    _seed_alice(client)
    _as_admin(client)

    assert client.get(f"/api/admin/users/{_user_id('alice')}/export").status_code == 200

    with connect() as conn:
        rows = conn.execute(
            "SELECT action, target, detail FROM admin_audit_log "
            "WHERE action = 'user.export' ORDER BY id DESC"
        ).fetchall()
    assert rows, "导出必须写审计"
    assert rows[0]["target"] == "alice"
    assert "个文件" in rows[0]["detail"]


def test_export_requires_admin(client) -> None:
    """普通用户不能导出别人的数据（403，不是 404）。"""
    register(client, "alice")
    register(client, "bob")

    response = client.get(f"/api/admin/users/{_user_id('alice')}/export")

    assert response.status_code == 403


def test_export_unknown_user_returns_404(client) -> None:
    """不存在的账号是 404，不能返回一个空包。"""
    _as_admin(client)

    response = client.get("/api/admin/users/9999/export")

    assert response.status_code == 404


def test_export_of_empty_account_still_works(client) -> None:
    """刚注册、什么都没做的账号也能导出（包里有清单和账号，不报错）。"""
    register(client, "alice")
    _as_admin(client)

    response = client.get(f"/api/admin/users/{_user_id('alice')}/export")

    assert response.status_code == 200
    files = _unzip(response.content)
    assert files["solutions.json"].strip() == "[]"
    assert json.loads(files["manifest.json"])["counts"]["jobs"] == 0


def test_login_still_works_after_export(client) -> None:
    """导出是只读的：导完 alice 仍能正常登录（防止误写成「导出即改密」）。"""
    _seed_alice(client)
    _as_admin(client)
    client.get(f"/api/admin/users/{_user_id('alice')}/export")

    assert login(client, "alice").status_code == 200


def test_export_over_the_size_limit_is_413(client, monkeypatch) -> None:
    """超过 `MAX_EXPORT_BYTES` 必须 413，且**不返回半截包**。

    为什么这条值得单独钉：`_ZipBuilder` 把整个包攒在内存里，一个职业用户的历史
    正文 + 对话 + 任务明细完全可能到几十上百 MB。没有这道闸，一次导出就可能把
    进程的 RSS 顶穿（OOM 时表现是「整个服务挂了」，与「某个人导了一次数据」
    在日志上几乎无法关联）。因此它必须**超限即拒**，而不是「慢慢攒完再说」。

    上限是常量，这里用 monkeypatch 压到 4KB 来构造超限——真塞 51MB 只是把同一行
    `if size > MAX_EXPORT_BYTES` 跑得更慢，验的不是别的东西。数据用随机十六进制
    （压缩率接近 1），否则 deflate 会把重复文本压到上限以下，闸门就永远不触发。
    """
    from backend.services import export_service

    register(client, "alice")
    solution_id = _new_solution(client, "大方案")
    owner_id = _user_id("alice")
    document_store.commit_generated(
        owner_id, solution_id, "prd", "# 大 PRD\n\n" + os.urandom(64 * 1024).hex()
    )
    _as_admin(client)

    monkeypatch.setattr(export_service, "MAX_EXPORT_BYTES", 4 * 1024)

    response = client.get(f"/api/admin/users/{owner_id}/export")

    assert response.status_code == 413, response.text
    assert "超过导出上限" in response.json()["detail"]
    # 拒绝时不能漏出半截包：响应体是错误说明，不是 application/zip
    assert not response.headers.get("content-type", "").startswith("application/zip")


def test_export_just_under_the_limit_still_succeeds(client, monkeypatch) -> None:
    """反向对照：上限之内的包必须照常返回。

    没有这一条，把闸门写成 `if size >= 0: raise` 也能让上面那条绿——而那样一来
    任何人都导不出东西。两条一起才说明「闸门是按体积判断的」。
    """
    from backend.services import export_service

    register(client, "alice")
    _new_solution(client, "小方案")
    _as_admin(client)

    monkeypatch.setattr(export_service, "MAX_EXPORT_BYTES", 4 * 1024)

    response = client.get(f"/api/admin/users/{_user_id('alice')}/export")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/zip")
