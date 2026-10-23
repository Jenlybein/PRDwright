# T15 · 数据访问层：`Database` 对象 + 单元事务

> 状态：**已被 v6 取代，且 v6 已完成**（2026-10-23，1001 例全绿）。对应决策见 `ADR.md` 的 ADR-072。
> ⚠️ 本文描述的 444 行自建连接池、`lease()` / `owned` 闸门、手写 `SCHEMA_SQL` 与
> `PRAGMA user_version` 迁移链，在 **v6（`docs/architecture/v6/`，ADR-073~082）** 中
> 全部交由 SQLModel + SQLAlchemy + Alembic 承担。最终落地状态：`core/database.py`、
> `core/db.py` 的 `connect`/`lease` 已删除；`SCHEMA_SQL` 与 `init_schema()` 作为冻结链的
> v0 起点搬进 `core/migrations.py`，生产启动只走 `prepare_schema`（守卫 C9）。
> T15.3 冻结在 batch A 的那 5 个文件已在 T16.6 完成迁移。
> 本文保留为历史记录：**它记录了「为什么当时的做法是对的」，这个判断在 2026-10 之前一直成立。**
> 读本文时请把「目标形态」当作历史方案，不要照着实现。

## 1. 目标与范围

三句话目标：

1. 让「多线程同时首次映射 WAL `-shm`」这个故障**在结构上不可能发生**，而不是靠一把锁把它挡住；
2. 把事务边界从「一个仓库函数」上移到「**一次业务操作**」，让跨仓库写入具有原子性；
3. 把「用例之间的数据库隔离」从「改全局环境变量 + 清一堆缓存」变成「**换一个 `Database` 实例**」。

### 本期做

| # | 内容 |
| --- | --- |
| 1 | 新增 `core/database.py`：`Database` 对象 + 固定连接的连接池 |
| 2 | `db.connect()` 改为委托 `Database.acquire()`（对外签名不变） |
| 3 | 归还连接时强制回滚未提交事务，并计入统计 |
| 4 | 仓库函数支持「传入连接」，服务层据此拥有事务边界 |
| 5 | 删除 `db._bootstrap_lock`，改由「连接只在 open 时创建」保证不变量 |
| 6 | 五条确定性守卫（见第 6 节） |

### 本期不做

| 不做 | 理由 |
| --- | --- |
| 换 SQLAlchemy / 换 Postgres | SQLite 单写者是本项目的既定边界；换存储是另一个量级的事，不属于「访问层封装」 |
| 异步驱动（aiosqlite） | 现有调用链全是同步函数（服务层在 `run_in_threadpool`/任务线程里跑），换异步驱动要改调用方语义，收益不明 |
| 拆 `repositories/` 目录 | 目录结构不是本期的痛点，事务边界才是 |
| 改 lifespan 的 `init_schema + run_migrations` | 生产上本来就只跑一次；测试侧的开销属于测试装置问题（T04 已用并行解决），不为此改生产启动路径 |
| 连接池动态伸缩 | 见 3.2：**连接数恒定**是「竞态不可能发生」的前提 |

## 2. 现状事实（均已实测，不是推断）

| 事实 | 数值 / 位置 |
| --- | --- |
| `with connect()` 使用点 | **92 处**：`repositories/` 89、`core/` 2、`api/` 1 |
| 直接开事务的公共仓库函数 | **89 个**（88 公共 + 1 私有）；公共里 **43 写 / 45 只读**。写函数里共 44 处 `.commit()`、2 处 `rollback()`（全在 `document_repo`） |
| 仓库内显式 `.commit()` | **44 处**（auth 16、generation_job 9、runtime_config 5、其余 14） |
| 共用的 `connect()` 实现 | `core/db.py:279`，每次操作新建连接 + 3 条 PRAGMA，`yield` 后关闭 |
| 单次 `connect + SELECT 1 + close` | **1.98 ms**（p50 1.93，300 次；预热后） |
| 路径解析 | `db_file_path()` **每次调用**读 `get_settings()`，所以路径可在运行期变化 |
| 测试隔离方式 | `conftest.py:39` 每个用例 `DB_PATH = tmp_path/test.db` |
| 已有的正确做法 | `document_repo.append_version`（`repositories/document_repo.py:171`）在一个 `with connect()` 里完成四步写入，并把 `conn` 传给 `_ensure_document(conn, ...)` |
| 事务边界止步于 | 仓库函数：服务层调两个仓库 = 两个事务 |

