# ADR-080

> 源：v6 层（原 `docs/architecture/v6/ADR.md`，已并入本目录）｜ [索引](./README.md)

### 模型不声明 `Relationship()`

**状态**：已定

**背景**

SQLModel 支持 `Relationship()`，可以用 `job.solution.title` 这种写法替代显式 join。

**决定**

**所有模型不声明 `Relationship()`**。跨表查询一律显式 `select(...).join(...)`。

**理由**

1. **detached 懒加载**：默认 `lazy="select"` 会在访问属性时发查询。而本项目的对象
   生命周期通常是「`session_scope()` 内查出、块外读属性」——session 已 close，
   访问未加载的关系就抛 `DetachedInstanceError`。这类错误只在特定分支偶发，很难在测试里覆盖全；
2. **N+1**：列表页（方案列表、任务列表、日志分页）一旦有 relationship 且被序列化，
   就是 N+1；而现有代码是**一条带 join 的查询**拿全部字段，性能特征明确；
3. 现有真正需要 join 的地方只有三处（`_ensure_document` 的归属校验、`to_history_item`
   的投影、admin 列表的跨 owner 查询），显式写更清楚，也更容易看出有没有走索引。

**被否决的方案**

| 方案 | 否决理由 |
| --- | --- |
| 用 `Relationship(lazy="raise")` | 可行但多一层配置；且一旦有人写 `lazy="select"` 就静默退化 |
| 用 `lazy="joined"` | 会让每次查任务都连带查方案与用户，把窄查询变成大 join |

**后果**

- 模型只有列，没有关系属性；
- 需要「任务 + 方案标题」这类组合时，在仓库方法里写 `select(GenerationJob, Solution.title).join(...)`，
  返回元组或显式投影模型。

---
