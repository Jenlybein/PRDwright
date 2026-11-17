"""失败退避列 `next_run_at`（票 #88）。

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-17

## 这条迁移做什么

给 `generation_jobs` 加**一列**：`next_run_at`。它是「这条任务最早什么时候可以被
下一个执行者领走」——可重试失败回排队时写「失败时刻 + 按错误类型算出的退避时长」，
到期之前抢占条件一行都改不到。

| 列 | 类型 | 默认 | 语义 |
|---|---|---|---|
| `next_run_at` | `VARCHAR(32)` | `''` | 最早可被领走的时刻（ISO 8601 文本，与 `created_at` 同一格式） |

空串表示「立即」——不是「没有值」，而是「现在就到期」。这样抢占条件里的「到期判据」
只有一个形状：`next_run_at = '' OR next_run_at <= now`。若允许 `NULL`，每一处判据都要
多写一条 `IS NULL` 分支，而漏写任何一处都不会报错、只会让那一行永远领不走。

## 为什么是文本而不是 `TIMESTAMPTZ`

与全库口径一致（`core/types.py::timestamp_text`）：ISO 8601 文本的字典序 = 时间序，
可以直接进 `WHERE next_run_at <= ?` 做条件写（判据与修改在同一条 SQL 里），不必把时间
取回 Python 再判。改成真时间类型会同时改变落盘格式、精度与时区解释，代价远大于收益。

## 为什么默认值是空串而不是「建行即到期」之外的东西

建行（登记）与「可以领走」是两件事，但**新建的这一刻两者都是真的**：任务刚被接单，
没有失败过，可以立即执行。空串把这件事表达成「立即到期」，语义与「没有退避」一致。

## 级别：本列不参与索引

本期唯一的扫描形态是「`status='running'` 且没有有效租约 且 已到期」，`status` 上已有
`idx_generation_jobs_recover`；排队规模在本期（单 worker、按方案建任务）远不到需要
一条专门索引的量级。真正的调度循环是 #89，等它落地时按那时的查询补索引，现在先加就是
给一条还没成型的查询做假设。

## 与基线 / 前序迁移的关系

`0001`~`0005` 一字不动；本迁移是独立的一条增量，`down_revision = "0005"`。空库跑
`upgrade head` 会依次执行六条，最终结构与 `core/models.py` 完全一致
（`test_schema_migration` 的「基线库 == 模型库」用例守着这一点）。

## 降级

`downgrade` 删掉这一列即可：它是纯增量，没有别的表引用它。旧代码不读 `next_run_at`，
删掉之后任务行回到「状态 + 载荷 + 租约」的老形状，行为与 #88 之前一致。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: Union[str, Sequence[str], None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 这一列的宽度：与 `core/types.py::timestamp_text()` 一致（默认 VARCHAR(32)）。
_NEXT_RUN_CHARS = 32


def upgrade() -> None:
    """Upgrade schema."""
    # 默认值给空串：既有行（以及任何裸 SQL 省略这一列的写）都落在「立即到期」，
    # 而不是让 NOT NULL 约束把一次正常的插入判死。
    op.add_column(
        "generation_jobs",
        sa.Column(
            "next_run_at",
            sa.String(length=_NEXT_RUN_CHARS),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("generation_jobs", "next_run_at")
