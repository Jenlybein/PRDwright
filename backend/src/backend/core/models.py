"""数据层模型（v9）：16 张表的**唯一结构真源**。

## 这个文件是什么

`SQLModel` 表模型同时承担两件事：运行时读写（SQLAlchemy）与结构定义（DDL）。
从这里开始，「库里有哪些表、哪些列」只由本文件决定，`SCHEMA_SQL` 与手写迁移清单都不再是结构来源。

## 四条必须遵守的写法（每一条都有对应的静默失败模式）

### ① 字符串列必须显式 `Column(...)`，且不容许裸 `str`

SQLModel 把裸 `str` 映射成**没有长度**的 `VARCHAR`。它本身能建表，但「哪些列有长度、
哪些列是大文本」这件事必须显式声明：索引列要有界（`short_text(n)`），大文本用
`long_text()`。结构对拍按列类型比较，裸 `str` 会让「模型以为的」与「迁移建出来的」
悄悄分叉。**所以本文件里没有裸的 `str` 字段。**

### ② 自增主键在 Postgres 上由序列提供，不要声明 SQLite 的表选项

SQLModel 的 `id: int | None = Field(default=None, primary_key=True)` 在 Postgres 上
映射成 `SERIAL`/identity 列：序列只增不回退，删掉最大 id 也不会复用它。
`app_logs` / `admin_audit_log` 的 id 被当作翻页游标用，复用会让「下一页」看见已删除的行。
（v9 之前的 `sqlite_autoincrement` 表选项已随 SQLite 后端一起删除——它只在 SQLite 上
有意义，在 Postgres 上是被静默忽略的死配置。）

### ③ 列名叫 `metadata` 的那个必须改属性名

`document_versions.metadata` 与 SQLAlchemy 声明式基类的 `Base.metadata` 同名。
直接写 `metadata: str` 不会报错，但会**遮蔽** `Base.metadata` 并触发两条 warning，
之后 `SQLModel.metadata.create_all()` 就找不到表了（症状是「建库成功但一张表都没有」）。
正确写法是属性叫 `meta`、列名绑回 `metadata`。

### ④ 时间戳保持 `str`，**不要**改成 `datetime`

库里存的是 ISO 8601 文本，且大量 SQL 直接做字符串比较（`WHERE expires_at > ?`）——
ISO 8601 的字典序等于时间序，这是承重设计。换 `DateTime` 会同时改变落盘格式、
精度与比较语义。

## 关于索引里的 `DESC`

模型里的索引**不带 `DESC`**，理由有两条：

1. Postgres 可以**反向扫描** B-tree（`Index Scan Backward`），`INDEX(a, b)` 对
   `ORDER BY b DESC` 同样有效——`DESC` 只是写法，不是行为；
2. 带 `DESC` 的索引在 SQLAlchemy 里是「表达式索引」，反射出来与模型声明不是同一份
   形状 → 结构对拍会出现幻影差异，基线里还可能少几个索引。这种缺失不报错，
   只是查询慢，属于最难发现的一类退化。

## 关于布尔列

`bool` 映射成 Postgres 的真 `BOOLEAN`；默认值必须是 `true` / `false` 字面量，
不能写 `0` / `1`（Postgres 的 `BOOLEAN` 不接受整数默认值）。`BOOLEAN_COLUMNS` 是
布尔列的登记清单，守卫用例靠它检查「新增布尔列必须同时登记、且默认值写成布尔字面量」。
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
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

# 列类型策略统一走 `core/types.py`（v8）。本文件**不给索引列声明裸 Text**：
# 索引列一旦没有长度上限，B-tree 条目会在数据量上来之后超限——那种错误只有到
# 生产迁移才暴露，所以由 `assert_metadata_is_portable` 在 import 时统一拦。
from backend.core.types import (
    assert_metadata_is_portable,
    long_text,
    short_text,
    timestamp_text,
)

# 大段文本（Postgres TEXT）。容量不是业务约束——60KB 由 `core/limits.py` 在写入前拦。
_BIG_TEXT = long_text()

# 用户名：大小写敏感的唯一键。
#
# Postgres 的 `VARCHAR` 默认就是大小写敏感的比较（不依赖 collation），所以这里
# 只需 `short_text(32)`（= `VARCHAR(32)`），不再需要 `with_variant`（#78 删掉了
# MySQL 的 `utf8mb4_0900_as_cs` 变体，那个分支已随 MySQL 后端一起移除）。
#
# 长度 32 与 `auth_service._USERNAME_MAX`（=32）对齐：校验与列宽必须是同一个数，
# 否则会出现「能注册但存不进去」或「列比校验宽一倍」这类不一致。
_USERNAME_TEXT = short_text(32)

# ---------------------------------------------------------------------------
# 用户与会话
# ---------------------------------------------------------------------------


class User(SQLModel, table=True):
    """用户。密码只存 scrypt 哈希与独立 salt，绝不存明文。"""

    __tablename__ = "users"

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(
        sa_column=Column(_USERNAME_TEXT, nullable=False, unique=True)
    )
    password_hash: str = Field(sa_column=Column(Text, nullable=False))
    salt: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    created_at: str = Field(sa_column=Column(timestamp_text(), nullable=False))
    is_active: bool = Field(
        default=True, sa_column=Column(Boolean, nullable=False, server_default=text("true"))
    )
    is_admin: bool = Field(
        default=False, sa_column=Column(Boolean, nullable=False, server_default=text("false"))
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

    # 主键是 token 哈希本身，不是自增 id → 没有序列（也不需要任何自增表选项）
    token_hash: str = Field(sa_column=Column(short_text(64), primary_key=True))
    user_id: int = Field(
        sa_column=Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    )
    created_at: str = Field(sa_column=Column(timestamp_text(), nullable=False))
    expires_at: str = Field(sa_column=Column(timestamp_text(), nullable=False))


class InviteCode(SQLModel, table=True):
    """邀请码。`max_uses = 0` 表示不限次数；`expires_at` 为 NULL 表示永不过期。"""

    __tablename__ = "invite_codes"

    code: str = Field(sa_column=Column(short_text(64), primary_key=True))
    max_uses: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    used_count: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    expires_at: str | None = Field(default=None, sa_column=Column(timestamp_text(), nullable=True))
    created_at: str = Field(sa_column=Column(timestamp_text(), nullable=False))
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
    )

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(sa_column=Column(short_text(32), nullable=False))
    ip: str = Field(
        default="", sa_column=Column(Text, nullable=False)
    )
    created_at: str = Field(sa_column=Column(timestamp_text(), nullable=False))


# ---------------------------------------------------------------------------
# 方案与文档
# ---------------------------------------------------------------------------


class Solution(SQLModel, table=True):
    """方案：一份完整会话的全量快照。

    方案状态已拆成标量列；`title` / `mode` / `stage` 等只是给列表用的摘要，
    列表查询不再需要反序列化大 JSON。旧的整份 `SessionDataV2` 快照列已在 v8 删除。
    """

    __tablename__ = "solutions"
    __table_args__ = (
        # 列表查询是「owner + 按内容时间倒序」
        Index("idx_solutions_owner_content", "user_id", "content_updated_at"),
        # 「最近打开」这类查询用的旧索引，保留
        Index("idx_solutions_owner", "user_id", "updated_at"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(
        sa_column=Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    )
    title: str = Field(
        default="", sa_column=Column(Text, nullable=False)
    )
    mode: str = Field(
        default="", sa_column=Column(Text, nullable=False)
    )
    stage: str = Field(
        default="", sa_column=Column(Text, nullable=False)
    )
    created_at: str = Field(sa_column=Column(timestamp_text(), nullable=False))
    updated_at: str = Field(sa_column=Column(timestamp_text(), nullable=False))
    # 「内容真正变过」的时间（v7）：列表排序用它与 updated_at 分开的两条时间轴
    content_updated_at: str = Field(
        default="", sa_column=Column(timestamp_text(), nullable=False, server_default=text("''"))
    )
    # 以下为前端状态回填（迁移 v9 / v10 追加），一律 TEXT + 空串默认
    view_state: str = Field(
        default="", sa_column=Column(Text, nullable=False)
    )
    entry_mode: str = Field(
        default="", sa_column=Column(Text, nullable=False)
    )
    chat_tab: str = Field(
        default="", sa_column=Column(Text, nullable=False)
    )
    summary_json: str = Field(
        default="", sa_column=Column(Text, nullable=False)
    )
    quick_product_name: str = Field(
        default="", sa_column=Column(Text, nullable=False)
    )
    quick_requirements: str = Field(
        default="", sa_column=Column(Text, nullable=False)
    )
    draft_prd_text: str = Field(
        default="", sa_column=Column(Text, nullable=False)
    )
    paste_prd_text: str = Field(
        default="", sa_column=Column(Text, nullable=False)
    )
    paste_api_docs_text: str = Field(
        default="", sa_column=Column(Text, nullable=False)
    )
    active_job_kind: str = Field(
        default="", sa_column=Column(Text, nullable=False)
    )
    active_job_doc_kind: str = Field(
        default="", sa_column=Column(Text, nullable=False)
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
    )

    id: int | None = Field(default=None, primary_key=True)
    solution_id: int = Field(
        sa_column=Column(Integer, ForeignKey("solutions.id", ondelete="CASCADE"), nullable=False)
    )
    kind: str = Field(sa_column=Column(short_text(32), nullable=False))
    created_at: str = Field(sa_column=Column(timestamp_text(), nullable=False))
    updated_at: str = Field(sa_column=Column(timestamp_text(), nullable=False))
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
    )

    id: int | None = Field(default=None, primary_key=True)
    document_id: int = Field(
        sa_column=Column(Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    )
    version_no: int = Field(sa_column=Column(Integer, nullable=False))
    # 一整篇 PRD：勘察实测 11,566 字 = 24,534 字节。
    # ⚠️ 不设 `server_default`：大文本默认空串会让「裸 SQL 省略这一列」静默写进空串，
    # 而空串与「没写」不可区分。ORM 插入总是显式带值，裸 SQL 省略该列应该失败——
    # 那是显式错误，比静默的空串好。
    content: str = Field(default="", sa_column=Column(_BIG_TEXT, nullable=False))
    content_sha: str = Field(
        default="", sa_column=Column(Text, nullable=False)
    )
    pending_items: str = Field(
        default="[]", sa_column=Column(Text, nullable=False)
    )
    last_run_summary: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    source: str = Field(
        default="generated",
        sa_column=Column(Text, nullable=False),
    )
    note: str = Field(
        default="", sa_column=Column(Text, nullable=False)
    )
    # ⚠️ 属性名是 `meta`、列名是 `metadata`：后者与 `Base.metadata` 撞名（见模块 docstring ③）
    meta: str = Field(
        default="{}",
        sa_column=Column("metadata", Text, nullable=False),
    )
    created_at: str = Field(sa_column=Column(timestamp_text(), nullable=False))
    updated_at: str = Field(sa_column=Column(timestamp_text(), nullable=False))
    # 审核结论（票 #52）：**一版一条**，被审的那一版正文配一条结论。JSON 文本，
    # 形状与 SSE `review` 事件的数据一致（判定 / 逐条问题 / 总结 / 是否改写 /
    # 失败章节 / 审核时间 / 这次试没试过）。**审核没跑成就是 NULL**——没有结论不等于
    # 通过，旧版本与旧数据读出来是「未审核」。
    #
    # ⚠️ 声明顺序 = 物理列序：迁移 0006 追加 `review`、0007 追加 `review_failed_at`，
    # 所以这两列都必须在老列之后、且保持这个先后。模型这边换个位置，`create_all`
    # 建出来的库列序就与迁移建的不同，`test_schema_migration` 的整份快照对比会转红。
    # ⚠️ 不设 `server_default`：大文本默认值会让「省略该列」静默写入空串。
    review: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    # 「这一版上一次审核试过但没跑成」的时间（UTC ISO 8601 文本）；NULL = 没有失败记录。
    # 生成时的审核失败与修复后的重审失败写同一列（#61）：两者都是「这份正文被审过、
    # 而且没审成」，界面与读侧不必分两套。
    #
    # 它是**落库事实**，不是会话内状态：审核失败不写结论（没有结论 ≠ 通过），但
    # 「这次没审成」本身必须刷新后还看得见，否则界面退化成「未审核」——用户会以为
    # 从没审过。拿到新结论时清空（见 `DocumentRepository.replace_current_review`）。
    #
    # 与 `review` 同一行的理由同上（US3）：失败描述的就是这一版。
    review_failed_at: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    # 「这一版正文**可用但不完整**」（票 #85）：写到一半失败、或有章节落成失败占位。
    #
    # 标记的**真源在版本行**：同一份文档的不同版本里，只有那一版不完整，所以它不能挂在
    # `documents`（那是文档的身份，不描述某一版的内容）。任务行上再留一份
    # （`generation_jobs.partial`），两处由同一次收尾事务写入。
    #
    # ⚠️ 它**不是**审核状态，也不影响「能不能下载 / 修复 / 重生成」：那三件事由正文与
    # 结论决定，`partial` 只回答「这份正文完整不完整」。
    partial: bool = Field(
        default=False, sa_column=Column(Boolean, nullable=False, server_default=text("false"))
    )


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
        # 运行中回答「这次用的是哪套技能包」：身份短串可索引，长度 ≤768
        Index("idx_generation_jobs_skill_manifest", "skill_manifest"),
    )

    id: int | None = Field(default=None, primary_key=True)
    owner_id: int = Field(
        sa_column=Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    )
    solution_id: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    # kind = 「做了什么」（prd / api_docs / prompts / optimize / chat）
    kind: str = Field(sa_column=Column(short_text(32), nullable=False))
    # doc_kind = 「写到哪一份产物」；`optimize` / `clarify` 凭 kind 定不了产物
    doc_kind: str = Field(default="", sa_column=Column(Text, nullable=False))
    # chat_stage = 对话任务属于哪条线程；并发判重与重连都要它
    chat_stage: str = Field(default="", sa_column=Column(Text, nullable=False))
    status: str = Field(
        default="running",
        sa_column=Column(short_text(32), nullable=False),
    )
    phase: str = Field(default="", sa_column=Column(Text, nullable=False))
    # 进程重启后的兜底恢复用；快照读内存运行时而非本列。
    # 勘察实测 26,957 字 = 36,919 字节：Postgres 的 `TEXT` 无长度上限，不必挑容量档位。
    # ⚠️ 不设 `server_default`：大文本默认值会让「省略该列」静默写入空串。
    draft_content: str = Field(default="", sa_column=Column(_BIG_TEXT, nullable=False))
    # 分章完成清单（JSON 数组），实测 15,777 字节，贴着 16KB 线，留一档余量
    completed_sections: str = Field(
        default="[]", sa_column=Column(_BIG_TEXT, nullable=False)
    )
    pending_items: str = Field(
        default="[]", sa_column=Column(Text, nullable=False)
    )
    input_fingerprint: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    usage: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    max_provider_switches: int = Field(
        default=2, sa_column=Column(Integer, nullable=False, server_default=text("2"))
    )
    error_code: str = Field(
        default="", sa_column=Column(Text, nullable=False)
    )
    error_message: str = Field(
        default="", sa_column=Column(Text, nullable=False)
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
        default="", sa_column=Column(Text, nullable=False)
    )
    provider_config_id: str = Field(
        default="", sa_column=Column(Text, nullable=False)
    )
    provider_name: str = Field(
        default="", sa_column=Column(Text, nullable=False)
    )
    provider_model: str = Field(
        default="", sa_column=Column(Text, nullable=False)
    )
    created_at: str = Field(sa_column=Column(timestamp_text(), nullable=False))
    updated_at: str = Field(sa_column=Column(timestamp_text(), nullable=False))
    # --- 任务单里不可变数据的完整投影（票 #34）---
    # 建任务那一刻一次写全，之后任何运行态写入路径都不碰这四列。
    # ⚠️ 不设 server_default：大文本默认值会让「省略该列」静默写入空串
    # （`assert_metadata_is_portable` 会在 import 时自检）。
    request_params: str = Field(default="{}", sa_column=Column(_BIG_TEXT, nullable=False))
    config_snapshot: str = Field(default="{}", sa_column=Column(_BIG_TEXT, nullable=False))
    skill_refs: str = Field(default="[]", sa_column=Column(_BIG_TEXT, nullable=False))
    # 主技能包的可索引身份（名称 / 版本 / 指纹拼出的短串），不参与生成链路。
    skill_manifest: str = Field(
        default="", sa_column=Column(short_text(64), nullable=False)
    )
    # --- 执行权（租约，票 #82）---
    #
    # 「谁在执行」从进程内存搬进这一行，并**会到期**：三件事合起来才是执行权。
    # 空串表示「没有人持有有效租约」——三态下这就是「排队中」（七态是 #84 的事）。
    #
    # 为什么归属与凭证分开：归属（`lease_owner`）只用来回答「出问题时找哪个进程」，
    # 判权必须看凭证（`lease_token`）。用归属判权时，同一个进程的上一轮会被认成这一轮，
    # 「旧执行者的晚到结果」就写得进去——那正是本票要挡的东西。
    lease_owner: str = Field(
        default="", sa_column=Column(short_text(64), nullable=False, server_default=text("''"))
    )
    lease_token: str = Field(
        default="", sa_column=Column(short_text(64), nullable=False, server_default=text("''"))
    )
    # 到期时间用文本而不是真时间类型：全库口径一致（`core/types.py::timestamp_text`），
    # ISO 8601 的字典序 = 时间序，可以直接进 `WHERE ... < ?` / `>= ?` 的条件写。
    lease_expire_at: str = Field(
        default="",
        sa_column=Column(timestamp_text(), nullable=False, server_default=text("''")),
    )
    # --- 失败退避（票 #88）---
    #
    # 「这条任务最早什么时候可以被下一个执行者领走」。可重试失败回排队时写
    # 「失败时刻 + 按错误类型算出的退避时长」（映射表在 `services/job_backoff.py`，
    # 全仓只一份）；到期之前抢占条件一行都改不到。
    #
    # 空串 = **立即到期**（不是「没有值」）：于是到期判据只有一种形状——
    # `next_run_at = '' OR next_run_at <= now`。允许 NULL 的话每处判据都要多一条
    # `IS NULL` 分支，漏写一处不会报错、只会让那一行永远领不走。
    next_run_at: str = Field(
        default="",
        sa_column=Column(timestamp_text(), nullable=False, server_default=text("''")),
    )
    # 「这次生成有内容可用、但不完整」（票 #85）：失败收尾（含用户取消）把已生成的正文
    # 落成一版 `document_versions.partial=true` 时，任务行同一次事务里留这一份投影。
    #
    # 为什么任务行也要一份：界面同时问两个问题——「这次任务怎么结束的」（任务行 status）
    # 与「这份正文能不能用」（版本行 partial）。只写一处，就有一边说不清。
    # 与 `usage.partial`（章节级占位的旧口径）并存：那条仍在运行中的快照里用，
    # 落库的这份是收尾时定格的结论。
    partial: bool = Field(
        default=False, sa_column=Column(Boolean, nullable=False, server_default=text("false"))
    )


class JobAttempt(SQLModel, table=True):
    """一次「已结束的执行」的账本行（票 #80）：**只增不改**。

    ## 它回答什么

    「这次任务是第几次、成还是败、为什么、花了多少、跑了多久」。任务行只留**最近一次**
    的错误（`generation_jobs.error_code` / `error_message` 是同值的单份投影），完整的执行
    历史只在这里——两者分工明确：任务行说「现在是什么状态」，账本说「一路过来都发生过
    什么」。任务行因此不必再长出一排历史列。

    ## 什么时候写

    **只有执行有结论时才写一行**（成功 / 失败 / 取消 / 回收判死）。抢占、登记、进度合并写
    都不产生账本行：那只是「开始」与「过程」，不是结论。被取代**不写**——它不是这次执行
    的结局，而是被更晚的任务接手（`SUPERSEDED` 留在任务行上供排查）。

    ## 序号

    `attempt_no` 在**同一任务内**从 1 起递增，与 `job_id` 组成唯一约束
    （`ux_job_attempts_job_attempt`）——它是「不重复」的数据库级保证；分配由
    `JobAttemptRepository.append` 在同一事务里算 `MAX+1`。今天每次执行都会走到终态
    且不重跑，所以实际取值恒为 1；#82 的重试（一次失败之后再来一次）才会让它变成 2、3。

    ## 「脱敏摘要」

    `error_summary` 是**单行、截断、抹掉疑似凭据**之后的错误文案（实现见
    `services/job_state._sanitize_summary`）：账本是长期留存的审计数据，不能把上游回显
    的 key / token 原样抄进来。

    ## 归属与级联

    `job_id` 外键指向 `generation_jobs.id` 且 `ON DELETE CASCADE`：账号被删、任务行被删
    时账本跟着消失（它是任务行的附属审计，不是独立事实）。刻意不存 `owner_id`——归属
    一律经任务行 join，避免两份归属真源。

    ## 用量与耗时

    用量取任务收尾时那一份汇总（与 `generation_jobs.usage` / 两个 token 列同源），耗时取
    同一个 `duration_ms`。回收判死（启动时把残留 `running` 收成失败）没有执行者在场，
    这两项记 0——那是**未知**，不是「没花」；#82 的租约恢复会把用量带进账本。
    """

    __tablename__ = "job_attempts"
    __table_args__ = (
        # 序号唯一性：同一任务的账本序号从 1 起、不重复。名字写死便于迁移里同形声明。
        UniqueConstraint("job_id", "attempt_no", name="ux_job_attempts_job_attempt"),
    )

    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(
        sa_column=Column(
            Integer, ForeignKey("generation_jobs.id", ondelete="CASCADE"), nullable=False
        )
    )
    # 同一任务内的执行序号（1 起）。唯一约束见 `__table_args__`。
    attempt_no: int = Field(sa_column=Column(Integer, nullable=False))
    # 结论：completed / failed / cancelled。取值住在 `repositories/job_attempt_repo.py`。
    outcome: str = Field(sa_column=Column(short_text(32), nullable=False))
    # 错误分类（`ErrorCode` 的取值或 CANCELLED 这类中止码）；成功时为空串。
    error_code: str = Field(default="", sa_column=Column(Text, nullable=False))
    # 脱敏后的单行摘要（见类 docstring「脱敏摘要」）；成功时为空串。
    error_summary: str = Field(default="", sa_column=Column(Text, nullable=False))
    # 这一轮花了多少：与 `generation_jobs` 的两个 token 列同源。
    prompt_tokens: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    completion_tokens: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    # 这一轮跑了多久（毫秒）：与 `usage.duration_ms` 同源。
    duration_ms: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    created_at: str = Field(sa_column=Column(timestamp_text(), nullable=False))


class JobEvent(SQLModel, table=True):
    """生成过程的一条事件日志（票 #86）：**只增不改、只按游标读**。

    ## 它回答什么

    「这次生成到第几步、已经吐出了哪些字」。任务行的 `phase` / `draft_content` /
    `completed_sections` 只留**最近一次的合并写**（每 ≤300ms 或 4KB 一次），中间那几次
    推进没有任何痕迹；浏览器断线重连时，「上次读到哪里」也没有落点。

    这张表把**过程日志**独立出来：

    - 订阅按 `id > cursor` 逐条读（`cursor` 是客户端手里那一条的 id），断线重连因此
      **不重不漏**；
    - 多个订阅者各持各的游标，各读各的（游标是读者的状态，不是任务的）；
    - 时间上的**唯一真相**在这里：`id` 单调自增，同一任务内的事件顺序即 id 顺序。

    ## 为什么不和「权威正文」混

    任务行上的 `draft_content` / `completed_sections` 是**快照**（冷读者一次拿走即可），
    本表是**过程**。两者分工：快照回答「现在长什么样」，事件表回答「一路走来发生了什么」。
    冷读者只取快照 + 快照游标，**不回放**本表历史（否则刷新一次就把整段生成重演一遍）；
    表里的行只是给「已经手持游标的读者」续读用。

    ## `id` 为什么是 BIGSERIAL

    它是**全局**自增游标（不是每任务一个序号）：一次查询只按 `(job_id, id)` 走一个索引
    区间，`id > cursor` 的比较对全表也成立。`Integer` 只有 21 亿，长期部署里事件表是
    增长最快的一张表，留一档尺寸不占任何实际空间。

    ## 保留策略

    终态任务的过程日志**保留 7 天**（`services/job_events.purge_expired_events`，由
    #89 的调度接线调它，本票只交付这个动作）。清理只删本表，权威正文在任务行与
    `document_versions` 上，一行都不受影响。

    ## 归属与级联

    `job_id` 外键指向 `generation_jobs.id` 且 `ON DELETE CASCADE`：任务行随账号删除一起
    消失时，它的过程日志跟着消失（否则库里会留下指向不存在任务的孤儿行）。**不存
    `owner_id`**：归属一律经任务行确认，避免两份归属真源。
    """

    __tablename__ = "job_events"
    __table_args__ = (
        # 订阅的唯一查询形态：按任务取「比游标新」的一段。`id` 放第二列，因为它是
        # INCLUDE 之外的自然序：同一个 job_id 下的 id 本来就有序，索引扫一遍即可。
        Index("idx_job_events_job_cursor", "job_id", "id"),
    )

    # 全局自增游标（BIGSERIAL）：读侧只做 `id > cursor`，写侧从不显式赋值。
    id: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, primary_key=True, autoincrement=True),
    )
    job_id: int = Field(
        sa_column=Column(
            Integer, ForeignKey("generation_jobs.id", ondelete="CASCADE"), nullable=False
        )
    )
    # 事件种类：与 SSE 的 `event:` 同名（status / chunk / part / stage / stage_chunk /
    # review / error / done / run_summary），见 `services/job_events.EVENT_KINDS`。
    # 短文本（列宽与 `short_text` 纪律对齐）——它不进索引，但索引列以外的文本也一律给宽度，
    # 免得「同名列在不同表里类型不一致」。
    kind: str = Field(sa_column=Column(short_text(32), nullable=False))
    # 事件体（JSON）：`{content, data, code}`，与 `StreamEvent` 三字段一一对应。
    # 大文本，**不给 server_default**（省略该列应当当场失败，而不是静默写空串）。
    payload: str = Field(sa_column=Column(Text, nullable=False))
    created_at: str = Field(sa_column=Column(timestamp_text(), nullable=False))


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
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(
        sa_column=Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    )
    solution_id: int = Field(
        sa_column=Column(Integer, ForeignKey("solutions.id", ondelete="CASCADE"), nullable=False)
    )
    stage: str = Field(sa_column=Column(short_text(32), nullable=False))
    conversation_id: str = Field(
        default="", sa_column=Column(Text, nullable=False)
    )
    updated_at: str = Field(sa_column=Column(timestamp_text(), nullable=False))


class ChatTurn(SQLModel, table=True):
    """一条对话轮次（用户 / 助手 / 工具结果）。

    ⚠️ `ux_chat_turns_job_role` 是**部分唯一索引**（只约束 `job_id` 非空的行），
    幂等写轮次靠它：同一个 job 的同一角色只允许一条。

    部分条件只声明 `postgresql_where`（#78）：SQLAlchemy 的 `Index` 不把 `sqlite_where`
    传给 Postgres，若只写它，PG 上会被**静默忽略**——不是报错，而是建成一条全量唯一
    索引（语义少了 WHERE 那一层）。非空条件在唯一索引里同义（NULL 互不冲突），
    所以写同一句 `text("job_id IS NOT NULL")` 就够。
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
            postgresql_where=text("job_id IS NOT NULL"),
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(
        sa_column=Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    )
    solution_id: int = Field(
        sa_column=Column(Integer, ForeignKey("solutions.id", ondelete="CASCADE"), nullable=False)
    )
    stage: str = Field(sa_column=Column(short_text(32), nullable=False))
    role: str = Field(sa_column=Column(short_text(32), nullable=False))
    content: str = Field(
        default="", sa_column=Column(Text, nullable=False)
    )
    tool_call_id: str = Field(
        default="", sa_column=Column(Text, nullable=False)
    )
    tool_result: str = Field(
        default="", sa_column=Column(Text, nullable=False)
    )
    asks: str = Field(
        default="[]", sa_column=Column(Text, nullable=False)
    )
    answers: str = Field(
        default="[]", sa_column=Column(Text, nullable=False)
    )
    tool_calls: str = Field(
        default="[]", sa_column=Column(Text, nullable=False)
    )
    interrupted: bool = Field(
        default=False, sa_column=Column(Boolean, nullable=False, server_default=text("false"))
    )
    job_id: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    created_at: str = Field(sa_column=Column(timestamp_text(), nullable=False))


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
    )

    id: int | None = Field(default=None, primary_key=True)
    ts: str = Field(sa_column=Column(timestamp_text(), nullable=False))
    level: str = Field(sa_column=Column(short_text(32), nullable=False))
    logger: str = Field(sa_column=Column(Text, nullable=False))
    msg: str = Field(sa_column=Column(Text, nullable=False))
    request_id: str | None = Field(
        default=None, sa_column=Column(short_text(64), nullable=True)
    )
    user_id: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    event: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    extra_json: str = Field(
        default="", sa_column=Column(Text, nullable=False)
    )
    exc: str | None = Field(default=None, sa_column=Column(Text, nullable=True))


