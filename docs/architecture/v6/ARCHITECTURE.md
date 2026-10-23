# v6 目标架构

> 本文是**目标状态**（写完之后代码长什么样），不是「现状盘点」。
> 现状数字只在「改动面账本」一节出现，用来标定工作量，不作为问题清单。

---

## 1. 目标形态

```
api/            HTTP 边界：解析请求、鉴权、调服务、组装响应
  ↓
services/       业务用例；**事务边界在这里**（session_scope）
  ↓
repositories/   只做「模型 ↔ 查询」；**不含事务、不含 SQL 字符串**
  ↓
core/models.py  SQLModel 表模型（唯一的结构真源）
core/engine.py  Engine + Session 工厂 + PRAGMA 事件 + 生命周期
  ↓
SQLAlchemy → sqlite3 → 文件
```

三条硬边界：

| 边界 | 谁不许越界 | 判据 |
| --- | --- | --- |
| 事务 | 仓库层不许 `commit()` / `rollback()` | AST 静态扫描（T16.12） |
| SQL | 仓库层不许出现 SQL 字符串字面量 | AST + 字面量扫描（T16.12） |
| 结构 | 建表只能由 Alembic 基线产生 | `alembic check` 无漂移（T16.4） |

---

## 2. 范围

### 本期做

- 14 张表的 SQLModel 模型（含 16 个索引、2 处表级 `UNIQUE`、1 个部分唯一索引）；
- `Engine` + `sessionmaker` + `session_scope()`；
- Alembic 基线迁移 + brownfield `stamp` 路径；
- 11 个仓库文件、102 个函数、86 个 `@staticmethod` 全部改为 session 绑定；
- 服务层事务边界上移，`commit_generated` 的多表写收口为一个事务；
- 删除 `core/db.py`、`core/database.py`（444 行自建池）、`core/migrations.py` 与
  `core/legacy_migrations.py`（`PRAGMA user_version` 的 20 步链，v6.1 / ADR-083）；
- 两条静态守卫 + 三条结构对拍测试。

### 本期不做

| 不做 | 理由 |
| --- | --- |
| 换数据库（PostgreSQL 等） | 本次只换访问方式，不动存储引擎；换库应是独立阶段 |
| 引入 `Relationship()` / 级联加载 | 会引入 N+1 与 detached 懒加载两类故障；join 保持显式（ADR-080） |
| 改 API 契约 | 响应字段、错误码、状态机一律不变；T16.13 逐字段对拍 |
| 改 `PRAGMA` 之外的 SQLite 调优（页大小、`mmap_size`） | 与本次目标无关，混进来会让回归定位变难 |
| 部署 / 上线 | 推迟到 v7（ADR-073） |
| 补测试覆盖 | v5 已完成；本次只补「迁移引入的新故障模式」的守卫 |

---

## 3. 引擎与连接

### 3.1 一个进程一个 Engine

```python
# core/engine.py
engine = create_engine(
    f"sqlite+pysqlite:///{default_db_file()}",
    pool_size=settings.db_pool_size,     # 默认 4
    max_overflow=0,                      # ⚠️ 关键：不许运行期扩容
    pool_timeout=5.0,
    connect_args={"timeout": 10.0, "check_same_thread": False},
    future=True,
)
```

`max_overflow=0` 不是调优参数，是**不变量**：T15 已经证明「多线程同时建连接」会撞上
WAL `-shm` 的 `SQLITE_READONLY`，而那个竞态**只在建连接时发生**。只要运行期一条新连接都不建，
竞态就没有发生的时机。`max_overflow=0` 把「池满」变成 `TimeoutError` 而不是「悄悄扩容建连接」。

### 3.2 PRAGMA 走 connect 事件

```python
@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()
```

三条 PRAGMA 的语义与今天完全一致（WAL 读写并发、写冲突等待、外键级联）。
搬家的意义是：**每条新连接自动带上，不再靠「记得在 `_new_connection` 里加」**。

