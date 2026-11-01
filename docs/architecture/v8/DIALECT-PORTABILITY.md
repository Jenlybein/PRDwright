# v8 方言可移植性方案

> 主题：让「这个项目跑在 SQLite 上」这件事只出现在一个文件里。
> 本文只写**设计与契约**，不含实施记录；实测数据见 `schema-survey.md`（同一目录）。
> 采集方式：`python backend/scripts/schema_survey.py --out docs/architecture/v8/schema-survey.md`

---

## 1 目标

三件事，按优先级：

1. **收敛**：`api/` / `services/` / `repositories/` 三层不再出现任何数据库品牌的痕迹。换库时只改一个模块，不改业务代码。
2. **前置声明**：模型层把"只对某个库成立"的约束写成显式声明（`with_variant`、collation），而不是散在实现里、靠人记。
3. **保住主路径**：收敛过程对当前 SQLite 部署**零行为变化**。任何会让 975 例测试变红、或让现有库结构对拍报漂移的改动，都不进本期。

### 为什么要做，什么时候做

做这件事的理由**不是**"将来要换 MySQL"，而是它顺带解决两个既存问题：

- `upsert` 的方言 import 散在 3 个仓库里，每个新仓库都要自己记得"用 `sqlite_insert` 而不是 `insert`"，漏了会在运行时才炸；
- `schema_snapshot.py` 那 300 行 `PRAGMA` 实现让"结构对拍"本身依赖 SQLite，而它恰恰是**判断迁移是否安全**的工具——工具和被检查对象用了同一个数据库的私有接口，等于没有独立验证。

**本期不选目标库。** 方案的可移植性只到"接口干净"为止，不做"三库都能跑"的承诺——那需要 CI 矩阵，见第 5 节取舍。

---

## 2 范围

### 本期做

| # | 内容 | 触碰文件 | 行为变化 |
|---|---|---|---|
| D1 | 三处 upsert 收敛到 `core/dialect.py` | 新建 1 个 + 改 3 个仓库 | 无 |
| D2 | 库地址、空库判定去 SQLite 特化 | `core/schema.py` | 无 |
| D3 | 大文本列声明 `MEDIUMTEXT` 变体 | `core/models.py` | 无（SQLite 侧仍是 `TEXT`） |
| D4 | 用户名唯一性声明大小写敏感 collation | `core/models.py` | 无（SQLite 侧本来就是敏感） |
| D5 | 按天统计的时区换算搬进应用层 | `repositories/admin_repo.py` | **有**（统计学口径不变，但实现换了，必须对拍） |
| D6 | ~~结构对拍改用 inspector~~ **实测否决**（见 3.5） | — | 不变 |

D1~D4 是纯粹的"搬运"，可以按批做、每批跑全量；D5、D6 各自单独一轮，理由是它们引入了新的等价性证明缺口。

### 本期不做

- **不改任何列的类型**（`Text` → `String(n)`）。理由见 3.3：这会改变 SQLite 的建表语句，让结构对拍从"全绿"变成"全红"，属于换库那次的活。
- **不引入目标库**、不写 docker-compose 的 db 服务、不搭 CI 矩阵。
- **不做数据迁移 ETL**。`upsert` 收敛与 ETL 没有依赖关系，后者只在真换库时才有对象。
- **不动 `engine._on_connect` 的三条 PRAGMA**。它无法被收敛——连接建立时要执行什么 SQL，只能按方言分支。它属于"永久适配点"，归在 3.2 的边界里说明，不算遗漏。
- **不改时间列的存储格式**。业务表 26 字符无 `Z`、日志表 24 字符带 `Z`，这个双轨是承重设计（ADR-077），本期一个字都不动。
- **不处理语义差异**（`LIKE` 大小写、`ORDER BY` 排序、事务隔离）。它们不需要"收敛"，需要的是"到了目标库上重新审一遍"。本期只把它们列进 3.6，让换库那次的清单是完整的——**清单不完整比清单难做更危险**。

---

## 3 设计

### 3.1 三层边界

同一个"方言问题"，实际落在三个互不相通的层次上。方案对每一层的态度不同：

