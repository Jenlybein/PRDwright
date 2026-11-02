# v8 架构：容器化与方言收敛

> 方言收敛的完整论证见附录 B；本文只写它与部署形态的交界面。
> 勘察数据见附录 D。决策依据见本层「取舍」一节与 issue 历史。

---

## 1 目标

两件事，彼此独立，但共用同一次发布窗口：

1. **容器化**：把 v7 的「裸机 + systemd + Nginx + 手写四目录」换成一个
   `docker compose up -d` 就能起来的形态，且**数据不丢、回滚可做**。
2. **方言收敛**：让「本项目的数据库是 SQLite」只出现在 `core/dialect.py`
   与 `core/engine.py` 两处；业务三层不再出现任何数据库品牌。

### 容器化要解决的三个具体问题

不是"为了用 Docker"，而是 v7 实操中真实卡住的三件事：

| 问题 | v7 的表现 | v8 的解法 |
|---|---|---|
| 目标机要装 Python 3.12 + uv + Node 22 + Nginx + systemd unit | 一台机器一套手工步骤，换机器就得重来 | 运行时依赖全部固化进镜像，目标机只需要 Docker |
| `sudo uv sync` 建出的 venv 属主是 root，服务起不来（203/EXEC） | 已踩过，且修法依赖"记得用 `sudo -u deploy`" | 镜像内构建，运行用户是固定的非 root，不存在属主问题 |
| 前端产物靠"服务器就地构建"，发布窗口被 `npm install` 拉长 | 每次发布都要在服务器上跑两遍 `npm install` | 前端在镜像构建期完成，发布只传镜像 |

### 方言收敛要解决的两个具体问题

不是"为了将来换库"，而是现在就有代价的两件事：

- **upsert 的方言 import 散在 3 个仓库**，每个新仓库都要自己记得"用 `sqlite_insert`
  而不是 `insert`"，漏了会到运行时才炸；
- **结构对拍用 `PRAGMA` 检查 SQLite 的库结构**，工具与被检查对象共用同一个数据库的
  私有接口，`assert_schema_matches_models` 因此缺少独立证据。

---

## 2 范围

### 本期做

| # | 内容 | 性质 |
|---|---|---|
| V8-1 | 方言收敛：upsert 收进 `core/dialect.py`；库地址与空库判定去特化 | 改代码，零行为变化 |
| V8-2 | 模型层声明：4 个大文本列挂 `MEDIUMTEXT` 变体；`username` 声明大小写敏感 collation | 改代码，零行为变化 |
| V8-3 | 时区聚合搬进应用层（`admin_repo` 的按天统计） | 改代码，**需等价性对拍** |
| V8-4 | 结构对拍改用 `sqlalchemy.inspect` | 改代码，**需重新定基线** |
| V8-5 | 后端镜像（多阶段）、前端镜像（Nginx + 两个 dist） | 新增文件 |
| V8-6 | `compose.yaml`：两服务、三个命名卷、只读代码层 | 新增文件 |
| V8-7 | `/healthz`（存活）与 `/readyz`（就绪）两条新接口 | 新增接口 |
| V8-8 | 优雅关闭：`SIGTERM` 时排空在途任务 | 改代码 |
| V8-9 | 首次启动迁移、备份、回滚三份演练记录 | 文档 + 演练 |

### 本期不做

- **不换数据库**。换库的最大收益（多副本）被进程内状态锁死，
  改造代价却全落在唯一真实运行的路径上。附录 B §3.6 的台账保留，
  换库那次按它执行。
- **不做多副本 / 不做水平扩展**。`_RUNTIMES`、`JobBus`、runtime 快照、固定连接池
  都在进程内。`api` 的副本数**恒为 1**，这是契约不是建议。
- **不引入 Kubernetes / 不做 CI 自动发镜像**。目标形态是"单机 + compose"，
  与 v7 的"单台轻量服务器"是同一个规模判断。
- **不改 `/health` 的语义**。它恒返回 200、`status` 只有 `ok`/`degraded`（v7 契约）。
  新增两条探针接口，而不是改它——改语义会动到已有的监控与文档。
- **不改前端代码**。两个前端已经是相对路径（`baseURL: '/'`），容器化正好利用这一点。
- **不把数据库放进独立容器**。本期仍是 SQLite 文件，独立 DB 容器没有对象。
- **不改时间列的存储格式**（业务表无 `Z`、日志表带 `Z` 的双轨）。

---

## 3 容器形态

两个服务，一个网络，宿主机只暴露一个端口。

```text
            宿主机 :80
                │
        ┌───────▼────────┐
        │  web (Nginx)   │  静态：/  → 用户端 dist
        │                │        /admin/ → 管理端 dist
        │                │  反代：/api/ → api:8000
        └───────┬────────┘
                │ 内部网络（不 publish）
        ┌───────▼────────┐
        │  api (uvicorn) │  workers=1（硬约束）
        │                │  lifespan: prepare_schema → warmup → 接请求
        └───┬────────┬───┘
            │        │
    volume:data  volume:logs
    （SQLite）   （轮转日志）
```

**为什么不是三个容器（加 DB）**：本期数据库仍是文件库，独立 DB 容器没有对象；
一旦换库，DB 会成为第三个服务，那时 `api` 的 `depends_on` 才有意义。

**为什么不是单容器（Nginx + uvicorn 同镜像）**：两个进程的职责、重启策略、
资源画像都不同；合并后"重启 Nginx 会杀掉生成任务"。多一层的成本远低于这个耦合。

### 3.1 端口与网络

| 位置 | 端口 | 说明 |
|---|---|---|
| 宿主机 | `80`（`443` 在有证书时） | 唯一对外入口 |
| `web` 容器 | `80` | 只由宿主机映射 |
| `api` 容器 | `8000` | **只 `expose`，不 `ports`**。宿主机上不可达，只能被 `web` 访问 |

`/health` 在 v7 里是"仅本机可访问"（Nginx 只放行 `127.0.0.1`）。容器化后"本机"
变成"同网络内的 `web` 容器"，这条约束保持不变：**`api` 的 8000 端口不得 publish 到宿主机**。

### 3.2 运行用户与文件权限

- 镜像内创建 `uid=10001` 的非 root 用户，`USER` 指令切过去后再启动。
- 代码层只读（镜像内的 `/app`），运行期不写任何镜像层。
- 三个卷的属主在**首次启动时**由 entrypoint 校正（只在卷为空时执行一次）。
  这是唯一需要 root 的时刻，用 `docker-entrypoint.sh` 的 `case` 判断，不做常驻 root。

---

## 4 进程与副本

**`api` 的副本数恒为 1。** 这不是性能选择，是正确性约束：

