"""pytest 公共夹具。

认证相关的测试有一个硬性前提：**每个用例都要用独立的数据库**。
否则用例之间会互相看见对方注册的用户、核销掉的邀请码，测试结果随执行顺序漂移。

配置是 `lru_cache` 单例，所以这里不是「改环境变量」就完事——
必须同时 `cache_clear()`，让下一次 `get_settings()` 重新读取。
"""

from __future__ import annotations

import pytest

from backend.core.config import get_settings


def _reset_runtime_state() -> None:
    """清掉所有进程级缓存，避免上一个用例的状态渗进下一个用例。"""
    get_settings.cache_clear()

    # 凭据签发器也是懒加载单例，且缓存了 TTL，必须一起重置
    from backend.services import auth_service

    auth_service._issuer = None  # noqa: SLF001 - 测试里刻意重置模块级单例

    # 运行时配置有进程内快照（TTL 5 秒）与 `.env` 快照缓存。不清的话，
    # 「上一个用例往 system_config 写了覆盖项」会带进下一个用例——
    # 而 `DB_PATH` 每次都换，缓存里那份却指向旧库，症状是「查到的配置对不上」。
    from backend.core import runtime_config, secrets

    runtime_config.invalidate()
    secrets.reset_cache()


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """把数据库指向用例专属的临时文件，并在结束后还原运行时状态。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("INVITE_CODE", "test-invite")
    monkeypatch.setenv("INIT_ADMIN_USER", "")
    monkeypatch.setenv("INIT_ADMIN_PASSWORD", "")
    # 日志目录也要隔离：v4 起 lifespan 会把日志写进轮转文件，若不指到临时目录，
    # 每跑一次用例就会往仓库的 backend/logs/ 里塞一份，且用例之间互相覆盖。
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    _reset_runtime_state()

    # 建表（幂等）。放在这里而不是客户端夹具里，纯服务层测试也能直接用数据库。
    from backend.core.db import init_schema

    init_schema()

    yield

    _reset_runtime_state()


@pytest.fixture
def client(isolated_db):  # noqa: ARG001 - 依赖 autouse 夹具的执行顺序
    """带生命周期的测试客户端（lifespan 会建表并播种初始数据）。"""
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth_off_client(isolated_db, monkeypatch):  # noqa: ARG001
    """`AUTH_ENABLED=false` 下的测试客户端，用于验证开发模式全放行。"""
    monkeypatch.setenv("AUTH_ENABLED", "false")
    get_settings.cache_clear()

    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as test_client:
        yield test_client


def register(client, username="alice", password="password123", invite_code="test-invite"):
    """辅助函数：注册一个用户。"""
    return client.post(
        "/api/auth/register",
        json={"username": username, "password": password, "invite_code": invite_code},
    )


def login(client, username="alice", password="password123"):
    """辅助函数：登录。"""
    return client.post("/api/auth/login", json={"username": username, "password": password})


# ---------------------------------------------------------------------------
# 技能包：注册表与 PRD 技能层的用例都要在临时目录里「造一个包」
# ---------------------------------------------------------------------------

# PRD 技能包的资源清单（语义键 -> 包内相对路径），与 `skills/prd-generator/skill.yaml` 对齐
PRD_SKILL_ARTIFACTS = {
    "field_schema": "references/field-schema.json",
    "prd_template": "references/prd-template.md",
    "writing_rules": "references/writing-rules.md",
}


@pytest.fixture
def temp_skills_root(tmp_path, monkeypatch):
    """把技能根目录指到用例专属的临时目录，并在结束后清掉读盘缓存。

    全部技能相关的用例都走它：**不碰仓库里真实的 `skills/`**，这样用例既不受本地
    技能包内容变化影响，也不会在跑测试时读到别的用例留下的缓存。
    """
    from backend.services import skill_registry

    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setenv("PRDWRIGHT_SKILLS_DIR", str(root))
    skill_registry.clear_caches()
    yield root
    skill_registry.clear_caches()


def write_skill_package(
    skills_root,
    name: str,
    *,
    applies_to: tuple[str, ...] = ("prd",),
    version: str = "0.1.0",
    artifacts: dict[str, str] | None = None,
):
    """在临时 skills 根目录下造一个技能包，返回它的目录。

    它同时也是那份 `skill.yaml` 格式的可执行说明：**放这里而不是各用例文件里，
    是为了让「一个技能包长什么样」只有一处定义**。

    Args:
        skills_root: skills 根目录（即 `temp_skills_root` 夹具给出的路径）。
        name: 技能包目录名，同时也是 `name` 字段。
        applies_to: 声明的适用产物；传空元组则不写这个字段。
        version: 版本号。
        artifacts: `语义键 -> 包内相对路径`；缺省用 PRD 那三份。

    Returns:
        技能包目录的路径。
    """
    skill_dir = skills_root / name
    chosen = dict(PRD_SKILL_ARTIFACTS if artifacts is None else artifacts)
    for relative_path in chosen.values():
        target = skill_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"初始内容：{relative_path}\n", encoding="utf-8")

    lines = [f"name: {name}", f"version: {version}"]
    if applies_to:
        lines.append("applies_to:")
        lines.extend(f"  - {kind}" for kind in applies_to)
    lines.append("references:")
    for key, relative_path in chosen.items():
        lines.append(f"  - key: {key}")
        lines.append(f"    path: {relative_path}")
    (skill_dir / "skill.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return skill_dir


def write_longer(path, suffix: str) -> None:
    """改写文件并保证「修改时间 + 大小」两项都变化。

    单纯往同一长度改写内容时，字节数不变、某些文件系统上的时间戳精度也不足，
    那样会让「改了要立刻生效」这类用例验不到真正的重载路径。
    """
    path.write_text(
        f"{path.read_text(encoding='utf-8')}新增行：{suffix}\n" * 2, encoding="utf-8"
    )
