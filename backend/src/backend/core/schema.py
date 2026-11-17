"""迁移编排（v9）：把库带到「最新结构」，并对未知来源的库保持怀疑。

## 三条路径

| 库的状态 | 走哪条 | 结果 |
| --- | --- | --- |
| 空库 | `upgrade head` | 建全量 schema（单份 Postgres 基线） |
| 结构已是当前形状、但没有 `alembic_version` | 校验 → `stamp head` | **一行 DDL 都不执行**，只登记版本 |
| 已接管（有 `alembic_version`） | `upgrade head` | 正常增量迁移 |

## 旧数据库不再自动升级（v6.1 起，v9 更硬）

`PRAGMA user_version` 的 20 步手写链**已删除**；v9 起连 SQLite 后端本身都已删除
（`core/database.py` 在配置阶段就拒绝 SQLite / MySQL 的地址）。所以「指向旧库启动」
的行为是**拒绝启动并说清原因**，不是悄悄兼容：

- 旧 SQLite 地址 / 旧 `DB_PATH` / 旧 `prdwright.db` 文件：`UnsupportedBackendError`
  （在 `database_url()` 就失败，服务根本不会起来）；
- Postgres 里结构对不上（手工改库、或从更早版本带过来的库）：`SchemaMismatchError`，
  逐条打印差异。

不做自动「搬迁」是有意的：SQLite 库的结构（布尔存成 INTEGER、`AUTOINCREMENT` 的自增
语义、空的默认值形态）与 Postgres 基线并不一一对应，自动猜一个对应关系会把差异悄悄
留在数据里；启动时明确失败，比运行期「写不进去、读出来是默认值」容易定位得多。

## 为什么 `stamp` 之前必须校验

`stamp` 只写一行 `alembic_version`，**不验证库真的长那样**。假如真实结构差一列，
这个差会**永久隐身**：`alembic check` 认为已是最新，而代码按模型读会读到不存在的列
（症状是「写不进去、读出来是默认值」，不报错）。

所以对「无 `alembic_version` 但已有结构」的库，先做一次结构对拍：与「用模型建出来的库」
逐列、逐索引、逐外键比较；不一致就**拒绝启动**并打印差异明细。宁可起不来，也不要带着
未知结构跑。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from sqlmodel import SQLModel

from backend.core.database import database_url
from backend.core.schema_snapshot import (
    diff_snapshots,
    inspector_snapshot,
    metadata_snapshot,
)

logger = logging.getLogger(__name__)

# `migrations/` 目录：`src/backend/core/schema.py` → 上溯到 `backend/`
_BACKEND_ROOT = Path(__file__).resolve().parents[3]

# `alembic.ini` 里的占位地址：视为「没配置」，继续往下一级找。
_INERT_URL_PLACEHOLDER = "driver://user:pass@localhost/dbname"


class SchemaMismatchError(RuntimeError):
    """库的结构与模型/基线不一致。**这是拒绝启动的理由，不是可以忽略的警告。**

    出现它通常意味着两件事之一：有人手工改过库；或者这个库来自更早的版本，
    需要先用当时的代码升级（旧的 20 步链已在 v6.1 删除，v9 起连 SQLite 后端都已删除，
    不再自动补）。
    """


def migration_url(*, override: str | None, configured: str | None) -> str:
    """迁移要连哪个库：`-x url=` → `Config.sqlalchemy.url` → **应用配置**。

    这个顺序是安全设计（原样搬自 `migrations/env.py` 的模块说明，别调）：

    1. `-x url=...`：CLI 显式指定（调试 / 对拍用）；
    2. `Config` 里的 `sqlalchemy.url`：程序化调用（`prepare_schema`）从 Engine 反推后
       传进来，保证「迁移改的库」与「服务读的库」是同一个；
    3. **`database_url()`**：CLI 不带参数时的兜底。

    ⚠️ 第 3 条以前写死 `sqlite+pysqlite:///{default_db_file()}`（#76 修正）。配了
    `DATABASE_URL` 的 Postgres 部署上，CLI 迁移会**跑到另一个 SQLite 文件**里去——
    这是「迁移改了 A 库、服务读的是 B 库」这类最难查的故障：迁移报成功，服务却仍
    停在旧结构上。走应用配置入口（唯一的 `DATABASE_URL`）就再也没有第二个真相。

    Args:
        override: `-x url=` 给出的地址（可为空）。
        configured: Alembic `Config` 里的地址（可为空，占位符视为没配置）。

    Returns:
        迁移用的数据库 URL。

    Raises:
        UnsupportedBackendError: 三级都没给出可用地址（见 `database.database_url`）。
    """
    if override:
        return override
    configured_url = (configured or "").strip()
    if configured_url and configured_url != _INERT_URL_PLACEHOLDER:
        return configured_url
    return database_url()


def _alembic_config(url: str) -> Config:
    """构造一个不依赖 `alembic.ini` 的 Config。

    不读 `alembic.ini` 是为了让「迁移改的库」与「服务读的库」永远同一个：
    地址由调用方（`prepare_schema`）从 Engine 里取，而不是从配置文件里再解析一遍。
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(_BACKEND_ROOT / "migrations"))
    # ⚠️ `%` 必须转义成 `%%`。
    #
    # Alembic 的 Config 背后是 configparser，默认开启 BasicInterpolation，把 `%` 当
    # 语法字符。而 `render_as_string()` 会按 URL 规范把路径里的 `:`、`\` 编码成
    # `%3A`、`%5C`——Windows 上**必然**发生。不转义的话 `set_main_option` 直接抛
    # `ValueError: invalid interpolation syntax`，而且报错位置指向 configparser，
    # 完全看不出是"路径里有盘符"导致的。
    #
    # 读回来时 configparser 会把 `%%` 还原成 `%`，所以 `migrations/env.py` 拿到的
    # 仍是可用的地址。这是 Alembic 官方文档 "Escaping Special Characters" 的做法。
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def _database_url(engine: Engine) -> str:
    """从 Engine 反推数据库地址（保证迁移与服务指向同一个库）。

    不手拼 URL 前缀：那等于把方言写死在这一行里，换成别的地址之后 Alembic 会照着它
    去连一个不存在的库——报错信息指向"库不存在"，真因是这里拼错了。

    `render_as_string(hide_password=False)` 是官方的逆序列化方式，任何方言都对。
    ⚠️ `hide_password` 必须显式给 `False`：默认会渲染成 `***`，迁移连不上。
    正因为这个返回值带密码，`migrations/env.py` 打印目标库时必须单独做脱敏。
    """
    return engine.url.render_as_string(hide_password=False)


