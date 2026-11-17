"""数据库装配（v9）：全项目**唯一**决定「连哪个库、怎么连」的地方。

## 单一后端：只有 Postgres（#78）

本项目**只支持 Postgres**。SQLite 与 MySQL 家族的分支、迁移链、测试基座与部署面都已删除；
这不是「默认换成 Postgres」，而是「认不出 Postgres 就拒绝启动」。

为什么把「双后端」删掉而不是留着：两个后端意味着测试基座、迁移基线、就绪探针、备份链
各自分叉成两份，而两份里只有一份是每天都跑的那份——另一份会静默烂掉（#76 的实测就是
`_mysql_on_connect` 挂在「非 SQLite」分支上，Postgres 一进白名单第一条连接就炸）。
单后端的代价是「不能换库」，收益是「所有东西只有一份真相」。

## 只做三件事

1. 把配置变成一个 SQLAlchemy URL（`database_url`）；
2. 从 URL 判定方言（`dialect_name`）；
3. 按方言建引擎，并装上该方言的**连接级配置**（`build_engine`）。

**不做**：事务边界（`engine.session_scope`）、语句构造（`dialect.upsert`）、
结构演进（`schema.prepare_schema`）。这三件事各自有归宿；混进来就会变成 v6 删掉的
那个 444 行 `core/db.py`——「连哪个库」和「怎么用这个库」搅在一起，没人能说清
哪一步在负责什么。

## 旧库为什么必须被拒绝（票面 AC）

`DATABASE_URL` 指向 SQLite / MySQL，或者一个部署只留了旧的 `DB_PATH` / 旧的
`backend/prdwright.db`，都会被 `database_url()` / `build_engine()` 当场拒绝，
并给出**可操作的中文说明**。不做兼容垫片是有意的：SQLite 库的结构（布尔存成
INTEGER、`AUTOINCREMENT` 的自增语义、没有 `SERIAL` 序列）与 Postgres 基线的语义
并不一一对应，自动「搬迁」会把差异悄悄留在数据里；启动时明确失败，比运行期
「写不进去、读出来是默认值」容易定位得多。
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

# 建连接的 DBAPI 超时（秒）。Postgres 上是 `connect_timeout`（libpq 的选项）。
DEFAULT_CONNECT_TIMEOUT = 10.0

# 连接在池里最多活多久。必须短于服务端/网络中间层的掐断时间：PG 没有 MySQL 那个
# `wait_timeout` 参数，但连接同样会被网络设备、pgbouncer、NAT 超时单方面关掉，
# 而客户端池子里那条连接看起来还活着——下一次借用就抛
# `server closed the connection unexpectedly`，报错位置在业务代码里。
DEFAULT_POOL_RECYCLE = 1800

# 认得出来的后端。**只有 Postgres**（#78）。
SUPPORTED_BACKENDS = frozenset({"postgresql"})


class UnsupportedBackendError(ValueError):
    """当前配置指向一个本项目不支持的数据库。**这是拒绝启动的理由，不是警告。**

    继承 `ValueError`：它是「配置值不合法」这一类错误，调用方（以及既有用例）用
    `ValueError` 捕获就能拿到它；同时它有独立的类型名，便于在启动路径上把这种
    「需要人照着做」的失败与真正的编程错误区分开。
    """


# 旧 SQLite 库文件的默认位置（与 `Settings.db_file` 同口径）。只用于**拒绝时**
# 告诉人「我们看见的是哪一个文件」——它不会被读取，更不会被升级。
def _legacy_database_hint() -> str:
    """把「我们看到的旧库痕迹」改成一句人能照做的话（没有痕迹时给通用说明）。"""
    settings = get_settings()
    legacy = settings.db_path.strip()
    if legacy:
        return f"检测到旧配置 DB_PATH={legacy}（SQLite 文件路径）。"
    old_file = default_db_file()
    if old_file.exists():
        return (
            f"检测到旧的 SQLite 库文件 {old_file}（本项目自 v9 起不再读取它）。"
        )
    return "当前没有配置 DATABASE_URL。"


# 告诉人「下一步做什么」的那一段。拒绝启动时**必须**带上它：只说「不支持」而不说
# 怎么改，下一个人只能去翻代码。
_MIGRATION_GUIDANCE = (
    "本项目自 v9 起只支持 Postgres 单一后端，SQLite / MySQL 的方言分支、迁移链\n"
    "与部署面都已删除，旧库**不会被自动升级**。请按下面三步迁移：\n"
    "  1. 启动 Postgres（compose 部署已内置 `db` 服务：cd deploy && docker compose up -d db）；\n"
    "  2. 在 deploy/.env 里配置 DATABASE_URL，例如\n"
    "       DATABASE_URL=postgresql+psycopg://prdwright:<口令>@db:5432/prdwright\n"
    "  3. 用旧版代码导出数据，再按 deploy/README.md 的「备份与恢复」导入新库；\n"
    "     旧 SQLite 文件留作历史备份即可，新链路不再读取它。"
)


def database_url() -> str:
    """当前部署要连的库。

    只认 `DATABASE_URL`。它**必须**是 Postgres 地址；没配置、或配置指向别的后端时，
    直接抛 `UnsupportedBackendError`（带可操作说明），而不是回落到某个默认 SQLite 文件——
    回落会让「配置漏了」伪装成「服务正常，只是数据不对」。
    """
    configured = (get_settings().database_url or "").strip()
    if not configured:
        raise UnsupportedBackendError(
            f"{_legacy_database_hint()}\n"
            "DATABASE_URL 必须明确指向 Postgres，本项目没有默认库。\n"
            f"{_MIGRATION_GUIDANCE}"
        )
    backend = dialect_name(configured)
    if backend not in SUPPORTED_BACKENDS:
        raise UnsupportedBackendError(
            f"不支持的数据库后端 {backend!r}（DATABASE_URL 指向 {configured!r}）。\n"
            f"{_MIGRATION_GUIDANCE}"
        )
    return configured


def dialect_name(url: str | None = None) -> str:
    """URL 对应的后端名（本项目只会是 `postgresql`）。

    取 `make_url(...).get_backend_name()` 而不是切字符串：SQLAlchemy 已经处理了
    `postgresql+psycopg` 这类 `backend+driver` 形态。
    """
    return make_url(url or database_url()).get_backend_name()


def resolve_url(target: Path | str | None = None) -> str:
    """把「URL 或什么都不给」归一成 URL。

    只接受完整 URL 与 `None`（生产路径走 `database_url()`）。**不再接受文件路径**：
    那是 SQLite 的入参形态（`Path` / 不带 `://` 的字符串），留着它等于给旧形态开口子，
    症状是「按旧文档指一个 .db 路径，服务照常起来，但连的是一个被拒绝过的后端」。
    """
    if target is None:
        return database_url()
    text = str(target)
    if "://" not in text:
        raise UnsupportedBackendError(
            f"数据库地址不是合法 URL：{text!r}（按路径/文件名指库是 SQLite 的旧用法）。\n"
            f"{_MIGRATION_GUIDANCE}"
        )
    return text


# ---------------------------------------------------------------------------
# 连接级配置：唯一按方言分支的地方
# ---------------------------------------------------------------------------


def _postgresql_on_connect(dbapi_connection: Any, _record: Any) -> None:
    """Postgres 的连接级配置：**只**固定会话时区，并且必须**显式提交**。

    为什么只有一句：

    - 时区要对齐「按 UTC 存时间文本」的口径：PG 的会话时区默认取服务端
      （`SHOW timezone` 可能是 `Asia/Shanghai`），`now()` 会写进本地时间，
      而字段是文本、读出来看不出错了 8 小时。显式 `SET TIME ZONE 'UTC'` 把它钉住。
    - 不设 `sql_mode`：Postgres 没有这个东西。

    ⚠️ **`SET` 在 Postgres 里是事务性的**（#77 实测）：连接是 `autocommit=False`
    的，钩子里那句 `SET` 会开一个隐式事务，而连接归还给池时 SQLAlchemy 会
    `rollback()` —— 于是设置被悄悄撤掉：**池里的第一条连接是 UTC，之后全是服务端
    时区**。这不是报错，是「第一条之后悄悄换口径」，比一眼红难查得多。
    所以这里显式 `commit()`：钩子跑到时，SQLAlchemy 还没开始任何业务事务，
    提交这一下只落住这条会话配置本身。
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("SET TIME ZONE 'UTC'")
    finally:
        cursor.close()
    # 见上面 ⚠️：不提交就等于没设（池归还连接时的 rollback 会把它撤掉）。
    dbapi_connection.commit()


