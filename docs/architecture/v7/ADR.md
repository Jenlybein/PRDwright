# v7 决策记录（ADR）

> **编号约定**：本层按原号重建 **ADR-054 ~ 059**（部署期六条），并新增 **ADR-084 ~ 087**。
>
> 为什么是「重建」而不是「重新编号」：`../v5/ADR.md` 的 ADR-070 已经把这件事定下来了——
> 那六条从未落盘的决策保留空号作墓碑，将来部署期**按原号重建、零重编号**
> （ADR 编号是身份，不是时序：ADR-042、ADR-027 被取代后编号都不动）。
> 现在兑现它：054~059 的正文在本文件，每条带一个「v7 修订」小节，
> 因为 v6 换掉数据层之后，其中几条的事实基础变了。
>
> 新增的四条接在 v6 的最后一条（ADR-083）之后，从 **ADR-084** 起。
> 架构描述见 `ARCHITECTURE.md`，排期见 `roadmap.md`。

---

## ADR-054：部署形态 = 单域名 + 路径前缀，两个前端共用同一个源

**状态**：已定（v5 起草 / **v7 重建**）。前端两处已落地；Nginx 与部署模板随 T17.4。

### 背景

v4 把管理员后台做成了**根目录独立应用**（ADR-039），因此有两份前端产物要上线。
它们怎么对外，有三种摆法：

1. 同一个域，用户端 `/`、管理端 `/admin/`；
2. 两个子域（`app.example.com` / `admin.example.com`）；
3. 两个完全独立的域。

本地开发时两个前端各自跑在 5173 / 5174，是「两个源」，所以这件事在本地根本不存在，
只有到了部署才第一次成为问题。

### 决策

**取 ①**：同一个域，用户端占 `/`，管理端占 `/admin/`，API 占 `/api/`。

配套三条：

- 后端只监听 `127.0.0.1:8000`，Nginx 是唯一对外进程；
- 管理端构建时 `base: '/admin/'`，路由用 `createWebHistory(import.meta.env.BASE_URL)`；
- `CORS_ORIGINS` **设空串**（见下方 v7 修订）。

### 为什么否决子域：Cookie 是 host-only 的

这一条不是偏好问题，是会被具体代码卡住的事实。后端在 `api/login_flow.py::set_session_cookie`
里这样写会话 Cookie：

```python
response.set_cookie(
    key=settings.auth_cookie_name,
    value=credential,
    max_age=settings.auth_session_ttl_hours * 3600,
    httponly=True,
    samesite="lax",
    secure=settings.auth_cookie_secure,
    path="/",
)
```

**没有 `domain` 参数**——这个 Cookie 只属于写入它的那个完整主机名。两个子域下的实际后果：

- 在 `admin.example.com` 登录 → Cookie 属于 `admin.example.com`；
- 页面请求 `app.example.com/api/...` → 浏览器**不会**带上它 → 每次都是 401；
- `samesite="lax"` 也不会让跨子域请求自动携带。

要修就得一次动三处：后端给 Cookie 设 `domain=.example.com`、`CORS_ORIGINS` 放开带凭据的
来源、多申请一张证书。换来的是「后台地址更好记」。不划算。

**同域同站则什么都不用做**：请求同源，CORS 根本不参与判定；Cookie 天然共享，
两个前端的登录态自动一致。

### 代价

- 管理端需要两处构建配置改动。其中路由那一处容易漏：Vue Router 的 `normalizeBase()`
  在没有传参时读 `<base>` 标签、再兜底 `/`，**不会**跟随 Vite 的 `base`。
  两个「base」是两件事，必须显式接上。
- 后台地址是一个路径而不是一个更好记的子域。接受。
- 本地 dev 地址会变成 `localhost:5174/admin/`。**接受，且刻意不做双轨**
  （`base: mode === 'production' ? '/admin/' : '/'`）：双轨会让「挂子路径」这件事在本地
  永远测不到，任何写死 `/xxx` 的跳转都要等部署那天才炸，而那时最贵。代价只是改一次书签。

### v7 修订

| 项 | v5 起草时 | v7 | 为什么改 |
| --- | --- | --- | --- |
| `CORS_ORIGINS` | 「保持默认，不放宽」 | **设空串** | 默认值是 `http://localhost:5173,http://localhost:5174`，而 `CORSMiddleware` 配的是 `allow_credentials=True`。同源部署下 CORS 完全不参与，这两个来源是**用不上的能力**；`SameSite=Lax` 虽然已经挡住跨站请求携带 Cookie，但「不需要的许可就不该开着」比「有别的机制兜着」更可靠。空串是显式的、能在 env 里一眼审计的 |

### 落地清单

- ✅ `frontend_admin/vite.config.ts` 加 `base: '/admin/'`（dev 也用同一个 base，不做双轨）；
- ✅ `frontend_admin/src/router.ts` 改 `createWebHistory(import.meta.env.BASE_URL)`；
- ✅ `frontend_admin/public/favicon.ico`（原本没有这个目录，`/favicon.ico` 因此没被 Vite
  加上 base 前缀，一直是个 404）；
- ⏳ Nginx 两段静态 location（含 `/admin` → `/admin/` 的 301 与 `/admin/index.html` 的 `no-store`）；
- ⏳ `deploy/backend.env.example` 里 `CORS_ORIGINS=`（空）并写明理由。

---

