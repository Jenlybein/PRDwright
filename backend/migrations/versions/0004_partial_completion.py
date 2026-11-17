"""「部分完成」标记两列（票 #85）。

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-17

## 这条迁移做什么

给两张表各加一列**同义的布尔标记** `partial`：

| 表 | 类型 | 默认 | 语义 |
|---|---|---|---|
| `document_versions` | `BOOLEAN` | `false` | 这一版正文**可用但不完整**（写到一半失败 / 有章节占位） |
| `generation_jobs` | `BOOLEAN` | `false` | 这次生成的结局是失败/部分完成，但**有内容可用** |

## 为什么两个表都要这一列

spec（`docs/architecture/state-flow-design.md` §「部分完成」怎么办）要求「把已经写出来的
正文落成一个**新版本**，在版本上标『部分完成』」。标记描述的是**某一版正文**，所以真源
必须落在版本行上——同一份文档的不同版本里，只有那一版不完整。

任务行再留一份，是因为界面的两个问题由两处回答：「这次任务怎么结束的」（任务行）与
「这份正文能不能用」（版本行）。只写一处就会有一边说不清：任务行说「失败」而版本链里
躺着能用的正文，界面只能二选一地撒谎。两份投影由**同一次收尾事务**写入（#81 的原子收尾），
因此不会出现「任务说失败、正文没落」或「正文落了、任务说成功」的错配。

## 为什么默认是 `false` 而不是可空

`NULL` 会让「不知道完不完整」成为一种状态，读侧只好三选一（真 / 假 / 未知），而这三者的
差别在界面上没有对应表达。`false` 的语义是明确的：「这一版没有不完整的记录」——历史数据
（迁移前的全部版本）本来就没有这个标记，它们不是「部分完成」，按 `false` 回填正好。

## 与基线 / 前序迁移的关系

`0001_baseline` / `0002_job_attempts` / `0003_job_leases` 一字不动；本迁移是独立的一条增量，
`down_revision = "0003"`。空库跑 `upgrade head` 会依次执行四条，最终结构与 `core/models.py`
完全一致（`test_schema_migration` 的「基线库 == 模型库」用例守着这一点）。

两列都是纯增量、不被任何外键或索引引用，`downgrade` 直接删掉即可：旧代码不读它们，
行为与 #85 之前一致（失败只留 `draft_content`，不落版本）。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 默认 `false`：既有行（迁移前的所有版本与任务）与任何裸 SQL 省略该列的写，
    # 都落在「没有不完整的记录」，而不是让 NOT NULL 把一次正常插入判死。
    # ⚠️ 字面量必须是 `false` 而不是 `0`：Postgres 的 BOOLEAN 不接受整数默认值
    # （见 `core/models.py::BOOLEAN_COLUMNS` 那段注释）。
    op.add_column(
        "document_versions",
        sa.Column("partial", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "generation_jobs",
        sa.Column("partial", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("generation_jobs", "partial")
    op.drop_column("document_versions", "partial")
