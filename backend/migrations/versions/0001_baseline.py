"""基线迁移：当前结构的**唯一** Postgres 基线（v9 / #78）。

Revision ID: 0001
Revises:
Create Date: 2026-11-17

## 为什么只有一份

#78 把 0001~0007 合并成这一条干净基线：从空库一路 `upgrade head` 就能到当前结构，
中间没有「只为 SQLite 存在」的写法（`batch_alter_table`、`sqlite_autoincrement`、
布尔默认值 `0/1`、`sqlite_where` 部分索引）——那些在 Postgres 上要么是语法错误，
要么是被静默忽略的死配置。历史七条迁移里只有 `0006`/`0007` 是纯 `add_column`，
其余都是「把 SQLite 的旧写法搬到新写法」，合并掉不损失任何信息：模型（`core/models.py`）
才是结构真源，基线由它 autogenerate 而来。

## 已有库怎么处理

- 空库：直接本基线建全量；
- 结构已是当前形状、但没有 `alembic_version`：`prepare_schema()` 先做一次结构对拍，
  一致就 `stamp head`（一行 DDL 都不执行），不一致就拒绝启动；
- 旧的 SQLite 库 / 旧 `DB_PATH`：在 `core/database.py` 就被拒绝，服务根本不会起来
  （本项目自 v9 起只支持 Postgres，不做兼容垫片）。

## 三个必须知道的点

1. **环形外键**：`documents.current_version_id -> document_versions.id` 与
   `document_versions.document_id -> documents.id` 互相指向。Postgres 建表当场校验
   外键目标，所以先建 `documents`（指针列留空、不声明那条外键）→ 再建
   `document_versions` → 最后 `create_foreign_key` 回填。插入顺序同理：先插
   `documents`（指针留空）→ 再插版本 → 最后回填指针，三步必须同一个事务。
2. **索引不带 `DESC`**：Postgres 能反向扫描 B-tree，`INDEX(a, b)` 对
   `ORDER BY b DESC` 同样有效；带 `DESC` 的表达式索引反射出来与模型声明不是同一份
   形状，结构对拍会出现幻影差异。
3. **布尔列的默认值是 `true` / `false` 字面量**：Postgres 的 `BOOLEAN` 不接受整数
   默认值（`DEFAULT 0` 会报 "column is of type boolean but default expression is
   of type integer"）。部分索引 `ux_chat_turns_job_role` 用 `postgresql_where` 声明
   WHERE——不带它时 Postgres 会静默建成一条全量唯一索引。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 环形外键的名字写死：downgrade 要按名字删它。
_DOCUMENTS_CURRENT_VERSION_FK = "fk_documents_current_version"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "admin_audit_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("actor_name", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.Column("ts", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_admin_audit_actor", "admin_audit_log", ["actor_id", "ts"], unique=False)
    op.create_index("idx_admin_audit_ts", "admin_audit_log", ["ts"], unique=False)

    op.create_table(
        "app_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ts", sa.String(length=32), nullable=False),
        sa.Column("level", sa.String(length=32), nullable=False),
        sa.Column("logger", sa.Text(), nullable=False),
        sa.Column("msg", sa.Text(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("event", sa.Text(), nullable=True),
        sa.Column("extra_json", sa.Text(), nullable=False),
        sa.Column("exc", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_app_logs_level", "app_logs", ["level", "ts"], unique=False)
    op.create_index("idx_app_logs_request", "app_logs", ["request_id"], unique=False)
    op.create_index("idx_app_logs_ts", "app_logs", ["ts"], unique=False)

    op.create_table(
        "invite_codes",
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("max_uses", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("used_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("expires_at", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("code"),
    )

    op.create_table(
        "login_failures",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=32), nullable=False),
        sa.Column("ip", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_login_failures_created", "login_failures", ["created_at"], unique=False)
    op.create_index(
        "idx_login_failures_user", "login_failures", ["username", "created_at"], unique=False
    )

    op.create_table(
        "model_providers",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("protocol", sa.Text(), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("value_token", sa.Text(), nullable=False),
        sa.Column("default_model", sa.Text(), nullable=False),
        sa.Column("max_tokens", sa.Integer(), server_default=sa.text("4096"), nullable=False),
        sa.Column("context_window", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_output", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "thinking_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("extra_json", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.Column("updated_at", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "system_config",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.String(length=32), nullable=False),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("key"),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=32), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("salt", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("is_admin", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("daily_task_limit", sa.Integer(), server_default=sa.text("-1"), nullable=False),
        sa.Column("daily_token_limit", sa.Integer(), server_default=sa.text("-1"), nullable=False),
        sa.Column("max_running", sa.Integer(), server_default=sa.text("-1"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )

    op.create_table(
        "auth_sessions",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    op.create_index("idx_auth_sessions_expires", "auth_sessions", ["expires_at"], unique=False)
    op.create_index("idx_auth_sessions_user", "auth_sessions", ["user_id"], unique=False)

    op.create_table(
        "solutions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.Column("updated_at", sa.String(length=32), nullable=False),
        sa.Column(
            "content_updated_at",
            sa.String(length=32),
            server_default=sa.text("''"),
            nullable=False,
        ),
        sa.Column("view_state", sa.Text(), nullable=False),
        sa.Column("entry_mode", sa.Text(), nullable=False),
        sa.Column("chat_tab", sa.Text(), nullable=False),
        sa.Column("summary_json", sa.Text(), nullable=False),
        sa.Column("quick_product_name", sa.Text(), nullable=False),
        sa.Column("quick_requirements", sa.Text(), nullable=False),
        sa.Column("draft_prd_text", sa.Text(), nullable=False),
        sa.Column("paste_prd_text", sa.Text(), nullable=False),
        sa.Column("paste_api_docs_text", sa.Text(), nullable=False),
        sa.Column("active_job_kind", sa.Text(), nullable=False),
        sa.Column("active_job_doc_kind", sa.Text(), nullable=False),
        sa.Column("active_job_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_solutions_owner", "solutions", ["user_id", "updated_at"], unique=False)
    op.create_index(
        "idx_solutions_owner_content", "solutions", ["user_id", "content_updated_at"], unique=False
    )

    # 环形外键的另一半：`documents.current_version_id -> document_versions.id` 延后建，
    # 因为 `document_versions` 在它之后才建。指针列先留空（nullable），最后回填外键。
    op.create_table(
        "documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("solution_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.Column("updated_at", sa.String(length=32), nullable=False),
        sa.Column("current_version_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["solution_id"], ["solutions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("solution_id", "kind"),
    )

    op.create_table(
        "document_versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_sha", sa.Text(), nullable=False),
        sa.Column("pending_items", sa.Text(), nullable=False),
        sa.Column("last_run_summary", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("metadata", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.Column("updated_at", sa.String(length=32), nullable=False),
        sa.Column("review", sa.Text(), nullable=True),
        sa.Column("review_failed_at", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "version_no"),
    )

    op.create_foreign_key(
        _DOCUMENTS_CURRENT_VERSION_FK,
        "documents",
        "document_versions",
        ["current_version_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "generation_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("solution_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("doc_kind", sa.Text(), nullable=False),
        sa.Column("chat_stage", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("draft_content", sa.Text(), nullable=False),
        sa.Column("completed_sections", sa.Text(), nullable=False),
        sa.Column("pending_items", sa.Text(), nullable=False),
        sa.Column("input_fingerprint", sa.Text(), nullable=True),
        sa.Column("usage", sa.Text(), nullable=True),
        sa.Column(
            "max_provider_switches", sa.Integer(), server_default=sa.text("2"), nullable=False
        ),
        sa.Column("error_code", sa.Text(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "completion_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("provider_protocol", sa.Text(), nullable=False),
        sa.Column("provider_config_id", sa.Text(), nullable=False),
        sa.Column("provider_name", sa.Text(), nullable=False),
        sa.Column("provider_model", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.Column("updated_at", sa.String(length=32), nullable=False),
        sa.Column("request_params", sa.Text(), nullable=False),
        sa.Column("config_snapshot", sa.Text(), nullable=False),
        sa.Column("skill_refs", sa.Text(), nullable=False),
        sa.Column("skill_manifest", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_generation_jobs_owner", "generation_jobs", ["owner_id", "created_at"], unique=False
    )
    op.create_index(
        "idx_generation_jobs_recover",
        "generation_jobs",
        ["status", "owner_id", "kind"],
        unique=False,
    )
    op.create_index(
        "idx_generation_jobs_skill_manifest",
        "generation_jobs",
        ["skill_manifest"],
        unique=False,
    )

    op.create_table(
        "chat_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("solution_id", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("conversation_id", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["solution_id"], ["solutions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ux_chat_sessions_thread", "chat_sessions", ["solution_id", "stage"], unique=True
    )

    op.create_table(
        "chat_turns",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("solution_id", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tool_call_id", sa.Text(), nullable=False),
        sa.Column("tool_result", sa.Text(), nullable=False),
        sa.Column("asks", sa.Text(), nullable=False),
        sa.Column("answers", sa.Text(), nullable=False),
        sa.Column("tool_calls", sa.Text(), nullable=False),
        sa.Column("interrupted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["solution_id"], ["solutions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_chat_turns_thread", "chat_turns", ["solution_id", "stage", "id"], unique=False
    )
    op.create_index(
        "ux_chat_turns_job_role",
        "chat_turns",
        ["job_id", "role"],
        unique=True,
        postgresql_where=sa.text("job_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Downgrade schema.

    ⚠️ 本项目的迁移纪律是「删列不可逆、回滚靠备份」（见 docs/agents/conventions.md）。
    这条 downgrade 只是让「空库 → 基线」这条路径可逆，不是生产回滚手段。
    """
    op.drop_index("ux_chat_turns_job_role", table_name="chat_turns")
    op.drop_index("idx_chat_turns_thread", table_name="chat_turns")
    op.drop_table("chat_turns")
    op.drop_index("ux_chat_sessions_thread", table_name="chat_sessions")
    op.drop_table("chat_sessions")
    op.drop_index("idx_generation_jobs_skill_manifest", table_name="generation_jobs")
    op.drop_index("idx_generation_jobs_recover", table_name="generation_jobs")
    op.drop_index("idx_generation_jobs_owner", table_name="generation_jobs")
    op.drop_table("generation_jobs")
    # 先拆环形外键，才能按依赖顺序删表
    op.drop_constraint(_DOCUMENTS_CURRENT_VERSION_FK, "documents", type_="foreignkey")
    op.drop_table("document_versions")
    op.drop_table("documents")
    op.drop_index("idx_solutions_owner_content", table_name="solutions")
    op.drop_index("idx_solutions_owner", table_name="solutions")
    op.drop_table("solutions")
    op.drop_index("idx_auth_sessions_user", table_name="auth_sessions")
    op.drop_index("idx_auth_sessions_expires", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_table("users")
    op.drop_table("system_config")
    op.drop_table("model_providers")
    op.drop_index("idx_login_failures_user", table_name="login_failures")
    op.drop_index("idx_login_failures_created", table_name="login_failures")
    op.drop_table("login_failures")
    op.drop_table("invite_codes")
    op.drop_index("idx_app_logs_ts", table_name="app_logs")
    op.drop_index("idx_app_logs_request", table_name="app_logs")
    op.drop_index("idx_app_logs_level", table_name="app_logs")
    op.drop_table("app_logs")
    op.drop_index("idx_admin_audit_ts", table_name="admin_audit_log")
    op.drop_index("idx_admin_audit_actor", table_name="admin_audit_log")
    op.drop_table("admin_audit_log")
