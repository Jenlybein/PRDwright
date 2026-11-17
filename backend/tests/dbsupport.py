"""测试专用的数据库辅助（v6 / T16.12；#77 起后端换成 Postgres）。

生产代码里**已经没有**「自己开一条连接」这条路了：连接只从 Engine 的池里借，
事务边界只由 `session_scope()` 决定。但用例需要两件生产代码不该有的事：

1. **绕过仓储直接读写库**：给某行回填一个更早的时间、数一张表有几行、
   验证级联删除是否真的发生。这些恰恰不能走仓储——仓储就是被测对象。
2. **建库**：`conftest.py` 每 worker 建一次「迁移到 head」的模板库，
   每个用例再 `CREATE DATABASE … TEMPLATE` 派生一个自己的库（用例结束删掉）。

`connect()` 返回的仍是**底层 DBAPI 连接**（这次是 psycopg 的 `Connection`），
而不是 SQLAlchemy 的 `Connection`：用例里的 SQL 全是 `%s` 占位符 + `conn.execute(...)`，
换成 `text()` 包装只是无意义的改写，而且这些 SQL 本来就该长得像「直接看库」。

## 维护库地址的约定（⚠️ 别拼错）

`PRDWRIGHT_TEST_POSTGRES_URL` 给的是**维护库地址、不带库名**：

```
postgresql+psycopg://postgres:postgres@127.0.0.1:5433
```

测试自己往后面拼 `/<库名>`（`maintenance_url() + "/" + name`）去
`CREATE DATABASE` / `DROP DATABASE`。**别把库名写进这个变量**：写进去之后
每个用例都连同一个库，隔离就没了；而且 `CREATE DATABASE` 不允许在事务块里跑，
维护连接必须走 `AUTOCOMMIT`。

## 行怎么取

用例里两种写法都在用，所以 `connect()` 装了一个「双形态」行工厂：

- 按列名：`row["status"]`、`dict(row)`；   （既有写法的主力）
- 按位置：`row[0]`、`fetchone()[0]`。       （数行、取第一列那种）

只装 psycopg 的 `dict_row` 会让后一类全部变成 `KeyError: 0`——那等于为了换库改写
十几条断言的形状，而它们本来与后端无关。
"""

from __future__ import annotations

import atexit
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool


# 集成库的环境变量名与本地默认值。**默认值不含库名**（见模块 docstring）。
POSTGRES_URL_ENV = "PRDWRIGHT_TEST_POSTGRES_URL"
DEFAULT_POSTGRES_URL = "postgresql+psycopg://postgres:postgres@127.0.0.1:5433"

# 用例库的命名前缀：`isolated_db` 用它断言「这个库是本用例新建的」。
CASE_DATABASE_PREFIX = "prdwright_test_"

# 每 worker 的模板库前缀（进程结束时会删掉）。
_TEMPLATE_PREFIX = "prdwright_template_"


class PostgresUnavailable(RuntimeError):
    """测试基座连不上集成库——**这是失败，不是跳过**。

    票面口径：没有 Postgres 时全量用例必须明确失败并给出可操作提示，
    不允许静默绿。异常文案里带着「该设哪个变量、本地该起在哪」。
    """


def maintenance_url() -> str:
    """维护库地址（不带库名）；环境变量优先，缺省用本地集成库。"""
    return (os.environ.get(POSTGRES_URL_ENV) or DEFAULT_POSTGRES_URL).rstrip("/")


def maintenance_engine(url: str | None = None) -> Engine:
    """连维护库 `postgres` 的 Engine。

    `AUTOCOMMIT` 是必须的：`CREATE DATABASE` / `DROP DATABASE` 不允许在事务块里跑。
    `NullPool` 也是必须的：维护连接只用一瞬间，不该在池里占着 PG 的
    `max_connections` 名额——每个 xdist worker 各开一池，很容易撞上限。

    Args:
        url: 维护库地址（不带库名）；缺省读环境变量。显式传它的唯一理由是
            **收尾要沿用建立时那个地址**：用例可能改过环境变量（那是它被测的对象），
            而删库不该因为「环境变量现在是错的」而失败。
    """
    base = url or maintenance_url()
    return create_engine(
        f"{base}/postgres",
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
        connect_args={"connect_timeout": 3},
    )


