# v8 双后端方案：SQLite / MySQL 部署时自选

> 目标不是"能在 MySQL 上跑起来"，而是**两个后端都是正式支持的对象**：同一份代码、
> 同一套用例、同一份迁移链，部署时由 `DATABASE_URL` 决定用哪个。
>
> 本文不按"开发成本"裁剪。凡是两库语义不同的地方，都给出明确处置，而不是
> "先这样，出问题再说"。与 `DIALECT-PORTABILITY.md` 的关系：那份文档记录的是
> **第一段（接口收敛）**，本文是**第二段（真正支持两个后端）**的完整设计。

---

## 1 目标与判据

### 1.1 目标

| # | 目标 | 判据 |
|---|---|---|
| G1 | 部署时换库 = 改一个环境变量 | `DATABASE_URL` 是唯一入口，代码里没有第二处拼接 |
| G2 | 两个后端跑同一套用例 | 1006 例在 SQLite 与 MySQL 上各跑一次，均绿 |
| G3 | 行为等价（同一请求同一结果） | 契约用例在两个后端上断言相同的响应体 |
| G4 | 迁移链唯一 | 一份 Alembic 链，两个后端都能 `upgrade head` |
| G5 | 数据可搬运 | 一条 ETL 把 SQLite 的库搬进 MySQL，行数与哈希对得上 |
| G6 | 回滚可执行 | 换库失败能退回 SQLite，且不丢数据 |

### 1.2 什么算"正式支持"

三条，缺一条就只是"试过"：

1. **CI 有矩阵**：每次提交都跑 SQLite + MySQL 两遍，不是"我本地试过"；
2. **并发用例在 MySQL 上跑**：SQLite 单写者跑不出竞态，只有在 MySQL 上跑才证明
   「加锁/约束真的生效」；
3. **新代码不能只在一个后端上验证**：评审时的默认问题是"MySQL 上呢"。

---

## 2 选型边界

| 后端 | 状态 | 说明 |
|---|---|---|
| SQLite | **一等** | 单机/单用户的默认形态，零运维 |
| MySQL 8.0 | **一等** | 多用户/多连接，有独立 DB 服务 |
| PostgreSQL | **暂不纳入** | 技术上迁移代价更小（`ON CONFLICT` 与 SQLite 同语法、TEXT 索引无长度限制、部分索引原生支持）。但每纳入一个后端，CI 多一条矩阵、每个语义差异多一套验证。**两个后端已经覆盖了"零运维"和"有 DBA"两种场景**，第三个只增加维护面 |

> 如果将来纳入 PostgreSQL，本文的四个模块边界不用改——它是按"方言能力"切的，
> 不是按"具体某个库"切的。

---

## 3 总体结构

四个模块，职责正交：

```
core/database.py    ← 用哪个库、怎么连（URL / 方言名 / 引擎装配 / 连接级配置）
core/dialect.py     ← 语句怎么写（upsert / ilike / 行锁 / 死锁重试）
core/types.py       ← 列怎么声明（短文本 / 大文本 / 时间戳）
core/schema.py      ← 结构怎么演进（迁移编排 + 结构对拍分派）
```

一条判据：**任何"跟具体数据库有关"的代码，必须落在这四个文件之一**。
落在别处就是漏网，评审时打回。

`core/engine.py` 保留会话与事务（`session_scope` 仍是唯一事务落点，ADR-073 不变），
它不含任何方言知识——建引擎的动作移交给 `database.py`。

---

## 4 设计

### 4.1 配置：`DATABASE_URL` 唯一入口

```
DATABASE_URL=sqlite:////var/lib/prdwright/prdwright.db
DATABASE_URL=mysql+pymysql://prdwright:***@db:3306/prdwright?charset=utf8mb4
```

- `DB_PATH` 保留为**兼容糖**：它存在且 `DATABASE_URL` 为空时，内部转成
  `sqlite:///{path}` 并打一条启动日志（"正在使用兼容配置 DB_PATH"）。
  纯路径配置留一个版本，下个版本删。
