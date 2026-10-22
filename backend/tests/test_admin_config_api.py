"""配置与供应商接口（v4 T3 / P4；v4.6 起改为「配置条目」模型）。

三类断言，各对应一种「上线才发现」的失败：

1. **门禁逐条**：`require_admin` 是逐条挂的，新加 11 条路由里漏挂一条，那条就完全裸奔，
   而它看起来和别的一模一样。
2. **密钥绝不出后端**：响应里只能有变量名与布尔。这条有既有先例
   （`test_admin_management.py` 的「重置密码不写审计」），是同一类纪律。
3. **规则只写一份**：同一时刻只有一条「使用中」、使用中的不能直删、协议创建后不可改——
   这些拦在服务层，UI 与脚本共用同一份判断。

v19 之后的前提变了
------------------
`model_providers` 里**不再有内置播种行**，id 也由后端生成（`cfg_xxx`）。所以本文件里
任何「拿 `ark` / `openai` 当 id 用」的写法都必须先自己 `create` 一条，再拿返回的 id 操作。
这不是测试写得啰嗦，而是真实前提：装完之后列表就是空的，管理员的第一个动作一定是「新增」。
"""

from __future__ import annotations

import json

import pytest
from conftest import login, register

from backend.core import runtime_config as rc
from backend.repositories.runtime_config_repo import (
    ModelProviderRepository,
    SystemConfigRepository,
)
from backend.services import admin_config_service, auth_service

# 全部新增端点。参数化比逐条写更值：这张表本身就是「该挂守卫的端点清单」。
_GET_PATHS = ["/api/admin/config", "/api/admin/config/effective", "/api/admin/providers", "/api/admin/providers/presets"]
# ⚠️ 这里的 id 可以是不存在的：`require_admin` 是依赖，先于路径参数解析生效，
# 所以拿到的仍是 403 而不是 404——正是要验的那件事。
_WRITE_CALLS = [
    ("patch", "/api/admin/config", {"updates": {"llm_max_retries": 2}}),
    ("post", "/api/admin/config/reset", {"keys": []}),
    ("post", "/api/admin/providers", {"name": "x1", "protocol": "openai_compatible"}),
    ("patch", "/api/admin/providers/cfg_nope", {"name": "n"}),
    ("delete", "/api/admin/providers/cfg_nope", None),
    ("post", "/api/admin/providers/cfg_nope/activate", None),
]

# 新增一条配置的默认载荷。名字故意带中文：配置名是给人看的标签，不是标识符。
_BASE_PAYLOAD: dict = {
    "name": "方舟-测试",
    "protocol": "ark",
    "base_url": "https://ark.cn-beijing.volces.com/api/v3",
    "default_model": "doubao-seed-2-1-turbo",
}


def _as_admin(client, username: str = "root") -> None:
    register(client, username)
    auth_service.set_admin(username, True)
    login(client, username)


def _providers(client) -> dict:
    return client.get("/api/admin/providers").json()


def _create(client, **overrides) -> str:
    """新增一条配置，返回后端生成的 id。

    ⚠️ 只能「按名字取最后一条」：id 是随机后缀，重名又合法，所以除了「刚建的那条」，
    没有任何字段能把两条同名配置区分开。列表按 `sort_order` 升序，最后一条就是刚建的。
    """
    payload = {**_BASE_PAYLOAD, **overrides}
    response = client.post("/api/admin/providers", json=payload)
    assert response.status_code == 201, response.text
    rows = [p for p in response.json()["providers"] if p["name"] == payload["name"]]
    assert rows, f"新建的配置没出现在列表里：{payload['name']}"
    return rows[-1]["id"]


def _id_by_name(client) -> dict[str, str]:
    """名字 → id。名字在单个用例里唯一时比 `_create` 的返回值好用。"""
    return {p["name"]: p["id"] for p in _providers(client)["providers"]}


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
# 供应商 / 配置条目
# ---------------------------------------------------------------------------