### 3.3 启动时预建连接

```python
def warmup(engine, size: int) -> None:
    """在启动期单线程建满连接，之后运行期一条都不新建。"""
    held = [engine.raw_connection() for _ in range(size)]
    for conn in held:
        conn.close()
```

这一条**取代**了 T15 的「连接只在 `open()` 时一次性建满」。语义不变，实现交给 SQLAlchemy。

### 3.4 生命周期

| 时机 | 动作 |
| --- | --- |
| 启动 | 建 Engine → 跑迁移 → `warmup()` → 注册到应用状态 |
| 关闭 | `engine.dispose()`（关闭池内全部连接） |
| 测试 | 每个用例换 `DB_PATH` → `reset_engine()` → `create_all()` |

`reset_engine()` 是 `reset_database()` 的替代：`dispose()` + 清模块级单例。测试装置改动集中在
`tests/conftest.py` 一处。

---

## 4. 模型层（`core/models.py`）

### 4.1 表与索引清单

14 张表、16 个索引。以下三处**必须逐字对应**，否则 upsert 与并发判重会静默失效：

| 约束 | 定义 | 谁依赖它 |
| --- | --- | --- |
| `documents` 表级 `UNIQUE (solution_id, kind)` | 表内约束 | `_ensure_document` 的 `ON CONFLICT` 目标 |
| `chat_sessions` 唯一索引 `ux_chat_sessions_thread (solution_id, stage)` | 独立索引 | `ChatSessionRepository.set` 的 upsert 目标 |
| `chat_turns` **部分**唯一索引 `ux_chat_turns_job_role (job_id, role) WHERE job_id IS NOT NULL` | 带 `sqlite_where` | 幂等写轮次（同一个 job 的同一角色只允许一条） |

部分索引在 SQLModel 里要显式写：

```python
__table_args__ = (
    Index("idx_chat_turns_thread", "solution_id", "stage", "id"),
    Index("ux_chat_turns_job_role", "job_id", "role",
          unique=True, sqlite_where=text("job_id IS NOT NULL")),
    {"sqlite_autoincrement": True},
)
```

### 4.2 四条列映射纪律

**① `AUTOINCREMENT` 必须显式开。**
SQLModel 的 `id: int | None = Field(default=None, primary_key=True)` 生成的是
`INTEGER NOT NULL PRIMARY KEY`，**没有** `AUTOINCREMENT`。少了它，SQLite 会复用已删除的最大 rowid。
现有 12 张表的 `id` 都是 `INTEGER PRIMARY KEY AUTOINCREMENT`，因此每张表都要
`__table_args__ = {"sqlite_autoincrement": True}`。

> 为什么要较真：`app_logs` / `admin_audit_log` 的 id 被当作「页码游标」用，
> 复用 id 会让「翻到下一页」看到上一次已删除的行。

**② 保留字列名用「属性改名 + 列名绑定」。**
`document_versions.metadata` 与声明式基类的 `Base.metadata` 同名，直接写 `metadata: str` 会抛
`InvalidRequestError`。正确写法：

```python
meta: str = Field(default="{}", sa_column=Column("metadata", TEXT, nullable=False, server_default=text("'{}'")))
```

同批要检查的还有 `documents`、`chat_turns`、`solutions` 的全部列名（T16.2 验收项：
用 `dir(Base)` 与列名求交集，必须为空）。

**③ 布尔列用 `bool`，但类型名要归一。**
`users.is_active` / `is_admin`、`model_providers.builtin` / `enabled` / `thinking_enabled`、
`chat_turns.interrupted` 用 `bool`。SQLModel 会映射成 `BOOLEAN`，而现有库是 `INTEGER`。

- 行为上无差异：SQLite 两者都存 0/1，读出来都是 Python `bool`；
- **但 Alembic autogenerate 会因此想 ALTER 列**（幻影迁移）。