| 层 | 例子 | 能不能收敛 | 归谁 |
|---|---|---|---|
| **查询表达式** | `select` / `where` / `join` / `session.exec` | 已经收敛（守卫 C1~C3 禁止 SQL 字符串） | 无需动作 |
| **语句构造** | upsert 的 `ON CONFLICT` / `ON DUPLICATE KEY` | 能收敛到 1 个函数 | D1 |
| **DDL 声明** | 列长度、`MEDIUMTEXT`、collation、部分索引 | 能"提前声明"，但要改就得连基线一起改 | D3 / D4 / 3.3 |
| **实例与连接** | `PRAGMA journal_mode` / `SET time_zone` | **不能**收敛 | 永久适配点 |
| **查询语义** | `LIKE` 的大小写敏感性、`ORDER BY` 的排序规则 | 不能收敛，只能显式化 | 见 3.6 |
| **事务语义** | 隔离级别、多写者并发 | **不能**收敛，只能显式加锁 | 见 3.6 |
| **数据库能力** | `TEXT` 不能整列建索引、`TEXT` 64KB 上限 | **不能**消除，只能换数据模型 | 换库那次 |

一条判断原则：**能在"我们写的代码"里解决的，收敛；只能靠"对方数据库支持"的，声明或改模型。**

### 3.2 收敛点：`core/dialect.py`

唯一的品牌感知模块。对外只暴露两件事：

```
upsert_values(session, model, values, *, conflict_cols, update_cols=None)
upsert_select(session, select_stmt, model, *, conflict_cols, update_cols=None)
```

- `update_cols=None` 表示"冲突就什么都不做"（DO NOTHING）；给了列表就是 DO UPDATE。
- 两个入口的差别只在于"待插入的行从哪来"：`values` 用于常量行，`upsert_select` 用于 `INSERT ... SELECT`（`document_repo._ensure_document` 的归属校验就写在 SELECT 的 WHERE 里，这条结构必须保留）。

各方言的分支：

| 方言 | DO UPDATE | DO NOTHING |
|---|---|---|
| SQLite / PostgreSQL | `ON CONFLICT (cols) DO UPDATE SET … = excluded.…` | `ON CONFLICT (cols) DO NOTHING` |
| MySQL / MariaDB | `ON DUPLICATE KEY UPDATE col = VALUES(col)` | `ON DUPLICATE KEY UPDATE <pk> = <pk>`（no-op） |

**MySQL 的 DO NOTHING 刻意不用 `INSERT IGNORE`**，尽管它更短：`IGNORE` 会把"列超长、违反 NOT NULL"这类错误一起降级成警告，等于用一个静默的写失败换一行代码。no-op 更新只吃掉唯一冲突，其它错误照常抛。

### 3.3 模型层：提前声明，而不是提前改

**能现在做的两件（SQLite 侧零变化）：**

1. 大文本列挂 MySQL 变体，SQLite 侧仍是 `TEXT`：

   ```
   Column(Text().with_variant(mysql.MEDIUMTEXT(), "mysql"), ...)
   ```

   只挂 4 列，依据是勘察报告的字节数：

   | 列 | 实测 max | 说明 |
   |---|---:|---|
   | `generation_jobs.draft_content` | 36,919 B | 草稿正文，已达 64KB 的 56% |
   | `document_versions.content` | 24,534 B | 一整篇 PRD，11.5k 汉字 |
   | `generation_jobs.completed_sections` | 15,777 B | 刚好在 16KB 线下，留一档余量 |
   | `solutions.data` | 0 B（本地库全空） | 整份会话快照，语义上必然更大，按最坏情况声明 |

   前两行是这条决策的全部依据：**中文一字 3 字节，64KB 只够 2 万字**。一份稍长的 PRD 就能撑满，而 `TEXT` 溢出在 MySQL 严格模式下是**写入失败**，不是截断。

2. 用户名唯一性显式声明大小写敏感：

   ```
   Column(Text, unique=True, mysql_collate="utf8mb4_0900_as_cs")
   ```

   全项目只有这一列需要：`login_failures.username` 的设计前提是"`users.username` 大小写敏感"，两者必须一致。SQLite 和 PostgreSQL 默认就是敏感的，所以这条声明只在 MySQL 下起作用——**它把"默认值恰好正确"变成了"显式正确"**。

**不能现在做的：列长度。**

`VARCHAR(n)` 在 SQLite 下会改变建表语句（`TEXT` → `VARCHAR(n)`），结构对拍会全表报漂移。行为虽然等价（两者的类型亲和性都是 TEXT，且 SQLite 不校验 n），但**基线一旦动，就失去了"结构没变"这个证据**。

所以长度档位只作为实施清单存在，等换库那次连同基线一起改。勘察给出的候选档位里，唯一定不下来的就是 `users.username`：

