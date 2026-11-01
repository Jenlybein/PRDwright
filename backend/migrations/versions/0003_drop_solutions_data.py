"""删除 `solutions.data`（v8 / ADR-096）。

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-02

## 为什么手写而不是 autogenerate

autogenerate 能识别出「模型少了这一列」，但它同时会把 batch 重建里**反射不到**的东西
写成 `drop_constraint(None)`（自增关键字、部分索引条件都属此类），执行时直接失败。
删列是不可逆动作，迁移应当只有一条明确的语句：手写在 review 里看得见。

## 为什么必须 batch

SQLite 的 `ALTER TABLE` 不支持 `DROP COLUMN`（3.35 起有语法，但仍有「列被索引/约束引用」
等限制）。`migrations/env.py` 已开 `render_as_batch=True`，这里仍显式写
`batch_alter_table`：这条迁移只做一件事，把「重建表」这个昂贵动作写在明面上。

## 为什么再声明 `sqlite_autoincrement`

batch 重建 = 建新表 + 拷数据 + 换名。`AUTOINCREMENT` 是建表语句末尾的关键字，
**反射读不到**；不显式声明回来，重建后的 `solutions.id` 会复用已删除的最大 rowid。
`0002_typed_columns.py` 已经踩过同一个坑，这里沿用同一处置。

## 回滚

按 ADR-096 / ADR-057：删列不可逆，生产回滚方式是**恢复备份**，不是 `git revert`。
`downgrade` 仍写了真正的反操作（把列补回来），但它只用于结构演练与对拍，
不承担生产回滚——旧快照的字节只在删除前的备份里。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# `solutions` 有自增主键；batch 重建必须把 AUTOINCREMENT 声明回来（见模块 docstring）。
_TABLE_KWARGS = {"sqlite_autoincrement": True}


def upgrade() -> None:
    """删掉退役列 `data`。"""
    with op.batch_alter_table(
        "solutions", schema=None, table_kwargs=_TABLE_KWARGS
    ) as batch_op:
        batch_op.drop_column("data")


def downgrade() -> None:
    """反操作：把 `data` 补回成「`TEXT NOT NULL`、无默认值」。

    分三步而不是一步 `add_column(..., nullable=False, server_default=...)`：

    1. 先补**可空**列——此刻旧行还没有值；
    2. 把旧行填成空串（与退役期间 `create` 写的值一致）；
    3. 再加回 `NOT NULL`。

    不用 `server_default` 是有意的：MySQL 的 `TEXT` 不接受字面量默认值，而原列本来
    就没有默认值。三步走完的结构才是 `0002` 时的形状。
    """
    with op.batch_alter_table(
        "solutions", schema=None, table_kwargs=_TABLE_KWARGS
    ) as batch_op:
        batch_op.add_column(sa.Column("data", sa.Text(), nullable=True))

    op.execute("UPDATE solutions SET data = '' WHERE data IS NULL")

    with op.batch_alter_table(
        "solutions", schema=None, table_kwargs=_TABLE_KWARGS
    ) as batch_op:
        batch_op.alter_column("data", existing_type=sa.Text(), nullable=False)
