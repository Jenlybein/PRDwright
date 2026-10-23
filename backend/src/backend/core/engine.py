"""Engine 与 Session（v6 / T16.1）：连接与事务的唯一来源。

## 这一层取代了什么

从前是「每次操作新建一条连接」（`sqlite3.connect`），v5 的 T15 为了绕开
「多线程同时首次映射 WAL 的 `-shm` 会拿到 `SQLITE_READONLY`」这个竞态，
自己写了 444 行固定大小连接池。本模块把那件事交给 SQLAlchemy，并**保留同一条不变量**：
**连接只在启动期建满，运行期一条都不新建**。

竞态的成因是「建连接」这个动作本身，所以只要运行期不建连接，竞态就没有发生的时机。
`max_overflow=0` 是这条不变量的落地方式：池满时抛超时（可观测、可调 `DB_POOL_SIZE`），
而不是悄悄扩容把竞态请回来。

## 两种作用域

- `session()`：借一个 Session，**不自动提交**。给需要自己控制提交时机的调用方用。
- `session_scope()`：一个业务操作 = 一个事务。正常提交，异常回滚，无论如何关闭。
  **服务层用这个**；仓库层两者都不用（仓库不碰事务边界）。

## 两条纪律

1. `expire_on_commit=False`：服务层会拿着模型对象在事务外读属性。默认的
   `expire_on_commit=True` 会让提交后所有属性变过期，读一次就触发 SELECT，
   而 Session 已关闭 → `DetachedInstanceError`。
2. `autoflush=False`：flush 时机显式化（需要自增 id 时自己 `flush()`），
   避免「随便读一次就顺手把半成品写进库」。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool
# ⚠️ 必须用 SQLModel 的 `Session`，不能用 `sqlalchemy.orm.Session`：
# 仓库层要靠 `session.exec(select(...))` / `session.get(...)` 这套 SQLModel 扩展方法，
# 原生 Session 上没有 `exec`（只有 `execute`+`scalars`），绑定错了会在第一次查询时
# 抛 `AttributeError: 'Session' object has no attribute 'exec'`。
from sqlmodel import Session

from backend.core.config import get_settings
from backend.core.paths import default_db_file

logger = logging.getLogger(__name__)

# 借不到连接时最多等多久。**必须有界**：没有它，池被借空时请求会无限挂起，
# 表现是「接口没有响应、也没有报错」，比抛异常难查得多。
DEFAULT_POOL_TIMEOUT = 5.0

# 建连接的 DBAPI 超时（秒）。写冲突时先等，而不是立刻抛 `database is locked`。
DEFAULT_CONNECT_TIMEOUT = 10.0


def _on_connect(dbapi_connection: Any, _record: Any) -> None:
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


def build_engine(
    path: Path | str | None = None,
    *,
    size: int | None = None,
    pool_timeout: float = DEFAULT_POOL_TIMEOUT,
) -> Engine:
    """创建一个 Engine。**不建连接**（连接在第一次借用时才建）。

    Args:
        path: 数据库文件路径；缺省读当前配置。
        size: 连接池大小；缺省读 `DB_POOL_SIZE`。
        pool_timeout: 借不到连接时最多等多久（秒）。测试里调小，避免用例为了等超时
            白等 5 秒。

    Returns:
        配好 PRAGMA 事件与固定池的 Engine。
    """
    target = Path(path) if path is not None else default_db_file()
    pool_size = int(size if size is not None else get_settings().db_pool_size)

    engine = create_engine(
        f"sqlite+pysqlite:///{target}",
        # ⚠️ 固定池：`max_overflow=0` 不许运行期扩容。理由见模块 docstring。
        poolclass=QueuePool,
        pool_size=max(1, pool_size),
        max_overflow=0,
        pool_timeout=float(pool_timeout),
        # 连接可跨线程复用（同一时刻只属于一个借用者）。
        # SQLite 的 `timeout` 是写锁等待，与 `busy_timeout` 同源但作用于 DBAPI 层。
        connect_args={
            "timeout": DEFAULT_CONNECT_TIMEOUT,
            "check_same_thread": False,
        },
        future=True,
    )
    event.listen(engine, "connect", _on_connect)
    return engine


def warmup(engine: Engine, size: int | None = None) -> int:
    """在**启动期单线程**把池建满，之后运行期不再新建连接。

    SQLAlchemy 的连接池是懒建的（第一次借用才建）。如果放任它懒建，那么并发请求
    到来时就是「多线程同时建连接」——正是要避免的那个竞态。所以这里显式建满。

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
    logger.debug("连接池已预热：path=%s size=%d", engine.url.database, count)
    return len(held)


