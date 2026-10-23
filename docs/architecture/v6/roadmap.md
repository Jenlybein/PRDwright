# v6 路线图

> 14 个任务（T16.0 ~ T16.13），按「先建旁路、再切入口、再逐批换血、最后拆旧」排列。
> 每个任务独立可运行、独立可回滚；**任一步中途停下，工作树都必须是绿的。**

---

## 总体节奏

```
T16.0 依赖与快照工具
  ↓
T16.1 Engine/session（旁路，不改任何调用点）
  ↓
T16.2 模型 ──→ T16.3 结构对拍 ◀── 关键门：不绿不许往下
  ↓
T16.4 Alembic 基线 + brownfield
  ↓
T16.5 建库入口切到 Alembic（旧 connect() 保留）
  ↓
T16.6~T16.10 五个仓库批次（每批 = 一个仓库 + 它的调用点 + 它的用例）
  ↓
T16.11 服务层事务收口
  ↓
T16.12 拆旧路径 + 守卫封口
  ↓
T16.13 契约对拍 + 文档收口
```

**为什么 T16.5 之后不立刻删 `connect()`**：五个批次迁移期间，未迁移的仓库仍在用
`connect()`。提前删掉会让「迁到一半」变成不可运行状态——而不可运行的中间态是最危险的，
因为它既不能验证也不能回滚。

---

## T16.0 · 依赖与快照工具

**目标**：把「迁移前的库长什么样」变成一份可机器比对的黄金快照。

**改动**

| 文件 | 动作 |
| --- | --- |
| `pyproject.toml` | 加 `sqlmodel`、`sqlalchemy`、`alembic` |
| `tests/support/schema_snapshot.py` | 新增：`schema_snapshot(path) -> dict`（§6.4 的语义快照） |
| `tests/fixtures/schema_v20.json` | 新增：由 `init_schema + run_migrations` 生成，作为黄金基准 |

**验收**

- `uv sync` 成功，`uv pip list` 见到三个包；
- 877 例全绿（新增依赖不改行为）；
- 快照文件含 14 张表、16 个索引、全部 FK。

**回滚**：撤依赖与两个新文件，无代码语义变化。

**⚠️ 注意**：`sqlmodel` 会带进 `pydantic` 的版本约束，与本项目的 `pydantic-settings`
可能冲突。装完立刻跑全量，不要等到 T16.2 才发现（v5 装 langchain 系依赖时踩过同类问题）。

---

## T16.1 · Engine 与 session_scope（旁路）

**目标**：把连接与事务的能力建起来，**但不接任何调用点**。

**改动**

| 文件 | 内容 |
| --- | --- |
| `core/engine.py`（新） | `build_engine()` / `engine` 单例 / `SessionLocal` / `session_scope()` / `warmup()` / `reset_engine()` / PRAGMA 事件 |
| `core/config.py` | 无改动（复用 `DB_POOL_SIZE`） |
| `tests/test_engine.py`（新） | 6 条：PRAGMA 生效、`max_overflow=0`、`warmup` 后 idle==size、`session_scope` 提交、异常回滚、`dispose` 后借用报错 |

**验收**

- 6 条新用例全绿；
- **877 条旧用例一条不红**（证明旁路没有副作用）；
- 手工验证：`warmup(4)` 之后再并发借 5 条，第 5 条在 5 秒后抛 `TimeoutError`
  （证明「不许运行期扩容」这条不变量成立）。

**回滚**：删 `engine.py` 与 `test_engine.py`。

---

## T16.2 · 模型层（14 张表）

**目标**：`core/models.py` 成为唯一的结构真源。

**改动**：新增 `core/models.py`，14 个模型 + 16 个索引 + 3 处约束。

**必须点名的六个坑**（每个都有对应验收项）

| # | 坑 | 写法 |
| --- | --- | --- |
| 1 | `id` 少 `AUTOINCREMENT` → rowid 复用 | 每表 `__table_args__ = {"sqlite_autoincrement": True, ...}` |
| 2 | `document_versions.metadata` 撞 `Base.metadata` | 属性名 `meta` + `Column("metadata", TEXT, ...)` |
| 3 | 布尔列类型 `BOOLEAN` vs 现有 `INTEGER` | 用 `bool`，白名单登记 6 列 |
| 4 | `chat_turns` 部分唯一索引 | `Index(..., unique=True, sqlite_where=text("job_id IS NOT NULL"))` |
| 5 | `documents` 表级 `UNIQUE (solution_id, kind)` | `UniqueConstraint("solution_id", "kind")` |
| 6 | 时间戳**保持 `TEXT`** | 字段类型 `str`，模块顶部注明不要改 `datetime` |

