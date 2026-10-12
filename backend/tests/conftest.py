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


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """把数据库指向用例专属的临时文件，并在结束后还原运行时状态。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("INVITE_CODE", "test-invite")
    monkeypatch.setenv("INIT_ADMIN_USER", "")
    monkeypatch.setenv("INIT_ADMIN_PASSWORD", "")
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
