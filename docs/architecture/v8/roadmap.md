# v8 路线图：容器化与方言收敛

> 排期真源本文件；架构依据与决策取舍见本层 `ARCHITECTURE.md`。
> 任务编号用于日常排期（T18.x），版本号用于对外收口（v8.x，对照见 `README.md`）。

---

## 0 排期总原则

1. **先收敛，再打包。** 方言收敛的每一批都能用现有用例自证；镜像一旦做出来，
   「是代码错了还是镜像错了」就多了一个变量。顺序反了会把排障成本翻倍。
2. **零行为变化的先做。** T18.1 / T18.2 改完，975 例应当**一条都不用改**。
   如果需要改用例才能过，说明行为变了——停下来查，而不是改用例。
3. **有等价性缺口的单独一轮。** T18.3（时区聚合）需要一份逐指标对拍证据，
   不与别的批次混做。T18.4（结构对拍换 inspector）经实测**否决**，不实施，理由见该节。
4. **容器化的顺序是「能起」→「不丢」→「能回」。** 先让 compose 起来（T18.5），
   再补健康检查与排空（T18.6），最后做卷与备份演练（T18.7）。反过来的话，
   "备份脚本"会在一个还不知道能不能起来的形态上写。
5. **每一批都要能回退。** 收敛是行为中性的，回退就是 `git revert`；
   容器化回退是「换回 v7 的 systemd 形态」，所以 T18.5 之前不要拆掉 v7 的部署文档。

---

## 阶段总览

| 阶段 | 版本 | 目标 | 依赖 | 性质 |
| --- | --- | --- | --- | --- |
| T18.0 | v8.0 | 文档与勘察：四份文档 + 只读勘察脚本 | — | 文档 |
| T18.1 | v8.1 | 方言收敛（一）：upsert 收敛、库地址与空库判定 | T18.0 | 改代码，零行为变化 |
| T18.2 | v8.2 | 方言收敛（二）：`MEDIUMTEXT` 变体、collation 声明 | T18.0 | 改代码，零行为变化 |
| T18.3 | v8.3 | 方言收敛（三）：时区聚合搬进应用层 | T18.1 | 改代码，需对拍 |
| T18.4 | — | ~~结构对拍改用 inspector~~ **实测否决**，保留 `PRAGMA` 实现 | — | 不实施 |
| T18.5 | v8.5 | 镜像与编排：两个 Dockerfile、`compose.yaml`、`.dockerignore` | T18.1、T18.2 | 新增文件 |
| T18.6 | v8.6 | 健康检查与启停：`/healthz`、`/readyz`、排空、grace period | T18.5 | 改代码 + 配置 |
| T18.7 | v8.7 | 卷与备份：备份脚本进容器、首次启动迁移、回滚演练 | T18.5 | 脚本 + 演练 |
| T18.8 | v8.8 | 死代码与退役内容清理：删两个退役列、三个未引用组件与孤儿资源、处置 v7 悬空模板 | — | 改代码 + 迁移 |

```text
第一阶段：容器化 + 接口收敛
T18.0 文档
  │
  ├─→ T18.1 ──→ T18.3
  │      │
  └─→ T18.2 ──┴─→ T18.5 ──┬─→ T18.6
                          ├─→ T18.7
                          └─→ T18.8（清理，建议排在容器化之后）

第二阶段：双后端（详细设计见 附录 C）
T18.9 ──→ T18.10 ──→ T18.11 ──→ T18.12 ──→ T18.13 ──→ T18.14
                                                        └─→ T18.15

T18.4（结构对拍换 inspector）已否决，不在图中。
T18.9 是第二阶段的开关，必须先单独做完；T18.10 与 T18.8 合并成一次发布。
```

> **T18.8 排在最后不是因为它不重要**，而是因为它的两个动作方向相反：
> 删前端文件随时可做；删数据库列**不可逆**（回滚靠恢复备份，不是 revert 代码）。
> 排在容器化之后，可以复用 T18.7 刚刚演练过的备份流程，而不是再造一套。

---

## T18.0 · 文档与勘察（v8.0）

**目标**：把「v8 要做什么、不做什么」先钉住，避免实施阶段边做边改范围。

