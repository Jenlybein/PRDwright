# PRDwright 现状架构

> **一句话**：本地优先的 PRD 生成平台；这份文档是仓库里唯一一版架构结论，正向写「目标 → 范围（含本期不做）→ 设计 → 契约 → 取舍」。
>
> 导航：[`README.md`](./README.md) 是**时间线真源**（阶段总表、v2 七波重写、已确认的失真、复核方法）；[`roadmap.md`](./roadmap.md) 是现状排期；[`../adr/0001-architecture-docs-consolidation.md`](../adr/0001-architecture-docs-consolidation.md) 是本次文档重组的**架构决策**；术语以 [`../../GLOSSARY.md`](../../GLOSSARY.md) 为准。
>
> v1~v9 是九个历史**层**，其目录已删除；演进史的索引是本文 §6 的九层摘要表，阶段规模与界碑只在时间线真源里写一次。**「v10」不是架构层**，它指数据库迁移版本与一批已关闭的历史 issue 编号。

---

## 1. 目标

1. **一处可读**：维护者打开仓库，读这一份文档就能回答「现在怎么跑、边界在哪、为什么这么选」，不必判断「哪一层说的算」。
2. **结论可追溯**：契约有编号（C-V9-1~10、C-D1~C-D6）；承重的实测数据、方言可移植性台账与恢复演练要求整段留在本文附录，不靠 git 历史当唯一依据。
3. **收口可验证**：文档里写出的每个仓库路径 `ls` 都能看见；一次改动把代码、架构文档、路线图三者同步更新。
4. **产品能力不变**：三份文档、澄清、审核、改写、技能包内容、鉴权与配额、用户端交互都按现行形态运行。

---

## 2. 范围

### 2.1 本文覆盖的现行架构

| # | 范围 | 落点 |
| --- | --- | --- |
| 1 | 后端分层（api/services/repositories/core） | §3.1 |
| 2 | 三份文档与文档版本链 | §3.2 |
| 3 | 生成任务与 SSE 契约 | §3.3 |
| 4 | 技能包 | §3.4 |
| 5 | 鉴权与配额 | §3.5 |
| 6 | 部署形态（单域名 / 单 worker / 命名卷 / 探针 / 排空 / 备份） | §3.6 |
| 7 | 唯一部署后端 SQLite | §3.7 |
| 8 | 现行契约 | §4 |
| 9 | 取舍、接受的风险、否决的方案与风险登记 | §5 |
| 10 | 九层摘要表（演进索引） | §6 |
| 11 | 承重专题：方言可移植性台账、库结构勘察实测数据、恢复演练要求 | 附录 A / B / C |

### 2.2 本期不做

| 事项 | 为什么本期不做 |
| --- | --- |
| **换数据库后端** | 项目固定 SQLite；没有第二个部署形态要支持，换库要连迁移链、结构对拍与备份链一起重做，收益不抵成本 |
| **k8s / Swarm、多副本、外置运行时表或跨进程事件总线** | 目标是单机 compose，滚动更新与探针权重还没有需要它的人；多副本要先外置 `_RUNTIMES` / `JobBus`，那是另一版的工作 |
| **全库真类型化**（时间从 ISO 文本改真时间、布尔改原生） | 自带 API 时间契约变更与两个前端的适配，改动面与收益不成比例 |
| **容器内 TLS、多架构镜像、开发者热重载容器** | 证书生命周期不放进取舍受限的单机 compose；开发仍走本机 `uv run uvicorn` 与两个 `npm run dev` |
| **业务链路自动化测试 / 老数据迁移** | 容器冒烟只验「主链路可达」；SQLite 库不搬迁，旧库结构不符时按「检测到旧结构就拒绝启动」处理 |
| **在途任务续跑或回放** | 运行态以内存为权威；重启仍中断在途任务，数据库投影只保证可读，不保证可继续执行 |
| **重开已关闭的双后端系列** | 那批 v10 issue 是历史记录，不是架构层 |
| **按层重写演进史** | 演进史只在 §6 留九行索引；阶段规模、界碑与失真由时间线真源独占，不在本文重复 |
| **改全局映射、后端或界面交互** | 入口模式登记表只收随入口模式变化的事实；14 个视图状态 → 进度格的全局映射、后端、数据库结构、界面文案与界面交互一律不动（见 §5.8） |

> §2.2 收自 v9 §1.3 的「本期明确不做」，并按现行形态补齐；入口模式这一轮的边界见 §5.8。

---

## 3. 设计

### 3.1 后端分层（api/services/repositories/core）

`backend/src/backend/` 下四层单向依赖：

| 层 | 职责 | 边界 |
| --- | --- | --- |
| `api/` | 解析 / 校验 / 封装 | Pydantic 模型集中在 `api/schemas/`（按领域拆成 9 个子模块，`__init__.py` 是唯一出口）；Router 各带完整 prefix，`main.py` 不叠加 |
| `services/` | 业务编排 | 一个业务操作 = 一个事务；事务边界唯一落点 `session_scope()`，不许在 `async def` 里跨 `await` 持有 |
| `repositories/` | 唯一碰模型与查询的地方 | 绑定 `Session` 的实例方法；返回模型对象或 frozen dataclass 投影，不返回裸 dict；不许 `import sqlite3`、不许 `.commit()` / `.rollback()`、不许 SQL 字符串字面量 |
| `core/` | 引擎、事务、类型、时钟、限制、方言、配置 | `expire_on_commit=False` 防 `DetachedInstanceError`、`autoflush=False`（要自增 id 就自己 `flush()`）、`max_overflow=0` 躲 WAL `-shm` 竞态 |

- 列类型只走 `core/types.py`（`short_text(n)` / `long_text()` / `timestamp_text()`）；大段文本上限 60KB 字节，闸门在 `core/limits.py::ensure_text_within_limit`，接在 `append_version` 与 `persist_progress/finish/fail`。
- Schema 只走 Alembic：手写迁移落 `backend/migrations/versions/`，入口 `core/schema.py::prepare_schema`，在 `lifespan` 里跑（重启 = 跑迁移）；autogenerate 在 SQLite 会生成 `drop_constraint(None)`，因此迁移必须手写。
- 删列 / 退役列是**单向门**（回滚靠备份、不设兼容期）；**加列不是单向门**。
- 提示词只在后端；前端只能点名场景（`profile_key`），不能递剧本文本。

### 3.2 三份文档与文档版本链

一个**方案**下挂 PRD、接口文档、提示词三份**文档**。正文只住 `document_versions`（**文档版本**是正文的一次不可变快照），`documents` 只剩身份 + `current_version_id` 指针，读当前一律 `LEFT JOIN`。

- 整篇生成 = 追加新版本（不再删下游）；`optimize` / `clarify` / 手改 = 就地改当前版本；checkpoint / **恢复** = 追加新版本。当前版本指针只前进不回拨，历史只增不改。
- 环形外键：先空指针插 `documents` 行 → 插版本 → 回填指针（`_ensure_document()`）。
- 写正文唯一入口是生成任务成功收尾（`_write_document` → `commit_generated`）：正文 + 待确认清单 + 回执同批写入，失败不覆写正文；新增产出正文的路径接进 `_build_event_stream` 的 `done`。
- 上游正文变化（含手改，按 `content_sha`）只让下游 `stale=true` 亮黄条：不删除、仍可交付、只汇总提示不硬拦。
- 交付走 `GET /api/solutions/{id}/export`：ZIP = 三份 md + `README.md`；目录名消毒、上限 50MB、CSV 带 BOM。

### 3.3 生成任务与 SSE 契约

**生成任务**是一次管线运行，有独立生命周期，与「用户是否正连着看」解耦：`POST /api/jobs` 建任务 + `GET /{id}/stream` 订阅；`detach()` 只退订不取消，同一任务绝不并发订阅两次。