**验收**

- C7：`Boolean` 列集合 == 6 列白名单；
- 模型字段名与 `dir(Base)` 无交集（防第 2 类撞名）；
- AUTOINCREMENT 行为测试：删掉最大 id 行 → 新插入的 id **不复用**；
- `models.py` 里出现 `DateTime` 即红（一条 grep 断言）。

**回滚**：删 `models.py`。

---

## T16.3 · 结构对拍（关键门）

**目标**：证明模型**忠实描述现有库**。这是整次迁移唯一不可绕过的门。

**改动**：新增 `tests/test_schema_parity.py`，三条用例：

```python
def test_create_all_matches_legacy_schema(tmp_path):
    """create_all() 的库 == init_schema + 20 步迁移的库"""
def test_every_column_is_mapped(tmp_path):
    """库的列集合 == 模型字段（或 Column 名）集合"""
def test_indexes_and_constraints_parity(tmp_path):
    """16 个索引、3 处唯一约束、8 个 FK 逐一对应"""
```

**验收**：三条全绿；且**反向验证**——故意在模型里少写一列、把某列类型写错、
漏一个索引，三条用例必须分别转红（改坏 → 跑 → 转红 → 逐字节还原）。

**回滚**：删测试文件。**但门没绿不许进 T16.4**。

> 这个任务的价值：模型少一列的后果是「写不进去、读出来是默认值」，**运行时不报错**。
> 它是本次迁移最可能的静默失败点，必须用机器判据挡在换库之前。

---

## T16.4 · Alembic 基线 + brownfield

**目标**：迁移链从 `user_version` 换成 Alembic，且已有库**一行 DDL 都不执行**。

**改动**

| 文件 | 内容 |
| --- | --- |
| `alembic.ini` | 指向 `sqlite+pysqlite:///` 与 `migrations/` |
| `migrations/env.py` | `target_metadata = SQLModel.metadata`；`compare_type` 布尔白名单归一；`render_as_batch=True` |
| `migrations/versions/0001_baseline.py` | autogenerate 生成的全量 DDL |
| `core/schema.py`（新） | `prepare_schema(engine)` 四条路径 + `assert_schema_matches_baseline()` |
| `core/legacy_migrations.py` | 由 `core/migrations.py` 冻结而来（改名为 `run(engine)` 入口，逻辑逐字不变） |

**验收**

- 空库 → `upgrade head` → 与黄金快照一致；
- v20 库 → `prepare_schema` → **数据行数不变、schema 不变、`alembic_version` 写入**；
- v18 库 → 跑冻结的 legacy 20 步到 v20 → `stamp` → 数据不丢；
- 人为改坏基线（删一列）→ `assert_schema_matches_baseline` **拒绝启动**并打印差异；
- 基线建的库与模型建的库**语义快照一致**（不用 `alembic check`，见 ADR-082 的实测说明）。

**回滚**：删 `alembic.ini` / `migrations/` / `core/schema.py`，启动仍走 `init_schema()`。

---

## T16.5 · 建库入口切到 Alembic

**目标**：启动路径换成 `prepare_schema`，`init_schema()` 退役（但 `connect()` 保留）。

**改动**

| 文件 | 动作 |
| --- | --- |
| `backend/__init__.py` 或 lifespan | `init_schema() + run_migrations()` → `build_engine() → prepare_schema() → warmup()` |
| `tests/conftest.py` | 建库改 `create_all(engine)`（快）；`reset_database()` → `reset_engine()` |
| `core/db.py::init_schema` | 标记 deprecated（仍保留，供 legacy 路径与快速脚本使用） |

**验收**

- 877 例全绿，且 **pass/fail 集合逐条一致**（不是只看数字）；
- 全量耗时不上涨（`create_all` 应比跑 20 步迁移更快）。

**回滚**：还原启动路径与 conftest（单次提交，`git revert` 可整体撤）。

