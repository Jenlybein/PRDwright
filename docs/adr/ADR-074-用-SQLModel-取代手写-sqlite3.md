# ADR-074

> 源：v6 层（原 `docs/architecture/v6/ADR.md`，已并入本目录）｜ [索引](./README.md)

### 用 SQLModel 取代手写 `sqlite3`

**状态**：已定

**背景**

数据访问层今天由三样东西组成：11 个仓库文件（102 函数、86 个 `@staticmethod`、44 处
`commit()`）、`core/db.py` 的 `SCHEMA_SQL` 与 `connect()`、`core/database.py` 的 444 行
自建连接池。

最后这个池是 v5 的 T15 为修一个竞态而写的——「多线程同时首次映射 WAL 的 `-shm`」会返回
`SQLITE_READONLY` 而非 `SQLITE_BUSY`，所以 `busy_timeout` 不重试。解法是把建连接收敛到
启动期单线程。但池写出来之后，在池里踩了两个 P0：异常路径漏还连接（`try/except` 而非
`try/finally`）、`dispose()` 之后池被归还路径悄悄复活。

**决定**

数据访问层换成 **SQLModel（表模型）+ SQLAlchemy Engine/Session（连接与事务）**。
手写的连接管理、事务边界、行映射全部删除。

**理由**

1. 那两个 P0 的成因是「自己实现池」，而池是成熟库的核心功能，不是业务复杂度；
2. 事务边界散在仓库层（44 处 commit），想做到「跨仓库原子」得改 86 个函数——
   而 `session_scope()` 让这件事变成「服务层包一层」；
3. 行映射（`sqlite3.Row` → `dict` → 服务层按字符串键取值）没有任何编译期检查，
   字段名写错只在运行时暴露；
4. 结构真源分裂成两处（`SCHEMA_SQL` 与 20 步迁移），每加一列都要改两个地方。

**明确不承诺的事**

「删掉所有手写 SQL」这个目标**做不到，也不该作为验收标准**。仓库层有 4 处 upsert、
6 处需要自增 id、若干带 `owner_id` 的多表 join。换 SQLModel 后这些仍是查询表达式
（`select(...).join(...)`、`sqlite_insert(...).on_conflict_do_update(...)`），
不是 ORM 的「对象图操作」。真正被删掉的是四样：**连接生命周期、事务边界、行映射、
结构真源的重复**。

**被否决的方案**

| 方案 | 否决理由 |
| --- | --- |
| 保留手写 `sqlite3`，只修池的两个 bug | 修完仍然要维护 444 行池；下一批需求（跨表原子、审计一致性）还会撞同一面墙 |
| 用 `databases`（async） | 只能解决连接管理，不解决模型与迁移；且要引入 async 穿透 |
| 用 SQLAlchemy Core（不上 SQLModel） | 会重新引入「手写 `Table` 定义」，模型与 Pydantic 仍要写两份 |
| 换 SQLModel 但保留 `SCHEMA_SQL` 建表 | 结构真源继续分裂，`alembic check` 无意义 |

**后果**

- 新增依赖 `sqlmodel`、`sqlalchemy`、`alembic`；
- 86 个 `@staticmethod` 改实例方法，71 个调用点改属性访问（分 5 批）；
- 877 个测试作为回归网，**不允许**为了迁移而放宽任何断言。

---
