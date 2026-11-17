"""执行权（租约）三列：`lease_owner` / `lease_token` / `lease_expire_at`（票 #82）。

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-17

## 这条迁移做什么

给 `generation_jobs` 加三列，把「谁在执行」从进程内存搬进数据库，并让它**会到期**：

| 列 | 类型 | 默认 | 语义 |
|---|---|---|---|
| `lease_owner` | `VARCHAR(64)` | `''` | 谁领的（`主机/进程`），**仅供排查**，不参与判权 |
| `lease_token` | `VARCHAR(64)` | `''` | 本轮凭证，逐轮随机，**参与判权** |
| `lease_expire_at` | `VARCHAR(32)` | `''` | 到期时间（ISO 8601 文本，与 `created_at` 同一格式） |

空串表示「没有人持有有效租约」——在这张票的三态形态里就是「排队中」（七态是 #84 的事，
本迁移**不**引入新的 status 取值，前端契约一个字不变）。

## 为什么到期时间是文本而不是 `TIMESTAMPTZ`

与全库口径一致（`core/types.py::timestamp_text`）：ISO 8601 文本的字典序 = 时间序，
可以直接进 `WHERE lease_expire_at < ?` 与 `>= ?` 做条件写，不必把时间取回 Python 判。
改成真时间类型会同时改变落盘格式、精度与时区解释，代价远大于收益。

## 为什么默认值是空串而不是「建行即到期」

建行（登记）与领走（抢占）是两件事：登记完成却没人领之前，这一行不该看起来像
「有人正在跑」。空串让「没有有效租约」有一个**明确**的表达，抢占条件因此可以写成
`lease_token = '' OR lease_expire_at < now`——判断与修改压在同一条 UPDATE 里。

`lease_owner` / `lease_token` 与 `lease_expire_at` 都不建索引：本期唯一的扫描方式是
「找 status='running' 且没有有效租约的行」，`status` 上已有 `idx_generation_jobs_recover`；
真正的队列扫描（含退避与排序）是 #83 / #88 的事，等它落地时再按那时的查询补索引，
现在先加就是给一条还没成型的查询做假设。

## 与基线的关系

基线（`0001_baseline.py`）与 `0002_job_attempts` 都保持原样不动；本迁移是独立的一条
增量，`down_revision = "0002"`。空库跑 `upgrade head` 会依次执行三条，最终结构与
`core/models.py` 完全一致（`test_schema_migration` 的「基线库 == 模型库」用例守着这一点）。

## 降级

`downgrade` 删掉三列即可：它们是纯增量，没有别的表引用它们，删掉之后任务行回到
「状态 + 载荷」的老形状（旧代码不读租约，行为与 #82 之前一致）。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 三列的宽度集中在这里：模型侧用 short_text(64) / timestamp_text()，与这两组数一致。
_OWNER_CHARS = 64
_TOKEN_CHARS = 64
_EXPIRE_CHARS = 32


def upgrade() -> None:
    """Upgrade schema."""
    # 默认值给空串：既有行（以及任何裸 SQL 省略这三列的写）都落在「没有有效租约」，
    # 而不是让 NOT NULL 约束把一次正常的插入判死。
    op.add_column(
        "generation_jobs",
        sa.Column(
            "lease_owner",
            sa.String(length=_OWNER_CHARS),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )
    op.add_column(
        "generation_jobs",
        sa.Column(
            "lease_token",
            sa.String(length=_TOKEN_CHARS),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )
    op.add_column(
        "generation_jobs",
        sa.Column(
            "lease_expire_at",
            sa.String(length=_EXPIRE_CHARS),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("generation_jobs", "lease_expire_at")
    op.drop_column("generation_jobs", "lease_token")
    op.drop_column("generation_jobs", "lease_owner")
