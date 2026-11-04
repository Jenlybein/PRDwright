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


def insert_job(
    owner_id: int,
    *,
    kind: str = "prd",
    status: str = "completed",
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
            "draft_content, completed_sections, pending_items, "
            "input_fingerprint, usage, max_provider_switches, "
            "error_code, error_message, prompt_tokens, completion_tokens, "
            "provider_protocol, provider_config_id, provider_name, provider_model, "
            "created_at, updated_at, request_params, config_snapshot, skill_refs, skill_manifest"
            ") VALUES (?, NULL, ?, '', '', ?, ?, '', '[]', '[]', NULL, NULL, 2, ?, '', ?, ?, '', '', '', '', ?, ?, '{}', '{}', '[]', '')",
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


__all__ = ["connect", "insert_job"]
