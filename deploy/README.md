# PRDwright 部署（v9）

三个进程（`db` / `api` / `web`）、一个入口、三个命名卷。**Linux 与 Windows 用同一份编排、
同一组命令**——持久化全在 Docker 命名卷里，没有宿主目录挂载，所以不需要平台的 override 文件
（为什么必须这样，见 [`../docs/architecture/ARCHITECTURE.md`](../docs/architecture/ARCHITECTURE.md) §3.6：
三个命名卷与唯一部署后端 Postgres）。

第一次部署的完整路径：**准备 `.env` 与主密钥 → 起栈 → 进管理后台配一个模型供应商 → 登录使用**。
下面的命令都可以直接粘贴，Docker 命令在 PowerShell 与 bash 里一致；只有复制文件那一步不同。

---

## 0. 前置条件

| 项 | 要求 |
| --- | --- |
| Docker | Docker Engine 或 Docker Desktop（Linux 容器模式），Compose v2 及以上 |
| 宿主端口 | `80` 空闲（被占用时见 §8，改一个变量即可） |
| 宿主机工具 | **不需要** Node、Python、uv——镜像里自带 |
| 磁盘 | 两个镜像合计约 1.3 GB（含 Python 依赖树与两个前端产物） |

本仓库的部署件在 `deploy/` 下，**所有命令都在这个目录里执行**：

```bash
cd deploy
```

---

## 1. 一次性准备

### 1.1 建 `.env`

```bash
cp .env.example .env
```

Windows PowerShell：

```powershell
Copy-Item .env.example .env
```

打开 `.env`，**至少改这三项**：

| 变量 | 改什么 |
| --- | --- |
| `POSTGRES_PASSWORD` | 数据库口令。compose 用它拼 api 的 `DATABASE_URL`，缺它直接起不来 |
| `INIT_ADMIN_PASSWORD` | 首个管理员密码（`users` 表为空时自动创建该账号，且直接是管理员） |
| `INVITE_CODE` | 注册邀请码（`invite_codes` 表为空时自动播种） |

其余变量都有中文说明与默认值，可以先不动。`.env` 已被版本控制忽略，不要提交。

### 1.2 生成主密钥（只需一次）

主密钥决定库里所有模型密钥能不能解开。**缺少它时 api 拒绝启动**（不会自动生成——
自动生成会让换机后的旧密文永久解不开），启动日志里也会打印同样的命令。

密钥生成器在应用镜像里，所以先构建镜像（不启动容器），再生成并写进密钥卷：

```bash
make build   # = docker compose build
make keygen  # = 下面那条 docker run
```

`make keygen` 展开后就是这条命令（Windows 原生 PowerShell 没有 `make`、也没有 `\` 续行，
直接用一行）：

```powershell
docker run --rm --user 10001:10001 -e PRDWRIGHT_MASTER_KEY_PATH=/secrets/master.key -v prdwright_secrets:/secrets --entrypoint python prdwright-api:local /app/scripts/keygen/generate_keys.py
```

四点提醒：

1. `--user 10001:10001` **不能省**：用 root 生成会得到一个属主 `root`、权限 600 的文件，
   应用（uid 10001）读不到，症状与「密钥不存在」几乎一样。
2. 卷名 `prdwright_secrets` 来自 compose 文件里的 `name: prdwright`；换目录、换机器都是这个卷名。
3. 已经有了就**不要重跑**：脚本会拒绝覆盖（要换密钥用 `--rotate`，它会先把库里的密钥重加密）。
4. **主密钥要单独备份**，并且不要和数据库备份放在同一个包里。恢复备份时，主密钥必须是
   生成那份库时用的那一把——换了一把，库里的模型密钥就全部解不开。

---

## 2. 起栈（一条命令）

```bash
make up
```

Windows 用**同一条命令**——命名卷不是宿主目录，两个平台的路径与文件锁语义一致。
没有 `make` 时展开成 `docker compose up -d --build`（见 §4 的展开表）。

看状态与日志：

```bash
make ps
make logs
```

等 api 的 health 列变成 `healthy`（首次启动要跑迁移并扫描技能包，通常几秒到几十秒）：

```bash
curl -fsS http://localhost/readyz
# {"status":"ready","checks":{"database":{...},"skills":{...}}}
```

PowerShell 也可以用 `Invoke-WebRequest http://localhost/readyz` 或直接浏览器打开
`http://localhost/readyz`。返回 `200` 即就绪；`503` 时响应体会指出是「库不可写」还是
「技能包加载失败」。