**⚠️ 这一步之后，仓库仍在用 `connect()`。** 这是有意的过渡态，见「总体节奏」。

---

## T16.6 · 仓库批 A（已条件化的 5 个）

**目标**：把 T15 batch A 已加 `lease()` 的仓库换成 session 实例方法。

**范围**

| 文件 | 函数 | 调用点 |
| --- | --- | --- |
| `log_repo.py` | 4 | 1 |
| `chat_session_repo.py` | 3 | 4 |
| `chat_turn_repo.py` | 6 + 3 辅助 + 2 投影 | 8 |
| `quota_repo.py` | 4（模块级函数） | 服务层直调 |
| `account_repo.py` | 2 | 2 |

**要点**

- `chat_turn_repo.create` 的「`INSERT OR IGNORE` + 回查」在 ORM 里是「查 → 无则 add → `flush()`」，
  两段在同一事务内；部分唯一索引继续兜底并发。
- 删除 `lease()` / `if owned:` / `conn=` 参数；`core/db.py::lease` 进入待删清单（T16.12 删）。

**验收**

- 本批相关的 12 个测试文件全绿；
- C1/C2/C3 守卫对本批 5 个文件转绿（其余文件仍豁免）；
- 反向验证：在 `log_repo` 里加一句 `.commit()` → C1 必须红。

**回滚**：按文件 `git checkout`（每批一个提交）。

---

## T16.7 · 仓库批 B（solution + document）

**目标**：迁移**最难的一块**——有 2 处 `rollback`、3 处 `lastrowid`、1 处 `INSERT...SELECT` upsert。

**范围**：`solution_repo.py`（6 函数 / 6 调用点）、`document_repo.py`（8 方法 + 2 辅助 / 15 调用点）。

**要点**

| 位置 | today | 之后 |
| --- | --- | --- |
| `_ensure_document` | `INSERT ... SELECT ... WHERE s.user_id = ?` + `ON CONFLICT DO NOTHING` | 同一事务内「查方案归属 → 查文档 → 无则 add + flush」；归属条件不得省略 |
| `append_version` | `INSERT` + `lastrowid` + 回填指针 + 2 处 rollback | `add` + `flush` + 赋值 `current_version_id`；失败由 `session_scope` 回滚 |
| `_update_current_version` | 同上 | 同上 |
| 环形外键 | 先空指针插 `documents` 再回填 | ORM 下同样两段，**必须同一事务**，否则 FK 约束在 `flush` 时就炸 |

**验收**

- `documents` / `document_versions` 相关用例全绿（含「先手改后生成」「先生成后手改」两条顺序）；
- 新增 1 条用例：`append_version` 在中途抛异常 → `documents.current_version_id` **不变**、
  无新版本行（证明跨表原子真的成立）；
- C1/C2 对本批转绿。

**回滚**：按文件 `git checkout`。

---

## T16.8 · 仓库批 C（generation_job）

**目标**：迁移改动面最大的单个文件。

**范围**：`generation_job_repo.py`（15 方法 + 3 辅助 / 18 调用点 / 9 处 commit / 15 处 `with connect()`）。

**要点**

- `supersede_active` / `cancel_*` 系列是**条件更新**（`WHERE status='running'`），
  改成 `update(...).where(...)` 并返回 `rowcount`，语义必须一致（调用方靠返回值判断「有没有真的改到」）；
- `persist_progress` 是高频写（≤300ms / 增量 4KB），**每次一个短事务**，不合并；
- 时间列 `updated_at` 仍用 `utc_now_text()`（ADR-077）。

**验收**

- 任务生命周期用例全绿（创建 / 顶替 / 取消 / 失败 / 收尾 / `reap_orphans`）；
- 并发语义用例：两个 `supersede_active` 交错 → 只有一次返回 1；
- C1/C2/C3 对本文件转绿。

**回滚**：`git checkout` 单文件。

---

## T16.9 · 仓库批 D（auth）

**目标**：迁移被所有请求依赖的文件，**放在最后**因为红它等于全站红。

**范围**：`auth_repo.py`（27 函数 / 16 commit / 1 `lastrowid`）。

**要点**