| 进程内状态 | 位置 | 多副本会怎样 |
|---|---|---|
| `_RUNTIMES: dict[int, JobRuntime]` | `services/job_service.py` | A 副本起的任务，B 副本的 SSE 找不到 → 客户端永远等不到分片 |
| `JobBus` | 同上（订阅与广播） | 订阅方与生产方不在同一进程 → 任务永不推进 |
| runtime 快照（5 秒 TTL） | `core/runtime_config.py` | 后台改了配置，只有一半副本生效 |
| 固定连接池（`max_overflow=0`） | `core/engine.py` | 多副本 = 多条进程各自建满，SQLite 的写锁冲突概率上升 |

配套三条：

1. **`--workers 1` 写死在镜像的 `CMD` 里**，不提供环境变量覆盖——一个"看起来能调"
   的参数迟早会被调。
2. **`compose.yaml` 里不写 `deploy.replicas`**，也不提供 `--scale` 的说明文档。
3. **发布即中断**：重建容器会让在途任务消失。所以发布前必须排空（见 §6.3）。

---

## 5 卷与数据

### 5.1 三个命名卷

| 卷 | 挂载点 | 内容 | 丢了会怎样 |
|---|---|---|---|
| `prdwright-data` | `/var/lib/prdwright` | `prdwright.db`（含 WAL 的 `-wal`/`-shm`） | **业务数据全丢** |
| `prdwright-logs` | `/var/log/prdwright` | 轮转日志文件 | 丢历史日志（`app_logs` 表里还有一份，但那是可丢弃的派生数据） |
| `prdwright-secrets` | `/run/secrets`（**只读**） | `master.key` | 模型供应商密钥全部解不开（`/health` 的 `llm` 项变 `false`） |

### 5.2 为什么数据库目录**必须**是命名卷

这是 v8 唯一一条会让人在开发机上就踩到的硬约束：

- SQLite 的 WAL 模式需要在数据库文件旁创建 `-wal` 与 `-shm`，`-shm` 是 **mmap 的共享内存**；
- Docker Desktop for Windows 的 bind mount 走 grpc-fuse / 9p，不支持 SQLite 需要的
  文件锁语义与 `-shm` 的 mmap；
- 症状不是"报错说挂载不对"，而是**随机** `disk I/O error`、`database is locked`，
  或者在容器重启后看见一个"少了最近几分钟写入"的库。

所以：`./data:/var/lib/prdwright` 这种写法在 Windows 开发机上**不许用**。
必须 `prdwright-data:/var/lib/prdwright`。

### 5.3 备份

沿用 v7 的结论：`VACUUM INTO` 到同一个卷内的另一个文件，再做自检
（能打开 + `integrity_check` + 读得到 `alembic_version`）。

容器化只改一件事：备份产物要落到**宿主机可取的地方**，所以备份命令通过
`docker compose exec api python scripts/deploy/backup.py` 执行，产物写在 `data` 卷内，
再由一条 `docker cp` 拷出。**不能对卷目录直接 `cp`**，理由与 v7 相同。

---

## 6 健康检查与启停

### 6.1 两条新接口（`/health` 不动）

| 路径 | 语义 | 实现 | 谁用 |
|---|---|---|---|
| `GET /healthz` | 进程活着、事件循环没卡死 | 固定返回 `{"ok": true}`，不查库、不查配置 | 容器 `healthcheck`、编排的 liveness |
| `GET /readyz` | 能接业务请求 | 连一次数据库（`SELECT 1`）+ 读 `alembic_version` 与代码 `head` 比对 | 编排的 readiness、发布脚本 |

`/readyz` 的三条判定，任何一条不满足就返回 `503`：

1. 数据库可连接；
2. `alembic_version` 等于当前代码的 head（迁移已跑完）；
3. 连接池已预热（`engine_stats().idle == size`）。

第 3 条为什么放进 readiness：`max_overflow=0` 下"池没预热"意味着第一个并发请求会
超时，这属于"还没准备好接客"，而不是"服务挂了"。

### 6.2 为什么不直接拿 `/health`

`/health` 恒返回 200（v7 明确契约，`api/health.py` 的 docstring 写明了）。
把它配成"非 200 就重启"，会在技能包缺失、模型未配置这类**可降级**状态下
把容器反复踢掉——而那些状态恰恰是容器还能正常服务的时候。

### 6.3 优雅关闭

`SIGTERM` 到达后的顺序（由 `lifespan` 的收尾段与 `job_service` 配合完成）：

1. 停止接受新连接（uvicorn 行为）；
2. 把 `_RUNTIMES` 里所有 running 任务标记为「进程退出导致中断」，落库为
   `failed` + `SUPERSEDED`/`CANCELLED` 口径之一（沿用 v6 的 `reap_orphans` 语义）；
3. flush 日志队列（`shutdown_log_persistence()`）；
4. 退出。

`compose.yaml` 里 `stop_grace_period` 必须覆盖最长一次生成的时间。当前无法给出确切
上限（取决于模型与章节数），先取 **300 秒**并在 T18.6 用真实任务实测校准。
不足时的表现是"任务被硬杀、库里留下 running"，下次启动由 `reap_orphans` 收尾——
所以这条不是数据安全问题，是体验问题。

---

## 7 镜像与构建

### 7.1 两个镜像

| 镜像 | 基础 | 构建阶段 | 产物 |
|---|---|---|---|
| `prdwright-api` | `python:3.12-slim` | builder（`uv sync --frozen --no-dev`）→ runtime | `/app` 只读代码 + `.venv` |
| `prdwright-web` | `nginx:1.27-alpine` | node 构建两个前端 → nginx | `/usr/share/nginx/html`（`/` 与 `/admin/`） |

前端单独成镜像的原因：`npm install` 与 `uv sync` 的缓存键完全不同，合在一个
Dockerfile 里会让"只改后端"也重装一遍 node_modules（6770 个文件）。

### 7.2 构建上下文与 `.dockerignore`

必须在 `.dockerignore` 里排掉的东西，每一条都有具体后果：

| 排除项 | 不排的后果 |
|---|---|
| `frontend/node_modules`、`frontend_admin/node_modules` | 构建上下文从几十 MB 涨到 GB 级，每层传输都变慢 |
| `backend/.venv` | 把宿主机的 venv 拷进镜像，架构/路径都可能不匹配 |
| `backend/prdwright.db`、`**/*.db-wal`、`**/*.db-shm` | **把开发数据打进镜像**，且是"看起来能跑"的那种错 |
| `backend/.env` | 泄漏配置；且会让镜像内的配置来源变得不可预测 |
| `backend/secrets/` | 泄漏主密钥。这是最严重的一条 |
| `**/__pycache__`、`**/*.pyc` | 无效层，且可能带宿主机的绝对路径 |
| `.git` | 体积；且镜像里不该有历史 |