### 入口一览（只有一个端口对宿主开放）

| 地址 | 是什么 |
| --- | --- |
| `http://<机器>/` | 用户端（Vue 单页） |
| `http://<机器>/admin/` | 管理后台（独立 SPA，基址 `/admin/`） |
| `http://<机器>/api/...` | 后端接口（含 SSE 流式生成） |
| `http://<机器>/healthz` | 生死探针：进程活着就恒 `200` |
| `http://<机器>/readyz` | 就绪探针：库可写 + 技能可加载，否则 `503` |
| `http://<机器>/health` | 给人看的体检详情：数据库 / 模型供应商 / 技能资产（恒 `200`） |

api 的 8000 端口**不发布到宿主**，只能被 web 通过内部网络访问。这是有意的：入口只有
一个，Cookie 与 CORS 行为才和 v7 的单域名前缀契约一致。

---

## 3. 第一次登录与「配一个模型供应商」

1. 浏览器打开 `http://<机器>/`，用 `.env` 里的 `INIT_ADMIN_USER` / `INIT_ADMIN_PASSWORD` 登录
   （它就是管理员，管理后台用同一账号）；或者用 `INVITE_CODE` 注册一个新账号。
2. 打开 `http://<机器>/admin/` → **模型配置** → 新增一条：选协议、填接口地址与模型名、
   填密钥（密钥加密后入库，之后不会再明文返回）→ 点「测试连通」→ 设为「使用中」。
3. 回到用户端：新建方案 → 对话页 → 生成。

**这一步是首次部署必须做的。** 在配置模型供应商之前：

- `/health` 的 `llm` 项会是 `false`（`status: degraded`），生成会报「还没有模型配置」；
- `/readyz` 仍然 `200`——就绪探针**刻意不查**模型供应商（它依赖外部网络，高频探针会烧额度、
  会误报）。所以「readyz 绿了但生成报错」不是矛盾，是分工。

---

## 4. 常用命令

| 目的 | 命令 |
| --- | --- |
| 起栈 / 改完代码重建 | `make up` |
| 停栈（**保留数据**） | `make down` |
| 重启 api（触发排空） | `make restart` |
| 看日志 | `make logs` |
| 看状态与健康 | `make ps` |
| 进 api 容器 | `make shell` |
| 看卷 | `make volumes` |
| 看生效的完整编排 | `make config` |
| 构建两个镜像 | `make build` |
| 生成主密钥（仅首次） | `make keygen` |
| 备份（见 §6） | `make backup` |
| 回滚预演练（见 §6.7） | `make drill` |

`deploy/Makefile` 的每条目标**只有一条 `docker compose …` / `uv run …`**，没有额外逻辑，
所以 `make` 与 docker 命令是同一套口径的简写与展开式。没有 `make` 的环境（Windows 原生
PowerShell）把上表逐行展开即可：

```
make up      = docker compose up -d --build
make down    = docker compose down
make restart = docker compose restart api
make logs    = docker compose logs -f api
make ps      = docker compose ps
make shell   = docker compose exec api sh
make volumes = docker volume ls --filter name=prdwright
make config  = docker compose config
make build   = docker compose build
make keygen  = 上面 §1.2 那条 docker run
make backup  = uv run --project ../backend python backup.py
make drill   = uv run --project ../backend python drill.py
```

---

## 5. 数据、日志、密钥在哪

| 卷（固定名） | 谁在用 | 内容 | 丢了会怎样 |
| --- | --- | --- | --- |
| `prdwright_pgdata` | `db` 服务 | Postgres 的数据目录（**业务数据的唯一真源**） | **业务数据全丢** |
| `prdwright_logs` | api | 轮转日志文件 | 丢历史日志（`app_logs` 表里还有一份派生数据） |
| `prdwright_secrets` | api（**只读**） | `master.key` | 库里所有模型密钥都解不开 |