def _has_alembic_version(engine: Engine) -> bool:
    return inspect(engine).has_table("alembic_version")


def _is_empty(engine: Engine) -> bool:
    """库里一张业务表都没有（Alembic 自己的账本不算）。

    只排除迁移账本——「有没有 `alembic_version`」在任何库上的含义都一样。
    （v9 之前还排除 `sqlite_sequence`：那是 SQLite `AUTOINCREMENT` 的副产物，
    换到 Postgres 后这个集合永远为空，判断会静默变成"永远非空"；随 SQLite 后端一起删除了。）
    """
    names = set(inspect(engine).get_table_names())
    return not (names - {"alembic_version"})


_MODELS_SNAPSHOTS: dict[str, dict] = {}


def _models_snapshot(dialect: Any) -> dict:
    """模型的「期望形状」快照（`prepare_schema` 对拍用）。

    ⚠️ **按进程缓存**：模型在进程生命周期内不会变，而 `prepare_schema` 每次启动都要
    拿它做基准。不缓存的话每个用例的 `client` 夹具都要「按方言编译一遍全部列」，
    900 多个用例白白多花十几秒——而这份结果在每个进程里是同一个常量。

    Args:
        dialect: 目标方言（`engine.dialect`）。期望值**按目标方言编译**模型元数据
            （`metadata_snapshot`），不建临时库：类型名与默认值必须与目标库说同一种语言。
    """
    name = dialect if isinstance(dialect, str) else dialect.name
    cached = _MODELS_SNAPSHOTS.get(name)
    if cached is not None:
        return cached
    snapshot = metadata_snapshot(SQLModel.metadata, dialect)
    _MODELS_SNAPSHOTS[name] = snapshot
    return snapshot


