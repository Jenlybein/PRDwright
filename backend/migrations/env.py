"""Alembic 环境（v9，单一后端）。

## 与默认模板不同的两处

### ① 数据库地址不来自 `alembic.ini`，来自应用配置

`alembic.ini` 里的 `sqlalchemy.url` 是个占位符。真实地址由 `core/schema.py::
migration_url()` 决定，**最后一级兜底是 `core/database.database_url()`**——与运行时
同一口径。否则会出现「迁移改了 A 库、服务读的是 B 库」这种最难查的故障。

CLI 想临时指定别的库，用 `-x url=...`：

```
uv run alembic -x url=postgresql+psycopg://u:p@127.0.0.1:5432/probe upgrade head
```

### ② 不按方言开任何补丁（#78）

v9 只有 Postgres，所以不需要 `render_as_batch`（那是 SQLite `ALTER TABLE` 能力不足的
补丁，「建新表 + 拷数据」），也不需要布尔列的 `compare_type` 归一（Postgres 的 `BOOLEAN`
就是真布尔，与模型侧同一个类型）。两处都随 SQLite 后端一起删除了：留着它们等于给
「还有人开 SQLite」留一条暗门。
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
from backend.core.schema import migration_url
from sqlmodel import SQLModel

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

logger = logging.getLogger("alembic.v9")

target_metadata = SQLModel.metadata


def _database_url() -> str:
    """迁移用的数据库地址（优先级契约见 `core/schema.py::migration_url`）。

    三级：`-x url=` → `Config.sqlalchemy.url` → **应用配置**（`DATABASE_URL`）。

    ⚠️ 第 3 级以前是写死的 `sqlite+pysqlite:///{default_db_file()}`（#76 修正）：
    配了 `DATABASE_URL` 的 Postgres 部署上，不带参数的 CLI 迁移会跑到**另一个
    SQLite 文件**里去。第 2 级也不能省：程序化调用不带它就会静默回落到默认库。
    """
    return migration_url(
        override=context.get_x_argument(as_dictionary=True).get("url"),
        configured=(config.get_main_option("sqlalchemy.url") or "").strip(),
    )


def run_migrations_offline() -> None:
    """离线模式：只打印 SQL，不连库。"""
    url = _database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连库并执行。"""
    url = _database_url()
    # 明确打印目标库：迁移是「不可逆的写操作」，任何时候都要能一眼看出改的是哪个库。
    #
    # ⚠️ 必须脱敏。这个返回值带密码（Postgres 部署里就是常态），而这个 logger 的输出
    # 会进轮转文件与 `app_logs` 表。
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
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
