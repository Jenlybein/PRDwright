"""配置与供应商接口（v4 T3 / P4）。

三类断言，各对应一种「上线才发现」的失败：

1. **门禁逐条**：`require_admin` 是逐条挂的，新加 11 条路由里漏挂一条，那条就完全裸奔，
   而它看起来和别的一模一样。
2. **密钥绝不出后端**：响应里只能有变量名与布尔。这条有既有先例
   （`test_admin_management.py` 的「重置密码不写审计」），是同一类纪律。
3. **规则只写一份**：内置不可删、激活中的不可删、删最后一家会让生成全线失败——
   这些拦在服务层，UI 与脚本共用同一份判断。
"""

from __future__ import annotations

import json

import pytest
from conftest import login, register

from backend.core import runtime_config as rc
from backend.core.db import connect
from backend.repositories.runtime_config_repo import (
    ModelProviderRepository,
    SystemConfigRepository,
)
from backend.services import admin_config_service, auth_service

# 全部新增端点。参数化比逐条写更值：这张表本身就是「该挂守卫的端点清单」。
_GET_PATHS = ["/api/admin/config", "/api/admin/config/effective", "/api/admin/providers", "/api/admin/providers/presets"]
_WRITE_CALLS = [
    ("patch", "/api/admin/config", {"updates": {"llm_max_retries": 2}}),
    ("post", "/api/admin/config/reset", {"keys": []}),
    ("post", "/api/admin/providers", {"id": "x1", "protocol": "openai_compatible"}),
    ("patch", "/api/admin/providers/ark", {"name": "n"}),
    ("delete", "/api/admin/providers/openai", None),
    ("post", "/api/admin/providers/ark/activate", None),
]


def _as_admin(client, username: str = "root") -> None:
    register(client, username)
    auth_service.set_admin(username, True)
    login(client, username)


def _providers(client) -> dict:
    return client.get("/api/admin/providers").json()


# ---------------------------------------------------------------------------
# 门禁
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _GET_PATHS)
def test_read_endpoints_reject_normal_user(client, path):
    """普通用户（已登录）访问配置接口必须 403。"""
    register(client)  # alice
    response = client.get(path)
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


@pytest.mark.parametrize("method,path,body", _WRITE_CALLS)
def test_write_endpoints_reject_normal_user(client, method, path, body):
    """写接口同样 403。

    ⚠️ 请求体必须是**合法**的：传空 body 会被 Pydantic 先拦成 422，
    那样测的是字段校验、不是权限守卫——看着红，其实没验到该验的。
    """
    register(client)
    kwargs = {"json": body} if body is not None else {}
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 403, response.text


@pytest.mark.parametrize("path", _GET_PATHS)
def test_read_endpoints_require_login(client, path):
    """未登录 401（而不是 403）：401 让前端去登录，403 会停在无权限页。"""
    assert client.get(path).status_code == 401


# ---------------------------------------------------------------------------
# 配置读写
# ---------------------------------------------------------------------------


def test_get_config_groups_and_metadata(client):
    """分组齐全，每项都带展示元数据与约束。"""
    _as_admin(client)
    data = client.get("/api/admin/config").json()

    assert [g["key"] for g in data["groups"]] == ["prd", "review", "llm", "quota", "auth"]
    items = {item["key"]: item for g in data["groups"] for item in g["items"]}
    assert len(items) == len(rc.WHITELIST)

    # 约束由 `Settings` 反射而来，不是接口层另写一份
    concurrency = items["prd_pipeline_concurrency"]
    assert concurrency["min"] == 1 and concurrency["max"] == 8
    assert concurrency["type"] == "number"
    # 危险项要能被前端识别，用于二次确认
    assert items["prd_section_review_enabled"]["danger"] is True
    # 枚举项带选项
    assert items["review_rewrite_min_severity"]["options"] == ["high", "medium", "low"]
    # 额度项的边界也要从 `Settings` 反射出来（时区偏移有上下界）
    tz = items["quota_timezone_offset_minutes"]
    assert tz["min"] == -720 and tz["max"] == 840

    # 布尔项必须渲染成开关而不是文本框：控件类型由字段注解推导。
    # 这条钉住一次真实踩坑——手写名单漏了 quota_enforce，配置页让管理员手敲 true/false。
    for key in ("quota_enforce", "auth_allow_register", "auth_cookie_secure"):
        assert items[key]["type"] == "bool", f"{key} 应渲染为开关"
    # 安全项：阈值 0 表示关闭限流，允许为 0；窗口至少 1 分钟
    assert items["login_max_attempts"]["min"] == 0
    assert items["login_failure_window_minutes"]["min"] == 1