- 启动时**校验 scheme**：只认 `sqlite` / `mysql`（含 `mariadb`），其他一律拒绝启动
  并打印"未适配的后端"。宁可起不来，也不要带着"以为连上了"的状态跑。
- `database_url()` 是唯一产出 URL 的函数；`schema.py` 的迁移地址、`engine` 的建引擎
  都从它取——「迁移改的库」与「服务读的库」由同一个来源保证，而不是各拼各的。

### 4.2 列类型：一个模块统一声明

`core/types.py` 提供四个构造函数，`models.py` 只引用、不直接写 `Text` / `String`：

```python
short_text(n)      # 有长度上限，可建索引。String(n) / MySQL VARCHAR(n)
long_text()        # 大段文本，不建索引。Text() / MySQL MEDIUMTEXT
fixed_text(n)      # 定长语义（token 哈希、时间戳串）。同上，但语义更明确
timestamp_text()   # 时间戳串，VARCHAR(32)。见 4.5
```

**为什么必须给长度**：MySQL 的 `TEXT` 不能整列建索引、不能做主键（要前缀长度）。
所有出现在 `Index` / `UniqueConstraint` / `primary_key` 里的列，必须是 `short_text(n)`。

长度上限三条硬规则，写进 `types.py` 的断言：

1. 单列 `n ≤ 768`（InnoDB 索引 3072 字节 ÷ utf8mb4 的 4 字节）；
2. 复合索引各列字节数之和 `≤ 3072`；
3. `long_text()` 不得出现在任何索引里（import 时扫 metadata，违反即抛错）。

第 3 条是防呆：这类错误在 SQLite 上**完全正常**，只有到 MySQL 才炸，而且是在
迁移那一步炸——那时已经动了生产库。

### 4.3 长度的来源：勘察，不是猜

`CLEANUP.md` 同目录的 `schema-survey.md` 给出实测值。定 n 的规则：

```
n = max(实测最大值, 语义上限) 向上取到档位（32 / 64 / 128 / 255 / 512 / 1024）
```

`users.username` 是唯一**定不下来**的：实测 6 字符，但 auth 层没有任何长度校验。
所以它的前置任务是先加校验（建议 64 字符），然后才能定 `short_text(64)`。
**在加校验之前不许写死 n**——用当前数据定长度，等于把"恰好没超"当成"不会超"。

### 4.4 大文本：容量与约束分离

| 事 | 谁负责 |
|---|---|
| 容量（能不能存下） | `long_text()`：SQLite `TEXT` / MySQL `MEDIUMTEXT` |
| 约束（业务允许多长） | `core/limits.py`：60KB，写入前拦截 |

两者不合并。理由：容量是数据库的实现细节，约束是产品口径。合并之后，业务上限会被
数据库版本绑架（MySQL 5.7 的 `TEXT` 是 64KB，8.0 也是；但换到别的库又不一样）。

⚠️ **`long_text()` 列不能有 `server_default`**。MySQL 的 `BLOB/TEXT` 列不接受字面量
默认值（8.0.13 之前完全不行，之后只能用表达式 `DEFAULT ('')`）。现有模型的
`server_default=text("''")` 会在 MySQL 建表时直接报
`BLOB, TEXT, GEOMETRY or JSON column can't have a default value`。

处置：删掉这三列的 `server_default`，靠应用层显式赋值（ORM 插入本来就总是带值）。
代价是"裸 SQL 省略该列"会违反 `NOT NULL` —— 那是好事，属于显式失败。

### 4.5 时间戳：保持字符串，改成长度受限的 VARCHAR

`created_at` 等列存 ISO 8601 文本，**字典序 = 时间序**是承重设计（ADR-077），大量
`WHERE expires_at > ?` 依赖它。改成 `DATETIME` 会同时改变落盘格式、精度、
与时区解释，代价远大于收益。

所以：**保持字符串**，但改成 `timestamp_text()`（`VARCHAR(32)`），因为
`idx_app_logs_ts`、`idx_auth_sessions_expires`、`idx_solutions_owner_content` 都在
时间列上，MySQL 下 `TEXT` 建不了索引。

