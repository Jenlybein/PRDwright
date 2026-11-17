"""数据库装配（v8）：全项目**唯一**决定「连哪个库、怎么连」的地方。

## 只做三件事

1. 把配置变成一个 SQLAlchemy URL（`database_url`）；
2. 从 URL 判定方言（`dialect_name`）；
3. 按方言建引擎，并装上该方言的**连接级配置**（`build_engine`）。

**不做**：事务边界（`engine.session_scope`）、语句构造（`dialect.upsert`）、
结构演进（`schema.prepare_schema`）。这三件事各自有归宿；混进来就会变成 v6 删掉的
那个 444 行 `core/db.py`——「连哪个库」和「怎么用这个库」搅在一起，没人能说清
哪一步在负责什么。

## 为什么连接级配置只能在这里

`PRAGMA journal_mode=WAL` 和 `SET time_zone='+00:00'` 之间没有共同抽象：它们是
「连接建立之后立刻执行的一段 SQL」。任何 ORM 都表达不了这种「连接生命周期钩子」，
所以它必须是方言分支，而且**必须只有这一处分支**。

## 为什么要 `pool_pre_ping` 与 `pool_recycle`（仅网络型后端）

SQLite 的连接是本地文件句柄，不会「掉线」。MySQL / Postgres 的连接是网络资源：
服务端 `wait_timeout`（默认 8 小时）到点会单方面关闭，而客户端池子里那条连接看起来
还活着——下一次借用就会抛 `MySQL server has gone away` / `server closed the
connection unexpectedly`，而且报错位置在业务代码里，看起来像是某条 SQL 写错了。
`pool_pre_ping` 在借用时先探一下，`pool_recycle` 主动在超时前换掉，两道都上。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.pool import QueuePool

from backend.core.config import get_settings
from backend.core.paths import default_db_file

logger = logging.getLogger(__name__)

# 借不到连接时最多等多久。**必须有界**：没有它，池被借空时请求会无限挂起，
# 表现是「接口没有响应、也没有报错」，比抛异常难查得多。
DEFAULT_POOL_TIMEOUT = 5.0

# 建连接的 DBAPI 超时（秒）。SQLite 下是写锁等待（与 `busy_timeout` 同源），
# 网络型后端下由驱动的 `connect_timeout` 承担。
DEFAULT_CONNECT_TIMEOUT = 10.0

# 网络型后端（MySQL / Postgres）的连接在池里最多活多久。
# 必须短于服务端 `wait_timeout`（MySQL 默认 28800 秒；PG 没有这个参数，
# 但连接同样会被网络设备 / 中间层掐掉，定期换掉是通行的做法）。
DEFAULT_POOL_RECYCLE = 1800

# 认得出来的后端。**加新后端必须同时改这里和本节顶部的模块清单**：
# 否则就是「代码里多了一个库，但没人知道它归谁管」。
# ⚠️ 放进这里只代表「引擎装得对、连接级配置对、迁移与结构快照能跑」，
# 不代表「所有测试用例都在这个库上跑过」——那是换测试基座那张票（#77）的事。
SUPPORTED_BACKENDS = frozenset({"sqlite", "postgresql", "mysql", "mariadb"})

# `sqlite+pysqlite` 之外的写法也常见（`sqlite:///...`），统一归一成前者，
# 保证 Engine 的 DBAPI 明确——`sqlite://` 在某些环境下会解析到别的驱动。
_SQLITE_SCHEME = "sqlite+pysqlite"


# ---------------------------------------------------------------------------
# URL 解析
# ---------------------------------------------------------------------------


def database_url() -> str:
    """当前部署要连的库。

    优先级（**顺序是契约，别调**）：

    1. `DATABASE_URL` —— 正式配置，任何后端都用它；
    2. `DB_PATH` —— 兼容糖：历史部署与测试装置只设了它，本期不破坏；
    3. 默认文件 —— `backend/prdwright.db`。

    第 2 条存在的意义是「让已有部署零改动地升级」。它会在启动日志里被标注为
    「正在使用兼容配置 DB_PATH」，提醒运维换成 `DATABASE_URL`。
    """
    configured = (get_settings().database_url or "").strip()
    if configured:
        return configured
    return f"{_SQLITE_SCHEME}:///{default_db_file()}"


def dialect_name(url: str | None = None) -> str:
    """URL 对应的后端名（`sqlite` / `postgresql` / `mysql` / `mariadb`）。

    取 `make_url(...).get_backend_name()` 而不是切字符串：SQLAlchemy 已经处理了
    `mysql+pymysql`、`sqlite+pysqlite`、`postgresql+psycopg` 这类 `backend+driver` 形态。
    """
    return make_url(url or database_url()).get_backend_name()


def resolve_url(target: Path | str | None = None) -> str:
    """把「路径、URL、或什么都不给」归一成 URL。

    三种入参都要支持，因为调用方是三种：

    - `None`：生产路径，走 `database_url()`；
    - `Path`：测试装置指向临时文件；
    - `str`：可能是 URL（含 `://`），也可能是旧式路径字符串。

    只看有没有 `://` 来区分路径与 URL 是够的：Windows 盘符（`C:\\...`）不含它，
    而任何合法 SQLAlchemy URL 都必须有。
    """
    if target is None:
        return database_url()
    if isinstance(target, Path):
        return f"{_SQLITE_SCHEME}:///{target}"
    text = str(target)
    return text if "://" in text else f"{_SQLITE_SCHEME}:///{text}"


def _ensure_sqlite_dir(url: str) -> None:
    """SQLite 的库文件目录必须先存在，否则 `create_engine` 会在连接时报
    `unable to open database file`——而那条报错看不出是「目录没建」。

    对 `:memory:` 与非 SQLite 后端不做任何事：前者没有文件，后者由服务端管目录。
    """
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite" or not parsed.database:
        return
    if parsed.database == ":memory:":
        return
    Path(parsed.database).expanduser().parent.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 连接级配置：唯一按方言分支的地方
# ---------------------------------------------------------------------------


def _sqlite_on_connect(dbapi_connection: Any, _record: Any) -> None:
    """每条新连接建立后立刻设置三条 PRAGMA。

    做成事件而不是「在某个函数里记得加」：连接可能在池扩容、故障替换等路径上产生，
    「每条都记得设」是不可靠的，而事件是结构性的。

    - `journal_mode=WAL`：读写不再互斥；
    - `busy_timeout=5000`：写冲突时等 5 秒（**只对 `SQLITE_BUSY` 有效**，
      `SQLITE_READONLY` 不在其列——那正是要靠在启动期建满连接来避免的那种错误）；
    - `foreign_keys=ON`：SQLite 默认关闭外键，不显式打开则级联删除不会发生。
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def _mysql_on_connect(dbapi_connection: Any, _record: Any) -> None:
    """MySQL 的连接级配置。

    两条都是**为了与 SQLite 行为对齐**：

    - `time_zone='+00:00'`：本项目按 UTC 存时间文本。不设的话会话时区
      跟随服务端，一旦有人把服务端设成 `+08:00`，`NOW()` 之类的函数就会写进本地时间，
      而读出来仍是"看起来像 UTC"的字符串——错 8 小时且不报错。
    - `sql_mode` 含 `STRICT_TRANS_TABLES`：让「列超长、非空缺失」直接报错，而不是
      静默截断。MySQL 8.0 默认已含，这里显式声明是为了不受服务端配置影响——
      我们宁可在写入时失败，也不要存进半条数据。

    ⚠️ 不设 `AUTOCOMMIT`、不设隔离级别：事务语义归 `session_scope()`，这里只配置
    连接本身。
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("SET time_zone = '+00:00'")
        cursor.execute("SET SESSION sql_mode = 'STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION'")
    finally:
        cursor.close()


def _postgresql_on_connect(dbapi_connection: Any, _record: Any) -> None:
    """Postgres 的连接级配置：**只**固定会话时区，并且必须**显式提交**。

    为什么只有一句：

    - 时区同样要对齐 SQLite 的「按 UTC 存时间文本」口径：PG 的会话时区默认取
      服务端（`SHOW timezone` 可能给 `Asia/Shanghai`），`now()` 会写进本地时间，
      而字段是文本、读出来看不出错了 8 小时。显式 `SET TIME ZONE 'UTC'` 把它钉住。
    - **不设 `sql_mode`**：Postgres 没有这个东西。这正是本文件历史上最危险的一处
      假设——非 SQLite 分支无条件挂 MySQL 的钩子，Postgres 接上之后第一条连接就会
      在那句 `SET SESSION sql_mode` 上报语法错误，而报错长在驱动里、看不出真因。
      所以连接级配置只能按方言取（见 `_CONNECT_HOOKS`）。

    ⚠️ **`SET` 在 Postgres 里是事务性的**（#77 实测）：连接是 `autocommit=False`
    的，钩子里那句 `SET` 会开一个隐式事务，而连接归还给池时 SQLAlchemy 会
    `rollback()` —— 于是设置被悄悄撤掉：**池里的第一条连接是 UTC，之后全是服务端
    时区**。这不是报错，是「第一条之后悄悄换口径」，比一眼红难查得多。
    所以这里显式 `commit()`：钩子跑到时，SQLAlchemy 还没开始任何业务事务，
    提交这一下只落住这条会话配置本身。

    也有人会把这条配置放进 `connect_args` 的 libpq `options`（`-c timezone=UTC`），
    同样有效；留在钩子里是因为本文件的纪律是「连接级配置只有这一处」。
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("SET TIME ZONE 'UTC'")
    finally:
        cursor.close()
    # 见上面 ⚠️：不提交就等于没设（池归还连接时的 rollback 会把它撤掉）。
    dbapi_connection.commit()


