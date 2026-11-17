"""生成过程事件表 `job_events`（票 #86）。

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-17

## 这条迁移做什么

只加一张表、不加列、不改任何既有表：`job_events` 记「生成过程中的一条事件」——
全局自增游标、任务 id、事件种类、事件体（JSON）、发生时间。

## 为什么是「日志表 + 全局游标」

订阅要回答的问题只有一个：「客户端手里那条之后还有什么」。因此主键 `id` 必须是
**全局自增**的游标（`BIGSERIAL`），查询形态固定为 `WHERE job_id = ? AND id > ?`
（索引 `idx_job_events_job_cursor`）。用「每任务一个序号」做游标会多一次
`MAX(seq)` 读、并且并发写入时要自己保证唯一，徒增一个能错的点。

## 为什么外键是 CASCADE 且不存 owner_id

事件日志是**任务行的附属过程记录**：任务行随账号删除一起消失时，日志跟着消失才是
对的（否则库里会留下指向不存在任务的孤儿行）。归属一律经任务行 join，不另存
`owner_id`——两份归属真源迟早会不一致。

## 与基线 / 前序迁移的关系

`0001`~`0004` 一字不动；本迁移是独立的一条增量，`down_revision = "0004"`。空库跑
`upgrade head` 会依次执行五条，最终结构与 `core/models.py` 完全一致
（`test_schema_migration` / `test_schema_parity` 守着这一点）。

## 保留策略与清理

过程日志终态后保留 7 天，清理单元是 `services/job_events.purge_expired_events`
（本票只交付这个可直接调用的动作，不起后台定时器、不接调度循环——那是 #89）。
清理**只删本表**：权威正文在 `generation_jobs.draft_content` 与
`document_versions.content` 上，与事件日志无关。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: Union[str, Sequence[str], None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "job_events",
        # 全局自增游标：BIGSERIAL（`autoincrement=True` 在 Postgres 上渲染成 BIGSERIAL）
        sa.Column("id", sa.BigInteger(), nullable=False, autoincrement=True),
        sa.Column("job_id", sa.Integer(), nullable=False),
        # 事件种类：与 SSE 的 `event:` 同名，短文本（全部取值远小于 32 字符）
        sa.Column("kind", sa.String(length=32), nullable=False),
        # 事件体 JSON：{content, data, code}；**不给 server_default**（大文本纪律）
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["generation_jobs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # 订阅的唯一查询形态：按任务取「比游标新」的一段
    op.create_index("idx_job_events_job_cursor", "job_events", ["job_id", "id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("idx_job_events_job_cursor", table_name="job_events")
    op.drop_table("job_events")