- 登录失败计数是「查 + 写 + 可能清账」三步，收进一个 `session_scope()`；
  ⚠️ 登录链路是同步代码（`api/login_flow.py::_sleep`），改事务不能引入 `time.sleep`；
- `create` 的 `lastrowid` → `flush()` + `user.id`；
- 2 条测试断言 `["is_admin"] == 1`，改为 `is True`（API 层形态由 `schemas.py` 的 `bool` 保证不变）。

**验收**

- 认证 / 邀请码 / 限流 / 管理端权限用例全绿；
- 删号级联用例全绿（`ON DELETE CASCADE` 在 ORM 下由外键 PRAGMA + 表约束保证）；
- C1/C2/C3 对本文件转绿。

**回滚**：`git checkout` 单文件。

---

## T16.10 · 仓库批 E（runtime_config + admin）

**目标**：迁移带「热配置」与「跨 owner 查询」的最后两块。

**范围**：`runtime_config_repo.py`（9 方法 + 1 辅助）、`admin_repo.py`（6 方法 / 6 调用点）。

**要点**

- ⚠️ **单例里不缓存 session**：`get_runtime_config()` 持有的是配置快照，不是 session；
  每次读配置开一个短 scope；
- `upsert_many` 的「整批一个事务」必须保留（配置是一组语义，写一半会撕裂）；
- admin 的跨 owner 查询（JOIN `users`）保持显式 join，边界仍只在路由 `require_admin`。

**验收**

- 配置热更新用例全绿（改配置 → `cache_clear()` → 新值生效）；
- 管理端统计 / 列表 / 审计用例全绿；
- C1/C2/C3 对本批转绿。

**回滚**：`git checkout`。

---

## T16.11 · 服务层事务收口

**目标**：把「一次业务操作 = 一个事务」真正用起来。这是唯一**语义变强**的任务。

**范围**

| 位置 | 改动 |
| --- | --- |
| `services/document_store.commit_generated` | 三步写收进一个 `session_scope()` |
| `services/auth_service` 登录失败计数 | 查 + 写 + 清账一个 scope |
| `services/solution_service` 的「删方案 + 取消任务」 | 一个 scope |
| 其余服务 | 逐处确认「多次仓库调用是否需要原子」，需要就包 |

**验收**

- 新增 3 条原子性用例：中途抛异常 → **一行都不落**（分别是 `commit_generated`、
  登录计数、删方案）；
- 新增「服务方法返回后重开 session 能读到写入」的断言（防「忘了提交 → 静默丢数据」）；
- 877 例全绿。

**回滚**：按服务回滚（每个服务一个提交）。

---

## T16.12 · 拆旧路径 + 守卫封口

**目标**：把过渡期的东西删干净，并让纪律变成永久红灯。

**改动**

| 删除 | 前置条件 |
| --- | --- |
| `core/db.py::connect` / `lease` / `init_schema` / `SCHEMA_SQL` | `repositories/` 里 `import sqlite3` 计数为 0 |
| `core/database.py`（444 行池） | 同上 |
| `tests/test_database_pool.py`（8 条池用例） | 池已删 |
| `core/db.py` 的时间工具 → 移到 `core/clock.py` | 全站 import 改完 |

**新增守卫**（`tests/test_architecture_guards.py`）

| # | 断言 |
| --- | --- |
| C1 | `repositories/` 无 `.commit()` / `.rollback()` |
| C2 | `repositories/` 无 SQL 字面量、无 `import sqlite3` |
| C3 | 仓库方法全是实例方法，`__init__` 首参为 `Session` |
| C4 | `async def` 内 `session_scope()` 的 body 无 `await` |
| C8 | `create_all` 只出现在 `tests/` |

**验收**

- 五条守卫全绿，且**逐条反向验证**（改坏 → 红 → 还原，与 v5 的做法一致）；
- `grep -c "import sqlite3" repositories/` == 0；
- 全量用例数**不少于** 877（删了 8 条池用例，新增的守卫与原子性用例必须补回来）。

**回滚**：单次提交 `git revert`（此步之后旧路径不再存在，回滚等于回到 T16.11）。

---

## T16.13 · 契约对拍与文档收口

**目标**：证明「换数据层，对外行为零变化」。

**改动**

