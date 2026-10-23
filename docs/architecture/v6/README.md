# v6 · 数据层重构（SQLModel）

> 阶段主题：把数据访问层从「手写 `sqlite3` + 自建连接池」换成「SQLModel + SQLAlchemy Engine/Session」，
> 迁移链从 `PRAGMA user_version` 的 20 步手写清单换成 Alembic。
> 状态：**已完成（T16.0 ~ T16.13）**；v6.1 追加一项：删掉旧版本迁移链（ADR-083）。

---

## 完成情况（最终数字）

| 判据 | 目标 | 实测 |
| --- | --- | --- |
| 全量用例 | ≥ 877 | **975 例全绿**（删掉 26 条只服务旧迁移链的用例，见 ADR-083） |
| 全量耗时 | ≤ 90 秒 | 86 ~ 88 秒（同机多次测量，波动来自并行 worker 调度） |
| `repositories/` 的 `import sqlite3` | 0 | **0** |
| `repositories/` 的 `.commit()` / `.rollback()` | 0 | **0 / 0** |
| `repositories/` 的 SQL 字面量 | 0 | **0**（守卫 C2 静态扫描） |
| 基线库 vs 模型库快照 | 完全一致 | 一致（C6） |
| `commit_generated` 原子性 | 中途失败零残留 | 有专门用例证明 |

11 个仓库文件全部改成「绑定 `Session` 的实例方法」：`log` / `chat_session` / `chat_turn` /
`quota` / `account` / `solution` / `document` / `generation_job` / `auth` / `runtime_config` / `admin`。

### v6.1：旧版本迁移链已删除（ADR-083）

T16.12 收尾时，`init_schema()` 与 `SCHEMA_SQL` 曾从 `core/db.py` 搬到 `core/migrations.py`
保留下来，理由是「它们能让『v18 的库升上来结构对不对』保有端到端证据」。

随后确认：**没有任何库需要这条路径**——项目还没有已交付的自托管实例，开发库已经是
`user_version=20` + `alembic_version=0001`。而这条链的存在让「版本」有两个真相，
每个读 `core/schema.py` 的人都要先理解「为什么有两套」。

于是整条链连同 `core/legacy_migrations.py` 一起删除。现在 `prepare_schema` 只有三条分支：
空库 `upgrade`、结构已正确但未登记版本 → 校验 + `stamp`、已接管 → `upgrade`；
**旧库（v20 之前）会被拒绝启动**并打印逐条差异——诚实地说「不支持了」，而不是悄悄 stamp。

守卫 **C9** 相应改成「旧链不许回来」：两个文件不得存在、生产代码不得引用
`SCHEMA_SQL` / `init_schema`、不得出现 `user_version` 字符串。

---

## 一句话验收

> **仓库层不再出现一条 SQL 字符串、不再出现一次 `commit()` / `rollback()`；
> 服务层能用 `session_scope()` 把多个仓库调用包成同一个事务。**

这句话是可以被静态检查的，不是形容词。判据落在 `tests/test_architecture_guards.py`（C1~C4、C8、C9）
与 `test_schema_parity.py` / `test_schema_migration.py`（C5~C7），且**每条都做过反向验证**
（故意写坏 → 必须转红 → 还原）。

---

## 为什么是现在

手写 `sqlite3` 在项目起步时是合理取舍：零 ORM 依赖、SQL 完全可控、`PRAGMA` / JSON 这类
SQLite 特有能力不用绕路。代价在 v5 的 T15 集中到期。

T15 要为「多线程同时首次映射 WAL 的 `-shm`」这个竞态做防护——那个竞态会返回
`SQLITE_READONLY` 而不是 `SQLITE_BUSY`，所以 `busy_timeout` 根本不重试。当时的解法是
自建一个固定大小连接池，把「建连接」收敛到启动期单线程。池本身是对的，问题在于**池也是要维护的代码**：

- 异常路径漏还连接（`try/except` 而非 `try/finally`）→ 池被借空、接口全部超时；
- `dispose()` 之后池被归还路径悄悄复活 → 旧库连接混进新库的池。

这两个都是成熟库二十年前就解决掉的问题。我们为绕开一个 SQLite 特性写了 444 行池代码，
然后在这 444 行里踩了两个 P0。**这不是「有人写错了」，是「自己维护基础设施」这笔账到期了。**

v6 的立场：**连接生命周期、事务边界、行映射这三件事，不再自己写。**

---

## 与 v5 的关系

| | v5 | v6 |
| --- | --- | --- |
| 主题 | 测试重构（ADR-060~072） | 数据层重构（ADR-073~082） |
| 收口 | T15「数据访问层」，T15.3 冻结在 batch A | T16.0~T16.13 |
| 资产处置 | 保留 `tests/` 重构成果、基线 877 例 | 444 行池在 T16.12 删除（T16.5 只切启动路径）；用例增至 1001 |
| 部署上线 | ADR-054~059 早已规划，未实施 | **推迟到 v7**，v6 期间不做部署变更 |

⚠️ T15 冻结留下的 batch A（5 个仓库文件已改 `lease()`，未提交）**不撤**：它的
`if owned:` 提交点就是迁移时的事务边界清单，改 SQLModel 时是「照单核对」而不是「重新推一遍」。

---

## 文档索引

| 文件 | 读它来回答什么问题 |
| --- | --- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 目标分层长什么样？约束清单有哪些？事务边界画在哪？ |
| [ADR.md](ADR.md) | 每个决定为什么这么定？被否决的方案是什么、为什么否决？ |
| [roadmap.md](roadmap.md) | 分成哪几批？每批的改动面、验收、回滚点是什么？ |

---

## 读完你会知道的三件反直觉的事

1. **`datetime` 不用。** 时间戳全部保持 `TEXT`。现有库里有真实数据，且 `WHERE expires_at > ?`
   这种字符串比较是**承重设计**（ISO 8601 字典序 = 时间序）。换成 `DateTime` 会同时改变
   落盘格式与比较语义，是「为了类型好看而引入数据风险」。见 ADR-077。

2. **`ON CONFLICT` 不换成「先查再写」。** 4 处 upsert 改用 SQLAlchemy 的
   `sqlite.insert(...).on_conflict_do_update()`，仍然是**一条语句原子完成**。
   换成 SELECT-then-INSERT 会把原本原子的操作拆成有竞态的两步。

3. **`metadata` 这个列名会炸。** `document_versions.metadata` 与 SQLAlchemy 声明式基类的
   `Base.metadata` 同名。模型里必须写成 `meta: str = Field(sa_column=Column("metadata", ...))`。
   这类「改名不改表」的映射是本次迁移里最容易静默出错的地方，已列入 T16.2 的验收清单。