- 阶段：需求澄清 → 大纲 → 撰写 → 审核 → 改写。两条并行信息轨是**过程字** `chunk`（可能缺标题）与**权威正文** `part`（整段覆盖该章、`index` 可乱序）；混用会把审核 JSON 写进正文。
- SSE 分帧唯一实现 `backend/src/backend/api/sse.py::make_sse_generator`（已带 `X-Accel-Buffering: no`）：事件 `status/chunk/part/stage/review/done/error` + 末尾 `data: [DONE]`；`: ping` 心跳只能走这个出口；服务层与路由层两层异常兜底；前端用原生 `fetch` + `ReadableStream`。
- 重连先收快照再接实时流，恢复正文 = `finalized_text + live_buffer`；失败不覆写正文；**被取代**与**已取消**是两种不同终局；只有 `high` 严重度触发改写、改写后不复审。
- 运行态以内存为权威：`_RUNTIMES` / `JobBus` / runtime 配置快照（5 秒 TTL）/ 固定连接池都在进程内；请求参数、配置/供应商快照与技能包身份在建任务那一刻一次写全，收尾不再另取供应商四格；进度按 300ms / 4KB 合并写 `draft_content / completed_sections / phase / pending_items`。
- token 计量以 `ContextVar` 挂成环境值（`backend/src/backend/services/llm_meter.py`）：观测层挂在模型调用之外，**业务层调用方式不变**；没有任务上下文时计量自然无操作。预算检查在 `astream_text` 开头、记账在末尾，超顶复用 `INVALID_REQUEST`。
- 排空顺序固定：就绪探针转 503 → 停止接收新任务 → 最多等 30 秒（等待上限、不是必须等满）→ 超时者标为已取消落终态并向订阅者发错误事件 → flush 日志退出。`stop_grace_period` 必须大于 30 秒。排空是**可直接调用的单元**（不起子进程、不发真信号），用例毫秒级验证。
- 启动回收孤儿任务（`reap_orphans`），把 `running` 收成确定终态，重启不留「进行中」的尸体。

### 3.4 技能包

`skills/` 是管线知识的唯一来源：归属与资源由 `skill.yaml` 自述（`applies_to` / `references[]`），加包 = 加目录、不加中心注册表；唯一入口 `load_skill_artifacts(kind)`，规范整段注入、不检索，改写 / 澄清按白名单 `format_rewrite_reference`。缺件返回 `None` 降级，且降级要在回执上看得见。

部署态把技能包随代码打进镜像、不挂载，`PRDWRIGHT_SKILLS_STRICT=true` 保持开启：技能包缺失时启动直接失败，而不是静默退回内置提示词。理由只有一条：**镜像即完整交付物**，挂载会让「镜像里的代码」与「挂载进来的技能包」版本不一致成为可能，而就绪探针检查的恰好是「技能包可加载」。

### 3.5 鉴权与配额

- 会话：随机 token 存库 + HttpOnly Cookie，`TokenIssuer` 封装以便替换；密码 `hashlib.scrypt` + 独立 salt；不上 JWT。单点登录 `replace_for_user` 在一条事务里先删该用户全部旧会话再插入。邀请码注册（`invite_codes.max_uses` 0 = 不限，首启播种 `INVITE_CODE`）。
- 门禁 = 中间件默认拦截 + 白名单（`/`、`/health`、`/api/auth/*`、`/api/conversation/questions`，以及 `/healthz` / `/readyz` 两条探针）；中间件必须是纯 ASGI 类；`AUTH_ENABLED` 默认 true。越权 / 不存在一律 404。未登录禁止创建任务：可浏览入口页，点入口即弹登录框。
- 主密钥只以文件形式存在（不在数据库、不在环境变量），由 `scripts/keygen/generate_keys.py` 生成与轮换；**密钥条目**密文入库、面板只回 `key_present`；轮换先重加密再换主密钥，主密钥保留每一把。
- **配额**给单个用户设用量上限；额度检查与记账在 `astream_text` 两端。管理端真边界是后端 `require_admin`，前端门禁只是体验；**审计日志**有意不随用户删除而级联清理。
- 日志唯一入口 `configure_logging`，双写轮转文件（永开）与 `app_logs`（可关）；`request_id` 由格式化器从 contextvar 现取。

### 3.6 部署形态（v9 落盘）

`deploy/` 一个目录装全部部署件：`deploy/compose.yaml`（唯一编排，全平台一份）、`deploy/.env.example`、`deploy/Makefile`、`deploy/backup.py`、`deploy/backups/`、`deploy/docker/`、`deploy/README.md`。宿主只发布 `80`，Nginx 唯一对外：`/` → 用户端 dist、`/admin/` → 管理端 dist、`/api/` 与两条探针 → `api:8000`（只 `expose` 不 `ports`）。**单域名 + 路径前缀**的硬理由是 Cookie host-only，跨子域会「后台登录了、调 API 仍 401」；`CORS_ORIGINS` 设空串。Nginx SSE 三行（`proxy_buffering off` / `read_timeout 1800s` / `gzip off`）+ 服务端 `: ping` 心跳；`assets/*` 长缓存、`index.html` 必须 `no-store`。

- **两个镜像**：应用镜像（`python:3.12-slim`，多阶段，`uv sync --frozen --no-dev`，`CMD` 写死 `--workers 1`）+ 静态站点镜像（`nginx:alpine`，node 构建两个前端）。`uv sync` 与 `npm install` 缓存键不同、两进程重启语义不同；合并成单容器会让「重启 Nginx 杀掉在途任务」。
- **单 worker 硬约束**：`_RUNTIMES` / `JobBus` / runtime 配置快照 / 固定连接池 / 技能包缓存都在进程内，多 worker 会让任务与 SSE 分属不同进程。不提供环境变量覆盖、不写 `replicas`、不引 gunicorn。
- **三个命名卷**：数据（SQLite 库 + `-wal` / `-shm` 边车）、日志、密钥（只读 `master.key`）。命名卷是**语义问题不是配置问题**：WAL 的 `-shm` 是 mmap 共享内存，Docker Desktop for Windows 的 bind mount 不支持 SQLite 需要的文件锁语义，症状是随机 `disk I/O error` 或静默丢最近几分钟写入。宿主目录挂载 + Windows 专用覆盖文件因此被否决；实时库不能双击查看，可见性由备份产物补上。
- **两个探针**：`GET /healthz` 恒 200、不查库不查配置不发外部请求（判生死）；`GET /readyz` 判「库可写」且「技能包可加载」，任一不满足返回 503 且响应体指出是哪一项（判就绪）。两者都进认证白名单，漏了会表现为「容器永远不健康」。`/health` 语义不变（恒 200、`status` 只有 `ok` / `degraded`、三项体检），它是人看的详情页，不是机器闸门；探针也不查模型供应商配置（依赖外部网络、高频会烧额度会误报）。
- **排空**：见 §3.3。就绪探针转 503 给「摘流」留时间，新的生成请求返回明确失败而不是排进一个马上要死的队列。
- **备份**：一条命令产出带日期的 `.db` 与同名 `.sha256`，落 `deploy/backups/`；SQLite 在线备份 `VACUUM INTO`（不能 `cp`，开了 WAL 会静默缺最近写入），自检 `integrity_check` = `ok` + 读得到 `alembic_version` + sha256。迁移之前必须先有备份，备份没做成不许起栈；删列与改类型后的回滚方式是恢复备份，不是 `git revert`。恢复演练要求整段见附录 C。
- 启动脚本以 root 校正数据 / 日志目录属主后降权 `exec`，代码层只读；迁移只由 `lifespan` 跑一次（C-V9-4）。主密钥缺失即拒绝启动并打印生成指引，绝不自动生成。`PRDWRIGHT_MASTER_KEY_PATH` 只经 `os.environ` 读，`.env` 文件读不到，必须走 `EnvironmentFile`；`PRDWRIGHT_SKILLS_DIR` 走 `lookup_env`。日志双写 stdout + 日志卷。
- 镜像 tag 用 `git describe`；CI **只构建不推送**，整栈冒烟轮询 `/readyz` 直到响应体 `status == "ready"`（只看 200 会假绿），并断言 api 的 PID 1 以非 root uid 运行。

### 3.7 唯一部署后端：SQLite

数据库固定是数据命名卷里的那个 SQLite 文件，编排里只有 `api` 与 `web` 两个服务，没有第三个数据库容器，也没有留给它的端口或卷。可移植性四模块边界 = `core/database.py`（连哪个库）/ `core/dialect.py`（语句）/ `core/types.py`（列声明）/ `core/schema.py`（迁移）；MySQL 相关分支是遗留代码（未验收、不再扩展），不计划引入其它后端。改动遗留 MySQL 分支时仍守三条：`TEXT` 列不能有字面量 `server_default`、索引列必须 `short_text(n)`（n≤768）、死锁要重试整个事务。

结构对拍保留 `PRAGMA` 实现（实测 `sqlalchemy.inspect` 会丢外键 `ON DELETE`，那会让判据消失而不是变宽松）；完整方言台账与 C-D1~C-D6 在附录 A，库结构勘察实测数据在附录 B。

---

## 4. 契约

### 4.1 C-V9-1 ~ C-V9-10（部署与运行形态）