def probe_postgres() -> None:
    """确认集成库连得上；连不上抛 `PostgresUnavailable` 并带上操作指引。

    Raises:
        PostgresUnavailable: 任何连不上的原因（拒绝连接、库不存在、认证失败…）。
            统一成一个异常，因为对跑用例的人来说它们的处置方式只有一种：
            「把集成库起起来，或把地址设对」。
    """
    engine = maintenance_engine()
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
    except Exception as exc:  # noqa: BLE001 - 任何连不上的原因都归为「基座没起来」
        raise PostgresUnavailable(
            "测试基座连不上 Postgres，全量用例无法运行（本票之后不再静默跳过）。\n"
            f"当前地址：{maintenance_url()}（来自 {POSTGRES_URL_ENV}，缺省 {DEFAULT_POSTGRES_URL}）\n"
            "怎么修：\n"
            f"  1. 起一个集成库（本地默认映射到 127.0.0.1:5433）；或\n"
            f"  2. 设 {POSTGRES_URL_ENV}=postgresql+psycopg://用户:密码@主机:端口 —— "
            "注意这是**维护库地址、不带库名**，库名由用例自己建；\n"
            "  3. CI 上由 `.github/workflows/ci.yml` 的 postgres service 提供并注入同一个变量。"
        ) from exc
    finally:
        engine.dispose()


def database_url(name: str) -> str:
    """某个库的完整 URL（维护库地址 + `/库名`）。"""
    return f"{maintenance_url()}/{name}"


def database_name(prefix: str = CASE_DATABASE_PREFIX) -> str:
    """一个唯一的新库名（并行 worker 之间也不会撞）。"""
    return f"{prefix}{uuid.uuid4().hex[:16]}"


def create_database(name: str, *, template: str | None = None) -> None:
    """建一个库；给了 `template` 就从它派生（结构一样、数据为空的那一份）。"""
    engine = maintenance_engine()
    try:
        with engine.connect() as conn:
            suffix = f' TEMPLATE "{template}"' if template else ""
            conn.exec_driver_sql(f'CREATE DATABASE "{name}"{suffix}')
    finally:
        engine.dispose()


def drop_database(name: str, *, url: str | None = None) -> None:
    """删库；先把还连在上面的会话踢掉（否则 `DROP DATABASE` 会被挡）。

    Args:
        name: 库名。
        url: 维护库地址；缺省读环境变量。**收尾时请传建立时那个地址**——
            用例可能把环境变量改成别的（那是被测对象），删库不该跟着变。
    """
    engine = maintenance_engine(url)
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}"')
    finally:
        engine.dispose()


@contextmanager
def database(*, template: str | None = None) -> Iterator[str]:
    """借一个**结构齐备**的用例库（派生自本 worker 的模板库），退出即删。

    给「用例自己需要另起一个库」的场合用：结构对拍、隔离性验证、多库对照。

    Args:
        template: 从哪个模板库派生；缺省用本 worker 那个「已迁移到 head」的模板。

    Returns:
        这个库的完整 URL。
    """
    with _use_database(
        database_name(), template if template is not None else template_database()
    ) as url:
        yield url


@contextmanager
def empty_database() -> Iterator[str]:
    """借一个**空库**（一张表都没有），退出即删。

    给「空库 → `upgrade head`」这类用例用：它要的正是一个还没建表的库，
    派生自模板反而会把被测的那条路径跳过去。
    """
    with _use_database(database_name(), None) as url:
        yield url



@contextmanager
def _use_database(name: str, template: str | None) -> Iterator[str]:
    create_database(name, template=template)
    try:
        yield database_url(name)
    finally:
        drop_database(name)


_TEMPLATE_DATABASE: str | None = None


def template_database() -> str:
    """本进程（= 本 xdist worker）建一次「迁移到 head」的模板库，之后一直复用。

    为什么不每用例都跑一次 `alembic upgrade head`：1000 多个用例 × 几百毫秒，
    等于没人会随手跑。而模板库与真实启动走的是同一条路（`prepare_schema`），
    派生出来的库「结构已是最新、只差数据」，`client` 夹具的 lifespan 再调
    `prepare_schema` 时就是 `upgrade head` 空转——正好是真实部署里最常见的分支。

    ⚠️ 模板库**只有结构、没有数据**，这是「用例之间数据互不可见」的根：
    每个用例从它派生，所以永远看不到上一个用例写的行。
    """
    global _TEMPLATE_DATABASE
    if _TEMPLATE_DATABASE is not None:
        return _TEMPLATE_DATABASE

    probe_postgres()

    name = f"{_TEMPLATE_PREFIX}{uuid.uuid4().hex[:12]}"
    create_database(name)

    # ⚠️ 用独立的 Engine，不碰 `get_engine()`：后者绑的是当前用例的 `DATABASE_URL`，
    # 而模板要在任何用例的库名都还不存在时就建好。
    from backend.core.database import build_engine
    from backend.core.schema import prepare_schema

    engine = build_engine(database_url(name))
    try:
        prepare_schema(engine)
    finally:
        # dispose 把池里的连接全关掉：模板库被别的会话连着的库不能当 `TEMPLATE` 用。
        engine.dispose()

    _TEMPLATE_DATABASE = name
    atexit.register(_drop_template_at_exit, name)
    return name