> 实测最大值 6 字符，但 auth 层**没有任何长度校验**（`max_length` 全项目搜不到）。按 6 定 `VARCHAR(32)` 是拿"当前数据恰好短"当保证。这一列的前置条件是**先补应用层校验**，然后才能定 n。

### 3.4 算法换位：把时区换算搬出 SQL

现状：`admin_repo.py:286` 用 `func.substr(func.datetime(created_at, '+480 minutes'), 1, 10)` 分组。这是全项目**唯一**一处既依赖方言函数、又无法用方言分支优雅解决的查询——因为 `datetime()` / `DATE_ADD` / `::timestamp + interval` 三种写法的参数形态都不一样。

换法不是"写三个分支"，而是**换掉换算发生的位置**：

- 现在：SQL 给每一行加时区偏移，然后按结果分组。
- 改后：Python 先算好每个本地日对应的 UTC 区间边界，SQL 只做 `created_at >= ? AND < ?` 的范围判断，按区间 `case` 分桶。

三条好处，顺序即重要性：

1. **零方言函数**——只用比较运算，任何库都支持；
2. **任意分钟偏移都精确**——`tz_offset_minutes` 是参数，可能不是整小时（+5:30 这种）。按小时聚合再在 Python 里合并的方案在这里是错的：整小时桶在半小时偏移下会跨本地日；
3. **顺带修一个性能问题**——`GROUP BY datetime(...)` 是对函数分组，走不了索引排序；改后过滤条件就是纯列比较。

代价是这段 SQL 会变长（每个本地日一个 `case` 分支，30 天就是 30 个）。这是显式的、可读的复杂度，换掉了一个隐式的、只在某个库上正确的函数。

验证方式：同一份 SQLite 库，新旧两版各跑一遍，逐日逐指标比对。**必须逐指标**（`task_count` / 各状态计数 / 两种 token），因为这四个数是四个独立的 `case` 聚合，错一个不会报错，只会让看板上的数字悄悄不对。

### 3.5 结构对拍：**实测否决**，保留 `PRAGMA` 实现

原计划把 `schema_snapshot.py` 那 300 行 `PRAGMA` 实现换成 `sqlalchemy.inspect`，
理由是"去掉循环论证"——现在用 SQLite 的私有接口检查 SQLite 的库结构，
工具与被检查对象共用同一套实现。方言中立只是附带好处。

**实测之后否决。** 探测脚本对现有库跑 `inspect(engine)`，拿到了三件事：

| 项 | `PRAGMA` | `inspect()` | 差在哪 |
|---|---|---|---|
| 外键的 `ON DELETE` | `CASCADE` / `SET NULL` 都能读到 | `options` 返回 `{}` | **级联规则完全丢失** |
| 部分索引的 `WHERE` | 从 `sqlite_master.sql` 取 | `dialect_options['sqlite_where']` 有值 | 能拿到，但那本身就是方言选项 |
| 列默认值 | `dflt_value` = `"'[]'"` / `'0'` | `default` = `"'[]'"` / `'0'` | 形态接近，可归一 |

第一行是致命项。级联规则是 `assert_schema_matches_models` **最重要**的一条判据——
`documents.current_version_id` 是 `SET NULL`、`document_versions.document_id` 是 `CASCADE`，
这两个写错任何一个，删除方案时会留下孤儿正文或误删正文，而两种错都不会当场报错。

换成 inspector 之后，两边（期望快照与实际快照）都不含 `ondelete`，
`diff_snapshots` 会**判它们相等**——判据不是变宽松了，是**这一项消失了**。
这类"改着改着把判据改没了"的退化，比原计划的循环论证更危险。

而第二行说明"方言中立"这个好处也不成立：部分索引的条件只能从 `dialect_options`
里拿，那本身就是 SQLite 专属的键。所谓中立，只是把 `PRAGMA` 换成了 `dialect_options`，
再补一个"外键要从 `PRAGMA` 另行读取"的分支——特化没有减少，只是更分散了。

**结论**：保留 `schema_snapshot.py` 的 `PRAGMA` 实现。它是**测试与启动校验**的工具，
不在业务路径上，方言特化在这里的代价是"少一层独立验证"，而不是"运行时出错"。
与之相比，丢掉外键级联检测的代价大得多。

> 这条是本次实施里唯一被实测推翻的方案。它也是一个提醒：**"换成官方抽象"不等于
> "更好"**——先量一下抽象层到底给了什么、又拿走了什么，再决定换不换。