镜像**必须**包含：`backend/migrations/`（迁移是随启动跑的）、`skills/`（技能资产，
`ensure_skills` 在启动时扫它）、`backend/alembic.ini`。

### 7.3 构建期 vs 运行期

| 配置 | 生效时机 | 容器里的做法 |
|---|---|---|
| 前端 API 基址 | **构建期**（Vite 替换 `import.meta.env`） | 只用相对路径 `/api`，**不**设 `VITE_API_BASE` |
| 后端配置 | 运行期（`pydantic-settings` + `system_config` 热配置） | 环境变量 + `env_file` |
| `PRDWRIGHT_MASTER_KEY_PATH` | 运行期，且**只读 `os.environ`** | 指向 `/run/secrets/master.key` |

第 1 行是最容易踩的：`VITE_API_BASE` 在 `docker run -e` 里改是**无效**的，
因为它早在 `npm run build` 时就被替换成字面量了。想改基址只能重新构建镜像。

---

## 8 方言收敛（摘要）

详细的层边界、收敛点契约、不收敛清单见附录 B。这里只写
它与部署形态的两处交界面：

1. **`DB_PATH` 仍然是文件路径**（本期不换库），但 `core/schema.py` 不再手工拼
   `sqlite+pysqlite:///`，而是从 `engine.url` 反推——这一步让「迁移改的库」与
   「服务读的库」的一致性由 URL 对象保证，而不是由字符串拼接保证。
2. **容器里的 `DB_PATH` 指向卷**（`/var/lib/prdwright/prdwright.db`）。
   路径变了但语义没变，`prepare_schema` 的三条分支（空库 / 无版本 / 有版本）
   一条都不用改。

---

## 9 契约

| # | 契约 | 破了会怎样 |
|---|---|---|
| **C-V8-1** | `api` 副本数恒为 1；`--workers 1` 写死在镜像 `CMD` | 任务与 SSE 分属不同进程，客户端永远等不到输出 |
| **C-V8-2** | 数据库目录必须是命名卷，禁止 bind mount 宿主目录 | 随机 `disk I/O error`、静默丢最近写入 |
| **C-V8-3** | 宿主机不得 publish `api:8000` | v7 的"后端只听回环"退化成"后端直接对外" |
| **C-V8-4** | `/health` 语义不变；探针一律用 `/healthz` / `/readyz` | 可降级状态被当成故障，容器被反复重启 |
| **C-V8-5** | 前端只用相对路径 `/api`，不引入构建期注入的基址 | 换域名要重新构建镜像，且 `docker run -e` 改了不生效 |
| **C-V8-6** | 迁移只在 `lifespan` 里跑一次，entrypoint 不跑 | 两个入口各跑一次，迁移日志与锁行为都不可预测 |
| **C-V8-7** | 主密钥只经由 `/run/secrets`（只读）进入容器，不写进镜像层、不进 `env_file` | 镜像被拉到任何地方都带着密钥 |
| **C-V8-8** | 大段文本（文档正文、草稿、章节清单）上限 **60KB 字节**；闸门在 `core/limits.py`，写入前报错 | 没有这条，超长内容会在 MySQL 写入那一刻以 `Data too long` 失败，错误信息指向数据库而不是"内容太长" |

---

## 10 取舍与风险

### 接受的风险

| 风险 | 为什么接受 | 缓解 |
|---|---|---|
| 单副本 → 发布必然中断服务 | 多副本需要先把 `_RUNTIMES` 外置，那是 v9 量级 | 发布前排空 + 300 秒 grace period |
| 卷删除 = 数据全丢（`docker compose down -v`） | 命名卷是正确做法，代价是"误删一条命令就没了" | 文档里把 `-v` 标红；备份脚本独立于卷 |
| 镜像体积（torch 类依赖不在本项目，但仍含 langchain 全家桶） | 多阶段构建已经把构建工具剥掉 | 只装 `--no-dev`；必要时按协议拆镜像 |
| 容器内时区是 UTC | 项目本来就按 UTC 存（`core/clock.py`） | 显式设 `TZ=UTC`，不继承宿主机 |

### 否决的方案

| 方案 | 否决理由 |
|---|---|
| 单容器塞 Nginx + uvicorn（supervisord） | 重启 Nginx 会杀掉生成任务；两个进程的失败语义不同 |
| 加一个 MySQL 容器（顺手换库） | 单 worker 约束下换库拿不到扩展收益；且改造代价落在唯一真实路径上 |
| 用 `docker compose --scale api=2` 做"高可用" | 会直接违反 C-V8-1，表现为任务卡死而不是报错 |
| 数据库目录 bind mount 到宿主目录（方便看文件） | Windows 开发机上直接不可用；Linux 上也会让"备份"与"数据"耦合到宿主路径 |
| 用 `/health` 当 healthcheck | 恒 200，等于没配 |
| 把 `VITE_API_BASE` 做成运行期环境变量 | Vite 是构建期替换，做不到；做了会产生"改了不生效"的错觉 |

## 附录 A：死代码与退役内容清理（原 v8 的清理清单，已并入）

> 正文由该专题原文逐字移入，仅标题降两级以适应附录层级；内部交叉引用已改指本文件对应附录。

> 本文件是 **T18.8** 的执行清单。目标不是"代码好看"，是**仓库里不存在"读不到、也删不掉"的东西**。
> 为什么单列一个阶段：这类内容的成本不在运行时，在读代码的人——每个新人都要先判断
> "这一列/这个组件还能不能用"，判断成本天天发生，而删掉它的成本只发生一次。
>
> ⚠️ 本清单于 2026-11-02 按工作区实测重审过一次：原 §2 的四项（A1~A4）判据仍成立，
> 但**漏了 6 类**，且验收基线写错了（1006）。下方 §2 已补全，§6 已改实测值。

---

#### 1 判据

一个东西算"死"，必须同时满足三条，缺一条就不动：

1. **没有任何代码读它**（`grep` 可证，不是"我觉得没人用"）；
2. **删掉它不会让用例变红**（若有，必须在清单里写明为什么仍要删、用例怎么改）；
3. **删掉它不需要改业务逻辑**（需要改逻辑的，属于重构，不属于清理）。

第 3 条是边界：`paste_prd_text` / `draft_prd_text` 这类列看起来"老"，但它们**仍在
`schemas.py` 与 `solution_service.py` 里读写**，不满足第 1 条，因此不在本清单内。