处置：`alembic/env.py` 的 `compare_type` 对「`BOOLEAN` ↔ `INTEGER` 且列名在白名单内」归一，
不产生迁移。白名单就是上面 6 个列，写在 `migrations/env.py` 里并加注释。

> 为什么不干脆保留 `int`：`is_admin == 1` 这种写法会让「真假」与「1」混在一起，
> 而 `api/schemas.py` 早已声明 `is_admin: bool`。模型层对齐 `bool` 是**收敛**，不是扩张。

**④ 时间戳保持 `TEXT`，不换 `DateTime`。** 见 ADR-077。

### 4.3 JSON 列

| 列 | 处置 | 理由 |
| --- | --- | --- |
| `solutions.data` | **保持 `TEXT`** | 后端「不透明快照」，从不解析；换 JSON 列等于给不透明数据加了结构假设 |
| `generation_jobs.completed_sections` / `usage` / `pending_items` | 保持 `TEXT` | 见下 |
| `document_versions.pending_items` / `metadata` / `last_run_summary` | 保持 `TEXT` | 见下 |
| `app_logs.extra_json` | 保持 `TEXT` | 日志是逃生通道，序列化失败不能影响写日志 |
| `model_providers.extra_json` | 保持 `TEXT` | 同上 |
| `system_config.value` | 保持 `TEXT` | 存的是「JSON 编码的标量」，本质是字符串 |

统一理由：现有代码用 `json.dumps(..., ensure_ascii=False)` 手工序列化，并在读侧有
`_decode_list` / `_dumps` 这类容错函数（历史坏数据不会让读路径崩）。
换成 `Column(JSON)` 会**同时**改变落盘字节（分隔符、`ensure_ascii`）与容错行为，
而收益只是「少两行序列化代码」。

这条是**有意的取舍**，与「不为开发成本妥协」不冲突：我们不为省事留着 SQL 字符串，
但也不会为了类型好看去动已经在库里的字节。见 ADR-077。

---

## 5. 会话与事务

### 5.1 两种作用域

```python
# core/engine.py
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

@contextmanager
def session_scope() -> Iterator[Session]:
    """一个业务操作 = 一个事务。正常提交，异常回滚，无论如何关闭。"""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
```

`expire_on_commit=False` 是**必需的**：服务层拿到模型对象后会在事务外读属性（组装响应、
写日志）。默认的 `expire_on_commit=True` 会让 `.commit()` 之后所有属性变过期，
读一次就触发一次 SELECT，而 session 已关闭 → `DetachedInstanceError`。

`autoflush=False` 是**有意的**：flush 时机显式化，避免「读一次就顺手把半成品写进去」。

### 5.2 仓库方法长什么样

```python
class GenerationJobRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, owner_id: int, kind: str, ...) -> GenerationJob:
        job = GenerationJob(owner_id=owner_id, kind=kind, status=STATUS_RUNNING, ...)
        self.session.add(job)
        self.session.flush()          # ⚠️ 替代 lastrowid，拿自增 id
        return job
```

三条纪律：

1. **不 commit、不 rollback、不 close** —— session 的生命周期归调用方；
2. **需要 id 就 `flush()`**，不要 `session.commit()`；
3. **返回模型对象**，不返回 `dict`。

第 3 条是本次改动面最大的一条（71 个调用点）。理由是它带来的是**编译期**收益：
`job.stauts` 会被类型检查抓住，`job["stauts"]` 要等运行时。见 ADR-078。

### 5.3 事务边界画在哪

| 场景 | 边界 | 现在 |
| --- | --- | --- |
| 读操作（列表、详情） | 一个 `session_scope()` 包整次读 | 一致 |
| 单条写（改标题、删方案） | 一个 `session_scope()` | 一致 |
| **`commit_generated`**（写 `documents` + `document_versions` + 回填 `current_version_id`） | **一个 `session_scope()`** | ⚠️ 现在是三次独立提交 |
| 任务进度 flush（≤300ms / 4KB） | 每次 flush 一个短事务 | 一致（不能合并，否则长事务持锁） |
| 登录失败计数（查 + 写 + 可能清账） | 一个 `session_scope()` | 现在是三条独立语句 |