两条配套：

- 业务表 26 字符（无 `Z`）、`app_logs.ts` 24 字符（带 `Z`）的双轨不变；
- 勘察发现的 16 行 `+00:00Z` 脏格式必须在 ETL 前清洗（`DATETIME` 解析会失败，
  虽然本期不改 `DATETIME`，但任何下游导出/分析都会踩）。

### 4.6 索引与唯一约束

| 项 | 设计 |
|---|---|
| 部分唯一索引 | **去掉 partial 条件**，改普通 `UNIQUE(job_id, role)`。三库的唯一索引都把 NULL 视为互不相等，语义等价（`chat_turn_repo` 的幂等本来就靠"先查 + SAVEPOINT + 撞索引回查"，不依赖 partial） |
| `DESC` 索引 | 不写。SQLite 可反向遍历，MySQL 8.0 也支持降序索引，但**写法差异不值得**——排序需求由查询的 `ORDER BY` 表达 |
| 唯一约束命名 | 显式命名（`ux_*` / `idx_*`），不依赖自动生成。MySQL 与 SQLite 的自动命名规则不同，迁移里按名字比对会失配 |
| 布尔列 | 声明 `Boolean`，MySQL 落 `TINYINT(1)`，SQLite 落 `INTEGER`。类型归一见 4.8 |

### 4.7 查询语义：三处必须显式

| 项 | 问题 | 设计 |
|---|---|---|
| `LIKE` 大小写 | SQLite 对 ASCII 不区分；MySQL 随 collation；PG 区分 | `dialect.ilike(column, pattern)`：SQLite/MySQL 走原生 `LIKE`（都是不区分），其他方言走 `lower()` 两侧。**4 处调用点全部改用它** |
| 排序 | SQLite 按字节序；MySQL `ai_ci` 按词典序（忽略大小写与重音） | 需要稳定顺序的地方显式 `ORDER BY id` 兜底；不承诺跨库的"字母序一致"（那是产品问题，不是技术问题） |
| 字符串长度 | MySQL 的 `length()` 返回字节，SQLite/PG 返回字符 | `content_length` 改**冗余列**，写入时 Python `len()` 算。顺带省掉读正文算长度的开销 |

### 4.8 结构对拍：按方言分派，不做"方言中立"

原计划换 `sqlalchemy.inspect` 已被实测否决（ADR-094）：它不返回外键的 `ondelete`，
级联规则这项判据会**消失**。

正确做法是**策略模式**，而不是"用抽象层讨好两边的共同子集"：

```
core/snapshot/__init__.py    ← snapshot(engine) 按 dialect 分派
core/snapshot/sqlite.py      ← 现有 PRAGMA 实现（保留）
core/snapshot/mysql.py       ← information_schema 实现
```

两份实现各自**取全**本方言的信息（SQLite 读 `PRAGMA foreign_key_list` 的 `on_delete`；
MySQL 读 `information_schema.REFERENTIAL_CONSTRAINTS.DELETE_RULE`），再归一成同一个
快照 dict。判据是"语义等价"，不是"字段一样"。

归一表（必须逐条有理由）：

| 差异 | 归一为 |
|---|---|
| `INTEGER` / `TINYINT(1)` / `BOOLEAN` | `BOOL`（仅限 `BOOLEAN_COLUMNS` 白名单） |
| `TEXT` / `VARCHAR(n)` | 不归一。它们是**不同的正确基线**：SQLite 的历史库是 `TEXT`，新基线是 `VARCHAR(n)`（见 4.11） |
| 默认值 `''` vs `('')` | 去括号、去引号后比较 |
| 自增 | `AUTOINCREMENT` / `AUTO_INCREMENT` 都归一为 `AUTO` |

### 4.9 并发：这是唯一"不报错"的一类

SQLite 单写者让所有"先查、再判断、然后写"的路径天然正确——写锁替它们排了队。
MySQL 没有这个保护。所以支持 MySQL 的前提是**把不变量落成数据库约束或显式锁**。

