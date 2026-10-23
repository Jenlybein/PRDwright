"""Alembic 环境（v6 / ADR-076）。

## 与默认模板的三处不同

### ① 数据库地址不来自 `alembic.ini`，来自应用配置

`alembic.ini` 里的 `sqlalchemy.url` 是个占位符。真实地址由 `core/paths.default_db_file()`
解析——与运行时**同一口径**。否则会出现「迁移改了 A 库、服务读的是 B 库」这种最难查的故障。

CLI 想临时指定别的库，用 `-x url=...`：

```
uv run alembic -x url=sqlite+pysqlite:////tmp/probe.db upgrade head
```

### ② `compare_type` 对布尔列做归一

`bool` 生成 `BOOLEAN`，现有库是 `INTEGER`。SQLite 里两者等价（都存 0/1），
但 autogenerate 会因此想 ALTER 每一列。白名单在 `core/models.BOOLEAN_COLUMNS`，
与结构对拍共用同一份——**新增布尔列只需登记一处**。

### ③ `render_as_batch=True`

SQLite 不支持大部分 `ALTER COLUMN`，Alembic 的 batch 模式会「建新表 + 拷数据 + 换名」。
本期用不上（基线之后还没有新迁移），但第一次真要改列时，没有它就只能手写建表逻辑。
"""

from __future__ import annotations

import logging
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# 导入模型 = 把所有表注册进 `SQLModel.metadata`。少了这一句，autogenerate 会认为
# 「模型是空的」，从而生成一份「删掉所有表」的迁移——这是最危险的空白。
from backend.core import models  # noqa: F401
from backend.core.models import BOOLEAN_COLUMNS
from backend.core.paths import default_db_file
from sqlmodel import SQLModel

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

logger = logging.getLogger("alembic.v6")

target_metadata = SQLModel.metadata


def _database_url() -> str:
    """迁移用的数据库地址。

    优先级（**这个顺序是安全设计，别调**）：

    1. `-x url=...`：CLI 显式指定（调试 / 对拍用）；
    2. `Config` 里的 `sqlalchemy.url`：程序化调用（`core/schema.py`）从 Engine 反推后传进来，
       保证「迁移改的库」与「服务读的库」是同一个；
    3. `default_db_file()`：CLI 不带参数时的兜底。

    ⚠️ 第 2 条不能省。省掉的后果是：程序化调用时静默回落到**默认库**——
    迁移跑到了开发库上，而调用方以为在操作临时库。这类错误在测试里表现为
    「建表已存在」，在生产里表现为「升级了不该升级的库」。
    """
    override = context.get_x_argument(as_dictionary=True).get("url")
    if override:
        return override

    configured = (config.get_main_option("sqlalchemy.url") or "").strip()
    # `alembic.ini` 里的占位符视为「没配置」
    if configured and configured != "driver://user:pass@localhost/dbname":
        return configured

    return f"sqlite+pysqlite:///{default_db_file()}"


def _compare_type(
    _context, inspected_column, _metadata_column, inspected_type, metadata_type
):  # noqa: ANN001
    """布尔列的类型比较：`BOOLEAN` 与 `INTEGER` 视为等价。

    返回 `False` = 「没有变化，不生成迁移」；返回 `None` = 「交给默认实现继续判断」。
    """
    table_name = getattr(inspected_column.table, "name", None)
    key = (table_name, getattr(inspected_column, "name", None))
    if key in BOOLEAN_COLUMNS:
        names = {type(inspected_type).__name__.upper(), type(metadata_type).__name__.upper()}
        if names <= {"BOOLEAN", "INTEGER"}:
            return False
    return None


def run_migrations_offline() -> None:
    """离线模式：只打印 SQL，不连库。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=_compare_type,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连库并执行。"""
    url = _database_url()
    # 明确打印目标库：迁移是「不可逆的写操作」，任何时候都要能一眼看出改的是哪个文件。
    logger.info("迁移目标库：%s", url)
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = url

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=_compare_type,
            # SQLite 的 ALTER 能力有限：改列 / 删列都要「重建表 + 拷数据」，
            # batch 模式把这件事自动化。只在第一次真正改列时用到。
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
