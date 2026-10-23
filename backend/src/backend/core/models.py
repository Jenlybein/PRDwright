"""数据层模型（v6）：14 张表的**唯一结构真源**。

## 这个文件是什么

`SQLModel` 表模型同时承担两件事：运行时读写（SQLAlchemy）与结构定义（DDL）。
从这里开始，「库里有哪些表、哪些列」只由本文件决定，`SCHEMA_SQL` 与手写迁移清单都不再是结构来源。

## 四条必须遵守的写法（每一条都有对应的静默失败模式）

### ① 字符串列必须显式 `Column(Text)`

SQLModel 把 `str` 映射成 **`VARCHAR`**，而现有库是 **`TEXT`**。两者在 SQLite 里亲和性相同、
行为一致，但 `PRAGMA table_info` 报的类型名不同 → 结构对拍全表报「漂移」，
Alembic autogenerate 会想 ALTER 每一列。
**所以本文件里没有裸的 `str` 字段，全部是 `sa_column=Column(Text, ...)`。**

### ② 每张有自增主键的表都要 `sqlite_autoincrement`

SQLModel 的 `id: int | None = Field(default=None, primary_key=True)` 生成的是
`INTEGER NOT NULL PRIMARY KEY`，**没有 `AUTOINCREMENT`**。少了它，SQLite 会复用已被删除的最大 rowid。
`app_logs` / `admin_audit_log` 的 id 被当作翻页游标用，复用会让「下一页」看见已删除的行。

### ③ 列名叫 `metadata` 的那个必须改属性名

`document_versions.metadata` 与 SQLAlchemy 声明式基类的 `Base.metadata` 同名。
直接写 `metadata: str` 不会报错，但会**遮蔽** `Base.metadata` 并触发两条 warning，
之后 `SQLModel.metadata.create_all()` 就找不到表了（症状是「建库成功但一张表都没有」）。
正确写法是属性叫 `meta`、列名绑回 `metadata`。

### ④ 时间戳保持 `str`，**不要**改成 `datetime`

库里存的是 ISO 8601 文本，且大量 SQL 直接做字符串比较（`WHERE expires_at > ?`）——
ISO 8601 的字典序等于时间序，这是承重设计。换 `DateTime` 会同时改变落盘格式、
精度与比较语义（见 ADR-077）。

## 关于索引里的 `DESC`

现有库有 4 个索引写成 `(a, b DESC)`。模型里**不带 `DESC`**，理由有两条：

1. SQLite 可以**反向遍历** B 树，`INDEX(a, b)` 对 `ORDER BY b DESC` 同样有效——
   `DESC` 在 SQLite 里只是写法，不是行为；
2. 带 `DESC` 的索引在 SQLAlchemy 里是「表达式索引」，**SQLite 方言无法反射它**
   → `alembic autogenerate` 会**静默跳过**，基线里就少 4 个索引（第一次生成基线时实测：
   16 个索引只出来了 12 个）。这种缺失不报错，只是查询慢，属于最难发现的一类退化。

## 关于布尔列

`bool` 会映射成 `BOOLEAN`，现有库是 `INTEGER`。SQLite 两者都存 0/1、读回都是 Python `bool`，
所以行为零变化；类型名差异由两份白名单归一：
`tests/support/schema_snapshot.py::BOOLEAN_COLUMNS` 与 `migrations/env.py` 的 `compare_type`。
**新增布尔列必须在两处同时登记**，否则 `alembic check` 会报幻影迁移。
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    text,
)
from sqlmodel import Field, SQLModel

# ---------------------------------------------------------------------------
# 用户与会话
# ---------------------------------------------------------------------------


class User(SQLModel, table=True):
    """用户。密码只存 scrypt 哈希与独立 salt，绝不存明文。"""

    __tablename__ = "users"
    __table_args__ = {"sqlite_autoincrement": True}

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(sa_column=Column(Text, nullable=False, unique=True))
    password_hash: str = Field(sa_column=Column(Text, nullable=False))
    salt: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    created_at: str = Field(sa_column=Column(Text, nullable=False))
    is_active: bool = Field(
        default=True, sa_column=Column(Boolean, nullable=False, server_default=text("1"))
    )
    is_admin: bool = Field(
        default=False, sa_column=Column(Boolean, nullable=False, server_default=text("0"))
    )
    # 每日额度覆盖：-1 = 跟随全局，0 = 不限，N > 0 = 该用户上限。默认 -1 保证升级后行为不变。
    daily_task_limit: int = Field(
        default=-1, sa_column=Column(Integer, nullable=False, server_default=text("-1"))
    )
    daily_token_limit: int = Field(
        default=-1, sa_column=Column(Integer, nullable=False, server_default=text("-1"))
    )
    max_running: int = Field(
        default=-1, sa_column=Column(Integer, nullable=False, server_default=text("-1"))
    )


class AuthSession(SQLModel, table=True):
    """登录会话。只存 token 的哈希，数据库泄露也无法直接拿去冒用。"""

    __tablename__ = "auth_sessions"
    __table_args__ = (
        Index("idx_auth_sessions_user", "user_id"),
        Index("idx_auth_sessions_expires", "expires_at"),
    )

    # 主键是 token 哈希本身，不是自增 id → 不能声明 sqlite_autoincrement
    token_hash: str = Field(sa_column=Column(Text, primary_key=True))
    user_id: int = Field(
        sa_column=Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    )
    created_at: str = Field(sa_column=Column(Text, nullable=False))
    expires_at: str = Field(sa_column=Column(Text, nullable=False))


class InviteCode(SQLModel, table=True):
    """邀请码。`max_uses = 0` 表示不限次数；`expires_at` 为 NULL 表示永不过期。"""

    __tablename__ = "invite_codes"

    code: str = Field(sa_column=Column(Text, primary_key=True))
    max_uses: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    used_count: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    expires_at: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    created_at: str = Field(sa_column=Column(Text, nullable=False))
    note: str | None = Field(default=None, sa_column=Column(Text, nullable=True))


class LoginFailure(SQLModel, table=True):
    """登录失败计数。成功登录即删该用户名的记录，因此行数 = 未清账的失败次数。

    `username` 按小写归一后存：`users.username` 大小写敏感，不归一的话在
    `admin` / `Admin` / `ADMIN` 之间轮换就能绕开计数。
    """

    __tablename__ = "login_failures"
    __table_args__ = (
        # 只查「某用户名在某时刻之后」，复合索引覆盖过滤与排序
        Index("idx_login_failures_user", "username", "created_at"),
        # 清理按时间扫全表（无用户名条件），单独一个索引避免退化成全表扫描
        Index("idx_login_failures_created", "created_at"),
        {"sqlite_autoincrement": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(sa_column=Column(Text, nullable=False))
    ip: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    created_at: str = Field(sa_column=Column(Text, nullable=False))


# ---------------------------------------------------------------------------
# 方案与文档
# ---------------------------------------------------------------------------


class Solution(SQLModel, table=True):
    """方案：一份完整会话的全量快照。

    `data` 是 `SessionDataV2` 的 JSON 文本，后端**不解析其内容**（保持「不透明快照」）。
    `title` / `mode` / `stage` 等是前端随快照提交的冗余摘要，让方案列表不必反序列化大 JSON。
    """

    __tablename__ = "solutions"
    __table_args__ = (
        # 列表查询是「owner + 按内容时间倒序」
        Index("idx_solutions_owner_content", "user_id", "content_updated_at"),
        # 「最近打开」这类查询用的旧索引，保留
        Index("idx_solutions_owner", "user_id", "updated_at"),
        {"sqlite_autoincrement": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(
        sa_column=Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    )
    title: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    mode: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    stage: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    # 后端不解析的不透明快照：保持 TEXT（见模块 docstring ①/④）
    data: str = Field(sa_column=Column(Text, nullable=False))
    created_at: str = Field(sa_column=Column(Text, nullable=False))
    updated_at: str = Field(sa_column=Column(Text, nullable=False))
    # 「内容真正变过」的时间（v7）：列表排序用它与 updated_at 分开的两条时间轴
    content_updated_at: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    # 以下为前端状态回填（迁移 v9 / v10 追加），一律 TEXT + 空串默认
    view_state: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    entry_mode: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    chat_tab: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    summary_json: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    quick_product_name: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    quick_requirements: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    draft_prd_text: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    paste_prd_text: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    paste_api_docs_text: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    active_job_kind: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    active_job_doc_kind: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    active_job_id: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))


class Document(SQLModel, table=True):
    """一份产物（prd / api_docs / prompts）的**身份**；正文住在 `document_versions`。

    ⚠️ 与 `DocumentVersion` 构成环形外键：
    `documents.current_version_id → document_versions.id`（ON DELETE SET NULL），
    `document_versions.document_id → documents.id`（ON DELETE CASCADE）。
    所以**插入顺序固定**：先插 `documents`（指针留空）→ 再插版本 → 最后回填指针。
    三步必须在同一个事务里，否则回填时外键会在 flush 阶段就炸。
    """

    __tablename__ = "documents"
    __table_args__ = (
        # ⚠️ upsert 的冲突目标，不能省：document_repo 靠它做「有就跳过、没有才插」
        UniqueConstraint("solution_id", "kind"),
        {"sqlite_autoincrement": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    solution_id: int = Field(
        sa_column=Column(Integer, ForeignKey("solutions.id", ondelete="CASCADE"), nullable=False)
    )
    kind: str = Field(sa_column=Column(Text, nullable=False))
    created_at: str = Field(sa_column=Column(Text, nullable=False))
    updated_at: str = Field(sa_column=Column(Text, nullable=False))
    current_version_id: int | None = Field(
        default=None,
        sa_column=Column(
            Integer, ForeignKey("document_versions.id", ondelete="SET NULL"), nullable=True
        ),
    )


class DocumentVersion(SQLModel, table=True):
    """文档的一个版本。**正文只住在这里**，`documents` 表不再有正文列。"""

    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version_no"),
        {"sqlite_autoincrement": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    document_id: int = Field(
        sa_column=Column(Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    )
    version_no: int = Field(sa_column=Column(Integer, nullable=False))
    content: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    content_sha: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    pending_items: str = Field(
        default="[]", sa_column=Column(Text, nullable=False, server_default=text("'[]'"))
    )
    last_run_summary: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    source: str = Field(
        default="generated",
        sa_column=Column(Text, nullable=False, server_default=text("'generated'")),
    )
    note: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    # ⚠️ 属性名是 `meta`、列名是 `metadata`：后者与 `Base.metadata` 撞名（见模块 docstring ③）
    meta: str = Field(
        default="{}",
        sa_column=Column("metadata", Text, nullable=False, server_default=text("'{}'")),
    )
    created_at: str = Field(sa_column=Column(Text, nullable=False))
    updated_at: str = Field(sa_column=Column(Text, nullable=False))


# ---------------------------------------------------------------------------
# 生成任务
# ---------------------------------------------------------------------------


class GenerationJob(SQLModel, table=True):
    """一次「生成」的过程记录，把生成与浏览器连接解耦。

    `solution_id` 可空且**刻意不加外键**：任务记录不应因为方案被删而级联消失，
    否则「任务跑完却没地方落」。
    """

    __tablename__ = "generation_jobs"
    __table_args__ = (
        Index("idx_generation_jobs_owner", "owner_id", "created_at"),
        # 兜底找回：按「运行态 + owner + 种类」定位可恢复的任务
        Index("idx_generation_jobs_recover", "status", "owner_id", "kind"),
        {"sqlite_autoincrement": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    owner_id: int = Field(
        sa_column=Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    )
    solution_id: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    # kind = 「做了什么」（prd / api_docs / prompts / optimize / chat）
    kind: str = Field(sa_column=Column(Text, nullable=False))
    # doc_kind = 「写到哪一份产物」；`optimize` / `clarify` 凭 kind 定不了产物
    doc_kind: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    # chat_stage = 对话任务属于哪条线程；并发判重与重连都要它
    chat_stage: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    status: str = Field(
        default="running",
        sa_column=Column(Text, nullable=False, server_default=text("'running'")),
    )
    phase: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    # 进程重启后的兜底恢复用；快照读内存运行时而非本列
    draft_content: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    completed_sections: str = Field(
        default="[]", sa_column=Column(Text, nullable=False, server_default=text("'[]'"))
    )
    pending_items: str = Field(
        default="[]", sa_column=Column(Text, nullable=False, server_default=text("'[]'"))
    )
    input_fingerprint: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    usage: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    max_provider_switches: int = Field(
        default=2, sa_column=Column(Integer, nullable=False, server_default=text("2"))
    )
    error_code: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    error_message: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    # Token 用量冗余成列：后台「按用户 / 按日」统计只查列，不解析 JSON
    prompt_tokens: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    completion_tokens: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    # 供应商快照（从 usage JSON 派生的副本，不设外键：配置条目会被删、会被改，
    # 回查等于让历史账单跟着今天的状态变）
    provider_protocol: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    provider_config_id: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    provider_name: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    provider_model: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    created_at: str = Field(sa_column=Column(Text, nullable=False))
    updated_at: str = Field(sa_column=Column(Text, nullable=False))


# ---------------------------------------------------------------------------
# 对话
# ---------------------------------------------------------------------------


class ChatSession(SQLModel, table=True):
    """方案在某条对话线程上绑定的上游会话 id。

    ⚠️ `ux_chat_sessions_thread` 是 upsert 的冲突目标，不能省。
    """

    __tablename__ = "chat_sessions"
    __table_args__ = (
        Index("ux_chat_sessions_thread", "solution_id", "stage", unique=True),
        {"sqlite_autoincrement": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(
        sa_column=Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    )
    solution_id: int = Field(
        sa_column=Column(Integer, ForeignKey("solutions.id", ondelete="CASCADE"), nullable=False)
    )
    stage: str = Field(sa_column=Column(Text, nullable=False))
    conversation_id: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    updated_at: str = Field(sa_column=Column(Text, nullable=False))


class ChatTurn(SQLModel, table=True):
    """一条对话轮次（用户 / 助手 / 工具结果）。

    ⚠️ `ux_chat_turns_job_role` 是**部分唯一索引**（只约束 `job_id` 非空的行），
    幂等写轮次靠它：同一个 job 的同一角色只允许一条。
    """

    __tablename__ = "chat_turns"
    __table_args__ = (
        # 读线程（按方案 + 线程取全部轮次，按 id 排序）与拼历史都走它
        Index("idx_chat_turns_thread", "solution_id", "stage", "id"),
        Index(
            "ux_chat_turns_job_role",
            "job_id",
            "role",
            unique=True,
            sqlite_where=text("job_id IS NOT NULL"),
        ),
        {"sqlite_autoincrement": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(
        sa_column=Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    )
    solution_id: int = Field(
        sa_column=Column(Integer, ForeignKey("solutions.id", ondelete="CASCADE"), nullable=False)
    )
    stage: str = Field(sa_column=Column(Text, nullable=False))
    role: str = Field(sa_column=Column(Text, nullable=False))
    content: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    tool_call_id: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    tool_result: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    asks: str = Field(
        default="[]", sa_column=Column(Text, nullable=False, server_default=text("'[]'"))
    )
    answers: str = Field(
        default="[]", sa_column=Column(Text, nullable=False, server_default=text("'[]'"))
    )
    tool_calls: str = Field(
        default="[]", sa_column=Column(Text, nullable=False, server_default=text("'[]'"))
    )
    interrupted: bool = Field(
        default=False, sa_column=Column(Boolean, nullable=False, server_default=text("0"))
    )
    job_id: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    created_at: str = Field(sa_column=Column(Text, nullable=False))


# ---------------------------------------------------------------------------
# 日志与审计
# ---------------------------------------------------------------------------


class AppLog(SQLModel, table=True):
    """应用日志。与 stdout **双写**：文件负责「永不丢」，本表负责「可过滤分页」。

    本表可随时清空重建，属**可丢弃的派生数据**，不参与任何业务约束（故无外键）。
    """

    __tablename__ = "app_logs"
    __table_args__ = (
        Index("idx_app_logs_ts", "ts"),
        Index("idx_app_logs_level", "level", "ts"),
        Index("idx_app_logs_request", "request_id"),
        {"sqlite_autoincrement": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    ts: str = Field(sa_column=Column(Text, nullable=False))
    level: str = Field(sa_column=Column(Text, nullable=False))
    logger: str = Field(sa_column=Column(Text, nullable=False))
    msg: str = Field(sa_column=Column(Text, nullable=False))
    request_id: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    user_id: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    event: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    extra_json: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    exc: str | None = Field(default=None, sa_column=Column(Text, nullable=True))


class AdminAuditLog(SQLModel, table=True):
    """管理员操作审计。**刻意不设外键**——管理员被删除后，他做过的操作仍要有据可查。"""

    __tablename__ = "admin_audit_log"
    __table_args__ = (
        Index("idx_admin_audit_ts", "ts"),
        Index("idx_admin_audit_actor", "actor_id", "ts"),
        {"sqlite_autoincrement": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    actor_id: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    actor_name: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    action: str = Field(sa_column=Column(Text, nullable=False))
    target: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    detail: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    request_id: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    ts: str = Field(sa_column=Column(Text, nullable=False))


# ---------------------------------------------------------------------------
# 配置与供应商
# ---------------------------------------------------------------------------


class ModelProvider(SQLModel, table=True):
    """供应商登记。密钥用主密钥加密后存 `value_token`（**只存密文**）。

    `protocol` 是「协议」而不是厂商：加一家 OpenAI 兼容的新厂商只需填 `base_url`。
    激活真源在 `system_config.active_provider_id`，本表**不加 is_active 列**——
    否则可能出现两行都为真的矛盾状态。
    """

    __tablename__ = "model_providers"

    id: str = Field(sa_column=Column(Text, primary_key=True))
    name: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    protocol: str = Field(
        default="openai_compatible",
        sa_column=Column(Text, nullable=False, server_default=text("'openai_compatible'")),
    )
    base_url: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    value_token: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    default_model: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default=text("''"))
    )
    max_tokens: int = Field(
        default=4096, sa_column=Column(Integer, nullable=False, server_default=text("4096"))
    )
    context_window: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    max_output: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    thinking_enabled: bool = Field(
        default=False, sa_column=Column(Boolean, nullable=False, server_default=text("0"))
    )
    extra_json: str = Field(
        default="{}", sa_column=Column(Text, nullable=False, server_default=text("'{}'"))
    )
    # 遗留列：v19 起一律为 0，**代码不再读取**（保留是为了不重建表）
    builtin: bool = Field(
        default=False, sa_column=Column(Boolean, nullable=False, server_default=text("0"))
    )
    enabled: bool = Field(
        default=True, sa_column=Column(Boolean, nullable=False, server_default=text("1"))
    )
    sort_order: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    created_at: str = Field(sa_column=Column(Text, nullable=False))
    updated_at: str = Field(sa_column=Column(Text, nullable=False))


class SystemConfig(SQLModel, table=True):
    """运行时配置的**覆盖项**。没有某一行 = 用 env 或代码默认值。

    这是「升级后行为零变化」的结构保证：「恢复默认」就是删掉那一行。
    """

    __tablename__ = "system_config"

    # ⚠️ `key` 是列名也是属性名：SQLAlchemy 允许（与 `metadata` 不同）
    key: str = Field(sa_column=Column(Text, primary_key=True))
    value: str = Field(sa_column=Column(Text, nullable=False))
    updated_at: str = Field(sa_column=Column(Text, nullable=False))
    # 管理员 id；删号后置空，故不加外键
    updated_by: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))


# 布尔列白名单。这不是「一份可以偷懒的清单」，而是一道**显式闸门**：
# `bool` 在 SQLite 里声明为 `BOOLEAN`，现有库是 `INTEGER`。两者行为一致（都存 0/1），
# 但类型名不同会让 `alembic check` 想 ALTER 每一列（幻影迁移）。
# 处置方式是让 alembic 的 `compare_type` 对这**些**列把 BOOLEAN/INTEGER 视为等价——
# 所以新增布尔列必须在这里登记，否则会冒出幻影迁移。
# `tests/test_schema_parity.py` 断言「从模型推导出的布尔列集合 == 这份清单」，
# 两头对不上就转红（既防漏登记，也防清单过期）。
BOOLEAN_COLUMNS: frozenset[tuple[str, str]] = frozenset(
    {
        ("users", "is_active"),
        ("users", "is_admin"),
        ("model_providers", "builtin"),
        ("model_providers", "enabled"),
        ("model_providers", "thinking_enabled"),
        ("chat_turns", "interrupted"),
    }
)


# 供 alembic 与测试引用的全部表模型（顺序与建表无关，SQLAlchemy 自己按外键排序）
__all__ = [
    "BOOLEAN_COLUMNS",
    "AdminAuditLog",
    "AppLog",
    "AuthSession",
    "ChatSession",
    "ChatTurn",
    "Document",
    "DocumentVersion",
    "GenerationJob",
    "InviteCode",
    "LoginFailure",
    "ModelProvider",
    "Solution",
    "SystemConfig",
    "User",
]