| 路径 | 不变量 | 处置 |
|---|---|---|
| `document_repo._ensure_document` | 同 solution 同 kind 至多一行 | 已由 `UNIQUE(solution_id, kind)` + upsert 保证 |
| `chat_turn_repo.write` | 同 job 同 role 至多一条 | 已由唯一索引 + SAVEPOINT 回查保证 |
| `chat_session_repo.set` | 同 solution 同 stage 至多一行 | 已由唯一索引 + upsert 保证 |
| **`generation_job_repo.supersede_active` + 建任务** | 同 owner+solution+kind 至多一个 running | **缺**。两步之间加 `dialect.lock_row()`：MySQL 用 `SELECT ... FOR UPDATE`，SQLite 退化为 no-op（单写者已保证） |
| **`quota_service` 额度检查** | 今日任务/Token 不超上限 | **缺**。同上，或把"已用量"落成一行计数表用条件更新 |

配套两条：

1. **`dialect.lock_row(session, model, *where)`**：SQLite 下是 `SELECT`（不生成
   `FOR UPDATE`，SQLAlchemy 的 SQLite 方言会忽略该子句），MySQL 下是 `SELECT ... FOR UPDATE`。
   调用方写法完全一致。
2. **死锁重试**：MySQL 的默认隔离级别 `REPEATABLE READ` 有间隙锁，并发写会返回
   1213 `Deadlock found`。这是**引擎行为，无法消除**，只能重试整个事务。
   在 `session_scope()` 外层包一层 `dialect.retry_on_deadlock(...)`：捕获 1213/1205，
   退避后重跑，最多 3 次。⚠️ 重试必须重跑**整个事务**，不能只重跑那条语句——
   事务已经回滚了。

### 4.10 迁移：一条链，按方言分派

- `render_as_batch` **按方言开关**：SQLite 需要（不支持 `ALTER COLUMN` / `DROP COLUMN`），
  MySQL 不需要（会生成无谓的中转表）。现在是硬编码 `True`。
- `compare_type` 的布尔归一按方言重算：MySQL 反射回来是 `TINYINT`，SQLite 是 `INTEGER`。
- SQLite 的 batch 模式会"建新表 + 拷数据 + 换名"，删列/改类型时**表越大越慢**，
  且需要可写的临时目录空间。这一点写进发布检查单。

### 4.11 结构的一次性变更

改成 `short_text(n)` 会让 SQLite 的建表语句从 `TEXT` 变成 `VARCHAR(n)`。这不是"漂移"，
是**新基线**。处置：

1. 一条迁移，把十几列从 `TEXT` 改成 `VARCHAR(n)`；
2. SQLite 下走 batch（重建表，数据保留），MySQL 下走原生 `ALTER`；
3. 迁移跑完，`test_schema_parity` 的期望快照随之更新；
4. **这一条与 T18.8 的删列合并成同一次发布**——反正都要重建表，分两次做等于把
   同样慢的操作做两遍。

### 4.12 数据搬运（SQLite → MySQL）

一条独立的 ETL，不进迁移链（迁移管结构，ETL 管数据，混在一起就无法单独重跑）：

```
读 SQLite（只读 mode=ro） → 按外键拓扑排序 → 分批写 MySQL → 校验
```

五条要求：

1. **顺序**：`users` → `solutions` → `documents`（指针留空）→ `document_versions`
   → 回填指针 → `chat_*` → `generation_jobs` → 配置类 → 日志类。环形外键
   （`documents.current_version_id`）必须三步走，与运行时写入逻辑一致。
2. **幂等**：可重跑。做法是目标表先清空（ETL 只用于首次搬运），或按主键 upsert。
3. **不转格式**：时间戳保持字符串原样搬，不在 ETL 里做"顺手清洗"——清洗是单独一步，
   否则出错时分不清是搬运错了还是清洗错了。
4. **校验**：每表行数 + 每表关键列的 sha256（按主键排序后拼接）。行数对但内容错
   是最难发现的一种失败。