> v9 之前那个 `prdwright_data` 卷（装 SQLite 库文件）**不再挂载、也不再被读取**。
> 旧库不会被自动迁移，只作历史归档；处置方式见 §6.5。

- 日志同时写 stdout，所以 `make logs` 是第一现场；卷里的文件供跨容器重建后回查。
- **实时库文件不能双击查看**，这是命名卷的代价。可见性由备份产物与 `docker compose exec db psql`
  补上：备份落到 `deploy/backups/`（已被版本控制忽略，由备份脚本创建）。
- 不要往 compose 里加宿主目录挂载。Windows 上的宿主目录经桥接层会丢掉文件系统语义
  （fsync、目录项、锁）；Postgres 会随机报 I/O 错误、启动失败，或静默丢最近写入。
  换文件系统才能解决，改配置解决不了。

---

## 6. 备份、恢复与回滚

### 6.1 纪律（比命令更重要）

- **迁移之前必须先有备份**；备份没做成，不许 `make up`。
- **删列 / 改类型之后的回滚方式是恢复备份，不是 `git revert`**：回退代码后的真实状态是
  「新结构 + 旧代码」，旧代码读不到那一列，启动即失败。代码可以回退，数据不会自己回去。
- 备份要能**在另一台机器上**被服务读起来，才算备份。

### 6.2 备份命令（一条命令，产物带 sha256）

在 `deploy/` 下（默认的产物目录、`.env`、卷名都按这个位置算）：

```bash
make backup
# 无 make 时展开成：uv run --project ../backend python backup.py
```

在仓库根目录执行的是同一件事：

```bash
uv run --project backend python deploy/backup.py
```

脚本用项目自己的 uv 环境跑（`backend/.venv`，只用 Python stdlib，不要求宿主装
`pg_dump`）。它按四步走，**任何一步失败都非零退出，并指出失败在哪一步**：

1. **前置检查**：Docker 可用、api 镜像存在（只为了校验主密钥）、与服务端同主版本的
   `postgres:<tag>` 镜像存在、`prdwright_net` 网络与 `prdwright_pgdata` /
   `prdwright_secrets` 两个卷存在。**主密钥缺失或不可用就拒绝备份**——没有密钥的备份
   解不开库里的模型密钥，等于无用备份。
2. **数据库导出**：在一次性 `postgres:<tag>` 容器里跑 `pg_dump -Fc`（走 compose 的用户
   自定义网络、服务名 `db`）。不拷数据目录：Postgres 的数据目录包含 WAL 与共享内存状态，
   直接拷会得到一份「能启动、不报错、就是少了最近写入」的假备份。
3. **自检**：`pg_restore --list` 必须能解析这份归档、归档里的 `TABLE DATA` 条目数必须与
   源库 `public` 下的表数相等、并且能读到 `alembic_version`（证明是一份已迁移的
   PRDwright 库，而不是空壳）。自检不过的产物会被删掉并报错——不留下一个「看起来可用」的文件。
4. **落盘与复核**：在宿主上算 sha256 与字节数，写同名 `.sha256`，并把产物属主改回调用者。

产物：

```
deploy/backups/
├── prdwright-20261117-1400.dump
└── prdwright-20261117-1400.dump.sha256   # 一行：<sha256>  <文件名>
```

成功时的输出如下（数字来自一次实测，会随库变化）：

```
[备份] ✓ 备份完成
        文件         /…/deploy/backups/prdwright-20261117-1400.dump
        字节数       200704
        sha256       666137bb…
        自检结论     ok（归档可解析、表数 15 与源库一致、alembic_version=0001）
        校验和文件   /…/deploy/backups/prdwright-20261117-1400.dump.sha256
```

**栈起着或停着都能备份**：`pg_dump` 是数据库侧的一致性快照，脚本也不依赖 api 容器活着；
它唯一需要的是**已经构建过的 api 镜像**（只借用里面的 python 来校验主密钥，不启动服务）。
所以升级前先把栈停掉再备份同样成立。

失败时的退出码含义（脚本开头有完整表）：

