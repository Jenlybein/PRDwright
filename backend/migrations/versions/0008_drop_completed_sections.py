"""删除分章遗留列 `generation_jobs.completed_sections`（票 #94）。

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-18

## 这条迁移做什么

删一列：`generation_jobs.completed_sections`。

它是分章时代的「章节完成清单」（JSON 数组）——生成过程每写完一章就往里追加，
刷新重连时前端据此把多章看板摆回去。

票 #92 退役分章、三份产物改成**整篇生成**之后，这一列**不再被任何代码写入**，
也**没有任何读点**：任务的进度状态只剩「已定稿正文 + 一段实时缓冲 + 当前阶段输出」，
它们分别住在 `draft_content` / 内存运行时 / 阶段输出里。列留着只会让读代码的人以为
「哪天会有值」，所以票 #94（spec #75 的收口票）把它删掉。

## 为什么是「删列」而不是「留列不用」

按 [`docs/agents/conventions.md`](../../../docs/agents/conventions.md) §3：删列 / 退役列是
**单向门**，删完旧代码读不到该列，回滚只能靠恢复备份（`pg_dump` 归档），不设兼容期。
这一列满足「删了不会有任何人读不到东西」——没有任何读点，删它是纯粹的减法：

- 留列 + 不写 = 一条永远为 `'[]'` 的 `NOT NULL` 列，每次插入都要显式喂一个值；
- 它还是「章节」这个已退役概念在数据层唯一的残骸。

## 与基线 / 前序迁移的关系

`0001`~`0007` 一字不动；本迁移是独立的一条增量，`down_revision = "0007"`。
删列用原生 `ALTER TABLE … DROP COLUMN`（同 §3：Postgres 下不需要重建表）。
空库跑 `upgrade head` 会依次执行八条，最终结构与 `core/models.py` 一致
（`test_schema_migration` 的「基线库 == 模型库」因此仍然成立）。

## 降级

`downgrade` 把列按**原来的定义**（`TEXT NOT NULL`，默认 `'[]'`）加回来，但**数据找不回来**
（这一列的数据只描述已经退役的分章进度，没有恢复价值）。真要回滚到旧代码，请用备份。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: Union[str, Sequence[str], None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade：删掉分章遗留列（无读点、无写入点）。"""
    op.drop_column("generation_jobs", "completed_sections")


def downgrade() -> None:
    """Downgrade：把列按原定义加回来（数据不恢复，见模块说明）。"""
    op.add_column(
        "generation_jobs",
        sa.Column(
            "completed_sections",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )
