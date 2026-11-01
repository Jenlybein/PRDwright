# ADR-088：方言收敛点 = `core/dialect.py`

> 源：v8 层（原 `docs/architecture/v8/ADR.md`，已并入本目录）｜ [索引](./README.md)

**状态**：已定（v8 / T18.1）。尚未落地。

### 背景

「这个项目跑在 SQLite 上」这句话，目前散在十几个地方。其中真正会咬人的是
**upsert**：三处写库都 `from sqlalchemy.dialects.sqlite import insert`，
每个新增的仓库都要自己记得"用 `sqlite_insert` 而不是 `insert`"。
漏了不会报错，会在运行时抛 `AttributeError: 'Insert' object has no attribute 'on_conflict_do_nothing'`。

这件事有两个可选形态：

1. 在各仓库里写 `if dialect == "sqlite": ...`；
2. 把方言差异收进一个函数，调用方无感。

### 决策

**取 ②**。新建 `core/dialect.py`，对外只暴露两个入口：

```
upsert_values(session, model, values, *, conflict_cols, update_cols=None)
upsert_select(session, select_stmt, model, *, conflict_cols, update_cols=None)
```

- `update_cols=None` 表示"冲突就什么都不做"；
- `upsert_select` 保留"待插入的行来自 SELECT"的形状——`document_repo._ensure_document`
  把归属校验写在 SELECT 的 WHERE 里，这条结构必须在收敛后仍然成立。

配套三条：

1. **MySQL 的 DO NOTHING 用 `ON DUPLICATE KEY UPDATE <pk> = <pk>`，不用 `INSERT IGNORE`**。
   `IGNORE` 会把列超长、违反 NOT NULL 一并降级成警告；
2. **契约 C-D1**：helper 只允许用在"该表只有一个唯一约束"的表上。MySQL 忽略冲突目标，
   表上若有第二个唯一键，`ON DUPLICATE KEY` 会命中它；
3. 仓库层仍然只管翻译，不碰事务边界（ADR-073 继续有效）——helper 只执行语句，不 commit。

### 被否决的方案

| 方案 | 理由 |
|---|---|
| 各仓库内分支 | 就是收敛前的状态，只是把 `sqlite_insert` 换成更长的 `if` |
| 改用「先查再写 + 捕获 IntegrityError」 | 通用，但把已经修好的竞态重新引回来（`chat_session_repo` 的注释专门反对过这个写法） |
| 用 `MERGE` 做通用 upsert | MySQL 不支持 `MERGE`；SQLAlchemy 的 `Merge` 方言覆盖不全 |

---