| 退出码 | 失败在哪一步 |
| --- | --- |
| `2` | 前置检查：Docker / 镜像 / 网络 / 卷 / 主密钥 / 库地址判定（只接受 Postgres） |
| `3` | 数据库导出：`pg_dump` 本身失败 |
| `4` | 自检：归档解析不了、表数对不上、读不到 `alembic_version` |
| `5` | 写盘或校验：写 sha256 失败，或产物的 sha256 / 字节数对不上 |

**本项目只支持 Postgres**：脚本从 `deploy/.env` 的 `POSTGRES_*`（或显式 `DATABASE_URL`）
推导连接参数；`DATABASE_URL` 指向 SQLite / MySQL 时明确报错并以 `2` 退出。数据库形态
已经定死，所以这里没有第二条备份流程。

### 6.3 校验

在 `deploy/backups/` 里（Linux / WSL / macOS）：

```bash
sha256sum -c prdwright-20261117-1400.dump.sha256
# prdwright-20261117-1400.dump: OK
```

Windows PowerShell 没有 `sha256sum`，用两条命令比对即可：

```powershell
Get-FileHash .\prdwright-20261117-1400.dump -Algorithm SHA256   # 与下一行的首列比
Get-Content .\prdwright-20261117-1400.dump.sha256
```

校验和只证明「文件没被改动」，证明不了「备份可用」——可用性由脚本里的
`pg_restore --list` + 表数对比 + `alembic_version` 自检负责，恢复演练见 §6.4。

### 6.4 恢复（也是「回滚」）

下面是恢复的**机械命令**。先选场景、再照做、每步怎么判断成功——见 §6.7 的入口与
[`ROLLBACK.md`](./ROLLBACK.md)（场景 A / B / C 的逐步命令与判据都在那里）。

```bash
make down                 # 停栈，保留卷（= docker compose down）

# 1) 把备份喂给运行中的 db（先起 db 与空的 api 卷：compose up 之前用一次临时网络）
cd deploy
docker compose up -d db
docker run --rm --network prdwright_net \
  -e PGPASSWORD="$(grep '^POSTGRES_PASSWORD=' .env | cut -d= -f2-)" \
  -v "$PWD/backups:/backup:ro" postgres:16 \
  sh -c "pg_restore --clean --if-exists --no-owner --no-privileges \
         -h db -U prdwright -d prdwright -v /backup/prdwright-<日期>.dump"

# 2) 起服务
#    · 只是「同一版本内恢复数据」：make up
#    · 回滚到**旧版本代码**：先按 §6.7 场景 B 把旧镜像 tag 指回，再 `docker compose up -d --no-build`
#      （用 make up 会按当前代码重建镜像，等于没回退）
make up
```

要点：

- 恢复前先让 `db` 起来（迁移与数据都在那个库里；空库也必须是「已初始化」的库）。
- `--clean --if-exists` 会先删掉现有对象再重建。**确认这是想要的动作**；如果只想追加到
  空库，去掉这两个参数。
- 备份来自另一台机器时，**同时**把那份备份对应时期的 `master.key` 放回 `prdwright_secrets`
  卷（否则库里的模型密钥解不开）。
- 恢复成功后还有一步「**校验**」：`alembic_version` 与表数必须和备份自检输出里记录的一致
  （命令与判据见 [`ROLLBACK.md`](./ROLLBACK.md) §7 的 B4）。跳过校验等于没验证恢复是否正确。
- 回滚到旧版本时，**数据库恢复与镜像回退必须成对**：用新代码启动一个旧备份库，会在启动期把库
  再迁移一次。完整顺序见 [`ROLLBACK.md`](./ROLLBACK.md) §7。
- 恢复后先打开 `/readyz` 与 `/admin/` 确认，再继续操作。

### 6.5 `docker compose down -v` 会删数据

| 命令 | 容器 | 网络 | **卷（数据库/日志/密钥）** |
| --- | --- | --- | --- |
| `make down`（= `docker compose down`） | 删 | 删 | **保留** |
| `docker compose down -v` | 删 | 删 | **连卷一起删，无法撤销** |