def test_fresh_install_has_no_config_entries(client):
    """装完就是空的，且没有「使用中」——这不是异常，是还没配过。

    这条钉住 v19 + ADR-051 的核心变化：不再有 6 条内置播种行，
    也不再退回 `env:` 合成的降级项（env 里已经没有第二份模型配置）。
    列表为空时 `active_provider_id` 是空串，前端据此显示「还没有任何模型配置」。
    """
    _as_admin(client)
    data = _providers(client)

    assert data["providers"] == []
    assert data["active_provider_id"] == ""
    assert data["presets"], "预设模板仍在（降级为表单模板）"
    assert data["protocols"], "协议选项仍在"


def test_protocols_no_longer_carry_key_var(client):
    """协议不再附带「默认密钥变量名」——密钥由管理员在后台直接录入。

    ADR-053 之后 env 不存密钥，`key_env_var` 这个预填字段也随之消失；
    若它还在，前端会渲染一个没有意义、且填了也不生效的输入框。
    """
    from backend.core.providers import PROTOCOLS

    _as_admin(client)
    options = _providers(client)["protocols"]

    assert [o["value"] for o in options] == list(PROTOCOLS)
    for option in options:
        assert "key_env_var" not in option
        assert set(option) == {"value", "label"}


def test_create_without_key_is_not_configured(client):
    """不填密钥 = 未配置：key_present=False，且列表里没有任何密钥字段。"""
    _as_admin(client)
    _create(client, name="本地")

    row = _providers(client)["providers"][0]
    assert row["key_present"] is False
    assert "api_key_env_var" not in row
    assert "value_token" not in row


def test_create_with_key_encrypts_and_marks_present(client):
    """传入明文密钥 → 加密入库；响应与列表都只有 `key_present` 布尔。

    数据库里必须是密文（不等于明文），否则「加密入库」就名不副实。
    """
    _as_admin(client)
    provider_id = _create(client, name="带密钥", api_key="sk-abc-123")

    row = _providers(client)["providers"][0]
    assert row["key_present"] is True

    stored = ModelProviderRepository.get(provider_id)
    assert stored is not None
    assert stored["value_token"] and stored["value_token"] != "sk-abc-123"


def test_providers_never_leak_secrets(client):
    """响应里只能有 `key_present` 布尔，**绝不能出现密钥值**。"""
    _as_admin(client)
    _create(client, api_key="sk-super-secret-value")

    body = client.get("/api/admin/providers").text
    assert "sk-super-secret-value" not in body

    row = _providers(client)["providers"][0]
    assert row["key_present"] is True
    assert "value_token" not in row
    assert "builtin" not in row


def test_reveal_key_returns_plaintext_and_audits(client):
    """查看明文是单独动作，返回一次并写审计；列表里从不带明文。"""
    _as_admin(client)
    provider_id = _create(client, api_key="sk-reveal-me")

    response = client.post(f"/api/admin/providers/{provider_id}/reveal-key")
    assert response.status_code == 200, response.text
    assert response.json()["api_key"] == "sk-reveal-me"

    items = client.get("/api/admin/audit").json()["items"]
    assert any(i["action"] == "provider.reveal_key" for i in items)
    assert "sk-reveal-me" not in json.dumps(items, ensure_ascii=False)


def test_presets_route_is_not_swallowed_by_provider_id(client):
    """`/providers/presets` 必须注册在 `/providers/{provider_id}` 之前。

    顺序反了它会被当成一个配置 id 去查，然后 404——而错误信息会让人
    以为「预设这个功能没做」。
    """
    _as_admin(client)
    response = client.get("/api/admin/providers/presets")
    assert response.status_code == 200
    presets = response.json()
    assert len(presets) >= 6
    assert {"key", "protocol", "base_url"} <= set(presets[0])
    assert "api_key_env_var" not in presets[0]
    # `key` 只是下拉框的 value，**不是**配置 id
    assert "id" not in presets[0]


def test_create_provider_generates_id_and_allows_duplicate_names(client):
    """id 由后端生成；配置名允许重名。

    重名拦下来的话，管理员会被迫起名叫「方舟-2」这种谁也记不住的东西——
    而身份是 id，名字只是标签。
    """
    _as_admin(client)
    first = _create(client, name="方舟-生产")
    second = _create(client, name="方舟-生产")

    assert first.startswith("cfg_") and second.startswith("cfg_")
    assert first != second, "同一条协议建两条配置必须拿到两个不同的 id"
    assert ModelProviderRepository.get(first) is not None
    assert ModelProviderRepository.get(second) is not None