**最后两行是本期设计的前提**：连接注入（`conn` 作为参数往下传）在本项目里**已经是局部惯例**，本期只是把它提升为全局约定。

## 3. 设计

### 3.1 组件关系

```
services/            ← 拥有事务边界：with db.transaction() as tx
   │                    一次业务操作 = 一个事务（可跨多个仓库）
repositories/        ← 只写 SQL；函数接受 tx（缺省自己开）
   │
core/database.py     ← Database：持有连接池、路径、PRAGMA、生命周期、统计
   │
sqlite3              ← WAL
```

`db.connect()` 保留为兼容入口（92 处调用点一行不用改），内部改为委托：

```python
@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    with get_database().acquire() as conn:
        yield conn
```

### 3.2 连接池：数量恒定、只在 open 时创建

这是整个方案的核心决策，也是「可以删锁」的**唯一**依据。

```
Database(path, size=4)   # 可用 DB_POOL_SIZE 覆盖
  .open()      ← 启动期、单线程：一次性建满 size 条连接（WAL -shm 只映射这一次）
  .acquire()   ← 借出 / 归还，期间不创建任何新连接
  .dispose()   ← 全部关闭（路径切换、测试 teardown）
```

三条不变量：

1. **连接总数恒定**（`size` 条），运行期不扩容、不新建；
2. 因此「多线程同时首次映射 `-shm`」**没有发生的时机**——竞态消失，而不是被锁挡住；
3. 连接以 `check_same_thread=False` 创建（现状已如此），借出期间**独占**，不允许多人同时用同一条。

为什么不做动态伸缩：一旦允许运行期扩容，扩容那一刻就是「多线程建连接」，又把竞态请回来了；要么给扩容加锁（等于锁没删），要么预建满。预建 4 条 SQLite 连接的代价可以忽略，所以选预建（默认 `DB_POOL_SIZE=4`）。

连接损坏（磁盘错误导致连接不可用）时的替换是唯一例外：走 `_replace_locked()`，拿一把只在故障路径上的窄锁。这条路径不在热路径上，且会被记进 `stats["replaced"]`。

#### 两条实现约束（否则守卫会假绿）

1. **`connect()` 必须作为可注入的工厂参数**：`Database(path, size=..., connect_factory=sqlite3.connect)`。
   测试靠它注入计数器。⚠️ **`core/database.py` 里禁止写 `from sqlite3 import connect`**——那样
   monkeypatch 会落空、计数恒为 0，而 `0 != size` 恰好"看起来通过"，是最难发现的假绿。

2. **`open()` 必须先于任何 `connect()` 发生**。启动链路里 `init_schema()` 与
   `logging_setup.install_log_persistence()` 内的 `LogRepository.prune()` 都会走 `connect()`，
   任何一个先跑，就会触发"第一次借用时才建连接"，直接破坏「只在 open 时创建」这条唯一依据。
   做法：让 `get_database()` 在创建实例时**立即 `open()`**（在初始化锁内完成），
   这样"谁先谁后"不再依赖调用顺序。

#### `dispose()` 的语义（必须写死，否则必偶发）

`dispose()` 置 `closed` 标志 → 关闭池内空闲连接 → **不等待在途借用**。配套三条：

| 场景 | 行为 |
| --- | --- |
| 在途连接归还到已 `closed` 的池 | 直接 `conn.close()`，**不 put 回池**（否则池被复活） |
| `closed` 之后调用 `acquire()` | 抛 `PoolClosed`（不是 `PoolExhausted`、更不是自动重开） |
| teardown 撞上后台日志线程仍在借用 | 不阻塞等待；该连接在归还时按上一条直接关闭 |