class AdminAuditLog(SQLModel, table=True):
    """管理员操作审计。**刻意不设外键**——管理员被删除后，他做过的操作仍要有据可查。"""

    __tablename__ = "admin_audit_log"
    __table_args__ = (
        Index("idx_admin_audit_ts", "ts"),
        Index("idx_admin_audit_actor", "actor_id", "ts"),
    )

    id: int | None = Field(default=None, primary_key=True)
    actor_id: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    actor_name: str = Field(
        default="", sa_column=Column(Text, nullable=False)
    )
    action: str = Field(sa_column=Column(Text, nullable=False))
    target: str = Field(
        default="", sa_column=Column(Text, nullable=False)
    )
    detail: str = Field(
        default="", sa_column=Column(Text, nullable=False)
    )
    request_id: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    ts: str = Field(sa_column=Column(timestamp_text(), nullable=False))


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

    id: str = Field(sa_column=Column(short_text(64), primary_key=True))
    name: str = Field(
        default="", sa_column=Column(Text, nullable=False)
    )
    protocol: str = Field(
        default="openai_compatible",
        sa_column=Column(Text, nullable=False),
    )
    base_url: str = Field(
        default="", sa_column=Column(Text, nullable=False)
    )
    value_token: str = Field(
        default="", sa_column=Column(Text, nullable=False)
    )
    default_model: str = Field(
        default="", sa_column=Column(Text, nullable=False)
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
        default=False, sa_column=Column(Boolean, nullable=False, server_default=text("false"))
    )
    extra_json: str = Field(
        default="{}", sa_column=Column(Text, nullable=False)
    )
    enabled: bool = Field(
        default=True, sa_column=Column(Boolean, nullable=False, server_default=text("true"))
    )
    sort_order: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    created_at: str = Field(sa_column=Column(timestamp_text(), nullable=False))
    updated_at: str = Field(sa_column=Column(timestamp_text(), nullable=False))