def test_patch_applies_and_changes_revision(client):
    """改完立即生效，指纹变化（前端据此判断「真的换了一份」）。"""
    _as_admin(client)
    before = client.get("/api/admin/config").json()

    response = client.patch(
        "/api/admin/config", json={"updates": {"prd_pipeline_concurrency": 5}}
    )
    assert response.status_code == 200, response.text

    data = response.json()
    items = {item["key"]: item for g in data["groups"] for item in g["items"]}
    assert items["prd_pipeline_concurrency"]["value"] == 5
    assert items["prd_pipeline_concurrency"]["source"] == "db"
    assert data["revision"] != before["revision"]

    # 独立复核：再拉一次，值必须仍是新的（排除「响应体是回声」）
    again = client.get("/api/admin/config/effective").json()
    items2 = {item["key"]: item for g in again["groups"] for item in g["items"]}
    assert items2["prd_pipeline_concurrency"]["value"] == 5


def test_patch_rejects_out_of_range_and_writes_nothing(client):
    """越界值整批拒绝：一项非法则一项都不写。

    「部分成功」最难用：管理员看到 200 却不知道哪几项生效了。
    """
    _as_admin(client)
    before = client.get("/api/admin/config").json()["revision"]

    response = client.patch(
        "/api/admin/config",
        json={"updates": {"llm_max_retries": 3, "prd_pipeline_concurrency": 99}},
    )
    assert response.status_code == 400
    # 合法的那一项也不能写进去
    assert client.get("/api/admin/config").json()["revision"] == before
    assert SystemConfigRepository.get_all() == {}


def test_patch_rejects_keys_outside_whitelist(client):
    """启动类配置一律不许在后台改。"""
    _as_admin(client)
    for key in ("db_path", "log_level", "auth_enabled"):
        response = client.patch("/api/admin/config", json={"updates": {key: "x"}})
        assert response.status_code == 400, f"{key} 应被拒"
        assert "不可在后台修改" in response.json()["detail"]


def test_reset_restores_default(client):
    """恢复默认 = 删掉覆盖行。"""
    _as_admin(client)
    client.patch("/api/admin/config", json={"updates": {"llm_max_retries": 4}})
    assert SystemConfigRepository.get_all() != {}

    response = client.post("/api/admin/config/reset", json={"keys": ["llm_max_retries"]})
    assert response.status_code == 200
    assert SystemConfigRepository.get_all() == {}
    items = {item["key"]: item for g in response.json()["groups"] for item in g["items"]}
    assert items["llm_max_retries"]["source"] == "env"


# ---------------------------------------------------------------------------
# 供应商
# ---------------------------------------------------------------------------


def test_providers_never_leak_secrets(client, monkeypatch):
    """响应里只能有变量名与布尔，**绝不能出现密钥值**。"""
    _as_admin(client)
    monkeypatch.setenv("ARK_API_KEY", "sk-super-secret-value")

    body = client.get("/api/admin/providers").text
    assert "sk-super-secret-value" not in body
    # 变量名可以给，它是排查缺密钥的唯一线索
    assert "ARK_API_KEY" in body

    ark = next(p for p in _providers(client)["providers"] if p["id"] == "ark")
    assert ark["key_present"] is True
    assert "api_key" not in {key for key in ark if key not in ("api_key_env_var",)}


def test_presets_route_is_not_swallowed_by_provider_id(client):
    """`/providers/presets` 必须注册在 `/providers/{provider_id}` 之前。

    顺序反了它会被当成一个 provider id 去查，然后 404——而错误信息会让人
    以为「预设这个功能没做」。
    """
    _as_admin(client)
    response = client.get("/api/admin/providers/presets")
    assert response.status_code == 200
    presets = response.json()
    assert len(presets) >= 5
    assert {"id", "protocol", "base_url", "api_key_env_var"} <= set(presets[0])


def test_create_provider_and_duplicate_rejected(client):
    _as_admin(client)
    created = client.post(
        "/api/admin/providers",
        json={
            "id": "deepseek",
            "name": "DeepSeek",
            "protocol": "openai_compatible",
            "base_url": "https://api.deepseek.com/v1",
            "api_key_env_var": "DEEPSEEK_API_KEY",
            "default_model": "deepseek-chat",
        },
    )
    assert created.status_code == 201, created.text
    ids = {p["id"] for p in created.json()["providers"]}
    assert "deepseek" in ids

    dup = client.post(
        "/api/admin/providers",
        json={"id": "deepseek", "protocol": "openai_compatible"},
    )
    assert dup.status_code == 400
    assert "已存在" in dup.json()["detail"]