⚠️ **"被文档说是死的"不算证据**：`AppDialog.vue` 被 `v2/ARCHITECTURE.md` 写成
"零外部引用"，实际 `ReviewStage.vue:7` 正在 import 它；照文档删会构建失败。每次删除前必须自己 grep。

---

#### 2 已确认清单（可删）

| # | 项 | 证据 | 处置 | 可逆 |
|---|---|---|---|---|
| **A1** | `solutions.data` 列 | `models.py:229`；`solution_repo.py:119` 写空串、`update` 不写；`solution_service` 文件头写「不再写、不再读」 | Alembic `drop_column` + 删模型字段 · **已完成（#8）** | **否** |
| **A2** | `model_providers.builtin` 列 | `models.py:625-627`「v19 起一律为 0，代码不再读取」；全仓零查询 | Alembic `drop_column` + 删模型字段 · **已完成（#9）** | **否** |
| **A3** | `frontend/src/components/ui/AppToast.vue` | 全仓 `grep AppToast` 零命中（含 kebab-case 与全局注册两条逃逸路径） | 删文件 | 是 |
| **A4** | `frontend/src/components/ui/AppEmpty.vue` | 同上，零命中 | 删文件 | 是 |
| **A5** | `frontend/src/components/ui/AppInput.vue` | **原清单漏项**。零 importer；`src` 内唯一命中是 `fieldStyles.ts:9` 的**注释** | 删文件 | 是 |
| **A6** | `services/job_service.py:363 cancel_for_solution` | **原清单漏项**。零调用；`delete_solution_and_cancel_jobs`（`:411-424`）把同样三步内联重写了一遍；3 处注释仍写「删除方案时必须走这里」 | 删函数 + 同步 3 处注释的指向 | 是 |
| **A7** | 10 个零提及的符号 | `normalize_stage`、`is_valid_role`（`core/chat_kinds.py:44/59`）、`master_key_exists`（`core/crypto.py:51`）、`is_present`（`core/secrets.py:119`）、`current_engine`（`core/engine.py:154`）、`config_service_window`、`input_ceiling_for`（`core/runtime_config.py:465/500`）、`ClearForSolution.clear_for_solution`（`chat_session_repo.py:105`）、`ChatTurnRepository.has_turns`（`chat_turn_repo.py:219`）、`_PLACEHOLDER_URL`（`core/schema.py:53`） | 删 | 是 |
| **A8** | 8 条未使用 import（ruff F401 已确认真无消费者） | `api/conversation.py:11`、`api/schemas/`（拆分前为单文件 19/22 行）、`core/engine.py:49/50`、`core/runtime_config.py:54`、`repositories/document_repo.py:34`、`services/conversation_service.py:42` | 删 | 是 |
| **A9** | `backend/src/backend/__init__.py` 的第二个 FastAPI app + `pyproject.toml:36` 坏入口点 | `__init__.py:1-7` 建了 `app` 与 `/health`，全仓 `from backend import app` 零命中；真 app 在 `main.py`、`/health` 在 `api/health.py:144`；`pyproject.toml:36` 的 `backend = "backend:main"` 指向不存在的属性（`hasattr(backend,'main')` 实测 False） | 删 app 代码块；入口点最终**整块删除**（见下方补记） | 是 |
| **A10** | 前端 6 项：3 个孤儿资源 + 2 个零引用函数 + 1 个死类型 | `assets/main.css`（0 importer）、`assets/base.css`（仅被 main.css 引用 → 级联死）、`assets/logo.svg`（0 命中，注意 `grep logo` 会命中 `logout` 是假阳性）、`api.ts:123 getQuestions()`、`api.ts:887 generatePrdStream()`、`types/index.ts:457 SessionData`（**不是** `:1155` 的 `SessionDataV2`，那个活着） | 删 | 是 |

##### A9 补记：入口点不是改对值就完事，它有两层坑

层一（本次执行中修掉）：`backend = "backend:main"` 指向包里不存在的属性 —— 只有真按脚本
启动才炸 `AttributeError`，所以长期没人碰。

层二（#4 收口之后才浮出来）：把值改成 `backend.main:app`，属性**确实解析得到**了，票面验收
「指向真实存在的属性」达成；但 console script 是**零参调用**入口对象，而 FastAPI 实例的
`__call__(scope, receive, send)` 要服务器递来的三个参数，于是 `uv run backend` 变成
`TypeError: FastAPI.__call__() missing 3 required positional arguments`。

根因不是「值写错」，是**把 web 应用当成了可执行命令**。应用只由服务器加载
（`uvicorn backend.main:app`，README / 部署 / CI 一直走这条）。最终处置：**把
`[project.scripts]` 整块删除**——全仓零使用者，留着它等于给下一个人（或下一个 agent）
留一个照 `pyproject.toml` 一敲就报错的陷阱；不包 `serve()` 是因为那会让「怎么启动」
多出第二种说法。守卫落在 `tests/test_entrypoint.py`：一条钉住「别再声明 backend 命令」，
一条对**任何**现存命令做 `signature().bind()` 零参可调用性检查（同时覆盖旧坑与新坑）。

##### A1 / A2 的删除障碍（必须一起改）

- `models.py:665` 的 `("model_providers","builtin")` 在 `BOOLEAN_COLUMNS` 白名单里，
  该集合被 `migrations/env.py:41,90`、`core/schema_snapshot.py:41,56`、`tests/test_schema_parity.py:23,133` 读。
  **只删字段不删这一行，parity 用例立刻红。**
- `tests/conftest.py:291` 的 `data.pop("builtin", None)` 是给这一列做的兼容垫片，删列后可一并删。
- `tests/test_text_limits.py:109` 手写 `data=""` 构造 `Solution(...)`，删列后这处构造要改。

##### 为什么 A1 值得删，而不是"留着当历史备份"

1. **"留着备查"这个用途从未兑现**（无导出功能、无管理端页面读它）；
2. **它会在换库时变成额外决策**（附录 B §3.3 把它列进 `MEDIUMTEXT` 变体名单，理由是"不知道它会多大"）；
3. **结构对拍要为它买单**（多一列 = `diff_snapshots` 多一条可能的假阳性）。

另有一处**文档自相矛盾**需在删列时一并修：`core/limits.py:6` 仍把 `solutions.data` 当作受
60KB 闸门约束的四列之一，而 `models.py:228` 明写"闸门不覆盖它"——且 `ensure_text_within_limit`
对该列**零调用**。

---

#### 3 需人判断（判据 1 成立、2 不成立：只被测试引用，或属公共 API）

**后端 7 项**

