# PRDwright 部署（v9）

两个镜像、一个入口、三个命名卷。**Linux 与 Windows 用同一份编排、同一组命令**——
持久化全在 Docker 命名卷里，没有宿主目录挂载，所以不需要平台的 override 文件
（为什么必须这样，见 [`../docs/architecture/v9/ARCHITECTURE.md`](../docs/architecture/v9/ARCHITECTURE.md) §4.2）。

第一次部署的完整路径：**准备 `.env` 与主密钥 → 起栈 → 进管理后台配一个模型供应商 → 登录使用**。
下面的命令都可以直接粘贴，Docker 命令在 PowerShell 与 bash 里一致；只有复制文件那一步不同。

---

## 0. 前置条件

| 项 | 要求 |
| --- | --- |
| Docker | Docker Engine 或 Docker Desktop（Linux 容器模式），Compose v2 及以上 |
| 宿主端口 | `80` 空闲（被占用时见 §9，改一个变量即可） |
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

打开 `.env`，**至少改这两项**：

| 变量 | 改什么 |
| --- | --- |
| `INIT_ADMIN_PASSWORD` | 首个管理员密码（`users` 表为空时自动创建该账号，且直接是管理员） |
| `INVITE_CODE` | 注册邀请码（`invite_codes` 表为空时自动播种） |

其余变量都有中文说明与默认值，可以先不动。`.env` 已被版本控制忽略，不要提交。

### 1.2 生成主密钥（只需一次）

主密钥决定库里所有模型密钥能不能解开。**缺少它时 api 拒绝启动**（不会自动生成——
自动生成会让换机后的旧密文永久解不开），启动日志里也会打印同样的命令。

密钥生成器在应用镜像里，所以先构建 api 镜像（不启动容器）：

```bash
docker compose build api
```

然后生成并写进密钥卷：

```bash
docker run --rm --user 10001:10001 \
  -e PRDWRIGHT_MASTER_KEY_PATH=/secrets/master.key \
  -v prdwright_secrets:/secrets \
  --entrypoint python prdwright-api:local \
  /app/scripts/keygen/generate_keys.py
```

Windows PowerShell 没有 `\` 续行，直接用一行：

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
docker compose up -d --build
```

Windows 用**同一条命令**——命名卷不是宿主目录，两个平台的路径与文件锁语义一致。

看状态与日志：

```bash
docker compose ps
docker compose logs -f api
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
| 起栈 / 改完代码重建 | `docker compose up -d --build` |
| 停栈（**保留数据**） | `docker compose down` |
| 重启 api（触发排空） | `docker compose restart api` |
| 看日志 | `docker compose logs -f api` |
| 看状态与健康 | `docker compose ps` |
| 进 api 容器 | `docker compose exec api sh` |
| 看卷 | `docker volume ls --filter name=prdwright` |
| 看生效的完整编排 | `docker compose config` |

有 `make` 的环境（Linux / macOS / WSL）可以用等价的 `deploy/Makefile`：
`make up`、`make logs`、`make restart`、`make down`、`make shell`。
Windows 原生 PowerShell 没有 `make`，直接用上表命令。

---

## 5. 数据、日志、密钥在哪

| 卷（固定名） | 容器内挂载点 | 内容 | 丢了会怎样 |
| --- | --- | --- | --- |
| `prdwright_data` | `/var/lib/prdwright` | SQLite 库 `prdwright.db`（含 `-wal` / `-shm` 边车） | **业务数据全丢** |
| `prdwright_logs` | `/var/log/prdwright` | 轮转日志文件 | 丢历史日志（`app_logs` 表里还有一份派生数据） |
| `prdwright_secrets` | `/secrets`（**只读**） | `master.key` | 库里所有模型密钥都解不开 |

- 日志同时写 stdout，所以 `docker compose logs` 是第一现场；卷里的文件供跨容器重建后回查。
- **实时库文件不能双击查看**，这是命名卷的代价。可见性由备份产物补上：备份落到
  `deploy/backups/`（已被版本控制忽略，由备份脚本创建）。
- 不要往 compose 里加宿主目录挂载。Windows 上的宿主目录经桥接层会丢掉文件锁与共享内存映射，
  SQLite 会随机报 I/O 错误甚至静默丢最近写入。换文件系统才能解决，改配置解决不了。

---

## 6. 备份、恢复与回滚

### 6.1 纪律（比命令更重要）

- **迁移之前必须先有备份**；备份没做成，不许 `docker compose up -d`。
- **删列 / 改类型之后的回滚方式是恢复备份，不是 `git revert`**：回退代码后的真实状态是
  「新结构 + 旧代码」，旧代码读不到那一列，启动即失败。代码可以回退，数据不会自己回去。
- 备份要能**在另一台机器上**被服务读起来，才算备份。

### 6.2 备份命令（脚本见 issue #18）

> **备份脚本由 issue #18 提供，本版尚未合入。** 合入后的目标形态是一条命令：
>
> ```bash
> cd deploy && make backup
> # → deploy/backups/prdwright-<日期>.db + 同名 .sha256（含 VACUUM INTO 与 integrity_check）
> ```
>
> 在它合入之前，可以用下面两条临时命令把库完整拷出来（**这不是长期方案**：没有 sha256、
> 没有自检输出）。备份文件请另存到宿主上你信任的位置。

```bash
mkdir -p backups

