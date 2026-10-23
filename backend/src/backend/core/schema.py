"""迁移编排（v6 / ADR-076）：把库带到「最新结构」，并对未知来源的库保持怀疑。

## 三条路径

| 库的状态 | 走哪条 | 结果 |
| --- | --- | --- |
| 空库 | `upgrade head` | 建全量 schema |
| 结构已是当前形状、但没有 `alembic_version` | 校验 → `stamp head` | **一行 DDL 都不执行**，只登记版本 |
| 已接管（有 `alembic_version`） | `upgrade head` | 正常增量迁移 |

## 旧版本库不再自动升级（v6.1）

`PRAGMA user_version` 的 20 步手写链**已删除**。它是「把 v1~v19 的库补到 v20」的垫片，
只在 v6 上线前后那段时间有服务对象；留着它的代价是每个读代码的人都要多理解一套
并存的历史。

现在遇到那种库，行为是**拒绝启动并说清原因**（`SchemaMismatchError`），
而不是悄悄 stamp 成最新——后者会把「少两张表」这类差异永久藏起来，
等到某次生成时才以「no such table」的形式炸出来。

需要救那种库时，用升级前的代码跑一次到 v20，或者直接重建（本项目的数据都可导出）。

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
import tempfile
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from sqlmodel import SQLModel

from backend.core.paths import default_db_file
from backend.core.schema_snapshot import diff_snapshots, schema_snapshot

logger = logging.getLogger(__name__)

# `alembic.ini` 里的占位地址。程序化调用时我们会把真实地址塞进 Config，
# 这个占位串只用于判断「配置里有没有真的设过地址」。
_PLACEHOLDER_URL = "driver://user:pass@localhost/dbname"

# `migrations/` 目录：`src/backend/core/schema.py` → 上溯到 `backend/`
_BACKEND_ROOT = Path(__file__).resolve().parents[3]


class SchemaMismatchError(RuntimeError):
    """库的结构与模型/基线不一致。**这是拒绝启动的理由，不是可以忽略的警告。**

    出现它通常意味着两件事之一：有人手工改过库；或者这个库来自更早的版本，
    需要先用当时的代码升级（旧的 20 步链已在 v6.1 删除，不再自动补）。
    """


def _alembic_config(url: str) -> Config:
    """构造一个不依赖 `alembic.ini` 的 Config。

    不读 `alembic.ini` 是为了让「迁移改的库」与「服务读的库」永远同一个：
    地址由调用方（`prepare_schema`）从 Engine 里取，而不是从配置文件里再解析一遍。
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(_BACKEND_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _database_url(engine: Engine) -> str:
    """从 Engine 反推数据库地址（保证迁移与服务指向同一个库）。"""
    database = engine.url.database
    if not database:
        return f"sqlite+pysqlite:///{default_db_file()}"
    return f"sqlite+pysqlite:///{database}"


def _has_alembic_version(engine: Engine) -> bool:
    return inspect(engine).has_table("alembic_version")


def _is_empty(engine: Engine) -> bool:
    """库里除了 SQLite 自己的表之外一张表都没有。"""
    names = set(inspect(engine).get_table_names())
    return not (names - {"sqlite_sequence"})


_MODELS_SNAPSHOT: dict | None = None


def _models_snapshot() -> dict:
    """用模型建一个临时库，取其结构快照（作为「期望形状」）。

    ⚠️ **按进程缓存**：模型在进程生命周期内不会变，而 `prepare_schema` 每次启动都要
    拿它做基准。不缓存的话每个用例的 `client` 夹具都要「建临时库 + create_all +
    读快照」，900 多个用例白白多花十几秒——而这份结果在每个进程里是同一个常量。
    """
    global _MODELS_SNAPSHOT
    if _MODELS_SNAPSHOT is not None:
        return _MODELS_SNAPSHOT

    from sqlalchemy import create_engine

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "expected.db"
        temp_engine = create_engine(f"sqlite+pysqlite:///{path}")
        try:
            SQLModel.metadata.create_all(temp_engine)
        finally:
            temp_engine.dispose()
        _MODELS_SNAPSHOT = schema_snapshot(path)
    return _MODELS_SNAPSHOT


def assert_schema_matches_models(engine: Engine) -> None:
    """校验库结构与模型一致；不一致抛 `SchemaMismatchError`。

    Raises:
        SchemaMismatchError: 存在差异。异常消息里带逐条差异，便于直接定位。
    """
    actual = schema_snapshot(engine.url.database)
    problems = diff_snapshots(_models_snapshot(), actual)
    if problems:
        detail = "\n  ".join(problems)
        raise SchemaMismatchError(
            "数据库结构与模型不一致，已拒绝启动（直接 stamp 会把差异永久藏起来）。\n"
            f"共 {len(problems)} 处差异：\n  {detail}\n"
            "若是本次升级引入的变更，请补一条 Alembic 迁移；若是手工改库，请先修复结构。\n"
            "⚠️ 若这是一个更早版本的库（v20 之前），请先用当时的代码升级到 v20——"
            "旧的 20 步迁移链已在 v6.1 删除，不再自动补。"
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
