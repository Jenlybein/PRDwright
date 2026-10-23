# v6 架构决策记录

> 编号接 v5（ADR-060~072）。本阶段 **ADR-073 ~ ADR-083**。
> 每条决策都写明「被否决的方案」——因为最贵的错误不是选错，是后来的人看不出还有别的选项。

| 编号 | 标题 | 一句话 |
| --- | --- | --- |
| [073](#adr-073) | 阶段定名 v6，v5 收口，部署推迟 v7 | 不覆盖已完成的测试重构文档 |
| [074](#adr-074) | 用 SQLModel 取代手写 `sqlite3` | 自建基础设施的账到期了 |
| [075](#adr-075) | 保留同步 Session，不引入 async | 换 async 只增加穿透层，不减少阻塞 |
| [076](#adr-076) | Alembic 基线 + `stamp`，不重放 20 步 | 已有库的真实形状是基线，不是历史 |
| [077](#adr-077) | 列类型只在不改落盘字节时升级 | 时间戳保持 `TEXT`、JSON 保持 `TEXT` |
| [078](#adr-078) | 仓库改 session 绑定实例、返回模型对象 | 类型检查要能在编译期抓住字段名 |
| [079](#adr-079) | 事务边界上移到服务层 | 让「跨表原子」第一次成为可能 |
| [080](#adr-080) | 模型不声明 `Relationship()` | 避开 N+1 与 detached 懒加载 |
| [081](#adr-081) | 连接池交给 SQLAlchemy，守住 `max_overflow=0` | 保留「运行期不建连接」这条不变量 |
| [082](#adr-082) | 验收靠三条静态守卫 + 三条结构对拍 | 机器判据替代人工过一遍 |
| [083](#adr-083) | **删除旧版本迁移链**，不再支持 v20 之前的库 | 没有服务对象的垫片只会变成第二套版本真相 |

---

## ADR-073

### 阶段定名 v6，v5 收口，部署推迟 v7

**状态**：已定

**背景**

`docs/architecture/v5/` 已有 2,940 行文档与 T01–T15 一整层任务，主题是「测试重构」。
用户在数据层问题暴露后决定「全面改用 SQLModel」。若把它并入 v5，等于把已完成的测试重构
文档改写成另一个主题——历史记录会失真（后来读文档的人无法区分「测试重构做了什么」与
「数据层重构做了什么」）。

**决定**

- **v6 = 数据层重构（SQLModel）**，独立目录 `docs/architecture/v6/`，ADR-073 起。
- v5 收口在「T15 数据访问层，T15.3 冻结在 batch A」，文档**原地保留不改**，
  `T15-data-access-layer.md` 顶部加一行指针说明「其连接池部分已被 v6 取代」。
- 部署与上线（ADR-054~059）**推迟到 v7**，v6 期间不改部署形态、不写部署脚本。

**理由**

阶段划分的目的是「后来的人能按主题找到来龙去脉」。把两个不同主题压进一个版本号，
省下的是目录数量，付出的是历史可读性。

**被否决的方案**

| 方案 | 否决理由 |
| --- | --- |
| 并入 v5，改写 v5 文档 | 已完成的工作被覆盖；`git log` 与文档对不上 |
| 用 v5.5 | 版本号不是小数序列，出现 `.5` 说明阶段边界没想清楚 |
| 部署与数据层重构同时做 | 两个高风险变更叠加，回归时无法归因 |

**后果**

- v6 期间部署形态保持 v5 现状（单进程、SQLite 文件、Nginx 反代）；
- v7 接手 ADR-054~059，届时数据库访问方式已换，部署文档要按新形态复核一遍。

---

## ADR-074

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

## ADR-075

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

## ADR-076

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

## ADR-077

### 列类型只在不改落盘字节时升级

**状态**：已定

**背景**

「改用 SQLModel」天然会诱使把 `TEXT` 换成 `DateTime`、把手工 JSON 串换成 `Column(JSON)`。
但这两种替换都会改变**已落盘的字节**与**读侧语义**。

具体到本库：

- 时间戳存 `ISO 8601` 文本（`2026-10-23T15:00:00.123Z` 或 `...+08:00` 形式），
  且**大量 SQL 直接做字符串比较**（`WHERE expires_at > ?`、`WHERE ts < ?`）。
  这是承重设计：ISO 8601 的字典序等于时间序，因此不必把时间取回 Python 再比。
- `json_extract` 有 5 处，全部在迁移 v19 的历史回填里，依赖「文本是合法 JSON」。
- `solutions.data` 是**后端从不解析**的不透明快照。
- `app_logs.extra_json` / `model_providers.extra_json` 的写路径要求「序列化失败不能影响主流程」。

**决定**

| 类 | 决定 |
| --- | --- |
| 时间戳 | **保持 `TEXT` + Python `str`**，不换 `DateTime` |
| `solutions.data` | 保持 `TEXT` |
| 所有 JSON 列（`completed_sections` / `usage` / `pending_items` / `metadata` / `extra_json` / `system_config.value`） | 保持 `TEXT` + 现有 `_dumps` / `_decode_*` 容错函数 |
| 布尔列 | 换成 `bool`（`BOOLEAN`），并在 Alembic `compare_type` 里归一，避免幻影迁移 |
| `users.salt` | `bytes` + `LargeBinary`（与现有 `BLOB` 一致） |

**理由**

判断标准不是「哪种类型更现代」，而是「换过去之后，已有库里的字节还能不能正确读出来、
SQL 里的比较还能不能成立」。时间戳换成 `DateTime` 会同时改变：落盘格式（`T`/空格、
`Z`/`+00:00`）、精度（秒 vs 微秒）、比较语义（字符串 vs 时间）。这是**用数据风险换类型美观**。

JSON 换 `Column(JSON)` 同样：落盘分隔符会从 `", "` 变 `","`、`ensure_ascii` 从 `False` 变
`True`（中文变 `\uXXXX`），而收益只是少两行 `json.dumps`。

**被否决的方案**

| 方案 | 否决理由 |
| --- | --- |
| 时间戳换 `DateTime` + 写迁移转换全表 | 迁移要逐行重写时间字段；`WHERE ts < ?` 这类比较全部要改成绑定参数；出错面远大于收益 |
| JSON 列换 `Column(JSON)` | 落盘字节变化 + 容错行为变化（坏数据会让读路径直接抛） |
| 全部保持 `int`（连布尔也不换） | `is_admin == 1` 与 API 层的 `is_admin: bool` 不一致，是把不一致固化下来 |

**后果**

- 模型里时间字段写 `str`，并在 `models.py` 顶部注释说明「不要改成 `datetime`」；
- 布尔列白名单（6 个：`users.is_active` / `is_admin`、`model_providers.builtin` /
  `enabled` / `thinking_enabled`、`chat_turns.interrupted`）由守卫 C7 守住；
- 将来若要换 `DateTime`，必须是**独立阶段**，带全表转换迁移与逐字段对拍。

---

## ADR-078

### 仓库改 session 绑定实例、返回模型对象

**状态**：已定

**背景**

仓库层今天是「静态函数命名空间」：86 个 `@staticmethod`，按 `XxxRepository.method(...)`
调用，71 个调用点在服务层与 API 层，且从没传过 session。

**决定**

1. 全部改为**实例方法**，构造参数是 `Session`：`Repo(session).method(...)`；
2. 仓库**不 commit / 不 rollback / 不 close**；
3. 返回 **ORM 模型对象**，不返回 `dict`；
4. 需要自增 id 时用 `flush()`，不用 `commit()` + `lastrowid`。

**理由**

第 1 条是被第 2 条逼出来的：仓库不持有连接，就必须从外部拿 session；而 `@staticmethod`
的签名里没有地方放它。加一个 `conn=` 关键字参数（T15.3 的做法）就是今天 `lease(conn)` 的
翻版——86 个函数多一个可选参数、71 个调用点各自决定传不传，事务边界继续散着。

第 3 条换来的收益是编译期：`job.stauts` 会被类型检查抓住，`job["stauts"]` 要等运行时，
而这类字段名错误在「返回 dict」的写法下只能靠测试覆盖到具体分支才暴露。

**代价（必须承认）**

- 71 个调用点要改；`job["status"]` → `job.status`；
- 有 2 条测试断言 `["is_admin"] == 1`，改模型后应为 `is True`（形态由 API 层保证不变）；
- 断言仓库返回 `dict` 的测试要改成断言模型字段。

**被否决的方案**

| 方案 | 否决理由 |
| --- | --- |
| 保持 `@staticmethod`，内部 `SessionLocal()` 自取 | 事务边界仍在仓库层，跨表原子依然做不到；且每个方法都要管 session 生命周期 |
| 加 `conn: Session | None = None` 参数（T15.3 路线） | 就是 `lease()` 的翻版，且这次要改 86 个签名 + 71 个调用点，成本不低、收益只有「少改调用点」 |
| 返回 `dict`（模型在仓库内转掉） | 放弃类型检查收益；且转换层要做 `__dict__` 过滤，容易漏 `metadata` 这类改名映射 |
| 返回独立 DTO（Pydantic） | 多一层模型定义，与 SQLModel 的「一个模型两用」初衷冲突 |

**后果**

- `to_history_item` / `to_display_turn` 这类投影函数保留，入参从 `dict` 改为模型对象；
- 响应组装统一走 `Model.model_validate(obj, from_attributes=True)`；
- `expire_on_commit=False` 是必须的，否则事务外读属性会 `DetachedInstanceError`（见 ADR-079）。

---

## ADR-079

### 事务边界上移到服务层

**状态**：已定

**背景**

今天 44 处 `commit()` 散在仓库层。`core/database.py` 提供了 `transaction()`，
但**全项目调用次数为 0**——也就是说「一次业务操作一个事务」这个能力，代码里从来没有过。

直接后果是 `document_store.commit_generated` 分三步写（确保 `documents` 行 → 插
`document_versions` → 回填 `documents.current_version_id`），任一步失败都会留下中间态。

**决定**

- 事务边界只在服务层出现，形态是 `with session_scope() as s:`；
- 仓库层零 commit / 零 rollback（守卫 C1）；
- `commit_generated` 的三步写收进**同一个** `session_scope()`；
- 任务进度 flush（≤300ms 或增量 >4KB）保持**每次一个短事务**，不合并成一个大事务
  （长事务会持写锁，把并发生成卡成串行）。

**理由**

`session_scope()` 让「跨仓库原子」从「改 86 个函数」变成「服务层包一层」。
这是本次重构唯一**语义变强**的地方，也是换 ORM 最实在的收益。

**风险与对策**

| 风险 | 对策 |
| --- | --- |
| 事务变大后持锁时间变长 | 只把「必须一起生效」的写合并；进度 flush、日志写入保持独立短事务 |
| 服务层忘记包 scope，仓库写不落库 | 仓库不 commit 之后「忘记提交」的后果是**静默丢数据** → 守卫 C1 只保证仓库干净，服务层的正确性靠用例：新增一条「服务方法返回后重开 session 能读到写入」的断言 |
| 异常路径只 rollback 不 re-raise | `session_scope` 固定 `except BaseException: rollback; raise`，不允许吞异常 |

**被否决的方案**

| 方案 | 否决理由 |
| --- | --- |
| 仓库层保留 commit（现状） | 跨表原子永久做不到 |
| 用 `Session.begin()` 显式事务 + 仓库传 session | 语义相同，但 `begin()` 嵌套时行为依赖 `autobegin`，比 `session_scope` 难讲清 |
| 全站一个大事务（请求级） | 请求里有 LLM 流，事务会横跨几十秒——持写锁把并发生成卡死 |

---

## ADR-080

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

## ADR-081

### 连接池交给 SQLAlchemy，守住 `max_overflow=0`

**状态**：已定

**背景**

T15 的自建池解决的是一个真实问题：`open()` 时一次性建满连接，让「多线程同时首次映射
WAL `-shm`」没有发生的时机。换 SQLAlchemy 后必须保留这条**结构性**不变量，
而不是退化成「需要时再建」。

**决定**

```python
create_engine(..., pool_size=N, max_overflow=0, pool_timeout=5.0)
# 启动期显式建满：
held = [engine.raw_connection() for _ in range(N)]
```

- `max_overflow=0`：不允许运行期扩容。池满 → `TimeoutError`，而不是「悄悄建一条新连接」；
- `pool_timeout=5.0`：与今天的 `acquire_timeout` 一致，避免请求无限挂起；
- 启动期 `warmup()` 显式建满：保证「运行期不建连接」这条不变量成立。

**理由**

`max_overflow>0` 会让「并发峰值时建连接」，而建连接正是那个 WAL 竞态的触发条件。
池满时宁可报错（可观测、可调 `DB_POOL_SIZE`），也不要悄悄扩容把竞态请回来。

**被否决的方案**

| 方案 | 否决理由 |
| --- | --- |
| `NullPool`（每次新建） | 直接回到 T15 要修的竞态，且每次付建连成本 |
| `StaticPool`（单连接共享） | 并发请求会串行化，且 `check_same_thread` 语义混乱 |
| 默认 `QueuePool`（`max_overflow=10`） | 高并发时扩容建连接 → 竞态窗口重现，且症状是偶发 `SQLITE_READONLY`，极难复现 |

**后果**

- `DB_POOL_SIZE` 仍是唯一调参口；`stats` 里的 `wait_ms_max` 改成从 SQLAlchemy 的
  pool 事件采集（或保留一层薄封装）；
- T15 的两个 P0（漏还连接、dispose 后复活）由 SQLAlchemy 的 `QueuePool` 承担；
- `test_database_pool.py` 里 8 条池专属用例删除，保留的 2 条语义改写为「池满报错」与
  「dispose 后借用报错」。

---

## ADR-082

### 验收靠三条静态守卫 + 三条结构对拍

**状态**：已定

**背景**

一次涉及 102 个函数、71 个调用点、14 张表的迁移，靠「人工过一遍」验收等于没验收。
而 v5 的经验是：**红久了的守卫等于没有**（守卫必须只对「已经违反纪律的代码」变红，
而不是对「还没迁完的代码」恒红）。

**决定**

六条机器判据，全部进 CI：

| # | 判据 | 类型 |
| --- | --- | --- |
| C1 | `repositories/` 无 `.commit()` / `.rollback()` | AST 静态 |
| C2 | `repositories/` 无 SQL 字符串字面量、无 `import sqlite3` | AST + 字面量 |
| C3 | 仓库方法是实例方法，构造参数为 `Session` | AST 静态 |
| C5 | 库的列集合 == 模型字段集合 | 结构对拍 |
| C6 | 基线建的库 == 模型建的库（语义快照） | 结构对拍 |
| C7 | 布尔列集合 == 白名单（6 列） | 模型断言 |

⚠️ **C6 原计划是 `alembic check`，实测后改用语义快照对拍。** SQLAlchemy 2.1.6 的 SQLite 方言
不反射外键的 `ON DELETE`，且把 `INTEGER PRIMARY KEY` 反射成 `nullable=True`——这两个缺陷让
`alembic check` 在本项目里**恒红**，红了的守卫等于没有。改成「比较基线建的库与模型建的库」后，
判据与方言反射无关，同样能抓住「模型改了却没写迁移」。

外加两条行为对拍：

- `test_baseline_matches_legacy_schema`：`upgrade head` 的库 == `init_schema + 20 步` 的库；
- `test_stamp_preserves_existing_db`：v20 库经 `prepare_schema` 后数据不少、schema 不变。

**理由**

每条判据都对应一个**静默失败模式**：

| 判据 | 不做会怎样 |
| --- | --- |
| C1 | 仓库又长出 commit，事务边界重新散掉（不知不觉回到今天） |
| C2 | 「先用裸 SQL 顶一下」成为常态，迁移永远收不干净 |
| C3 | 有人为省事加个 `SessionLocal()` 在仓库内部，跨表原子再次失效 |
| C5 | 模型少一列 → 写不进去、读出来是默认值，**不报错** |
| C6 | 模型与迁移漂移 → 新库与老库形状不同，只在某些部署上炸 |
| C7 | 加个布尔列忘了登记 → Alembic 幻影迁移，`check` 恒红后被忽略 |

**守卫的生效范围**

C1/C2/C3 **只在已迁移的模块上生效**，判据是「文件里不再 `import sqlite3`」——
这样分批迁移期间守卫不会恒红（ADR-082 与 v5 的 T15 守卫同一思路）。全部迁移完成后，
再加一条「`repositories/` 里 `import sqlite3` 计数为 0」把口彻底封上。

**被否决的方案**

| 方案 | 否决理由 |
| --- | --- |
| 只靠 code review | 102 个函数、5 批，review 疲劳是必然的 |
| 靠运行时断言（查 `session.dirty` 之类） | 抓不到「已经在仓库里 commit 了」，那时代码能跑 |
| 迁移完成后一次性加守卫 | 中途会积累一批「已经违反纪律但没人知道」的代码 |

**后果**

- 守卫本身要有**反向验证**（改坏 → 必须转红 → 还原），与 v5 的做法一致；
- 守卫文件集中放 `tests/test_architecture_guards.py`，按判据分函数。

---

## ADR-083

### 删除旧版本迁移链，不再支持 v20 之前的库

**状态**：已定（v6.1）

**背景**

ADR-076 把 `PRAGMA user_version` 的 20 步链冻结成 `core/legacy_migrations.py`，
计划「保留到 v7 删除」，理由是「万一还有停在 v18 的库」。

实际上它一个都没等到：

- 项目没有已交付的自托管实例，v20 之前的库只可能出现在开发机上；
- 开发机上的库（`backend/prdwright.db`）已经是 `user_version=20` + `alembic_version=0001`；
- 而这条链的**存在本身**有成本：它让「版本」有两个真相（`user_version` 与
  `alembic_version`），每个读 `core/schema.py` 的人都得先理解「为什么有两套」，
  再判断「我改的这个库算哪一套」。

**决定**

删掉整条链：`core/migrations.py`（含 `SCHEMA_SQL`、`init_schema`、20 步清单）与
`core/legacy_migrations.py` 一并删除。`prepare_schema` 只剩两条分支：

| 库的状态 | 行为 |
| --- | --- |
| 空库 | `upgrade head` |
| 结构已是当前形状、无 `alembic_version` | 校验 → `stamp head`（零 DDL） |
| 有 `alembic_version` | `upgrade head` |
| **结构不符（含 v20 之前的旧库）** | **拒绝启动**，错误信息里写明「旧链已删除、该怎么办」 |

**理由**

一条「没有服务对象」的迁移链，代价不是它自己那 1400 行，而是它让人**无法确定哪个是真源**。
删掉之后，「库是什么结构」只有一个答案：模型 + Alembic。

而「拒绝启动」比「自动升级」更适合旧库：自动升级会把「它到底缺了什么」变成一次不可见的
DDL 批量执行；拒绝启动会把差异逐条打印出来，让人当场决定是修还是重建。

**被否决的方案**

| 方案 | 否决理由 |
| --- | --- |
| 继续保留链到 v7 | 没有服务对象；每天都要为「万一」支付理解成本 |
| 删链，但对旧库「尽力 stamp」 | 正是 ADR-076 里被否决的那条：差异被永久藏起来，只在某次生成时炸 |
| 删链，并删掉 `stamp` 分支（只支持空库/已接管） | 结构已经正确的旧部署会撞上 `CREATE TABLE ... already exists`，错误难读；`stamp` 只有 6 行，且它守的是「不执行 DDL」这条安全属性 |

**后果**

- 守卫 **C9** 改成守「旧链不许回来」：两个文件不得存在、生产代码不得引用
  `SCHEMA_SQL` / `init_schema`、不得出现 `user_version` 字符串；
- 测试装置从「`init_schema` 建 v0 基线」换成「`prepare_schema` 建模板库，每用例拷一份」——
  测试与真实启动走同一条建库路径；
- 删掉 26 条只服务旧链的用例（迁移步骤、旧快照回填、双写守卫），因为它们测的行为已经不存在了。
  把它们改成恒绿的断言比删掉更糟：那会让「守卫覆盖了什么」变成一句谎；
- 老库的升级路径不再有自动化验证——**这是明确接受的**：那条路径本身已不存在。