| # | 契约 | 破了会怎样 |
| --- | --- | --- |
| **C-V9-1** | 宿主只发布 `80`；`api` 的 `8000` 只 `expose` 不 `ports` | v7 的「后端只听回环」退化成「后端直接对外」，CORS 与 Cookie 行为随之改变 |
| **C-V9-2** | `api` 副本恒为 1，`--workers 1` 写死在镜像 `CMD`，不提供覆盖 | 任务与 SSE 分属不同进程，客户端永远等不到输出 |
| **C-V9-3** | 数据、密钥、日志**必须是命名卷**，禁止 bind mount 宿主目录 | 随机 `disk I/O error`、静默丢最近写入，Windows 上必现 |
| **C-V9-4** | 迁移只在 `lifespan` 里跑一次，启动脚本不跑迁移 | 两个入口各跑一次，迁移日志与锁行为都不可预测 |
| **C-V9-5** | 技能包打进镜像，不挂载；`PRDWRIGHT_SKILLS_STRICT=true` | 镜像与技能包版本可能不一致，就绪探针失去意义 |
| **C-V9-6** | 主密钥缺失即拒绝启动，绝不自动生成 | 换机器时旧密文永久解不开，且当场无人发现 |
| **C-V9-7** | `/health` 语义不变（恒 200）；探针一律用 `/healthz` / `/readyz` | 可降级状态被当成故障，容器被反复重启 |
| **C-V9-8** | 排空窗口 30 秒；超时的在途任务落「已取消」终态，不留 `running` | 每次重启都留尸体，只能等下一条启动的 `reap_orphans` 收尾 |
| **C-V9-9** | 备份以 SQLite 在线备份（`VACUUM INTO`）产出，产物必须有 `sha256` 且自检通过 | 「备份完成」变成一句没有证据的话 |
| **C-V9-10** | CI 只构建镜像，不推送；tag 用 `git describe` | 没有凭据通道的推送会把 CI 变成不可复现的猜谜 |

### 4.2 C-D1 ~ C-D6（方言可移植性）

| 契约 | 内容 | 破了会怎样 |
| --- | --- | --- |
| **C-D1** | `upsert` 的冲突目标只在「该表只有一个唯一约束」时等价。新增表若有两个以上唯一约束，MySQL 下 `ON DUPLICATE KEY` 会命中非目标约束 | 静默写错行。三个现有调用点都已核对：`documents` 仅 `(solution_id, kind)`、`chat_sessions` 仅 `ux_chat_sessions_thread`、`system_config` 仅主键 |
| **C-D2** | 布尔列一律登记 `BOOLEAN_COLUMNS`；`is_` / `has_` 命名不作为判据 | 类型归一漏一项 → 幻影迁移。`test_schema_parity` 已在守这条 |
| **C-D3** | 时间列只允许两种形态：业务表 26 字符无 `Z`、`app_logs.ts` 24 字符带 `Z`。**`+00:00Z` 零容忍** | 勘察已捞出 16 行脏格式；转 `DATETIME` 时解析失败 |
| **C-D4** | 结构对拍必须在两种库上都能跑（SQLite 现状 + 将来的目标库），不接受「只在 SQLite 上成立」的实现 | D6 白做 |
| **C-D5** | 搜索的大小写行为必须显式（`lower()` 或方言 `ILIKE`），不依赖库默认 | 换库后「搜 admin 搜不到 Admin」，被当成业务 bug 报上来 |
| **C-D6** | 业务不变量（「至多一个 running」「额度不超过上限」）不能只靠「单写者」保证。换库前每条都必须落成唯一约束、条件更新或显式锁 | 并发下静默出现两条 `running`、额度被击穿。这类错不报错，只在对账时才发现 |

### 4.3 契约的落点

- 完整论证在附录 A（方言台账）与附录 B（库结构实测）；分层守卫 C1~C4 与 60KB 闸门在 [`docs/agents/conventions.md`](../agents/conventions.md) §2。路径即承诺是普通纪律：文档写出的每个路径都要看得见（[`../../AGENTS.md`](../../AGENTS.md) 纪律 4，复核方法见 `docs/agents/conventions.md` §1），写完逐条 `Test-Path` + `git status` 复核。
- 改探针或排空，必须同步改本文 §3.3 / §3.6 与 §4.1 的对应契约行；**一次改动要收口**：代码、架构文档、路线图三者同步，谁都不滞后。

---

## 5. 取舍

### 5.1 接受的风险（v9 §8.1）

| 风险 | 为什么接受 | 缓解 |
| --- | --- | --- |
| 单副本 → 重启必然中断在途任务 | 多副本要先外置 `_RUNTIMES` / `JobBus`，那是另一版的工作 | 排空 + `stop_grace_period` 大于 30 秒（C-V9-8） |
| 命名卷不能直接双击查看 | 它是 SQLite 在 Windows 上不坏的**唯一**做法 | 备份产物落宿主目录，可见性由它补上 |
| 镜像体积（含 langchain 全家桶） | 多阶段构建已把构建工具剥掉 | 只装 `--no-dev`；必要时按协议拆镜像 |
| 容器内时区是 UTC | 项目本来就按 UTC 存（`backend/src/backend/core/clock.py`） | 显式 `TZ=UTC`，不继承宿主机 |
| 就绪探针不覆盖「模型供应商配置是否可用」 | 依赖外部网络，探针高频触发会烧额度、会误报 | 留给 `/health` 三项体检与人工判断 |
| 容器冒烟不跑业务链路 | 登录 → 生成的端到端在 CI 上不稳定（真模型调用） | 冒烟覆盖「可达」，业务链路由既有用例守 |

### 5.2 否决的方案（v9 §8.2）

| 方案 | 否决理由 |
| --- | --- |
| 单容器塞 Nginx + uvicorn（supervisord） | 重启 Nginx 会杀掉生成任务；两个进程的失败语义不同 |
| 三个容器（再加一个 DB 容器） | 数据库固定是 SQLite 文件库，独立 DB 容器没有对象 |
| 换数据库后端 | 维护者已决定固定 SQLite：没有第二个部署形态要支持，换库要连迁移链、结构对拍与备份链一起重做，收益不抵成本 |
| Windows 专用 compose + 宿主目录挂载 | bind mount 在 Windows 上是语义问题不是配置问题；两份编排会让「哪些目录要持久化」写两遍 |
| 技能包挂载进容器 | 让「镜像与技能包版本不一致」成为可能，而这正是就绪探针要保证的事 |
| 用 `/health` 当 `healthcheck` | 恒 200，等于没配；技能包缺失时会把还能用的容器反复踢掉 |
| 自动生成主密钥 | 换机时旧密文永久解不开，且没人会发现 |
| 容器里跑两个前端 dev server / 热重载 | 开发体验不如本机；容器只承载部署形态 |
| 系统级 `sqlite3` CLI 做备份 | 多一个「目标机上装没装」的变量；用项目自己的 venv，脚本还能顺手做自检 |

### 5.3 风险登记

| 风险 | 缓解 |
| --- | --- |
| 排空窗口不够长，任务被硬杀 | `stop_grace_period` 大于 30 秒；实测一条真实长任务校准 |
| 就绪探针漏挂白名单 → 容器永远不健康 | 探针端点与白名单在同一个提交里改，用例钉住「未登录访问返回 200/503 而不是 401」 |
| 主密钥路径没进进程环境 | 启动即失败并给出下一步命令（C-V9-6），而不是降级到「未配置密钥」继续跑 |
| 卷被 `docker compose down -v` 删掉 | 文档把 `-v` 标为危险命令；备份产物在宿主目录，不在卷里 |

### 5.4 拆分依据：取消 1200 行阈值，改以接缝为依据

**取消单文件 >1200 行阈值、改以接缝为依据，取代 v8 附录 A §5 的 W1 口径。** 文件边界按「对外 / 内部」的**接缝**划分：包入口只露正常动作，包内互引私有名合法；行数不是判据，接缝才是。七个实现文件归入同一内部包即可，不必合并回单个大文件。原文「拆不动的就不拆——这条优先于『压到 1200 以内』」的让步随之作废，因为它预设了行数目标存在。`backend/tests/test_layering.py` 里 `_CONTRACT_LINE_LIMIT = 1200` 是 `api/schemas` 契约模型包既有的活跃护栏，管的是「拆完之后别又长回一行」，与「拿行数当拆分依据」是两回事，本轮不动它。

### 5.5 清理判据（三条同时成立才动）

1. 没有任何代码读它（`grep` 可证，不是「我觉得没人用」）；
2. 删掉它不会让用例变红；
3. 删掉它不需要改业务逻辑（需要改逻辑的属于重构，不属于清理）。

「被文档说是死的」不算证据；每次删除前自己 `grep` 一次。反向守卫在 `backend/tests/test_cleanup_dead_code.py`：它用 `assert not hasattr` 守住「名字回来就红」，将来真需要同名能力时连守卫对应的一行一起改，而不是把守卫注释掉。

