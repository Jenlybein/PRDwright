"""Alembic 环境（v6）。

## 与默认模板的三处不同

### ① 数据库地址不来自 `alembic.ini`，来自应用配置

`alembic.ini` 里的 `sqlalchemy.url` 是个占位符。真实地址由 `core/schema.py::
migration_url()` 决定，**最后一级兜底是 `core/database.database_url()`**——与运行时
同一口径。否则会出现「迁移改了 A 库、服务读的是 B 库」这种最难查的故障。

CLI 想临时指定别的库，用 `-x url=...`：

```
uv run alembic -x url=sqlite+pysqlite:////tmp/probe.db upgrade head
```

### ② `compare_type` 对布尔列做归一（仅 SQLite）

`bool` 生成 `BOOLEAN`，现有 SQLite 库是 `INTEGER`。SQLite 里两者等价（都存 0/1），
但 autogenerate 会因此想 ALTER 每一列。白名单在 `core/models.BOOLEAN_COLUMNS`，
与结构对拍共用同一份——**新增布尔列只需登记一处**。
其它方言上不需要这个补丁（Postgres 就是真 `BOOLEAN`），所以那里交给默认比较。

### ③ `render_as_batch` 只在 SQLite 上开

SQLite 不支持大部分 `ALTER COLUMN`，Alembic 的 batch 模式会「建新表 + 拷数据」。
Postgres / MySQL 上开着它只是每次改列都白拷一遍数据，所以按方言开关
（`core/dialect.needs_batch_mode`）。
"""

from __future__ import annotations

import logging
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

# 导入模型 = 把所有表注册进 `SQLModel.metadata`。少了这一句，autogenerate 会认为
# 「模型是空的」，从而生成一份「删掉所有表」的迁移——这是最危险的空白。
from backend.core import models  # noqa: F401
from backend.core.dialect import needs_batch_mode
from backend.core.models import BOOLEAN_COLUMNS
from backend.core.schema import migration_url
from sqlmodel import SQLModel

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

logger = logging.getLogger("alembic.v6")

target_metadata = SQLModel.metadata


def _database_url() -> str:
    """迁移用的数据库地址（优先级契约见 `core/schema.py::migration_url`）。

    三级：`-x url=` → `Config.sqlalchemy.url` → **应用配置**（`DATABASE_URL` →
    `DB_PATH` → 默认文件）。

    ⚠️ 第 3 级以前是写死的 `sqlite+pysqlite:///{default_db_file()}`（#76 修正）：
    配了 `DATABASE_URL` 的 Postgres 部署上，不带参数的 CLI 迁移会跑到**另一个
    SQLite 文件**里去。第 2 级也不能省：程序化调用不带它就会静默回落到默认库。
    """
    return migration_url(
        override=context.get_x_argument(as_dictionary=True).get("url"),
        configured=(config.get_main_option("sqlalchemy.url") or "").strip(),
    )


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


def _migration_options(dialect_name: str) -> dict:
    """按方言给 Alembic 的比较/渲染选项。

    - `compare_type`：布尔列归一**只在 SQLite 上需要**（Postgres 就是真 `BOOLEAN`，
      交给默认比较即可）；
    - `render_as_batch`：batch 模式（「建新表 + 拷数据」）是 SQLite 的补丁，
      别的方言上开着只会白拷一遍数据。
    """
    options: dict = {"render_as_batch": needs_batch_mode(dialect_name)}
    if dialect_name == "sqlite":
        options["compare_type"] = _compare_type
    return options


def run_migrations_offline() -> None:
    """离线模式：只打印 SQL，不连库。"""
    url = _database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **_migration_options(make_url(url).get_backend_name()),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连库并执行。"""
    url = _database_url()
    # 明确打印目标库：迁移是「不可逆的写操作」，任何时候都要能一眼看出改的是哪个库。
    #
    # ⚠️ 必须脱敏。这个返回值可能带密码（换到 Postgres / MySQL 之后就是常态），
    # 而这个 logger 的输出会进轮转文件与 `app_logs` 表。
    try:
        shown = make_url(url).render_as_string(hide_password=True)
    except ArgumentError:
        # 地址解析不了时宁可少打一段，也不要为了格式化让迁移起不来
        shown = "<无法解析的数据库地址>"
    logger.info("迁移目标库：%s", shown)
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
            # 方言取自**真实连接**，不重新解析地址：程序化调用时 URL 是 Engine 反推的，
            # 两者本该一致；万一不一致，以连上的那个为准。
            **_migration_options(connection.dialect.name),
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