#### 另外三处实现细节（文档原先没写，实现时需要）

1. **`open()` / `dispose()` 自带生命周期锁**。因为「未打开时借用」这条兜底路径（见下）可能被
   多线程同时撞上；没有锁就会出现两个线程各建 `size` 条、后一批在 `put_nowait` 上撞 `queue.Full`。
2. **`Database.closed` 属性**：G-DB3 要断言"旧池已 dispose"，靠 `stats["idle"] == 0` +
   `closed` 两处才说得清。
3. **路径解析放在 `database.default_db_file()`**，`db.db_file_path()` 委托它。
   依赖方向是 `db → database`，反过来 import 会成环；对外签名、返回值与 `mkdir` 副作用不变。

#### `acquire()` 在池未打开时怎么办

**抛 `PoolClosed`，不要「补一次 `open()`」**。理由：

- 那会让 `dispose()` 之后仍能被静默复活——`reset_database()` 做完、若还有后台线程调用
  `connect()`，就会为**已删除的临时库**重新建 4 条连接并重建文件，测试清理随之失效；
- 生产路径不受影响：`get_database()` 一定先 `open()`。

> 注意：**加了生命周期锁不等于可以不删这条兜底**。锁只解决"并发建两份"，
> 不解决"关闭后又被复活"。守卫 G-DB3 与 `test_acquire_on_closed_pool_raises` 各守一头。

#### 已知行为变化（不是缺陷，但要写进 `connect()` 的 docstring）

池化之后 `with connect() as conn:` **不再在退出时关闭连接**（改为归还）。因此
"在 `with` 块外继续使用 `conn`" 不会再立刻抛 `sqlite3.ProgrammingError(closed)`，
而是可能读到一条已归还、正被他人使用的连接。全量对拍 0 差异说明现有调用点不依赖旧语义，
但这是一个**悄悄变宽松**的地方，必须在 docstring 里写明。

### 3.3 借出与归还

```
acquire():
    conn = pool.get(timeout=acquire_timeout)      # 池空则等待，超时抛 PoolExhausted
    broken = False
    try:
        yield conn
    except sqlite3.Error:                          # 连接可能已坏（磁盘 / 句柄）
        broken = True
        raise
    finally:                                       # ⚠️ 必须 finally，不能 else
        if conn.in_transaction:                    # 调用方漏了 commit / 抛了异常
            conn.rollback()
            stats["rolled_back"] += 1
        if 池已关闭 or 代次不符:
            conn.close()                           # 绝不放进新池
        elif broken:
            _replace_locked(conn)                  # 故障路径，窄锁补一条
        else:
            pool.put(conn)
```

⚠️ **必须是 `try/finally`，不能写 `try/except/else`**：块里抛出的异常若不是 `sqlite3.Error`
（`ValueError` / `KeyError` / `CancelledError`…），`except` 不匹配、`else` 也不执行 ——
**连接一次都不会归还**。实测：`size=1` 时抛 1 次 `ValueError` 就 `in_use=1 / idle=0`，
下一次借用立刻 `PoolExhausted`；默认 `size=4` 意味着 **4 次普通异常之后全站不可用**。
这是本模块最容易写错、也最难从症状反查的一处。

**不做归还时的 `SELECT 1` 探针**：那会给每次操作加一次往返（约 0.1ms × 全部 DB 操作），
而它能发现的故障（连接损坏）在"使用中抛 `sqlite3.Error`"这一条上已经能捕获，代价却是常驻的。

**归还前回滚是硬要求**，不是优化项：

- 现状是「每次新建连接」，所以「漏 commit」的影响被限制在一次调用内；
- 池化复用后，未提交事务会跟着连接回到池里，下一个借用者可能看到脏数据、或持锁不放。

### 3.4 事务边界：`UnitOfWork`