### 5.6 数字与基线口径

文档里的**数字先复核再写**，对外动作与名字数量、入口模式同步点数量一律以实测为准，不沿用旧报告里的约数；**用例基线现取现算**（`cd backend && uv run pytest --collect-only -q`），连旧文档里「文本计数 / 参数化展开差额」整段一起重算。入口模式按本轮实测收口：13 个核心标识符在 `frontend/src/` 下 #44 时点实测 **154 行 / 23 文件**、#49 终检复测 **199 行 / 23 文件**；四入口字面量在 `frontend/src/__tests__` 下 #44 时点实测 **151 行 / 13 文件**、#49 终检复测 **213 行 / 13 文件**（两轮文件数一致）；新增快照字段的同步点实测**至少 8 处**。这些数字只作改写登记表前后的对照，新形态以 §5.8 的「一行一个入口」为准。**加列不是单向门；单向门指删列 / 退役列**，回滚靠备份库文件、不设兼容期。全流程离线、零 LLM Key。本轮终检实测口径见 [`docs/agents/conventions.md`](../agents/conventions.md) §10。

### 5.7 生成任务收口后的取舍

- **任务单不落库、重启中断**：**造单**是公开动作，产出无 id、不可变的任务单，不落库、不顶替旧任务、不发事件；**登记**才在一个事务里落库并当场查「方案是否仍存在」。运行态仍以内存为权威，单副本重启必然中断在途任务，数据库行只是不可变数据的完整投影，不保证续跑（见 §2.2 / §5.1）。新增列属于**加列**，不是删列 / 退役列那种单向门。
- **深还原 `thaw_object` 的边界**：任务单内部深冻结以保持不可变；跨到外部 API（事件流、DB 投影、chat 输入轮次）时统一经 `thaw_object` 递归还原成普通 dict / list，不用浅 `dict()` 回退。嵌套 `pending_items.sections` 与 `chat_tool_result.answers` 因此在提示词、库行与落库轮次里都保持原结构。
- **对外形状按实测收口**：`job_service.__all__` 实测 9 个正常动作，新三步与「等待在途」各有自己的公开面；再导出块删空、不设旧名兼容期，排空留在包外。数字口径不沿用旧报告的约数（见 §5.6）。
- **守卫落在行为接缝上**：反向守卫守住「死符号与旧入口不许回来」，分层守卫守住既有契约模型行数护栏，终检守卫守住「文档写出的路径可见、无悬空引用」。测试只跨「造单 → 登记 → 派发 → 订阅」主接缝断言外部可观察结果。

### 5.8 入口模式收成一张登记表

**目标**：四个**入口模式**的资料以**一张登记表**为唯一真源，**一行一个入口**，**七类资料为列**；原来散在各处的按入口写死改成「查这一行」，加第 5 个入口就是加一行。产品用户看到的四个入口、文案、交互与三份**文档**的产出能力保持原样。结论写在本节，不另立架构决策档案；本轮票序与依赖见 [`roadmap.md`](./roadmap.md) §1。

**范围**：登记表**只收随入口模式变化的事实**，七类资料为列：① 键、组别与卡片文案，② 选中后的落页（含改需求与返回跳哪页），③ 进度条步骤，④ 三个布尔（录入页是否在进度条上 / 是否需需求澄清 / 是否需审核改写），⑤ 完成所需**文档**，⑥ 文案（列表里的模式名、返回与改需求两个按钮的文案），⑦ 录入字段（字段名、默认值、算不算「有内容」、落库映射到哪一列）。**本期不做**：14 个**视图状态** → 进度格的全局映射（`STEP_OF_STATE` / `STEPS`）、任务种类 → 生成中状态的映射、按视图状态的阶段名与四条全局降级、方案标题的固定回落链（`frontend/src/stores/solutions.ts` 的 `computeTitle()`，不随入口分叉）、`useJob.ts` 整份、按视图状态选渲染组件的链条，一律原地不动；**后端**、数据库结构、部署形态、界面文案、**界面交互**与产品能力一律不动；不做构建期代码生成，运行时读这一行；不做「加一个入口只改一行」的自动化证明，由完整性校验与评审兜住。侧栏把模式名显示出来属于界面变更，另开票，本期不做（实况见 [`README.md`](./README.md) §4 第 18 行）。

**设计**：前端纯函数层的「流程阶段机」是唯一答问者，登记表是它的内部实现；对外只答「这个入口在此场景给什么」：落哪页、画哪几步、要不要澄清、要不要审核、完成要哪几份、列表叫什么、返回写什么、录入哪些字段、刷新存哪些。入口页要「有哪些行」时枚举 `ENTRY_REGISTRY` 的键，每行的取值仍逐条走 `entrySpecFor`，不直接读列；工作流状态存储、方案存储、文档流水线 composable、应用外壳与入口选择页都向它问，规则里不出现入口字面量。七类资料按列写全，一行填齐才算一个入口；规则只在此处判一次，不在每个调用点各判一次。

**契约**：**入口模式**可为「未选择」——用户停在入口页、或老**方案**没记入口名，都归一成这个合法状态，照常显示四张卡、照常打开与编辑保存，并按完整流程给出展示口径；前端用空值、后端契约用空串，后端契约不变。未知入口键、登记表缺行、某列缺失，都是程序员漏干活：在**问询那一刻明确失败**，不返回任何默认值，失败转成「该块界面不可用」而不是整页白屏。测试只跨两条公开接缝：**应用外壳集成接缝**（真 router + 点四张入口卡 + 只断言屏幕上看得见的结果）与**登记表完整性接缝**（喂一份只填一半的假第 5 入口，断言当场被拒）；不扫源码、不数落点、不查标识符词频。

**取舍**：选「运行时读一张登记表」而不选「各处继续写死 + 人工同步清单」：后者漏改不报错、只悄悄错位，历史上真加第 4 个入口那次提交动了 33 个文件；前者把漏配从静默行为变成当场失败，人工同步清单随之整段退役，约定文档改描述这张表（见 [`docs/agents/conventions.md`](../agents/conventions.md) §6）。选「只收随入口变化的事实」而不选「把五套互逆坐标一起重排」：14 个视图状态 → 进度格等映射是全流程共用、不随入口模式变化，搬进表里会把全局语义错按入口分叉。选「结论写进现状架构文档的取舍节」而不选新立 ADR：这是实施取舍，与路线图、约定文档和时间线真源一次收口即可追溯。已复核的数字校正与旧口径作废记录留在时间线真源 [`README.md`](./README.md) §4，不在本节重复。

### 5.9 本次文档重组的决策

被否决的「保留按层演进史」及理由写在 [`docs/adr/0001-architecture-docs-consolidation.md`](../adr/0001-architecture-docs-consolidation.md)；决策档案编号从 0001 重新开始，旧 `ADR-001~099` 只在 git 历史里。

---

## 6. 九层摘要表（演进索引）

> 只写主题与关键取舍；阶段规模、界碑与失真清单在 [`README.md`](./README.md)（时间线真源），此处不重复口径。九层目录已删除，这里的九行是唯一的演进索引。

| 层 | 主题 | 关键取舍 |
| --- | --- | --- |
| **v1** | 一次性流程原型 | 只验证「一段需求能否生成三份文档」；会话状态在内存 dict、测试列为范围外，不为长期使用做准备 |
| **v2** | 能用（另起炉灶重写） | 账号 / 持久化 / UI 体系 / 入口与生成质量先做，护栏（CI、错误契约、流式去重）最后补；`AppV2` 取代单体 `App.vue` |
| **v3** | 放心用 | 长文续传与自愈靠生成任务骨架；观测与预算走环境计量器，业务层调用方式不变；技能包成为管线知识唯一来源 |
| **v4** | 平台化运营（管理端） | 管理员身份由服务端裁决、前端门禁只是体验；日志可查，但审计日志有意不随用户删除级联 |
| **v5** | 测试重构 | 用例数不是指标、能抓 bug 才算；共享状态治理与 SSE 分帧唯一实现收口 |
| **v6** | 数据层重构（SQLModel） | 手写 `sqlite3` + 自建连接池换成 SQLModel + Alembic；环形外键靠「先空指针插行再回填」 |
| **v7** | 部署与上线 | 单域名 + 路径前缀、单 worker、`VACUUM INTO` 备份、回滚 = 恢复备份而非反向迁移；恢复演练是验收项（附录 C） |
| **v8** | 方言收敛与清理 | 品牌感知收进 `core/dialect.py` 一个模块；结构对拍保留 `PRAGMA`（实测否决 inspector）；删列不可逆、靠备份 |
| **v9** | 容器化 | 两个镜像 + 三个命名卷 + 两个探针 + 一次可预期的排空；固定 SQLite 单后端，否决宿主目录挂载与多副本 |

