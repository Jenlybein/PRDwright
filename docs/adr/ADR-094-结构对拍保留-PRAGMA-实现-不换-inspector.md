# ADR-094：结构对拍保留 `PRAGMA` 实现，不换 inspector

> 源：v8 层（原 `docs/architecture/v8/ADR.md`，已并入本目录）｜ [索引](./README.md)

**状态**：已定（v8 / 实施期**实测否决**原计划）。替代 `DIALECT-PORTABILITY.md` §3.5 的原方案。

### 背景

`schema_snapshot.py` 那 300 行用 `sqlite3` + `PRAGMA` 读库结构，被两个地方使用：
`assert_schema_matches_models`（启动时拒绝不一致的库）与 `test_schema_parity`
（模型与迁移基线的对拍）。

原计划换成 `sqlalchemy.inspect`，理由是"去掉循环论证"：现在是用 SQLite 的私有接口
检查 SQLite 的库结构，工具与被检查对象共用同一套实现。方言中立只是附带好处。

### 决策

**保留 `PRAGMA` 实现。** 探测之后发现换 inspector 的实际代价大于收益。

### 实测证据

对现有库同时跑两种读法：

| 判据项 | `PRAGMA foreign_key_list` | `inspect(engine).get_foreign_keys()` |
|---|---|---|
| `document_versions.document_id` | `ON DELETE CASCADE` | `options: {}` |
| `documents.current_version_id` | `ON DELETE SET NULL` | `options: {}` |
| 部分索引 `ux_chat_turns_job_role` 的 `WHERE` | 从 `sqlite_master.sql` 取 | 只能从 `dialect_options['sqlite_where']` 取 |
| 列默认值 | `"'[]'"` / `'0'` | `"'[]'"` / `'0'`（形态接近） |

前两行是致命项。级联规则是本项目**最不能出错**的一类结构约束：

- `documents.current_version_id` 是 `SET NULL`——漏了它，删版本时文档行会指向一条不存在的记录；
- `document_versions.document_id` 是 `CASCADE`——漏了它，删方案会留下孤儿正文（占空间且能被导出）。

两者都不会当场报错，只会在某次删除后留下不可见的数据损伤。

### 为什么"判据消失"比"判据变松"更危险

换 inspector 之后，**期望快照与实际快照都不含 `ondelete`**，于是 `diff_snapshots`
判定二者相等、测试全绿。问题不在于它误报，而在于它**不再检查这一项**——
而且没有任何信号提示"这里少检查了什么"。

这与"把归一规则放宽"是两种性质：放宽会让本来该红的地方变绿，至少有人讨论；
而丢字段是静默地缩小了检查范围，测试跑绿了，反而更有信心。

### 为什么"方言中立"这个好处也不成立

部分索引的 `WHERE` 只能从 `dialect_options['sqlite_where']` 里取，那本身就是
SQLite 专属的键。所谓中立，实际是「`PRAGMA` 换成 `dialect_options`，
再为外键补一条 `PRAGMA` 分支」——特化没有减少，只是从一处分散到两处。

### 被否决的方案

| 方案 | 理由 |
|---|---|
| 换 inspector，接受丢 `ondelete` | 用"少一项检查"换"少一处方言"，方向反了 |
| 换 inspector + 对 SQLite 补 `PRAGMA` 读外键 | 特化从一处变两处，复杂度上升，收益为零 |
| 换 inspector + 从模型侧读期望的 `ondelete` | 拿期望值去比期望值，等于不检查实际库结构 |

### 推论

- `schema_snapshot.py` 的方言特化保留，但它是**测试与启动校验工具**，不在业务路径上；
  它的特化代价是"少一层独立验证"，不是"运行时出错"。
- 将来真换库时，需要为 `schema_snapshot` 补一套目标库的读取实现，而不是期待
  "方言中立"自动获得。这一条已写进 `DIALECT-PORTABILITY.md` §6 的换库清单。

---