5. **可回退**：搬运前 `VACUUM INTO` 备份 SQLite；搬运后不删源库，观察一个发布周期。

### 4.13 备份：按后端分派，接口统一

`scripts/deploy/backup.py` 对外只有一个命令，内部按 `DATABASE_URL` 的 scheme 分派：

| 后端 | 方式 | 注意 |
|---|---|---|
| SQLite | `VACUUM INTO` | 不能 `cp`（WAL 会缺最近写入）；产物要自检（能打开 + `integrity_check` + 读得到 `alembic_version`） |
| MySQL | `mysqldump --single-transaction --set-gtid-purged=OFF` | InnoDB 一致快照；`--single-transaction` 必须有，否则备份会锁表 |

两者都追加**同一条自检**：备份产物能被读、`alembic_version` 等于预期。

---

## 5 测试策略

这一节是"正式支持"与"试过"的分界线。核心结论先说：

> **不是所有用例都该在两个后端上跑。** 有些用例验证的是「某个后端的特性」，
> 它们在另一个后端上失败是**正确**的。把"1006 例双跑"当目标，会得到一堆
> 无意义的红点，然后有人开始加 `skip`——那才是"写了没法验证"的开始。

### 5.1 三类用例

| 类 | 例子 | 跑在哪 |
|---|---|---|
| **契约类**（业务行为） | 仓储契约、API 响应、权限、导出、60KB 闸门 | **两个后端都跑**，断言完全相同 |
| **后端特性类** | `test_pragmas_are_applied`（三条 PRAGMA）、`test_second_writer_waits_instead_of_failing`（WAL 写锁等待）、batch 迁移的建表过程 | **只在对应后端跑** |
| **并发类** | 「只有一个 running」「额度不超限」「死锁重试」 | 两个后端都跑，但**断言不同**：SQLite 上证明"串行/等待"，MySQL 上证明"锁生效" |

实测的当前分布（这是要改造的对象）：

| 指标 | 数量 |
|---|---|
| 直接触 SQLite 的测试文件（`PRAGMA` / `sqlite_sequence` / `sqlite3.`） | **11 个** |
| 用 `?` 占位符的文件 | **15 个** |
| `dbsupport.connect()` 的调用点 | **21 处** |
| 现有并发用例 | **1 条**（且是 SQLite 专属） |

### 5.2 用例级后端标记

不搬文件、不改目录结构，用**标记 + 一条 collection hook**：

```python
# tests/conftest.py
def pytest_collection_modifyitems(config, items):
    backend = current_backend()          # 读 DATABASE_URL 的 scheme
    if backend == "mysql":
        skip = pytest.mark.skip(reason="SQLite 特性用例")
        for item in items:
            if "sqlite_only" in item.keywords:
                item.add_marker(skip)
    else:
        skip = pytest.mark.skip(reason="MySQL 特性用例")
        for item in items:
            if "mysql_only" in item.keywords:
                item.add_marker(skip)
```

用法：`@pytest.mark.sqlite_only` / `@pytest.mark.mysql_only`。

**默认不标记 = 契约类 = 必须双跑。** 这个默认值是关键：新写的用例自动就要在两个后端上
成立，而不是"等我哪天想起来补"。已有的 11 个 SQLite 耦合文件要逐个过一遍，
按"业务行为 vs 后端特性"归类——这一步不能省，否则默认值会被误伤。

`pytest.ini` 里把未知 marker 报错（`--strict-markers`），防止拼错的标记静默失效。

### 5.3 数据隔离：三种策略，选两种

| 策略 | 速度 | 适用 | 问题 |
|---|---|---|---|
| **SQLite：模板库 + 拷贝文件**（现状） | ~1ms/例 | 保留 | 无 |
| **MySQL：全量建表一次 + 每用例清空数据** | ~10ms/例（待实测） | 推荐 | 需处理外键 |
| MySQL：每用例一个 schema | ~50ms/例 → 1006 例多 50 秒 | 备选 | DDL 太慢 |
| 事务回滚（外层事务 + session 用 SAVEPOINT） | <1ms/例 | **不采用** | 要让 `session_scope()` 绑到注入的 Connection，等于为测试改生产代码 |

