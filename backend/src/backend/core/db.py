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
#
# ⚠️ 这里定义的是**新库**的最终形状；**已存在的库**不会因为这里加了列而跟着变
# （`CREATE TABLE IF NOT EXISTS` 对已存在的表是空操作）。因此每加一个列都要配一步
# 迁移（见 `core/migrations.py`）：新库走这里，旧库走迁移，两条路缺一不可。
# 只改这里 → 旧库永远少这一列；只加迁移 → 只跑 `init_schema` 的测试与脚本少这一列。
SCHEMA_SQL = """
-- 用户：密码只存 scrypt 哈希与独立 salt，绝不存明文
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    salt          BLOB    NOT NULL,
    created_at    TEXT    NOT NULL,
    is_active     INTEGER NOT NULL DEFAULT 1,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    -- 每日额度覆盖（v17）。-1 = 跟随全局（system_config 的 quota_*），
    -- 0 = 不限，N > 0 = 该用户的具体上限。默认 -1 保证升级后行为不变。
    daily_task_limit  INTEGER NOT NULL DEFAULT -1,
    daily_token_limit INTEGER NOT NULL DEFAULT -1,
    max_running       INTEGER NOT NULL DEFAULT -1
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
    -- Token 用量冗余成列：管理员后台要「按用户 / 按日」统计消耗，
    -- 若只留在 usage 的 JSON 里就得把每行取回 Python 解析，量一大就跑不动。
    -- 它们是从 usage 派生的**副本**，不与 JSON 竞争真源——聚合只查列，展示仍读 JSON。
    prompt_tokens         INTEGER NOT NULL DEFAULT 0,
    completion_tokens     INTEGER NOT NULL DEFAULT 0,
    created_at            TEXT    NOT NULL,
    updated_at            TEXT    NOT NULL
);

-- 任务列表按「owner + 最近创建」查询
CREATE INDEX IF NOT EXISTS idx_generation_jobs_owner   ON generation_jobs(owner_id, created_at DESC);
-- 兜底找回：按「运行态 + owner + 种类」定位可恢复的任务
CREATE INDEX IF NOT EXISTS idx_generation_jobs_recover ON generation_jobs(status, owner_id, kind);

-- 应用日志：管理员后台「日志查看」的数据来源。
-- 与 stdout **同时写**（双写）：文件负责「永不丢、可脱机排查」，本表负责「可过滤分页」。
-- 只写这里不写文件 → 日志写不动时连兜底都没有；只写文件 → 后台要现解析文本才能检索。
-- 本表可随时清空重建，属**可丢弃的派生数据**，不参与任何业务约束（故无外键）。
CREATE TABLE IF NOT EXISTS app_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT    NOT NULL,              -- UTC ISO8601（毫秒），与日志格式化器一致
    level      TEXT    NOT NULL,              -- DEBUG / INFO / WARNING / ERROR
    logger     TEXT    NOT NULL,              -- 来源，如 backend.services.job_service
    msg        TEXT    NOT NULL,
    request_id TEXT,                          -- 可空：启动日志本就不属于任何请求
    user_id    INTEGER,                       -- 尽力带出：extra 里没有就留空
    event      TEXT,                          -- 从 extra.event 提升成列，是分类检索的主维度
    extra_json TEXT    NOT NULL DEFAULT '',   -- 其余 extra 业务字段原样存 JSON
    exc        TEXT                           -- traceback，仅 exc_info 存在时
);

-- 默认视图是「按时间倒序翻页」，这个索引覆盖排序与范围过滤
CREATE INDEX IF NOT EXISTS idx_app_logs_ts     ON app_logs(ts);
-- 「某级别最近 N 条」是排障最高频的查询
CREATE INDEX IF NOT EXISTS idx_app_logs_level  ON app_logs(level, ts);
-- 按请求号反查一次请求的全过程，是排障的主路径
CREATE INDEX IF NOT EXISTS idx_app_logs_request ON app_logs(request_id);

-- 管理员操作审计：后台每一次**写操作**都留一行。
-- 它是问责底线，不是调试日志：调试看 app_logs，追「谁改了什么」看这里。
-- 刻意不设外键——管理员被删除后，他做过的操作仍要有据可查；
-- 设了外键就会随删除一起消失，那恰恰是最需要保留的那些记录。
CREATE TABLE IF NOT EXISTS admin_audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id   INTEGER,                        -- 操作者；账号被删后置空（见上）
    actor_name TEXT    NOT NULL DEFAULT '',    -- 冗余一份用户名：id 变空后仍看得见是谁
    action     TEXT    NOT NULL,               -- 如 user.create / invite.delete
    target     TEXT    NOT NULL DEFAULT '',    -- 被操作对象，如用户名或邀请码
    detail     TEXT    NOT NULL DEFAULT '',    -- 变更内容（不写密码等敏感值）
    request_id TEXT,                           -- 与当次请求的日志对得上
    ts         TEXT    NOT NULL
);

-- 后台默认按时间倒序翻页
CREATE INDEX IF NOT EXISTS idx_admin_audit_ts    ON admin_audit_log(ts);
-- 「这个人最近做过什么」是事后追责最高频的问法
CREATE INDEX IF NOT EXISTS idx_admin_audit_actor ON admin_audit_log(actor_id, ts);

-- 供应商登记（迁移 v16）。密钥**只存环境变量名**，不落明文（ADR-042）。
-- `protocol` 是「协议」而不是厂商：加一家 OpenAI 兼容的新厂商只需填 base_url，
-- 不改代码、不加依赖。激活真源在 system_config.active_provider_id，
-- 本表刻意**不加 is_active 列**——否则可能出现两行都为 1 的矛盾状态。
CREATE TABLE IF NOT EXISTS model_providers (
    id                TEXT PRIMARY KEY,
    name              TEXT    NOT NULL DEFAULT '',
    protocol          TEXT    NOT NULL DEFAULT 'openai_compatible',
    base_url          TEXT    NOT NULL DEFAULT '',
    api_key_env_var   TEXT    NOT NULL DEFAULT '',
    default_model     TEXT    NOT NULL DEFAULT '',
    max_tokens        INTEGER NOT NULL DEFAULT 4096,
    context_window    INTEGER NOT NULL DEFAULT 0,   -- 0 = 交给 model_registry
    max_output        INTEGER NOT NULL DEFAULT 0,
    thinking_enabled  INTEGER NOT NULL DEFAULT 0,   -- 仅 ark 有语义
    extra_json        TEXT    NOT NULL DEFAULT '{}',
    builtin           INTEGER NOT NULL DEFAULT 0,   -- 1 = 内置，不可删
    enabled           INTEGER NOT NULL DEFAULT 1,
    sort_order        INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL
);

-- 运行时配置的**覆盖项**（迁移 v16）。没有某一行 = 用 env 或代码默认值，
-- 这是「升级后行为零变化」的结构保证：「恢复默认」就是删掉那一行。
CREATE TABLE IF NOT EXISTS system_config (
    key        TEXT PRIMARY KEY,   -- 白名单键，或 active_provider_id
    value      TEXT NOT NULL,      -- JSON 编码的标量
    updated_at TEXT NOT NULL,
    updated_by INTEGER             -- 管理员 id；删号后置空，故不加外键
);
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


def utc_ms_text(moment: datetime) -> str:
    """把时间转成**毫秒精度、带 `Z` 后缀**的 UTC ISO 8601 字符串。

    这是 `app_logs.ts` 与日志格式化器**共用**的格式。统一它不为好看：
    日志裁剪要用字符串比较时间（`WHERE ts < ?`），一旦一边带 `Z`、另一边不带，
    字典序就会悄悄错位（`'4' < 'Z'`），表现为「该删的没删」且**不报错**。

    带时区的入参会先折算成 UTC 再去掉时区标记：`isoformat()` 对有时区的
    datetime 输出的是 `+00:00` 而不是 `Z`，直接拼一个 `Z` 上去会得到
    `...+00:00Z` 这种两头不靠的格式——正是本函数要防的那种漂移。
    """
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment.isoformat(timespec="milliseconds") + "Z"


def utc_now_ms_text() -> str:
    """当前 UTC 时间的毫秒精度字符串（`utc_ms_text(utc_now())` 的简写）。"""
    return utc_ms_text(utc_now())


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