`commit_generated` 是本次唯一**语义变强**的地方：今天它分三步写（确保 documents 行、
插版本、回填指针），任一步失败都会留下「版本写了但指针没动」的中间态。
换成一个事务后，要么全成要么全不成。

### 5.4 一条必须守住的纪律：session 不能跨 `await`

```python
# ❌ 绝对禁止
with session_scope() as s:
    await llm.astream(...)   # 一条池连接被占满整个流式期间

# ✅ 正确
with session_scope() as s:
    job = repo.get(job_id)
    payload = job.to_payload()
# ... await ...
with session_scope() as s:
    repo.persist_progress(payload)
```

原 G-DB4 守卫（「连接不跨 `await`」）**保留并改写**为 session 版本。它在 T16.12 与
「仓库层无 commit」同批落地，扫描对象从 `connect()` 换成 `session_scope()`。

---

## 6. 迁移（Alembic）

### 6.1 目录

```
backend/
  alembic.ini
  migrations/
    env.py
    script.py.mako
    versions/
      0001_baseline.py      ← 由 autogenerate 生成，是全量 DDL
```

### 6.2 启动时的三条路径

```python
def prepare_schema(engine) -> str:
    if not _has_alembic_version(engine) and not _is_empty(engine):
        assert_schema_matches_models(engine)   # 漂移校验，不符就拒绝启动
        command.stamp(cfg, "head")             # 只登记，不执行 DDL
    command.upgrade(cfg, "head")
```

| 库的状态 | 走哪条 | 结果 |
| --- | --- | --- |
| 空库 | `upgrade head` | 建全量 schema |
| 已有当前结构（无 `alembic_version`） | `assert` → `stamp head` | 一行 DDL 都不执行，只登记版本 |
| 已接管（有 `alembic_version`） | `upgrade head` | 正常增量迁移 |
| **旧库（v20 之前）** | `assert` 失败 → **拒绝启动** | 打印逐条差异，并说明旧链已删除 |

⚠️ **`stamp` 之前必须做漂移校验。** 直接 stamp 的风险是：如果基线 DDL 与真实库有一列之差，
这个差会**永久隐身**——`alembic check` 认为已是最新，而代码按模型读会读到不存在的列。
校验方式见 §9.1。

### 6.3 20 步旧迁移：已整体删除

原计划是把 `core/migrations.py`（1365 行）冻结成 `core/legacy_migrations.py`、留到 v7。
实际在 **v6.1 就删干净了**（ADR-083）：

- 没有任何库需要它（没有已交付的自托管实例，开发库已是 v20 + `alembic_version`）；
- 留着它意味着「版本」有两个真相（`user_version` 与 `alembic_version`），
  每个读这段代码的人都得先理解「为什么有两套」再判断「我改的库算哪套」；
- 旧库现在的归宿是**拒绝启动**——比「悄悄 stamp 成最新」诚实，也比它安全。

守卫 **C9** 守着它不许回来。

### 6.4 验证「基线 == 现有库」

```python
def test_baseline_matches_legacy_schema(tmp_path):
    a = tmp_path / "baseline.db"    # 基线迁移建出来的库
    b = tmp_path / "alembic.db"     # alembic upgrade head
    assert schema_snapshot(a) == schema_snapshot(b)
```

`schema_snapshot()` 不做文本比较（`INTEGER NOT NULL` 的空白、列序、约束渲染顺序都会变），
而是比较**语义快照**：

```python
{t: {
   "columns":  [(name, type_norm, notnull, default, pk), ...],
   "indexes":  [(name, unique, [cols], where), ...],
   "fks":      [(from, to_table, to_col, on_delete), ...],
}}
```