| # | 交付物 | 落点 | 验收要点 |
|---|---|---|---|
| 1 | 架构文档 | `docs/architecture/v8/ARCHITECTURE.md` | 含「本期不做」清单；契约 C-V8-1~7 可逐条检验 |
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

## T18.5 · 镜像与编排（v8.5）

**目标**：`docker compose up -d` 能起来，且起来之后数据库落在卷里。

| # | 交付物 | 落点 | 验收要点 |
|---|---|---|---|
| 1 | 后端镜像 | `deploy/docker/api.Dockerfile` | 多阶段；`USER 10001`；`CMD` 里 `--workers 1`；含 `migrations/` 与 `skills/` |
| 2 | 前端镜像 | `deploy/docker/web.Dockerfile` | 两阶段 node 构建 → `nginx:alpine`；产物含 `/admin/` 子目录 |
| 3 | 编排 | `deploy/docker/compose.yaml` | 两服务；`api` 只 `expose`；三个命名卷；`stop_grace_period` |
| 4 | Nginx 站点 | `deploy/docker/nginx.conf` | 沿用 v7 三行 SSE 配置 + `/admin/` 的 `try_files` |
| 5 | 构建排除 | `.dockerignore` | 逐条对照 `ARCHITECTURE.md` §7.2 的七个排除项 |
| 6 | 启动脚本 | `deploy/docker/docker-entrypoint.sh` | 只在卷为空时校正属主；**不跑迁移**（C-V8-6） |

**验收**：全新机器上 `docker compose up -d` 后，`/`、`/admin/`、`/api/...` 三者可达；
`docker compose exec api sh -c 'ls /var/lib/prdwright'` 能看到 `prdwright.db`；
重启容器后数据仍在。

---

## T18.6 · 健康检查与启停（v8.6）

**目标**：编排能分辨"活着"与"能接客"，且重建容器不静默丢任务。

| # | 交付物 | 落点 | 验收要点 |
|---|---|---|---|
| 1 | 存活探针 | `api/health.py` 新增 `GET /healthz` | 固定 200，不查库 |
| 2 | 就绪探针 | 同文件新增 `GET /readyz` | 三条件（连库 / revision==head / 池已预热）任一不满足返回 503 |
| 3 | 认证白名单 | `api/deps.py` 的 `WHITELIST` | 两条新路径放行（它们必须能在未登录时被探针访问） |
| 4 | 优雅关闭 | `services/job_service.py` 新增排空函数 | `SIGTERM` 后把 running 任务落 `failed`，再 flush 日志 |
| 5 | 编排配置 | `compose.yaml` | `healthcheck` 指向 `/healthz`；`stop_grace_period: 300s` |
| 6 | 实测校准 | 本文件附录 | 用一个真实长任务测出实际需要多久，回填 grace period |

**验收**：`docker compose stop` 时在途任务被标记为中断而不是留在 `running`；
`/render` 之后立刻访问 `/readyz` 返回 503，`/healthz` 仍 200。

**风险**：新路径必须加进白名单，否则探针会被鉴权拦成 401，表现为"容器永远不健康"。

---

## T18.7 · 卷与备份（v8.7）

**目标**：数据能拿出容器，也能放回去。

| # | 交付物 | 落点 | 验收要点 |
|---|---|---|---|
| 1 | 备份脚本 | `scripts/deploy/backup.py` 适配容器 | `VACUUM INTO` + 自检（可打开 / `integrity_check` / 读得到 `alembic_version`） |
| 2 | 取出方式 | 文档给出 `docker cp` 命令 | 明确写"不许对卷目录直接 `cp`" |
| 3 | 首次启动迁移演练 | 本文件附录 | 空卷启动 → `alembic_version` 等于 head；重建容器不重复迁移 |
| 4 | 回滚演练 | 本文件附录 | 换回旧镜像 tag + 恢复备份 → 服务可用；记录"代码旧、库新"时的启动失败表现 |
| 5 | 卷删除警示 | 文档 | `docker compose down -v` 会连数据一起删，标为危险命令 |

**验收**：备份产物在另一台机器上能恢复并被服务读取；回滚演练记录完整。

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
所以前置条件是「备份 + 自检通过」，且与 T18.7 演练过的流程保持一致。

**验收**：附录 A §6 的六条判据逐条通过。