## ADR-055：进程模型固定为单 worker，并把「扩展前置条件」写进文档

**状态**：已定（v5 起草 / **v7 重建**，待实施）。**修订** v1 部署文档的「`-w 2`」。

### 背景

v1 的部署文档建议 `gunicorn -w 2`，理由是多 worker 能容错。文档末尾自己补了一句
「v2 引入进程内后台任务与事件总线后应保持单 worker」——到 v7 时这个约束不是「建议」
而是**硬约束**，因为它已经不是一处状态，而是四处。

### 决策

**固定单进程单 worker**，并且：

- 不引 gunicorn（它在本项目的唯一价值就是多 worker）；
- `--workers 1` **显式写进 systemd 的 ExecStart**，不靠默认值省略；
- 把「将来要扩展必须先做哪三件事」写进架构文档（§3.3），作为唯一允许推翻本决策的路径。

### 四处内存态（v7 版本）

| 状态 | 位置 | 多进程后果 |
| --- | --- | --- |
| `_RUNTIMES: dict[int, JobRuntime]` | `services/job_service.py` | A 进程建的任务，B 进程查不到 → 快照恢复失败、取消失效 |
| `JobBus._subscribers` | `services/job_bus.py`（源码注释原文：**仅进程内，单实例**） | 订阅在 B、任务在 A → 浏览器一个事件都收不到 |
| 运行时配置快照（5 秒 / 降级 1 秒 TTL） | `core/runtime_config.py` | 后台改配置只有被改那个进程生效 |
| SQLAlchemy 固定连接池（`pool_size=4`、`max_overflow=0`） | `core/engine.py` | 每进程 4 条连接打同一份 SQLite 文件，写冲突从「不存在」变成「偶发 `database is locked`」 |

第二条最致命，而且**不报错**：两个 worker 各持一部分订阅，用户点「生成」后有一半概率
一个字都不出，刷新又好了。这类「时好时坏」在单机上极难归因——它看起来像模型抽风，
实际是进程调度。

### 由此推出的三条纪律（它们不是建议，是推论）

1. **只起一个进程**。gunicorn 在只剩一个 worker 时只剩成本：多一层父子进程、
   多一套超时语义、多一个「为什么重启了两次」的疑问。
2. **重启会中断在飞任务**。`lifespan` 里的 `reap_orphans()` 把上次残留的 `running`
   收成失败，所以发布前必须排空，不能指望优雅停止保住任务。
3. **迁移与重启是同一个动作**（详见 ADR-057）。

### 接受的代价

- 纵向扩展有上限：一台机器的 CPU / 内存就是全部。生成任务的瓶颈在等 LLM 而不在本地算力，
  本期量级下这个上限远高于实际需求。
- 无法零停机发布。用「排空 → 短窗重启 → 冒烟」替代。
- 一个进程挂了服务整体不可用。用 `Restart=always` + 健康检查兜住（重启是秒级）。

### 扩展的前置条件（做完这三条才能重新讨论 worker 数量）

1. `_RUNTIMES` 与 `JobBus` 换成跨进程设施（任务队列 + 发布订阅）；
2. 运行时配置的进程内快照换成「写后广播失效」；
3. 评估 SQLite 的写并发，必要时迁到 PostgreSQL。

**顺序不能颠倒**：只做 1 不做 2，会出现「A 进程改了配置、B 进程照旧」；只做 3 不做 1，
数据库换了但任务仍然分裂。

### v7 修订

| 项 | v5 起草时 | v7 |
| --- | --- | --- |
| 第四条内存态 | 「SQLite 连接 + WAL，`core/db.py`」 | `core/engine.py` 的 SQLAlchemy `QueuePool`（`pool_size=4`、`max_overflow=0`）。**约束本身不变**：连接只在启动期建满，运行期一条都不新建 |
| 新增推论 | — | 因为 `max_overflow=0`，启动路径里必须有 `warmup()`，否则这条不变量在生产上不成立（ADR-084） |

---

## ADR-056：代码 / 数据 / 静态 / 日志四目录分离，配置移出代码树

**状态**：已定（v5 起草 / **v7 重建**，待实施）。

### 背景

v1 时期的部署把一切都放在 clone 出来的那一棵树里：数据库在 `backend/prdwright.db`、
日志在 `backend/logs/`、静态产物在 `frontend/dist/`、`.env` 也在 `backend/` 下。

这套摆法在「部署一次就不动了」时看不出问题，但它让**四类生命周期完全不同的东西共用
一个目录**：换代码（每次发布）、保数据（永远）、重建静态（随时）、清日志（可丢）。
于是「回滚代码」和「保住数据」变成同一个动作的两半——发布时要么先把库挪走，
要么赌这次不会失败。

### 决策

| 路径 | 内容 | 生命周期 |
| --- | --- | --- |
| `/opt/prdwright` | 代码 + `.venv` + `skills/` + `backend/migrations/` | 可整目录替换 |
| `/var/lib/prdwright` | `prdwright.db`（+ `-wal` / `-shm`） | 不可重建，只能备份恢复 |
| `/var/www/prdwright` | `releases/<ts>/{user,admin}` + `current` 符号链接 | 可随时重建 |
| `/var/log/prdwright` | 轮转日志 | 可丢 |
| `/etc/prdwright/backend.env` | 启动项（600） | 变更时改 |
| `/etc/prdwright/master.key` | 主密钥（600） | 轮换时改 |