def _drop_template_at_exit(name: str) -> None:
    """进程退出时删掉模板库（正常跑完不留垃圾；崩溃/被杀时留给下一次环境重置）。"""
    try:
        drop_database(name)
    except Exception:  # noqa: BLE001 - 退出阶段的清理失败不该让进程带着异常结束
        pass


# ---------------------------------------------------------------------------
# 行的「双形态」视图
# ---------------------------------------------------------------------------


class _DualRow(tuple):
    """一行既可按位置取（`row[0]`），也可按列名取（`row["status"]`）。

    为什么要两个形态：既有用例两种写法都在用，而且都不是坏写法——
    「数一张表有几行」用 `fetchone()[0]` 最直白，「断言某一列」用列名最抗列序漂移。
    换库不该逼着改写这些断言的形式，所以行对象自己兼容两种。
    """

    def __new__(cls, values: Any, names: Any) -> _DualRow:
        row = super().__new__(cls, values)
        row._names = tuple(names)  # noqa: SLF001 - tuple 子类靠实例属性记住列名
        return row

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            for index, name in enumerate(self._names):
                if name == key:
                    return tuple.__getitem__(self, index)
            raise KeyError(key)
        return tuple.__getitem__(self, key)

    def keys(self) -> tuple[str, ...]:
        """满足映射协议：`dict(row)` 要靠它。"""
        return self._names  # noqa: SLF001


def _dual_row_factory(cursor: Any) -> Any:
    """psycopg 的行工厂：返回 `_DualRow`（列名从游标的 description 取）。"""
    names = tuple(column.name for column in (cursor.description or ()))

    def make_row(values: Any) -> _DualRow:
        return _DualRow(values, names)

    return make_row


# ---------------------------------------------------------------------------
# 借一条底层连接
# ---------------------------------------------------------------------------


@contextmanager
def connect() -> Iterator[Any]:
    """借一条底层连接（用完归还给 Engine 的池）。

    ⚠️ 行工厂装在这个**池化连接**上，所以退出前必须还原：不还原的话，
    同一条连接下一次被 SQLAlchemy 借去执行 ORM 查询时，驱动回的是 `_DualRow`
    而不是元组，SQLAlchemy 的行装配会拿到一堆列名字符串。
    """
    from backend.core.engine import get_engine

    raw = get_engine().raw_connection()
    try:
        conn = raw.driver_connection
        previous = conn.row_factory
        conn.row_factory = _dual_row_factory
        try:
            yield conn
        finally:
            conn.row_factory = previous
    finally:
        raw.close()


def insert_job(
    owner_id: int,
    *,
    kind: str = "prd",
    status: str = "succeeded",
    phase: str = "",
    error_code: str = "",
    created_at: str | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> None:
    """直接插一行任务（绕开仓储的 running-only 建法）。

    为什么集中在这里：这行 INSERT 原先在 7 个测试文件里各写了一份，列清单互相抄。
    v8 删掉 `doc_kind` 等列的 `server_default`（MySQL 的 TEXT 不接受字面量默认值）之后，
    7 个文件同时红——那说明重复的是"表结构知识"，而它只该有一个副本。

    列清单的原则：**把 NOT NULL 且无默认值的列全部显式写出来**。表结构再变时只改这里。
    """
    from backend.core.clock import utc_now_text

    moment = created_at or utc_now_text()
    with connect() as conn:
        conn.execute(
            "INSERT INTO generation_jobs ("
            "owner_id, solution_id, kind, doc_kind, chat_stage, status, phase, "
            "draft_content, pending_items, "
            "input_fingerprint, usage, max_provider_switches, "
            "error_code, error_message, prompt_tokens, completion_tokens, "
            "provider_protocol, provider_config_id, provider_name, provider_model, "
            "created_at, updated_at, request_params, config_snapshot, skill_refs, skill_manifest"
            ") VALUES (%s, NULL, %s, '', '', %s, %s, '', '[]', NULL, NULL, 2, %s, '', %s, %s, "
            "'', '', '', '', %s, %s, '{}', '{}', '[]', '')",
            (
                owner_id,
                kind,
                status,
                phase,
                error_code,
                prompt_tokens,
                completion_tokens,
                moment,
                moment,
            ),
        )
        conn.commit()


__all__ = [
    "CASE_DATABASE_PREFIX",
    "DEFAULT_POSTGRES_URL",
    "POSTGRES_URL_ENV",
    "PostgresUnavailable",
    "connect",
    "create_database",
    "database",
    "database_name",
    "database_url",
    "drop_database",
    "insert_job",
    "maintenance_engine",
    "maintenance_url",
    "probe_postgres",
    "template_database",
]