---

## 附录索引

以下三段是承重专题，按原文整段搬入，不留在 git 历史里当唯一依据：附录 A（v8 附录 B，方言可移植性台账与 C-D1~C-D6）、附录 B（v8 附录 D，库结构勘察实测数据与列长度取值来源）、附录 C（v7 §8.5，恢复演练要求）。专题里出现的实施期用例基线数字属当时口径，现行基线现取现算（见 §5.6）。

---

## 附录 A：方言可移植性台账与 C-D1~C-D6

> 正文由 v8 附录 B 原文逐字移入，仅标题降两级以适应附录层级；内部交叉引用已改指本文对应附录。
>
> **曾计划 MySQL、后评估 PostgreSQL，最终决定固定 SQLite。**

> 主题：让「这个项目跑在 SQLite 上」这件事只出现在一个文件里。
> 本文只写**设计与契约**，不含实施记录；实测数据见本文附录 B。
> 采集方式：`uv run python backend/scripts/schema_survey.py --out /tmp/schema-survey.md`（一次性产物不入库）

---

#### 1 目标

三件事，按优先级：

1. **收敛**：`api/` / `services/` / `repositories/` 三层不再出现任何数据库品牌的痕迹。换库时只改一个模块，不改业务代码。
2. **前置声明**：模型层把"只对某个库成立"的约束写成显式声明（`with_variant`、collation），而不是散在实现里、靠人记。
3. **保住主路径**：收敛过程对当前 SQLite 部署**零行为变化**。任何会让 975 例测试变红、或让现有库结构对拍报漂移的改动，都不进本期。

##### 为什么要做，什么时候做

做这件事的理由**不是**"将来要换 MySQL"，而是它顺带解决两个既存问题：

- `upsert` 的方言 import 散在 3 个仓库里，每个新仓库都要自己记得"用 `sqlite_insert` 而不是 `insert`"，漏了会在运行时才炸；
- `schema_snapshot.py` 那 300 行 `PRAGMA` 实现让"结构对拍"本身依赖 SQLite，而它恰恰是**判断迁移是否安全**的工具——工具和被检查对象用了同一个数据库的私有接口，等于没有独立验证。

**本期不选目标库。** 方案的可移植性只到"接口干净"为止，不做"三库都能跑"的承诺——那需要 CI 矩阵，见第 5 节取舍。

---

#### 2 范围

##### 本期做

| # | 内容 | 触碰文件 | 行为变化 |
|---|---|---|---|
| D1 | 三处 upsert 收敛到 `core/dialect.py` | 新建 1 个 + 改 3 个仓库 | 无 |
| D2 | 库地址、空库判定去 SQLite 特化 | `core/schema.py` | 无 |
| D3 | 大文本列声明 `MEDIUMTEXT` 变体 | `core/models.py` | 无（SQLite 侧仍是 `TEXT`） |
| D4 | 用户名唯一性声明大小写敏感 collation | `core/models.py` | 无（SQLite 侧本来就是敏感） |
| D5 | 按天统计的时区换算搬进应用层 | `repositories/admin_repo.py` | **有**（统计学口径不变，但实现换了，必须对拍） |
| D6 | ~~结构对拍改用 inspector~~ **实测否决**（见 3.5） | — | 不变 |

D1~D4 是纯粹的"搬运"，可以按批做、每批跑全量；D5、D6 各自单独一轮，理由是它们引入了新的等价性证明缺口。

##### 本期不做

- **不改任何列的类型**（`Text` → `String(n)`）。理由见 3.3：这会改变 SQLite 的建表语句，让结构对拍从"全绿"变成"全红"，属于换库那次的活。
- **不引入目标库**、不写 docker-compose 的 db 服务、不搭 CI 矩阵。
- **不做数据迁移 ETL**。`upsert` 收敛与 ETL 没有依赖关系，后者只在真换库时才有对象。
- **不动 `engine._on_connect` 的三条 PRAGMA**。它无法被收敛——连接建立时要执行什么 SQL，只能按方言分支。它属于"永久适配点"，归在 3.2 的边界里说明，不算遗漏。
- **不改时间列的存储格式**。业务表 26 字符无 `Z`、日志表 24 字符带 `Z`，这个双轨是承重设计，本期一个字都不动。
- **不处理语义差异**（`LIKE` 大小写、`ORDER BY` 排序、事务隔离）。它们不需要"收敛"，需要的是"到了目标库上重新审一遍"。本期只把它们列进 3.6，让换库那次的清单是完整的——**清单不完整比清单难做更危险**。

---

#### 3 设计

##### 3.1 三层边界

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

##### 3.2 收敛点：`core/dialect.py`

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

##### 3.3 模型层：提前声明，而不是提前改

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

##### 3.4 算法换位：把时区换算搬出 SQL

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

##### 3.5 结构对拍：**实测否决**，保留 `PRAGMA` 实现

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

##### 3.6 不收敛清单（完整）

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

###### 事务隔离：唯一"不报错、只在并发时偶发"的一类

SQLite 是**单写者**：同一时刻只有一个事务能写，序列化由数据库保证。所以这个项目里所有"先查、再判断、然后写"的路径，**从来没被并发检验过**——它们之所以正确，是因为写锁替它们排了队。

换成多写者之后，同样的代码会暴露。全项目 `with_for_update`、`isolation_level` 各 0 处，说明没有任何显式加锁。要审的路径：

| 路径 | 依赖的不变量 | 换库后的风险 |
|---|---|---|
| `generation_job_repo.supersede_active` + 紧接着的建任务 | 「同 owner + solution + kind 至多一个 running」 | 两个并发请求可能各自把旧任务置 `failed`、然后各插一条 `running`。不变量由应用层的两步操作维持，不是数据库约束 |
| `quota_service` 的额度检查 | 「今日任务数/`token` 数不超过上限」 | 先算已用量再放行，两个并发请求会都看到"还没超" |
| `chat_turn_repo.write` | 「同 job 同 role 至多一条」 | **已防御**：SAVEPOINT + 撞唯一索引回查，逻辑本身与库无关 |
| `document_repo._ensure_document` | 「同 solution 同 kind 至多一行」 | **已防御**：走 upsert，唯一约束兜底 |

所以真正裸露的是前两条。它们在单 worker 下也很难触发（并发度低），但"很难触发"和"不会触发"是两件事——MySQL 的默认隔离级别是 `REPEATABLE READ`，还会引入间隙锁与死锁（1213），后者需要重试包装。

###### `LIKE` 的大小写

| 库 | `LIKE '%admin%'` 能否匹配 `Admin` |
|---|---|
| SQLite | 能（ASCII 默认不区分；`case_sensitive_like` 默认为关） |
| MySQL | 能（取决于 collation，`ai_ci` 不区分） |
| PostgreSQL | **不能**（`LIKE` 区分大小写，需要 `ILIKE`） |

四处 `LIKE` 里有两处是**用户可见的搜索**（管理端按用户名搜任务、按关键字搜日志）。换到 PG 之后，"搜 admin 搜不到 Admin"会被当成 bug 报上来，而代码没改过一个字。

统一的做法是显式化：搜索一律 `lower(col).like(lower(pattern))`，或者按方言映射成 `ILIKE`。前者牺牲索引（两侧都要函数），后者要在 `core/dialect.py` 加一个 `ilike()`。**这属于换库那次的决定**，本期不做，但必须写进清单——因为它和 `TEXT` 索引是同一类东西：不会报错，只会让结果悄悄变。

---

#### 4 契约

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

#### 5 取舍

##### 为什么不追求"零方言"

因为右边那一列不属于写法问题：

- `TEXT` 不能整列建索引 —— MySQL 的索引键有长度上限，`TEXT` 必须给前缀长度，主键完全不行；
- `TEXT` 64KB —— 存储规格，不是语法；
- collation —— 实例与列的排序规则，影响的是"什么叫相等"。

这些只能靠换数据模型（改 `VARCHAR(n)`、改 `MEDIUMTEXT`）绕过去，而换模型的代价会落在**当前唯一真实运行的 SQLite 路径**上。为不存在的第二个部署形态付日常维护成本，是负收益。

##### 为什么分两段

第一段（D1~D6）的目标是"接口干净"，可以用现有 975 例自证；第二段（改列类型、ETL）的目标是"在某目标库上真能跑"，**没有目标库就无法验证**。把两者混在一起做，第二段的每个决策都只能靠猜，而且猜错的表现为"上线那天才炸"。

##### 否决的替代方案