对应地必须显式设定 `DB_PATH`、`LOG_DIR`、`PRDWRIGHT_SKILLS_DIR`、
`PRDWRIGHT_MASTER_KEY_PATH`——它们的默认值都会落回代码目录。

### 为什么生产用 `EnvironmentFile` 而不是代码目录里的 `.env`

两个理由，第二个是硬性的：

1. `.env` 在代码树里，会被「换代码目录」这个动作波及（不是被删，而是「哪一份才是生效的」
   变成需要人工确认的事）。
2. ⚠️ **`PRDWRIGHT_MASTER_KEY_PATH` 走 `os.environ`**（`core/crypto.py::master_key_path`），
   写进 `.env` **读不到**。`pydantic-settings` 读 `.env` 但不会把内容写进进程环境——
   这个坑 `skill_registry` 已经踩过一次（当时表现为「模板给了开关却不生效」），这里不重蹈。

systemd 的 `EnvironmentFile` 提供的是真正的进程环境，两条路都通。

### 收获

发布动作被压缩成一句话：**换 `/opt`，其余三个目录不动**。备份动作同样被压缩成一句话：
**抄 `/var/lib`**（外加 `/etc` 下那两个文件）。

### v7 修订

| 项 | 补充 |
| --- | --- |
| `/opt/prdwright` 的内容 | 追加 **`backend/migrations/`**：`core/schema.py` 把 Alembic 的 `script_location` 指向它。只拷 `src/` 不拷 `migrations/` 会在启动时报「找不到脚本目录」，而报错与数据库无关，极容易误判成库坏了 |
| `/var/www/prdwright` 的结构 | 从「两个 dist 目录」改成 **`releases/<ts>/{user,admin}` + `current` 符号链接**：发布要能原子切换、要能一键退回上一份静态（ADR-086） |
| 默认值风险 | `LOG_DIR` / `DB_PATH` 留空会让数据库与日志落回代码目录，而代码目录每次发布都被替换——这等于「每次发布丢日志、回滚时丢数据」。所以这两项**必须显式配**，不是「可选优化」 |

---

## ADR-057：发布顺序「先备份 → 换代码 → 重启（迁移自动跑）」，回滚 = 恢复备份

**状态**：已定（v5 起草 / **v7 重建**，待实施）。

### 背景

`main.py::lifespan` 里第一件正事就是 `prepare_schema(get_engine())`。也就是说
**「重启服务」这一个动作同时包含了「改表结构」**，而且迁移是自动的、无需人工触发。

这带来一个容易被忽略的耦合：平时「重启」是无害的运维动作，在这里它是**一次性、不可逆的
结构变更**的触发器。

### 决策

发布顺序固定为：

```text
0) 排空（确认无在飞任务）
1) 备份数据库（同时就是回滚点；失败即中止）
2) 换代码：git checkout <tag> + uv sync --frozen --no-dev + 两份前端构建
3) 发布静态产物（新 releases 目录 + 原子切符号链接）
4) systemctl restart   ← 迁移在此自动发生
5) 冒烟（basic，必要时 deep）
```

**回滚 = 恢复备份，不是跑反向迁移。**

### 为什么不能靠反向迁移

本项目的 schema 演进纪律是「只追加、不回改」。这条纪律保证了**前进**的幂等与可重复，
但它不提供**后退**的能力：没有任何一条迁移会删掉新加的列。

于是「回退代码」之后的真实状态是：**新结构 + 旧代码**。旧代码未必能在新结构上正常运行
（例如它写的插入语句不含新列、或它的查询不认识新的表）。唯一能保证回到可用状态的手段是
把数据也一起退回去。

### 三条推论

1. **备份失败 → 停止发布**。备份没做成却继续发布，等于当场取消回滚能力。
2. **静态产物用「新目录 + 符号链接原子切换」**，不用原地覆盖：覆盖过程中请求会拿到
   半新半旧的资产集合（新 `index.html` + 旧 `assets/`），表现为随机白屏。
3. **排空是必需步骤，不是礼貌**。判据是 `GET /api/admin/jobs?status=running` 的
   `total == 0`（ADR-055 推论 2）。

### v7 修订

| 项 | v5 起草时 | v7 |
| --- | --- | --- |
| 迁移入口 | `init_schema()` 之后 `run_migrations()`（`user_version` 20 步链） | `prepare_schema()` + Alembic（ADR-076 / 083）。旧链已整体删除 |
| 迁移对旧库的态度 | 自动补到最新 | **拒绝启动**并打印逐条差异（`SchemaMismatchError`） |
| 新增推论 | — | **恢复备份之后第一次启动会再跑一次 `upgrade head`**。若备份里的 `alembic_version` 比当前代码新，启动会失败——所以恢复演练必须用**当前代码**跑（§8.4 的表） |

---

## ADR-058：备份 = 数据库在线备份 + 主密钥，两者分开存放且必须演练恢复

**状态**：已定（v5 起草 / **v7 重建**，待实施）。

### 背景

数据是唯一不可重建的东西：`/var/lib/prdwright/prdwright.db`。而 `core/engine.py` 在每条
连接上执行 `PRAGMA journal_mode=WAL`，让「怎么备份」这件看似简单的事有了唯一的正确答案。

同时 ADR-053 引入的主密钥决定了「备份能不能用」：`model_providers.value_token` 全是它加密的
密文，**没有它，恢复出来的数据库里那批密钥一个都解不开**。