| 项 | 证据 | 为什么不能一刀切 |
|---|---|---|
| `document_store.get_document` | 生产零调用（内部走 `_read_document`），22 处测试调用 | 删要连 22 处用例一起改 |
| `review_whole_document` | 生产零调用，docstring 自己写"面向用户的生成链路不要用"；4 处测试调用 | 看着像"给测试留的第二实现" |
| `solution_service.delete_solution` | 生产零调用（路由走 `job_service.delete_solution_and_cancel_jobs`），1 处测试调用 | 同上 |
| `DOWNSTREAM_KINDS` | 零代码引用；`v3/ARCHITECTURE.md:209` 把它当"依赖失效"语义的锚点 | 当文档用还是当数据用 |
| `langchain` 顶层依赖 | `pyproject.toml:13`，backend/src 零 import（只用 `langchain_core`/`langchain_openai`）；`uv.lock` 反向依赖里只有 backend 需要它 | 删前须确认没有隐式 `import langchain` 的插件路径 |
| `python-dotenv` | 零 import，`core/secrets.py:42-48` 明写"刻意不用 `dotenv_values`"；但 `pydantic-settings`/`uvicorn` 依赖它，删声明不会从环境消失 | 收益低 |
| `api/config.py` 空路由 | 0 个端点，被 `main.py:20/240` import，`config.py:10` 挂 TODO | 自述"给将来留的接口" |

**前端 28 项**：全部是"源码零引用、只有 `__tests__` 存活"（`api.ts` 的 8 个流式接口、
`runSubscriptions.detach`、store 的若干导出、`utils/workflow.ts` 的一组守卫函数等）。
其中 `frontend/src/__tests__/AppV2.spec.ts:99-101` 的注释**自己承认**那 8 个流式接口
"应用里已经没有任何调用点"。另：35 处只是 `export` 关键字多余（声明本身活着），删的是 `export` 二字不是代码。

**两类明确不删**：

- `resetForTests`（`runSubscriptions.ts:53`）——测试隔离钩子，属设计如此，判据 1 不适用；
- `pymysql`（`pyproject.toml:24`）——零 import，但 SQLAlchemy 按 `mysql+pymysql` **动态加载驱动**，
  删了 MySQL 部署第一次连接就报 `No module named 'pymysql'`（证据 `tests/test_database_url.py:33,84`）。

---

#### 4 文档承诺、但仓库里不存在的路径

这一类不是死代码，是**文档承诺与仓库现实不一致**——更容易误导人：读者照着文档去 `deploy/` 找模板，会以为是自己 clone 错了。

原 §3 只记了 3 份模板；**实测是整个"部署交付物"层从未落盘**，另有两处同类：

| 出处 | 承诺的路径 | 实际 |
|---|---|---|
| `v7/roadmap.md:57,58,59,60`、`v7/ARCHITECTURE.md:846-849`、`v7/README.md:75`、附录 C:262、`v8/roadmap.md:198` | `deploy/backend.env.example`、`deploy/systemd/prdwright.service`、`deploy/nginx/prdwright.conf`、`scripts/deploy/{backup,restore,release,smoke}.py` | **否**（连 `deploy/` 目录都没有；`scripts/` 下只有 `keygen/generate_keys.py`） |
| 附录 C:487,533、`v8/roadmap.md:246` | `scripts/deploy/test-mysql.sh` | **否** |
| 附录 C:485、`v8/roadmap.md:244` | `core/snapshot/{sqlite,mysql}.py` | **否**（`core/snapshot/` 目录不存在） |
| 已在本轮清理中删除 | `docs/plans/`、`docs/artifacts/` | 目录已删；根 `README.md` 与历史档案索引里的引用已同步改写（历史档案目录随后也整体移除） |

**处置（已定）**：对 `deploy/` 与 `scripts/deploy/` 选「废」——在相关文档标注「未落盘，以 v8 容器化为准」，
不补一套只为存档的裸机模板（v8 之后裸机不是主路径，补了就是两份真相并存）。
`v6/roadmap.md:45-46` 的 `tests/support/`、`tests/fixtures/` 与 `backend/tests/integration/`（早先已被明确否决过的形态）同属此类，一并标注。

> ⚠️ 这与 v5 那批部署文档是同一类问题的**第二次出现**：**文档写了、文件没落盘**。
> 所以 v8 的验收里加一条硬要求：**凡是文档里写出的路径，`ls` 必须能看见**。

---

#### 5 重写（阈值：单文件 > 1200 行）

判定口径：**只看文件行数，不看函数行数**——Vue 的 composable 与 Pinia 的 `defineStore` setup 函数
天然等于整个模块（如 `useDocumentPipeline` 1543 行、`useSolutionsStore` 648 行），把它们当"大函数"
等于把"重写整个 store"当成"拆一个函数"。函数口径因此作废。

| # | 文件 | 行数 | 备注 |
|---|---|---|---|
| W1 | `backend/src/backend/services/job_service.py` | 792 | **已完成（#5）**。按职责拆到 `backend/src/backend/services/job_runtime.py` / `job_runtime_events.py` / `job_finalize.py` 等子模块；`_apply_event` 已随迁 |
| W2 | `backend/src/backend/services/prd_section_writer.py` | 1129 | **已完成（#6）**。并行车道拆进新模块 `backend/src/backend/services/prd_section_parallel.py`（474 行） |
| W3 | `backend/src/backend/api/schemas/` | 9 个模块，最大 358 | **已完成（#7）**。无函数，纯 Pydantic 模型，按领域拆模块 |

临界区不入列（`document_service.py` 1144、`prompts.py` 918、`conversation_service.py` 913、
`adminService.ts` 873、`admin.py` 842、`admin_service.py` 835、`admin_config_service.py` 812）。
前端 `useDocumentPipeline.ts` 1627 / `api.ts` 1269 / `types/index.ts` 1201 **本轮不动**，下一轮再议。
拆不动的就不拆——这条优先于"压到 1200 以内"。

**唯一一对 ≥30 行完全重复**：`prd_section_writer.py` 553-585 ↔ 739-771（33 行，
`_stream_linear` 与 `_stream_parallel` 的同一段初始化）。是否抽成私有方法，**留到重写 W2 时一并决定**。

**验收方式**：TDD——先补覆盖再改结构；改前后行为不变由全量用例证明；补的用例只覆盖原本没覆盖的分支；
并给出重写前后的行数/函数数对比。

---

#### 6 顺序与不可逆点

```text
笔 1  文档层收敛 + 修正硬错      → 链接自检 + 逐条 ls
笔 2  前端死代码（A3/A4/A5/A10） → npm run build（两个工程）
笔 3  后端死代码（A6~A9）        → uv run pytest + 构建
笔 4  重写 W1~W3（TDD）          → 全量绿 + 行数对比
笔 5  删列 A1/A2                 → 备份 → 迁移 → 结构对拍 → 全量
```