| 方案 | 否决理由 |
|---|---|
| 全库把 `Text` 换成 `String(n)`，一次性通用 | 与现有 SQLite 基线直接冲突，等于立刻做一次全表迁移；且 n 依赖生产数据，本地库量不出来 |
| MySQL 的 DO NOTHING 用 `INSERT IGNORE` | 会把列超长、违反 NOT NULL 一并降级成警告，用静默失败换一行代码 |
| 在各仓库里写 `if dialect == ...` | 就是收敛前的状态，只是把 `sqlite_insert` 换成更长的分支 |
| 用 `MERGE` 语句做通用 upsert | MySQL 不支持 `MERGE`；SQLAlchemy 的 `Merge` 覆盖不全 |
| 时区聚合按小时分组后在 Python 合并 | 半小时偏移（+5:30）下整小时桶会跨本地日，统计学口径会错 |

---

#### 6 实施顺序

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

#### 7 待决

- **`users.username` 的长度上限取多少**。数据（max 6）不能作为依据，需要产品层面定：是"用户名最多 32 字"这种规则，还是保持无上限、把唯一索引改到哈希列上。这个决定必须在换库之前做掉，否则 `VARCHAR(n)` 的 n 定不下来。
- **`solutions.data` 的真实规模未知**。本地库里 4 行全是空串，无法判断它属于 `MEDIUMTEXT` 够用还是需要 `LONGTEXT`。生产库跑一次勘察即可回答。
- **3.6 的三类清单要不要各自成为换库那次的前置条件**。建议是：列长度与部分索引算"必做"，`LIKE` 与排序算"必审"，事务隔离算"必加锁"。三者的验证方式不同（前者靠结构对拍，中者靠搜索用例，后者靠并发用例），不能合成一张清单。
- **`LIKE` 的显式化时机**。改成 `lower(col).like(...)` 会让现有前缀索引失效（`AppLog.logger` 的前缀匹配会退化成全表扫）。如果决定现在做，需要同时评估 `log_repo` 的查询计划；如果不做，就等换库那次连索引一起设计。
- **本文是否拆成两份文档**（建议：一份「方言收敛点 = `core/dialect.py`」、一份「可移植性分两段，本期只做接口收敛」）。3.6 的不收敛清单可以并进后者的"范围之外"，也可以单独成一份。


---

## 附录 B：库结构勘察（prdwright.db 实测）

> 正文由 v8 附录 D 原文逐字移入，仅标题降两级以适应附录层级；各表「建议」列是当时的列长度取值来源。

- 表 14 张，共 2001 行，文件 1788 KB
- 只读打开（`mode=ro`），不存在任何写入路径

#### 1 表的规模

| 表 | 行数 |
| --- | ---: |
| `admin_audit_log` | 5 |
| `app_logs` | 1905 |
| `auth_sessions` | 1 |
| `chat_sessions` | 3 |
| `chat_turns` | 9 |
| `document_versions` | 8 |
| `documents` | 7 |
| `generation_jobs` | 54 |
| `invite_codes` | 1 |
| `login_failures` | 0 |
| `model_providers` | 1 |
| `solutions` | 4 |
| `system_config` | 2 |
| `users` | 1 |

#### 2 文本列的长度分布

`超字符阈值` 只列出**有命中**的档位。`索引` 一列是「该列是否需要能建索引」——MySQL 下 `TEXT` 列建索引必须给前缀长度，主键更是完全不行。

`max字节` 与 `超64KB` 才是 `TEXT` 够不够的依据：MySQL 的 `TEXT` 上限 64KB 按**字节**算，中文一字 3 字节——11500 字的中文 PRD 已经是 34KB，不是「才 11K」。

