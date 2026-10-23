# v7 架构：部署与上线

> **一句话**：把已在本地跑通的 PRDwright 放到一台公网 Linux 上——一个域名、一份 HTTPS
> 证书、一个后端进程对外服务，并让它**可重复部署、丢不了数据、改得动、退得回**。
>
> **下层**：`../v6/`（数据层重构，已完成）。本层不改业务能力，只回答
> 「这些东西怎么搬到公网上、怎么长期活着」。
> 时间线真源：**`../evolution-truth.md`**

---

## 1. 目标与范围

### 1.1 四句话目标

1. **公网可用**：别人用域名打开就能用，全程 HTTPS，流式输出照旧一段段出现。
2. **可重复部署**：同一套步骤能在第二台机器上从零搭出同样的服务，不依赖「当时怎么弄好的」。
3. **丢不了数据**：数据库与主密钥都有备份，而且**备份被验证过能恢复**。
4. **改得动、退得回**：发布有固定动作、有冒烟清单；失败能回到上一版可用状态。

### 1.2 本期范围（In）

| # | 范围 | 落到哪一节 |
| --- | --- | --- |
| 1 | 目标机基线（系统用户 / 四目录 / uv + Python 3.12 / Node 22 / Nginx / 防火墙） | §2 |
| 2 | **进程模型**（单 worker 是硬约束，以及它推出的三条发布纪律） | §3 |
| 3 | **启动路径**（`lifespan` 的九步、`prepare_schema` 的三条分支、连接池预热） | §4 |
| 4 | 两个前端的产物、路由基址与缓存策略 | §5 |
| 5 | Nginx 契约（静态 / 反代 / SSE / 安全头 / HTTPS） | §6 |
| 6 | 上线配置清单（启动项 / 热配置 / 主密钥 / 首个管理员） | §7 |
| 7 | 数据：SQLite 备份、**备份自检**、恢复演练、迁移不可回滚 | §8 |
| 8 | 进程管理（systemd）与优雅停止 | §9 |
| 9 | 发布流程、冒烟清单、回滚 | §10 |
| 10 | 最小可观测（探针 / 日志落盘 / 磁盘告警） | §11 |

### 1.3 本期明确不做（Out）

| 事项 | 为什么本期不做 |
| --- | --- |
| **容器化**（Docker / Compose / 镜像仓库） | 单台单机、单人维护。镜像带来的可复现性用 `uv sync --frozen` + `npm ci` 已经拿到（两个 lock 文件都在版本控制里）；引入镜像要先付「分层 / 构建机 / 卷挂载」三笔学费 |
| **CI/CD 自动发布** | CI 已经在跑三套用例（`.github/workflows/ci.yml`），但**没有制品库、没有部署凭据通道**。本期用「CI 绿 → 服务器上跑发布脚本」，人只在中间做一次确认 |
| **多副本 / 负载均衡 / Redis / PostgreSQL** | 被 §3 的单 worker 约束挡住——先做完 §3.3 那三件事才有讨论前提 |
| **蓝绿 / 金丝雀 / 零停机** | 零停机要求「新旧两版同时服务」，与单进程冲突；本期用「排空 → 短窗重启 → 冒烟」 |
| **对象存储 / CDN / WAF** | 两个前端的静态资源总量在 MB 级，Nginx 直出即可 |
| **指标平台**（Prometheus / Grafana） | 日志（含 `request_id`）+ `/health` 已够定位本期问题；指标是「有规模之后」的需求 |
| **备份加密 / 异地备份** | 备份未离开本机。若将来备份出机器，**必须先加密**（ADR-058 的代价一栏已登记） |
| **用户自助导出之外的数据迁移工具** | v4 的管理员导出（50MB 上限的 zip）已覆盖「把数据拿走」这个需求 |
| **主密钥托管（KMS / Vault）** | 单机自托管，密钥就是 `/etc/prdwright/master.key` 一个文件；引入托管会新增一个「起服务前必须先连上外部」的依赖 |

---

## 2. 运行环境与拓扑

### 2.1 拓扑

```text
浏览器
  │  HTTPS
  ▼
Nginx（80 强制跳 443 / 443 终止 TLS）
  ├─ /                静态 → /var/www/prdwright/current/user      （用户端 SPA）
  ├─ /admin/          静态 → /var/www/prdwright/current/admin     （管理端 SPA）
  ├─ /api/            反代 → 127.0.0.1:8000                       （唯一对外业务入口）
  │    └─ /api/admin/docs·redoc·openapi.json                        （接口文档，仅管理员，§4.4）
  ├─ /health          反代 → 127.0.0.1:8000                       （仅本机可达，§6.1）
  └─ /docs 等         404（路由根本不存在，§4.4）

后端进程（uvicorn，1 个，监听 127.0.0.1:8000）
  ├─ /var/lib/prdwright/prdwright.db   SQLite（WAL）+ -wal / -shm 边车
  ├─ /etc/prdwright/master.key         主密钥（600），解密库里的模型密钥
  ├─ /opt/prdwright/skills             技能资产（只读）
  └─ /var/log/prdwright/prdwright.log* 轮转日志
        │
        └─ HTTPS 出网 → 模型供应商 API（唯一的外部依赖）
```

Nginx 是**唯一对外**的进程。后端只监听回环地址，公网没有任何路径能绕过 Nginx 直接碰到
ASGI 端口——这比「监听 `0.0.0.0` 靠防火墙挡」少一层假设：防火墙规则被改、被重置、换机器时
忘记同步，都不会把 8000 端口裸出去。

### 2.2 四个目录：按「会不会被替换」切分

| 路径 | 放什么 | 生命周期 | 属主 / 权限 |
| --- | --- | --- | --- |
| `/opt/prdwright` | 代码 + `.venv` + `skills/` + `backend/migrations/` | **可整目录替换**（每次发布） | `root:prdwright`，750 |
| `/var/lib/prdwright` | `prdwright.db`（+ WAL 边车） | **不可重建**，只能备份恢复 | `prdwright:prdwright`，700 |
| `/var/www/prdwright` | `releases/<时间戳>/{user,admin}` + `current` 符号链接 | 可随时重建（重新 build） | `root:root`，755 |
| `/var/log/prdwright` | 轮转日志 | 可丢（丢了不故障，只影响排查） | `prdwright:prdwright`，750 |

外加两个非目录的落点：`/etc/prdwright/backend.env`（启动项，600）与
`/etc/prdwright/master.key`（主密钥，600）。

> **为什么必须分家**：v1 时期的部署摆法是把一切都放在 clone 出来的那棵树里
> （`backend/prdwright.db`、`backend/logs/`、`frontend/dist/`、`.env` 也在 `backend/` 下）。
> 那套摆法让四类生命周期完全不同的东西共用一个目录，于是「回滚代码」和「保住数据」
> 变成同一个动作的两半——发布时要么先把库挪走，要么赌这次不会失败。
> 分家之后：**换代码 = 换 `/opt`；备份 = 抄 `/var/lib`；静态坏了重建；日志可直接清**。