```python
# services/ 里
with db.transaction() as tx:          # 一个连接 + 一个事务
    job_repo.finish(tx, job_id, ...)
    document_store.commit_generated(tx, ...)
```

仓库函数统一改成「可传入连接」：

```python
def append_version(..., conn: sqlite3.Connection | None = None) -> int | None:
    with lease(conn) as (c, owned):   # 传了就用传入的；没传就自己开（旧调用点不受影响）
        ...
        if owned:                     # 自己开的连接 = 提交由自己负责
            c.commit()
```

`lease()` 是新建的小工具（`core/db.py`）：`yield (conn, owned)`。传了 `conn` 就 `owned=False`
——**不提交、不回滚**，事务边界归外层；没传就 `owned=True`，调用方写完之后自己 `commit()`。

为什么用 `owned` 布尔量而不是「传没传连接再判一次」：后者在函数中途被重新赋值之后就不可靠，
读代码的人还得回头找默认值才知道这一段在不在事务里。而 `if owned:` 是可以被 AST 静态守卫的
（`tests/test_database_pool.py::test_every_repo_commit_is_guarded_by_owned`），
「漏改一处」不再依赖人工过一遍。

**只读函数也要接受 `conn`**：不只是为了将来可能加写操作。`finish` 写完 → `get` 读回来，
若后者另开连接，WAL 下会读到**提交前**的旧值（WAL 的隔离是连接的隔离，不是进程的）。
「刚生成完就查不到正文」这类故障最难查，所以 45 个只读函数一并迁移。

这样 89 个仓库函数（43 写 + 45 只读 + 1 私有）可以**逐个迁移**，不必一次性改完；
未迁移的继续走「自开事务」，行为不变。

### 3.5 错误映射与重试

| `sqlite3` 错误 | 语义 | 处理 |
| --- | --- | --- |
| `SQLITE_BUSY` | 短暂争用 | 有界重试（上限 3 次，10/20/40ms 退避）；超限抛 `DatabaseBusy` |
| `SQLITE_READONLY` | IO / 部署错误，重试无用 | **不重试**，立即抛 `DatabaseReadOnly` |
| 其余 `OperationalError` | 未知 | 原样上抛，不吞 |

现状的问题正是这两类被混在一起看，才让「调大 `busy_timeout` 完全没用」这件事看起来像谜。

### 3.6 可观测性

`get_database().stats` 暴露：

| 字段 | 用途 |
| --- | --- |
| `size` / `in_use` / `idle` | 池是否被打满 |
| `wait_ms_total` / `wait_ms_max` | 借不到连接的时间（真实瓶颈信号） |
| `rolled_back` | 调用方漏提交的次数——**这是个告警指标**，正常应恒为 0 |
| `replaced` | 连接损坏替换次数 |
| `pool_exhausted` | 超时次数 |

这些字段后续接进 T14 的接口性能面板，让「池该多大」变成数据问题。

## 4. 关键取舍

| 决策 | 选择 | 否决的方案与理由 |
| --- | --- | --- |
| 连接数 | **open 时预建满、运行期恒定** | 动态伸缩：扩容那一刻重新引入竞态，等于锁没删 |
| 池实现 | 自研（`queue.LifoQueue`） | SQLAlchemy `QueuePool`：为 4 条 SQLite 连接引入一个 ORM 生态，依赖与心智成本都不对等 |
| 事务 API | `db.transaction()` 上下文管理器 | 装饰器式 `@transactional`：无法表达「在事务里传连接」这一层 |
| 仓库兼容 | `conn=None` 缺省自开 | 一次性改签名：92 处调用点全断，且无法分批验收 |
| 归还策略 | 强制 `rollback` | 只清理 `commit` 成功的：漏提交正是要防的那种 |
| 锁的去留 | **删掉**，由恒定连接数保证 | 保留锁 + 池：锁已经没有保护对象，留着会让人误以为并发引导仍可能发生 |

## 5. 落地顺序

每一步独立可验收，**上一次全绿才做下一步**。

