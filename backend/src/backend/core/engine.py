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
from typing import Any

from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
# ⚠️ 必须用 SQLModel 的 `Session`，不能用 `sqlalchemy.orm.Session`：
# 仓库层要靠 `session.exec(select(...))` / `session.get(...)` 这套 SQLModel 扩展方法，
# 原生 Session 上没有 `exec`（只有 `execute`+`scalars`），绑定错了会在第一次查询时
# 抛 `AttributeError: 'Session' object has no attribute 'exec'`。
from sqlmodel import Session

# 建引擎与连接级配置归 `core/database.py`（v8 / ADR-097）。
# 这里**再导出**是为了让 `from backend.core.engine import build_engine, warmup`
# 这类既有 import 一行都不用改——迁移期最重要的是"公开路径不变"。
from backend.core.database import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_POOL_TIMEOUT,
    build_engine,
    database_url,
    warmup,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 进程级单例：按「当前配置的库地址」取
# ---------------------------------------------------------------------------

_init_lock = threading.Lock()
_engine: Engine | None = None
_engine_url: str | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """取当前库的 Engine；地址变了就 dispose 旧的再建一个新的。

    为什么按**地址**而不是按文件路径区分：`database_url()` 每次调用都读配置，所以
    地址可以在运行期变化（测试装置每个用例换一个临时库；将来接 MySQL 之后也可能是
    别的形态）。不比对地址的话，切库之后拿到的还是旧库的 Engine，症状是
    「新用例看见了上一个用例的数据」。

    用完整 URL 做键而不是 `Path`：URL 是「连哪个库」的完整表达（含驱动与连接参数），
    文件路径只是它的一个特例。

    快路径**不持锁**（每个操作都会走到这里）；只有「首次创建 / 换地址」才拿初始化锁。
    """
    global _engine, _engine_url, _session_factory

    target = database_url()
    current = _engine
    if current is not None and _engine_url == target:
        return current

    with _init_lock:
        # 双检：等锁期间可能已经有人建好了
        current = _engine
        if current is not None and _engine_url == target:
            return current
        if current is not None:
            current.dispose()
        fresh = build_engine(target)
        _engine = fresh
        _engine_url = target
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
    global _engine, _engine_url, _session_factory
    with _init_lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _engine_url = None
        _session_factory = None


def engine_stats() -> dict[str, Any]:
    """连接池的当前状态（接性能面板）。

    ⚠️ **`idle` 不能用 `size - in_use` 算**：`QueuePool.size()` 返回的是
    `maxsize`（配置的池容量），**不是当前池里有多少条连接**——它恒等于
    `DB_POOL_SIZE`，与实际建了几条无关。用减法算出来的「空闲」会在「池是懒建的、
    只建了 1 条」时报告「4 条空闲」，这正好把「预热没做」这个退化盖住。

    现在 `idle` 取 `checkedin()`（队列里现成可用的条数）。于是：
    - 预热之后：`idle == size`（建满了且都归还）；
    - 没预热：`idle < size`，一眼能看出缺口。
    """
    engine = _engine
    if engine is None:
        return {"size": 0, "in_use": 0, "idle": 0, "opened": False}
    pool = engine.pool
    size = int(pool.size())  # type: ignore[attr-defined]  # = maxsize
    in_use = int(pool.checkedout())  # type: ignore[attr-defined]
    idle = int(pool.checkedin())  # type: ignore[attr-defined]  # 真的空闲条数
    return {
        "size": size,
        "in_use": in_use,
        "idle": idle,
        "opened": True,
    }