⚠️ `/opt/prdwright` 里有两样东西**不是可选项**，漏了服务起不来或悄悄降级：

| 路径 | 谁需要它 | 漏了会怎样 |
| --- | --- | --- |
| `backend/migrations/`（含 `versions/` 与 `env.py`） | `core/schema.py::prepare_schema` 把 `script_location` 指向它 | 启动即失败（Alembic 找不到脚本目录），报错与数据库无关，容易误判成库坏了 |
| `skills/`（三个技能包） | `skill_registry.skills_root()` + `PRDWRIGHT_SKILLS_STRICT=true` | 启动即失败（这正是 strict 的用处）；若 strict 为 false，会静默退回内置提示词 |

### 2.3 目标机基线

| 项 | 取值 | 为什么是这个 |
| --- | --- | --- |
| 发行版 | **Ubuntu 24.04 LTS**（或任何自带 Python ≥3.12 的发行版） | `backend/pyproject.toml` 写死 `requires-python = ">=3.12"`。Ubuntu 22.04 自带 3.10，**不满足**——要么升系统，要么 `uv python install 3.12` |
| 规格 | 2 vCPU / **4 GB** 内存 / 40 GB 磁盘（2 GB 能跑，见风险表） | 构建两份前端时 Node 峰值约 1 GB；运行时进程常驻约 300 MB；SQLite 与日志同盘 |
| Python | 3.12（`uv python install 3.12` 自有版本，不用系统 apt 那个） | 见上 |
| uv | `/usr/local/bin/uv`（官方安装脚本） | 依赖真源是 `pyproject.toml` + `uv.lock`，**仓库没有 requirements.txt** |
| Node | `^22.18.0 \|\| >=24.12.0`（两个工程的 `engines`，Vite 8 要求） | 系统源里的旧 Node 会在 `npm ci` 阶段报 `EBADENGINE`，症状看起来像依赖坏了 |
| Nginx | 系统包 | `nginx -t` 通过即可 |
| 端口 | 对外只放行 80 / 443 / SSH | 从外网扫不到 8000 |

### 2.4 进程与端口

| 进程 | 监听 | 对外 |
| --- | --- | --- |
| Nginx | `0.0.0.0:80`、`0.0.0.0:443` | 是 |
| uvicorn（后端） | `127.0.0.1:8000` | 否 |

---

## 3. 进程模型：单 worker 是硬约束

这是整个部署方案里唯一不可绕开的约束，也是它与「找一个通用 FastAPI 部署教程」
最大的分歧点：**本项目的正确性依赖「只有一个进程」**。

### 3.1 内存态清单（v6 之后的版本）

| 状态 | 位置 | 多进程的后果 |
| --- | --- | --- |
| 任务运行时表 `_RUNTIMES: dict[int, JobRuntime]` | `services/job_service.py` | 在 A 进程建的任务，B 进程查不到 → 快照恢复失败、取消无效 |
| 事件总线 `JobBus._subscribers` | `services/job_bus.py`（源码注释原文：**仅进程内，单实例**） | 订阅落在 B 进程，任务在 A 进程 publish → **浏览器永远收不到一个事件** |
| 运行时配置快照（成功 5 秒 / 降级 1 秒 TTL） | `core/runtime_config.py` | 后台改了配置，只有被改的那个进程生效 |
| SQLAlchemy 固定连接池（`pool_size=4`、`max_overflow=0`） | `core/engine.py` | 每个进程各 4 条连接，同一份 SQLite 文件的写冲突从「不存在」变成「偶发 `database is locked`」 |

其中第二条最致命，而且**不报错**：两个 worker 各自持有一部分订阅，用户点「生成」后有一半
概率什么字都不出，刷新后又好了。这类「时好时坏」在单机上极难归因——它看起来像模型抽风，
实际是进程调度。

> v6 把第四条从「自建 444 行池」换成了 SQLAlchemy 的 `QueuePool`，约束本身没变：
> **连接只在启动期建满，运行期一条都不新建**（`max_overflow=0` 是这条不变量的落地方式）。
> 它对部署的直接影响写在 §4.3：启动路径里必须有 `warmup()`。

### 3.2 由此推出的三条发布纪律

1. **只起 1 个进程，且不引 gunicorn。**
   gunicorn 在本项目的唯一价值是多 worker，而多 worker 正是被禁止的；多一层进程管理器
   只会多出「父进程 / 子进程谁在重启」的新疑问。直接
   `uvicorn backend.main:app --workers 1`。
2. **重启会中断在飞任务。** `lifespan` 里的 `job_service.reap_orphans()` 会在启动时把上次
   残留的 `running` 任务收成失败。所以发布要**先排空**（§10.1 第 0 步）。
3. **迁移与重启是同一个动作。** `prepare_schema()` 在 `lifespan` 里跑（§4.2），
   所以「重启服务」同时包含「改表结构」。备份必须排在重启**之前**。

### 3.3 将来要横向扩展，必须先做这三件事

顺序不能颠倒，做完一件才能谈下一件：

1. 把 `_RUNTIMES` 与 `JobBus` 换成跨进程设施（任务队列 + 发布订阅）；
2. 把运行时配置的进程内快照换成「写后广播失效」；
3. 评估 SQLite 的写并发（连接池已是每进程固定 4 条），必要时迁到 PostgreSQL。

**本期不做**，但写在这里是为了：将来有人想「先加个 worker 试试」时，第一眼能看到
这三条前置条件，而不是先去调 `--workers`。

---

## 4. 启动路径

这一节是 v7 相对 v5 素材**新增**的。原因很实在：v6 换掉了建库方式（Alembic 取代
`user_version` 手写链），「第一次起服务会发生什么」整件事都变了；不懂这段，
排障时会按旧经验误判。

### 4.1 `lifespan` 的九步 + 一条启动记录（顺序不是随意的）

```text
1. configure_logging()          最前面：连「建库失败」都要带时间与来源
2. prepare_schema(get_engine()) 建库 / 迁移（§4.2）
3. warmup(get_engine())         ★ v7 新增（T17.1 落地）：启动期把连接池建满（§4.3）
4. invalidate()                 让配置快照失效，别沿用「表不存在 → 降级」那份
5. install_log_persistence()    必须在建表之后；否则启动期这批日志全部写库失败
6. ensure_bootstrap()           邀请码播种 + 首个管理员（仅 users 表为空时）
7. ensure_dev_user()            仅 AUTH_ENABLED=false 时（生产不会走到）
8. reap_orphans()               把上次进程残留的 running 收成 failed
9. ensure_skills(strict)        技能自检；strict=true 时有问题即中止启动
→  一条 service.started 日志：记下实际生效的供应商、config_source 与技能包指纹
```

