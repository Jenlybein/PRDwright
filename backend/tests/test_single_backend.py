"""单一后端（v9 / #78）：只接受 Postgres，旧库拒绝启动。

这一组守的是票面 AC 的三条：

1. 仓库里没有 SQLite / MySQL 的方言分支（`SUPPORTED_BACKENDS` 只含 Postgres，
   未知方言在语句层直接报错）；
2. 迁移链只剩一份 Postgres 基线（另见 `test_architecture_guards.py::C10` 的静态守卫）；
3. **指向旧库启动时给出明确报错并拒绝提供服务**——这里在真启动路径（`main.lifespan`）
   上验证：错误类型、中文说明、以及「根本没有进入服务循环」。

第 3 条为什么要走 `TestClient` 而不是只测 `database_url()`：AC 的措辞是「拒绝提供服务」，
判据应当是**应用起不来**，而不是「某个 helper 抛了异常」。lifespan 的第一件事就是
`prepare_schema(get_engine())`，它会先撞上配置层，于是服务不会开始接请求。
"""

from __future__ import annotations

import pytest

from backend.core import database


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    from backend.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_supported_backends_has_only_postgres():
    """白名单只有 Postgres——「别的库进不来」是结构性的，不是文档上的承诺。"""
    assert database.SUPPORTED_BACKENDS == frozenset({"postgresql"})


@pytest.mark.parametrize(
    "url",
    [
        "sqlite+pysqlite:////var/lib/prdwright/prdwright.db",
        "mysql+pymysql://u:p@h:3306/prdwright",
        "mariadb+pymysql://u:p@h:3306/prdwright",
    ],
)
def test_build_engine_refuses_other_backends(monkeypatch, url):
    """建引擎这一步就拒绝：不让「未适配上」在第一条 SQL 上才暴露。"""
    monkeypatch.setenv("DATABASE_URL", url)
    with pytest.raises(database.UnsupportedBackendError):
        database.build_engine()


def test_startup_refuses_sqlite_and_gives_actionable_message(monkeypatch, tmp_path):
    """旧 SQLite 地址 → 应用**起不来**，且报错是给运维看的中文说明书。

    「拒绝启动」不是异常类型对就行：报错里必须能读到「只支持 Postgres」「下一步把
    DATABASE_URL 改成什么」，否则运维只知道自己挂了。
    """
    from fastapi.testclient import TestClient

    from backend.core.config import get_settings
    from backend.main import app

    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:////var/lib/prdwright/prdwright.db")
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    # autouse 夹具（isolated_db）在测试体之前已经建好一份配置缓存，这里必须清掉。
    get_settings.cache_clear()

    with pytest.raises(database.UnsupportedBackendError) as caught:
        # TestClient 的 `with` 才会跑 lifespan；服务循环根本没机会开始。
        with TestClient(app):
            pass

    message = str(caught.value)
    assert "sqlite" in message.lower(), "报错必须点名是哪个后端"
    assert "只支持 Postgres" in message
    assert "DATABASE_URL" in message
    assert "deploy" in message, "必须给出照着做的下一步"


def test_startup_refuses_missing_database_url(monkeypatch, tmp_path):
    """没配 DATABASE_URL 同样拒绝启动：本项目没有默认库。"""
    from fastapi.testclient import TestClient

    from backend.core.config import get_settings
    from backend.main import app

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()

    with pytest.raises(database.UnsupportedBackendError) as caught:
        with TestClient(app):
            pass
    assert "DATABASE_URL" in str(caught.value)


def test_legacy_sqlite_file_is_named_in_the_refusal(monkeypatch, tmp_path):
    """默认位置的旧库文件存在时，拒绝信息要指出「看见的是这个文件、但不再读取」。"""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DB_PATH", raising=False)
    legacy = tmp_path / "prdwright.db"
    legacy.write_bytes(b"SQLite format 3\x00")
    monkeypatch.setattr(database, "default_db_file", lambda: legacy)

    with pytest.raises(database.UnsupportedBackendError) as caught:
        database.database_url()
    message = str(caught.value)
    assert str(legacy) in message
    assert "不再读取" in message


def test_engine_singleton_refuses_before_caching_a_bad_url(monkeypatch):
    """`get_engine()` 遇到不支持的地址时**不能留下缓存**。

    缓存了坏地址的后果是：进程级单例指向一个从没连上的 Engine，后续即使配置改对了，
    快路径也会直接返回它——表现为「改完配置重新起还是报同一个错」，而配置看上去是对的。
    """
    from backend.core import engine as engine_module
    from backend.core.config import get_settings

    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///./prdwright.db")
    get_settings.cache_clear()

    with pytest.raises(database.UnsupportedBackendError):
        engine_module.get_engine()

    assert engine_module._engine is None  # noqa: SLF001 - 检测单例是否被污染