`-v` 只该用在「反正我要从零重来」的时候。它删的不只是数据库，还有主密钥——两者一起没，
等于所有加密配置永久作废。普通停栈、升级、重启都**不要**带 `-v`。这一条刻意没有
`make` 目标：删除数据只该是一次需要人真的把 `-v` 敲出来的动作。

### 6.6 历史遗留：旧的 SQLite 备份（`.db` 文件）

v9 之前 `backup.py` 产出的是 `prdwright-<日期>.db`（SQLite 在线快照）。仓库里若还留着
这种文件（或本机 `deploy/backups/` 里有），**不要删、也不要喂给新的恢复流程**：

- 它不属于新链路：现在的备份是 `pg_dump` 归档（`.dump`），恢复走 `pg_restore`；
- 它的结构是 SQLite 时代的（布尔存成 0/1、`AUTOINCREMENT` 自增语义），与本项目的
  Postgres 基线并不一一对应，本项目**不做**旧库自动迁移；
- 正确处理是把它当**历史归档**单独保管（连同当时的 `master.key`）。要迁到新栈，用旧版代码
  起一次、导出需要的业务数据，再按业务需要逐条导入——而不是把它塞回数据卷。

同理，`prdwright_data` 这个旧卷（装 SQLite 库文件）不再挂载，也没有任何代码读它。

### 6.7 回滚预案（切换当天照这个做）

**完整的手册在 [`ROLLBACK.md`](./ROLLBACK.md)**：场景 A / B / C 的逐步命令、预期输出、
「这一步怎么判断成功」、演练记录（含实测耗时）与红线清单都在那里。本节只给入口与最关键的约束。

#### 三个场景，先选一个

| 场景 | 何时用 | 动什么 |
| --- | --- | --- |
| **A** 只回退应用镜像 | 新版本代码有问题，本次发布**没跑过迁移**（`alembic_version` 没变） | 停栈 → 旧镜像 tag 指回 → `docker compose up -d --no-build` |
| **B** 应用 + 数据库一起回退 | 本次发布**跑过迁移**，或数据被新代码写坏 | 停栈 → 起 db → `pg_restore`（§6.4）→ 校验版本/表数 → 起旧镜像 |
| **C** 只回退某一张表 | 只有一张表的数据被误操作，结构没变，其余表正常 | 停 api → `TRUNCATE` 该表 → `pg_restore --data-only --table=<表名>` → 同步序列 |

**A 与 B 的分界线只有一条：`alembic_version` / 表结构变了没有。** 拿不准就按 B 走（更重，但不会留下「新代码 + 旧库」的错配）。
每条命令的预期输出与判据见 [`ROLLBACK.md`](./ROLLBACK.md) §6/§7/§8。

#### 切换当天要备份什么、留多久

| 对象 | 怎么备 | 保留期限 |
| --- | --- | --- |
| **数据库** | `make backup`（`pg_dump -Fc` + 自检 + sha256，§6.2） | 至少 **90 天**，且至少留最近 **3 次切换**各一份；异地再存一份 |
| **镜像 tag** | 切换前 `docker tag prdwright-api:local prdwright-api:rollback-<日期>`（web 同样）+ `docker save` | 到下一次发布验收通过，最短 **30 天** |
| **`deploy/.env`** | 复制进密码管理器/离线加密介质 | 与该版本同寿命 |
| **`master.key`** | 从 `prdwright_secrets` 卷单独导出 | **永久**（每一把都留） |
| 代码 / 迁移版本 | `git tag release-<日期>` + 记录 `alembic_version` | 永久 |

**口令与主密钥不进备份包**：`.env` 与 `master.key` 单独保管，不跟 `.dump` 放同一个包、同一个介质
（`README.md` §1.2 第 4 点）。命令与判据见 [`ROLLBACK.md`](./ROLLBACK.md) §3。

#### 两条边界（别混）

- **回滚 ≠ 并行**：回滚是「停新、起旧」的串行动作，数据单向。本项目的任务运行时、`JobBus`、连接池全在单个 api 进程内，
  且启动即跑迁移——两个版本同时连一个库会互相把结构改回去或启动失败。设计取舍见
  [`../docs/architecture/state-flow-design.md`](../docs/architecture/state-flow-design.md)（「一次性切换，不能靠并行运行退回」）。