`type_norm` 把 `BOOLEAN` 归一成 `INTEGER`（见 §4.2③），其余类型原样。

---

## 7. 仓库层契约

### 7.1 签名约定

```python
class XxxRepository:
    def __init__(self, session: Session) -> None: ...
```

- **实例方法**，不再 `@staticmethod`；
- 所有查询显式 `.where(...)`，不用 `session.get()` 做归属校验（`session.get()` 只按主键，
  绕过 `owner_id` 条件会越权）；
- 涉及用户数据的查询**必须带 `owner_id` 条件**，与今天一致。

### 7.2 四个 upsert 的写法

| 位置 | 今天 | 之后 |
| --- | --- | --- |
| `ChatSessionRepository.set` | `ON CONFLICT DO UPDATE` | `sqlite_insert(...).on_conflict_do_update(index_elements=["solution_id","stage"], set_=...)` |
| `DocumentRepository._ensure_document` | `INSERT ... SELECT ... ON CONFLICT DO NOTHING` | `sqlite_insert(...).from_select(...)` + `on_conflict_do_nothing()` |
| `SystemConfigRepository.upsert_many` | `executemany` + `ON CONFLICT DO UPDATE` | 循环 `on_conflict_do_update`，同 `session`（一个事务） |
| `ModelProviderRepository` 写配置 | 普通 INSERT | 不变 |

⚠️ `_ensure_document` 的 `INSERT ... SELECT` 里带着归属条件（`WHERE s.user_id = ?`），
这是「归属不通过就一行都插不进去」的承重写法。改成 ORM 后必须保留：先查方案归属、
再插文档行，两步在**同一个事务**里——因为事务隔离已经保证「查的时候没有、插的时候被别人插了」
不会发生（同一个 `BEGIN IMMEDIATE` 序列化）。

### 7.3 行 → 模型

今天仓库返回 `dict[str, Any]`（`sqlite3.Row` 转的）。之后返回 ORM 对象。
服务层有两类地方要改：

- `job["status"]` → `job.status`；
- 把整行塞给 Pydantic 响应模型时，用 `Model.model_validate(obj, from_attributes=True)`。

`chat_turn_repo.to_history_item` / `to_display_turn` 这类**投影函数**保留，
入参从 `dict` 改为模型对象。

### 7.4 模型不声明 `Relationship()`

理由（ADR-080）：

- 声明了就会在「session 已关闭」时触发懒加载 → `DetachedInstanceError`，
  而这类错误只在高并发路径上偶发；
- 现有 join 只有三处，显式 `select(...).join(...)` 更清楚；
- 避免 N+1：`Relationship` 的默认 `lazy="select"` 是 N+1 的经典来源。

---

## 8. 启动与关闭顺序

```
lifespan 启动
  ├─ 1. build_engine()            # 不建连接
  ├─ 2. prepare_schema(engine)    # 迁移 / stamp（此时才建连接，单线程）
  ├─ 3. warmup(engine, size)      # 建满池
  ├─ 4. get_runtime_config()      # 读配置（首次读会走 session）
  └─ 5. start_workers()
lifespan 关闭
  └─ engine.dispose()
```

⚠️ 顺序不能换：`prepare_schema` 必须在 `warmup` 之前（迁移会改 schema，
  预建连接之后再迁移没意义）；`get_runtime_config` 必须在迁移之后（配置表可能刚被建出来）。

---

## 9. 测试装置

### 9.1 三条结构对拍（新增）

| 测试 | 断言 |
| --- | --- |
| `test_baseline_produces_exactly_the_model_schema` | `alembic upgrade head` 的库 == 模型建出来的库（语义快照） |
| `test_stamp_preserves_existing_db` | v20 库 → `prepare_schema` → 数据一行不少、schema 不变、`alembic_version` 已写入 |
| `test_every_column_is_mapped` | `PRAGMA table_info` 的列名集合 == 模型字段（或 `Column("名")`）集合 |