# 方言 → 连接级配置钩子。**新增后端就在这张表上加一行**：
# 以前这里默认「非 SQLite 一律按 MySQL 配」，那等于把 MySQL 的方言细节当成了
# 所有网络库的公共约定。
_CONNECT_HOOKS: dict[str, Any] = {
    "sqlite": _sqlite_on_connect,
    "postgresql": _postgresql_on_connect,
    "mysql": _mysql_on_connect,
    "mariadb": _mysql_on_connect,
}


def _connect_hook(backend: str) -> Any:
    """该方言的「连接建立后」钩子（`SUPPORTED_BACKENDS` 里的每个后端都有）。"""
    return _CONNECT_HOOKS[backend]


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------


def build_engine(
    target: Path | str | None = None,
    *,
    size: int | None = None,
    pool_timeout: float = DEFAULT_POOL_TIMEOUT,
) -> Engine:
    """创建一个 Engine。**不建连接**（连接在第一次借用时才建）。

    Args:
        target: 库文件路径或完整 URL；缺省读当前配置（见 `database_url`）。
        size: 连接池大小；缺省读 `DB_POOL_SIZE`。
        pool_timeout: 借不到连接时最多等多久（秒）。测试里调小，避免用例为了等超时
            白等 5 秒。

    Returns:
        配好连接级配置与固定池的 Engine。

    Raises:
        ValueError: URL 的后端不在 `SUPPORTED_BACKENDS` 里。这里**明确拒绝**而不是
            让它连上去再炸：未适配的后端在第一条 SQL 上就会失败，而那条报错会指向
            驱动或语法，看不出真因是「这个后端还没被支持」。
    """
    url = resolve_url(target)
    backend = dialect_name(url)
    if backend not in SUPPORTED_BACKENDS:
        raise ValueError(
            f"未适配的数据库后端 {backend!r}；当前支持 {sorted(SUPPORTED_BACKENDS)}。"
            "新增后端请改 core/database.py（连接配置）与 core/dialect.py（语句构造）。"
        )

    pool_size = int(size if size is not None else get_settings().db_pool_size)
    _ensure_sqlite_dir(url)

    # ⚠️ 固定池：`max_overflow=0` 不许运行期扩容。
    # 对 SQLite 是承重设计（躲 WAL `-shm` 的首次映射竞态）；
    # 对网络型后端是"池满即报错"的可观测性选择——悄悄扩容会把连接数问题推迟到
    # 服务端的 `max_connections` 上，那时更难查。
    common: dict[str, Any] = {
        "poolclass": QueuePool,
        "pool_size": max(1, pool_size),
        "max_overflow": 0,
        "pool_timeout": float(pool_timeout),
        "future": True,
    }

    if backend == "sqlite":
        engine = create_engine(
            url,
            # 连接可跨线程复用（同一时刻只属于一个借用者）。
            # SQLite 的 `timeout` 是写锁等待，与 `busy_timeout` 同源但作用于 DBAPI 层。
            connect_args={
                "timeout": DEFAULT_CONNECT_TIMEOUT,
                "check_same_thread": False,
            },
            **common,
        )
        event.listen(engine, "connect", _connect_hook(backend))
        return engine

    # 网络型后端（Postgres / MySQL 家族）：池的装配相同（连接会掉线，要 pre_ping +
    # 定期换掉），**差异只在连接级配置**——按方言取钩子，不是一律挂 MySQL 的那一套。
    engine = create_engine(
        url,
        connect_args={"connect_timeout": int(DEFAULT_CONNECT_TIMEOUT)},
        pool_pre_ping=True,
        pool_recycle=DEFAULT_POOL_RECYCLE,
        **common,
    )
    event.listen(engine, "connect", _connect_hook(backend))
    return engine