### 决策

1. **用 SQLite 的在线备份接口**，不复制文件：

   ```bash
   sqlite3 /var/lib/prdwright/prdwright.db "VACUUM INTO '/var/backups/prdwright/db-YYYYMMDD.db'"
   ```

   WAL 模式下数据先写 `prdwright.db-wal`、再择机合并回主文件。直接 `cp` 主文件拿到的是
   「合并前」的快照，而 `-wal` / `-shm` 两个边车文件又不属于同一时刻——拷出来的库可能打不开，
   或者**静默缺最近几分钟的写入**（后者更糟：它不报错）。

2. **主密钥每次随备份同批处理，并且保留每一把**。轮换之后，旧备份里的密文只能用
   **旧**密钥解开。所以密钥备份不是「留最新一把」，而是留全部历史版本。

3. **两者分开存放**。同机不同路径只能防「一起被打包拷走」；真正的分离是备份介质分离。

4. **备份产物必须自检**（v7 新增，见下）。

5. **恢复演练是验收项，不是可选项**：拿一份备份恢复到临时路径，用**当前代码**起一个
   只听回环的实例，确认 `/health` 三项全绿、能登录、能看到方案列表、能打开版本抽屉。

### 保留策略

| 对象 | 频率 | 保留 |
| --- | --- | --- |
| 数据库 | 每日 | 近 14 份 |
| 主密钥 | 变更时 + 随每次备份 | 全部版本（按指纹命名） |
| `backend.env` | 变更时 | 最新 1 份（无密钥值） |
| `manifest.json` | 随每次备份 | 与数据库同寿命 |

### 接受的代价

- 最坏丢 24 小时数据。若要更细，将来加 WAL 归档即可，不改今天的结论。
- 备份未加密存放（与数据库同一台机器）。本期规模接受；若将来备份出机器，必须一并加密。

### v7 修订：备份自检

v5 起草时只要求「生成备份文件」。v7 追加三条，任一失败即以非 0 退出：

1. 打开备份文件并 `PRAGMA integrity_check`（期望 `ok`）；
2. 读出 `alembic_version` 写进 `manifest.json`——恢复时要靠它判断「补不补迁移」；
3. 记录 sha256 与字节数。

**为什么这一条不能省**：`VACUUM INTO` 本身很少失败，但「路径写错、磁盘满、权限不对、
备份到一个空文件」全都不会在生成时炸。而这三类恰好是自动化脚本最常见的失败方式，
且都是「看起来成功了」。一份没自检过的备份，和没有备份在出事那天是一样的。

---

## ADR-059：SSE 保活用「长 read_timeout + 服务端心跳」双保险（心跳在 v7 升为必做）

**状态**：已定（v5 起草 / **v7 重建**，心跳从「可选」改为**必做**）。

### 背景

`GET /api/jobs/{id}/stream` 是长连接流式响应，一篇 PRD 含「分章撰写 → 审核 → 改写」
多轮 LLM 调用，耗时可能达数分钟，且**两次事件之间可能长时间完全静默**。

Nginx 默认会缓冲响应、且 `proxy_read_timeout` 默认 60 秒——两者都会把流式体验破坏掉，
后者还会直接切断连接。后端 `api/sse.py` 已带 `X-Accel-Buffering: no`，但**没有心跳帧**。

### 决策

两道保险都上，它们防的不是同一个东西：

| 手段 | 防什么 | 改动 |
| --- | --- | --- |
| Nginx `proxy_buffering off` + `proxy_read_timeout 1800s` + `gzip off` | 反向代理按空闲超时切断、按缓冲攒批 | 零代码改动，**必做** |
| 服务端每 15 秒发一行 `: ping` 注释帧 | 链路中间其它网关（运营商 / 企业代理）的空闲超时 | `api/sse.py` 一处，**v7 必做** |

心跳是 SSE 规范内的注释行（以冒号开头），`EventSource` 会忽略它，不产生任何业务事件：

```text
: ping

```

### 为什么 v7 把它从「可选」升为「必做」

v5 起草时的判断是「Nginx 三行已覆盖我们能控制的全部环节，心跳风险不大但不是零，
排在最后」。当时的生成链路是**整篇一次调用**——模型在持续吐字，事件密集。

v6 之后不是了：分章生成一章里要「撰写 → 审核 → 改写」串行跑，审核期间前端**收不到任何
`chunk`**。一次 PRD 有好几次这样的静默窗口，每次可能几分钟。运营商 NAT 与企业网关的
空闲超时常见值是 60 秒，正好落在这个窗口里。

而静默断流的表现是「页面卡住不动、没有任何错误」——**它不会报错，只会停住**。
这类故障在部署后极难归因（看起来像模型卡了、像前端卡了、像网络抖了）。
用一个 15 秒一行的注释帧把它变成「不可能发生」，代价是十几行代码。

### 实现约束（三条，缺一条就别做）

1. **必须走 `make_sse_generator` 的同一个出口**。SSE 分帧全项目只此一处
   （`api/sse.py` 的模块 docstring 写明），另起一条发送路径会让「事件格式」出现两个真相。