第 3 条专门抓「模型少一列」这种静默故障：少一列的后果是「写不进去、读出来是默认值」，
不报错。

### 9.2 单元测试怎么建库

- 每个用例：`create_all(engine)`（快，约 3ms）；
- **不**每个用例跑 Alembic（877 个用例 × 跑迁移会让全量从 72 秒涨到数分钟）；
- 迁移本身由 §9.1 三条测试覆盖，各跑一次。

### 9.3 必须新增的四条行为守卫

| 守卫 | 抓什么 |
| --- | --- |
| 仓库层无 `commit`/`rollback` | AST 扫描（替换 T15 的 `test_every_repo_commit_is_guarded_by_owned`） |
| 仓库层无 SQL 字面量 | 字面量含 `SELECT`/`INSERT`/`UPDATE`/`DELETE`/`PRAGMA` 即红 |
| `session_scope` 不跨 `await` | AST：`async def` 内 `with session_scope()` 的 body 不得含 `await` |
| 布尔列类型归一白名单 | 模型 `Boolean` 列集合 == 白名单（6 列），

新加布尔列必须显式登记 |

---

## 10. 约束清单（可被机器检查的全部条款）

| # | 条款 | 谁检查 |
| --- | --- | --- |
| C1 | `repositories/` 无 `commit()` / `rollback()` | AST 守卫 |
| C2 | `repositories/` 无 SQL 字符串字面量、无 `import sqlite3` | AST + 字面量守卫 |
| C3 | 仓库方法全部是实例方法，构造参数是 `Session` | AST 守卫 |
| C4 | `async def` 内 `session_scope()` 的 body 无 `await` | AST 守卫 |
| C5 | 模型列集合 == 库列集合 | `test_every_table_is_modelled` |
| C6 | 基线迁移建出来的库 == 模型建的库（语义快照） | `test_baseline_produces_exactly_the_model_schema` |
| C7 | 布尔列集合 == 白名单 | 模型断言 |
| C8 | 建表只能由 Alembic 产生（`create_all` 仅限测试） | AST：生产代码不得调 `create_all` |
| C9 | 旧迁移链（`user_version` 20 步）不许回来 | AST + 文件存在性检查 |

### ⚠️ C6 为什么不是 `alembic check`

**实测（SQLAlchemy 2.1.6 + SQLite 方言）：`alembic check` 在本项目里恒红**，原因是两个方言反射缺陷：

| 缺陷 | 表现 |
| --- | --- |
| `PRAGMA foreign_key_list` 的 `on_delete` 没被反射进 `options` | 每个带 `ON DELETE` 的外键都被判成 `remove_fk` + `add_fk` |
| 主键列反射成 `nullable=True`（`INTEGER PRIMARY KEY` 声明里没有 `NOT NULL`） | 每个主键都被判成 `modify_nullable` |

两者都不是真实漂移。在测试里过滤幻影差异的代价是「过滤条件一多，真差异也会被吞掉」，
所以改用**反射无关**的判据：直接比较「基线建出来的库」与「模型建出来的库」的语义快照
（`core/schema_snapshot.py`）。它同时覆盖「模型改了却忘了写迁移」——模型一变，两边快照立刻不等。

代价：C6 不检查「迁移脚本的历史链是否完整」（比如两条迁移顺序写反）。本项目只有一个基线，
这条风险目前为零；出现第二条迁移时必须重新评估。


---

## 11. 改动面账本

> 数字来自 AST 与 grep 实测，用于标定分批与工期，不是问题清单。

