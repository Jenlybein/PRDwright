"""给 `document_versions` 补上「审核失败时间」列（票 #51 评审修复 / US3）。

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-08

## 加的是哪一列

| 列 | 内容 | 类型 |
| --- | --- | --- |
| `review_failed_at` | 这一版**上一次重审没跑成**的时间（UTC ISO 8601 文本） | `TEXT`，可空 |

## 为什么失败要落库

重审的**唯一产出就是判定**，因此「没审成」本身就是一条事实（C-R1：没有结论 ≠ 通过）。
从前这条事实只活在会话内存里，刷新后界面退化成「未审核」——用户再也看不出这份正文
被审过、且没审成。落库之后它和结论同一行、同一版，刷新仍读得到。

`NULL` = 没有失败记录（旧数据、从未失败、或拿到新结论后被清空），因此**不回填历史**，
与 0006 的 `review` 同口径。

## 为什么手写而不是 autogenerate

autogenerate 在 SQLite 的 batch 重建里会反射不到表级参数，写出 `drop_constraint(None)`
而执行失败（见 0003 / 0004 / 0005 / 0006 的说明）。这一列是**新增列**（加列不是单向门：
回滚靠备份仍是约定），只写一条明确的 `ALTER TABLE ... ADD COLUMN`，不重建表也就不会
丢 `sqlite_autoincrement`。

## 类型与方言纪律

`TEXT` 列不能带字面量 `server_default`（MySQL 会报 can't have a default value），
所以这里只声明可空、不给默认值。列类型与模型里的 `Column(Text, nullable=True)` 逐项一致，
`test_schema_parity` 会按语义快照对拍。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """加一列可空的 `review_failed_at`（旧行为 NULL = 没有失败记录）。"""
    op.add_column(
        "document_versions",
        sa.Column("review_failed_at", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """反操作：删掉这一列（结构可完整退回 0006）。"""
    with op.batch_alter_table(
        "document_versions", schema=None, table_kwargs={"sqlite_autoincrement": True}
    ) as batch_op:
        batch_op.drop_column("review_failed_at")