**笔 2/3 先做**：零风险、零依赖，可以立刻验证清理流程本身。

**笔 5 的不可逆性必须说清楚**：

- 删列之后，**回滚代码会让旧代码读不到列**（启动即报错）。所以回滚方式是
  **恢复备份**，不是 `git revert`（同样的推论在删列场景下也成立）。
- 因此三步必须在同一个窗口内完成：`VACUUM INTO` 备份 + 自检 → 跑迁移 → 全量用例。
  备份没做成不许跑迁移。
- SQLite 的 `DROP COLUMN` 需要 batch 模式（`migrations/env.py` 已配 `render_as_batch=True`）。

---

#### 7 验收

| # | 判据 | 怎么验 |
|---|---|---|
| 1 | 删掉的列不再出现在任何读写路径 | `grep -rn "\.data\b" backend/src` 只剩注释；`grep builtin` 只剩历史迁移 |
| 2 | 结构对拍全绿 | `uv run pytest tests/test_schema_parity.py` |
| 3 | 全量用例绿 | `uv run pytest`；**工作区基线 1014 例 / HEAD 986 例**（`--collect-only` 口径；`def test_` 文本计数 890，差额来自 parametrize 展开）。原写的 1006 是旧账 |
| 4 | 前端两个工程仍能构建 | `npm run build`（用户端 + 管理端） |
| 5 | 文档里写出的路径都真实存在 | 逐条 `ls` |
| 6 | 库结构里没有"模型没有、库里还在"的表 | `alembic check` 无差异 |

---

#### 8 本轮接受的偏差（记录用）

这一节只记录**与既定口径不一致、但有意保留**的地方——不是待办，也不是辩解；写下来是为了
下一轮 review 不必重新判断「这是漏了还是已经决定过」。

- **新增 3 个守卫测试文件 + `test_layering.py` 的棘轮/冻结点**（`backend/tests/test_entrypoint.py`、`backend/tests/test_cleanup_dead_code.py`、`frontend/src/__tests__/dead-code.spec.ts`）：偏离「零新增接缝」，理由是它们钉住的是「删掉的东西不许回流」与「契约形状」，属于仓库既有的**架构守卫**范式，不是新的测试接缝。
- **#2 多删 3 个级联类型**（`GeneratePrdStreamPayload` / `QuestionsConfig` / `QuestionConfig`）、**#3 多删 2 个常量**（`DEFAULT_CHAT_STAGE` / `TURN_ROLES`）：前者的唯一消费者是被删的 `getQuestions` / `generatePrdStream`，后者口径已并入 `backend/src/backend/core/chat_kinds.py` 的 docstring——都是删干净而不是留下「有类型没函数」的空壳。
- **#6 两处行为校准**（失败任务不再被 `done_callback` 摘除 → 恢复「出错只发 error 不发 done」；断开时 `lane.aclose()` 立即取消在途调用）：两处都有新用例覆盖，不是顺手改行为。
- **`prd_section_parallel` 与 `prd_section_writer` 存在双向依赖**（函数内延迟 import），且并行模块会调 writer 的若干私有方法：记录为已知的边界代价——本轮重写只搬不改，拆成两个模块仍比留一个 1129 行的单模块可读。
- **`job_service` 用 `noqa` 再导出 25 个名字（其中 23 个私有名）**：是为保住既有测试的 `import` 面（`js._finalize` / `js._RUNTIMES` 这类直触），与 `api/schemas/__init__.py` 的再导出是同一种范式。

---

#### 9 删列的备份窗口（演练记录）

删列不可逆，窗口内三步必须齐：`VACUUM INTO` 备份 + 自检 → 跑迁移 → 全量用例。
A1（`solutions.data`）删除时实际做过一遍自检，记录如下（据 #8 报告）：

1. 临时库先建到 `0002`（删列前的最新结构）；
2. `VACUUM INTO` 生成备份；
3. 三条断言：`PRAGMA integrity_check` = `ok`；备份内 `alembic_version` = `0002`；备份含 `data` 列；
4. 备份 180,224 字节，sha256 `bb1cd19205dc532c163789c8547158d9c84e3e6d9b9e72ccfe1e4b1347b0c20a`；
5. **三条断言通过之后**才执行迁移。

同一窗口内的处置对 #9（`model_providers.builtin`）同样适用。

---

#### A-1 扫描工具与误报

| 工具 | 命令 | 实测 |
|---|---|---|
| ruff F401/F811 | `uv run ruff check --select F401,F811 backend/src` | 命中 11 条，其中 8 条是真未使用 import（→ A8） |
| vulture | `uv run --with vulture vulture backend/src --min-confidence 80` | ⚠️ **0 条**。该阈值太高；放低到默认 60% 才出 122 条 |
| knip | `npx --yes knip@5 --reporter compact` | 两个工程都跑通；但**不认 vitest 入口**，把 45+14 个 spec 报成"未使用文件"、`@vue/test-utils` 报成"未使用依赖"——**误报** |

**`--min-confidence 80` 这条命令要从清单里改掉**：它给出的是"零命中"的假安全感。放低到 60% 后，
122 条里**真死的只有 1 条**（`DOWNSTREAM_KINDS`），其余 121 条分别是路由函数（19）、
LangChain 覆盖点（3）、Pydantic/SQLModel 字段（74）、测试装置 helper（8）——误报比 **121:1**。

处置规则（照旧）：路由函数、`@field_validator`、模型字段 → **保留**；私有函数零调用 → 删；
公共函数零调用 → 先问"是不是给将来留的接口"，答不上来就删。

#### A-2 查未引用 `.vue` 的一次性脚本

```python
import pathlib

for base in ("frontend/src", "frontend_admin/src"):
    root = pathlib.Path(base)
    files = [p for p in root.rglob("*") if p.suffix in (".vue", ".ts")]
    texts = {p: p.read_text(encoding="utf-8", errors="ignore") for p in files}
    for p in files:
        if p.suffix != ".vue":
            continue
        used = [q for q in files if q != p and p.stem in texts[q]]
        if not used:
            print("UNUSED", p.relative_to(root))
```

三个注意点（第三个是 A5 被漏掉的原因）：

- 它按**文件名**匹配，所以对"全局注册"（`app.component("AppToast", ...)`）会漏；跑完对每个命中项再 grep 一次确认；
- 对 `backend/` 的模块级分析**不适用**（`from backend.services import x` 这种写法里没有完整模块路径，会误报）；后端请用 `vulture`；
- ⚠️ 它按文件名做**子串**匹配，**任何一句注释里提到名字都会让它漏报**。
  `AppInput.vue` 就是这么被漏掉的（`fieldStyles.ts:9` 的一句注释）。


