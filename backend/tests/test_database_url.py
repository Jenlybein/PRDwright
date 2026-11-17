"""数据库地址解析（v8 / T18.9）与方言装配（#76 Postgres 接入）。

这一层要守的是「换库 = 改一个环境变量」这个承诺。四个必须先钉住的点：

1. **`DATABASE_URL` 优先，`DB_PATH` 是兼容糖**。兼容糖的意义是「已有部署零改动
   升级」——一旦它被当成并列选项，就会出现"两个都设了听谁的"这种没人能答的问题；
2. **不认识的 scheme 要立刻拒绝**。未适配的后端在第一条 SQL 上才会失败，而那条报错
   指向驱动或语法，看不出真因是"这个后端还没被支持"；
3. **URL 是「连哪个库」的完整表达**。文件路径只是它的一个特例，所以缓存键、
   日志、迁移地址都该以 URL 为准；
4. **连接级 SQL 必须按方言选**（#76 的真因）。以前非 SQLite 一律挂 MySQL 的
   `SET SESSION sql_mode`，Postgres 接上之后第一条连接就会在那句上失败——而报错
   出现在驱动里，看不出真因是"把别人的方言配置搬过来了"。
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
        database.build_engine("oracle+oracledb://u:p@h:1521/prdwright")
    message = str(caught.value)
    assert "oracle" in message
    assert "core/database.py" in message


def test_postgres_engine_is_accepted_with_network_pool_settings():
    """Postgres 是**已适配的后端**：建得出引擎，且按网络型后端装配池。

    不连库：`create_engine` 是懒的，建引擎不产生任何连接。这条守的是
    "配置层认得 Postgres"（`SUPPORTED_BACKENDS` 与池装配），不是"库能跑"——
    后者由 `test_postgres_integration.py` 在真库上证明。
    """
    engine = database.build_engine("postgresql+psycopg://u:p@127.0.0.1:5432/prdwright")
    try:
        assert engine.dialect.name == "postgresql"
        # 网络型后端：连接会掉线，必须 pre_ping + 定期换掉
        assert engine.pool._pre_ping is True  # noqa: SLF001 - 断言池的装配结果
        assert engine.pool._recycle == database.DEFAULT_POOL_RECYCLE  # noqa: SLF001
        # 固定池约定对任何后端都一样：运行期不许扩容
        assert engine.pool._max_overflow == 0  # noqa: SLF001
    finally:
        engine.dispose()


def _run_connect_hook(backend: str) -> list[str]:
    """跑一遍该方言的「连接建立后」钩子，把执行的 SQL 记下来。

    钩子的入参是 DBAPI 连接（用到 `cursor()` 与 `commit()`），所以可以用假连接跑，
    既不建真连接也看得见到底发了哪些语句。

    ⚠️ 假连接必须带 `commit()`：Postgres 的 `SET` 是**事务性**的，钩子里的配置
    不显式提交就会被池归还时的 rollback 撤掉（#77 在真库上实测过）。
    少了这个方法，这条用例会漏掉「设置了但没落住」这类故障。
    """
    executed: list[str] = []

    class _Cursor:
        def execute(self, sql: str) -> None:
            executed.append(sql)

        def close(self) -> None:
            pass

    class _Connection:
        def cursor(self) -> _Cursor:
            return _Cursor()

        def commit(self) -> None:
            executed.append("<commit>")

    database._connect_hook(backend)(_Connection(), None)
    return executed


@pytest.mark.parametrize(
    ("backend", "required", "forbidden"),
    [
        (
            "sqlite",
            ("PRAGMA journal_mode=WAL", "PRAGMA busy_timeout=5000", "PRAGMA foreign_keys=ON"),
            ("sql_mode", "TIME ZONE"),
        ),
        # Postgres 上**绝不能**出现 sql_mode / BEGIN IMMEDIATE / PRAGMA：
        # 那是别的方言的语法，执行它等于第一条连接就炸。
        ("postgresql", ("'UTC'",), ("sql_mode", "BEGIN IMMEDIATE", "PRAGMA")),
        ("mysql", ("SET time_zone = '+00:00'", "sql_mode"), ("BEGIN IMMEDIATE", "PRAGMA")),
        ("mariadb", ("SET time_zone = '+00:00'", "sql_mode"), ("BEGIN IMMEDIATE", "PRAGMA")),
    ],
)
def test_connection_setup_is_dialect_specific(backend, required, forbidden):
    """连接级 SQL 按方言选，不许把一套配置搬到另一套上。

    这是 #76 的**第一条硬约束**：非 SQLite 分支曾经无条件挂 MySQL 的
    `SET SESSION sql_mode`，Postgres 一进白名单，第一条连接就在那句上失败。
    """
    executed = _run_connect_hook(backend)
    joined = "\n".join(executed)
    for needle in required:
        assert needle.lower() in joined.lower(), f"{backend} 的连接配置缺 {needle}：{executed}"
    for needle in forbidden:
        assert needle.lower() not in joined.lower(), f"{backend} 的连接配置混进了 {needle}：{executed}"


def test_postgres_connect_hook_commits_the_session_setting():
    """PG 的 `SET` 是**事务性**的：钩子必须提交它，否则等于没设。

    池归还连接时会 `rollback()`，那条 `SET TIME ZONE 'UTC'` 就会被悄悄撤掉：
    **池里的第一条连接是 UTC，之后全是服务端时区**。这条钉住「设置了就要落住」。
    """
    executed = _run_connect_hook("postgresql")
    assert "<commit>" in executed, f"钩子没有提交会话配置（会被 rollback 撤掉）：{executed}"


def test_every_supported_backend_has_a_connect_hook():
    """白名单里的每个后端都必须有连接级配置钩子。

    这条是给下一个人准备的：往 `SUPPORTED_BACKENDS` 加后端时忘了加钩子，
    症状是 `build_engine` 在这一行抛 `KeyError`（而不是「这个后端没适配」），
    报错看起来像是字典写错了而不是配置缺了。
    """
    for backend in sorted(database.SUPPORTED_BACKENDS):
        assert callable(database._connect_hook(backend)), f"{backend} 没有连接级配置钩子"



def test_resolve_url_accepts_three_shapes(tmp_path):
    """路径、URL、以及"什么都不给"三种入参都要能归一。

    「什么都不给」的含义是**走应用配置**（`database_url()`），而不是某个写死的形态：
    #77 之后测试基座把配置指到 Postgres，这条不该因此变成红的。
    """
    assert database.resolve_url(None) == database.database_url()
    # 路径一律归一成 SQLite URL：`DB_PATH` 兼容糖与历史调用方都靠这个形状
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
        # `max_overflow=0` 是承重设计：运行期一条连接都不许新建
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