| 项 | 数量 | 说明 |
| --- | --- | --- |
| 表 | 14 | 全部由 Alembic 基线建（不再有第二份 DDL） |
| 索引 | 16 | 含 2 个唯一索引、1 个部分唯一索引、2 处表级 `UNIQUE` |
| 迁移步骤 | 20 → **0** | 旧链已删除（ADR-083），结构变更只写成 Alembic 迁移 |
| 仓库文件 | 11 | 其中 10 个有类，`quota_repo` 只有模块级函数 |
| 仓库函数 | 102 | 86 个 `@staticmethod`、16 个模块级/私有辅助 |
| `commit()` | 44 | 全部删除 |
| `rollback()` | 2 | 全部删除 |
| `lastrowid` | 6 | 全部换 `flush()` |
| `ON CONFLICT` | 4（仓库层）+ 3（迁移） | 迁移里的随基线消失 |
| `json_extract` | 5 | 全在迁移 v19，随基线消失，**无需移植** |
| `PRAGMA` | 3（连接）+ 16（迁移守卫） | 连接 3 条搬进事件；迁移 16 处随基线消失 |
| 服务/API 调用点 | 71 | 改属性访问 + `session_scope()` 包装 |

### 分批顺序的依据

| 批 | 文件 | 为什么这个顺序 |
| --- | --- | --- |
| A | `log_repo` / `chat_session_repo` / `chat_turn_repo` / `quota_repo` / `account_repo` | T15 已加 `lease()`，事务边界已条件化，改起来是「换壳」；且不影响核心链路 |
| B | `solution_repo` / `document_repo` | `document_repo` 有 2 处 rollback、2 处 lastrowid、1 处 upsert，是**最难的一块**，放在有 A 批经验之后 |
| C | `generation_job_repo` | 18 函数 / 9 commit / 15 处 `with connect()`，改动面最大但模式单一 |
| D | `auth_repo` | 27 函数 / 16 commit，模式单一；放在最后因为它被所有请求依赖，红了全站红 |
| E | `runtime_config_repo` / `admin_repo` | 有热配置读取，需要额外确认「读配置不缓存 session」 |

---

## 12. 风险与对策

| 风险 | 症状 | 对策 |
| --- | --- | --- |
| 模型列名/类型与库不一致 | 写不进去 / 读到默认值 / 幻影迁移 | C5 + C6 两条机器检查 |
| `stamp` 掩盖漂移 | 上线后某列「消失」 | `stamp` 前强制 `assert_schema_matches_baseline` |
| `expire_on_commit=True` | 事务外读属性抛 `DetachedInstanceError` | 固定 `False`，并用一条用例覆盖「commit 后读属性」 |
| session 跨 `await` | 池被占满，接口全超时 | C4 静态守卫 |
| 分批迁移期间两套写法并存 | 同一张表两种访问方式，事务边界混乱 | 每批独立可运行、可回滚；批与批之间**不共享** session |
| 布尔列 `BOOLEAN` → 幻影迁移 | `alembic check` 恒红 | 白名单归一（C7） |

---

## 13. 与 v5 资产的接口

| v5 资产 | v6 处置 |
| --- | --- |
| `tests/` 全部用例（877） | **保留**，作为迁移的回归网；只改建库方式（§9.2） |
| `test_database_pool.py`（10 条） | **已删**（池不存在了）；`lease`/`owned` 的守卫由 C1/C3 接管 |
| `core/database.py`（444 行池） | **已删**（T16.12） |
| `core/db.py::connect` / `lease` | **已删**；测试侧改用 `tests/dbsupport.py::connect`（仅限 tests） |
| `core/db.py::SCHEMA_SQL` / `init_schema()` | **已删除**（随旧链一起，ADR-083）。旧库不再自动升级，会拒绝启动 |
| `core/db.py` 时间工具（`utc_now` 等） | **已搬到** `core/clock.py` |
| `core/migrations.py` / `core/legacy_migrations.py` | **已删除**（v6.1 / ADR-083）；守卫 C9 守着它们不许回来 |
| `T15-data-access-layer.md` | 保留为历史记录，顶部加「已被 v6 取代」指针 |
