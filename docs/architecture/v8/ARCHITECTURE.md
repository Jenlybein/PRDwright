# v8 架构：容器化与方言收敛

> 方言收敛的完整论证见 `DIALECT-PORTABILITY.md`；本文只写它与部署形态的交界面。
> 勘察数据见 `schema-survey.md`。决策依据见 [`../../adr/`](../../adr/README.md)（ADR-088 ~ 093）。

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

- **不换数据库**。理由见 ADR-091：换库的最大收益（多副本）被进程内状态锁死，
  改造代价却全落在唯一真实运行的路径上。`DIALECT-PORTABILITY.md` §3.6 的台账保留，
  换库那次按它执行。
- **不做多副本 / 不做水平扩展**。`_RUNTIMES`、`JobBus`、runtime 快照、固定连接池
  都在进程内（ADR-055 继续有效）。`api` 的副本数**恒为 1**，这是契约不是建议。
- **不引入 Kubernetes / 不做 CI 自动发镜像**。目标形态是"单机 + compose"，
  与 v7 的"单台轻量服务器"是同一个规模判断。
- **不改 `/health` 的语义**。它恒返回 200、`status` 只有 `ok`/`degraded`（v7 契约）。
  新增两条探针接口，而不是改它——改语义会动到已有的监控与文档。
- **不改前端代码**。两个前端已经是相对路径（`baseURL: '/'`），容器化正好利用这一点。
- **不把数据库放进独立容器**。本期仍是 SQLite 文件，独立 DB 容器没有对象。
- **不改时间列的存储格式**（业务表无 `Z`、日志表带 `Z` 的双轨，ADR-077）。

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

沿用 v7 的结论（ADR-058）：`VACUUM INTO` 到同一个卷内的另一个文件，再做自检
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

详细的层边界、收敛点契约、不收敛清单见 `DIALECT-PORTABILITY.md`。这里只写
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
| 加一个 MySQL 容器（顺手换库） | 单 worker 约束下换库拿不到扩展收益；且改造代价落在唯一真实路径上（ADR-091） |
| 用 `docker compose --scale api=2` 做"高可用" | 会直接违反 C-V8-1，表现为任务卡死而不是报错 |
| 数据库目录 bind mount 到宿主目录（方便看文件） | Windows 开发机上直接不可用；Linux 上也会让"备份"与"数据"耦合到宿主路径 |
| 用 `/health` 当 healthcheck | 恒 200，等于没配 |
| 把 `VITE_API_BASE` 做成运行期环境变量 | Vite 是构建期替换，做不到；做了会产生"改了不生效"的错觉 |
