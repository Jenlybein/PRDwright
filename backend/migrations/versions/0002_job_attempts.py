"""执行账本表 `job_attempts`（票 #80）。

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-17

## 这条迁移做什么

只加一张表、不加列、不改任何既有表：`job_attempts` 记「每次已结束的执行」一行——
第几次、成败、为什么、花多少、跑多久。它是审计数据，写入口唯一（状态机收尾，
与任务状态更新同一事务），读取面在管理端/排查侧（本期不进用户界面）。

## 为什么序号是唯一约束而不是自增列

`attempt_no` 是**同一任务内**的序号（1 起、不重复），与 `job_id` 组成唯一约束
`ux_job_attempts_job_attempt`：分配由仓储在同一事务里算 `MAX+1`，唯一约束是并发下
「重复记账」的数据库级兜底——与 `document_versions` 的 `(document_id, version_no)`
同一个套路（单进程下靠它兜住重试/重连这类重复写入）。

## 为什么外键是 CASCADE 且不存 owner_id

账本是任务行的**附属审计**：任务行随账号删除一起消失时，账本跟着消失才是对的
（否则库里会留下指向不存在任务的孤儿账）。归属一律经任务行 join，不另存一份
`owner_id`——两份归属真源迟早会不一致。

## 与基线的关系

基线（`0001_baseline.py`）保持原样不动（它是「从空库到当前结构」的那一条）；
本迁移是独立的一条增量，`down_revision = "0001"`。空库跑 `upgrade head` 会依次
执行这两条，最终结构与 `core/models.py` 完全一致（`test_schema_migration` 的
「基线库 == 模型库」用例守着这一点）。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "job_attempts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        # 结论取值：completed / failed / cancelled（短文本，索引不涉及它，仍按全局纪律给长度）
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=False),
        sa.Column("error_summary", sa.Text(), nullable=False),
        sa.Column(
            "prompt_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "completion_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["generation_jobs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        # 同一任务内账本序号唯一：重试 / 重连写重复时由数据库裁决（不是「语句没报错」）
        sa.UniqueConstraint("job_id", "attempt_no", name="ux_job_attempts_job_attempt"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("job_attempts")