| 编号 | 内容 | 落点 | 依赖 |
| --- | --- | --- | --- |
| **T15.1** | `Database` + 池 + `connect()` 委托 + **同一步删锁**；守卫 G-DB1 / G-DB3 / G-DB5；`conftest` 接入 `database.reset()` 与 teardown 断言 | 新增 `core/database.py`；改 `core/db.py`、`tests/conftest.py`；新增 `tests/test_database_pool.py` | — |
| **T15.2** | 归还回滚 + 统计；守卫 G-DB2 | `core/database.py` | T15.1 |
| **T15.3** | 仓库改「可传连接」（**89 个**函数：43 写 + 45 只读 + 1 私有；分批 A/B/C） | `repositories/*.py` | T15.2 |
| **T15.4** | 服务层事务收口（跨仓库原子）+ 跨仓库回滚用例 | `services/job_service.py`、`services/document_store.py` 等 | T15.3 |
| **T15.5** | 静态守卫 G-DB4 + 全量对拍收口；**并记录「删锁前后 `PytestUnhandledThreadExceptionWarning` 出现次数」各一次** —— 作为「那把锁到底有没有用」的观测证据（**观测，不是判据**） | `tests/test_database_pool.py` | T15.4 |
| **T15.6** | 文档与记忆同步 | `docs/architecture/v5/*`、`ADR.md` | T15.5 |

> 顺序的红线不是「锁必须最后删」，而是**「删锁」与「连接只在 `open()` 时创建」必须在同一步完成**。
> T15.1 建池的那一刻，锁就已经没有保护对象了；把它留到后面四步，是一段**没人调用的死代码**，
> 比删掉更危险（后来者会以为并发引导仍可能发生，从而在别处重复加锁）。
>
> 另一条红线：**T15.2 必须先于 T15.3**。没有「归还即回滚」，池复用就会把未提交事务
> 传给下一个借用者——那比偶发 500 更难查。

## 6. 验收标准

### 6.1 功能与回归

| 项 | 要求 |
| --- | --- |
| 收集数 | 与 T04 基线一致（**867 + 本期新增**） |
| pass / fail 集合 | 与基线逐条一致（`--junit-xml` 对拍，不认总数） |
| 全量耗时 | ≤150 秒（T04 的验收线不得回退） |
| 前端 / 管理端 | 不受影响，但提交前各跑一次确认 |

### 6.2 五条确定性守卫

都不依赖「复现那个 READONLY」，只守我们选择依赖的不变量。

| # | 用例 | 断言 | 反向验证 |
| --- | --- | --- | --- |
| G-DB1 | `test_bootstrap_happens_only_at_open` | **分两段计数**：① `open()` 阶段建的条数 == `size`；② 清零后 10 线程各借 50 次，新建条数 == **0** | 改成懒建 → 第 ② 段计数 > 0 → 红 |
| G-DB2 | `test_checkout_rolls_back_leaked_transaction` | 借连接 INSERT 不提交就归还；下一位借用者读不到该行，且 `stats["rolled_back"] == 1` | 去掉归还回滚 → 读到脏行 → 红 |
| G-DB3 | `test_pool_is_keyed_by_db_path` | 切 `DB_PATH` 后借到的是新库的连接，旧库行不可见 | 去掉路径比对 → 读到旧库 → 红 |
| G-DB4 | `test_no_connection_held_across_await` | AST 扫描 `src/backend`：`async def` 体内出现 `with connect()` / `with transaction()` 的数量为 **0** | 在某个 async 函数里塞一个 → 红 |
| G-DB5 | `test_pool_exhaustion_times_out` | `size=1, acquire_timeout=0`，借走唯一连接后再借 → **立即**抛 `PoolExhausted` | 去掉超时 → 用例挂住（需 `pytest-timeout` 兜底） |

落点：G-DB1 / G-DB3 / G-DB5 随 T15.1（建池当步交付），G-DB2 随 T15.2，
G-DB4 随 T15.5。**每条守卫都必须与它所守的那一步同批落地**，不留「后面补」。