几处「位置决定行为」的地方：

- **日志配置在第 1 步**：写成模块级 `logging.basicConfig()` 的话，建库失败的报错会是裸文本，
  而建库失败恰恰是最需要看清的一条。
- **`install_log_persistence()` 必须晚于 `prepare_schema()`**：它写的是 `app_logs` 表。
  提前挂上，启动这几分钟的所有日志会写库失败并降级——「启动日志恰好没有」是最影响排障、
  又最不容易被察觉的一种丢数据。
- **`reap_orphans()` 在生成任务之前**：它保证「库里显示的 running」与「真的在跑」一致。
  它执行的那一刻，上一个进程的所有在飞任务已经被判定为失败。

### 4.2 `prepare_schema()` 的三条分支

| 库的状态 | 行为 | 结果 |
| --- | --- | --- |
| 空库（除了 `sqlite_sequence` 没有任何表） | `alembic upgrade head` | 建全量结构，写入 `alembic_version = 0001` |
| 有结构、但**没有** `alembic_version` | 先与「用模型建出来的库」逐列逐索引对拍 → 一致则 `stamp head` | **一行 DDL 都不执行**，只登记版本 |
| 已接管（有 `alembic_version`） | `alembic upgrade head` | 正常增量迁移 |
| **结构与模型不一致**（含 v20 之前的老库） | 抛 `SchemaMismatchError`，**拒绝启动**并打印逐条差异 | 起不来，但看得见原因 |

三个对部署有直接影响的推论：

1. **「拒绝启动」是设计，不是故障。** v6.1 删掉了旧迁移链（ADR-083），老库不会再被
   悄悄 `stamp` 成最新——因为悄悄 `stamp` 会把「少两张表」这类差异永久藏起来，
   等到某次生成才以 `no such table` 的形式炸出来。老库的处理方式是「用当时的代码升到 v20，
   或直接重建」，文档写在异常消息里。
2. **`stamp` 分支会建一个临时库。** `core/schema.py::_models_snapshot()` 用
   `tempfile.TemporaryDirectory()` 建一个「预期形状」的库来对拍。所以 systemd 必须给它
   可写的临时目录（§9.1 的 `PrivateTmp=yes`），否则这一分支会以权限错误失败——
   而这个错误长得像数据库问题。
3. **重启 = 跑一次 `upgrade head`。** 只要迁移是「只追加」，重复执行是幂等的；
   但**它不可逆**，回滚方式见 §8.4。

### 4.3 连接池必须在开始服务之前建满

`core/engine.py` 的 `warmup(engine)` 在**启动期单线程**借满 `DB_POOL_SIZE` 条连接再归还。
它的必要性来自两个事实的组合：

- `max_overflow=0`：池满即等待，**运行期不会新建连接**；
- SQLAlchemy 的池是懒建的：不预热的话，连接会在「第一次真被借用」时才建。

**谁会同时借？** 只有两个角色（按当前代码核过）：

| 借用者 | 并发性 |
| --- | --- |
| HTTP 请求 | 串行（路由都是 `async def`，内部同步 DB 调用会阻塞事件循环） |
| `log-store` 后台线程（写 `app_logs`） | 与主线程并发 |

所以窗口在**启动期**：`install_log_persistence()` 起线程之后，主线程还要跑
`ensure_bootstrap()` / `reap_orphans()` / `ensure_skills()`，两边可能同时建连接。
启动完成后反而不会——那时池里那几条已经建好了。

而 T15 那个既有竞态正是「多线程同时首次映射 WAL 的 `-shm` 会返回 `SQLITE_READONLY`」——
`busy_timeout` 对 `SQLITE_READONLY` **不生效**，所以它不会重试，只会失败。
失败信息看起来像「数据库文件是只读的」，会把人引去查文件权限（典型查错方向）。

> ✅ **已修（T17.1）**：`lifespan` 第 3 步调用 `warmup(get_engine())`，并记一条
> `service.pool_warmed` 日志（带 `size`）。
>
> **修之前实测的数字**：不预热时 `engine_stats()` 是 `{'size': 4, 'in_use': 0, 'idle': 1}` ——
> 池里真的只有 1 条。注意这里的 `size: 4` 是**假象**：`QueuePool.size()` 返回的是
> `maxsize`（配置容量），与实际建了几条无关。所以「池子建满了没有」只能看
> `checkedin()`，`engine_stats().idle` 已按此修正（原来算的是 `maxsize - in_use`，
> 恒等于容量，会把「没预热」这个退化完全盖住）。

### 4.4 接口文档只给管理员看

FastAPI 默认注册 `/docs`、`/redoc`、`/openapi.json`，而这三条同时躺在
`api/deps.py::_PUBLIC_EXACT` 白名单里——也就是说**未登录也能看**，看的还是全部接口形状
（含管理员接口的参数结构）。

v7 的做法是**把文档搬到管理员名下**，而不是「生产直接关掉」：

| 地址 | 谁能进 | 是什么 |
| --- | --- | --- |
| `/api/admin/openapi.json` | 管理员 | 完整 schema（JSON），三个页面都引用它 |
| `/api/admin/docs` | 管理员 | Swagger UI，可展开、可在线发请求 |
| `/api/admin/redoc` | 管理员 | ReDoc，只读三栏 |
| `/docs`、`/redoc`、`/openapi.json`、`/docs/oauth2-redirect` | **没人（路由不存在）** | 框架默认那四条，`main.py` 传三个 `None` 后不再注册 |
| 管理端 `/admin/api-docs` 页 | 管理员 | 把上面两个 HTML 页摆成两个按钮（实现见 `ApiDocsView.vue`） |

四层各管一件事：

| 层 | 做法 | 为什么不能省 |
| --- | --- | --- |
| 后端 | `FastAPI(docs_url=None, redoc_url=None, openapi_url=None)` | 任何绕过 Nginx 的路径（本机 `curl`、误开的端口、SSH 隧道）都看不到 |
| 鉴权 | 三条管理员路由逐条挂 `require_admin` | 「能打开页面」与「能拿到 schema」是两次请求，两次都要挡 |
| 白名单 | 三条从 `_PUBLIC_EXACT` 删掉 | 白名单只管「谁能匿名访问」；留着等于给「将来把 docs 打开」预置口子 |
| Nginx | `location ~ ^/(docs\|redoc\|openapi\.json)($\|/) { return 404; }` | 「后端哪天被改回默认」时还有一道 |

三个细节，漏了会以为已经关干净：

1. **框架注册的是四条，不是三条。** 除了 `/docs` / `/redoc` / `/openapi.json`，
   还有 `/docs/oauth2-redirect`（Swagger UI 的 OAuth2 重定向页）。
   传 `docs_url=None` 会连带去掉它，但 **Nginx 的正则不能加 `$`**，否则匹配不到。