| 表.列 | 行 | max字 | max字节 | p99 | NULL | 超字符阈值 | 超64KB | 索引 | 建议 |
| --- | ---: | ---: | ---: | ---: | ---: | --- | ---: | --- | --- |
| `admin_audit_log.actor_name` | 5 | 6 | 6 | 6 | 0 | — | — | — | 保留 TEXT |
| `admin_audit_log.action` | 5 | 15 | 15 | 15 | 0 | — | — | — | 保留 TEXT |
| `admin_audit_log.target` | 5 | 14 | 14 | 14 | 0 | — | — | — | 保留 TEXT |
| `admin_audit_log.detail` | 5 | 75 | 85 | 75 | 0 | >64: 2 | — | — | 保留 TEXT |
| `admin_audit_log.request_id` | 5 | 32 | 32 | 32 | 0 | — | — | — | 保留 TEXT |
| `admin_audit_log.ts` | 5 | 26 | 26 | 26 | 0 | — | — | idx_admin_audit_actor、idx_admin_audit_ts | `VARCHAR(32)` |
| `app_logs.ts` | 1905 | 30 | 30 | 24 | 0 | — | — | idx_app_logs_level、idx_app_logs_ts | `VARCHAR(32)` |
| `app_logs.level` | 1905 | 7 | 7 | 4 | 0 | — | — | idx_app_logs_level | `VARCHAR(32)` |
| `app_logs.logger` | 1905 | 37 | 37 | 28 | 0 | — | — | — | 保留 TEXT |
| `app_logs.msg` | 1905 | 94 | 94 | 16 | 0 | >64: 16 | — | — | 保留 TEXT |
| `app_logs.request_id` | 1905 | 32 | 32 | 32 | 769 | — | — | idx_app_logs_request | `VARCHAR(32)` |
| `app_logs.event` | 1905 | 20 | 20 | 17 | 28 | — | — | — | 保留 TEXT |
| `app_logs.extra_json` | 1905 | 191 | 207 | 191 | 0 | >64: 1866、>128: 758 | — | — | 保留 TEXT |
| `app_logs.exc` | 1905 | 1084 | 1184 | 1084 | 1902 | >64: 3、>128: 3、>255: 3、>512: 1、>1024: 1 | — | — | 保留 TEXT |
| `auth_sessions.token_hash` | 1 | 64 | 64 | 64 | 0 | — | — | sqlite_autoindex_auth_sessions_1 | `VARCHAR(64)` |
| `auth_sessions.created_at` | 1 | 26 | 26 | 26 | 0 | — | — | — | 保留 TEXT |
| `auth_sessions.expires_at` | 1 | 26 | 26 | 26 | 0 | — | — | idx_auth_sessions_expires | `VARCHAR(32)` |
| `chat_sessions.stage` | 3 | 11 | 11 | 11 | 0 | — | — | ux_chat_sessions_thread | `VARCHAR(32)` |
| `chat_sessions.conversation_id` | 3 | 12 | 12 | 12 | 0 | — | — | — | 保留 TEXT |
| `chat_sessions.updated_at` | 3 | 26 | 26 | 26 | 0 | — | — | — | 保留 TEXT |
| `chat_turns.stage` | 9 | 11 | 11 | 11 | 0 | — | — | idx_chat_turns_thread | `VARCHAR(32)` |
| `chat_turns.role` | 9 | 9 | 9 | 9 | 0 | — | — | ux_chat_turns_job_role | `VARCHAR(32)` |
| `chat_turns.content` | 9 | 1359 | 3425 | 1359 | 0 | >64: 8、>128: 7、>255: 7、>512: 4、>1024: 4 | — | — | 保留 TEXT |
| `chat_turns.tool_call_id` | 9 | 29 | 29 | 29 | 0 | — | — | — | 保留 TEXT |
| `chat_turns.tool_result` | 9 | 160 | 208 | 160 | 0 | >64: 1、>128: 1 | — | — | 保留 TEXT |
| `chat_turns.asks` | 9 | 591 | 927 | 591 | 0 | >64: 4、>128: 4、>255: 4、>512: 2 | — | — | 保留 TEXT |
| `chat_turns.answers` | 9 | 98 | 146 | 98 | 0 | >64: 1 | — | — | 保留 TEXT |
| `chat_turns.tool_calls` | 9 | 597 | 914 | 597 | 0 | >64: 4、>128: 4、>255: 4、>512: 2 | — | — | 保留 TEXT |
| `chat_turns.created_at` | 9 | 26 | 26 | 26 | 0 | — | — | — | 保留 TEXT |
| `document_versions.content` | 8 | 11566 | 24534 | 11566 | 0 | >64: 8、>128: 8、>255: 8、>512: 8、>1024: 8、>4096: 3 | — | — | 保留 TEXT |
| `document_versions.content_sha` | 8 | 12 | 12 | 12 | 0 | — | — | — | 保留 TEXT |
| `document_versions.pending_items` | 8 | 3724 | 5276 | 3724 | 0 | >64: 6、>128: 6、>255: 6、>512: 6、>1024: 6 | — | — | 保留 TEXT |
| `document_versions.last_run_summary` | 8 | 839 | 839 | 839 | 0 | >64: 8、>128: 8、>255: 8、>512: 8 | — | — | 保留 TEXT |
| `document_versions.source` | 8 | 8 | 8 | 8 | 0 | — | — | — | 保留 TEXT |
| `document_versions.note` | 8 | 0 | 0 | 0 | 0 | — | — | — | 保留 TEXT |
| `document_versions.metadata` | 8 | 75 | 75 | 75 | 0 | >64: 3 | — | — | 保留 TEXT |
| `document_versions.created_at` | 8 | 26 | 26 | 26 | 0 | — | — | — | 保留 TEXT |
| `document_versions.updated_at` | 8 | 26 | 26 | 26 | 0 | — | — | — | 保留 TEXT |
| `documents.kind` | 7 | 8 | 8 | 8 | 0 | — | — | sqlite_autoindex_documents_1 | `VARCHAR(32)` |
| `documents.created_at` | 7 | 26 | 26 | 26 | 0 | — | — | — | 保留 TEXT |
| `documents.updated_at` | 7 | 26 | 26 | 26 | 0 | — | — | — | 保留 TEXT |
| `generation_jobs.kind` | 54 | 12 | 12 | 12 | 0 | — | — | idx_generation_jobs_recover | `VARCHAR(32)` |
| `generation_jobs.status` | 54 | 9 | 9 | 9 | 0 | — | — | idx_generation_jobs_recover | `VARCHAR(32)` |
| `generation_jobs.phase` | 54 | 9 | 9 | 9 | 0 | — | — | — | 保留 TEXT |
| `generation_jobs.draft_content` | 54 | 26957 | 36919 | 26957 | 0 | >64: 53、>128: 52、>255: 52、>512: 38、>1024: 36、>4096: 20、>16384: 4 | — | — | 保留 TEXT |
| `generation_jobs.completed_sections` | 54 | 7125 | 15777 | 7125 | 0 | >64: 13、>128: 13、>255: 13、>512: 13、>1024: 13、>4096: 4 | — | — | 保留 TEXT |
| `generation_jobs.input_fingerprint` | 54 | 64 | 64 | 64 | 41 | — | — | — | 保留 TEXT |
| `generation_jobs.usage` | 54 | 843 | 843 | 843 | 2 | >64: 52、>128: 52、>255: 52、>512: 31 | — | — | 保留 TEXT |
| `generation_jobs.error_code` | 54 | 18 | 18 | 18 | 0 | — | — | — | 保留 TEXT |
| `generation_jobs.error_message` | 54 | 55 | 165 | 55 | 0 | — | — | — | 保留 TEXT |
| `generation_jobs.created_at` | 54 | 26 | 26 | 26 | 0 | — | — | idx_generation_jobs_owner | `VARCHAR(32)` |
| `generation_jobs.updated_at` | 54 | 26 | 26 | 26 | 0 | — | — | — | 保留 TEXT |
| `generation_jobs.pending_items` | 54 | 3743 | 5351 | 3743 | 0 | >64: 23、>128: 23、>255: 23、>512: 23、>1024: 18 | — | — | 保留 TEXT |
| `generation_jobs.doc_kind` | 54 | 8 | 8 | 8 | 0 | — | — | — | 保留 TEXT |
| `generation_jobs.chat_stage` | 54 | 11 | 11 | 11 | 0 | — | — | — | 保留 TEXT |
| `generation_jobs.provider_protocol` | 54 | 3 | 3 | 3 | 0 | — | — | — | 保留 TEXT |
| `generation_jobs.provider_config_id` | 54 | 7 | 7 | 7 | 0 | — | — | — | 保留 TEXT |
| `generation_jobs.provider_name` | 54 | 4 | 12 | 4 | 0 | — | — | — | 保留 TEXT |
| `generation_jobs.provider_model` | 54 | 28 | 28 | 28 | 0 | — | — | — | 保留 TEXT |
| `invite_codes.code` | 1 | 6 | 6 | 6 | 0 | — | — | sqlite_autoindex_invite_codes_1 | `VARCHAR(32)` |
| `invite_codes.expires_at` | 1 | 0 | 0 | 0 | 1 | — | — | — | 保留 TEXT |
| `invite_codes.created_at` | 1 | 26 | 26 | 26 | 0 | — | — | — | 保留 TEXT |
| `invite_codes.note` | 1 | 25 | 49 | 25 | 0 | — | — | — | 保留 TEXT |
| `model_providers.id` | 1 | 14 | 14 | 14 | 0 | — | — | sqlite_autoindex_model_providers_1 | `VARCHAR(32)` |
| `model_providers.name` | 1 | 4 | 12 | 4 | 0 | — | — | — | 保留 TEXT |
| `model_providers.protocol` | 1 | 3 | 3 | 3 | 0 | — | — | — | 保留 TEXT |
| `model_providers.base_url` | 1 | 40 | 40 | 40 | 0 | — | — | — | 保留 TEXT |
| `model_providers.default_model` | 1 | 28 | 28 | 28 | 0 | — | — | — | 保留 TEXT |
| `model_providers.extra_json` | 1 | 2 | 2 | 2 | 0 | — | — | — | 保留 TEXT |
| `model_providers.created_at` | 1 | 26 | 26 | 26 | 0 | — | — | — | 保留 TEXT |
| `model_providers.updated_at` | 1 | 26 | 26 | 26 | 0 | — | — | — | 保留 TEXT |
| `model_providers.value_token` | 1 | 140 | 140 | 140 | 0 | >64: 1、>128: 1 | — | — | 保留 TEXT |
| `solutions.title` | 4 | 9 | 9 | 9 | 0 | — | — | — | 保留 TEXT |
| `solutions.mode` | 4 | 10 | 10 | 10 | 0 | — | — | — | 保留 TEXT |
| `solutions.stage` | 4 | 15 | 15 | 15 | 0 | — | — | — | 保留 TEXT |
| `solutions.data` | 4 | 0 | 0 | 0 | 0 | — | — | — | 保留 TEXT |
| `solutions.created_at` | 4 | 26 | 26 | 26 | 0 | — | — | — | 保留 TEXT |
| `solutions.updated_at` | 4 | 26 | 26 | 26 | 0 | — | — | idx_solutions_owner | `VARCHAR(32)` |
| `solutions.content_updated_at` | 4 | 26 | 26 | 26 | 0 | — | — | idx_solutions_owner_content | `VARCHAR(32)` |
| `solutions.view_state` | 4 | 15 | 15 | 15 | 0 | — | — | — | 保留 TEXT |
| `solutions.entry_mode` | 4 | 10 | 10 | 10 | 0 | — | — | — | 保留 TEXT |
| `solutions.chat_tab` | 4 | 11 | 11 | 11 | 0 | — | — | — | 保留 TEXT |
| `solutions.summary_json` | 4 | 1166 | 1800 | 1166 | 0 | >64: 4、>128: 4、>255: 4、>512: 4、>1024: 4 | — | — | 保留 TEXT |
| `solutions.quick_product_name` | 4 | 0 | 0 | 0 | 0 | — | — | — | 保留 TEXT |
| `solutions.quick_requirements` | 4 | 0 | 0 | 0 | 0 | — | — | — | 保留 TEXT |
| `solutions.draft_prd_text` | 4 | 0 | 0 | 0 | 0 | — | — | — | 保留 TEXT |
| `solutions.paste_prd_text` | 4 | 0 | 0 | 0 | 0 | — | — | — | 保留 TEXT |
| `solutions.paste_api_docs_text` | 4 | 0 | 0 | 0 | 0 | — | — | — | 保留 TEXT |
| `solutions.active_job_kind` | 4 | 0 | 0 | 0 | 0 | — | — | — | 保留 TEXT |
| `solutions.active_job_doc_kind` | 4 | 0 | 0 | 0 | 0 | — | — | — | 保留 TEXT |
| `system_config.key` | 2 | 24 | 24 | 24 | 0 | — | — | sqlite_autoindex_system_config_1 | `VARCHAR(32)` |
| `system_config.value` | 2 | 16 | 16 | 16 | 0 | — | — | — | 保留 TEXT |
| `system_config.updated_at` | 2 | 26 | 26 | 26 | 0 | — | — | — | 保留 TEXT |
| `users.username` | 1 | 6 | 6 | 6 | 0 | — | — | sqlite_autoindex_users_1 | `VARCHAR(32)` |
| `users.password_hash` | 1 | 128 | 128 | 128 | 0 | >64: 1 | — | — | 保留 TEXT |
| `users.created_at` | 1 | 26 | 26 | 26 | 0 | — | — | — | 保留 TEXT |

#### 3 唯一约束的重复检查