⚠️ G-DB1 **绝不能**写成「全局 `connect` 总数 == `size`」。两个理由：
① 故障路径 `_replace_locked()` 会补连接，总数必然可能 > `size`；
② 更致命的是它守的是**总数**而不是**时机**——「池空且未满时才建」这种实现最终创建数
仍然恰好是 `size`，改坏了也照绿。**要守的是第 ② 段「并发借用阶段一条都不建」。**

⚠️ G-DB5 的时间依赖必须是 **0**（`acquire_timeout=0`）。本环境**没装 `pytest-timeout`**，
用非零超时写「要能快速失败」会在慢机器上偶发；而反向验证「去掉超时」会**挂死**而不是转红——
挂死会把整个 CI job 拖死，比红更糟。

⚠️ **G-DB4 的优先级要提到 P0，不再是「低成本静态红线」。** 池化把它从"局部报错"变成
"全站不可用"：从前在 `async def` 里跨 `await` 持有连接，最坏是这条连接用不了；
现在则是**长时间占住一条池连接**——4 条池被 4 个协程各占一条，第 5 个请求就 `PoolExhausted`。
一次 LLM 流式可能持续数分钟，这个窗口足够把池锁死。

G-DB1 是原来那把锁的使命继承者：**锁守的是「不要并发引导」，它守的是「引导只发生一次」，后者更强也更可测。**

### 6.3 新增的行为用例

| # | 用例 | 断言 |
| --- | --- | --- |
| 1 | 跨仓库原子性 | 注入「第二个仓库写入失败」，第一个仓库的写入必须**不存在** |
| 2 | 池复用不串味 | 连续 50 次借还，每次写入的行互不可见（除非已提交） |
| 3 | 日志线程与请求线程共用池 | 两者并发写不出现 `database is locked`，也不互相阻塞超过阈值 |
| 4 | `dispose()` 撞上在途借用 | 两个线程各持一条连接并阻塞；`dispose()` 后释放：两线程不抛异常、`in_use` 归 0、后续 `acquire()` 抛 `PoolClosed`（**池不许被复活**） |
| 5 | 启动顺序 | 启动链路里 `init_schema()` 与 `LogRepository.prune()` 借连接时，`stats["size"]` 已等于 `size`（即已 `open()`），不是懒建 |

> 第 4 条防的是"换路径 / teardown 撞上后台日志线程"时的偶发 500；第 5 条防的是
> 「`prune()` 先于 `open()` 跑，于是第一次借用触发了懒建」——这种破坏**只看连接总数发现不了**，
> 必须由启动顺序来守。

### 6.4 测试装置的两处硬要求

1. `conftest._reset_runtime_state()` 里补 `database.reset()`（dispose + 清实例缓存）。
   否则每个用例都会留下一个装着 `size` 条打开连接的池，指向已删除的临时库。
2. `isolated_db` 的 teardown 里加断言：`stats["in_use"] == 0` 且 `idle == size`。
   这样**每一个用例都在守「没有连接被泄漏」**，覆盖面比一条专门用例大三个数量级。

## 7. 风险与回滚

| 风险 | 缓解 |
| --- | --- |
| 池复用导致跨请求事务串味 | 归还强制回滚（T15.2）+ G-DB2 守卫；T15.2 必须先于 T15.3 |
| 路径切换时旧连接仍被借出 | `get_database()` 比对路径并 `dispose()`；G-DB3 守卫 |
| 池过小导致请求排队 | `wait_ms_max` 进统计；默认 4（`DB_POOL_SIZE` 可调）；耗尽超时可观测（G-DB5） |
| 仓库批量改签名引入回归 | 分批改 + `conn=None` 缺省；每批跑全量对拍 |
| 改造期系统处于「半池化」 | 允许：`connect()` 委托后，未迁移的仓库照旧走自开事务，语义完全一致 |

回滚方式：逐个提交可独立回退（T15.1–T15.5 各自一个 commit）。**不采用**「一次性大提交」，否则回滚只能整体撤销。