- **回滚 ≠ 文档版本历史**：`document_versions` 只增不改，`restore` 是**追加**一版、指针不回拨（业务功能，不停服务）。
  数据库回滚是**整库退回某个时间点**，会连同回滚点之后新追加的版本行一起丢掉。两者是两层，不能互相替代——
  详见 [`ROLLBACK.md`](./ROLLBACK.md) §2。

#### 演练过一次（不是生产演练）

```bash
make drill   # = uv run --project ../backend python drill.py
```

在本机开发库上真跑「迁移 → 造数据 → `pg_dump` → 砸库 → `pg_restore` → 核对」，逐见证耗时（本机实测总耗时 7.3 秒，
每步耗时与判据见 [`ROLLBACK.md`](./ROLLBACK.md) §9）。它幂等、只动演练库 `prdwright_drill`、
不写 `deploy/backups/`、不碰三个命名卷（生产卷）。**这不是生产演练**：生产数据量与磁盘/网络不可比，
切换窗口必须另做一次并把真实耗时补记进去。

#### 红线（每一条都别做）

`docker compose down -v`；用 `docker compose up -d --build` 当回滚；数据库已迁移后还只回退镜像；
用新代码启动旧备份库；`alembic downgrade` 当回滚；把 `.db` 旧备份喂回新栈；把 `.env` / `master.key` 打进备份包；
api 还跑着就 `pg_restore --clean` 或 TRUNCATE；验收前删旧镜像/旧备份。

---
## 7. 重启与升级

### 7.1 `make restart`（重启 api）时正在生成的任务

v9 的排空语义（实现见 `backend/src/backend/services/job_drain.py`）：

1. 收到 `SIGTERM` → `/readyz` 转 `503`（摘流，不再派新活）；
2. 停止接收新的生成任务（接口返回 503）；
3. 等在途任务最多 `PRDWRIGHT_DRAIN_TIMEOUT` 秒（默认 30，这是上限不是必等）；
4. 到点仍未跑完的任务被标成**已取消**并落终态，SSE 订阅者收到 `error` 事件；
5. flush 日志后退出。

compose 的 `stop_grace_period` 是 **45 秒**，必须保持大于上面的等待上限；否则进程会在写完
终态之前被 `SIGKILL`，任务又变回 `running` 的尸体。

日志里能看到这次重启影响了几个任务：

```bash
# 一次性扫日志（`make logs` 是 `-f` 跟随，不适合管道）
docker compose logs api | grep -E "drain_started|cancel_in_flight|drain_finished"
```

### 7.2 升级

```bash
# 1) 先备份（§6）
# 2) 拉代码 / 切到目标提交
# 3) 重建并替换
make up
```

重启会让在途任务有确定结局（跑完或被取消），不会凭空消失。

#### 从 SQLite 时代（v9 之前）升级上来

这一段是**升级**独有的，全新安装不需要看。v9 把后端换成 Postgres 单一后端，所以旧 `.env`
与旧镜像**不能直接沿用**：

```bash
# 1) 先确认旧 .env 里有没有 Postgres 三件套；没有就补上（否则 compose 直接起不来，
#    报错只有一句 "required variable POSTGRES_PASSWORD is missing a value"）
grep -E '^(POSTGRES_USER|POSTGRES_PASSWORD|POSTGRES_DB)=' .env || \
  printf '\nPOSTGRES_USER=prdwright\nPOSTGRES_PASSWORD=%s\nPOSTGRES_DB=prdwright\n' \
    "$(openssl rand -base64 24)" >> .env

# 2) 必须重建镜像：旧镜像的默认 DATABASE_URL 是 sqlite 形态，api 会拒绝启动
make build

# 3) 起栈（首次启动由 api 自己把迁移跑到 head）
make up
```

三条要知道的边界：

- **旧数据不迁移**：v9 之前那个装 SQLite 库文件的 `prdwright_data` 卷不再被挂载、也不再被读取
  （见 §3 的卷说明）。要保留历史数据，请按 §6 把旧 `.db` 备份留档，再按上面的步骤起新库。