**MySQL 的清空做法**：

```sql
SET FOREIGN_KEY_CHECKS = 0;
TRUNCATE TABLE chat_turns;   -- 按拓扑顺序无所谓，因为外键检查已关
...
SET FOREIGN_KEY_CHECKS = 1;
```

不能直接 `TRUNCATE` 被外键引用的表（MySQL 会拒绝），所以要先关检查。
⚠️ `FOREIGN_KEY_CHECKS` 是**会话级**变量，必须在实际执行的那条连接上设，
而不是连接池里随便一条——这一点在 `dbsupport` 里要写死。

### 5.4 并发用例：怎么造并发

项目是 async 框架但仓储是同步的，所以**用线程造并发**，不用 asyncio：

```python
barrier = threading.Barrier(2)
errors = []

def worker():
    try:
        barrier.wait(timeout=5)
        with session_scope() as session:
            repo.supersede_active(owner, solution_id=sid, kind="prd")
            repo.create(...)                     # 同一事务内
    except Exception as exc:
        errors.append(exc)

threads = [threading.Thread(target=worker) for _ in range(2)]
```

`Barrier` 保证两个线程真的同时进入临界区；没有它，第一个可能在第二个启动前就跑完了，
用例恒绿而什么都没测到。

三条用例与各自的后端断言：

| 用例 | SQLite 上断言 | MySQL 上断言 |
|---|---|---|
| 并发新建同 solution 的任务 | 两个都成功或一个等待后成功，最终 **1 条 running** | 同上，且**其中一个可能撞死锁并重试** |
| 并发扣额度 | 串行，最终不超限 | 锁生效，最终不超限 |
| 制造死锁（两事务反序更新两行） | 不适用（无行锁）→ `mysql_only` | 一个被 1213 中断，`retry_on_deadlock` 后成功，且**只成功一次** |

第三条只能 `mysql_only`：SQLite 没有行锁，构造不出死锁。

### 5.5 本地怎么跑 MySQL（这条不能只留给 CI）

只靠 CI 跑 MySQL，等于"推上去才知道对不对"——迭代一轮 10 分钟。本地必须有：

```yaml
# deploy/docker/compose.yaml 里加一个 test profile
services:
  mysql-test:
    image: mysql:8.0
    profiles: ["test"]
    environment:
      MYSQL_ROOT_PASSWORD: root
      MYSQL_DATABASE: prdwright_test
      MYSQL_USER: prdwright
      MYSQL_PASSWORD: prdwright
    ports: ["3306:3306"]
    tmpfs: /var/lib/mysql          # 测试库不落盘，快且用完即弃
```

配套一个脚本：

```bash
scripts/deploy/test-mysql.sh
  → docker compose --profile test up -d mysql-test
  → 等 mysqladmin ping
  → DATABASE_URL=mysql+pymysql://... uv run pytest -q
```

`tmpfs` 是刻意的：测试库不需要持久化，内存盘能让每例清空的成本降下来。

### 5.6 CI 矩阵与时间预算

```yaml
strategy:
  matrix:
    backend: [sqlite, mysql]
services:
  mysql:
    image: mysql:8.0
    env: { MYSQL_ROOT_PASSWORD: root, MYSQL_DATABASE: prdwright, MYSQL_USER: prdwright, ... }
    options: --health-cmd="mysqladmin ping" --health-interval=5s
```

三条硬规定：

1. **MySQL job 不许 `continue-on-error`**——"允许失败"等于没有矩阵；
2. **两个 job 跑同一条命令**（`uv run pytest -q`），差别只在 `DATABASE_URL`；
   跑不同的命令等于两套口径；
3. **时间预算写进验收**：SQLite 全量 ~85 秒，MySQL 预期 2~4 分钟
   （清空 + 无并行化优势）。超过 6 分钟要查是不是隔离策略退化了。

### 5.7 支持范围