2. **未登录看到的是 401，不是 404。** `AuthMiddleware` 跑在路由之前，而 `/docs` 已不在
   白名单。只有**已登录**的普通用户才会走到路由表、拿到 404（路由真的不存在）。
   所以「公开文档已关闭」的验证不能只断言 404。
3. **管理员那两个页面靠 Cookie 认身份。** 浏览器新标签页打开 `/api/admin/docs` 是同源
   顶层导航，会带上会话 Cookie；页面里 Swagger UI 再同源 `fetch` schema，同样带 Cookie。
   若将来前端改成 Bearer token，这两个页面会一起失效。

> **一个外部依赖要记住**：Swagger UI / ReDoc 的 JS/CSS 走 CDN（`api/admin_docs.py`
> 顶部三个常量）。浏览器需要能访问 jsdelivr；内网环境要自托管时**只改那三个常量**。

### 4.5 启动失败的四种典型形态（对号入座）

| 现象 | 真实原因 | 处置 |
| --- | --- | --- |
| 进程退出，日志里有 `SchemaMismatchError` + 逐条差异 | 库结构与模型不一致，或这是 v20 之前的老库 | 别 `stamp`。要么用当时的代码升到 v20，要么按 §8.5 从备份恢复 / 重建 |
| 进程退出，`RuntimeError: 技能资产自检未通过` | `PRDWRIGHT_SKILLS_STRICT=true` 且 `skills/` 没打包 / 元数据读不动 | 按报错里给出的 `skills 根目录` 修路径或补文件。这条失败的**价值**就是当场炸 |
| 进程退出，`Address already in use` | 上一版进程没退干净，或另一个服务占了 8000 | `systemctl status prdwright`；`ss -lntp \| grep 8000` |
| **进程起来了，`/health` 的 `llm` 报「未配置密钥」** | 十有八九是 `PRDWRIGHT_MASTER_KEY_PATH` 没进进程环境（写进了 `.env` 而没进 `EnvironmentFile`），或密钥文件权限不对 | 见 §7.3。⚠️ 这条**不是**启动失败，而是「能起但用不了」——最容易被当成「后台没填密钥」而查错方向 |

> 为什么最后一条会这样：`/health` 的 `llm` 项走 `provider_key_present()` →
> `crypto.is_configured()`，而后者对「解不开」的返回是 `False`（不抛）。
> 「主密钥丢了」和「密钥没填」在体检里长得一样。真正的完整报错
> （`找不到主密钥文件：…`）只在**第一次真正解密**时抛出，也就是第一次生成或第一次保存密钥时。

---

## 5. 前端产物与路由基址

两个前端是**两个独立的 Vite 工程**（ADR-039），各自 build 出一份 `dist/`，互不影响。

### 5.1 用户端：站点根 `/`

`frontend/vite.config.ts` 未设 `base`（默认 `/`），`src/router.ts` 的
`createWebHistory()` 也以 `/` 为基址，**部署无需改动**，产物直接作为站点根。

### 5.2 管理端：挂在 `/admin/`

`frontend_admin/` 按「站点根」写的（`createWebHistory()` 无参），挂到子路径需要两处改动：

| # | 改动 | 不做的后果 |
| --- | --- | --- |
| 1 | `vite.config.ts` 加 `base: '/admin/'` | 产物里的 JS / CSS 仍指向 `/assets/...`，命中用户端的资源或直接 404 |
| 2 | `src/router.ts` 改 `createWebHistory(import.meta.env.BASE_URL)` | 不带参数时回退到 `/`，浏览器地址 `/admin/dashboard` 会被路由当成未知路径 → 空白页 |

> 第 2 条容易漏：Vue Router 的 `normalizeBase()` 在没传参时读 `<base>` 标签、再兜底 `/`，
> **不会**自动跟随 Vite 的 `base`。两个「base」是两件事，必须显式接上。

**dev 也走同一个 base**（已落地，不是「以后再收口」）：本地地址变成
`localhost:5174/admin/`。接受这个代价，**刻意不做** `mode === 'production' ? '/admin/' : '/'`
那样的双轨——双轨会让「挂子路径」这件事在本地永远测不到，任何写死 `/xxx` 的跳转
都要等到部署那天才炸。代价只是每个开发者改一次书签。

> 一个已经踩到的细节：`index.html` 里的 `<link rel="icon" href="/favicon.ico">`
> **只有文件真的存在于 `public/` 时**才会被 Vite 改写成 `/admin/favicon.ico`。
> 管理端原本没有 `public/` 目录，这一行就原样留着 → 浏览器去站点根要图标（404 或拿到用户端的）。
> 已补 `frontend_admin/public/favicon.ico`（与用户端同一个文件），构建后确认产物里是
> `/admin/favicon.ico`。

### 5.3 静态资源缓存策略

Vite 产出的 `assets/*` 文件名带内容哈希，内容变则文件名变，因此可以**长缓存**；
`index.html` 是唯一没有哈希的入口，必须**每次回源**：

| 资源 | 策略 |
| --- | --- |
| `assets/*`（带哈希） | `Cache-Control: public, max-age=31536000, immutable` |
| `index.html` | `Cache-Control: no-store` |

> 反过来配的后果很具体：发新版后旧 `index.html` 引用的那批哈希文件已被删除，用户拿到
> 的是「页面能打开、点什么都 404」，而清缓存就好了——这种锅最难查，因为开发者自己
> 几乎不会遇到（每次都是硬刷新）。

### 5.4 为什么管理端不单独开子域

讨论过 `admin.example.com`，否决。理由是 **Cookie 是 host-only 的**：后端在
`api/login_flow.py` 里 `response.set_cookie(...)` **没有 `domain` 参数**，这个 Cookie 只属于
写入它的那个完整主机名。两个子域的实际后果：

- 在 `admin.example.com` 登录 → Cookie 属于该子域；
- 页面请求 `app.example.com/api/...` → 浏览器**不带**这个 Cookie → 每次都是 401；
- `samesite="lax"` 也不会让跨子域请求自动携带。

要修就得同时做三件事（后端设 `domain=.example.com`、CORS 放开带凭据、多一张证书），
换来的是「后台地址更好记」。不划算。

**同域同站（`/` 与 `/admin/`）则什么都不用做**：请求同源，CORS 根本不参与判定；
Cookie 天然共享，两个前端的登录态自动一致。这也是 §7.2 里 `CORS_ORIGINS` 可以设空的原因。

---

## 6. Nginx 契约

### 6.1 路由表