- **`DATABASE_URL` 不用手写**：compose 用 `POSTGRES_*` 拼出 `postgresql+psycopg://…@db:5432/…`；
  只有在要连外部数据库时才在 `.env` 里显式覆盖它。
- **主密钥沿用旧的**：`prdwright_secrets` 卷不需要重建，换库不影响它；反过来，删掉它等于所有
  已录入的模型密钥作废（§5）。


### 7.2.1 挂在 nginx-proxy 后面时（HTTPS 由代理层终止）

若这台机器的 HTTPS 由上游的 `nginx-proxy` + `acme-companion` 提供，本栈的 `web` 需要**额外的
覆盖文件**声明域名并加入 `web-proxy` 外部网络：

```bash
# 1) 一次性：从模板复制成自己的覆盖文件（**不进版本控制**，域名与邮箱是部署方私有配置）
cp compose.proxy.example.yaml compose.proxy.yaml
#    填好 VIRTUAL_HOST / LETSENCRYPT_HOST / LETSENCRYPT_EMAIL

# 2) 之后**每次**操作都带两个文件
docker compose -f compose.yaml -f compose.proxy.yaml up -d
docker compose -f compose.yaml -f compose.proxy.yaml build
make up COMPOSE_FILES="compose.yaml compose.proxy.yaml"      # 用 make 的等价写法
```

**这一步漏了不会报错，只会断站**：只带 `compose.yaml` 重建容器，会丢掉 `VIRTUAL_HOST` 与
`web-proxy` 网络，`nginx-proxy` 就不再为这个域名生成站点，浏览器表现为：

| 现象 | 真实原因 |
| --- | --- |
| `ERR_SSL_UNRECOGNIZED_NAME_ALERT` | 代理按 SNI 找不到站点，只好拿默认证书回应（SNI 与证书域名不匹配） |
| `502 Bad Gateway` | 域名站点还在，但后端容器没就绪或没被代理发现 |
| 页面能开但接口 404 | 容器重建后换了网络，代理里的 upstream 失效 |

自查两条命令：

```bash
docker inspect prdwright-web-1 --format '{{range .Config.Env}}{{println .}}{{end}}' | grep VIRTUAL_HOST
docker inspect prdwright-web-1 --format '{{range $k, $v := .NetworkSettings.Networks}}{{println $k}}{{end}}'
```

第一条必须有输出，第二条必须同时有 `prdwright_net` 与 `web-proxy`。缺了就用上面第 2 步的命令重新拉起。

> **按 IP 访问 `https://<公网IP>/` 永远会报证书错**——证书只签了域名，IP 不在 SAN 里。
> 这不是故障，直接改用 `https://<你的域名>/` 即可。


### 7.3 回滚