2. **不得产生业务事件**：只发 `: ping\n\n` 这一种注释帧，不进 `StreamEvent` 序列。
3. **不得撞坏形状断言**：`stage` / `chunk` / `part` / `error` 各有独立用例，
   心跳加入后这些用例必须一条不改地继续绿。实现上建议用
   `asyncio.wait_for(anext(events), timeout=15)` 包一层，超时就 `yield ": ping\n\n"`，
   而不是另开一个后台 task 往同一个响应写（那会引入并发写顺序问题）。

### 与既有实现的关系

- `X-Accel-Buffering: no` 保留（它防的是「换网关时忘了配」，与 Nginx 配置是两道）。
- 心跳只在**流已建立、但没有事件**时发；流结束后不再发（`[DONE]` 之后必须彻底安静，
  否则前端 `EventSource` 会一直认为连接还活着）。

---

## ADR-084：连接池预热进启动路径（`warmup()` 必须在开始服务之前跑）

**状态**：已定（v7 新增，**T17.1 已落地**）。

### 背景

`core/engine.py` 的设计不变量是：**连接只在启动期建满，运行期一条都不新建**。
它的落地方式是 `QueuePool(pool_size=4, max_overflow=0)`——池满即等 `pool_timeout`（5 秒），
然后抛超时，**不会悄悄扩容**。

这条不变量不是洁癖。T15 时期踩过一个具体的坑：多线程**同时首次映射** WAL 的 `-shm` 文件
会返回 `SQLITE_READONLY`，而 `busy_timeout` 只对 `SQLITE_BUSY` 生效，所以它不重试、
直接失败。当时的解法就是「把建连接这个动作收敛到启动期单线程」。

v6 把自建的 444 行池换成了 SQLAlchemy，但**保留了同一条不变量**，并提供了
`engine.warmup()` 来实现它。

### 现状（这是本 ADR 存在的全部理由）

```
$ grep -rn "warmup" --include=*.py src tests
src/backend/core/engine.py:120:def warmup(...)
tests/test_engine.py:53:def test_warmup_builds_all_connections_before_serving(...)
tests/test_engine.py:67:    assert engine_module.warmup(eng, 3) == 3
```

**只有测试在调它。** `main.py::lifespan` 里没有。

于是「连接只在启动期建满」这条不变量在生产上不成立：池是懒建的，实际只会建到
「真被同时用到」的那几条。`prepare_schema()` 借过又归还 1 条，所以启动那一刻池里是 1 条——
`DB_POOL_SIZE=4` 成了一个没有兑现的配置。

### 谁会同时碰数据库（窗口到底在哪）

这一点必须先看清，否则会修错地方。本项目里能借到池的只有两个角色：

| 借用者 | 并发性 | 说明 |
| --- | --- | --- |
| HTTP 请求处理 | **串行** | 所有路由都是 `async def`，内部直接调同步的 service；同步 DB 调用会阻塞事件循环，所以同一时刻只有一个请求在碰库 |
| `log-store` 后台线程 | **与上者并发** | `core/logging_setup.py` 里 `threading.Thread(name="log-store")` 从队列取日志批量写 `app_logs`，走同一个池 |

两者在**启动期**天然重叠：`install_log_persistence()` 起线程之后，主线程还要继续跑
`ensure_bootstrap()` / `reap_orphans()` / `ensure_skills()`，每一步都可能借连接。
第一处真正「两个线程同时建连接」的时机就在这里。

**不在「第一批用户请求」**：那时启动已经完成，日志线程也早把连接借出来了，
池里那几条已经建好。这一点与 v5 起草时的判断不同（当时写的是「启动完成后第一批并发请求」），
按当前代码核过之后修正。

### 决策

在 `lifespan` 里、`prepare_schema()` 之后、任何可能借到池的后台设施之前，显式预热：

```python
from backend.core.engine import get_engine, warmup

revision = prepare_schema(get_engine())
warmup(get_engine())          # ★ 建满 DB_POOL_SIZE 条，只在此处建连接
invalidate()
install_log_persistence()     # 日志线程之后才可能借连接
```

位置三条理由：

- **在 `prepare_schema` 之后**：迁移期间借的那条连接先归还，预热才是「从头建满」；
- **在 `install_log_persistence` 之前**：否则日志后台线程可能与预热并发建连接，
  把竞态请回来（虽然窗口很小，但这条不变量的价值就在于「窗口为零」）；
- **在接请求之前**：这是显而易见的，但它决定了「预热失败 = 启动失败」，
  而不是「第一个用户遇到 500」。

### 被否决的方案

| 方案 | 否决理由 |
| --- | --- |
| 调大 `pool_size`（比如 16）不预热 | 没用。懒建的池无论多大，都是「第一批并发请求里同时建」，竞态窗口与原池相同 |
| 允许 `max_overflow > 0` | 把 T15 的竞态请回来。溢出连接正是「运行期新建连接」 |
| 把 `pool_pre_ping=True` 当成解药 | `pre_ping` 解决的是「连接被服务端断开」，不是「同时在建连接」。两件事 |
| 启动后人工打几个并发请求「预热」 | 不可重复、不可验证，且发布脚本里没人会记得 |

### 后果

- **启动多一次固定开销**：建 4 条 SQLite 连接，毫秒级。相比「第一批用户撞竞态」的代价可忽略。
- **预热失败即启动失败**（例如目录只读、磁盘满）。这是好事：它把「库根本连不上」
  从「运行期偶发 500」提前成「进程起不来」，而后者在 `systemctl status` 里一眼可见。