### 3.6 不收敛清单（完整）

前五节讲的是"怎么收敛"。这一节相反：把**收敛不掉**的东西一次列全，避免方案读起来像"做完就与库无关了"。

按"能不能消除"分三类，只有第一类是真的没法消除。

| 类 | 项 | 具体位置 | 处置 |
|---|---|---|---|
| **永久适配** | 连接级配置 | `engine._on_connect` 的三条 `PRAGMA` | 按 `dialect.name` 分支。连接建立时执行什么 SQL，只能由 SQL 决定，任何 ORM 都管不了 |
| **永久适配** | 迁移的 batch 模式 | `migrations/env.py` 的 `render_as_batch=True` | SQLite 专用的"建新表+拷数据"。换库前必须关掉，否则每次改列都会生成无谓的中转表 |
| **永久适配** | 连接池参数 | `max_overflow=0`、`pool_size` | 这条不变量的**理由**是 WAL `-shm` 竞态，换库后理由消失。此时要补 `pool_pre_ping=True`（防 `MySQL server has gone away`）与 `pool_recycle`（短于服务的 `wait_timeout`） |
| **永久适配** | 类型归一表 | `BOOLEAN_COLUMNS` | `BOOLEAN`/`INTEGER` 的等价只对 SQLite 成立。换库后要重算：MySQL 是 `TINYINT(1)`，PG 是真 `BOOLEAN`。C-D2 登记的是"哪些列是布尔"，映射规则得跟着库走 |
| **换模型** | 列长度 | 全部 `Column(Text)` | 换库那次改 `String(n)`，档位见勘察报告（3.3 已说明为何本期不做） |
| **换模型** | 部分唯一索引 | `models.py` 的 `ux_chat_turns_job_role` 带 `sqlite_where` | 功能上可去掉：三库的唯一索引都把 NULL 视为互不相等，普通 `UNIQUE(job_id, role)` 语义等价（`chat_turn_repo` 的幂等本来就靠"先查 + SAVEPOINT + 撞索引回查"，不依赖 partial）。但它会改变索引定义 → 结构快照变红，所以归到换库那次与列长度一起改 |
| **换模型** | `length()` 的返回语义 | `document_repo.py:181` 的 `content_length` | MySQL 返回字节、SQLite/PG 返回字符。换库那次改冗余列（写入时 Python `len()`），或按方言选函数 |
| **语义差异** | **事务隔离与并发** | 见下方专述 | **方案原稿漏项。** 换库前必须逐条审"先查后写" |
| **语义差异** | **`LIKE` 的大小写** | `generation_job_repo.py:483`、`:514`（用户名搜索）、`log_repo.py:196`、`:211`（日志搜索） | **方案原稿漏项。** 三库行为不同，见下方专述 |
| **语义差异** | **`ORDER BY` 的排序** | 方案列表按标题、日志按 `logger` | 排序结果受 collation 影响（`ci` 下大小写混排）。属于"看起来没坏、但顺序变了" |
| **语义差异** | **布尔默认值字面量** | `server_default=text("1")` / `text("0")` | MySQL 可以；PG 的 `BOOLEAN` 列遇到 `DEFAULT 1` 会报类型错误，要改成 `true`/`false` 的方言表达式 |
| 无需处理 | `sqlite_autoincrement=True` | 所有自增表 | SQLAlchemy 对非 SQLite 方言静默忽略该参数，语义由目标库的 `AUTO_INCREMENT`/`SERIAL` 承担。**不是遗漏** |

#### 事务隔离：唯一"不报错、只在并发时偶发"的一类

SQLite 是**单写者**：同一时刻只有一个事务能写，序列化由数据库保证。所以这个项目里所有"先查、再判断、然后写"的路径，**从来没被并发检验过**——它们之所以正确，是因为写锁替它们排了队。

换成多写者之后，同样的代码会暴露。全项目 `with_for_update`、`isolation_level` 各 0 处，说明没有任何显式加锁。要审的路径：

| 路径 | 依赖的不变量 | 换库后的风险 |
|---|---|---|
| `generation_job_repo.supersede_active` + 紧接着的建任务 | 「同 owner + solution + kind 至多一个 running」 | 两个并发请求可能各自把旧任务置 `failed`、然后各插一条 `running`。不变量由应用层的两步操作维持，不是数据库约束 |
| `quota_service` 的额度检查 | 「今日任务数/`token` 数不超过上限」 | 先算已用量再放行，两个并发请求会都看到"还没超" |
| `chat_turn_repo.write` | 「同 job 同 role 至多一条」 | **已防御**：SAVEPOINT + 撞唯一索引回查，逻辑本身与库无关 |
| `document_repo._ensure_document` | 「同 solution 同 kind 至多一行」 | **已防御**：走 upsert，唯一约束兜底 |

