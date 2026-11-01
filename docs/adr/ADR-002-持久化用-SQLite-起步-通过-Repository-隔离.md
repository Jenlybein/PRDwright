# ADR-002：持久化用 SQLite 起步，通过 Repository 隔离

> 源：v2 层（原 `docs/architecture/v2/ADR.md`，已并入本目录）｜ [索引](./README.md)

**状态**：Accepted（2026-10-13 已落地，首用于认证；P1 扩展至方案持久化）

### Context

需要持久化，但当前是单人本地开发，未来也只是「少量人使用」。选型的真实约束是**运维成本**，不是吞吐上限。

### Options

| 方案 | 优点 | 代价 |
| --- | --- | --- |
| **A. SQLite 单文件 + Repository 抽象** | 零运维、备份就是拷文件、Python 标准库自带 | 并发写能力弱；将来切 Postgres 需迁移 |
| B. 直接上 Postgres | 一步到位，无迁移成本 | 本地开发要起容器/服务，拖慢 P0-P2 的迭代；当前负载完全用不上 |
| C. 文件（JSON per session） | 最简单 | 无查询能力，「会话列表」需求一出现就崩 |

### Decision

**选 A。** 服务层只依赖 Repository 协议（`UserRepository` / `AuthSessionRepository` / `InviteCodeRepository`
已落地；P1 再补 `SolutionRepository`），具体实现默认 SQLite。开启 `journal_mode=WAL`。

落地情况（2026-10-13）：`core/db.py` 负责连接与建表，`repositories/` 是**全项目唯一出现 SQL 的地方**，
服务层与路由层一律不得 import `sqlite3`。这条约束靠 review 与目录约定维持。

### Consequences

- 变容易：本地开发零依赖；备份与调试直观。
- 变困难：真正的多用户并发写场景需要评估（少量人场景应无压力）。
- 中立：切换到 Postgres 的成本被 Repository 限制在一个文件内；**前提是业务代码不得出现任何 SQL 或 sqlite3 import**。

---
