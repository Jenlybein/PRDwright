"""删除 `model_providers.builtin`（v8 / ADR-096）。

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-02

## 为什么手写而不是 autogenerate

autogenerate 能识别出「模型少了这一列」，但它同时会把 batch 重建里**反射不到**的东西
写成 `drop_constraint(None)`（自增关键字、部分索引条件都属此类），执行时直接失败。
删列是不可逆动作，迁移应当只有一条明确的语句：手写在 review 里看得见。

## 为什么必须 batch

SQLite 的 `ALTER TABLE` 不支持 `DROP COLUMN`（3.35 起有语法，但仍有「列被索引/约束引用」
等限制）。`migrations/env.py` 已开 `render_as_batch=True`，这里仍显式写
`batch_alter_table`：这条迁移只做一件事，把「重建表」这个昂贵动作写在明面上。

## 为什么不声明 `sqlite_autoincrement`

batch 重建 = 建新表 + 拷数据 + 换名，建表语句尾部的表选项要按原样声明回来。
但 `model_providers` 的主键 `id` 是 `VARCHAR(64)`（业务生成的 `cfg_xxx`），
**不是** `INTEGER PRIMARY KEY`——`AUTOINCREMENT` 在 SQLite 上只对自增整数主键有意义，
写在这张表上不会进 DDL，只是给 review 制造「这张表也有自增 id」的错觉。
`0002_typed_columns.py::_AUTOINCREMENT_TABLES` 同样没有登记这张表，这里跟随同一处置。

## 回滚

按 ADR-096 / ADR-057：删列不可逆，生产回滚方式是**恢复备份**，不是 `git revert`。
`downgrade` 仍写了真正的反操作（把列补回来），但它只用于结构演练与对拍，
不承担生产回滚——旧快照的字节只在删除前的备份里。

反操作要还原成 `0001_baseline.py` 里的原形状：`BOOLEAN NOT NULL DEFAULT 0`。
分三步而不是一步 `add_column(..., nullable=False, server_default=...)`：

1. 先补**可空**列——此刻旧行还没有值；
2. 把旧行填 0（与退役期间的值一致）；
3. 再加回 `NOT NULL` 与原来的 `DEFAULT 0`。

三步走完，`PRAGMA table_info` 读出的形状才与 `0003` 时的库逐列相等。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """删掉退役列 `builtin`。"""
    with op.batch_alter_table("model_providers", schema=None) as batch_op:
        batch_op.drop_column("builtin")


def downgrade() -> None:
    """反操作：把 `builtin` 补回成 `0001` 的 `BOOLEAN NOT NULL DEFAULT 0`。"""
    with op.batch_alter_table("model_providers", schema=None) as batch_op:
        batch_op.add_column(sa.Column("builtin", sa.Boolean(), nullable=True))

    op.execute("UPDATE model_providers SET builtin = 0 WHERE builtin IS NULL")

    with op.batch_alter_table("model_providers", schema=None) as batch_op:
        batch_op.alter_column(
            "builtin",
            existing_type=sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        )