# ---------------------------------------------------------------------------
# 进程级单例：按「当前配置的库路径」取
# ---------------------------------------------------------------------------

_init_lock = threading.Lock()
_engine: Engine | None = None
_engine_path: Path | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """取当前库的 Engine；路径变了就 dispose 旧的再建一个新的。

    为什么按路径区分：`default_db_file()` 每次调用都读配置，所以路径可以在运行期变化
    （测试装置每个用例换一个临时库）。不比对路径的话，切库之后拿到的还是旧库的 Engine，
    症状是「新用例看见了上一个用例的数据」。

    快路径**不持锁**（每个操作都会走到这里）；只有「首次创建 / 换路径」才拿初始化锁。
    """
    global _engine, _engine_path, _session_factory

    target = default_db_file()
    current = _engine
    if current is not None and _engine_path == target:
        return current

    with _init_lock:
        # 双检：等锁期间可能已经有人建好了
        current = _engine
        if current is not None and _engine_path == target:
            return current
        if current is not None:
            current.dispose()
        fresh = build_engine(target)
        _engine = fresh
        _engine_path = target
        _session_factory = sessionmaker(
            bind=fresh,
            class_=Session,
            # ⚠️ 必须 False：见模块 docstring「两条纪律」
            expire_on_commit=False,
            autoflush=False,
        )
        return fresh


def get_session_factory() -> sessionmaker[Session]:
    """取当前 Engine 对应的 Session 工厂（没有 Engine 就先建一个）。"""
    get_engine()
    assert _session_factory is not None  # get_engine 里必然赋值
    return _session_factory


@contextmanager
def session() -> Iterator[Session]:
    """借一个 Session，**不自动提交**；退出时只关闭。

    给需要自己决定提交时机的调用方用（例如一个流程里要在两处显式提交）。
    绝大多数业务代码应该用 `session_scope()`。
    """
    factory = get_session_factory()
    db = factory()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """一个业务操作 = 一个事务：正常提交，异常回滚，无论如何关闭。

    这是**唯一**允许出现事务边界的地方（仓库层既不许 commit 也不许 rollback）。

    ⚠️ 不要在 `async def` 里跨 `await` 使用：块内持有一条池连接，去等 LLM 流式输出会把
    整条池子占住，表现是「接口全部超时」。守卫 C4 静态扫描这条。
    """
    factory = get_session_factory()
    db = factory()
    try:
        yield db
        db.commit()
    except BaseException:
        # 用 BaseException 而不是 Exception：`CancelledError` 不在 Exception 之下，
        # 漏掉它会让取消的请求把未提交事务带回池里。
        db.rollback()
        raise
    finally:
        db.close()


def current_engine() -> Engine | None:
    """当前已经建好的 Engine；**没建过就返回 None，不会顺手建一个**。

    `get_engine()` 会「没有就开一个」，所以不能拿它做收尾检查——那会让从未碰过数据库的
    用例也白开一池连接，并把「池里有没有泄漏」这件事变成恒真。
    """
    return _engine


def reset_engine() -> None:
    """销毁当前 Engine（测试装置 teardown / 换库前显式清理）。

    生产启动路径不需要它；测试里每个用例换库，靠它把上一份的连接**立刻**关掉，
    否则会在 Windows 上一直握着临时库的文件句柄。
    """
    global _engine, _engine_path, _session_factory
    with _init_lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _engine_path = None
        _session_factory = None


def engine_stats() -> dict[str, Any]:
    """连接池的当前状态（接性能面板）。

    与旧 `Database.stats` 的字段名保持一致，避免面板那边跟着改：
    `size` / `in_use` / `idle`。`wait_ms_*` 与 `replaced` 由 SQLAlchemy 内部维护，
    这里不再统计（要拿得靠 pool 事件，等面板真的需要再加）。
    """
    engine = _engine
    if engine is None:
        return {"size": 0, "in_use": 0, "idle": 0, "opened": False}
    pool = engine.pool
    size = int(pool.size())  # type: ignore[attr-defined]
    in_use = int(pool.checkedout())  # type: ignore[attr-defined]
    return {
        "size": size,
        "in_use": in_use,
        "idle": max(0, size - in_use),
        "opened": True,
    }