# 方言 → 连接级配置钩子。只有 Postgres 一条；新增后端要同时改 `SUPPORTED_BACKENDS`、
# 这里的钩子、`core/dialect.py` 的语句分支与 `migrations/`。
_CONNECT_HOOKS: dict[str, Any] = {"postgresql": _postgresql_on_connect}


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
        target: 完整数据库 URL；缺省读当前配置（见 `database_url`）。文件路径形态
            已被拒绝（那是 SQLite 的旧用法）。
        size: 连接池大小；缺省读 `DB_POOL_SIZE`。
        pool_timeout: 借不到连接时最多等多久（秒）。测试里调小，避免用例为了等超时
            白等 5 秒。

    Returns:
        配好连接级配置与固定池的 Engine。

    Raises:
        UnsupportedBackendError: URL 的后端不是 Postgres（或不是合法 URL）。这里
            **明确拒绝**而不是让它连上去再炸：未适配的后端会在第一条 SQL 上失败，
            而那条报错会指向驱动或语法，看不出真因是「这个后端不被支持」。
    """
    url = resolve_url(target)
    backend = dialect_name(url)
    if backend not in SUPPORTED_BACKENDS:
        raise UnsupportedBackendError(
            f"未支持的数据库后端 {backend!r}；本项目只支持 {sorted(SUPPORTED_BACKENDS)}。\n"
            f"{_MIGRATION_GUIDANCE}"
        )

    pool_size = int(size if size is not None else get_settings().db_pool_size)

    # ⚠️ 固定池：`max_overflow=0` 不许运行期扩容。
    # 对网络型后端是「池满即报错」的可观测性选择——悄悄扩容会把连接数问题推迟到
    # 服务端的 `max_connections` 上，那时更难查。
    common: dict[str, Any] = {
        "poolclass": QueuePool,
        "pool_size": max(1, pool_size),
        "max_overflow": 0,
        "pool_timeout": float(pool_timeout),
        "future": True,
    }

    # 网络型后端的连接会掉线：借用前先探活，并定期换掉（见模块顶部常量）。
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

    SQLAlchemy 的连接池是懒建的（第一次借用才建）。放任它懒建的话，并发请求到来时
    就是「多线程同时建连接」——对网络型后端就是「启动时看着正常、第一个并发请求
    才发现连不上」。所以这里显式建满。

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
    # 打印脱敏后的地址：这个返回值带密码，而启动日志会进轮转文件与 `app_logs` 表。
    shown = engine.url.render_as_string(hide_password=True)
    logger.debug("连接池已预热：url=%s size=%d", shown, count)
    return len(held)


__all__ = [
    "DEFAULT_CONNECT_TIMEOUT",
    "DEFAULT_POOL_RECYCLE",
    "DEFAULT_POOL_TIMEOUT",
    "SUPPORTED_BACKENDS",
    "UnsupportedBackendError",
    "build_engine",
    "database_url",
    "dialect_name",
    "resolve_url",
    "warmup",
]