def warmup(engine: Engine, size: int | None = None) -> int:
    """在**启动期单线程**把池建满，之后运行期不再新建连接。

    SQLAlchemy 的连接池是懒建的（第一次借用才建）。如果放任它懒建，那么并发请求
    到来时就是「多线程同时建连接」——对 SQLite 正是要避免的那个竞态
    （多线程同时首次映射 WAL 的 `-shm` 会拿到 `SQLITE_READONLY`）；对 MySQL 则是
    「启动时看着正常、第一个并发请求才发现连不上」。
    所以这里显式建满。

    Args:
        engine: 目标 Engine。
        size: 建几条；缺省读 `DB_POOL_SIZE`。

    Returns:
        实际建好的连接数。
    """
    count = int(size if size is not None else get_settings().db_pool_size)
    held = [engine.raw_connection() for _ in range(max(1, count))]
    for connection in held:
        connection.close()
    # 打印脱敏后的地址：这个返回值可能带密码（换到 MySQL 之后就是常态），
    # 而启动日志会进轮转文件与 `app_logs` 表。
    shown = engine.url.render_as_string(hide_password=True)
    logger.debug("连接池已预热：url=%s size=%d", shown, count)
    return len(held)


__all__ = [
    "DEFAULT_CONNECT_TIMEOUT",
    "DEFAULT_POOL_RECYCLE",
    "DEFAULT_POOL_TIMEOUT",
    "SUPPORTED_BACKENDS",
    "build_engine",
    "database_url",
    "dialect_name",
    "resolve_url",
    "warmup",
]