| 路径 | 去向 | 说明 |
| --- | --- | --- |
| `/` | 静态 `current/user` | SPA：`try_files` 兜底 `index.html` |
| `/assets/` | 同上 | 长缓存；缺失即 404（不兜底成 `index.html`） |
| `/admin`、`/admin/` | 静态 `current/admin` | `/admin` 301 到 `/admin/` |
| `/admin/assets/` | 同上 | 长缓存 |
| `/api/` | 反代 `127.0.0.1:8000` | SSE 专项配置，见 §6.3 |
| `/health` | 反代，**仅本机** | 单机部署没有外部探针，收紧到 `127.0.0.1` |
| `/docs`、`/redoc`、`/openapi.json`、`/docs/oauth2-redirect` | **404** | 路由根本不存在（§4.4），Nginx 再挡一道 |
| `/api/admin/docs`、`/api/admin/redoc`、`/api/admin/openapi.json` | 反代 → 后端，`require_admin` | 接口文档的唯一入口：匿名 401、普通用户 403、管理员 200 |

### 6.2 站点骨架

```nginx
# /etc/nginx/conf.d/prdwright.conf
upstream prdwright_api {
    server 127.0.0.1:8000;
    keepalive 8;
}

server {
    listen 80;
    server_name example.com;
    # 证书签发期（certbot webroot）要留这条
    location ^~ /.well-known/acme-challenge/ { root /var/www/acme; }
    location / { return 301 https://$host$request_uri; }
}

server {
    listen 443 ssl;
    http2 on;
    server_name example.com;

    ssl_certificate     /etc/letsencrypt/live/example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/example.com/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    add_header Strict-Transport-Security "max-age=31536000" always;

    # 四个「无副作用」的安全头。刻意**不加 CSP**：两个前端的 index.html 里有内联的
    # 首帧主题脚本（防暗色刷新闪白），上 CSP 要么放行 unsafe-inline（等于没加），
    # 要么改造成 nonce（要动构建链路）。收益与代价不成比例，本期不做。
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    add_header Permissions-Policy "geolocation=(), microphone=(), camera=()" always;

    gzip on;
    gzip_types text/plain text/css application/javascript application/json image/svg+xml;
    gzip_min_length 1024;

    client_max_body_size 10m;   # 需求描述 / 粘贴的接口文档可能不小，默认 1m 会 413

    # ---------------- 用户端 ----------------
    location / {
        root /var/www/prdwright/current/user;
        try_files $uri $uri/ /index.html;
    }
    location = /index.html {
        root /var/www/prdwright/current/user;
        add_header Cache-Control "no-store" always;
    }
    location ^~ /assets/ {
        root /var/www/prdwright/current/user;
        expires 1y;
        add_header Cache-Control "public, immutable" always;
        try_files $uri =404;
    }

    # ---------------- 管理端 ----------------
    location = /admin { return 301 /admin/; }
    # ⚠️ 这条不能省：`/admin/index.html` 否则会落到下面的 `^~ /admin/`，
    # 拿不到 no-store。发新版后用户会带着旧 index.html 去请求已被替换掉的 hash 资源，
    # 表现是「页面能打开、点什么都 404」。用户端有 `location = /index.html` 管着，这里对称补上。
    location = /admin/index.html {
        alias /var/www/prdwright/current/admin/index.html;
        add_header Cache-Control "no-store" always;
    }
    location ^~ /admin/assets/ {
        alias /var/www/prdwright/current/admin/assets/;
        expires 1y;
        add_header Cache-Control "public, immutable" always;
        try_files $uri =404;
    }
    location ^~ /admin/ {
        alias /var/www/prdwright/current/admin/;
        index index.html;
        try_files $uri $uri/ /admin/index.html;
    }

    # ---------------- 健康检查（仅本机） ----------------
    location = /health {
        proxy_pass http://prdwright_api/health;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Connection "";   # 配合 upstream keepalive
        allow 127.0.0.1;
        deny all;
    }

    # ---------------- API 文档：公开的一律 404 ----------------
    # ⚠️ 正则结尾必须是 ($|/) 而不是 $：框架默认还会注册 `/docs/oauth2-redirect`，
    # 只匹配 `/docs` 会漏掉它。后端那侧其实已不注册这四条（§4.4），这里是第二道。
    # `/api/admin/docs`、`/api/admin/redoc` 不需要单独配置——它们以 `/api/` 开头，
    # 由下面的反代段处理，鉴权在后端 `require_admin`。
    location ~ ^/(docs|redoc|openapi\.json)($|/) { return 404; }

    # ---------------- 业务 API ----------------
    location ^~ /api/ {
        proxy_pass http://prdwright_api;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection        "";

        # ---- SSE 三件事 ----
        proxy_buffering    off;   # 1) 关缓冲，否则流式攒到最后一次性吐出
        proxy_cache        off;
        chunked_transfer_encoding on;
        proxy_read_timeout 1800s; # 2) 长任务：一篇 PRD 含多轮 LLM 调用
        proxy_send_timeout 1800s;
        gzip               off;   # 3) 压缩会重新引入缓冲

        client_max_body_size 10m;
    }
}
```

### 6.3 SSE：为什么这几行不能少

`GET /api/jobs/{id}/stream` 是长连接流式响应。Nginx 默认开启响应缓冲，会把后端一块一块
发出的数据攒在内存里再一次性转发——表现为「转圈很久，然后整篇刷出来」，超时还会变成错误页。

三行各有各的职责，缺一个都不行：

| 配置 | 缺了会怎样 |
| --- | --- |
| `proxy_buffering off` | 流式特效消失，退化为「等很久一次性出」 |
| `proxy_read_timeout 1800s` | 默认 60 秒；一次长生成期间只要 60 秒没有新事件就被切断 |
| `gzip off` | gzip 自身带缓冲，会把关掉的缓冲又加回来 |

后端 `api/sse.py` 已经带了 `X-Accel-Buffering: no` 响应头，与 `proxy_buffering off`
是**同一件事的两道保险**：后者防 Nginx，前者防其它反向代理（以及将来换网关时忘了配）。

**为什么还要服务端心跳（v7 从「可选」升为必做）**：分章生成是「撰写 → 审核 → 改写」串起来的，
一章里可能连续几分钟没有任何 `chunk`；两次事件之间连接完全静默。1800 秒只放宽了我们自己
这一跳，运营商 NAT、企业网关仍会按自己的空闲超时（常见 60 秒）**静默断流**——
表现是「页面卡住不动、没有任何错误」，最难归因。

心跳是 SSE 规范内的注释帧，`EventSource` 会忽略它：

```text
: ping

```

实现约束（写在 ADR-059 的「v7 修订」里）：必须在 `make_sse_generator` 的**同一个出口**
上做（SSE 分帧全项目只此一处），且不得产生任何业务事件、不得干扰
`stage` / `chunk` / `part` / `error` 的形状断言。

---

## 7. 配置与密钥：上线前的清单