- **要用例守住**（已加，全部在 `tests/test_engine.py`）：
  1. 串行借还 5 次，新建连接数不增（原有）；
  2. **并发**借 8 次，新建连接数不增（新增，用线程池造出「同时第一次借」那个瞬间）；
  3. **`lifespan` 真的调用了它**（新增）。
  第 3 条最关键：前两条只测 `warmup()` 这个函数本身，`main.py` 里漏掉那一行时它们照样绿——
  那正是 v7 之前的状态。
- ⚠️ **判据只能是 `checkedin()`，不能是 `size()`。** `QueuePool.size()` 返回的是
  `maxsize`（配置容量），恒等于 `DB_POOL_SIZE`，与实际建了几条无关。
  第一版用例拿 `size()` 断言，反向验证时发现「完全不预热」也能通过。
  顺着这条线还查出两处假象：`engine_stats().idle` 原来算的是 `maxsize - in_use`
  （同样恒真），已改成 `checkedin()`；`conftest` 收尾守卫里那条 `idle == size`
  因此**删掉**——它从来没守过任何东西。
- **实测**：不预热时 `{'size': 4, 'in_use': 0, 'idle': 1}`，池里真的只有 1 条；
  预热后 `idle` 等于 `DB_POOL_SIZE`。这就是「配置写 4、实际 1」的量化证据。
- **测试开销**：每个走 `client` 夹具的用例都会因 `lifespan` 多建 4 条连接。
  v6 的装置是「每进程模板库 + 每用例拷贝」，做不到真正的进程级预热（每用例换库）。
  实测全量 **986 例 / 92.9 秒**，与修前（984 例 / 95.3 秒）在并行波动范围内——
  预热开销可忽略。若将来用例数翻倍再考虑把测试里的 `DB_POOL_SIZE` 调小。

---

## ADR-085：接口文档搬到管理员专属路由，公开的四条一律不注册

**状态**：已定（v7 新增，**T17.1 已落地**）。

### 背景

FastAPI 默认注册 `/docs`、`/redoc`、`/openapi.json`。而 `api/deps.py::_PUBLIC_EXACT`
把这三条**显式列进了免登录白名单**——注释写的是「交互式文档，方便本地联调；
部署到公网时可从这里移除」。

现在就是「部署到公网」的时刻。这三条暴露的是**全部接口形状**：

- 所有管理员接口的路径与参数结构（虽然调用仍需 `require_admin`，但形状本身是情报）；
- 请求 / 响应的完整 Schema，包括字段约束；
- 内部错误码清单（`core/errors.py` 的全部 `ErrorCode`）。

### 决策

**公开的四条一律不注册；在 `/api/admin/` 下重建三条，逐条挂 `require_admin`；
管理端加一页「接口文档」把入口摆出来。**

| 层 | 做法 | 落地 |
| --- | --- | --- |
| 后端 | `FastAPI(docs_url=None, redoc_url=None, openapi_url=None)` | `main.py` |
| 鉴权 | `/docs`、`/redoc`、`/openapi.json` 从 `_PUBLIC_EXACT` 删掉 | `api/deps.py` |
| 管理员入口 | `GET /api/admin/openapi.json`、`/docs`（Swagger UI）、`/redoc`（ReDoc），三条都挂 `require_admin` | `api/admin_docs.py` |
| 面板 | 新增「接口文档」页，两个按钮分别打开上面两个 HTML 页 | `frontend_admin/src/views/ApiDocsView.vue` |
| Nginx | `location ~ ^/(docs\|redoc\|openapi\.json)($\|/) { return 404; }` | `/etc/nginx/conf.d/prdwright.conf` |

**为什么是「管理员专用」而不是「生产直接关掉」**：关掉最省事，代价是线上完全没法查接口
（只剩读 `api/schemas.py` 一条路）。而这三条本来就只需要给管理员看——把它们挪到
`/api/admin/` 下，就同时拿到「不对外」与「线上可查」。多出来的成本只有三条十几行的路由
和一个前台页面。

**为什么不用一个 `API_DOCS_ENABLED` 开关**：开关的语义是「两种模式」，而这里只有一种——
公开的永远关、管理员的永远开。加开关等于允许「把公开文档打开」这个状态存在，
而那正是要消灭的东西。

### 三个落地细节（漏了会以为已经关干净）

1. **FastAPI 实际注册四条路由，不是三条。** 用 0.142.2 实测：

   ```text
   ['/openapi.json', '/docs', '/docs/oauth2-redirect', '/redoc']
   ```

   传三个 `None` 之后四条全部消失（`routes == []`），而 `app.openapi()` **仍然可用**——
   管理员那条路由正是靠它取 schema。所以「关掉路由」与「拿不到 schema」是两件事。

2. **未登录时是 401，不是 404。** `AuthMiddleware` 跑在路由之前：`/docs` 已从白名单移除
   → 匿名请求在进路由前就被拦成 401。只有**已登录**的普通用户走到路由表，才会得到 404
   （因为路由真的不存在）。两个断言分开写，就是在钉这条时序：

   | 身份 | 访问 `/docs` | 访问 `/api/admin/docs` |
   | --- | --- | --- |
   | 未登录 | 401（中间件） | 401（中间件） |
   | 已登录普通用户 | 404（路由不存在） | 403（`require_admin`） |
   | 管理员 | 404（路由不存在） | 200 |

   推论：经 Nginx 时外部看到的是 404（Nginx 直接返回），直连 8000 看到的是 401/404。
   验收判据要写成「不是 200 且响应体里没有 openapi 字段」，写成「一律 404」会误报失败。