所以真正裸露的是前两条。它们在单 worker 下也很难触发（并发度低），但"很难触发"和"不会触发"是两件事——MySQL 的默认隔离级别是 `REPEATABLE READ`，还会引入间隙锁与死锁（1213），后者需要重试包装。

#### `LIKE` 的大小写

| 库 | `LIKE '%admin%'` 能否匹配 `Admin` |
|---|---|
| SQLite | 能（ASCII 默认不区分；`case_sensitive_like` 默认为关） |
| MySQL | 能（取决于 collation，`ai_ci` 不区分） |
| PostgreSQL | **不能**（`LIKE` 区分大小写，需要 `ILIKE`） |

四处 `LIKE` 里有两处是**用户可见的搜索**（管理端按用户名搜任务、按关键字搜日志）。换到 PG 之后，"搜 admin 搜不到 Admin"会被当成 bug 报上来，而代码没改过一个字。

统一的做法是显式化：搜索一律 `lower(col).like(lower(pattern))`，或者按方言映射成 `ILIKE`。前者牺牲索引（两侧都要函数），后者要在 `core/dialect.py` 加一个 `ilike()`。**这属于换库那次的决定**，本期不做，但必须写进清单——因为它和 `TEXT` 索引是同一类东西：不会报错，只会让结果悄悄变。

---

## 4 契约

以下四条是收敛之后必须守住的，否则"只有一处知道方言"会重新散开。

| 契约 | 内容 | 破了会怎样 |
|---|---|---|
| **C-D1** | `upsert` 的冲突目标只在"该表只有一个唯一约束"时等价。新增表若有两个以上唯一约束，MySQL 下 `ON DUPLICATE KEY` 会命中非目标约束 | 静默写错行。三个现有调用点都已核对：`documents` 仅 `(solution_id, kind)`、`chat_sessions` 仅 `ux_chat_sessions_thread`、`system_config` 仅主键 |
| **C-D2** | 布尔列一律登记 `BOOLEAN_COLUMNS`；`is_`/`has_` 命名不作为判据 | 类型归一漏一项 → 幻影迁移。`test_schema_parity` 已在守这条 |
| **C-D3** | 时间列只允许两种形态：业务表 26 字符无 `Z`、`app_logs.ts` 24 字符带 `Z`。**`+00:00Z` 零容忍** | 勘察已捞出 16 行脏格式；转 `DATETIME` 时解析失败 |
| **C-D4** | 结构对拍必须在两种库上都能跑（SQLite 现状 + 将来的目标库），不接受"只在 SQLite 上成立"的实现 | D6 白做 |
| **C-D5** | 搜索的大小写行为必须显式（`lower()` 或方言 `ILIKE`），不依赖库默认 | 换库后"搜 admin 搜不到 Admin"，被当成业务 bug 报上来 |
| **C-D6** | 业务不变量（「至多一个 running」「额度不超过上限」）不能只靠"单写者"保证。换库前每条都必须落成唯一约束、条件更新或显式锁 | 并发下静默出现两条 `running`、额度被击穿。这类错不报错，只在对账时才发现 |

C-D6 是 3.6 那两条裸露路径的收口。它不要求本期就加锁（单 worker 下没问题），要求的是**换库时不能靠"我们一直这么做也没出事"作为依据**——那句依据在 SQLite 上成立，恰恰是因为写锁。

C-D3 值得单独说一句：那 16 行 `2026-10-21T16:14:03.700+00:00Z` 正是 `utc_ms_text` 的 docstring 里明确要防的形态（`isoformat()` 对带时区的值输出 `+00:00`，再拼 `Z` 就成了两头不靠）。函数已经修了，但**旧数据还在库里**。这就是"勘察"比"看代码"多出来的东西——代码读起来是对的。

---

## 5 取舍

### 为什么不追求"零方言"

因为右边那一列不属于写法问题：

- `TEXT` 不能整列建索引 —— MySQL 的索引键有长度上限，`TEXT` 必须给前缀长度，主键完全不行；
- `TEXT` 64KB —— 存储规格，不是语法；
- collation —— 实例与列的排序规则，影响的是"什么叫相等"。