| 项 | 内容 |
| --- | --- |
| 响应形态对拍 | 逐接口比对 `is_admin`/`is_active` 为 `true`/`false`、时间戳仍带 `T`、JSON 字段结构不变 |
| 数据对拍 | 用同一份种子数据，分别经旧/新代码跑关键读接口，响应逐字段 diff |
| 文档 | `v6/README.md` 加「已完成」与最终数字；`T15-data-access-layer.md` 顶部加取代指针；`MEMORY.md` 更新铁律 |

**验收**

- 关键 12 个接口的响应 diff 为空（时间戳字段按正则归一后比较）；
- 全量用例全绿，最终数字写入 `README.md`；
- `MEMORY.md` 里「手写 sqlite3」相关铁律全部改写成 SQLModel 版。

**回滚**：仅文档与测试，无生产代码。

---

## 验收总表

| 判据 | 目标 | 落在哪 |
| --- | --- | --- |
| 全量用例 | ≥ 877，逐条 pass/fail 一致 | 每个任务 |
| 全量耗时 | ≤ 90 秒（当前 72 秒，迁移引入的开销可接受上限） | T16.13 |
| `repositories/` 的 `import sqlite3` | 0 | T16.12 |
| `repositories/` 的 `.commit()` | 0 | T16.12 |
| `repositories/` 的 SQL 字面量 | 0 | T16.12 |
| 基线库 vs 模型库快照 | 完全一致 | T16.4 / T16.12 |
| 模型 vs 库列集合 | 完全一致 | T16.3 |
| 基线 vs 现有库 schema | 语义快照一致 | T16.3 / T16.4 |
| `commit_generated` 原子性 | 中途失败零残留 | T16.11 |

---

## 风险与止损点

| 风险 | 止损方式 |
| --- | --- |
| `sqlmodel` 与现有 `pydantic` 版本冲突 | T16.0 装完立刻跑全量；冲突则锁定 `sqlmodel` 版本，不升级 `pydantic` |
| 模型漏列/错类型 | T16.3 是硬门，不绿不许进 T16.4 |
| `stamp` 掩盖漂移 | `prepare_schema` 里强制校验，不一致拒绝启动 |
| 分批期间两套访问路径并存 | 每批只碰一个仓库文件 + 它的调用点；批与批之间不共享 session |
| 某批迁完后出现「读不到刚写的数据」 | 检查服务层是否漏了 `session_scope`；补「重开 session 能读到」的断言 |
| 全量耗时明显上涨 | 若 `create_all` 建库比 20 步迁移慢，改用「模板库 + 文件拷贝」装置 |

---

## 执行结果（v6 已完成）

### 各任务实际结果

| 任务 | 结果 | 关键点 |
| --- | --- | --- |
| T16.9 auth | ✅ | 4 个类改实例方法；`register` 从「3 次独立提交」收成 1 个事务（邀请码不再被白吃）；`set_admin` 的「数人数 + 改标记」进同一事务（关掉两人互撤清零的窗口） |
| T16.10 runtime_config + admin | ✅ | 聚合查询用 frozen dataclass 投影（`UserUsage` / `TopUser` / `PlatformUsage` / `DailyUsage`）；`create_provider` 的冲突捕获从 `sqlite3.IntegrityError` 改为 `sqlalchemy.exc.IntegrityError` |
| T16.11 服务层收口 | ✅ | `commit_generated` 已是单事务；新增 `job_service.delete_solution_and_cancel_jobs`（删方案 + 取消任务同一事务）；补 6 条原子性 / 可见性用例 |
| T16.12 拆旧路径 + 守卫 | ✅（一处偏差） | `core/db.py`、`core/database.py`、`test_database_pool.py` 已删；时间工具移到 `core/clock.py`；启动只走 `prepare_schema`；`init_schema`/`SCHEMA_SQL` **搬进** `core/migrations.py` 而非删除（理由见下） |
| T16.13 契约对拍 | ✅ | 新增 `tests/test_api_contract_parity.py`（7 条：字段集合、布尔类型、时间戳口径、错误体结构） |

### T16.12 当时的取舍（已被 v6.1 推翻）

T16.12 收尾时，`init_schema` / `SCHEMA_SQL` 是从 `core/db.py` **搬进** `core/migrations.py` 的，
理由是「它们是旧库升级链的 v0 起点，删了会让那条路径失去端到端证据」。

