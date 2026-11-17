"""数据库装配与「只接受 Postgres」（v9 / #78）。

这一层要守的是「单一后端」这个承诺。四个必须先钉住的点：

1. **只认 `DATABASE_URL`，且必须是 Postgres**。SQLite / MySQL 的地址、`DB_PATH` 兼容糖
   都在这一层被拒绝——不做兼容垫片是有意的，见 `core/database.py` 的模块说明；
2. **拒绝必须是可操作的**：报错里要写清「为什么」「下一步做什么」（改哪个环境变量、
   旧库怎么导出），否则下一个人只知道自己起不来；
3. **旧库痕迹要被认出来**（旧 `DB_PATH` / 旧 `prdwright.db` 文件），并明确说「不再读取」；
4. **连接级 SQL 只有一套**：Postgres 的 `SET TIME ZONE 'UTC'` 必须显式提交
   （`SET` 是事务性的，不提交会被池归还时的 rollback 撤掉）。
"""

from __future__ import annotations

import pytest

from backend.core import database


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """配置是 lru_cache 的，改环境变量后必须清一次，否则读到的还是上一组值。"""
    from backend.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_postgres_database_url_is_accepted(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:5432/prdwright"
    )
    assert (
        database.database_url() == "postgresql+psycopg://u:p@127.0.0.1:5432/prdwright"
    )
    assert database.dialect_name() == "postgresql"


@pytest.mark.parametrize(
    "url",
    [
        "sqlite+pysqlite:////var/lib/prdwright/prdwright.db",
        "sqlite:///./prdwright.db",
        "mysql+pymysql://u:p@h:3306/prdwright",
        "mariadb+pymysql://u:p@h:3306/prdwright",
        "oracle+oracledb://u:p@h:1521/prdwright",
    ],
)
def test_non_postgres_url_is_rejected_with_actionable_message(monkeypatch, url):
    """指向别的后端时**在配置阶段就拒绝**，而不是连上去再炸。

    报错必须同时说清三件事：哪个后端不被支持、为什么（单一后端）、下一步做什么
    （compose 里的 db 服务、DATABASE_URL 的形态）。少任何一条，读者只能去翻代码。
    """
    monkeypatch.setenv("DATABASE_URL", url)
    with pytest.raises(database.UnsupportedBackendError) as caught:
        database.database_url()
    message = str(caught.value)
    assert database.dialect_name(url) in message, "报错里必须点名是哪个后端"
    assert "Postgres" in message
    assert "DATABASE_URL" in message
    assert "deploy" in message or "compose" in message, "必须给出下一步怎么做"


def test_missing_database_url_is_rejected(monkeypatch):
    """没配 `DATABASE_URL` 时不许回落到某个默认 SQLite 文件。

    回落会让「配置漏了」伪装成「服务正常、只是数据不对」，这是最难查的一类故障。
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DB_PATH", raising=False)
    with pytest.raises(database.UnsupportedBackendError) as caught:
        database.database_url()
    message = str(caught.value)
    assert "DATABASE_URL" in message
    assert "只有 Postgres" in message or "只支持 Postgres" in message


def test_legacy_db_path_is_reported_as_old_sqlite(monkeypatch, tmp_path):
    """旧 `DB_PATH` 要被认出来并说明「不再读取」，而不是静默忽略。"""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    legacy = tmp_path / "prdwright.db"
    monkeypatch.setenv("DB_PATH", str(legacy))
    with pytest.raises(database.UnsupportedBackendError) as caught:
        database.database_url()
    message = str(caught.value)
    assert "DB_PATH" in message
    assert str(legacy) in message, "报错里要指出看见的是哪一个旧路径"


def test_legacy_sqlite_file_is_reported(monkeypatch, tmp_path):
    """默认位置的旧 SQLite 库文件存在时，要说清「检测到了、但不再读取」。"""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DB_PATH", raising=False)
    legacy = tmp_path / "prdwright.db"
    legacy.write_bytes(b"SQLite format 3\x00")
    # 替换「默认库文件在哪」这处依赖，不往仓库里写文件。
    monkeypatch.setattr(database, "default_db_file", lambda: legacy)

    with pytest.raises(database.UnsupportedBackendError) as caught:
        database.database_url()
    assert str(legacy) in str(caught.value), "出问题时必须指出看见的是哪一个旧文件"


def test_build_engine_rejects_non_postgres_target():
    """`build_engine` 的 target 同样只接受 Postgres URL。"""
    with pytest.raises(database.UnsupportedBackendError):
        database.build_engine("sqlite+pysqlite:///:memory:")
    with pytest.raises(database.UnsupportedBackendError):
        database.build_engine("mysql+pymysql://u:p@h:3306/prdwright")


def test_build_engine_rejects_bare_path(tmp_path):
    """按文件路径指库是 SQLite 的旧用法，必须报错而不是拼成 SQLite URL。"""
    with pytest.raises(database.UnsupportedBackendError) as caught:
        database.build_engine(tmp_path / "a.db")
    assert "URL" in str(caught.value)


def test_postgres_engine_is_accepted_with_network_pool_settings():
    """Postgres 引擎按网络型后端装配池。

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
        # 固定池约定：运行期不许扩容
        assert engine.pool._max_overflow == 0  # noqa: SLF001
    finally:
        engine.dispose()


