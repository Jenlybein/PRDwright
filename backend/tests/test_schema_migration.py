"""迁移编排守卫（v9 / #78 起只有一份 Postgres 基线）。

四条用例，守的是**升级安全性**，不是功能正确性：

| 用例 | 不做会怎样 |
| --- | --- |
| 空库 → 基线 | 全新部署起不来 |
| 已有结构但无 `alembic_version` → stamp（零 DDL） | 已有库被重建 / 数据丢失 |
| 结构不符 → 拒绝启动 | 差异被永久藏起来，之后某个列「消失」 |
| 基线库 == 模型库（语义快照） | 模型改了却忘了写迁移，只在某些部署上炸 |

> **旧的 SQLite 库不再支持。** `PRAGMA user_version` 的 20 步链已在 v6.1 删除，
> v9 起连 SQLite 后端本身都已删除（`core/database.py` 在配置阶段就拒绝它的地址）；
> 那种库现在会撞上第三条用例（拒绝启动），并在错误信息里被告知该怎么做。
> 这是有意的：宁可起不来，也不要「悄悄 stamp 成最新、把少掉的结构永久藏起来」。

这几条从「临时 SQLite 文件」搬到「临时 Postgres 库」时**只换了手段**：
建库走 `dbsupport.empty_database()`（一个真正空着的库），读结构走
`schema_snapshot.inspector_snapshot(engine)`（SQLAlchemy 反射）。
被验的性质一条没变：到最新、stamp 零 DDL、漂移即拒启动、基线 == 模型。
另外 #78 把 0001~0007 合并成一条基线：空库只需要跑**一条**迁移。
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect

import dbsupport
from backend.core.database import build_engine
from backend.core.schema import (
    SchemaMismatchError,
    current_revision,
    head_revision,
    prepare_schema,
)
from backend.core.schema_snapshot import diff_snapshots, inspector_snapshot


def _case_engine():
    """当前用例那个库的 Engine（`isolated_db` 已经把 `DATABASE_URL` 指过去）。"""
    from backend.core.engine import get_engine

    return get_engine()


def _forget_alembic_adoption() -> None:
    """把库退回「结构已是最新、但没登记过 alembic 版本」的状态。

    这正是「从 v6 之前的部署直接过来」的样子：结构是对的，只差一行版本登记。
    删 `alembic_version` 比另建一个库更贴近现实，也不受 conftest 已建库的影响。
    """
    with dbsupport.connect() as conn:
        conn.execute("DROP TABLE IF EXISTS alembic_version")
        conn.commit()


def test_empty_db_gets_baseline():
    """空库 → `upgrade head` → 16 张业务表 + `alembic_version` = 当前 head。

    不写死版本号：这条用例要表达的是「到最新」，不是「到某一号迁移」。
    表数是逐条迁移累加的结果（#78 合并成一条基线，`0002` 起是增量），所以新加一张表
    要回来改这里——它正是「模型多了一张表、迁移却没跟上」的报警点。
    """
    with dbsupport.empty_database() as url:
        engine = build_engine(url)
        try:
            revision = prepare_schema(engine)
            # 不写死 "0001"：这条用例要表达的是「到最新」，不是「到第 1 步」。
            # 写死之后每加一条迁移都要回来改一次，而改的过程最容易把断言改成恒真。
            assert revision == head_revision()
            assert current_revision(engine) == head_revision()

            tables = set(inspect(engine).get_table_names())
            assert "alembic_version" in tables
            assert len(tables) == 17, f"应当是 16 张业务表 + alembic_version，实际 {sorted(tables)}"
        finally:
            engine.dispose()


def test_existing_current_db_is_stamped_without_any_ddl(isolated_db):  # noqa: ARG001
    """已有当前结构的库 → `stamp head`：**结构一个字节都不变**，数据一行不少。"""
    _forget_alembic_adoption()
    engine = _case_engine()
    before = inspector_snapshot(engine)

    with dbsupport.connect() as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, salt, created_at) VALUES (%s,%s,%s,%s)",
            ("keep-me", "h", b"s", "2026-01-01T00:00:00"),
        )
        conn.commit()

    revision = prepare_schema(engine)

    assert revision == head_revision()
    assert inspector_snapshot(engine) == before, "stamp 路径执行了 DDL：结构被改了"

    with dbsupport.connect() as conn:
        kept = conn.execute("SELECT COUNT(*) FROM users WHERE username='keep-me'").fetchone()[0]
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    assert kept == 1
    assert version == head_revision()


def test_mismatched_db_refuses_to_start(isolated_db):  # noqa: ARG001
    """结构与模型不符 → 抛 `SchemaMismatchError`，**不 stamp、不继续**。

    这也是「更早版本的库」现在的归宿：不再有自动升级，只有一条说清原因的拒绝。
    """
    _forget_alembic_adoption()
    with dbsupport.connect() as conn:
        # 造一个真实存在的漂移：多一个模型里没有的列
        conn.execute("ALTER TABLE users ADD COLUMN legacy_extra TEXT NOT NULL DEFAULT ''")
        conn.commit()

    with pytest.raises(SchemaMismatchError) as excinfo:
        prepare_schema(_case_engine())

    assert "users.legacy_extra" in str(excinfo.value), "差异明细里必须指出是哪一列"
    assert "SQLite" in str(excinfo.value), "必须告诉人这是旧库、以及该怎么做"


def test_baseline_produces_exactly_the_model_schema():
    """基线迁移建出来的库 == 模型建出来的库（守卫 C6 的实际实现）。

    ## 为什么不用 `alembic check`

    实测（SQLAlchemy 2.1.6 + 反射）有两个让 `alembic check` 恒红的缺陷：

    1. 外键 `on_delete=...` 没有被反射进 `options`
       → 每个带 `ON DELETE` 的外键都被判成 `remove_fk` + `add_fk`；
    2. 主键列反射出来 `nullable=True`
       → 每个主键都被判成 `modify_nullable`。

    两者都不是真实漂移。与其在测试里过滤幻影差异（过滤条件一多，真差异也会被吞掉），
    不如换一个**反射无关**的判据：直接比较「基线建出来的库」与「模型建出来的库」的语义快照。

    这条判据同时覆盖「模型改了却忘了写迁移」：模型一变，两边快照就不再相等。
    """
    from backend.core.schema import _models_snapshot

    with dbsupport.empty_database() as url:
        engine = build_engine(url)
        try:
            prepare_schema(engine)
            # 「期望形状」按**目标方言**编译（没有任何临时库），实际形状用反射读
            problems = diff_snapshots(
                _models_snapshot(engine.dialect), inspector_snapshot(engine)
            )
            assert problems == [], "基线迁移与模型不一致：\n  " + "\n  ".join(problems)
        finally:
            engine.dispose()


def test_migration_url_falls_back_to_application_configuration(monkeypatch):
    """迁移地址的第三级兜底是**应用配置**，不是写死的 SQLite 文件（#76）。

    以前 `migrations/env.py` 的最后一级写死 `sqlite+pysqlite:///{default_db_file()}`：
    CLI 不带 `-x url=` 时，配了 `DATABASE_URL` 的 Postgres 部署会被迁移到
    **另一个 SQLite 文件**上（「迁移改了 A 库、服务读的是 B 库」这类最难查的故障）。
    现在只有 `DATABASE_URL` 一个入口，所以不存在第二份真相。
    """
    from backend.core.config import get_settings
    from backend.core.schema import migration_url

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:5432/prdwright")
    monkeypatch.delenv("DB_PATH", raising=False)
    get_settings.cache_clear()

    assert (
        migration_url(override=None, configured=None)
        == "postgresql+psycopg://u:p@127.0.0.1:5432/prdwright"
    )
    # `-x url=` 仍然第一优先（调试 / 对拍；这里不校验后端，它只是把地址交给 Alembic）
    assert migration_url(override="postgresql+psycopg://u:p@127.0.0.1:5433/probe", configured=None) == (
        "postgresql+psycopg://u:p@127.0.0.1:5433/probe"
    )
    # 程序化调用传进来的地址第二优先
    assert migration_url(override=None, configured="postgresql+psycopg://u:p@h/prdwright") == (
        "postgresql+psycopg://u:p@h/prdwright"
    )
    # `alembic.ini` 里的占位符视为「没配置」
    assert migration_url(override=None, configured="driver://user:pass@localhost/dbname") == (
        "postgresql+psycopg://u:p@127.0.0.1:5432/prdwright"
    )


def test_models_declare_no_relationship():
    """模型不声明 `Relationship()`。

    声明了就会在「Session 已关闭」时触发懒加载 → `DetachedInstanceError`，
    而这类错误只在特定分支偶发。跨表查询一律显式 join。
    """
    from sqlalchemy import inspect as sa_inspect

    from backend.core import models

    offenders: list[str] = []
    for name in models.__all__:
        if name == "BOOLEAN_COLUMNS":
            continue
        model = getattr(models, name)
        relationships = sa_inspect(model).relationships
        if relationships:
            offenders.append(f"{name}: {[r.key for r in relationships]}")
    assert offenders == []