SQLite 这边约束**已经生效**（`PRAGMA foreign_keys=ON`），所以正常情况下不该有重复。真正要检查的是 **`fold` 一列**：换库后 MySQL 默认 collation 不区分大小写，`Admin` 与 `admin` 会变成同一个键。

| 表 | 约束 | 列 | 精确重复 | 忽略大小写重复 |
| --- | --- | --- | ---: | ---: |
| `auth_sessions` | sqlite_autoindex_auth_sessions_1 | token_hash | 0 | 同左 |
| `chat_sessions` | ux_chat_sessions_thread | solution_id, stage | 0 | 同左 |
| `chat_turns` | ux_chat_turns_job_role | job_id, role | 0 | 同左 |
| `document_versions` | <unique-constraint> | document_id, version_no | 0 | 同左 |
| `documents` | <unique-constraint> | solution_id, kind | 0 | 同左 |
| `invite_codes` | sqlite_autoindex_invite_codes_1 | code | 0 | 同左 |
| `model_providers` | sqlite_autoindex_model_providers_1 | id | 0 | 同左 |
| `system_config` | sqlite_autoindex_system_config_1 | key | 0 | 同左 |
| `users` | <unique-constraint> | username | 0 | 同左 |

#### 4 布尔列的取值合规性

按 `models.BOOLEAN_COLUMNS` 白名单检查，**不按数据分布猜**——`model_providers.sort_order` 在全表恰好只有 0 的时候也满足「只落在 0/1」。这张表回答的是：换库后 `BOOLEAN` ↔ `TINYINT(1)` 的映射会不会丢掉非法值。

| 表.列 | 实测取值 | 合规 |
| --- | --- | --- |
| `chat_turns.interrupted` | 0 | 是 |
| `model_providers.thinking_enabled` | 0 | 是 |
| `model_providers.builtin` | 0 | 是 |
| `model_providers.enabled` | 1 | 是 |
| `users.is_active` | 1 | 是 |
| `users.is_admin` | 1 | 是 |

白名单内所有布尔列的取值都在 `{0, 1, NULL}` 内。

#### 5 时间列的格式一致性

`UTC(Z)` 是 `utc_now_text()` 的规范形态。`ISO` 但**不带** Z 的行需要盯——那可能混进了本地时间，换库改成 `DATETIME` 时会错 8 小时。

| 表.列 | 行 | NULL | ISO 形态 | 带 Z | 非 Z | 脏格式 | 格式异常 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `admin_audit_log.ts` | 5 | 0 | 5 | 0 | 5 | — | — |
| `app_logs.ts` | 1905 | 0 | 1905 | 1905 | — | 16 | — |
| `auth_sessions.created_at` | 1 | 0 | 1 | 0 | 1 | — | — |
| `auth_sessions.expires_at` | 1 | 0 | 1 | 0 | 1 | — | — |
| `chat_sessions.updated_at` | 3 | 0 | 3 | 0 | 3 | — | — |
| `chat_turns.created_at` | 9 | 0 | 9 | 0 | 9 | — | — |
| `document_versions.created_at` | 8 | 0 | 8 | 0 | 8 | — | — |
| `document_versions.updated_at` | 8 | 0 | 8 | 0 | 8 | — | — |
| `documents.created_at` | 7 | 0 | 7 | 0 | 7 | — | — |
| `documents.updated_at` | 7 | 0 | 7 | 0 | 7 | — | — |
| `generation_jobs.created_at` | 54 | 0 | 54 | 0 | 54 | — | — |
| `generation_jobs.updated_at` | 54 | 0 | 54 | 0 | 54 | — | — |
| `invite_codes.expires_at` | 1 | 1 | 0 | 0 | — | — | — |
| `invite_codes.created_at` | 1 | 0 | 1 | 0 | 1 | — | — |
| `model_providers.created_at` | 1 | 0 | 1 | 0 | 1 | — | — |
| `model_providers.updated_at` | 1 | 0 | 1 | 0 | 1 | — | — |
| `solutions.created_at` | 4 | 0 | 4 | 0 | 4 | — | — |
| `solutions.updated_at` | 4 | 0 | 4 | 0 | 4 | — | — |
| `solutions.content_updated_at` | 4 | 0 | 4 | 0 | 4 | — | — |
| `system_config.updated_at` | 2 | 0 | 2 | 0 | 2 | — | — |
| `users.created_at` | 1 | 0 | 1 | 0 | 1 | — | — |

所有时间列都形如 `YYYY-MM-DDTHH:MM:SS…`，可安全转 `DATETIME`。

**脏格式（`+00:00Z`）**：`app_logs.ts`（16 行）——`DATETIME` 解析会直接失败，必须在迁移脚本里剥掉偏移再补 `Z`。

#### 6 由数据推出的候选长度

| 表.列 | 实测 max字 | 实测 max字节 | 建议档位 |
| --- | ---: | ---: | --- |
| `admin_audit_log.ts` | 26 | 26 | `VARCHAR(32)` |
| `app_logs.ts` | 30 | 30 | `VARCHAR(32)` |
| `app_logs.level` | 7 | 7 | `VARCHAR(32)` |
| `app_logs.request_id` | 32 | 32 | `VARCHAR(32)` |
| `auth_sessions.token_hash` | 64 | 64 | `VARCHAR(64)` |
| `auth_sessions.expires_at` | 26 | 26 | `VARCHAR(32)` |
| `chat_sessions.stage` | 11 | 11 | `VARCHAR(32)` |
| `chat_turns.stage` | 11 | 11 | `VARCHAR(32)` |
| `chat_turns.role` | 9 | 9 | `VARCHAR(32)` |
| `documents.kind` | 8 | 8 | `VARCHAR(32)` |
| `generation_jobs.kind` | 12 | 12 | `VARCHAR(32)` |
| `generation_jobs.status` | 9 | 9 | `VARCHAR(32)` |
| `generation_jobs.created_at` | 26 | 26 | `VARCHAR(32)` |
| `invite_codes.code` | 6 | 6 | `VARCHAR(32)` |
| `model_providers.id` | 14 | 14 | `VARCHAR(32)` |
| `solutions.updated_at` | 26 | 26 | `VARCHAR(32)` |
| `solutions.content_updated_at` | 26 | 26 | `VARCHAR(32)` |
| `system_config.key` | 24 | 24 | `VARCHAR(32)` |
| `users.username` | 6 | 6 | `VARCHAR(32)` |

> 这些档位只反映**当前这个库**。生产库必须用同一份脚本再跑一次——
> `VARCHAR(n)` 的 n 小于线上最大值时，迁移当天会以 `Data too long` 失败。

#### 7 超过 16KB 的未索引文本列

这些列不建索引，所以不受 `VARCHAR` 约束；但 MySQL 的 `TEXT` 只有 64KB（字节），超过 16KB 就建议直接声明成 `MEDIUMTEXT`，别赌「还能再长一点」。

| 表.列 | max字 | max字节 | 建议 |
| --- | ---: | ---: | --- |
| `document_versions.content` | 11566 | 24534 | `MEDIUMTEXT` |
| `generation_jobs.draft_content` | 26957 | 36919 | `MEDIUMTEXT` |


---

## 附录 C：恢复演练要求（v7 §8.5 原文整段搬入）

> 正文由 v7 §8.5 及其前置结论逐字移入；「T17.6」是当时的排期编号，现行落点见 `docs/architecture/roadmap.md` 与 `deploy/backup.py`。

### 前置结论（v7 §8.4 末）

**回滚 = 恢复备份，不是跑反向迁移。**

补充一条 v7 新增的推论：**恢复之后第一次启动会再跑一次 `upgrade head`。**

| 备份里的 `alembic_version` | 恢复后的第一次启动 | 说明 |
| --- | --- | --- |
| 等于当前代码 head | `upgrade` 跑空 | 正常 |
| 比代码旧 | 自动补到 head | 正常，这正是迁移的用处 |
| **比代码新**（备份来自更新的版本） | 启动失败（Alembic 找不到对应 revision） | 说明「代码退得比数据还早」。必须先升代码，或接受用不上这份备份 |

所以**恢复演练必须用当前代码跑**，而不是「用备份那天的代码跑一遍」——后者验证的是别的东西。


### 8.5 恢复演练是验收项，不是可选项


「有备份」和「能恢复」是两件事。T17.6 的验收必须包含一次**完整演练**：

1. 停掉生产实例（或换一个空闲端口）；
2. 把一份备份恢复到临时库路径，权限设成 `prdwright`；
3. 用**当前代码**起一个只监听回环的实例（`DB_PATH` 指向临时库）；
4. `/health` 三项全绿、能登录、能看到方案列表、能打开一份文档的版本抽屉。

没演练过的备份，在真正需要它的那天只是个文件。