3. **这两个页面靠 Cookie 认身份，不是 Bearer。** `/api/admin/docs` 是浏览器用新标签页
   打开的 HTML，同源**顶层导航**会带上会话 Cookie（`SameSite=Lax` 允许 GET 导航）；
   页面里 Swagger UI 再用 `fetch` 取 `/api/admin/openapi.json`，同源请求同样带 Cookie。
   若将来前端改成把 token 放 `Authorization` 头，这两个页面会一起失效——那时要另想办法
   （给文档页签名临时地址，或让会话同时写进可被同源读取的 Cookie）。

### 为什么不能只做 Nginx 那一层

Nginx 只挡住「从公网进来的请求」。而 8000 端口在本机是**真实存在的**：
Nginx 配置被改坏、换机器时忘了 deny、临时起一个端口转发，文档就直接暴露了。
安全边界应当落在**应用自身**，反向代理只是第二道。

同理，白名单也必须改：只在 Nginx 挡、白名单里留着三条，等于给「将来有人把 docs 打开」
预置了一个未登录即可访问的口子。

### 为什么不是「从白名单删掉、保留注册」

这是最省事的一条（一行改动），但它把门槛定成了「**已登录**」而不是「**是管理员**」。
任何注册用户都能看到全部管理员接口的参数结构——包括那些他调用会被 403 的接口长什么样。
「知道形状」本身不构成越权，但它是不必要的信息暴露，而这个项目里「默认拒绝」的成本
已经被中间件摊平了，没有理由在这里放宽。

### 代价

- **多一个外部依赖**：Swagger UI 与 ReDoc 的 JS/CSS 走 CDN（`api/admin_docs.py` 顶部三个常量）。
  浏览器（不是服务器）需要能访问 jsdelivr。内网或 CDN 不可达时页面会白屏——
  解法是把产物放到本机静态目录并改那三个常量，**只改常量，不改函数体**。
- **两个页面各有一份 HTML**，样式与后台不统一。接受：它们本来就是开发工具，
  不是后台的一屏；强行套 Sky Brutalism 要重写 Swagger UI 主题，不成比例。
- **在线试调是真的写操作**：Swagger UI 里点「执行」会取消任务、改配置、删用户。
  页面上已用文案提醒，但这是「能力」而不是「缺陷」——它正是相对于 ReDoc 的价值。

### 落地清单（已全部完成）

- `api/admin_docs.py`：新增三条路由 + 三个静态资源常量 + 「只有管理员能看」那套论证；
- `main.py`：`FastAPI(..., docs_url=None, redoc_url=None, openapi_url=None)`，并注册新 router；
- `api/deps.py`：白名单删三条（**保留注释说明为什么删**，否则下一个人会「顺手加回来」）；
- `frontend_admin/src/nav.ts` + `router.ts` + `views/ApiDocsView.vue`：新增「接口文档」页；
- `tests/test_admin_docs.py`：9 条，覆盖「路由不存在 / 不在白名单 / 匿名 401 / 普通用户 404 /
  管理员 403 / 管理员 200 / 页面指向管理员 schema」；
- `frontend_admin/src/__tests__/api-docs.spec.ts`：3 条，覆盖两个按钮的地址与「仅管理员」文案；
- Nginx：`location ~ ^/(docs|redoc|openapi\.json)($|/) { return 404; }`（T17.4 落地）。

**反向验证做过**：把 `main.py` 里的三个 `None` 临时去掉 → `test_public_doc_routes_are_not_registered`
必须转红 → 还原。这条纪律来自 v6 的守卫用例：没有反向验证过的断言，分不清「守住了」
和「本来就没事」。

---

## ADR-086：静态产物在服务器上构建，用「新目录 + 符号链接」原子切换

**状态**：已定（v7 新增，T17.4 落地）。

### 背景

两份前端产物（用户端 + 管理端）要在每次发布时更新。三种摆法：

1. **服务器上构建**：`git checkout <tag>` 后 `npm ci && npm run build`；
2. **CI 构建后下载产物**：GitHub Actions 上传 artifact，服务器 `curl` 下载；
3. **本地构建后 scp 上传**。

### 决策

**取 ①**，并把产物目录版本化：

```text
/var/www/prdwright/
  releases/
    20261023-1530/{user,admin}        ← 本次发布
    20261022-0912/{user,admin}        ← 上一版（保留最近 3 份）
  current -> releases/20261023-1530   ← 唯一被 Nginx 引用的路径
```

发布时：构建到 `releases/<新时间戳>/`，然后 `ln -sfn releases/<新> current`。
`ln -sfn` 在 POSIX 上是一次 `rename`，不会出现「链接短暂不存在」的窗口。

### 为什么否决 ②：CI 构建产物

- **没有制品库，也没有服务器上的下载凭据**。要走通得给服务器配一个能读 artifact 的
  token（或让仓库公开可读），这等于为「省一次构建」新增一条长期凭据；
- 两个前端产物加起来是几十 MB，跨网下载时间与本地构建时间同量级；
- CI 的 Node 版本与服务器可能不一致（`engines` 只规定范围），产物差异难复现。

### 为什么否决 ③：本地构建上传