def test_create_provider_requires_name(client):
    """配置名必填：空名字在列表里就是一串 `cfg_xxx`，管理员无法辨认。"""
    _as_admin(client)
    response = client.post(
        "/api/admin/providers", json={"name": "", "protocol": "openai_compatible"}
    )
    assert response.status_code in (400, 422), response.text
    assert ModelProviderRepository.count() == 0

    # 只有空格也算空（`strip()` 之后为空）
    spaces = client.post(
        "/api/admin/providers", json={"name": "   ", "protocol": "openai_compatible"}
    )
    assert spaces.status_code == 400
    assert "配置名不能为空" in spaces.json()["detail"]


def test_create_provider_rejects_bad_protocol(client):
    _as_admin(client)
    bad_protocol = client.post(
        "/api/admin/providers", json={"name": "x1", "protocol": "no_such_protocol"}
    )
    assert bad_protocol.status_code == 400
    assert "不支持的协议" in bad_protocol.json()["detail"]
    assert ModelProviderRepository.count() == 0, "非法协议不该写进任何行"


def test_first_entry_becomes_in_use_automatically(client):
    """第一条配置自动成为「使用中」。

    为什么不要求再点一次「设为使用中」：此时它是唯一可选项，那一步只是仪式；
    而一旦忘了点，用户会得到「配好了却生成说没有配置」——排查还得先懂「指针」。
    """
    _as_admin(client)
    provider_id = _create(client, name="第一条")

    data = _providers(client)
    assert data["active_provider_id"] == provider_id
    assert [p["id"] for p in data["providers"] if p["active"]] == [provider_id]


def test_new_entry_does_not_steal_the_active_pointer(client):
    """新建第二条不抢「使用中」：否则改配置等于悄悄切换线上在跑的那条。"""
    _as_admin(client)
    first = _create(client, name="生产")
    second = _create(client, name="测试")

    data = _providers(client)
    assert second != first
    assert data["active_provider_id"] == first
    assert [p["id"] for p in data["providers"] if p["active"]] == [first]


def test_in_use_entry_cannot_be_deleted(client):
    """使用中的那条不可删：删完下次生成会全线失败。

    唯一拦截就是这个，没有「内置行」之分——所有条目一视同仁。
    """
    _as_admin(client)
    provider_id = _create(client)
    client.post(f"/api/admin/providers/{provider_id}/activate")

    response = client.delete(f"/api/admin/providers/{provider_id}")
    assert response.status_code == 403
    assert "使用中" in response.json()["detail"]
    assert ModelProviderRepository.get(provider_id) is not None


def test_other_entries_can_be_deleted(client):
    """非使用中的条目可以直删，删完列表里就没了。"""
    _as_admin(client)
    keep = _create(client, name="留下的")
    drop = _create(client, name="要删的")
    client.post(f"/api/admin/providers/{keep}/activate")

    response = client.delete(f"/api/admin/providers/{drop}")
    assert response.status_code == 200, response.text
    names = {p["name"] for p in response.json()["providers"]}
    assert names == {"留下的"}
    assert ModelProviderRepository.get(drop) is None


def test_disabled_in_use_entry_still_cannot_be_deleted(client):
    """「使用中」的判断必须看**原始指针**，不能看解析结果。

    停用之后解析会回退到另一条可用配置（这里一条都没有，所以解析结果是 `None`），
    于是「用解析结果判断」就会放行删除，库里留下一个指向已删行的悬空指针——
    此后每次读配置都刷一条 WARN，而后台「当前供应商」与列表对不上。
    """
    _as_admin(client)
    provider_id = _create(client)
    client.post(f"/api/admin/providers/{provider_id}/activate")
    client.patch(f"/api/admin/providers/{provider_id}", json={"enabled": False})

    # 前置校验：解析结果确实已经不是这一条了（否则本用例会「假绿」）
    rc.invalidate()
    resolved = rc.get_runtime_config().active_provider
    assert resolved is None or resolved.id != provider_id

    response = client.delete(f"/api/admin/providers/{provider_id}")
    assert response.status_code == 403
    assert ModelProviderRepository.get(provider_id) is not None