**这条判断在 v6.1 被推翻**：确认没有任何库需要那条路径（没有已交付的自托管实例，
开发库已是 v20 + `alembic_version`），而留着它让「版本」有两个真相。
于是整条链连同两个文件一起删除，见下方 T16.14。

### 测试装置的变化

| 变化 | 说明 |
| --- | --- |
| `tests/dbsupport.py`（新增） | 测试专用的底层连接（`sqlite3.Connection` + `Row` 工厂）与 `init_schema()`。生产代码已无这条路；C9 守住它不外泄 |
| conftest 的收尾守卫 | 从 `Database.stats` 换成 `engine_stats()`；`reset_database()` → `reset_engine()` |
| 仓储调用点 | 约 120 处测试调用改走 conftest 的**适配器实例**（`users` / `auth_sessions` / `system_config` …）：每次开一个 `session_scope()` 调**真实仓储**，读回 dict，断言一字未改 |
| `_models_snapshot()` | 加进程级缓存：否则每个 `client` 用例都要「建临时库 + create_all + 读快照」，900 多个用例白花十几秒 |

### 最终数字

- **975 例全绿**，86 ~ 88 秒（并行 `-n auto`）。（1001 → 975 是因为 T16.14 删掉了
  26 条只服务旧迁移链的用例。）
- 反向验证 7 条：C1（`.commit()`）、C2（`import sqlite3` / SQL 字面量）、C3（模块级函数拿 session）、
  C4（session 跨 `await`）、C8（生产代码 `create_all`）、C9（生产代码碰 `init_schema`）
  ——逐条「写坏 → 转红 → 还原」。
- 唯一放宽的两处断言（都在测试里，且都写了理由）：`test_auth_service.py` 的异常类型
  （SQLAlchemy 包装后的 `IntegrityError`）、`test_admin_management.py` 的布尔断言（`== 1` → `is True`）。


---

## T16.14 · 删除旧版本迁移链（v6.1，已执行）

**目标**：不再支持 v20 之前的库，让「版本」只剩一个真相（`alembic_version`）。

**改动**

| 删除 | 说明 |
| --- | --- |
| `core/migrations.py`（1400+ 行） | `SCHEMA_SQL` + `init_schema` + 20 步 `user_version` 清单 |
| `core/legacy_migrations.py`（96 行） | 旧链的 Engine 适配器 |
| `tests/test_migrations.py`（13 条） | 测的就是这条链本身 |
| `test_config_tables` 的迁移步骤用例（6 条） | v16 建表 / v19 清空壳 / v19 回填，行为已不存在 |
| `test_documents` 的回填用例（5 条） | v2/v5 旧快照回填，行为已不存在 |
| `test_schema_migration::test_db_below_legacy_floor_*` | 「低于 v20 先跑冻结链」这条路径没了 |

**新增 / 改写**

| 项 | 内容 |
| --- | --- |
| `core/schema.py::prepare_schema` | 三条分支：空库 upgrade / 结构正确但未登记 → 校验 + stamp / 已接管 → upgrade |
| `SchemaMismatchError` 文案 | 明确写「旧库（v20 之前）请用当时的代码升级或重建」 |
| 守卫 **C9** | 从「`init_schema` 只能在 migrations.py」改成「旧链不许回来」（文件不存在 + 生产代码无 `user_version` / `SCHEMA_SQL` / `init_schema`） |
| conftest 建库 | 从「每用例 `init_schema` 建 v0」改成「每进程 `prepare_schema` 建模板库 + 每用例拷贝」 |
| `test_schema_parity` | 对拍对象从「20 步链终点」改成「Alembic 基线」 |

**验收**

- 975 例全绿，86 ~ 88 秒；
- `grep -r "user_version" src/` 只剩说明文字（守卫 C9 用 AST 排除 docstring）；
- C9 反向验证三条全红：放回 `core/migrations.py`、生产代码出现 `PRAGMA user_version` 字符串、
  生产代码引用 `SCHEMA_SQL`；
- 旧库现在**拒绝启动**并打印逐条差异（`test_mismatched_db_refuses_to_start` 覆盖）。

**回滚**：`git revert` 单次提交（旧链代码在历史里，随时可取回）。