| 后端 | 版本 | 说明 |
|---|---|---|
| SQLite | 3.35+ | 需要 `RETURNING`（SQLAlchemy 用于自增回填）与 `DROP COLUMN` |
| MySQL | **8.0 / 8.4** | 8.0.13+ 才支持表达式默认值（我们不用，见 4.4）；8.0 的 `VALUES()` 语法在 8.0.20 起标记弃用但仍可用，换别名语法时只改 `core/dialect.py` |

CI 矩阵默认只跑 **8.0**；8.4 每月跑一次（`schedule` 触发），用来提前发现"下一个 LTS
会坏在哪"。不把 5.7 纳入：它没有 `utf8mb4_0900_as_cs`，也没有表达式默认值，
支持它等于给类型策略开一个例外分支。

### 5.8 怎么证明"两个后端行为真的一样"

只靠"两个 job 都绿"是不够的——它们各自断言了同样的字面量，但如果某个断言写得
太松（比如只判 `status_code == 200`），两边都绿而行为不同。

补两条：

1. **结构等价**（离线对拍）：把两个后端的 `schema_snapshot()` 各导一份，归一后比较。
   这是唯一能发现"MySQL 上少了一个索引"的检查，因为各自的 `test_schema_parity`
   只和自己比。
2. **关键接口的响应快照**：对 10 个核心接口（登录、方案 CRUD、文档追加、导出、
   admin 统计）把响应体存成 golden 文件，两个后端比对同一份 golden。
   选这 10 个的理由是它们覆盖了所有"跨表、跨排序、跨分页"的查询。

---

## 6 契约

| # | 契约 | 破了会怎样 |
|---|---|---|
| **C-M1** | `DATABASE_URL` 是唯一入口；代码里不许第二处拼 URL | 换库时改一处、漏三处 |
| **C-M2** | 索引/主键/唯一约束里的列必须是 `short_text(n)`，且 `n ≤ 768` | MySQL 建表失败；或索引超 3072 字节被拒 |
| **C-M3** | `long_text()` 不出现在索引里；且不带 `server_default` | MySQL 建表报 `can't have a default value` |
| **C-M4** | 任何"先查后写"的路径必须有唯一约束或 `lock_row()` 兜底 | 并发下静默出现两条 running / 额度被击穿 |
| **C-M5** | 代码里不出现方言函数（`func.datetime` / `func.length` / 字符串日期算术） | 换库后语法错误或语义漂移 |
| **C-M6** | 每个 PR 都要过 SQLite + MySQL 两个 job | 退回"只有一个后端是真的" |
| **C-M7** | 新列一律走 `core/types.py`，不直接写 `Text` / `String` | 类型策略散开，C-M2 形同虚设 |

C-M2 / C-M3 / C-M7 都靠 `core/types.py` 在 import 时扫 `SQLModel.metadata` 自检：
发现索引里出现长文本、或 `long_text` 带默认值，直接抛错。**让错误发生在 `import` 时，
而不是迁移时。**

---

## 7 实施顺序

每一阶段都能独立验证，不许"攒到最后一起测"。

| 阶段 | 内容 | 验证 |
|---|---|---|
| **P1** | `core/database.py`（URL/方言/引擎装配）、`DATABASE_URL` 配置、`DB_PATH` 兼容糖 | 1006 例在 SQLite 上全绿，行为零变化 |
| **P2** | `core/types.py` + 一次性改列类型（4.11）+ 删 `long_text` 的 `server_default` | 结构对拍绿；`alembic upgrade head` 在 SQLite 与 MySQL 上空库都能跑通 |
| **P3** | `core/snapshot/mysql.py`；`render_as_batch` 按方言开关 | 两个后端各跑一次 `test_schema_parity` |
| **P4** | `dialect.ilike` / `lock_row` / `retry_on_deadlock`；4 处 `LIKE` 改造；两条并发路径加锁 | SQLite 全绿 + MySQL 上新增 3 条并发用例绿 |
| **P5** | 测试基建：`dbsupport` → SQLAlchemy `Connection`；`sqlite_only` / `mysql_only` 标记 + collection hook；MySQL 用「全量建表一次 + 每例清空数据」；`scripts/deploy/test-mysql.sh` | 契约类用例两个后端都绿；特性类各自绿；本地一条命令能跑 MySQL |
| **P6** | CI 矩阵（两个 job 同一条命令）+ `--strict-markers` + 结构等价对拍 + 10 个接口的 golden 响应 | 两个 job 常绿；MySQL 耗时 ≤ 4 分钟 |
| **P7** | ETL + 校验 + 备份分派 | 一次真实搬运：行数与哈希对得上；备份可恢复 |