### 7.1 两类配置，两个入口

延续 ADR-041 / ADR-052：**决定「进程怎么起来」的项走环境（改了必须重启），决定「业务怎么跑」
的项走管理后台（热生效，5 秒快照内一致）**。生产一律用 systemd 的 `EnvironmentFile`
提供环境项，**不在代码目录放 `.env`**。

热配置的白名单在 `core/runtime_config.py::WHITELIST`（审核 / 降级 / 预算 / 额度 / 安全五项）。
其中 **`auth_cookie_secure` 是热配置**——后台改完下一次登录即生效；但生产仍建议在
`EnvironmentFile` 里写死 `true`，避免「某次被后台误关」。

### 7.2 上线必须设定的项

模板落点：`deploy/backend.env.example`（T17.0 新增，逐项带生产值与理由）。

| 项 | 生产值 | 为什么 |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | 只让 Nginx 能连（§2.4） |
| `PORT` | `8000` | 与 upstream 一致 |
| `DB_PATH` | `/var/lib/prdwright/prdwright.db` | 留空会落在代码目录 `backend/` 下（§2.2） |
| `DB_POOL_SIZE` | `4`（默认） | 运行期不扩容；调大只在「借不到连接」时才有意义 |
| `LOG_DIR` | `/var/log/prdwright` | 留空会落在代码目录 `backend/logs/` 下 |
| `LOG_FORMAT` | `json` | 一行一条，字段与 `text` 完全相同，切换不丢信息 |
| `PRDWRIGHT_SKILLS_DIR` | `/opt/prdwright/skills` | 部署时不猜「向上查找」的结果 |
| `PRDWRIGHT_SKILLS_STRICT` | `true` | 漏打包技能包应当**起服务时就炸**，而不是悄悄退回内置提示词 |
| `PRDWRIGHT_MASTER_KEY_PATH` | `/etc/prdwright/master.key` | ⚠️ 必须是**进程环境**，写 `.env` 无效 |
| `AUTH_ENABLED` | `true` | 默认已是 true，但生产必须显式写死，防止被误关后全站裸奔 |
| `AUTH_COOKIE_SECURE` | `true` | HTTPS 下必须为 true，否则浏览器不回传会话 Cookie |
| `CORS_ORIGINS` | **空串** | 同域同站部署，CORS 不参与；留默认值等于在生产上给 `localhost:5173/5174` 一个跨源许可（v7 修订，ADR-054） |
| `TZ` | `Asia/Shanghai` | 影响日志时间与「按本地日期」的统计口径 |
| `INVITE_CODE` / `INIT_ADMIN_*` | 仅首次部署 | 见 §7.4 |

### 7.3 主密钥

- **生成**：`cd /opt/prdwright/backend && uv run python ../scripts/keygen/generate_keys.py`，
  产出 `backend/secrets/master.key`；部署时移到 `/etc/prdwright/master.key`，
  `chmod 600`、`chown prdwright:prdwright`。
- **它决定一切**：库里的模型密钥全是它加密的。**丢了它，数据库里那批密文全部解不开**，
  且无法找回——这是设计内的自毁（ADR-053）。
- **读取路径只认进程环境**：`core/crypto.py` 用 `os.environ.get("PRDWRIGHT_MASTER_KEY_PATH")`。
  `pydantic-settings` 读 `.env` **不会**把内容写进 `os.environ`，所以写进 `.env` 是**无效**的
  （`skill_registry` 当年踩过同一个坑，后来才改成 `lookup_env`）。
- **轮换顺序**：`generate_keys.py --rotate` 会**先重加密库里全部 `value_token`，最后才换文件**。
  中途崩溃时旧主密钥还在，可恢复；反过来先换文件再改库，中断即不可逆。
- **备份**：随每次备份同批归档，**保留每一把**（旧备份要旧密钥才能解开），且与数据库分开存放。

### 7.4 首个管理员

`INIT_ADMIN_USER` / `INIT_ADMIN_PASSWORD` **只在 `users` 表为空时**生效
（`auth_service.ensure_bootstrap`）。幂等，重复启动不会重复建号，也不会改已有账号的密码。

第一次起服务用它建号，随后：

1. 立刻登录后台确认可用；
2. **把这两行从 `EnvironmentFile` 里删掉**——它们已完成使命，留着只是给「某次空库重建」
   留一个弱口令后门。

---

## 8. 数据与备份

### 8.1 为什么不能直接 `cp` 数据库文件

`core/engine.py` 在每条连接上执行 `PRAGMA journal_mode=WAL`：数据先写
`prdwright.db-wal`，再择机合并回主文件。直接复制 `prdwright.db` 拿到的是「合并前」的快照，
而 `-wal` / `-shm` 两个边车文件与它不是一个时刻的一致视图——拷出来的库可能打不开，
或者**静默缺最近几分钟的写入**（后者更糟：它不报错）。

必须用 SQLite 自己的在线备份接口：

```bash
# 二选一，都保证一致视图
sqlite3 /var/lib/prdwright/prdwright.db ".backup '/var/backups/prdwright/db-20261023.db'"
sqlite3 /var/lib/prdwright/prdwright.db "VACUUM INTO '/var/backups/prdwright/db-20261023.db'"
```

`VACUUM INTO` 顺带整理碎片（产物更小），代价是额外读一遍全库。v7 的备份脚本走
Python 的 `sqlite3` 模块执行同一条语句（ADR-087）。

### 8.2 备份什么

| 对象 | 频率 | 保留 | 备注 |
| --- | --- | --- | --- |
| 数据库（`VACUUM INTO`） | 每日 | 近 14 份 | 文件名带时间戳 |
| 主密钥 `master.key` | 变更时 + 随每次备份 | **全部版本** | 用指纹（如 `master.key.<sha256前8位>`）命名，避免互相覆盖 |
| `backend.env` | 变更时 | 最新 1 份 | 无密钥值，可以放一起 |
| `manifest.json` | 随每次备份 | 与数据库同寿命 | 记 `alembic_version`、字节数、sha256、备份时刻 |

> ⚠️ **旧备份配旧密钥**：轮换主密钥之后，此前那份数据库备份里的密文只能用**旧**的
> `master.key` 解开。所以密钥备份不是「留最新一把」，而是**留每一把**。

### 8.3 备份必须自检（v7 新增）

「文件生成了」不等于「备份可用」。备份脚本在收尾时做三件事，任一失败即以非 0 退出：

1. 用 `sqlite3` 打开备份文件并执行 `PRAGMA integrity_check`（期望 `ok`）；
2. 读出 `alembic_version` 并写进 `manifest.json`（恢复时要靠它判断补不补迁移）；
3. 记录 sha256 与字节数。

### 8.4 迁移不可回滚 → 发布顺序被它钉死

