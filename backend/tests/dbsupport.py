"""测试专用的数据库辅助（v6 / T16.12）。

生产代码里**已经没有**「自己开一条连接」这条路了：连接只从 Engine 的池里借，
事务边界只由 `session_scope()` 决定。但用例需要两件生产代码不该有的事：

1. **绕过仓储直接读写库**：给某行回填一个更早的时间、数一张表有几行、
   验证级联删除是否真的发生。这些恰恰不能走仓储——仓储就是被测对象。
2. （建库不在这里：`conftest.py` 用 `prepare_schema` 建一次模板库，每用例拷一份。）

`connect()` 返回的是**底层 DBAPI 连接**（`sqlite3.Connection`），而不是
SQLAlchemy 的 `Connection`：用例里的 SQL 全是 `?` 占位符 + `conn.execute(...)`，
换成 `text()` 包装只是无意义的改写，而且这些 SQL 本来就该长得像「直接看库」。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """借一条底层连接（用完归还给 Engine 的池）。"""
    from backend.core.engine import get_engine

    raw = get_engine().raw_connection()
    try:
        conn: sqlite3.Connection = raw.driver_connection
        # 取值按列名（`row["status"]` / `dict(row)`）。旧连接池一直设着 Row 工厂，
        # 用例也据此写的；不设的话拿到的是元组，`dict(row)` 会直接 ValueError。
        conn.row_factory = sqlite3.Row
        yield conn
    finally:
        raw.close()


__all__ = ["connect"]
