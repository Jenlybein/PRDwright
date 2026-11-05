"""给 `generation_jobs` 补上「任务单不可变数据」的投影列（票 #34）。

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-05

## 加的是哪三块

生成任务的 DB 行要成为任务单里**不可变数据**的完整投影，因此建任务那一刻
一次写全：

| 列 | 内容 | 类型 |
| --- | --- | --- |
| `request_params` | 摊平后的原始请求参数（JSON 对象） | `long_text()` |
| `config_snapshot` | 配置/供应商快照（JSON 对象） | `long_text()` |
| `skill_refs` | 技能包身份清单（JSON 数组） | `long_text()` |
| `skill_manifest` | 主技能包的可索引短身份 | `short_text(64)` |

运行态仍以内存运行时为权威、按 300ms / 4KB 的节奏落进度；这四列**只在建任务时
写一次**，收尾不再改写，也就不会从 `summary` 那条路径派生出第二份供应商四列。

## 为什么手写而不是 autogenerate

autogenerate 在 SQLite 的 batch 重建里会反射不出表级参数，写出
`drop_constraint(None)` 而执行失败（见 0003 / 0004 的说明）。加列是**新增列，
不是删列/退役列那种单向门**（回滚靠备份仍是约定），但迁移同样只写明确的语句。

## 类型与方言纪律

列类型统一走 `core/types.py`：大段文本用 `long_text()`（SQLite `TEXT` /
遗留 MySQL `MEDIUMTEXT`），可索引短串用 `short_text(64)`（≤768）。
`assert_metadata_is_portable` 在 `models.py` import 时自检：索引列必须有长度、
无长度文本不得带字面量 `server_default`。因此这里**不给任何新列设默认值**，
旧行的值由下面的 `UPDATE` 回填，之后才收紧成 `NOT NULL`。

## 为什么分三步

SQLite 允许 `ALTER TABLE ... ADD COLUMN`，但对已有行的 `NOT NULL` 新列必须给值。
先补可空列、再回填、再收紧非空，才能让「旧库升上来」与「空库一次建到 head」
得到同一份结构；`test_schema_parity` 与 `assert_schema_matches_models` 都按
语义快照对拍，不认建表语句文本。

## 为什么再声明 `sqlite_autoincrement`

`generation_jobs` 在 `0002::_AUTOINCREMENT_TABLES` 里：batch 重建 = 建新表 + 拷数据
+ 换名，反射读不到 `AUTOINCREMENT` 关键字。不显式声明回来，重建后会复用已删除的最大
rowid（翻页游标会看见旧行）。沿用 0002 / 0003 的同一处置。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from backend.core.types import long_text, short_text

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# `generation_jobs` 有自增主键；batch 重建必须把 AUTOINCREMENT 声明回来。
_TABLE_KWARGS = {"sqlite_autoincrement": True}

# 回填值用「空 JSON」而不是空串：读侧按对象/数组解析，坏数据一律退化成空容器。
_BACKFILL = (
    ("request_params", "'{}'"),
    ("config_snapshot", "'{}'"),
    ("skill_refs", "'[]'"),
    ("skill_manifest", "''"),
)


def upgrade() -> None:
    """补四列 + 一个索引，回填旧行，最后收紧成 `NOT NULL`。"""
    with op.batch_alter_table(
        "generation_jobs", schema=None, table_kwargs=_TABLE_KWARGS
    ) as batch_op:
        batch_op.add_column(sa.Column("request_params", long_text(), nullable=True))
        batch_op.add_column(sa.Column("config_snapshot", long_text(), nullable=True))
        batch_op.add_column(sa.Column("skill_refs", long_text(), nullable=True))
        batch_op.add_column(sa.Column("skill_manifest", short_text(64), nullable=True))
        batch_op.create_index(
            "idx_generation_jobs_skill_manifest", ["skill_manifest"], unique=False
        )

    for column, value in _BACKFILL:
        op.execute(
            f"UPDATE generation_jobs SET {column} = {value} WHERE {column} IS NULL"
        )

    with op.batch_alter_table(
        "generation_jobs", schema=None, table_kwargs=_TABLE_KWARGS
    ) as batch_op:
        batch_op.alter_column(
            "request_params", existing_type=sa.Text(), nullable=False
        )
        batch_op.alter_column(
            "config_snapshot", existing_type=sa.Text(), nullable=False
        )
        batch_op.alter_column("skill_refs", existing_type=sa.Text(), nullable=False)
        batch_op.alter_column(
            "skill_manifest", existing_type=sa.String(64), nullable=False
        )


def downgrade() -> None:
    """反操作：删掉索引与四列（加列不是单向门，结构可以完整退回 0004）。"""
    with op.batch_alter_table(
        "generation_jobs", schema=None, table_kwargs=_TABLE_KWARGS
    ) as batch_op:
        batch_op.drop_index("idx_generation_jobs_skill_manifest")
        batch_op.drop_column("skill_manifest")
        batch_op.drop_column("skill_refs")
        batch_op.drop_column("config_snapshot")
        batch_op.drop_column("request_params")
