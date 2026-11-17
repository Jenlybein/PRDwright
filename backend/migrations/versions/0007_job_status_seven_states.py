"""七态取值与旧行一次性映射（票 #84）。

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-17

## 这条迁移做什么

**不改结构**（七态复用已有的 `status` 列），只做一次**数据映射**：把旧口径留下的
行改成新语义。仓库当前是「空库起新库」，正常不会有存量行；这一段是给「已经跑过旧
版本、库里还留着旧取值」的部署的一条可重复（幂等）清洗：

| 旧行 | 新行 | 理由 |
|---|---|---|
| `failed` + `error_code='CANCELLED'` | `cancelled` + `error_code=''` | `CANCELLED` 不再承担状态语义（AC4），状态由 `status` 表达 |
| `failed` + `error_code='SUPERSEDED'` | `superseded` + `error_code=''` | 同上；「被取代」与「已取消」从同一个 `failed` 里拆开（AC5） |
| `running` 且 `lease_token=''` | `pending` | 旧口径里「排队中」与「执行中」共用 `running`，区别只在有没有租约；七态之后「排队中」只有 `pending` 一个表示，不许两个真源 |
| `pending` 且 `lease_token!=''` | 清空租约三列 | 排队中的行不该持有执行权（防御性：正常路径不会产生这种行） |

## 为什么可以重复跑

两句 `UPDATE` 的条件都是**旧取值的精确匹配**（`status='failed' AND error_code='CANCELLED'`
等）。映射完成后那些行不再满足条件，第二次执行为 0 行；即使把库退回 `0006` 再升一次
（`downgrade` 是空操作、不回溯数据），结果也不变。空库上执行它同样安全（0 行）。

## 降级

数据映射**不可逆**：`cancelled` 与 `superseded` 无法安全地合回 `failed + 某个码`
（做了也只是一次猜测，会把两件事重新混成一团）。因此 `downgrade` 只把版本号退回去、
不动数据；真要回滚到旧代码，请用备份（#90 的演练里保留了旧库副本）。

## 与基线 / 前序迁移的关系

`0001`~`0006` 一字不动；本迁移是独立的一条增量，`down_revision = "0006"`。
结构没有变化，所以 `test_schema_migration` 的「基线库 == 模型库」仍然成立。
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: Union[str, Sequence[str], None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade data：旧取值 → 七态（幂等，可重复跑）。"""
    # 1) 旧 `failed + CANCELLED/SUPERSEDED` → 各自的状态，错误码清空。
    #    错误码清空是 AC4 的要求：状态由 `status` 表达，不再让同一列有两个含义。
    op.execute(
        """
        UPDATE generation_jobs
           SET status = 'cancelled', error_code = ''
         WHERE status = 'failed'
           AND error_code = 'CANCELLED'
        """
    )
    op.execute(
        """
        UPDATE generation_jobs
           SET status = 'superseded', error_code = ''
         WHERE status = 'failed'
           AND error_code = 'SUPERSEDED'
        """
    )

    # 2) 旧「排队中」（`running` 且没有租约）→ `pending`。
    #    这一步是「排队中只有一个表示」的落点：之后 `running` 一定持有租约。
    op.execute(
        """
        UPDATE generation_jobs
           SET status = 'pending'
         WHERE status = 'running'
           AND lease_token = ''
        """
    )

    # 3) 防御性清洗：`pending` 行不该持有租约（正常路径不会产生，但旧数据可能手工造过）。
    op.execute(
        """
        UPDATE generation_jobs
           SET lease_owner = '', lease_token = '', lease_expire_at = ''
         WHERE status = 'pending'
           AND lease_token <> ''
        """
    )


def downgrade() -> None:
    """Downgrade：数据映射不可逆，只退版本号（不动数据）。"""
    # 有意为空：把 `cancelled` / `superseded` 合回 `failed + 某个错误码` 会把 AC5 要
    # 分开的两件事重新混在一起，而且无法判断原来的码是哪一个。回滚请用备份。
    pass
