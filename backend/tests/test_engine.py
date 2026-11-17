"""Engine 与 Session 的行为守卫（v6 / T16.1；#77 起跑在 Postgres 上）。

六条判据，每条对应一个**静默失败模式**：

| 用例 | 不做会怎样 |
| --- | --- |
| 连接级配置生效（会话时区钉在 UTC） | 库里是 UTC 文本、会话却按服务端时区写，时间错 8 小时还不报错 |
| 池固定大小、满了就报错 | 池悄悄扩容 → 运行期建连接 → 连接数问题被推到服务端 `max_connections`，更难查 |
| 预热后（含并发）不再建连接 | 同上，且是**不可观测**的退化 |
| `lifespan` 真的调用了 `warmup` | 函数写好了却没接线——v7 之前的真实状态 |
| `session_scope` 提交 | 写操作全部丢失（而且不报错） |
| `session_scope` 异常回滚 | 半截写入留在库里 |
| `expire_on_commit=False` | 事务外读属性抛 `DetachedInstanceError` |

⚠️ #77 把这条文件从 SQLite 搬到 Postgres 时，**只换了表达手段**：
「外键强开 / WAL / busy_timeout」三条 PRAGMA 在 Postgres 上没有对应物——外键恒开、
读写不互斥由 MVCC 保证、写冲突由行锁 + 死锁检测处理，它们是**引擎自带**的而不是
「有人记得配」的。所以那条用例改成钉住 Postgres 侧真实存在的那一项连接级配置
（会话时区）；池 / 预热 / 事务边界 / `expire_on_commit` 这些**与库无关**的性质原样保留。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from backend.core import engine as engine_module


def test_connection_level_settings_apply_to_every_connection(isolated_db):  # noqa: ARG001
    """每条连接都带「会话时区 = UTC」——这是 Postgres 侧唯一需要配的连接级设置。

    为什么这条必须留着：本项目把时间存成**文本**（`timestamp_text`）。会话时区若跟随
    服务端（`SHOW timezone` 可能给 `Asia/Shanghai`），`now()` 之类的函数就会写进本地
    时间，而读出来仍是「看起来像 UTC」的字符串——错 8 小时且不报错。
    `_postgresql_on_connect` 每条连接都发一句 `SET TIME ZONE 'UTC'`，这条验证它真的生效。
    """
    from backend.core.engine import get_engine

    eng = get_engine()
    with eng.connect() as conn:
        assert conn.exec_driver_sql("SHOW TimeZone").scalar() == "UTC"
    # 换一条连接再验一次：钩子挂在 `connect` 事件上，只验池里第一条等于只验了第一次借用
    with eng.connect() as conn:
        assert conn.exec_driver_sql("SHOW TimeZone").scalar() == "UTC"


def _case_url() -> str:
    """当前用例那个库的完整 URL（用例自己的 Engine 都建在它上面）。"""
    from backend.core.database import database_url

    return database_url()


def test_pool_is_fixed_size_and_fails_when_exhausted():
    """池满时抛超时，**不扩容**。

    `max_overflow=0` 是「运行期不建连接」这条不变量的落地方式。这条用例直接验证
    「借第 size+1 条会失败」——如果哪天有人把 `max_overflow` 调大，这里会转红。
    与库无关：换了后端之后「池就是这么大」仍然是同一条承诺。
    """
    eng = engine_module.build_engine(_case_url(), size=1, pool_timeout=0.05)
    try:
        first = eng.raw_connection()
        # 池只有 1 条且已被占住；再借必须等超时，而不是新建一条
        with pytest.raises(PoolTimeoutError):
            eng.raw_connection()
        first.close()
    finally:
        eng.dispose()


def test_warmup_builds_all_connections_before_serving():
    """`warmup()` 之后池里已有 size 条，且运行期不再新建连接。

    竞态的触发条件是「建连接」，所以这里同时断言两件事：
    预热后 `checkedout == 0`、`idle == size`；以及预热之后再借还，**连接创建计数不增加**。
    """
    eng = engine_module.build_engine(_case_url(), size=3)
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


def test_warmup_prevents_concurrent_connection_creation():
    """预热之后，**并发**借连接也不新建。

    上一条是串行借还：它证明「池够用时不会新建」，但竞态的触发条件是
    「多个线程**同时**第一次借」。这条用线程池把那个瞬间造出来——
    预热漏做时，`created` 会从 2 涨上去。

    为什么这条比上一条重要：串行借还永远不会撞（同一时刻只有一个借用者），
    而本项目里真正并发的两个借用者是「事件循环线程」与「`log-store` 日志线程」。
    """
    eng = engine_module.build_engine(_case_url(), size=2)
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
    函数写好了、测试也有，唯独生产路径里没有它。

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


def test_reset_engine_follows_database_change(isolated_db, monkeypatch):  # noqa: ARG001
    """换库（`DATABASE_URL` 变了）之后，`get_engine()` 必须换到新库（不能还连着旧的）。

    #77 之前这里换的是 `DB_PATH`（SQLite 文件路径）；现在换的是一整个库名——
    「换库」这件事在 Postgres 上就是换 URL，语义没变。
    """
    import dbsupport
    from backend.core.config import get_settings
    from backend.core.engine import get_engine, reset_engine

    first = get_engine()

    other_name = dbsupport.database_name()
    dbsupport.create_database(other_name)
    try:
        monkeypatch.setenv("DATABASE_URL", dbsupport.database_url(other_name))
        get_settings.cache_clear()
        reset_engine()

        second = get_engine()
        assert second is not first
        assert second.url.database == other_name
    finally:
        reset_engine()
        dbsupport.drop_database(other_name)
