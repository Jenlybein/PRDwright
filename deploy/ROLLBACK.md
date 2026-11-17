# 回滚预案（PRDwright v9）

切换当天出问题怎么办：**先备份，再造镜像 tag；回滚时按下面三个场景之一，按顺序敲命令。**
本文是给人照做的手册，不是设计论证。谁都能读：没参与本次改造的值班人员，照着 §4 的清单记录现状、按 §5 选场景、按对应场景的步骤执行即可。

**一句话**：回滚是「停新、起旧」的**串行**动作，数据只能往回走；它和产品里的「文档版本历史」不是一回事。
这两条边界分别在 §1 与 §2，先读它们，再动手。

- 备份与恢复的机制、命令来源：[`README.md`](./README.md) §6（备份/恢复/校验）
- 迁移纪律（「回滚靠备份，不靠 downgrade」）：[`../docs/agents/conventions.md`](../docs/agents/conventions.md) §3、§7
- 「不是并行运行」的设计取舍：[`../docs/architecture/state-flow-design.md`](../docs/architecture/state-flow-design.md)
- 演练的可重复执行入口：`deploy/drill.py`（`make drill`），记录见 §9

---

## 目录

0. [适用边界](#0-适用边界)
1. [边界一：回滚 ≠ 并行](#1-边界一回滚--并行)
2. [边界二：回滚 ≠ 文档版本历史](#2-边界二回滚--文档版本历史)
3. [切换当天备份什么、留多久](#3-切换当天备份什么留多久)
4. [回滚前 10 分钟：记录基线](#4-回滚前-10-分钟记录基线)
5. [决策表：选哪个场景](#5-决策表选哪个场景)
6. [场景 A：只回退应用镜像（数据库未变）](#6-场景-a只回退应用镜像数据库未变)
7. [场景 B：应用 + 数据库一起回退](#7-场景-b应用--数据库一起回退)
8. [场景 C：只回退某一张表](#8-场景-c只回退某一张表)
9. [演练记录（本机开发环境，非生产）](#9-演练记录本机开发环境非生产)
10. [红线清单](#10-红线清单)

---

## 0. 适用边界

本文覆盖三种「切换当天会遇到的回退」：

| 场景 | 什么时候用 | 动什么 | 典型耗时量级 |
| --- | --- | --- | --- |
| A（§6） | 新版本**代码**有问题，本次发布**没跑过结构迁移**（`alembic_version` 没变） | 只换镜像 | 分钟级（起栈 + 健康检查） |
| B（§7） | 本次发布**跑过迁移**，或新代码把数据写坏了 | 换镜像 + 恢复整库备份 | 分钟到十分钟级（看库大小） |
| C（§8） | 只有**一张表**的数据被误操作（结构没变），其余表正常 | 只恢复该表的数据 | 分钟级 |

**本文不覆盖**：给某一条记录做「手工修数据」（那属于数据订正，不是回滚，必须有第二人复核）；
把旧 SQLite 库迁回新栈（旧库是历史归档，`README.md` §6.6 已明确不做自动迁移）。

---

## 1. 边界一：回滚 ≠ 并行

**回滚 = 停掉新版本，起回旧版本；数据单向（只能恢复备份，不能让旧代码顺手继续写新结构）。**
这不是「两个版本一起跑、慢慢切流量」——后者叫并行运行，本项目明确不做，理由在设计文档里（`../docs/architecture/state-flow-design.md`：
「一次性切换，不能靠并行运行退回」「新旧版本并行运行 … 指**后端/数据库版本**并行，不是文档版本」）。

为什么不能并行，三条都是硬事实：

1. **启动即迁移**。api 每次启动都会 `upgrade head`；两个版本连同一个库时，先启动的那个会按自己的迁移链改结构，
   另一个进程随后要么改回去、要么因 `SchemaMismatchError` 拒绝启动（`core/schema.py` 会在结构不符时拒启动并打印差异）。
2. **迁移是单向门**。删列 / 改类型之后，旧代码读不到那一列或类型不匹配，启动即失败。代码可以回退，数据不会自己回去
   （[`../docs/agents/conventions.md`](../docs/agents/conventions.md) §3）。
3. **运行期状态全在进程内**：单 worker、任务运行时（`_RUNTIMES`）、`JobBus`、限额快照、连接池都不跨进程共享。
   两个版本同时跑会得到两份互不知道对方存在的任务状态，SSE 订阅者会断。

**可观测判据（回滚期间任何时刻检查）：**

```bash
docker compose ps
```

- 期望：`db` / `api` / `web` 各自**最多一个**运行中的容器；不存在第二套 `api`（没有全套「新旧并行」）。
- 期望：`api` 在回滚过程中是 0 个（`stopped`/`removed`）或 1 个（健康）。不允许「2 个 api 同时连着同一个库」。

正确的动作顺序永远是：**停新 → （如果走 B）恢复数据 → 起旧**。反过来做（先起旧、后停新）就是并行。

---

## 2. 边界二：回滚 ≠ 文档版本历史

这一条最容易混，单独写一节。

**文档版本历史（`document_versions`）是产品功能**：旧版本的正文行只会新增、不会被删掉，版本指针只前进不回拨；
整篇生成 / 修复 / `checkpoint` / `restore` 都是**追加一个新版本**，手改 / `optimize` / `clarify` 就地更新当前版本、
同样不删旧行（[`../docs/agents/conventions.md`](../docs/agents/conventions.md) §7，
[`../GLOSSARY.md`](../GLOSSARY.md) 的「版本」与「恢复」词条）。它保证的是「用户在界面里能翻到旧版本」，服务不停、不需要备份。

**数据库回滚（本文）是运维动作**：把整库退回到某一时刻的备份。它会**物理地丢掉**回滚点之后写进去的所有行——
包括那些只增不删的文档版本行。两者不是替代关系，而是两层：版本历史是业务语义，备份是数据保险。

| | 文档版本历史（`document_versions`） | 数据库回滚（本文） |
| --- | --- | --- |
| 谁在用 | 用户 / 产品功能 | 值班 / 运维 |
| 作用对象 | 单份文档的正文版本链 | 整个库（或一张表） |
| 操作方式 | 页面上的「恢复历史版本」→ **追加**一版 | 命令行 `pg_restore` → **整库**替换 |
| 会丢东西吗 | 不会；旧版本行只增不删 | 会：回滚点之后的所有写入都没了（包括新追加的版本行） |
| 需要停服务吗 | 不需要 | 需要（先停 api） |
| 数据来源 | 库里已有的旧版本行 | 库外的 `pg_dump -Fc` 归档 |

三条实践结论：

1. **「有版本历史」不等于「不用备份」**——版本历史本身也住在同一个 Postgres 里，库坏了 / 卷丢了 / 误执行 `down -v`，
   版本链会一起没。备份链见 §3。
2. **不要用版本历史当回滚工具**。产品层的「恢复」只能把某份文档的正文往回写，不能撤销一次误跑的 DDL、不能救回被
   `DROP TABLE` 的表，也不能撤销一次错误的批量 UPDATE。那三件事只能走本文的场景 B / C。
3. **反过来也不要拿数据库回滚去处理「文档版本对不上」**。应用的写路径不会删掉旧版本行，正常使用不会「弄丢历史版本」；
   遇到这类现象先查业务与代码（版本来源、指针、任务收尾），不要整库回滚——整库回滚会把回滚点之后**所有**文档的新版本
   一起抹掉，症状比问题本身更大。

---

## 3. 切换当天备份什么、留多久

### 3.1 清单

| # | 对象 | 切换当天的动作（命令） | 产物 | 放在哪 | 保留期限 |
| --- | --- | --- | --- | --- | --- |
| 1 | **数据库** | `cd deploy && make backup` | `prdwright-<日期>.dump` + 同名 `.sha256`（`pg_dump -Fc`） | `deploy/backups/` **以及另一台机器/另一块盘各一份** | **至少 90 天**，且**至少保留最近 3 次切换**的各一份（两者取更长的） |
| 2 | **应用镜像** | 见 §3.3：`docker tag` 旧镜像 + `docker save` | `prdwright-api:<回滚tag>`、`prdwright-web:<回滚tag>`、一个 `.tar`（约 1.3 GB） | 本机 `docker images` + 与库备份同介质的离线 tar | 到**下一次发布验收通过**为止，最短 **30 天** |
| 3 | **`deploy/.env`** | 复制到密码管理器/离线加密介质（见 §3.4） | 一份文本（含 `POSTGRES_PASSWORD` 等） | **不进备份包**；与库备份分开保管 | 与该版本镜像/备份同寿命，直到这一版不再需要回退 |
| 4 | **`master.key`** | 从 `prdwright_secrets` 卷导出（见 §3.5） | `master.key.tar` | **不进备份包**；单独离线介质 | **永久**，每一把都留（换密钥也只增不删） |
| 5 | **代码版本** | `git tag release-<日期>`，并记录 commit | git tag + commit hash | git 仓库 | 永久 |
| 6 | 迁移版本（`alembic_version`） | 记录在发布记录里（一条 `select version_num`） | 文本 | 发布记录 | 永久（与第 5 项绑定） |

**不进备份包的两样东西**：`deploy/.env` 里的口令、`master.key`。原因：备份包会被复制到别的机器/介质，
把「解库的钥匙」和「库本身」装在一起，等于一份备份就是一个完整的入侵工具；而主密钥一旦泄漏，库里所有模型密钥都能被解开。
`README.md` §1.2 第 4 点也写了这条纪律。

### 3.2 数据库备份

```bash
cd deploy
make backup
```

预期输出（数字随库变化，这一行是判据）：

```
[备份] ✓ 备份完成
        文件         …/deploy/backups/prdwright-<日期>.dump
        字节数       <N>
        sha256       <64 位十六进制>
        自检结论     ok（归档可解析、表数 <T> 与源库一致、alembic_version=<版本>）
        校验和文件   …/prdwright-<日期>.dump.sha256
```

判据（三条都过才算「有了备份」）：

1. 退出码 `0`（非零时按 `README.md` §6.2 的退出码表定位：`2` 前置检查 / `3` 导出 / `4` 自检 / `5` 写盘校验）；
2. 自检结论是 `ok`，并且**把「表数」与「`alembic_version`」抄进发布记录**（回滚后要拿它比对）；
3. `sha256` 与 `.sha256` 文件一致（校验命令见 `README.md` §6.3）。

**异地副本**：把 `.dump` 与 `.sha256` 一起复制到另一台机器或另一块盘（走共享介质时先加密，口令单独保管）。
「备份要能在另一台机器上被读起来，才算备份」（`README.md` §6.1）。

### 3.3 镜像：先给旧版本打一个不会被覆盖的 tag

切换**之前**（新版本镜像还没构建时）执行：

```bash
# 回滚 tag 用日期，别用 local——local 会被下一次构建覆盖
docker tag prdwright-api:local prdwright-api:rollback-<日期>
docker tag prdwright-web:local prdwright-web:rollback-<日期>

# 记下镜像 ID，回退后要靠它证明「跑的是旧镜像」而不是同一 tag 的新镜像
docker image inspect -f '{{.Id}}' prdwright-api:rollback-<日期>
docker image inspect -f '{{.Id}}' prdwright-web:rollback-<日期>

# 离线副本（约 1.3 GB，两个镜像一起）
docker save prdwright-api:rollback-<日期> prdwright-web:rollback-<日期> -o prdwright-images-<日期>.tar
```

PowerShell 只有最后一条要换写法：

```powershell
docker save prdwright-api:rollback-<日期> prdwright-web:rollback-<日期> -o "$env:USERPROFILE\prdwright-images-<日期>.tar"
```

判据：两条 `docker image inspect` 都输出 `sha256:…`（记进发布记录）；`docker save` 退出码 `0`、tar 文件几 GB 以内、
换一台机器 `docker load -i prdwright-images-<日期>.tar` 后 `docker images | Select-String rollback-<日期>` 看得见两个 tag。

> `.env` 里如果设了 `PRDWRIGHT_IMAGE_TAG`，镜像名就不是 `local`；按实际 tag 打回滚 tag，不要改 `.env`（见 §6 边界）。

### 3.4 `deploy/.env` 怎么留

`.env` 里有 `POSTGRES_PASSWORD` / `INIT_ADMIN_PASSWORD` / `INVITE_CODE`，**是密钥，不进备份包**：

1. 把 `deploy/.env` 复制进密码管理器（或公司离线加密介质），标上「对应 commit + 日期」；
2. 不要提交到 git（`.gitignore` 已排除 `.env`）；
3. 恢复旧备份到同一数据卷时，`.env` 里的 `POSTGRES_PASSWORD` 必须与那份库初始化时用的一致——Postgres 的口令存在库卷里，
   改 `.env` 不会改库里的口令，症状是 api 报认证失败（`README.md` §8 最后几行）。

判据：密码管理器里能按「commit + 日期」找到这一版 `.env`，且与当前 `deploy/.env` 的 `POSTGRES_PASSWORD` 一致（没改过就一致）。

### 3.5 `master.key` 怎么单独导出

主密钥在命名卷 `prdwright_secrets` 里，**换机/恢复旧备份时必须用生成那份库时的同一把**。
切到离线介质（该目录**不要**放在 `deploy/backups/`，也不要和 dump 打成一个包）：

```bash
# 1) 建一个仓库外的目录（示例路径，放到离线介质即可）
mkdir -p "$HOME/prdwright-offline-secrets"

# 2) 从密钥卷导出（postgres 镜像里就有 tar；该卷只读挂载）
docker run --rm \
  -v prdwright_secrets:/secrets:ro \
  -v "$HOME/prdwright-offline-secrets:/out" \
  postgres:16 tar -C /secrets -cf /out/master.key-<日期>.tar master.key

# 3) 复核包里确实只有一把密钥
docker run --rm -v "$HOME/prdwright-offline-secrets:/in:ro" postgres:16 tar -tf /in/master.key-<日期>.tar
```

PowerShell 换 `$HOME/prdwright-offline-secrets` 为 `"$env:USERPROFILE\prdwright-offline-secrets"`。

预期：步骤 2 退出码 `0`、目录里出现 `master.key-<日期>.tar`；步骤 3 打印一行 `master.key`。

**放回（只在换机/恢复旧备份时需要）**：

```bash
docker run --rm \
  -v prdwright_secrets:/secrets \
  -v "$HOME/prdwright-offline-secrets:/in:ro" \
  postgres:16 sh -c "tar -C /secrets -xf /in/master.key-<日期>.tar \
    && chown 10001:10001 /secrets/master.key && chmod 600 /secrets/master.key"
```

判据：退出码 `0`；`docker run --rm -v prdwright_secrets:/secrets:ro postgres:16 ls -l /secrets/master.key`
输出属主 `10001`、权限 `-rw-------`（否则 api 读不到，症状与「密钥不存在」几乎一样）。

### 3.6 代码版本

```bash
git tag release-<日期>
git rev-parse --short HEAD
```

判据：`git tag --list 'release-*'` 里看得见 `release-<日期>`，`git rev-parse` 的 hash 与发布记录一致（回退时 `git checkout` 同一个 hash）。

### 3.7 保留期限与清理纪律

| 产物 | 保留期限 | 什么时候可以删 |
| --- | --- | --- |
| 数据库 `.dump` / `.sha256` | 至少 **90 天**，且至少保留**最近 3 次切换**各一份 | 有更新的一份备份、且该备份通过过一次恢复演练（§9 或生产假恢复）之后 |
| 镜像 tag + `.tar` | 到下一次发布验收通过，最短 **30 天** | 新版本已验收，且旧镜像不再作为回退目标 |
| `master.key` 的每一把 | **永久** | 永不删除（除非确认那把密钥对应的密文已全部作废） |
| `.env` 各版本 | 与对应镜像/备份同寿命 | 该版本不再需要回退时 |
| 代码 tag | 永久 | 不删（git 历史） |

清理由**发布负责人**在每次发布收尾时执行一次；核对命令（按时间倒序看最近产物）：

```bash
ls -lt deploy/backups
```

```powershell
Get-ChildItem deploy/backups | Sort-Object LastWriteTime -Descending
```

删之前先做一次「可用性判据」：`sha256` 校验通过（`README.md` §6.3）**且**这份备份在 §9 的演练里恢复过（或做过一次
「恢复到临时库再核对表数」的假恢复）。只看文件在不在，不算可用。

---

## 4. 回滚前 10 分钟：记录基线

**先记录，再动手。** 下面每一条都要把实际输出抄进发布记录（这是回滚后判断「成功没成功」的唯一参照）。

```bash
cd deploy

# 1) 当前代码版本
git rev-parse --short HEAD

# 2) 当前镜像（服务 → 镜像名；回滚后要和第 2 项记录的旧镜像做对比）
docker compose ps --format '{{.Service}} {{.Image}} {{.State}}'

# 3) 数据库迁移版本与表数
docker compose exec -T db psql -U prdwright -d prdwright \
  -c "select version_num from alembic_version" \
  -c "select count(*) from pg_tables where schemaname='public'"

# 4) 就绪状态
curl -fsS http://localhost/readyz

# 5) 是否有在途生成任务（发布前应当等排空；接口需要管理员 Cookie）
curl -fsS http://localhost/api/admin/jobs?status=running
```

PowerShell 用 `Invoke-RestMethod http://localhost/readyz` 代替 `curl`，其余命令一致。

判据：

- 第 2 项输出三行，`db` / `api` / `web` 各一个容器；记下 api 与 web 现在的镜像名（新版本）以及**回滚目标是哪个 tag**；
- 第 3 项输出一个版本号（如 `0001`）与一个表数（如 `15`）；**表数要与备份自检里的一致**；
- 第 4 项是 `"status":"ready"`；不是就先把栈弄绿，别在生病的状态上下手；
- 第 5 项没有 `running` 任务（或已确认排空）。

然后**立刻**做 §3.1 的 1~4 项（备份、打镜像 tag、存 `.env`、导出 `master.key`），再开始发布。

---

## 5. 决策表：选哪个场景

| 症状 | 本次发布跑过迁移吗 | 走哪个场景 |
| --- | --- | --- |
| 页面白屏 / 接口 500 / 生成报错，`/readyz` 仍是 `ready`，`alembic_version` 没变 | 没有 | **A**（§6） |
| 本次发布含迁移（`alembic_version` 变了，或表数/列变了），新版本行为不对 | 有 | **B**（§7） |
| 迁移跑到一半失败 / 库结构半新半旧 / 启动报 `SchemaMismatchError` | 不论 | **B**（§7）：只能恢复备份，禁止手工补迁移 |
| 数据被新代码写坏（例如批量 UPDATE 错了），结构没变 | 可能没有 | **B**（§7，整库恢复）或 **C**（§8，只有一张表时） |
| 只有某张表被误操作（手工 SQL 删了行 / 改错列值），其余表正常，结构没变 | 没有 | **C**（§8） |
| 只是想回退代码、库结构已经变了 | 有 | **不允许只做 A** → 必须 **B** |

**A 与 B 的分界线只有一条：`alembic_version` 有没有变、结构有没有变。** 拿不准就按 B（更重，但不会留下新代码 + 旧库的错配）。

---

## 6. 场景 A：只回退应用镜像（数据库未变）

**适用**：新版本只是代码/前端/技能包问题，本次发布没跑过迁移，`alembic_version` 与发布前一致。
**不适用**：跑过迁移、表结构变了、数据被新代码写坏——那些走 B。

### 步骤

**A1. 停新栈（保留卷）**

```bash
cd deploy
make down
```

预期：输出里每个容器一行 `Removed`；**没有**出现 `volume … removed`。
判据：`docker volume ls --filter name=prdwright` 仍能看到 `prdwright_pgdata` / `prdwright_logs` / `prdwright_secrets` 三个卷。

**A2. 把旧镜像的 tag 指回 compose 认的名字**

```bash
docker tag prdwright-api:rollback-<日期> prdwright-api:local
docker tag prdwright-web:rollback-<日期> prdwright-web:local
```

预期：两条命令都无输出、退出码 `0`。
判据：`docker image inspect -f '{{.Id}}' prdwright-api:local` 的输出与发布记录里 `rollback-<日期>` 的 ID **完全一致**。

> 若 `.env` 里设了 `PRDWRIGHT_IMAGE_TAG`（镜像名不是 `local`），把上面的 `local` 换成那个 tag；
> **不要**为了回退去改 `.env`（那是另一种变量，改了会影响下次构建的命名）。

**A3. 起栈，且不重建镜像**

```bash
docker compose up -d --no-build
```

预期（三行左右）：

```
Container prdwright-db-1  Running
Container prdwright-api-1  Started
Container prdwright-web-1  Started
```

判据：容器都起来且 `--no-build` 没有触发任何构建步骤（输出里没有 `Building` / `Built` 字样）。
**不能**用 `docker compose up -d --build`（那会按当前代码重新构建，等于没回退）。

**A4. 等健康并验证就绪**

```bash
docker compose ps
curl -fsS http://localhost/readyz
```

预期：`api` 状态含 `(healthy)`，`db` 含 `(healthy)`；`readyz` 返回 `{"status":"ready","checks":{…}}`。
判据：退出码 `0` 且 `status` 为 `ready`（首次启动有 40 秒 `start_period`，最多等 1 分钟）。

**A5. 证明「跑的是旧镜像」而不是「同名的重建镜像」**

```bash
# bash / WSL
docker inspect -f '{{.Image}}' $(docker compose ps -q api)
docker image inspect -f '{{.Id}}' prdwright-api:rollback-<日期>
```

```powershell
# PowerShell
docker inspect -f '{{.Image}}' (docker compose ps -q api)
docker image inspect -f '{{.Id}}' prdwright-api:rollback-<日期>
```

判据：两行**完全相同**（都是 `sha256:…`）。这一条是 A 的核心判据——tag 可以被覆盖，镜像 ID 不会。

**A6. 确认数据库没被动过，并抽检业务**

```bash
docker compose exec -T db psql -U prdwright -d prdwright -c "select version_num from alembic_version"
docker compose logs api --tail 50
```

判据：

- `version_num` 与 §4 记录的一致（数据库未变）；
- 日志里没有 `SchemaMismatchError` / `启动失败` / 重启循环；若日志里出现 `迁移目标库：` 那一行，库地址里的口令应当是
  被脱敏成 `***` 的（没有这一行不影响判据，判据以上面的就绪与镜像 ID 为准）；
- 浏览器打开 `http://<机器>/` 能登录、能打开一份已有方案、`/admin/` 可进（人工抽检）。

**A 的边界（不能这样做）**

- 不能只改 `.env` 的 `PRDWRIGHT_IMAGE_TAG` 就宣布回退：tag 若已被新版本覆盖，回退会「看起来成功、跑的是新代码」。
  A5 的镜像 ID 比对就是专门防这个的。
- 不能用 `docker compose up -d --build`；不能带 `-v`（会删卷）。
- 不能在 `alembic_version` 已经变化时用 A（旧代码读新结构会在启动期被 `SchemaMismatchError` 拦下，服务起不来）。
- 回退成功后**先别删**刚做的数据库备份与旧镜像 tag：验收完成前它们还是唯一退路。

---

## 7. 场景 B：应用 + 数据库一起回退

**适用**：本次发布跑过迁移（结构变了），或新代码把数据写坏，需要把库恢复到发布前的备份。
**前提**：§3.1 的备份已做成、且记录过它的 `alembic_version` 与表数；旧镜像 tag 已打好（§3.3）。

### 核心约束

**镜像与备份必须成对。** 恢复一个「旧 revision」的备份之后，如果用**新代码**启动，api 会在启动期 `upgrade head`，
把库重新迁移到新结构——那就不是回滚，而是「在一个旧数据上又跑了一次新迁移」，比原来更糟。
所以顺序是：停新 → 恢复备份 → **起旧镜像**。

### 步骤

**B1. 停栈（保留卷）**

```bash
cd deploy
make down
```

判据同 A1（三个命名卷还在）。

**B2. 只起数据库**

```bash
docker compose up -d db
docker compose ps db
```

预期：`prdwright-db-1` 状态从 `starting` 变成 `(healthy)`。
判据：看到 `(healthy)` 才继续；`docker compose logs db` 无报错。**这一步也会重建 `prdwright_net` 网络**，
后面的 `docker run` 要用它。

**B3. 恢复备份（`pg_restore --clean --if-exists`）**

bash / WSL：

```bash
docker run --rm --network prdwright_net \
  -e PGPASSWORD="$(grep '^POSTGRES_PASSWORD=' .env | cut -d= -f2-)" \
  -v "$PWD/backups:/backup:ro" postgres:16 \
  sh -c "pg_restore --clean --if-exists --no-owner --no-privileges \
         -h db -U prdwright -d prdwright -v /backup/prdwright-<日期>.dump"
```

PowerShell：

```powershell
$pw = (Select-String -Path .env -Pattern '^POSTGRES_PASSWORD=' | ForEach-Object { $_.Line.Split('=',2)[1] })
docker run --rm --network prdwright_net `
  -e "PGPASSWORD=$pw" `
  -v "${PWD}\backups:/backup:ro" postgres:16 `
  sh -c "pg_restore --clean --if-exists --no-owner --no-privileges -h db -U prdwright -d prdwright -v /backup/prdwright-<日期>.dump"
```

预期输出尾部（表多时很长）：

```
pg_restore: creating INDEX "public.ux_chat_turns_job_role"
pg_restore: creating FK CONSTRAINT "public.solutions solutions_user_id_fkey"
```

判据：退出码 `0`，且输出里**没有** `ERROR` 行（`pg_restore` 有错时会打印 `errors ignored on restore: …` 并以非零退出）。

> `--clean --if-exists` 会先删掉现有对象再重建——这正是整库回滚要的动作；**只在 api 停着时做**。
> 只想往空库里追加就不带这两个参数（本场景不适用那种用法）。

**B4. 校验库已是「备份里的那个库」**

```bash
docker compose exec -T db psql -U prdwright -d prdwright \
  -c "select version_num from alembic_version" \
  -c "select count(*) from pg_tables where schemaname='public'"
```

判据：版本号与表数都与 §3.2 抄进发布记录的值**一致**。不一致就别往下走（说明恢复的不是这份备份，或恢复不完整）。

**B5. （仅换机 / 恢复旧备份时）放回当时的 `master.key`**

命令见 §3.5。判据：密钥卷里的 `master.key` 属主 `10001`、权限 `600`。

**B6. 起旧镜像**

```bash
docker tag prdwright-api:rollback-<日期> prdwright-api:local
docker tag prdwright-web:rollback-<日期> prdwright-web:local
docker compose up -d --no-build
```

预期与判据同 A2 + A3 + A5（镜像 ID 必须等于回滚 tag 的 ID）。

**B7. 验证就绪、日志与数据**

```bash
docker compose ps
curl -fsS http://localhost/readyz
docker compose exec -T db psql -U prdwright -d prdwright -c "select version_num from alembic_version"
docker compose logs api --tail 50
```

判据：

- `/readyz` 返回 `{"status":"ready",…}`；
- `version_num` **仍等于** §3.2 记录的值。如果它变成了更新的版本，说明启动的是**新代码**（它把库又迁移了一次），回滚失败，回到 B1 重来；
- 日志无 `SchemaMismatchError`、无重启循环；
- 人工抽检：登录、打开已有方案、翻一次文档版本历史（确认历史行是备份里的那份）。

**B8. 记录**

把「恢复的 dump 文件名 + sha256 + 版本 + 表数 + 用时」写进发布记录，并在旧备份上标注「已用于回滚，保留至验收」。

### B 的边界（不能这样做）

- **不能只用新代码启动旧备份库**（会把库升级回新结构，回滚白做）；
- **不能只恢复库、不回退镜像**（同上）；
- **不能 `docker compose down -v`**（会连卷一起删，数据库与主密钥同时没）；
- **不能跳过 B4 就起 api**（拿错备份 = 往生产库里灌另一个时间点的数据）；
- **不能在生产库上 `alembic downgrade`**：迁移链只有一份 Postgres 基线，删列/改类型是单向门，「回滚靠备份、不靠反向迁移」是仓库纪律
  （[`../docs/agents/conventions.md`](../docs/agents/conventions.md) §3）；
- **不能把 v9 之前的 `.db` 备份喂回新栈**（旧 SQLite 库不迁移，见 `README.md` §6.6）。

---

## 8. 场景 C：只回退某一张表

**适用**：结构没变，**只有一张表的数据被误操作**（手工误 UPDATE / 误 DELETE / 误 TRUNCATE），其它表正常，
并且那份备份里有该表的数据。
**不适用**：结构变更（列 / 类型 / 索引 / 约束）、多张表一起坏、跨表一致性被破坏——那些走 B。

### 步骤

**C1. 先把「现在的样子」也备份一份**

```bash
cd deploy
make backup
```

判据：退出码 `0`，记下这次的新产物名（它代表「误操作之后的状态」）。回退本身也可能出错，有它才能再退一步。

**C2. 停 api（避免回退期间有新写入）**

```bash
docker compose stop api
```

预期：`Container prdwright-api-1  Stopped`。
判据：`docker compose ps api` 不再显示 `Up`。

**C3. 清空目标表（`--data-only` 不能带 `--clean`，实测会直接报错）**

```bash
docker compose exec -T db psql -U prdwright -d prdwright -c "TRUNCATE TABLE <表名> RESTART IDENTITY;"
```

预期：`TRUNCATE TABLE`。

> **实测**：`pg_restore --clean --data-only` 会被 pg_restore 直接拒绝——
> `pg_restore: error: options -c/--clean and -a/--data-only cannot be used together`。
> 所以单表回退的固定写法是**先 TRUNCATE，再 `--data-only`**。

**C4. 只恢复该表的数据**

```bash
docker run --rm --network prdwright_net \
  -e PGPASSWORD="$(grep '^POSTGRES_PASSWORD=' .env | cut -d= -f2-)" \
  -v "$PWD/backups:/backup:ro" postgres:16 \
  pg_restore --data-only --no-owner --no-privileges --table=<表名> -v \
  -h db -U prdwright -d prdwright /backup/prdwright-<日期>.dump
```

预期输出（关键一行）：

```
pg_restore: connecting to database for restore
pg_restore: processing data for table "public.<表名>"
```

判据：退出码 `0`，且出现上表 `processing data for table` 那一行。

**C5. 同步自增序列（表里有 serial / identity 主键时必须做）**

```bash
docker compose exec -T db psql -U prdwright -d prdwright \
  -c "SELECT setval(pg_get_serial_sequence('<表名>','id'), COALESCE((SELECT max(id) FROM <表名>), 1));"
```

判据：输出一个 `setval` 值，等于该表当前 `max(id)`（表为空时为 `1`）。不做这一步，下一次 INSERT 会撞主键冲突。

**C6. 校验行数与关键字段**

```bash
docker compose exec -T db psql -U prdwright -d prdwright \
  -c "select count(*) from <表名>"
```

判据：行数与「回滚点应有的行数」一致（按备份时间点估算或对照业务方）；抽 2~3 行业务字段看值对不对；
若其它表引用该表，再跑一次 `select count(*) from <子表> where <外键> not in (select id from <表名>)`，应为 `0`。

**C7. 起 api 并验证**

```bash
docker compose start api
curl -fsS http://localhost/readyz
```

判据：`readyz` 为 `ready`；页面相关列表数据恢复；日志无异常。

### C 的边界（不能这样做）

- **不能**用单表恢复处理结构变更：`--data-only` 只搬行，不搬列/类型/索引/约束；表结构不一致时会失败或写入半截。
- **不能**用 `--clean`（与 `--data-only` 互斥，而且它会 drop 表重建，把表上的约束/触发器打成备份时的样子）。
- **不能**在没有评估的情况下 `TRUNCATE … CASCADE`：会连带清空引用该表的子表数据；需要时先恢复子表，或干脆整库走 B。
- **不能**漏掉 C5 的序列同步。
- **不能**在 api 还在跑的时候做 C3~C4（并发写入会让恢复的行与新写入混在一起）。
- **不能**用 C 处理「多张表一起坏 / 外键不一致」：那是整库一致性问题，走 B。

---

## 9. 演练记录（本机开发环境，非生产）

> **这不是生产演练。** 下面是 2026-11-17 在本机开发库上跑的一次真实「备份 → 破坏 → 恢复」，
> 目的是证明**流程和命令可用、判据可执行**，并给出步骤耗时量级。数据量、磁盘、网络都与生产不可比，
> 生产切换窗口的实测耗时必须按 §9.5 在做完后补记进本节。

### 9.1 环境

| 项 | 值 |
| --- | --- |
| 日期 | 2026-11-17（本机本地时间） |
| 宿主 | Windows（开发机），Docker 29.9.2，Linux 容器 |
| 数据库 | Postgres 16（`pgvector/pgvector:pg16`），发布在本机 `127.0.0.1:5433`，`postgres` 超级用户 |
| 演练库 | `prdwright_drill` —— 每次由脚本 `DROP/CREATE` 重建，跑 **应用自己的迁移入口** 到 head（`0001`） |
| 数据规模 | 15 张表（与当前基线一致），`system_config` 里 3 行标记数据；**开发数据量，不是生产数据量** |
| 客户端 | `postgres:16` 镜像里的 `pg_dump` / `pg_restore`（与生产备份同一条链路），宿主不装 PG 工具 |
| 命令入口 | `cd deploy && make drill`（= `uv run --project ../backend python drill.py`） |

> **口令说明（仅本地）**：演练默认连接用 `postgres:postgres@127.0.0.1:5433`，这是本机开发集成库的账号口令，
> **只能用于本地演练**，不是任何部署的口令。生产演练用 `deploy/.env` 里的真实凭据，且不要把凭据写进任何文档。

### 9.2 实际执行的命令

```bash
cd deploy
make drill
```

脚本内部按 §3.2 / §7 的链路走：重建演练库 → 迁移到 head → 写 3 行标记数据 → `pg_dump -Fc` → `pg_restore --list` 自检
→ `DROP SCHEMA public CASCADE`（模拟整库被砸）→ `pg_restore --clean --if-exists` → 比对表数/版本/标记行 → 清理。

产物落在系统临时目录，**刻意不落 `deploy/backups/`**；跑完删库删目录，`git status` 保持干净（脚本末尾还会核对
`deploy/backups/` 的文件清单没变）。

### 9.3 实测耗时（同一台机器连续跑 3 次，结论一致）

| 步骤 | 耗时 |
| --- | --- |
| ① 预检（docker / 客户端镜像 / 本地库可达） | 483 ms |
| ② 重建演练库 `prdwright_drill` | 314 ms |
| ③ 迁移到 head（应用同一个入口，`prepare_schema`） | 1.2 s |
| ④ 造数据（`system_config` 3 行） | 36 ms |
| ⑤ 备份（`pg_dump -Fc` + `pg_restore --list` 自检 + sha256） | 2.1 s |
| ⑥ 破坏（`DROP SCHEMA public CASCADE`） | 103 ms |
| ⑦ 恢复（`pg_restore --clean --if-exists`） | 1.8 s |
| ⑧ 校验（表数 / 版本 / 标记行） | 31 ms |
| ⑨ 清理（删演练库 + 临时目录） | 1.2 s |
| **合计（脚本内）** | **7.3 s** |
| 进程外总耗时（含 `uv` 启动） | 7.8 s |

三次重复：总耗时 7.2 s / 7.3 s / 7.3 s；之后重复跑在 **5~9 s** 区间波动（差在容器启动与镜像/磁盘缓存，
量级不变）。**归档大小 40 846 字节**（15 张表、开发数据量）；
每次归档的 sha256 前缀不同（`2e07d6e2…` / `ef34bd16…`）是正常的——`pg_dump` 归档头里带生成时间，
校验时要与**同名** `.sha256` 文件比对，不能跨次比对。

### 9.4 成功判据与关键输出

```
[演练] ✓ ⑤ 备份（pg_dump -Fc + --list 自检） 2.1s
[演练] ✓ ⑦ 恢复（pg_restore --clean --if-exists） 1.8s
[演练] 库               prdwright_drill（每次重建，跑完删除）
[演练] 迁移版本          0001
[演练] 表数              备份前 15 → 恢复后 15
[演练] 标记行            3 行（drill-a, drill-b, drill-c）恢复一致
[演练] 归档              40846 字节，sha256 ef34bd16cccbc558…，15 张表的数据
[演练] 结论：这份备份**真的**能恢复；生产演练仍需在切换窗口按 ROLLBACK.md 走一遍。
```

四条判据（与 §7 的判据同源，脚本里逐条断言，任何一条不满足就以非零退出）：

1. **归档自检**：`pg_restore --list` 能解析、`TABLE DATA` 条目数 == 源库 `public` 表数（15 == 15）、归档里有 `alembic_version`；
2. **破坏成立**：`DROP SCHEMA` 之后 `pg_tables` 计数为 0（证明恢复不是「本来就没坏」）；
3. **恢复完整**：表数 15、`alembic_version=0001` 与备份前一致；
4. **数据一致**：3 行标记数据（`drill-a=alpha` / `drill-b=beta` / `drill-c=gamma`）逐值一致。

退出码约定（脚本开头也有完整表）：`2` 预检 / `3` 建库与迁移 / `4` 备份 / `5` 破坏与恢复 / `6` 恢复后校验。

### 9.5 与生产演练的差距（必须补的作业）

本记录**只证明流程可用**，生产环境还需要在切换窗口另做一次，并把真实数字补进本节。差异至少五处：

1. **数据量**：生产库远大于 15 表 / 3 行，`pg_dump` / `pg_restore` 的耗时随数据量增长；本机 2 秒的量级不可移植。
2. **IO 与磁盘**：开发机 SSD 与本地卷，生产可能是网络盘/云盘，备份与恢复都会更慢。
3. **网络**：本机 `pg_dump` 走 Docker Desktop 的 `host.docker.internal`；生产走 compose 网络内服务名 `db`，跳数不同。
4. **镜像冷启动**：本机 `postgres:16` 镜像已缓存、容器秒级启动；生产首次拉起可能要先落镜像。
5. **生产演练本身要做的动作**：把**本次的** `.dump` 恢复到一台非生产库（预发 / 同主版本的临时库）→ 核对表数与
   `alembic_version` → 在那边起一次 api 验证 `/readyz`。或者退一步：至少把本次 `.dump` 在一台非生产机器上
   `pg_restore --list` + 恢复一次（就是本节的 `make drill`，换成真实归档跑）。把耗时与结论写回本节，注明「生产演练」与日期。

### 9.6 复现与两个踩过的坑

复现：`cd deploy && make drill`。幂等、可重复跑；只动 `prdwright_drill` 这一个库名（脚本里是常量，命令行不能覆盖），
`--keep` 时才保留现场。它不碰 compose 的三个命名卷（`prdwright_pgdata` / `prdwright_logs` / `prdwright_secrets`）。

写这类脚本时踩到的两个坑，写在这里省下一个人半小时：

1. **容器里不能用自己的 `127.0.0.1` 连宿主**：`pg_dump` 跑在一次性的 `postgres:16` 容器里，容器内的 `127.0.0.1`
   是容器自己。要连宿主发布的 5433 得用 `host.docker.internal`（`docker run` 时加
   `--add-host=host.docker.internal:host-gateway`，Linux 上才成立）。生产备份没有这个问题——它走 compose 网络里的服务名 `db`。
2. **SQLAlchemy 的 URL `str()` 会把口令渲染成 `***`**：想用 `str(url.set(database=…))` 拼连接串，会得到一个
   `password authentication failed`——看起来像库的问题，其实是自己把口令擦掉了。要用
   `render_as_string(hide_password=False)`。

---

## 10. 红线清单

回滚当天，下面每一条都是「不要做」：

1. 不要 `docker compose down -v`（删卷 = 删库 + 删主密钥，无法撤销）。
2. 不要 `docker compose up -d --build` 当回滚（重建 = 又回到当前代码）。
3. 不要在数据库已迁移后用场景 A（旧代码 + 新结构 = 启动失败）。
4. 不要用新代码启动旧备份库（会把库再迁移一次）。
5. 不要 `alembic downgrade` 当回滚（迁移链只有一份基线，且删列/改类型是单向门）。
6. 不要把 `.db`（旧 SQLite）备份喂回 v9 栈。
7. 不要把 `deploy/.env` 或 `master.key` 打进数据库备份包。
8. 不要在 api 还跑着的时候做 `pg_restore --clean --if-exists` 或单表 TRUNCATE。
9. 不要跳过 §4 的基线记录和 §7 的 B4 校验。
10. 不要在验收前删掉旧镜像 tag、旧备份和上一把 `master.key`。

---

## 附：回滚记录单（照填）

| 字段 | 值 |
| --- | --- |
| 日期/时间 | |
| 触发原因 | |
| 选用场景 | A / B / C |
| 回滚目标代码 | `git rev-parse` hash + tag |
| 回滚目标镜像 | `rollback-<日期>` 的 image ID |
| 恢复的备份 | `prdwright-<日期>.dump`，sha256 = |
| 备份里的 `alembic_version` / 表数 | |
| 恢复后 `alembic_version` / 表数 | |
| 总耗时（从停栈到 `/readyz` ready） | |
| 操作人 / 复核人 | |
| 结论（已恢复 / 需升级处置） | |
