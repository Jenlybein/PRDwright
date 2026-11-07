"""给 `document_versions` 补上「审核结论」列（票 #52）。

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-07

## 加的是哪一列

| 列 | 内容 | 类型 |
| --- | --- | --- |
| `review` | 这一版正文的审核结论（JSON 文本：判定 / 逐条问题 / 总结 / 是否改写 / 失败章节 / 审核时间） | `TEXT`，可空 |

结论挂在**被审的那一版正文**上，一版一条；重新审核覆盖同一条（不追加版本）。
**审核没跑成就不写**：没有结论不等于通过，界面据此标「审核失败」。

## 为什么可空、不回填

旧文档与旧版本从未存过结论，回填只能编一个值——那会把「未审核」伪装成
「审过了」。可空列让读侧天然得到「未审核」，也符合 ADR 0002 的后果：
**迁移只加列（nullable），不回填历史**。

## 为什么手写而不是 autogenerate

autogenerate 在 SQLite 的 batch 重建里会反射不到表级参数，写出
`drop_constraint(None)` 而执行失败（见 0003 / 0004 / 0005 的说明）。
这一列是**新增列**（加列不是单向门：回滚靠备份仍是约定），只写一条明确的
`ALTER TABLE ... ADD COLUMN`，不重建表也就不会丢 `sqlite_autoincrement`。

## 类型与方言纪律

`TEXT` 列不能带字面量 `server_default`（MySQL 会报 can't have a default value），
所以这里只声明可空、不给默认值。列类型与模型里的 `Column(Text, nullable=True)`
逐项一致，`test_schema_parity` 会按语义快照对拍。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """加一列可空的 `review`（旧行为 NULL = 未审核）。"""
    op.add_column(
        "document_versions",
        sa.Column("review", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """反操作：删掉这一列（结构可完整退回 0005）。"""
    with op.batch_alter_table(
        "document_versions", schema=None, table_kwargs={"sqlite_autoincrement": True}
    ) as batch_op:
        batch_op.drop_column("review")
