# ADR-076

> 源：v6 层（原 `docs/architecture/v6/ADR.md`，已并入本目录）｜ [索引](./README.md)

### Alembic 基线 + `stamp`，不重放 20 步

**状态**：已定（第 4 条与「后果」的垫片部分已被 ADR-083 取代）

**背景**

现有迁移链是 `PRAGMA user_version` 的 20 步手写清单（`core/migrations.py`，1365 行）。
库里已有真实数据。换 Alembic 有三种做法。

**决定**

**Brownfield 基线法**：

1. 用 `autogenerate` 从 SQLModel 模型生成 `0001_baseline`，内容是**当前 v20 的全量 DDL**；
2. 对已有库：**先校验 schema 与基线一致**，再 `alembic stamp head`——不执行任何 DDL，
   只写 `alembic_version` 一行；
3. 对空库：`alembic upgrade head`；
4. ~~`core/migrations.py` 冻结为 `core/legacy_migrations.py`，只在「`user_version < 20`」时执行，
   v7 删除。~~ **已被 ADR-083 取代**：这条垫片在 v6.1 就删掉了（比原计划早一个阶段）。

**理由**

历史迁移的价值是「把旧库带到今天」，而**已有库的形状就是今天**。重放 20 步只是把
「已知的最终状态」用 20 个中间态重新走一遍，风险（20 次 DDL、20 次可能失败的分支）
远大于收益（一个好看的历史）。

但「不重放」有个前提必须补上：**基线必须与真实库逐列一致**。否则 `stamp` 会把差异
永久藏起来——`alembic check` 认为已是最新，而代码按模型读会读到不存在的列。
所以 `stamp` 之前强制跑 `assert_schema_matches_baseline()`，不一致就**拒绝启动**。

**被否决的方案**

| 方案 | 否决理由 |
| --- | --- |
| 写 20 个 Alembic revision 重放历史 | 只为已有库「看起来历史完整」，付出 20 步 DDL 风险；且第 1~20 步的正确性无法用现有数据验证 |
| 只做 baseline，不做漂移校验 | 差异永久隐身，是本方案唯一的致命失败模式 |
| 继续用 `user_version`，SQLModel 只管 CRUD | 结构真源继续分裂；放弃 `alembic check` 这条机器判据 |
| 新建库、导出导入数据 | 迁移期要停机、要验证「导出的 JSON 能无损导入」，风险更高 |

**后果**

- 多一条启动期校验（schema 快照比较），失败即拒绝启动并给出差异明细；
- ~~`legacy_migrations.py` 保留到 v7~~ → **已在 v6.1 删除**，见 ADR-083；
- 漂移检查进 CI（T16.12），但走的是**语义快照对拍**而不是 `alembic check`——后者在 SQLite 方言下
  因反射缺陷恒红（见 ADR-082 的说明）。

---