## 附录 B：方言可移植性方案（原 v8 的方言专题，已并入）

> 正文由该专题原文逐字移入，仅标题降两级以适应附录层级；内部交叉引用已改指本文件对应附录。

> 主题：让「这个项目跑在 SQLite 上」这件事只出现在一个文件里。
> 本文只写**设计与契约**，不含实施记录；实测数据见附录 D。
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


## 附录 C：双后端方案：SQLite / MySQL 部署时自选（原 v8 的双后端专题，已并入）

> 正文由该专题原文逐字移入，仅标题降两级以适应附录层级；内部交叉引用已改指本文件对应附录。

> 目标不是"能在 MySQL 上跑起来"，而是**两个后端都是正式支持的对象**：同一份代码、
> 同一套用例、同一份迁移链，部署时由 `DATABASE_URL` 决定用哪个。
>
> 本文不按"开发成本"裁剪。凡是两库语义不同的地方，都给出明确处置，而不是
> "先这样，出问题再说"。与附录 B 的关系：那部分记录的是
> **第一段（接口收敛）**，本文是**第二段（真正支持两个后端）**的完整设计。

---

#### 1 目标与判据

##### 1.1 目标

| # | 目标 | 判据 |
|---|---|---|
| G1 | 部署时换库 = 改一个环境变量 | `DATABASE_URL` 是唯一入口，代码里没有第二处拼接 |
| G2 | 两个后端跑同一套用例 | 1006 例在 SQLite 与 MySQL 上各跑一次，均绿 |
| G3 | 行为等价（同一请求同一结果） | 契约用例在两个后端上断言相同的响应体 |
| G4 | 迁移链唯一 | 一份 Alembic 链，两个后端都能 `upgrade head` |
| G5 | 数据可搬运 | 一条 ETL 把 SQLite 的库搬进 MySQL，行数与哈希对得上 |
| G6 | 回滚可执行 | 换库失败能退回 SQLite，且不丢数据 |

##### 1.2 什么算"正式支持"

三条，缺一条就只是"试过"：

1. **CI 有矩阵**：每次提交都跑 SQLite + MySQL 两遍，不是"我本地试过"；
2. **并发用例在 MySQL 上跑**：SQLite 单写者跑不出竞态，只有在 MySQL 上跑才证明
   「加锁/约束真的生效」；
3. **新代码不能只在一个后端上验证**：评审时的默认问题是"MySQL 上呢"。

---

#### 2 选型边界

| 后端 | 状态 | 说明 |
|---|---|---|
| SQLite | **一等** | 单机/单用户的默认形态，零运维 |
| MySQL 8.0 | **一等** | 多用户/多连接，有独立 DB 服务 |
| PostgreSQL | **暂不纳入** | 技术上迁移代价更小（`ON CONFLICT` 与 SQLite 同语法、TEXT 索引无长度限制、部分索引原生支持）。但每纳入一个后端，CI 多一条矩阵、每个语义差异多一套验证。**两个后端已经覆盖了"零运维"和"有 DBA"两种场景**，第三个只增加维护面 |

> 如果将来纳入 PostgreSQL，本文的四个模块边界不用改——它是按"方言能力"切的，
> 不是按"具体某个库"切的。

---

#### 3 总体结构

四个模块，职责正交：

```
core/database.py    ← 用哪个库、怎么连（URL / 方言名 / 引擎装配 / 连接级配置）
core/dialect.py     ← 语句怎么写（upsert / ilike / 行锁 / 死锁重试）
core/types.py       ← 列怎么声明（短文本 / 大文本 / 时间戳）
core/schema.py      ← 结构怎么演进（迁移编排 + 结构对拍分派）
```

一条判据：**任何"跟具体数据库有关"的代码，必须落在这四个文件之一**。
落在别处就是漏网，评审时打回。

`core/engine.py` 保留会话与事务（`session_scope` 仍是唯一事务落点），
它不含任何方言知识——建引擎的动作移交给 `database.py`。

---

#### 4 设计

##### 4.1 配置：`DATABASE_URL` 唯一入口

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

##### 4.2 列类型：一个模块统一声明

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

##### 4.3 长度的来源：勘察，不是猜

附录 D 给出实测值。定 n 的规则：

```
n = max(实测最大值, 语义上限) 向上取到档位（32 / 64 / 128 / 255 / 512 / 1024）
```

`users.username` 是唯一**定不下来**的：实测 6 字符，但 auth 层没有任何长度校验。
所以它的前置任务是先加校验（建议 64 字符），然后才能定 `short_text(64)`。
**在加校验之前不许写死 n**——用当前数据定长度，等于把"恰好没超"当成"不会超"。

##### 4.4 大文本：容量与约束分离

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

##### 4.5 时间戳：保持字符串，改成长度受限的 VARCHAR

`created_at` 等列存 ISO 8601 文本，**字典序 = 时间序**是承重设计，大量
`WHERE expires_at > ?` 依赖它。改成 `DATETIME` 会同时改变落盘格式、精度、
与时区解释，代价远大于收益。

所以：**保持字符串**，但改成 `timestamp_text()`（`VARCHAR(32)`），因为
`idx_app_logs_ts`、`idx_auth_sessions_expires`、`idx_solutions_owner_content` 都在
时间列上，MySQL 下 `TEXT` 建不了索引。

两条配套：

- 业务表 26 字符（无 `Z`）、`app_logs.ts` 24 字符（带 `Z`）的双轨不变；
- 勘察发现的 16 行 `+00:00Z` 脏格式必须在 ETL 前清洗（`DATETIME` 解析会失败，
  虽然本期不改 `DATETIME`，但任何下游导出/分析都会踩）。

##### 4.6 索引与唯一约束

| 项 | 设计 |
|---|---|
| 部分唯一索引 | **去掉 partial 条件**，改普通 `UNIQUE(job_id, role)`。三库的唯一索引都把 NULL 视为互不相等，语义等价（`chat_turn_repo` 的幂等本来就靠"先查 + SAVEPOINT + 撞索引回查"，不依赖 partial） |
| `DESC` 索引 | 不写。SQLite 可反向遍历，MySQL 8.0 也支持降序索引，但**写法差异不值得**——排序需求由查询的 `ORDER BY` 表达 |
| 唯一约束命名 | 显式命名（`ux_*` / `idx_*`），不依赖自动生成。MySQL 与 SQLite 的自动命名规则不同，迁移里按名字比对会失配 |
| 布尔列 | 声明 `Boolean`，MySQL 落 `TINYINT(1)`，SQLite 落 `INTEGER`。类型归一见 4.8 |