`prepare_schema()` 在 `lifespan` 里跑，也就是说**「重启服务」这一个动作同时包含「改表结构」**。
而迁移纪律是「只追加、不回改」（ADR-029 起确立）：新代码建了新列，旧代码不会删它，
但**旧代码未必能在新结构上正常运行**。

由此推出唯一正确的发布顺序（§10.1）：

```text
排空 → 备份 → 换代码 → 发布静态 → 重启（迁移在此自动发生）→ 冒烟
                                      ↑
                            失败时：用备份恢复，而不是「反向迁移」
```

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

---

## 9. 进程管理（systemd）

### 9.1 unit

```ini
[Unit]
Description=PRDwright Backend
After=network.target

[Service]
Type=simple
User=prdwright
Group=prdwright
WorkingDirectory=/opt/prdwright/backend
EnvironmentFile=/etc/prdwright/backend.env
Environment=PATH=/usr/local/bin:/usr/bin:/bin
Environment=HOME=/var/lib/prdwright
ExecStart=/opt/prdwright/backend/.venv/bin/uvicorn backend.main:app \
          --host 127.0.0.1 --port 8000 --workers 1 \
          --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=always
RestartSec=5
TimeoutStopSec=30

# 最小加固：只放行确实要写的目录
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/lib/prdwright /var/log/prdwright

StandardOutput=journal
StandardError=journal
SyslogIdentifier=prdwright

[Install]
WantedBy=multi-user.target
```

几处刻意的写法：

- **用 `.venv/bin/uvicorn` 的绝对路径**，不写 `uv run uvicorn`：后者会在启动时走一遍依赖
  解析，把「起服务」押在 uv 缓存与网络上。依赖解析是**发布时**（`uv sync --frozen`）该做的事。
- **`--workers 1` 显式写出来**：它不是默认值省略，而是把 §3 的约束钉在能看到的地方。
- **`--proxy-headers --forwarded-allow-ips=127.0.0.1`**：让 uvicorn 采信 Nginx 传来的
  `X-Forwarded-*`，日志里的客户端 IP 才是真实来源；`allow-ips` 限死本机，防止有人直接
  伪造该头打到 8000。
- **`PrivateTmp=yes` 是承重的**，别当成可选加固：`prepare_schema` 的 `stamp` 分支要建
  临时库做结构对拍（§4.2），没有可写临时目录时它会以权限错误失败。
- **`ProtectSystem=strict` + `ReadWritePaths`**：整个文件系统只读，只开两个口子。
  这样「某个路径忘了授权」会以明确的只读错误暴露，而不是等到备份时才发现日志没写进去。

### 9.2 为什么不引 gunicorn

v1 时期的文档用 gunicorn 是为了多 worker。本项目从 v2 起就有进程内后台任务与事件总线，
到 v4 又多了运行时配置缓存，到 v6 又多了固定连接池。既然**只剩一个 worker**，
gunicorn 就只剩成本：多一层父子进程、多一套超时语义、多一个「为什么重启了两次」的疑问。

### 9.3 优雅停止

`TimeoutStopSec=30` 给日志 handler 留出 flush 时间（`logging_setup` 的后台线程按 ≤50ms
小段等队列，30 秒绰绰有余）。⚠️ **优雅停止只保证「进程自己收干净」，不保证在飞任务**
——任务会随事件循环一起消失，并在下次启动时被 `reap_orphans()` 收成失败。
所以发布前必须排空（§10.1 第 0 步）。

---

## 10. 发布流程与回滚

### 10.1 一次发布的标准步骤

```bash
# 0) 排空：确认没有在飞任务
#    人工：后台「任务监控」页看 running 列表
#    脚本：GET /api/admin/jobs?status=running  → total == 0
# 1) 备份（必做，因为它同时是回滚点；失败即中止）
/opt/prdwright/scripts/deploy/backup.py
# 2) 换代码（不碰 /var/lib、不动 /etc）
cd /opt/prdwright && git fetch --tags && git checkout <tag>
cd backend && uv sync --frozen --no-dev
# 3) 构建两份静态产物到新目录（串行，不要并行）
#    /var/www/prdwright/releases/<时间戳>/{user,admin}
# 4) 原子切换 current 符号链接（ln -sfn，一次 rename）
# 5) 重启（迁移在此自动发生）
systemctl restart prdwright
# 6) 冒烟
/opt/prdwright/scripts/deploy/smoke.py --basic
```

第 4 步用「符号链接整目录切换」，不要「原地覆盖」：覆盖过程中请求会拿到半新半旧的资产集合
（新 `index.html` + 旧 `assets/`），表现为随机白屏。符号链接的 `ln -sfn` 在 POSIX 上是
一次 `rename`，不会出现「链接短暂不存在」的窗口。

### 10.2 回滚

| 故障类型 | 回滚方式 |
| --- | --- |
| 代码有问题（启动失败 / 功能错），**未改结构** | `git checkout <上一个 tag>` → `uv sync --frozen` → 重建静态 → 重启 |
| 代码有问题，**已改结构**（本次发布含新迁移） | **先停服务** → 恢复数据库备份 → 回代码 → 起服务（会再跑一次 `upgrade`，见 §8.4） |
| 只有静态产物坏了 | 把 `current` 指回上一个 `releases/<ts>`，**不需要重启后端** |
| 健康检查 `skills` 红 | 十有八九是 `PRDWRIGHT_SKILLS_DIR` 或打包问题，只改代码目录与 env，不动数据 |

⚠️ 回滚的唯一前提是**第 1 步的备份真的做成了**。备份失败却继续发布，等于当场取消回滚能力。

### 10.3 冒烟清单（分两档）

`smoke.py --basic`（不需要账号，每次发布都跑）：

| # | 动作 | 期望 |
| --- | --- | --- |
| 1 | `GET http://127.0.0.1:8000/health` | `status: ok`，`database` / `llm` / `skills` 三项全绿 |
| 2 | 域名打开首页 | 用户端出现，HTTPS 无告警 |
| 3 | 未登录访问 `/api/solutions` | 401，且响应体带 `code`（不是裸 500） |
| 4 | `/docs`、`/docs/oauth2-redirect`、`/openapi.json`（**经域名**） | 404；本机直连 8000 未登录时是 **401**（中间件先于路由）。判据是「不是 200 且响应体里没有 openapi 字段」 |
| 4b | `/api/admin/openapi.json`（不带管理员身份） | 401（未登录）/ 403（普通用户）；带管理员身份时 200 且含 `paths` |
| 5 | 静态资源 | `index.html` 响应头是 `no-store`；`assets/*` 是长缓存 |

`smoke.py --deep`（需要管理员凭据，**验证流式**）：

