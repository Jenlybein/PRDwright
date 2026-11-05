"""把索引列改成长度受限的 VARCHAR(n)，并去掉 TEXT 列的字面量默认值。

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-25

## 为什么必须改

MySQL 的两条硬约束，在 SQLite 上**完全看不出来**——SQLite 既不管长度，也不限制
默认值，所以同一份 DDL 在 SQLite 上跑得好好的，到 MySQL 建表就失败：

1. `TEXT` 列不能整列建索引、不能做主键（必须给前缀长度，而前缀唯一 ≠ 全值唯一）；
2. `BLOB/TEXT` 列不接受**字面量**默认值（8.0.13+ 也只接受表达式 `DEFAULT ('')`）。

## 这份清单是算出来的，不是抄的

86 列 = 36 个「被索引但没有长度上限」的列 + 50 个「无长度文本但带默认值」的列。
清单由 `uv run python` 对比「只跑到 0001 的库」与「当前 `core/models.py`」生成，
再贴进本文件。手抄 86 行的漏检率远高于生成。

判据在 `core/types.py::assert_metadata_is_portable`——它在 `models.py` import 时就会
把违规列一次列全，所以这份迁移是「自检通过之后的最小改动集」。

## 每个数字为什么是它

长度来自 `docs/architecture/ARCHITECTURE.md 附录 B`（库结构勘察实测）的最大值向上取档，附录 B §6「由数据推出的候选长度」是列长度取值来源：

- 时间戳 `32`：`utc_now_text()` 产出 26 字符、`utc_ms_text()` 24 字符（带 Z）；
- 枚举（kind / status / stage / role / level）`32`：取值都是 12 字符以内；
- 用户名 `32`：与 `auth_service._USERNAME_MAX` **对齐**。校验与列宽必须是同一个数，
  否则会出现「能注册但存不进去」；
- `token_hash` `64`：sha256 的 hex 长度就是 64，不是估算；
- `request_id` / `code` / `key` / `id` `64`：给系统生成的标识留一倍余量。

## SQLite 上怎么执行

`render_as_batch=True`（`migrations/env.py`）把每个 `alter_column` 变成
「建新表 + 拷数据 + 换名」。14 张表里 13 张会各重建一次，数据保留。
⚠️ 表越大越慢，且需要可写的临时目录空间（`PrivateTmp` 与 `ProtectSystem` 要一起配）。

## 为什么不实现 downgrade

反向操作要把 86 列的默认值逐个还原（`''` / `'[]'` / `'{}'` / `'generated'` /
`'running'` / `'0'` / `'2'`），任何一处对不上，`test_schema_parity` 就会在
「回滚后的库」上报假差异——那种差异比不回滚更难查。
按既有约定：**回滚用备份，不用 downgrade**。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 表 → [(列名, 新的字符长度或 None, 是否移除默认值)]
# `None` 表示「这列不变长度，只是要把默认值去掉」。
_CHANGES: dict[str, list[tuple[str, int | None, bool]]] = {
    "admin_audit_log": [
        ("actor_name", None, True),
        ("target", None, True),
        ("detail", None, True),
        ("ts", 32, False),
    ],
    "app_logs": [
        ("ts", 32, False),
        ("level", 32, False),
        ("request_id", 64, False),
        ("extra_json", None, True),
    ],
    "auth_sessions": [
        ("token_hash", 64, False),
        ("created_at", 32, False),
        ("expires_at", 32, False),
    ],
    "chat_sessions": [
        ("stage", 32, False),
        ("conversation_id", None, True),
        ("updated_at", 32, False),
    ],
    "chat_turns": [
        ("stage", 32, False),
        ("role", 32, False),
        ("content", None, True),
        ("tool_call_id", None, True),
        ("tool_result", None, True),
        ("asks", None, True),
        ("answers", None, True),
        ("tool_calls", None, True),
        ("created_at", 32, False),
    ],
    "document_versions": [
        ("content", None, True),
        ("content_sha", None, True),
        ("pending_items", None, True),
        ("source", None, True),
        ("note", None, True),
        ("metadata", None, True),
        ("created_at", 32, False),
        ("updated_at", 32, False),
    ],
    "documents": [
        ("kind", 32, False),
        ("created_at", 32, False),
        ("updated_at", 32, False),
    ],
    "generation_jobs": [
        ("kind", 32, False),
        ("doc_kind", None, True),
        ("chat_stage", None, True),
        # 这一列既改长度又去默认值：原来是 TEXT + DEFAULT 'running'
        ("status", 32, True),
        ("phase", None, True),
        ("draft_content", None, True),
        ("completed_sections", None, True),
        ("pending_items", None, True),
        ("error_code", None, True),
        ("error_message", None, True),
        ("provider_protocol", None, True),
        ("provider_config_id", None, True),
        ("provider_name", None, True),
        ("provider_model", None, True),
        ("created_at", 32, False),
        ("updated_at", 32, False),
    ],
    "invite_codes": [
        ("code", 64, False),
        ("expires_at", 32, False),
        ("created_at", 32, False),
    ],
    "login_failures": [
        ("username", 32, False),
        ("ip", None, True),
        ("created_at", 32, False),
    ],
    "model_providers": [
        ("id", 64, False),
        ("name", None, True),
        ("protocol", None, True),
        ("base_url", None, True),
        ("value_token", None, True),
        ("default_model", None, True),
        ("extra_json", None, True),
        ("created_at", 32, False),
        ("updated_at", 32, False),
    ],
    "solutions": [
        ("title", None, True),
        ("mode", None, True),
        ("stage", None, True),
        ("created_at", 32, False),
        ("updated_at", 32, False),
        ("content_updated_at", 32, False),
        ("view_state", None, True),
        ("entry_mode", None, True),
        ("chat_tab", None, True),
        ("summary_json", None, True),
        ("quick_product_name", None, True),
        ("quick_requirements", None, True),
        ("draft_prd_text", None, True),
        ("paste_prd_text", None, True),
        ("paste_api_docs_text", None, True),
        ("active_job_kind", None, True),
        ("active_job_doc_kind", None, True),
    ],
    "system_config": [
        ("key", 64, False),
        ("updated_at", 32, False),
    ],
    "users": [
        ("username", 32, False),
        ("created_at", 32, False),
    ],
}


# ⚠️ batch 重建会让 `sqlite_autoincrement` 丢失。
#
# 它不是列定义的一部分，而是建表语句末尾的 `AUTOINCREMENT` 关键字，**反射读不到**。
# 重建出来的新表没有它，SQLite 就会复用「已被删除的最大 rowid」——
# `app_logs` / `admin_audit_log` 的 id 被当作翻页游标，复用会让「下一页」看见已删除的行；
# `test_deleted_max_id_is_not_reused` 会立刻红。
#
# 所以重建时必须显式把表级参数再声明一次。这份名单是「有自增主键」的表全集。
_AUTOINCREMENT_TABLES = frozenset(
    {
        "admin_audit_log",
        "app_logs",
        "chat_sessions",
        "chat_turns",
        "document_versions",
        "documents",
        "generation_jobs",
        "login_failures",
        "solutions",
        "users",
    }
)


def upgrade() -> None:
    """逐表 batch 重建。每张表只重建一次——把该表所有变更塞进同一个 batch。"""
    for table in sorted(_CHANGES):
        with op.batch_alter_table(
            table,
            schema=None,
            # 自增要显式声明回来，否则重建后的表会复用已删除的 id（见上方注释）
            table_kwargs=(
                {"sqlite_autoincrement": True}
                if table in _AUTOINCREMENT_TABLES
                else {}
            ),
        ) as batch_op:
            for column, length, drop_default in _CHANGES[table]:
                kwargs: dict = {"existing_type": sa.Text()}
                if length is not None:
                    kwargs["type_"] = sa.String(length)
                if drop_default:
                    # 显式传 None = 移除默认值；不传则保持原样
                    kwargs["server_default"] = None
                batch_op.alter_column(column, **kwargs)


def downgrade() -> None:
    """不回滚。理由见模块 docstring 最后一节：回滚用备份。"""
    raise NotImplementedError(
        "0002 不支持 downgrade：它改了 86 列的类型与默认值，反向还原无法保证逐列一致。"
        "回滚请恢复备份。"
    )