def test_activate_switches_and_warns_on_missing_key(client):
    """激活后真切换；缺密钥时给 warning 但**不拦**。

    拦住会让「先切过去、再配密钥」这个正常顺序走不通。
    """
    _as_admin(client)
    provider_id = _create(
        client,
        name="DeepSeek",
        protocol="openai_compatible",
        base_url="https://api.deepseek.com/v1",
        default_model="deepseek-chat",
    )

    response = client.post(f"/api/admin/providers/{provider_id}/activate")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["active_provider_id"] == provider_id
    assert "密钥" in data["warning"]

    config = client.get("/api/admin/config").json()
    assert config["active_provider_id"] == provider_id


def test_only_one_entry_is_active_at_a_time(client):
    """同一时刻只有一条生效——否则生成时不知道该走哪条。"""
    _as_admin(client)
    first = _create(client, name="甲")
    second = _create(client, name="乙")

    client.post(f"/api/admin/providers/{first}/activate")
    client.post(f"/api/admin/providers/{second}/activate")

    data = _providers(client)
    assert data["active_provider_id"] == second
    assert [p["id"] for p in data["providers"] if p["active"]] == [second]


def test_disabled_provider_cannot_be_activated(client):
    _as_admin(client)
    provider_id = _create(client)
    client.patch(f"/api/admin/providers/{provider_id}", json={"enabled": False})

    response = client.post(f"/api/admin/providers/{provider_id}/activate")
    assert response.status_code == 400
    assert "停用" in response.json()["detail"]


def test_provider_update_cannot_change_id(client):
    """`id` 不能通过 PATCH 改（它进任务回执与计量快照，改了历史就对不上）。"""
    _as_admin(client)
    provider_id = _create(client)

    response = client.patch(
        f"/api/admin/providers/{provider_id}", json={"id": "hacked", "name": "改过的名字"}
    )
    assert response.status_code == 200, response.text
    row = ModelProviderRepository.get(provider_id)
    assert row is not None and row["name"] == "改过的名字"
    assert ModelProviderRepository.get("hacked") is None


def test_provider_update_cannot_change_protocol(client):
    """协议创建后不可改：换协议等于换一家厂商，参数语义全变。

    ⚠️ 必须是**显式 400**，不能静默丢掉这个字段——静默丢的结果是
    「200 但没改」，比报错难查得多。
    """
    _as_admin(client)
    provider_id = _create(client, protocol="ark")

    changed = client.patch(
        f"/api/admin/providers/{provider_id}", json={"protocol": "openai_compatible"}
    )
    assert changed.status_code == 400
    assert "协议创建后不可修改" in changed.json()["detail"]
    assert ModelProviderRepository.get(provider_id)["protocol"] == "ark"

    # 原样回传同一个协议不算修改，不该被拦（否则「整对象提交」的表单永远保存不了）
    same = client.patch(f"/api/admin/providers/{provider_id}", json={"protocol": "ark"})
    assert same.status_code == 200, same.text


def test_unknown_provider_id_is_404(client):
    _as_admin(client)
    assert client.patch("/api/admin/providers/cfg_nope", json={"name": "x"}).status_code == 404
    assert client.delete("/api/admin/providers/cfg_nope").status_code == 404
    assert client.post("/api/admin/providers/cfg_nope/activate").status_code == 404


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------


def test_audit_records_config_and_provider_actions(client):
    """改配置与切供应商都要留痕，且**不含密钥值**。

    审计是给人翻的，不是保险箱：写进去的密钥等于给下一个读到它的人一份明文口令。
    """
    _as_admin(client)

    client.patch("/api/admin/config", json={"updates": {"llm_max_retries": 2}})
    provider_id = _create(
        client, name="我的", protocol="openai_compatible", api_key="sk-must-not-appear"
    )
    client.post(f"/api/admin/providers/{provider_id}/activate")

    items = client.get("/api/admin/audit").json()["items"]
    actions = {item["action"] for item in items}
    assert {"config.update", "provider.create", "provider.activate"} <= actions

    serialized = json.dumps(items, ensure_ascii=False)
    assert "sk-must-not-appear" not in serialized
    assert all(item["actor_name"] == "root" for item in items)