| # | 动作 | 期望 |
| --- | --- | --- |
| 6 | 登录 | 成功；浏览器里 Cookie 的 `Secure` 已置位 |
| 7 | 跑一次完整生成 | **文字一段段流式出现**（不是最后一次性刷出） |
| 8 | 生成中刷新页面 | 能从快照恢复，进度不丢 |
| 9 | 打开 `/admin/` | 管理端出现；非管理员被挡（403，文案「账号不存在」） |
| 10 | 后台日志页 | 能看到刚才那次生成的事件（含 `request_id`） |

第 7 条是**唯一能验证 SSE 全链路**的一条，不能省：Nginx 配错了，前面所有页面都正常，
只有这一条会挂。

---

## 11. 最小可观测

| 手段 | 位置 | 用途 |
| --- | --- | --- |
| 健康检查 | `GET /health`（仅本机） | 三项体检：数据库 / 供应商 / 技能资产。`status` 只有 `ok` / `degraded` 两档，且**恒返回 200**——它区分的是「能不能用」而不是「进程活没活」 |
| 应用日志 | `/var/log/prdwright/prdwright.log*`（5 MB × 5 轮转）+ `app_logs` 表 | 带 `request_id`，可在后台日志页按号反查 |
| 服务日志 | `journalctl -u prdwright` | 启动期、崩溃、重启 |
| 访问日志 | Nginx `access_log` | 状态码分布、慢请求 |
| 磁盘告警 | cron / systemd timer | 盯 `/var/lib`（数据库增长）与 `/var/log`（日志增长），任一满了服务都会异常 |

`app_logs` 的裁剪由 `log_retention_days`（默认 7 天）与 `log_max_rows`（默认 5 万）两个闸门
自动执行，不需要额外定时任务。`LOG_DB_ENABLED=false` 可整体关掉落库（文件仍照写）。

> ⚠️ **不要用 `/health` 的状态码做存活探针。** 它恒 200 是设计（技能缺失时服务仍可降级运行，
> 打成失败会让编排器把还能用的容器踢掉）。要判「活着」请看 `systemctl is-active` 或
> `/health` 返回体里的 `checks.database`。

---

## 12. 契约（运维接口）

| 接口 / 文件 | 用途 | 备注 |
| --- | --- | --- |
| `GET /health` | 部署后自检、日常探活 | 无 `/api` 前缀，在鉴权白名单内；返回 `status` + `checks{database,llm,skills}` |
| `GET /api/admin/jobs?status=running` | 排空检查 | 需要管理员会话；`running` 按「跑得最久优先」排序，`is_stuck` 由服务端判定 |
| `GET /api/admin/openapi.json` | 取完整接口定义 | 仅管理员；公开的 `/openapi.json` 已不注册（§4.4） |
| `GET /api/admin/docs`、`/api/admin/redoc` | 在线接口文档 | 仅管理员；浏览器新标签页打开，靠会话 Cookie 认身份 |
| 管理端 `/admin/api-docs` 页 | 两个文档入口的按钮 | 页面本身走后台路由守卫，真边界仍是后端 `require_admin` |
| `/etc/prdwright/backend.env` | 启动项（600） | 改了必须 `systemctl restart` |
| `/etc/prdwright/master.key` | 主密钥（600） | 轮换只能用 `generate_keys.py --rotate` |
| `scripts/deploy/backup.py` | 备份数据库与主密钥 + 自检 | 见 §8.2 / §8.3 |
| `scripts/deploy/restore.py` | 从备份恢复到一个目标路径 | 停服务后执行；不自动起服务 |
| `scripts/deploy/release.py` | 按 §10.1 六步发布 | 任一步失败即中止 |
| `scripts/deploy/smoke.py` | 跑 §10.3 清单 | 退出码非 0 即发布失败 |
| `systemctl restart prdwright` | 唯一的重启入口 | 迁移随之发生 |

---

## 13. 取舍与风险

| 决策 | 得到 | 放弃 |
| --- | --- | --- |
| 单进程单 worker | 内存态语义简单、无跨进程一致性问题 | 纵向扩展上限；无法零停机；一次重启影响全部在飞任务 |
| 不用容器 | 少一层构建与镜像运维，排障时看到的就是真实文件系统 | 环境可复现性依赖 `uv.lock` 与两个 `package-lock.json`（已足够，但不如镜像彻底） |
| 数据库留在单机 SQLite | 零运维、备份就是一个文件 | 无法多副本；单机磁盘故障即数据故障（靠备份缓解） |
| 管理端挂子路径而非子域 | Cookie 天然共享、一张证书、CORS 不参与 | 后台地址不如 `admin.` 好记；需改前端 `base` 与路由基址 |
| 在服务器上构建静态产物 | 不需要构建机与制品库 | 发布窗口变长；2 GB 内存机器构建时需串行并限堆 |
| 备份每日一次 | 简单 | 最坏丢 24 小时数据。要更细就得加 WAL 归档 |
| 发布脚本不自动回滚数据 | 不会「自动」把数据退到一个脚本认为正确的时点 | 出事时要人做判断；因此把恢复步骤写进脚本输出 |

**风险登记**

| 风险 | 缓解 |
| --- | --- |
| 重启中断在飞任务 | 发布前排空（§10.1）；前端会显示失败，用户可重跑 |
| 迁移不可回滚 | 发布前必备份，且备份要自检通过；回滚走恢复备份（§8.4） |
| 主密钥丢失 → 全部密钥解不开 | 与数据库分开备份、保留每一把（§7.3） |
| 主密钥路径没进进程环境 | `/health` 会显示「未配置密钥」而不是报错（§4.5 最后一条）；验收时**必须**跑一次生成，而不是只看体检 |
| 磁盘被数据库或日志写满 | `app_logs` 自动裁剪；磁盘告警覆盖两个目录 |
| 模型密钥被盗刷 | 密钥加密入库，`.env` 与代码里都没有明文；供应商控制台另设消费告警 |
| 2 GB 内存机器构建前端 OOM | 两份前端**串行**构建；`NODE_OPTIONS=--max-old-space-size=1024`；必要时加 swap |
| 静态产物半新半旧 | `releases/<ts>` + 符号链接原子切换（§10.1 第 4 步） |

---

## 14. 与 v6 的关系

v7 **不改** v6 已定型的任何业务能力与数据层契约：

- 生成链路（分章撰写 / 审核 / 改写、`chunk` 与 `part` 两条轨、`stage` 语义）；
- 文档所有权与版本链（正文只住 `document_versions`）；
- 管理员后台、日志子系统、运行时配置与密钥管理；
- 仓库层「不碰事务边界」，`session_scope()` 是唯一事务落点（ADR-073 ~ 083）。

v7 的后端改动只有四处，全部列在 `README.md` 的「与 v6 的关系」一节，且每一条都对应
一个 ADR 编号。除此之外，v7 只新增脚本与配置模板——**部署不是一个功能，它是一次搬动**。