---

## 第二阶段：双后端（T18.9 ~ T18.15）

**目标**：`DATABASE_URL` 指向 SQLite 或 MySQL，同一份代码、同一套用例都成立。
完整设计（含差异处置表、契约 C-M1~M7、验收 8 条）见 **附录 C**；
决策见 附录 C。

这一组与前面 T18.1~T18.8 的分工：前面是"代码不再依赖某个库"，这里是"两个库都能真跑"。

| 阶段 | 内容 | 验证判据 | 对应 |
|---|---|---|---|
| T18.9 | 装配层：`core/database.py`（URL / 方言名 / 引擎装配）、`DATABASE_URL` 配置、`DB_PATH` 兼容糖 | 1006 例在 SQLite 上全绿，行为零变化 | P1 |
| T18.10 | 类型策略：`core/types.py`（`short_text` / `long_text` / `timestamp_text`）+ 列类型重基线 + 删大文本列的 `server_default` | `alembic upgrade head` 在两个后端空库上都成功 | P2 |
| T18.11 | 结构对拍双实现（`core/snapshot/sqlite.py` + `mysql.py`）+ `render_as_batch` 按方言开关 | 两个后端各跑一次 `test_schema_parity` | P3 |
| T18.12 | 查询与并发：`dialect.ilike` / `lock_row` / `retry_on_deadlock`；4 处 `LIKE` 改造；两条"先查后写"路径加锁 | MySQL 上新增 3 条并发用例绿 | P4 |
| T18.13 | 测试基建：`dbsupport` → SQLAlchemy `Connection`；`sqlite_only` / `mysql_only` 标记；MySQL 用「建表一次 + 每例清空」；`scripts/deploy/test-mysql.sh` | 契约类用例两个后端都绿、特性类各自绿；本地一条命令能跑 MySQL | P5 |
| T18.14 | CI 矩阵（两个 job 同一条命令）+ `--strict-markers` + 结构等价对拍 + 10 个接口的 golden 响应 | 两个 job 常绿；MySQL 全量 ≤ 4 分钟 | P6 |
| T18.15 | 数据搬运：ETL（按外键拓扑排序、环形外键三步走）+ 行数/哈希校验 + 备份按后端分派 | 一次真实搬运，行数与哈希对得上；备份可恢复 | P7 |

三条排序理由：

1. **T18.9 必须单独做完**。它是"开关"——装好之后，后面每个阶段都能在两个后端之间
   切换验证；否则每次试 MySQL 都要手工改代码。
2. **T18.10 与 T18.8 合并成一次发布**。改列类型和删列都要在 SQLite 上重建表
   （batch 模式），分两次做等于把同样慢的操作做两遍。
3. **T18.14（CI）不能放到最后才补，但也不能早于 T18.13**。没有矩阵的阶段，MySQL 只是
   "本地试过"——而"试过"和"支持"的区别全在 CI 有没有跑。但矩阵早于标记机制上线，
   MySQL job 会一半红（特性类用例在错的后端上跑），然后有人开始加 `skip` 让红变绿。
   **顺序必须是：先有标记，再有矩阵。**

> 这一组的每一步都**不改业务语义**：T18.9 改装配，T18.10/T18.11 改声明与工具，
> T18.12 加锁（并发正确性只增不减），T18.13 改测试工具，T18.15 只搬数据。
> 如果某一步需要改 `services/` 的业务判断，说明方案有问题，停下来重新看。

> ⚠️ **测试不是收尾工作。** T18.9 一装好开关，T18.13 的标记机制就该跟着上——
> 否则从那一刻起，新写的用例都不知道自己属于"双跑"还是"单跑"，
> 而这类欠账在 1000 多个用例的规模上，越晚补越贵。

---

## 附录：待回填的实测值

| 项 | 从哪来 | 什么时候填 |
|---|---|---|
| `stop_grace_period` 的真实需求 | T18.6 用一个真实长任务测 | T18.6 |
| 按天统计的查询计划 | T18.3 的 `EXPLAIN QUERY PLAN` | T18.3 |
| 生产库的列长度与脏数据量 | 在服务器上跑 `schema_survey.py` | T18.7 之前 |
| 镜像体积 | T18.5 构建后 `docker images` | T18.5 |