# 1) 在容器内用 VACUUM INTO 生成一份在线一致快照（不要直接 cp 库文件：
#    开了 WAL，直接复制会静默丢掉最近几分钟的写入）
docker compose exec api python -c "import sqlite3; s=sqlite3.connect('/var/lib/prdwright/prdwright.db'); s.execute(\"VACUUM INTO '/var/lib/prdwright/prdwright-backup.db'\"); s.close()"

# 2) 把它从数据卷拷到宿主（容器名固定为 prdwright-api-1）
docker cp prdwright-api-1:/var/lib/prdwright/prdwright-backup.db backups/prdwright-manual.db
docker compose exec api rm -f /var/lib/prdwright/prdwright-backup.db
```

### 6.3 恢复（也是「回滚」）

```bash
docker compose down                     # 停栈，保留卷

# 用一次性容器把备份放回数据卷；卷名固定为 prdwright_data
docker run --rm -v prdwright_data:/data -v "${PWD}/backups:/backup" alpine \
  sh -c "rm -f /data/prdwright.db-wal /data/prdwright.db-shm && cp /backup/prdwright-<日期>.db /data/prdwright.db && chown 10001:10001 /data/prdwright.db"

docker compose up -d
```

- 恢复前删掉 `-wal` / `-shm`：留着旧边车文件，SQLite 会把它们和新库拼在一起。
- 备份来自另一台机器时，**同时**把那份备份对应时期的 `master.key` 放回 `prdwright_secrets`
  卷（否则库里的模型密钥解不开）。
- 恢复后先打开 `/readyz` 与 `/admin/` 确认，再继续操作。

### 6.4 `docker compose down -v` 会删数据

| 命令 | 容器 | 网络 | **卷（数据/日志/密钥）** |
| --- | --- | --- | --- |
| `docker compose down` | 删 | 删 | **保留** |
| `docker compose down -v` | 删 | 删 | **连卷一起删，无法撤销** |

`-v` 只该用在「反正我要从零重来」的时候。它删的不只是数据库，还有主密钥——两者一起没，
等于所有加密配置永久作废。普通停栈、升级、重启都**不要**带 `-v`。

---

## 7. 重启与升级

### 7.1 `docker compose restart api` 时正在生成的任务

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
docker compose logs api | grep -E "drain_started|drain_cancelled|drain_finished"
```

### 7.2 升级

```bash
# 1) 先备份（§6）
# 2) 拉代码 / 切到目标提交
# 3) 重建并替换
docker compose up -d --build
```

