"""SQLite 数据库连接与建表。

登录（用户 / 会话 / 邀请码）与方案（solutions）都落在本文件管理的 SQLite 库里。
这里**只负责连接与表结构**，不含任何业务读写——业务读写一律走 `repositories/`，
从而满足「业务代码不出现 SQL」的约束。

连接策略：
- 每次操作新建连接、用完即关。SQLite 建连成本极低（微秒级），换来的是天然线程安全，
  不必在 async 环境里维护连接池或线程局部存储。
- 开启 WAL：读写不再互斥，为将来的并发写预留。
- 设置 busy_timeout：并发写时先等待，而不是立刻抛 `database is locked`。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from backend.core.config import get_settings

# 建表语句。全部带 IF NOT EXISTS，重复启动不会破坏已有数据。
SCHEMA_SQL = """
-- 用户：密码只存 scrypt 哈希与独立 salt，绝不存明文
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    salt          BLOB    NOT NULL,
    created_at    TEXT    NOT NULL,
    is_active     INTEGER NOT NULL DEFAULT 1
);

-- 登录会话：只存 token 的哈希，数据库泄露也无法直接拿去冒用
CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash TEXT    PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT    NOT NULL,
    expires_at TEXT    NOT NULL
);

-- 邀请码：max_uses = 0 表示不限次数；expires_at 为 NULL 表示永不过期
CREATE TABLE IF NOT EXISTS invite_codes (
    code       TEXT    PRIMARY KEY,
    max_uses   INTEGER NOT NULL DEFAULT 0,
    used_count INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT,
    created_at TEXT    NOT NULL,
    note       TEXT
);

-- 方案：一份完整会话的全量快照。
-- data 是 SessionDataV2 的 JSON 文本，后端**不解析其内容**，保持「不透明快照」；
-- title / mode / stage 是前端随快照提交的冗余摘要，让方案列表不必反序列化大 JSON。
-- user_id 非空并级联删除：删号时其方案一并清掉，不留孤儿数据。
CREATE TABLE IF NOT EXISTS solutions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title      TEXT    NOT NULL DEFAULT '',
    mode       TEXT    NOT NULL DEFAULT '',
    stage      TEXT    NOT NULL DEFAULT '',
    data       TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    updated_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_auth_sessions_user    ON auth_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires ON auth_sessions(expires_at);

-- 方案列表按「owner + 最近更新」查询，复合索引同时覆盖过滤与排序
CREATE INDEX IF NOT EXISTS idx_solutions_owner ON solutions(user_id, updated_at DESC);

-- 生成任务：一次「生成」的过程记录，把生成与浏览器连接解耦。
-- 刷新 / 断线只断订阅，不中断任务；重连时先读快照补全已产出正文，再接 live 流。
--
-- 设计要点：
-- - `solution_id` 可空（快捷入口等逃生口可能还没有方案），**刻意不加外键**：
--   任务记录不应因为方案被删而级联消失，否则「任务跑完却没地方落」。
-- - `draft_content` 由 `job_service` 合并写（内存权威，DB 每 ≤300ms 或增量 >4KB flush）：
--   快照读内存运行时而非本列，本列只用于进程重启后的兜底恢复。
-- - `usage` / `input_fingerprint` / `max_provider_switches` 为后续阶段预留，
--   本期可为空 / 默认值，不在逻辑中使用。
CREATE TABLE IF NOT EXISTS generation_jobs (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id              INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    solution_id           INTEGER,                      -- 可空（快捷入口逃生口）；刻意不加外键
    kind                  TEXT    NOT NULL,             -- prd | api_docs | prompts | optimize
    status                TEXT    NOT NULL DEFAULT 'running',
    phase                 TEXT    NOT NULL DEFAULT '',
    draft_content         TEXT    NOT NULL DEFAULT '',
    completed_sections    TEXT    NOT NULL DEFAULT '[]',
    input_fingerprint     TEXT,
    usage                 TEXT,
    max_provider_switches INTEGER NOT NULL DEFAULT 2,
    error_code            TEXT    NOT NULL DEFAULT '',
    error_message         TEXT    NOT NULL DEFAULT '',
    created_at            TEXT    NOT NULL,
    updated_at            TEXT    NOT NULL
);

-- 任务列表按「owner + 最近创建」查询
CREATE INDEX IF NOT EXISTS idx_generation_jobs_owner   ON generation_jobs(owner_id, created_at DESC);
-- 兜底找回：按「运行态 + owner + 种类」定位可恢复的任务
CREATE INDEX IF NOT EXISTS idx_generation_jobs_recover ON generation_jobs(status, owner_id, kind);
"""

# 时间戳统一用 UTC 的 ISO 8601，避免本地时区带来的歧义
DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S"


def utc_now() -> datetime:
    """当前 UTC 时间（naive，不带时区）——库里一律按 UTC 存，读取时也按 UTC 解释。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_now_text() -> str:
    """当前 UTC 时间的 ISO 8601 字符串，用于写入库里的日期字段。

    这个格式的好处是**字典序与时间序一致**，可以直接在 SQL 里比较大小
    （例如 `WHERE expires_at > ?`），不必把时间取回 Python 再判断。
    """
    return utc_now().isoformat()


def db_file_path() -> Path:
    """返回数据库文件路径，并确保其父目录存在。"""
    path = get_settings().db_file
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """打开一个数据库连接，退出时自动关闭。

    连接固定开启三项设置：WAL（读写并发）、busy_timeout（并发写时等待而非报错）、
    外键约束（SQLite 默认关闭外键，必须显式打开）。行以 `sqlite3.Row` 返回，
    便于按列名取值。

    事务由调用方显式 `commit()`；本函数只负责连接的生命周期。
    """
    conn = sqlite3.connect(str(db_file_path()), timeout=10.0, check_same_thread=False)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
    finally:
        conn.close()


def init_schema() -> None:
    """建表（幂等），应用启动时调用一次。"""
    with connect() as conn:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
