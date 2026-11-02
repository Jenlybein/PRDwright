# v8 路线图：方言收敛与清理

> 排期真源本文件；架构依据与决策取舍见本层 `ARCHITECTURE.md`。
> 任务编号用于日常排期（T18.x），版本号用于对外收口（v8.x，对照见 `README.md`）。

---

## 0 排期总原则

1. **零行为变化的先做。** T18.1 / T18.2 改完，975 例应当**一条都不用改**。
   如果需要改用例才能过，说明行为变了——停下来查，而不是改用例。
2. **有等价性缺口的单独一轮。** T18.3（时区聚合）需要一份逐指标对拍证据，不与别的批次混做。
   T18.4（结构对拍换 inspector）经实测**否决**，不实施，理由见该节。
3. **删除排在有判据之后。** 清理（T18.8）的每一步都要求「`grep` 可证 + 用例不红 +
   不改业务逻辑」三条同时成立；缺一条就不动，缺的那条写进附录 A 的待判清单。
4. **不可逆的动作落在同一个窗口。** 删列不可逆，回滚只能靠恢复备份：
   `VACUUM INTO` + 自检 → 迁移 → 全量用例，三步同一窗口，备份没做成不许跑迁移。
5. **每一批都要能回退。** 收敛与删代码都是行为中性的，回退就是 `git revert`；
   唯独删列例外——它的回退是恢复备份，不是 revert（C-V8-2）。

---

## 阶段总览

| 阶段 | 版本 | 目标 | 依赖 | 性质 |
| --- | --- | --- | --- | --- |
| T18.0 | v8.0 | 文档与勘察：三份文档 + 只读勘察脚本 | — | 文档 |
| T18.1 | v8.1 | 方言收敛（一）：upsert 收敛、库地址与空库判定 | T18.0 | 改代码，零行为变化 |
| T18.2 | v8.2 | 方言收敛（二）：`MEDIUMTEXT` 变体、collation 声明 | T18.0 | 改代码，零行为变化 |
| T18.3 | v8.3 | 方言收敛（三）：时区聚合搬进应用层 | T18.1 | 改代码，需对拍 |
| T18.4 | — | ~~结构对拍改用 inspector~~ **实测否决**，保留 `PRAGMA` 实现 | — | 不实施 |
| T18.8 | v8.8 | 死代码与退役内容清理：删两个退役列、三个未引用组件与孤儿资源、处置 v7 悬空模板 | T18.0 | 改代码 + 迁移 |

```text
T18.0 文档与勘察
  │
  ├─→ T18.1 ──→ T18.3      （方言收敛：只收敛接口，零行为变化）
  ├─→ T18.2 ──┘
  └─→ T18.8                （清理：零风险项先删，删列最后）

T18.4（结构对拍换 inspector）已否决，不在图中。
```

> **T18.8 排在最后不是因为它不重要**，而是因为它的两个动作方向相反：
> 删前端文件随时可做；删数据库列**不可逆**（回滚靠恢复备份，不是 revert 代码）。
> 所以先把零风险项跑通、把备份窗口演练一遍，再动列。

---

## T18.0 · 文档与勘察（v8.0）

**目标**：把「v8 要做什么、不做什么」先钉住，避免实施阶段边做边改范围。

| # | 交付物 | 落点 | 验收要点 |
|---|---|---|---|
| 1 | 架构文档 | `docs/architecture/v8/ARCHITECTURE.md` | 含「本期不做」清单；契约 C-V8-1~3 可逐条检验 |
| 2 | 方言专题 | `docs/architecture/v8/ARCHITECTURE.md 附录 B` | §3.6 不收敛清单覆盖 10 项；每项标「永久适配 / 换模型 / 语义差异」 |
| 3 | 勘察报告 | `docs/architecture/v8/ARCHITECTURE.md 附录 D` | 7 节齐全；脏格式与字节数有实测值 |
| 4 | 勘察脚本 | `backend/scripts/schema_survey.py` | `mode=ro` 只读；`--out` 可写文件；生产库可直接复用 |
| 5 | 决策与取舍 | 本层 `ARCHITECTURE.md` 的「取舍」节 | 已写进正文 |
| 6 | 路线图 | 本文件 | 阶段之间无循环依赖 |

