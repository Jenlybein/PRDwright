"""数据库地址解析（v8 / T18.9 / ADR-097）。

这一层要守的是「换库 = 改一个环境变量」这个承诺。三个必须先钉住的点：

1. **`DATABASE_URL` 优先，`DB_PATH` 是兼容糖**。兼容糖的意义是「已有部署零改动
   升级」——一旦它被当成并列选项，就会出现"两个都设了听谁的"这种没人能答的问题；
2. **不认识的 scheme 要立刻拒绝**。未适配的后端在第一条 SQL 上才会失败，而那条报错
   指向驱动或语法，看不出真因是"这个后端还没被支持"；
3. **URL 是「连哪个库」的完整表达**。文件路径只是它的一个特例，所以缓存键、
   日志、迁移地址都该以 URL 为准。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.core import database


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """配置是 lru_cache 的，改环境变量后必须清一次，否则读到的还是上一组值。"""
    from backend.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_database_url_wins_over_db_path(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "mysql+pymysql://u:p@h:3306/prdwright")
    monkeypatch.setenv("DB_PATH", str(Path("/tmp/ignored.db")))
    assert database.database_url() == "mysql+pymysql://u:p@h:3306/prdwright"


def test_db_path_is_compat_sugar(monkeypatch, tmp_path):
    """只设 DB_PATH 时，产出的仍是 SQLite URL——老部署与测试装置都靠这条。"""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    target = tmp_path / "compat.db"
    monkeypatch.setenv("DB_PATH", str(target))

    url = database.database_url()
    assert url.startswith("sqlite+pysqlite:///")
    assert url.endswith("compat.db")
    assert database.dialect_name(url) == "sqlite"


def test_default_when_nothing_configured(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DB_PATH", raising=False)
    assert database.database_url().startswith("sqlite+pysqlite:///")
    assert database.database_url().endswith("prdwright.db")


def test_unknown_backend_is_rejected_at_build_time():
    """未适配的后端在**建引擎**时就被拒，而不是等第一条 SQL 失败。

    报错信息必须点明"该改哪两个文件"，否则下一个人只会知道"连不上"。
    """
    with pytest.raises(ValueError) as caught:
        database.build_engine("postgresql://u:p@h:5432/prdwright")
    message = str(caught.value)
    assert "postgresql" in message
    assert "core/database.py" in message


def test_resolve_url_accepts_three_shapes(tmp_path):
    """路径、URL、以及"什么都不给"三种入参都要能归一。"""
    assert database.resolve_url(None).startswith("sqlite+pysqlite:///")
    assert database.resolve_url(tmp_path / "a.db").startswith("sqlite+pysqlite:///")
    assert database.resolve_url(str(tmp_path / "b.db")).startswith("sqlite+pysqlite:///")
    # 带 `://` 的按 URL 处理，不拼 sqlite 前缀
    assert database.resolve_url("mysql+pymysql://u:p@h/db") == "mysql+pymysql://u:p@h/db"


def test_mysql_engine_gets_pool_pre_ping_and_prefixed_scheme():
    """MySQL 引擎要带 `pool_pre_ping`（防 `server has gone away`）且能建出来。

    不连库：`create_engine` 是懒的，建引擎不产生任何网络连接。这条守的是
    "配置层认得 MySQL"，而不是"MySQL 能跑"——那是 P2~P5 的事。
    """
    engine = database.build_engine("mysql+pymysql://u:p@127.0.0.1:3306/prdwright")
    try:
        assert engine.dialect.name == "mysql"
        assert engine.pool._pre_ping is True  # noqa: SLF001 - 断言池的装配结果
    finally:
        engine.dispose()


def test_sqlite_engine_keeps_fixed_pool_and_no_pre_ping():
    """SQLite 不该带 `pool_pre_ping`：本地文件句柄不会掉线，多探一次只是多一条 SQL。"""
    engine = database.build_engine("sqlite+pysqlite:///:memory:")
    try:
        assert engine.dialect.name == "sqlite"
        assert engine.pool._pre_ping is False  # noqa: SLF001
        # `max_overflow=0` 是承重设计（ADR-084）：运行期一条连接都不许新建
        assert engine.pool._max_overflow == 0  # noqa: SLF001
    finally:
        engine.dispose()


def test_sqlite_url_creates_parent_directory(tmp_path):
    """库文件所在目录不存在时，建引擎这一步就要把它建出来。

    否则报错是 `unable to open database file`，看不出真因是"目录没建"。
    """
    nested = tmp_path / "deep" / "dir" / "app.db"
    engine = database.build_engine(f"sqlite+pysqlite:///{nested}")
    try:
        assert nested.parent.is_dir()
    finally:
        engine.dispose()