重启会让在途任务有确定结局（跑完或被取消），不会凭空消失。

### 7.3 回滚

- **代码**：切回上一个提交后 `docker compose up -d --build` 重建镜像。
- **数据**：如果这次发布动过表结构，按 §6.3 恢复备份。`git revert` 只回退代码，回退不了
  已经被迁移过的库。

---

## 8. 升级到下一版（PostgreSQL）时的预留位

compose 里有一个 **默认关闭** 的 `db` 服务（`profiles: ["db"]`）：

- 它不会随 `docker compose up -d` 启动，v9 的数据库仍是 `prdwright_data` 卷里的 SQLite；
- 现在启动它（`docker compose --profile db up -d db`）也不会让应用改用 PG——api 的
  `DATABASE_URL` 没有指向它，服务之间也没有依赖关系；
- 它的存在只为下一版接 PostgreSQL 时不必重写编排。`.env.example` 里的 `POSTGRES_*`
  同样是「v9 不使用，见 v10」。

---

## 9. 常见问题

| 症状 | 原因与处理 |
| --- | --- |
| `env file ... .env not found` | 没建 `.env`。§1.1 |
| api 反复重启，日志说「找不到主密钥」 | 没生成密钥，或生成时用了 root（属主不对）。按 §1.2 用 `--user 10001:10001` 重来；已存在就先删掉再生成。 |
| api 起不来，日志说「主密钥存在但运行用户读不到」 | 文件属主不是 10001，或权限不是 600/640。 |
| 端口 80 被占用 | 在 `.env` 里设 `PRDWRIGHT_HTTP_PORT=8080`，然后访问 `http://localhost:8080/`（`/admin/`、`/api/` 同前缀）。 |
| `/` 正常但 `/api/` 502 | api 没起来或正在重启。`docker compose logs api`；web 会一直服务静态页，所以页面打得开不代表后端健康。 |
| `/readyz` 一直 503 | 看响应体的 `checks`：`database` 失败多与卷权限/磁盘有关，`skills` 失败说明镜像里的技能包不完整（重建镜像，不要挂载覆盖）。 |
| 登录接口 200，但下个请求又是未登录 | `AUTH_COOKIE_SECURE=true` 却用 HTTP 访问。纯 HTTP 必须 `false`；上前置 HTTPS 后再改 `true`。 |
| 生成报「还没有模型配置」 | 首次部署没配模型供应商。§3。 |
| `/health` 显示 `degraded`（llm 项 false） | 同上。另外技能包缺失也会让它是 degraded，但 `/readyz` 会拦下这种。 |
| 想看实时数据库文件 | 不能直接双击。用 §6 的备份产物看。 |
| Windows 上想改成 `D:\data` 挂载 | 不要。见 §5 最后一条。 |

---

## 10. 部署件清单与延伸阅读

| 文件 | 作用 |
| --- | --- |
| `deploy/compose.yaml` | 唯一编排：两个服务 + 一个默认关闭的 db 预留位、卷、网络、探针、排空宽限 |
| `deploy/.env.example` | 每个可调变量的中文说明与默认值 |
| `deploy/docker/api.Dockerfile` 与 `deploy/docker/api-entrypoint.sh` | 应用镜像（单 worker、非 root、主密钥缺失即拒启动） |
| `deploy/docker/web.Dockerfile` 与 `deploy/docker/nginx.conf` | 静态站点镜像与三路反代、SSE 不缓冲 |
| `deploy/Makefile` | 常用命令的简写（起/停/日志/重启/进容器） |
| `deploy/backups/` | 备份产物目录（被版本控制忽略；由 #18 的脚本创建） |

设计依据与取舍：[`../docs/architecture/v9/ARCHITECTURE.md`](../docs/architecture/v9/ARCHITECTURE.md)。
路线图与各票落点：[`../docs/architecture/v9/roadmap.md`](../docs/architecture/v9/roadmap.md)。