---

## T18.1 · 方言收敛（一）（v8.1）

**目标**：把三处 upsert 收进一个模块，让业务三层不再出现数据库品牌。

| # | 交付物 | 落点 | 验收要点 |
|---|---|---|---|
| 1 | 收敛点 | `core/dialect.py` | 只暴露 `upsert_values` / `upsert_select`；无业务语义 |
| 2 | 改写三处调用 | `document_repo.py`（`_ensure_document`）、`chat_session_repo.py`（`bind_thread`）、`runtime_config_repo.py`（`upsert_many`） | 三处都不再 import `sqlalchemy.dialects.sqlite` |
| 3 | 库地址反推 | `core/schema.py::_database_url` | 改用 `engine.url.render_as_string(hide_password=False)` |
| 4 | 空库判定 | `core/schema.py::_is_empty` | 改用 `inspect(engine).get_table_names()`，不再出现 `sqlite_sequence` |
| 5 | 契约用例 | `tests/test_dialect.py`（新增） | 断言 helper 在 SQLite 上产出的 SQL 与原 `sqlite_insert` 等价；断言 `update_cols=None` 时是 DO NOTHING |

**验收**：全量用例通过，且**没有一条用例被修改**。重点看
`test_repo_contract_batch_*`（仓储契约）与 `test_db_concurrency`。

**风险**：`_ensure_document` 那条 `INSERT ... SELECT ... WHERE` 的归属校验写在
SELECT 里，改写时必须保留这个形状。改成「先查归属再插入」会重新引入窗口。

---

## T18.2 · 方言收敛（二）（v8.2）

**目标**：把"只对 MySQL 成立"的两条约束提前声明在模型里，SQLite 侧零变化。

| # | 交付物 | 落点 | 验收要点 |
|---|---|---|---|
| 1 | `MEDIUMTEXT` 变体 | `models.py` 的 4 列：`document_versions.content`、`generation_jobs.draft_content`、`generation_jobs.completed_sections`、`solutions.data` | SQLite 下仍是 `TEXT`；`alembic check` 无变化 |
| 2 | 大小写敏感 collation | `models.py` 的 `users.username` | 声明 `mysql_collate="utf8mb4_0900_as_cs"`；SQLite 侧忽略 |
| 3 | 结构对拍 | `tests/test_schema_parity.py` | **不需要改**——若红了说明声明位置写错 |

**验收**：`test_schema_parity` 与 `alembic check` 均无差异。这一批的正确性判据是
"什么都没变"，所以任何差异都是错误信号。

---

## T18.3 · 方言收敛（三）（v8.3）

**目标**：去掉全项目唯一一处既依赖方言函数、又没有优雅分支的查询。

| # | 交付物 | 落点 | 验收要点 |
|---|---|---|---|
| 1 | 边界计算 | `admin_repo.py` 新增「本地日 → UTC 区间」的纯函数 | 对任意 `tz_offset_minutes`（含 330 这种半小时）都正确 |
| 2 | 分桶查询 | 同文件，替换 `func.datetime` | 只用比较运算与 `case`，无任何库函数 |
| 3 | 对拍用例 | `tests/` 新增 | 同一份库上新旧两版逐日逐指标相等（`task_count` / 四种状态 / 两种 token） |
| 4 | 性能记录 | 本文件附录 | 记录 `EXPLAIN QUERY PLAN`，确认过滤走 `created_at` 索引 |

**验收**：对拍用例逐指标相等；`test_admin_management` 全绿。