##### 4.7 查询语义：三处必须显式

| 项 | 问题 | 设计 |
|---|---|---|
| `LIKE` 大小写 | SQLite 对 ASCII 不区分；MySQL 随 collation；PG 区分 | `dialect.ilike(column, pattern)`：SQLite/MySQL 走原生 `LIKE`（都是不区分），其他方言走 `lower()` 两侧。**4 处调用点全部改用它** |
| 排序 | SQLite 按字节序；MySQL `ai_ci` 按词典序（忽略大小写与重音） | 需要稳定顺序的地方显式 `ORDER BY id` 兜底；不承诺跨库的"字母序一致"（那是产品问题，不是技术问题） |
| 字符串长度 | MySQL 的 `length()` 返回字节，SQLite/PG 返回字符 | `content_length` 改**冗余列**，写入时 Python `len()` 算。顺带省掉读正文算长度的开销 |

##### 4.8 结构对拍：按方言分派，不做"方言中立"

原计划换 `sqlalchemy.inspect` 已被实测否决：它不返回外键的 `ondelete`，
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

##### 4.9 并发：这是唯一"不报错"的一类

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

##### 4.10 迁移：一条链，按方言分派

- `render_as_batch` **按方言开关**：SQLite 需要（不支持 `ALTER COLUMN` / `DROP COLUMN`），
  MySQL 不需要（会生成无谓的中转表）。现在是硬编码 `True`。
- `compare_type` 的布尔归一按方言重算：MySQL 反射回来是 `TINYINT`，SQLite 是 `INTEGER`。
- SQLite 的 batch 模式会"建新表 + 拷数据 + 换名"，删列/改类型时**表越大越慢**，
  且需要可写的临时目录空间。这一点写进发布检查单。

##### 4.11 结构的一次性变更

改成 `short_text(n)` 会让 SQLite 的建表语句从 `TEXT` 变成 `VARCHAR(n)`。这不是"漂移"，
是**新基线**。处置：

1. 一条迁移，把十几列从 `TEXT` 改成 `VARCHAR(n)`；
2. SQLite 下走 batch（重建表，数据保留），MySQL 下走原生 `ALTER`；
3. 迁移跑完，`test_schema_parity` 的期望快照随之更新；
4. **这一条与 T18.8 的删列合并成同一次发布**——反正都要重建表，分两次做等于把
   同样慢的操作做两遍。

##### 4.12 数据搬运（SQLite → MySQL）

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

##### 4.13 备份：按后端分派，接口统一

`scripts/deploy/backup.py` 对外只有一个命令，内部按 `DATABASE_URL` 的 scheme 分派：

| 后端 | 方式 | 注意 |
|---|---|---|
| SQLite | `VACUUM INTO` | 不能 `cp`（WAL 会缺最近写入）；产物要自检（能打开 + `integrity_check` + 读得到 `alembic_version`） |
| MySQL | `mysqldump --single-transaction --set-gtid-purged=OFF` | InnoDB 一致快照；`--single-transaction` 必须有，否则备份会锁表 |

两者都追加**同一条自检**：备份产物能被读、`alembic_version` 等于预期。

---

#### 5 测试策略

这一节是"正式支持"与"试过"的分界线。核心结论先说：

> **不是所有用例都该在两个后端上跑。** 有些用例验证的是「某个后端的特性」，
> 它们在另一个后端上失败是**正确**的。把"1006 例双跑"当目标，会得到一堆
> 无意义的红点，然后有人开始加 `skip`——那才是"写了没法验证"的开始。

##### 5.1 三类用例

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

##### 5.2 用例级后端标记

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

##### 5.3 数据隔离：三种策略，选两种

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

##### 5.4 并发用例：怎么造并发

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

##### 5.5 本地怎么跑 MySQL（这条不能只留给 CI）

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

##### 5.6 CI 矩阵与时间预算

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

##### 5.7 支持范围

| 后端 | 版本 | 说明 |
|---|---|---|
| SQLite | 3.35+ | 需要 `RETURNING`（SQLAlchemy 用于自增回填）与 `DROP COLUMN` |
| MySQL | **8.0 / 8.4** | 8.0.13+ 才支持表达式默认值（我们不用，见 4.4）；8.0 的 `VALUES()` 语法在 8.0.20 起标记弃用但仍可用，换别名语法时只改 `core/dialect.py` |

CI 矩阵默认只跑 **8.0**；8.4 每月跑一次（`schedule` 触发），用来提前发现"下一个 LTS
会坏在哪"。不把 5.7 纳入：它没有 `utf8mb4_0900_as_cs`，也没有表达式默认值，
支持它等于给类型策略开一个例外分支。

##### 5.8 怎么证明"两个后端行为真的一样"

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

#### 6 契约

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

#### 7 实施顺序

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

#### 8 风险与取舍

##### 8.1 接受的风险

| 风险 | 为什么接受 | 缓解 |
|---|---|---|
| MySQL 的死锁无法消除 | 间隙锁是引擎行为 | 重试包装 + 并发用例 |
| 两个后端的排序结果可能不同 | 词典序是产品问题，不是技术问题 | 需要稳定顺序的地方用 `id` 兜底；不承诺字母序一致 |
| 测试基建改造会碰到大量用例 | 这是"正式支持"的必要成本 | 一次做完，不留两种写法 |
| SQLite 的 batch 迁移慢（重建表） | 单机部署，数据量小 | 与 T18.8 合并成一次发布 |
| MySQL 多一个需要运维的组件 | 用户明确要这个能力 | 容器化（T18.5）已覆盖 |

##### 8.2 被否决的方案

| 方案 | 理由 |
|---|---|
| 只让 MySQL 能跑，SQLite 弃用 | SQLite 是零运维形态，砍掉等于放弃"个人自部署"场景 |
| 用 ORM 抽象抹平一切，不做显式分派 | 实测过：`inspect()` 会丢 `ondelete`。抽象层给的是共同子集，而我们要的是"两边都完整" |
| 保留 `TEXT` + MySQL 前缀索引 | 前缀唯一 ≠ 全值唯一，用户名/哈希这种列语义会变 |
| 把时间戳改成 `DATETIME` | 改动波及 `clock.py` 与所有比较点，收益（更小的索引）不足以抵消 |
| 运行时切库 | 需要回答"在途任务往哪写""旧池怎么处置""迁移何时跑"，且没有使用场景 |

---

#### 9 验收

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


## 附录 D：库结构勘察（prdwright.db 实测）（原 v8 的勘察报告，已并入）

> 正文由该报告原文逐字移入，仅标题降两级以适应附录层级；内部交叉引用已改指本文件对应附录。

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