这些只能靠换数据模型（改 `VARCHAR(n)`、改 `MEDIUMTEXT`）绕过去，而换模型的代价会落在**当前唯一真实运行的 SQLite 路径**上。为不存在的第二个部署形态付日常维护成本，是负收益。

### 为什么分两段

第一段（D1~D6）的目标是"接口干净"，可以用现有 975 例自证；第二段（改列类型、ETL）的目标是"在某目标库上真能跑"，**没有目标库就无法验证**。把两者混在一起做，第二段的每个决策都只能靠猜，而且猜错的表现为"上线那天才炸"。

### 否决的替代方案

| 方案 | 否决理由 |
|---|---|
| 全库把 `Text` 换成 `String(n)`，一次性通用 | 与现有 SQLite 基线直接冲突，等于立刻做一次全表迁移；且 n 依赖生产数据，本地库量不出来 |
| MySQL 的 DO NOTHING 用 `INSERT IGNORE` | 会把列超长、违反 NOT NULL 一并降级成警告，用静默失败换一行代码 |
| 在各仓库里写 `if dialect == ...` | 就是收敛前的状态，只是把 `sqlite_insert` 换成更长的分支 |
| 用 `MERGE` 语句做通用 upsert | MySQL 不支持 `MERGE`；SQLAlchemy 的 `Merge` 覆盖不全 |
| 时区聚合按小时分组后在 Python 合并 | 半小时偏移（+5:30）下整小时桶会跨本地日，统计学口径会错 |

---

## 6 实施顺序

只排依赖，不排期。

```
D1（upsert 收敛）──┬─→ D2（地址/空库判定）
                   └─→ D5（时区聚合换位）──→ 需要在同一轮做等价性对拍
D3（MEDIUMTEXT 变体）
D4（collation 声明）        ← 与 D1 无依赖，可并行；都是零行为变化
D6（snapshot 换 inspector） ← **已否决**（3.5）；原计划独立一轮，实测后取消
```

1. **D1 + D2**：一批。跑全量 975 例。写一个专门的用例证明"helper 在 SQLite 上产出的 SQL 与原 `sqlite_insert` 一致"——这条比"测试全绿"更有说服力，因为全绿也可能是调用点被改少了。
2. **D3 + D4**：一批。验收标准是**结构对拍仍然全绿**（SQLite 侧一个字节都没变）。如果红了，说明 `with_variant` 的声明位置写错了。
3. **D5**：单独。验收标准是新旧两版逐日逐指标相等，而不只是"页面上的图看起来一样"。
4. **D6**：不做。`PRAGMA` 实现保留，理由见 3.5（外键级联规则会丢）。

换库那次（不在本期）：按勘察报告改列长度 → 清洗 16 行脏格式 → 补 `username` 长度校验 → ETL + 行数与哈希对账 → 目标库上确认结构对拍可用（届时 `schema_snapshot.py` 需要按目标库补一套读取实现，而不是靠"方言中立"自动获得）。

---

## 7 待决

- **`users.username` 的长度上限取多少**。数据（max 6）不能作为依据，需要产品层面定：是"用户名最多 32 字"这种规则，还是保持无上限、把唯一索引改到哈希列上。这个决定必须在换库之前做掉，否则 `VARCHAR(n)` 的 n 定不下来。
- **`solutions.data` 的真实规模未知**。本地库里 4 行全是空串，无法判断它属于 `MEDIUMTEXT` 够用还是需要 `LONGTEXT`。生产库跑一次勘察即可回答。
- **3.6 的三类清单要不要各自成为换库那次的前置条件**。建议是：列长度与部分索引算"必做"，`LIKE` 与排序算"必审"，事务隔离算"必加锁"。三者的验证方式不同（前者靠结构对拍，中者靠搜索用例，后者靠并发用例），不能合成一张清单。
- **`LIKE` 的显式化时机**。改成 `lower(col).like(...)` 会让现有前缀索引失效（`AppLog.logger` 的前缀匹配会退化成全表扫）。如果决定现在做，需要同时评估 `log_repo` 的查询计划；如果不做，就等换库那次连索引一起设计。
- **本文是否拆成两条 ADR**（建议：ADR-088「方言收敛点 = `core/dialect.py`」、ADR-089「可移植性分两段，本期只做接口收敛」）。按 v7 的编号约定接在 ADR-087 之后。3.6 的不收敛清单可以并进 ADR-089 的"范围之外"，也可以单独成一条 ADR-090。