回滚有独立的一节：[`§6.7`](#67-回滚预案切换当天照这个做) 与完整手册 [`ROLLBACK.md`](./ROLLBACK.md)。
这里只重复三条最容易被违反的：

- **代码**：切回上一个提交后**必须** `make up` 重建；但「用 `make up` 重建当前代码」不是回滚——
  回滚要用 §6.7 场景 A 的 `--no-build` + 旧镜像 tag，判据是容器镜像 ID 等于回滚 tag 的 ID。
- **数据**：动过表结构（`alembic_version` 变了）就必须按 §6.4 / 场景 B 恢复备份。`git revert` 只回退代码，
  回退不了已经被迁移过的库；恢复旧备份后**也必须**起旧镜像，否则新代码会把库再迁移一次。
- **顺序**：停新 → （需要时）恢复数据 → 起旧。任何时刻不要让两个版本的 api 同时连同一个库。

---

## 8. 常见问题

| 症状 | 原因与处理 |
| --- | --- |
| `env file ... .env not found` | 没建 `.env`。§1.1 |
| api 反复重启，日志说「找不到主密钥」 | 没生成密钥，或生成时用了 root（属主不对）。按 §1.2 用 `--user 10001:10001` 重来；已存在就先删掉再生成。 |
| api 起不来，日志说「主密钥存在但运行用户读不到」 | 文件属主不是 10001，或权限不是 600/640。 |
| 端口 80 被占用 | 在 `.env` 里设 `PRDWRIGHT_HTTP_PORT=8080`，然后访问 `http://localhost:8080/`（`/admin/`、`/api/` 同前缀）。 |
| `/` 正常但 `/api/` 502 | api 没起来或正在重启。`make logs`；web 会一直服务静态页，所以页面打得开不代表后端健康。 |
| `/readyz` 一直 503 | 看响应体的 `checks`：`database` 失败多与卷权限/磁盘有关，`skills` 失败说明镜像里的技能包不完整（重建镜像，不要挂载覆盖）。 |
| 登录接口 200，但下个请求又是未登录 | `AUTH_COOKIE_SECURE=true` 却用 HTTP 访问。纯 HTTP 必须 `false`；上前置 HTTPS 后再改 `true`。 |
| 生成报「还没有模型配置」 | 首次部署没配模型供应商。§3。 |
| `/health` 显示 `degraded`（llm 项 false） | 同上。另外技能包缺失也会让它是 degraded，但 `/readyz` 会拦下这种。 |
| 备份报「数据卷不存在」 | 这套栈还没起过，或卷被 `down -v` 删了。先起栈并等 db 健康、`/readyz` 200（§2）。 |
| 备份报「找不到主密钥」 | 密钥卷里没有 `master.key`。§1.2 生成后再备份；没有密钥的备份解不开密文，脚本因此拒绝。 |
| 备份报「镜像不存在」 | 还没构建过 api 镜像（校验主密钥要用），或没拉 `postgres:<tag>`（`pg_dump` 要用）。`make build` + `docker pull postgres:16`。 |
| 备份报「`pg_restore --list` 解析不了」 | 归档不完整（磁盘满 / dump 期间库不可达）。看 §6.2 的退出码与提示后重跑。 |
| 备份报「表数与源库不一致」 | dump 期间结构被改过（例如迁移正在跑）。等部署稳定再备份。 |
| api 起不来，日志说「数据库在 60s 内没有就绪」 | db 没健康，或凭据与已初始化的 `pgdata` 卷不一致。`docker compose ps db`、`docker compose logs db`，必要时按 §6.4 恢复或 `down -v` 从零（后者会删数据）。 |
| 想看实时数据库 | 不能直接双击。用 `docker compose exec db psql -U prdwright -d prdwright` 查，或用 §6 的备份产物。 |
| Windows 上想改成 `D:\data` 挂载 | 不要。见 §5 最后一条。 |

---

## 9. 部署件清单与延伸阅读

| 文件 | 作用 |
| --- | --- |
| `deploy/compose.yaml` | 唯一编排：三个服务（db / api / web）、三个命名卷、网络、探针、排空宽限 |
| `deploy/.env.example` | 每个可调变量的中文说明与默认值 |
| `deploy/docker/api.Dockerfile` 与 `deploy/docker/api-entrypoint.sh` | 应用镜像（单 worker、非 root、主密钥缺失即拒启动） |
| `deploy/docker/web.Dockerfile` 与 `deploy/docker/nginx.conf` | 静态站点镜像与三路反代、SSE 不缓冲 |
| `deploy/backup.py` | 备份脚本：`pg_dump -Fc` + `pg_restore --list` 自检 + sha256（§6） |
| `deploy/drill.py` | 回滚预演练：本机库上「备份 → 破坏 → 恢复」，记每步耗时（§6.7、`make drill`） |
| `deploy/ROLLBACK.md` | 回滚预案：备份清单与保留期限、三个场景的逐步命令与判据、演练记录 |
| `deploy/Makefile` | 常用命令的简写（起/停/日志/重启/进容器/备份/演练） |
| `deploy/backups/` | 备份产物目录（**内容**被版本控制忽略，目录本身靠 `.gitkeep` 入库；产物由 `backup.py` 写入，可整目录删除） |

设计依据与取舍：[`../docs/architecture/ARCHITECTURE.md`](../docs/architecture/ARCHITECTURE.md)（§3.6 部署形态、§5 取舍）。
现状排期：[`../docs/architecture/roadmap.md`](../docs/architecture/roadmap.md)。
