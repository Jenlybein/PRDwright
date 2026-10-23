"""Engine 与 Session 的行为守卫（v6 / T16.1）。

六条判据，每条对应一个**静默失败模式**：

| 用例 | 不做会怎样 |
| --- | --- |
| PRAGMA 生效 | 外键不生效 → 级联删除静默不发生；WAL 没开 → 读写互相阻塞 |
| 池固定大小、满了就报错 | 池悄悄扩容 → 运行期建连接 → WAL `-shm` 竞态重现（偶发 `SQLITE_READONLY`） |
| 预热后（含并发）不再建连接 | 同上，且是**不可观测**的退化 |
| `lifespan` 真的调用了 `warmup` | 函数写好了却没接线——v7 之前的真实状态（ADR-084） |
| `session_scope` 提交 | 写操作全部丢失（而且不报错） |
| `session_scope` 异常回滚 | 半截写入留在库里 |
| `expire_on_commit=False` | 事务外读属性抛 `DetachedInstanceError` |
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlmodel import SQLModel

from backend.core import engine as engine_module


def test_pragmas_are_applied(isolated_db):
    """每条连接都带 WAL / busy_timeout / foreign_keys。"""
    from backend.core.engine import get_engine

    eng = get_engine()
    with eng.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000


def test_pool_is_fixed_size_and_fails_when_exhausted(tmp_path):
    """池满时抛超时，**不扩容**。

    `max_overflow=0` 是「运行期不建连接」这条不变量的落地方式。这条用例直接验证
    「借第 size+1 条会失败」——如果哪天有人把 `max_overflow` 调大，这里会转红。
    """
    eng = engine_module.build_engine(tmp_path / "pool.db", size=1, pool_timeout=0.05)
    try:
        first = eng.raw_connection()
        # 池只有 1 条且已被占住；再借必须等超时，而不是新建一条
        with pytest.raises(PoolTimeoutError):
            eng.raw_connection()
        first.close()
    finally:
        eng.dispose()


def test_warmup_builds_all_connections_before_serving(tmp_path):
    """`warmup()` 之后池里已有 size 条，且运行期不再新建连接。

    竞态的触发条件是「建连接」，所以这里同时断言两件事：
    预热后 `checkedout == 0`、`idle == size`；以及预热之后再借还，**连接创建计数不增加**。
    """
    eng = engine_module.build_engine(tmp_path / "warm.db", size=3)
    created = {"n": 0}

    @event.listens_for(eng, "connect")
    def _count(_dbapi_connection, _record):  # noqa: ANN001
        created["n"] += 1

    try:
        assert engine_module.warmup(eng, 3) == 3
        assert created["n"] == 3
        assert eng.pool.checkedout() == 0  # type: ignore[attr-defined]

        # 运行期借还 5 次：一条新连接都不许建
        for _ in range(5):
            with eng.connect() as conn:
                conn.execute(text("SELECT 1"))
        assert created["n"] == 3, "运行期新建了连接：预热不彻底，或池被允许扩容"
    finally:
        eng.dispose()


def test_warmup_prevents_concurrent_connection_creation(tmp_path):
    """预热之后，**并发**借连接也不新建。

    上一条是串行借还：它证明「池够用时不会新建」，但竞态的触发条件是
    「多个线程**同时**第一次借」。这条用线程池把那个瞬间造出来——
    预热漏做时，`created` 会从 2 涨上去。

    为什么这条比上一条重要：串行借还永远不会撞（同一时刻只有一个借用者），
    而本项目里真正并发的两个借用者是「事件循环线程」与「`log-store` 日志线程」。
    """
    eng = engine_module.build_engine(tmp_path / "warm-concurrent.db", size=2)
    created = {"n": 0}

    @event.listens_for(eng, "connect")
    def _count(_dbapi_connection, _record):  # noqa: ANN001
        created["n"] += 1

    def borrow(_: int) -> None:
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))

    try:
        assert engine_module.warmup(eng, 2) == 2
        assert created["n"] == 2

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(borrow, range(8)))

        assert created["n"] == 2, "并发借用时新建了连接：预热不彻底，或池被允许扩容"
        assert eng.pool.checkedout() == 0  # type: ignore[attr-defined]
    finally:
        eng.dispose()


def test_lifespan_warms_the_pool(client):  # noqa: ARG001 - 只需 client 触发 lifespan
    """真实启动路径（`lifespan`）必须把池建满。

    前两条测的是 `warmup()` **这个函数本身**；这条测的是**它有没有被调用**。
    没有这条，`main.py` 里漏掉那一行不会有任何用例转红——而这正是 v7 之前的状态：
    函数写好了、测试也有，唯独生产路径里没有它（ADR-084）。

    ⚠️ 判据必须取 `idle`（`pool.checkedin()`），**不能取 `size`**：
    `QueuePool.size()` 返回的是 `maxsize`（配置容量），恒等于 `DB_POOL_SIZE`，
    与实际建了几条无关。用它做断言，这条用例会在「完全没预热」时也是绿的——
    我第一版就是这么写的，反向验证时才发现它抓不住任何东西。
    """
    from backend.core.config import get_settings
    from backend.core.engine import engine_stats

    stats = engine_stats()
    assert stats["opened"] is True, "启动完成后 Engine 还没建，lifespan 走的路不对"
    assert stats["idle"] == get_settings().db_pool_size, (
        f"连接池没有建满：{stats}。多半是 lifespan 里漏了 warmup()"
    )
    assert stats["in_use"] == 0, f"启动完成后还有连接没归还：{stats}"


def test_session_scope_commits(isolated_db):
    """正常退出即提交：换一个 Session 能读到刚写的行。"""
    from sqlmodel import select

    from backend.core.engine import session, session_scope
    from backend.core.models import User

    with session_scope() as db:
        db.add(
            User(username="commit-u", password_hash="h", salt=b"s", created_at="2026-01-01")
        )

    # ⚠️ 用全新的 Session 读：在同一个 Session 里读，未提交也能看到（identity map）
    with session() as db:
        assert db.exec(select(User).where(User.username == "commit-u")).first() is not None


def test_session_scope_rolls_back_on_exception(isolated_db):
    """异常退出即回滚：一行都不落。"""
    from sqlmodel import select

    from backend.core.engine import session, session_scope
    from backend.core.models import User

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom):
        with session_scope() as db:
            db.add(
                User(username="rollback-u", password_hash="h", salt=b"s", created_at="2026-01-01")
            )
            db.flush()
            raise Boom

    with session() as db:
        assert db.exec(select(User).where(User.username == "rollback-u")).first() is None


def test_expire_on_commit_is_disabled(isolated_db):
    """提交之后属性仍可读（不触发 SELECT、不抛 `DetachedInstanceError`）。

    服务层会拿着模型对象在事务外组装响应。若 `expire_on_commit=True`，
    提交后读任意属性都会尝试重新查库，而那是事务外——直接抛异常。
    """
    from backend.core.engine import session_scope
    from backend.core.models import User

    with session_scope() as db:
        user = User(username="expire-u", password_hash="h", salt=b"s", created_at="2026-01-01")
        db.add(user)
        db.flush()
        db.commit()
        # 提交之后、关闭之前读：必须不报错
        assert user.username == "expire-u"
    # 关闭之后读已加载的属性：同样必须不报错
    assert user.username == "expire-u"


def test_reset_engine_follows_path_change(isolated_db, tmp_path, monkeypatch):
    """换库路径之后，`get_engine()` 必须换到新库（不能还连着旧的）。"""
    from backend.core.engine import get_engine, reset_engine

    first = get_engine()
    other = tmp_path / "other.db"
    monkeypatch.setenv("DB_PATH", str(other))
    from backend.core.config import get_settings

    get_settings.cache_clear()

    reset_engine()
    second = get_engine()
    assert second is not first
    # 用 `.database` 而不是 `str(url)`：URL 会把 Windows 路径做百分号转义，字符串比对会假红
    assert second.url.database == str(other)