def assert_schema_matches_models(engine: Engine) -> None:
    """校验库结构与模型一致；不一致抛 `SchemaMismatchError`。

    读法只有一条（v9 单后端）：`inspector_snapshot(engine)` 走 SQLAlchemy inspector
    （Postgres 上就是 `pg_catalog` 反射），期望值由 `metadata_snapshot` 按目标方言编译。

    ⚠️ **不能因为「换库了」就跳过这条校验**：没有它，`stamp` 会把差异永久藏起来，
    症状是「写不进去、读出来是默认值」，运行时不报错。

    Raises:
        SchemaMismatchError: 存在差异。异常消息里带逐条差异，便于直接定位。
    """
    actual = inspector_snapshot(engine)
    problems = diff_snapshots(_models_snapshot(engine.dialect), actual)
    if problems:
        detail = "\n  ".join(problems)
        raise SchemaMismatchError(
            "数据库结构与模型不一致，已拒绝启动（直接 stamp 会把差异永久藏起来）。\n"
            f"共 {len(problems)} 处差异：\n  {detail}\n"
            "若是本次升级引入的变更，请补一条 Alembic 迁移；若是手工改库，请先修复结构。\n"
            "⚠️ 若这是一个更早版本的库（SQLite 时代的 v20 及以前），本项目已不再支持升级："
            "SQLite 后端已删除，旧的 20 步迁移链也不再自动补。请按 deploy/README.md 用新库重建并导入数据。"
        )


def prepare_schema(engine: Engine) -> str:
    """把库带到最新结构，返回最终的 alembic 版本号。

    Args:
        engine: 已建好的 Engine（此时还没有预热连接池）。

    Returns:
        当前 alembic revision 标识（新库为 `"0001"`）。

    Raises:
        SchemaMismatchError: 已有结构但无法确认它与模型一致。
    """
    url = _database_url(engine)
    cfg = _alembic_config(url)

    if not _has_alembic_version(engine) and not _is_empty(engine):
        # 已经有结构、但没登记过 alembic 版本：这是「从 v6 之前的部署直接过来」的库。
        # 先确认它真的等于基线，再登记版本——**不执行任何 DDL**。
        assert_schema_matches_models(engine)
        logger.info("已有库结构与模型一致，登记基线（不执行任何 DDL）")
        command.stamp(cfg, "head")

    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        return str(conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar())


def current_revision(engine: Engine) -> str | None:
    """当前 alembic 版本号；表不存在时返回 None。"""
    if not _has_alembic_version(engine):
        return None
    with engine.connect() as conn:
        return conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()


def head_revision() -> str | None:
    """代码里迁移链的 head（**不连库**）。

    两个用途：

    - 测试断言「库的版本 == 代码的 head」，而不是写死 `"0001"`——写死的用例
      每加一条迁移就要改一次，而它本该表达的是「是否到最新」；
    - 判断「迁移跑完没有」：`alembic_version != head` 说明迁移还没跑完，
      此时库是通的但业务会因缺列而 500。⚠️ v9 的就绪探针 `/readyz` **没有**用它——
      那里只查「库可写 + 技能包可加载」两件事，迁移由启动期的 `prepare_schema` 保证；
      将来若要把「迁移未完成」也纳入摘流判据，入口就是这里。
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(_BACKEND_ROOT / "migrations"))
    return ScriptDirectory.from_config(cfg).get_current_head()