**风险**：四个 `case` 聚合是四个独立判断，错一个不会报错，只让看板数字悄悄不对。
所以对拍必须逐指标，不能只比 `task_count`。

---

## T18.4 · 结构对拍换 inspector：**实测否决**

**原目标**：让结构对拍不再使用被检查对象的私有接口。

**否决理由**（探测脚本对现有库实测）：

| 判据项 | `PRAGMA` | `inspect()` |
|---|---|---|
| 外键 `ON DELETE` | `CASCADE` / `SET NULL` 都能读 | `options` 返回 `{}`，**规则丢失** |
| 部分索引 `WHERE` | 从 `sqlite_master.sql` 取 | 只能从 `dialect_options['sqlite_where']` 取，那本身是方言选项 |

级联规则是 `assert_schema_matches_models` 最重要的一条判据（`current_version_id` 是
`SET NULL`、`document_id` 是 `CASCADE`，写错任一都会留下孤儿或误删，且不报错）。
换 inspector 后两边快照都不含 `ondelete`，`diff_snapshots` 会判它们相等——
不是判据变松，是**这一项消失**。

**结论**：保留 `core/schema_snapshot.py` 的 `PRAGMA` 实现。代价是它只在 SQLite 上成立，
但它是测试与启动校验工具，不在业务路径上。详见 附录 B §3.5。

**留存的产出**：探测结论本身。下次有人提"把结构对拍换成 inspector"，先看这张表。

---

## T18.8 · 死代码与退役内容清理（v8.8）

**目标**：仓库里不存在「读不到、也删不掉」的东西。完整清单与判据见 **附录 A**。

为什么值一个独立阶段：这类内容的成本不在运行时，而在**每个读代码的人**——他必须先判断
「这一列还能不能用」，判断成本天天发生，删除成本只发生一次。

| # | 交付物 | 落点 | 验收要点 |
|---|---|---|---|
| 1 | 删 `solutions.data` 列 | 一条 Alembic 迁移 + `models.Solution` 删字段 | `grep -rn "\.data\b" backend/src` 只剩注释；结构对拍绿 |
| 2 | 删 `model_providers.builtin` 列 | 同一条迁移（或独立一条） | `grep builtin` 只剩历史迁移与注释 |
| 3 | 删三个未引用组件 + 三个孤儿资源 + 两个零引用函数 + 一个死类型 | `ui/{AppToast,AppEmpty,AppInput}.vue`、`assets/{main.css,base.css,logo.svg}`、`api.ts` 的 `getQuestions` / `generatePrdStream`、`types/index.ts` 的 `SessionData`（逐项证据见 附录 A A3~A5 / A10） | 全仓 `grep` 零命中；两个前端仍能 `npm run build` |
| 4 | 处置 v7 悬空模板 | `../v7/` 文档加注（推荐废弃，不补） | 文档里出现的每个 `deploy/...` 路径，`ls` 都能看见 |
| 5 | 工具扫描记录 | 附录 A 附录补实测结果 | `ruff F401/F811`、`vulture`、`knip` 各跑一次，结论落纸 |

**顺序**：先 3（零风险，验证清理流程），再 1/2（不可逆），最后 5（扫描可能引出新项）。

**风险**：删列**不可逆**。删完之后旧代码读不到列，回滚只能靠恢复备份。
所以前置条件是「备份 + 自检通过」，且备份按附录 A §9 记下的窗口执行（`VACUUM INTO` +
三条断言 → 迁移 → 全量用例）。

**验收**：附录 A §6 的六条判据逐条通过。

---

## 附录：待回填的实测值

| 项 | 从哪来 | 什么时候填 |
|---|---|---|
| 按天统计的查询计划 | T18.3 的 `EXPLAIN QUERY PLAN` | T18.3 |
| 生产库的列长度与脏数据量 | 在服务器上跑 `schema_survey.py` | T18.8 之前（删列前先勘察） |