**P1 单独做且先做**：它是"开关"，装好之后所有后续验证都可以在两个后端之间切换，
否则每个阶段都要手工改代码才能试 MySQL。

---

## 8 风险与取舍

### 8.1 接受的风险

| 风险 | 为什么接受 | 缓解 |
|---|---|---|
| MySQL 的死锁无法消除 | 间隙锁是引擎行为 | 重试包装 + 并发用例 |
| 两个后端的排序结果可能不同 | 词典序是产品问题，不是技术问题 | 需要稳定顺序的地方用 `id` 兜底；不承诺字母序一致 |
| 测试基建改造会碰到大量用例 | 这是"正式支持"的必要成本 | 一次做完，不留两种写法 |
| SQLite 的 batch 迁移慢（重建表） | 单机部署，数据量小 | 与 T18.8 合并成一次发布 |
| MySQL 多一个需要运维的组件 | 用户明确要这个能力 | 容器化（T18.5）已覆盖 |

### 8.2 被否决的方案

| 方案 | 理由 |
|---|---|
| 只让 MySQL 能跑，SQLite 弃用 | SQLite 是零运维形态，砍掉等于放弃"个人自部署"场景 |
| 用 ORM 抽象抹平一切，不做显式分派 | 实测过：`inspect()` 会丢 `ondelete`（ADR-094）。抽象层给的是共同子集，而我们要的是"两边都完整" |
| 保留 `TEXT` + MySQL 前缀索引 | 前缀唯一 ≠ 全值唯一，用户名/哈希这种列语义会变 |
| 把时间戳改成 `DATETIME` | 改动波及 `clock.py` 与所有比较点，收益（更小的索引）不足以抵消 |
| 运行时切库 | 需要回答"在途任务往哪写""旧池怎么处置""迁移何时跑"，且没有使用场景 |

---

## 9 验收

| # | 判据 | 怎么验 |
|---|---|---|
| 1 | `DATABASE_URL` 切换后端，代码零改动 | 同一个镜像/同一份代码，两个 URL 各起一次 |
| 2 | 契约类用例在两个后端都绿，且**断言完全相同** | CI 两个 job 跑同一条命令 |
| 3 | 特性类用例只在对应后端跑（skip 而非 fail） | 故意给一条用例加 `sqlite_only`，在 MySQL job 里看到 skipped |
| 4 | 空库 `upgrade head` 在两个后端都成功 | 各建一个空库跑一次 |
| 5 | 结构对拍在两个后端都能发现人为改坏 | 手工删一个外键的 `ON DELETE`，断言拒绝启动 |
| 6 | 两个后端的结构快照**归一后等价** | 离线对拍（§5.8），能发现"MySQL 上少了一个索引" |
| 7 | 3 条并发用例在 MySQL 上绿 | 见 §5.4 |
| 8 | ETL 后行数与哈希对得上 | 见 §4.12 |
| 9 | 两个后端都能备份并恢复 | 见 §4.13 |
| 10 | 本地一条命令能跑 MySQL 全量 | `scripts/deploy/test-mysql.sh`，不需要先理解 compose |
| 11 | 代码里 `grep` 不到方言函数 | `grep -rn "func\.\(datetime\|length\|date\)" src/` 为空 |
| 12 | MySQL 全量耗时 ≤ 4 分钟 | CI 记录；超 6 分钟说明隔离策略退化（比如退回了"每例一个 schema"） |