class SystemConfig(SQLModel, table=True):
    """运行时配置的**覆盖项**。没有某一行 = 用 env 或代码默认值。

    这是「升级后行为零变化」的结构保证：「恢复默认」就是删掉那一行。
    """

    __tablename__ = "system_config"

    # ⚠️ `key` 是列名也是属性名：SQLAlchemy 允许（与 `metadata` 不同）
    key: str = Field(sa_column=Column(short_text(64), primary_key=True))
    value: str = Field(sa_column=Column(Text, nullable=False))
    updated_at: str = Field(sa_column=Column(timestamp_text(), nullable=False))
    # 管理员 id；删号后置空，故不加外键
    updated_by: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))


# 布尔列白名单。这不是「一份可以偷懒的清单」，而是一道**显式闸门**：
# 布尔列的 `server_default` 必须写 `true` / `false`，不能写 `0` / `1`——Postgres
# 的 `BOOLEAN` 不接受整数默认值，空库跑基线的第一条建表语句就会报
# 「column is of type boolean but default expression is of type integer」。
# `tests/test_schema_parity.py` 断言「从模型推导出的布尔列集合 == 这份清单」，
# 两头对不上就转红（既防漏登记，也防清单过期）。
BOOLEAN_COLUMNS: frozenset[tuple[str, str]] = frozenset(
    {
        ("users", "is_active"),
        ("users", "is_admin"),
        ("model_providers", "enabled"),
        ("model_providers", "thinking_enabled"),
        ("chat_turns", "interrupted"),
        # 票 #85：部分完成标记，版本行是「这份正文不完整」的真源，任务行是它的镜像。
        ("document_versions", "partial"),
        ("generation_jobs", "partial"),
    }
)


# 供 alembic 与测试引用的全部表模型（顺序与建表无关，SQLAlchemy 自己按外键排序）
#
# ⚠️ 自检放在**最后**：所有表都注册进 metadata 之后才能扫。
# 它拦的是「索引列没有长度上限 / 大文本带 server_default」这两类写法，让错误发生在
# import 时。
assert_metadata_is_portable(SQLModel.metadata)

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
    "JobAttempt",
    "JobEvent",
    "LoginFailure",
    "ModelProvider",
    "Solution",
    "SystemConfig",
    "User",
]