def test_create_provider_rejects_bad_protocol_and_key_var(client):
    _as_admin(client)
    bad_protocol = client.post(
        "/api/admin/providers", json={"id": "x1", "protocol": "no_such_protocol"}
    )
    assert bad_protocol.status_code == 400
    assert "不支持的协议" in bad_protocol.json()["detail"]

    # 变量名会被当成环境变量去查，写成一句中文永远查不到
    bad_var = client.post(
        "/api/admin/providers",
        json={"id": "x2", "protocol": "openai_compatible", "api_key_env_var": "我的密钥"},
    )
    assert bad_var.status_code == 400


def test_builtin_provider_cannot_be_deleted(client):
    """内置行不可删：它是「回到出厂状态」的锚点。"""
    _as_admin(client)
    response = client.delete("/api/admin/providers/ark")
    assert response.status_code == 403
    assert "内置" in response.json()["detail"]
    assert ModelProviderRepository.get("ark") is not None


def test_active_provider_cannot_be_deleted(client):
    """激活中的那家不可删：删完下次生成会全线失败。"""
    _as_admin(client)
    # 先建一家非内置的并激活它
    client.post(
        "/api/admin/providers",
        json={"id": "mine", "protocol": "openai_compatible", "api_key_env_var": "MINE_KEY"},
    )
    client.post("/api/admin/providers/mine/activate")

    response = client.delete("/api/admin/providers/mine")
    assert response.status_code == 403
    assert "激活" in response.json()["detail"]


def test_activate_switches_and_warns_on_missing_key(client, monkeypatch):
    """激活后真切换；缺密钥时给 warning 但**不拦**。

    拦住会让「先切过去、再配密钥」这个正常顺序走不通。
    """
    _as_admin(client)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    client.post(
        "/api/admin/providers",
        json={
            "id": "deepseek",
            "protocol": "openai_compatible",
            "api_key_env_var": "DEEPSEEK_API_KEY",
        },
    )
    rc.invalidate()

    response = client.post("/api/admin/providers/deepseek/activate")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["active_provider_id"] == "deepseek"
    assert "DEEPSEEK_API_KEY" in data["warning"]

    # 配置页上的「当前供应商」也跟着变
    config = client.get("/api/admin/config").json()
    assert config["active_provider_id"] == "deepseek"


def test_disabled_provider_cannot_be_activated(client):
    _as_admin(client)
    client.patch("/api/admin/providers/openai", json={"enabled": False})
    response = client.post("/api/admin/providers/openai/activate")
    assert response.status_code == 400
    assert "停用" in response.json()["detail"]


def test_provider_update_cannot_change_identity(client):
    """`id` 不能通过 PATCH 改（会进任务回执，改了历史就对不上）。"""
    _as_admin(client)
    response = client.patch(
        "/api/admin/providers/openai", json={"id": "hacked", "name": "X"}
    )
    assert response.status_code == 200
    row = ModelProviderRepository.get("openai")
    assert row is not None and row["name"] == "X"
    assert ModelProviderRepository.get("hacked") is None


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------


def test_audit_records_config_and_provider_actions(client, monkeypatch):
    """改配置与切供应商都要留痕，且**不含密钥值**。

    审计是给人翻的，不是保险箱：写进去的密钥等于给下一个读到它的人一份明文口令。
    """
    _as_admin(client)
    monkeypatch.setenv("MINE_KEY", "sk-must-not-appear")

    client.patch("/api/admin/config", json={"updates": {"llm_max_retries": 2}})
    client.post(
        "/api/admin/providers",
        json={"id": "mine", "protocol": "openai_compatible", "api_key_env_var": "MINE_KEY"},
    )
    client.post("/api/admin/providers/mine/activate")

    items = client.get("/api/admin/audit").json()["items"]
    actions = {item["action"] for item in items}
    assert {"config.update", "provider.create", "provider.activate"} <= actions

    serialized = json.dumps(items, ensure_ascii=False)
    assert "sk-must-not-appear" not in serialized
    # 变量名可以出现：它是排查「这家为什么缺密钥」的线索
    assert "MINE_KEY" in serialized
    assert all(item["actor_name"] == "root" for item in items)


# ---------------------------------------------------------------------------
# 服务层规则（绕过 HTTP 直接调，证明规则不在接口层）
# ---------------------------------------------------------------------------


def test_service_layer_enforces_same_rules(client):
    """直接调服务层也一样被挡——接口层不是唯一入口。"""
    _as_admin(client)
    actor = auth_service.get_user(1)
    assert actor is not None

    from backend.core.errors import AppError

    with pytest.raises(AppError) as exc:
        admin_config_service.delete_provider(actor, "ark")
    assert exc.value.status_code == 403

    with pytest.raises(AppError) as exc:
        admin_config_service.update_config(actor, {"db_path": "/tmp/x"})
    assert exc.value.status_code == 400