def _run_connect_hook(backend: str) -> list[str]:
    """跑一遍该方言的「连接建立后」钩子，把执行的 SQL 记下来。

    钩子的入参是 DBAPI 连接（用到 `cursor()` 与 `commit()`），所以可以用假连接跑，
    既不建真连接也看得见到底发了哪些语句。

    ⚠️ 假连接必须带 `commit()`：Postgres 的 `SET` 是**事务性**的，钩子里的配置
    不显式提交就会被池归还时的 rollback 撤掉（#77 在真库上实测过）。
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


def test_connection_setup_is_postgres_only():
    """连接级 SQL 只有一套：PG 绝不能出现 sql_mode / BEGIN IMMEDIATE / PRAGMA。"""
    executed = _run_connect_hook("postgresql")
    joined = "\n".join(executed)
    assert "utc" in joined.lower()
    for needle in ("sql_mode", "BEGIN IMMEDIATE", "PRAGMA"):
        assert needle.lower() not in joined.lower(), f"PG 的连接配置混进了 {needle}：{executed}"


def test_every_supported_backend_has_a_connect_hook():
    """白名单里的每个后端都必须有连接级配置钩子。

    往 `SUPPORTED_BACKENDS` 加后端时忘了加钩子，症状是 `build_engine` 抛 `KeyError`，
    报错看起来像字典写错了而不是配置缺了。
    """
    for backend in sorted(database.SUPPORTED_BACKENDS):
        assert callable(database._connect_hook(backend)), f"{backend} 没有连接级配置钩子"


def test_postgres_connect_hook_commits_the_session_setting():
    """PG 的 `SET` 是**事务性**的：钩子必须提交它，否则等于没设。

    池归还连接时会 `rollback()`，那条 `SET TIME ZONE 'UTC'` 就会被悄悄撤掉：
    **池里的第一条连接是 UTC，之后全是服务端时区**。这条钉住「设置了就要落住」。
    """
    executed = _run_connect_hook("postgresql")
    assert "<commit>" in executed, f"钩子没有提交会话配置（会被 rollback 撤掉）：{executed}"


def test_supported_backends_is_postgres_only():
    """白名单只有 Postgres：不是「Postgres 能用」，而是「别的库进不来」。"""
    assert database.SUPPORTED_BACKENDS == frozenset({"postgresql"})


def test_resolve_url_accepts_url_and_none(monkeypatch):
    """URL 与「什么都不给」两种入参；文件路径不再是合法入参。"""
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:5432/prdwright"
    )
    assert database.resolve_url(None) == database.database_url()
    assert (
        database.resolve_url("postgresql+psycopg://u:p@127.0.0.1:5433/other")
        == "postgresql+psycopg://u:p@127.0.0.1:5433/other"
    )
