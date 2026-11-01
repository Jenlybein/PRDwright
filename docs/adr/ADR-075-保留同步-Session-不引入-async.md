# ADR-075

> 源：v6 层（原 `docs/architecture/v6/ADR.md`，已并入本目录）｜ [索引](./README.md)

### 保留同步 Session，不引入 async

**状态**：已定

**背景**

应用是 FastAPI + `async def` 端点。数据库是本地 SQLite 文件。

**决定**

使用 **同步** `Session`（`sqlite+pysqlite`），不引入 `aiosqlite` / `AsyncSession`。

**理由**

1. 现状本来就是同步：`sqlite3` 在 `async def` 里同步执行，单次查询微秒级。
   换 async 不会让「阻塞」消失，只会把它从「同步调用」变成「`await` 挂起点」；
2. async session 的成本是穿透式的：86 个仓库方法变 `async def`、71 个调用点加 `await`、
   所有 `session_scope()` 块变成 `async with`，且**仍然不能跨 `await` 持 session**
   （原来的 G-DB4 约束一点没松）；
3. SQLite 是进程内文件访问，没有网络往返，异步 I/O 的收益接近零；
4. 真正会长时间阻塞的是 LLM 流式输出，那件事本来就不该在数据库事务里做——
   这条约束在同步/异步下完全相同。

**被否决的方案**

| 方案 | 否决理由 |
| --- | --- |
| `aiosqlite` + `AsyncSession` | 穿透成本高、收益接近零；且不能解决「持 session 跨 await」的真正风险 |
| 同步 Session + `run_in_threadpool` 包一层 | 多一层线程调度，连接池还要跨线程借还，复杂度上去、收益不明 |

**后果**

- `G-DB4`（连接不跨 `await`）改写为「`session_scope()` 的 body 不得含 `await`」，
  由 AST 守卫 C4 强制；
- 若将来换 PostgreSQL（网络往返真实存在），这条决策需要重开——届时一并评估异步。

---