**它让「服务器上跑的东西」与「仓库里的这个 commit」之间失去可验证的对应关系。**
本地 `dist/` 可能与 `git status` 里的未提交改动混在一起，而服务器上没有任何东西能发现它。
这是「看起来最快、出事最难查」的那一种。

### 代价

- **发布窗口变长**：两次 `npm ci` + 两次 `npm run build`，在 2 vCPU 上约 3–6 分钟。
  这段时间旧版本仍在服务（我们还没重启），只是 CPU 被占用，生成任务会稍慢。
- **内存风险**：Node 构建峰值约 1 GB。2 GB 机器上必须**串行**构建，并设
  `NODE_OPTIONS=--max-old-space-size=1024`；4 GB 机器无此问题。
- **保留策略**：`releases/` 只留最近 3 份（回滚需要上一份，再往前没必要）。
  清理动作放在发布脚本末尾，不留定时任务。

### 与 ADR-057 的关系

ADR-057 说「静态产物用新目录 + 原子改名，不用原地覆盖」。本 ADR 把它具体化成
「符号链接切换」——因为 `mv` 一个可能正在被 Nginx 读的目录，比 `rename` 一个符号链接
风险高得多（`mv` 在跨文件系统时是「拷贝 + 删除」，中间态可能不完整）。

---

## ADR-087：运维脚本用 Python（走 `backend/.venv`），不用 bash 拼 CLI

**状态**：已定（v7 新增，T17.0 落地）。

### 背景

v5 起草时定的是三个 bash 脚本：`backup_db.sh` / `smoke.sh` / `deploy.sh`。
它们要用的能力是：

- 备份：对 SQLite 执行 `VACUUM INTO`，然后**打开产物做自检**（`integrity_check`、
  读 `alembic_version`、算 sha256）；
- 冒烟：发 HTTP 请求、解析 JSON、判断状态码与响应头；
- 发布：串行执行一串步骤，任一步失败即中止，并且**要能判断「这次发布是否包含迁移」**。

### 决策

**全部用 Python**，落点 `scripts/deploy/`，运行方式是
`/opt/prdwright/backend/.venv/bin/python scripts/deploy/xxx.py`：

| 脚本 | 职责 |
| --- | --- |
| `backup.py` | `VACUUM INTO` + 主密钥归档 + `manifest.json` + 自检；失败非 0 退出 |
| `restore.py` | 从备份恢复到一个**指定目标路径**（不自动起服务，不自动覆盖生产库） |
| `release.py` | 按 ADR-057 的六步发布；每步失败即中止并打印恢复指引 |
| `smoke.py` | `--basic` / `--deep` 两档冒烟；非 0 即失败 |

### 理由

1. **零新增依赖**。`sqlite3`、`hashlib`、`json`、`urllib`、`http.client` 全在 stdlib；
   运行环境就是后端自己的 `.venv`（`uv sync` 已经装好），不需要目标机额外装 `sqlite3` CLI、
   `jq`、`curl` 的某个特定版本。
2. **备份自检只能用代码做**（ADR-058 的 v7 修订）。bash 里做 `PRAGMA integrity_check`
   要 `sqlite3 ... | grep ok`，而 `grep` 成功不代表文件完整；读 `alembic_version` 也同理。
   用 Python 是 `conn.execute(...).fetchone()`，失败会抛异常，不会静默通过。
3. **发布脚本要判断「有没有新迁移」**。`sqlite3` 读 `alembic_version` + 比对
   `migrations/versions/` 的 head，用 bash 写要三次字符串处理，用 Python 是两行。
   这个判断决定了回滚时要不要恢复数据（ADR-057 §10.2）。
4. **失败语义统一**：Python 的 `SystemExit(1)` 与异常栈比 bash 的 `set -e` 更容易
   做到「在正确的位置停」，而 `set -e` 在管道与函数里有一堆反直觉的例外。

### 被否决的方案

| 方案 | 否决理由 |
| --- | --- |
| 纯 bash（v5 起草时的做法） | 能用，但自检与 JSON 解析都要靠外部工具；而且「备份成功」的判据会退化成「文件存在且非空」——恰恰是 ADR-058 要否掉的那种弱判据 |
| 引入 `boto3` / `click` / `requests` 等库 | 运维脚本的依赖也会变成「发布时要先装它」。stdlib 够用，就不引 |
| 放到 systemd timer 里跑备份 | 备份这件事需要**看得见失败**。timer 的失败只进 journal，而发布脚本里的备份失败必须当场中止发布。两者可以共存：日常备份用 timer 调同一个 `backup.py` |

### 代价

- 脚本不能「一行命令贴到聊天窗口让别人执行」——得带解释器路径。用
  `#!/opt/prdwright/backend/.venv/bin/python` 的 shebang 加 `chmod +x` 解决。
- `restore.py` 必须**故意**做得很笨：只接受显式的 `--to` 参数，不猜路径、不自动停服务、
  不自动覆盖。恢复是危险动作，脚本的价值在于「不替人做决定」。

### 落地清单

- `scripts/deploy/{backup,restore,release,smoke}.py`；
- `deploy/backend.env.example`（生产启动项模板，逐项带理由）；
- `deploy/systemd/prdwright.service`、`deploy/nginx/prdwright.conf`（可复制到目标机）；
- 每个脚本 `--help` 能自解释，且**不依赖当前工作目录**（路径全部相对脚本自身解析）。
