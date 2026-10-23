# v7 路线图：部署与上线

> 排期真源本文件；架构依据见 `ARCHITECTURE.md`；决策依据见 `ADR.md`。
> 任务编号用于日常排期（T17.x），版本号用于对外收口（v7.x，对照见 `README.md`）。

---

## 0. 排期总原则

1. **先补启动期的洞，再谈部署。** T17.1 排在目标机之前，不是排错了：`warmup()` 现在只有
   测试在调（ADR-084），而它守的是「连接只在启动期建满」这条不变量。部署完再改，
   就要为两行代码重走一遍完整发布。
2. **先让它起来，再让它安全，最后让它好养。** T17.2–T17.4 是「能用」，T17.5 是「敢对外」，
   T17.6–T17.7 是「长期活着」。反过来的顺序（先折腾备份再上线）会把验证反馈推迟太久。
3. **每一阶段都能单独交付**：阶段之间只靠「上一阶段已经成立的东西」衔接，不出现半成品
   互相等待。最典型的是 T17.4：Nginx 配好之前站点不可用，但后端已经在 T17.3 单独验证过。
4. **备份排在第一次改结构之前。** T17.3 的首次启动会建库并 `upgrade head`，之后每次发布
   都会重启——所以备份脚本（T17.6）必须在**第一次正式发布**之前就绪。若推进中 T17.6 尚未
   做完就要重启服务，至少手工执行一次 `VACUUM INTO`（ADR-057 推论 1）。
5. **改动面尽量小。** v7 的后端改动只有三处（预热、文档入口、SSE 心跳），前端只有两处
   （`base` 与路由基址），其余全是新增脚本与模板。每多改一处，都要问它是不是真的属于部署。

---

## 阶段总览

| 阶段 | 版本 | 目标 | 依赖 | 性质 |
| --- | --- | --- | --- | --- |
| T17.0 | v7.0 | 部署骨架：脚本目录、生产 env 模板、systemd / Nginx 模板 | — | 新增文件 |
| T17.1 | v7.1 | 启动期硬化：连接池预热、接口文档收归管理员、SSE 心跳 + 用例 | — | **改代码** |
| T17.2 | v7.2 | 目标机与四目录契约 | T17.0 | 目标机操作 |
| T17.3 | v7.3 | 后端进程起来（systemd + 配置 + 密钥 + 技能） | T17.1、T17.2 | 目标机操作 |
| T17.4 | v7.4 | 两个前端上线 + Nginx 契约 + SSE 验证 | T17.3 | 构建 + 配置 |
| T17.5 | v7.5 | 域名、HTTPS、对外收敛 | T17.4 | 外部依赖 |
| T17.6 | v7.6 | 备份 / **恢复演练** / 日志 / 磁盘告警 | T17.3 | 脚本 + 演练 |
| T17.7 | v7.7 | 发布脚本、冒烟、**回滚演练**、运维手册 | T17.4–T17.6 | 脚本 + 演练 |

```text
T17.0 骨架
  │
  ├─→ T17.1 代码硬化（独立，可并行）
  │            │
  └─→ T17.2 ──┴─→ T17.3 ──→ T17.4 ──→ T17.5
                     │           │
                     └─→ T17.6 ──┴─→ T17.7
```

---

## T17.0 · 部署骨架（v7.0）

**目标**：把「部署这件事需要哪些文件」先摆好，让后面每个阶段都只往里填内容，
不再临时 invent 一套目录。

| # | 交付物 | 落点 | 验收要点 |
| --- | --- | --- | --- |
| 1 | 生产启动项模板 | `deploy/backend.env.example` | 覆盖 `ARCHITECTURE.md` §7.2 全部条目；**每项带一行理由**；`CORS_ORIGINS=` 为空并有注释 |
| 2 | systemd unit 模板 | `deploy/systemd/prdwright.service` | 与 §9.1 逐字一致（含 `--workers 1`、`PrivateTmp=yes`） |
| 3 | Nginx 站点模板 | `deploy/nginx/prdwright.conf` | 与 §6.2 逐字一致（含 SSE 三行 + `/docs` 家族 404 + `/health` 仅本机） |
| 4 | 脚本占位 + 解释器约定 | `scripts/deploy/{backup,restore,release,smoke}.py` | 每个都能 `--help`；shebang 指向 `.venv/bin/python`；路径相对脚本自身解析 |
| 5 | `.gitignore` 复核 | 仓库根 | `backend/secrets/`、`backend/.env`、`backend/prdwright.db*`、`backend/logs/` 全在忽略列表里；`deploy/**` 与 `scripts/deploy/**` **不在** |
| 6 | 文档落盘 | `docs/architecture/v7/` 四份 | `ls` 能看到四个文件；`git status` 显示为未跟踪（**写完必须复核，见「交叉纪律」） |

**风险**

- 第 6 项是本项目已经踩过的坑：v5 时期那批部署文档「写入报成功但次日消失」，
  `git log` 零命中。所以这次**写完当场 `ls` + `git status`**，并在收口时一次提交。
- 第 5 项要小心「顺手把 `deploy/` 也忽略掉」——它是要进版本控制的。

**回滚**：删掉新增文件即可，无代码语义变化。

---

## T17.1 · 启动期硬化（v7.1）

**目标**：让「启动完成」这件事真的意味着「连接池已建满、文档不可见、流不会静默断」。
这三处都是**部署前必须做**，因为它们都只在真实运行形态下才暴露。

| # | 交付物 | 落点 | 验收要点 |
| --- | --- | --- | --- |
| 1 | 连接池预热进启动路径 | `main.py::lifespan`（第 3 步） | ✅ **已完成**：`prepare_schema` 之后、`install_log_persistence` 之前调 `warmup(get_engine())`，并记 `service.pool_warmed`（带 `size`） |
| 2 | 预热不变量用例 | `tests/test_engine.py` | ✅ **已完成**：串行 + **并发**「不新建连接」+ **`lifespan` 真的调用了它**。⚠️ 判据用 `pool.checkedin()`，不是 `size()`（后者返回 `maxsize`，恒真） |
| 3 | 公开四条不注册 | `main.py`（构造 `FastAPI` 处） | `docs_url=None, redoc_url=None, openapi_url=None`——连带去掉 `/docs/oauth2-redirect`；`app.openapi()` 仍可用 |
| 4 | 管理员专属三条 | 新文件 `api/admin_docs.py` + `main.py` 注册 | `/api/admin/{openapi.json,docs,redoc}` 三条都挂 `require_admin`；Swagger/ReDoc 的 `openapi_url` 指向 `/api/admin/openapi.json` |
| 4b | 三条移出认证白名单 | `api/deps.py::_PUBLIC_EXACT` | 删除 `/docs`、`/redoc`、`/openapi.json`；**保留注释写明为什么删**（ADR-085） |
| 4c | 管理端「接口文档」页 | `frontend_admin/src/{nav.ts,router.ts,views/ApiDocsView.vue}` | 侧栏「系统」组多一项；页内两个按钮分别新标签打开 `/api/admin/docs` 与 `/api/admin/redoc` |
| 5 | 文档权限用例 | `tests/test_admin_docs.py`（9 条）+ `__tests__/api-docs.spec.ts`（3 条） | 路由不存在 / 不在白名单 / 匿名 401 / 普通用户 404（公开）+ 403（管理员入口）/ 管理员 200；两个按钮地址正确 |
| 6 | SSE 心跳 | `api/sse.py::make_sse_generator` | 15 秒无事件即 `yield ": ping\n\n"`；走同一出口；不产生 `StreamEvent`（ADR-059 v7 修订） |
| 7 | 心跳用例 | `tests/test_sse*.py` | ① 静默期确实产出 `: ping`；② `stage` / `chunk` / `part` / `error` 形状断言**一条不改**地继续绿；③ `[DONE]` 之后不再有 ping |
| 8 | 全量回归 | `uv run pytest --junit-xml=pytest-report.xml` | 基线 **975 例** + 新增 ≥6 条，全绿；退出码以 junit 为准（沙箱偶尔拦 `%TEMP%`）。实测收口 **986 例 / 92.9 秒** |

**为什么把心跳也放在这一批**

它本来是「可选、排最后」的（v5 起草时的判断）。但它改的是**同一份文件**
（`api/sse.py`）的同一个函数，和预热 / 文档入口一样属于「部署前一次改完、跑一次全量」的
性质。拆成两批只会多跑一次 975 例。

**风险（本阶段最高的一项）**

- **预热位置的顺序**：放错到 `install_log_persistence()` 之后，日志后台线程可能与预热
  并发建连接——窗口很小，但这条不变量的全部价值就在于「窗口为零」。用例第 2 条
  （connect 事件计数）是唯一能抓住它的断言，不要用「pool.size() 对不对」代替。
- **心跳实现方式**：必须用 `asyncio.wait_for(anext(events), timeout=15)` 包住「取下一个
  事件」，**不要**另开一个后台 task 往同一个响应写。后者会引入两个写者，`[DONE]`
  与 ping 的顺序无法保证。
- **文档那三条靠 Cookie 认身份**：`/api/admin/docs` 是浏览器新标签页打开的 HTML，
  同源顶层导航会带会话 Cookie；页面里 Swagger UI 再同源 `fetch` schema，同样带 Cookie。
  如果将来前端改成 Bearer token，这两个页面会一起失效——那时要重新设计（ADR-085 细节 3）。

**回滚**：三处改动各自独立，可以单独撤。撤了之后系统回到「能跑但有一个部署期缺口」的状态——
不算故障，但 T17.3 之前必须补。

---

## T17.2 · 目标机与四目录（v7.2）

**目标**：把「这台机器能装 PRDwright」变成一套可重复的命令，并把四类目录按 ADR-056 分开。

| # | 交付物 | 落点 | 验收要点 |
| --- | --- | --- | --- |
| 1 | 系统用户 `prdwright` | `useradd -r -s /usr/sbin/nologin prdwright` | 后端不以 root 运行；`id prdwright` 无登录 shell |
| 2 | 四个目录 | `/opt/prdwright`、`/var/lib/prdwright`、`/var/www/prdwright/{releases,current}`、`/var/log/prdwright` | 属主与权限符合 §2.2 的表 |
| 3 | 代码就位 | `git clone` 到 `/opt/prdwright` | `skills/`、`backend/migrations/` **都在**（少了哪个都会让启动失败，见 §2.2 的警告表） |
| 4 | **Python 3.12（不是系统自带那个）** | `uv python install 3.12` | `uv run python -V` ≥ 3.12 |
| 5 | uv 与 Node 22 | `/usr/local/bin/uv`；Node `^22.18 \|\| >=24.12` | `uv --version`、`node -v` 均满足 `engines` |
| 6 | Nginx | 系统包 | `nginx -t` 通过 |
| 7 | 防火墙 / 安全组 | 只放行 80、443、SSH | 从外网扫不到 8000 |

**风险**

- 第 4 项是旧部署文档的**直接反例**：它写「Ubuntu 22.04 自带 3.10，通常不需要额外安装」。
  现在 `pyproject.toml` 与 `uv.lock` 都要求 `>=3.12`，自带版本会让 `uv sync` 直接失败。
  两条路：换 Ubuntu 24.04，或 `uv python install 3.12`（推荐后者，不动系统）。
- 第 5 项的 Node 版本被 `engines` 钉死（Vite 8）。系统源里的旧 Node 会在 `npm ci` 阶段
  报 `EBADENGINE`，症状看起来像依赖坏了。

---

## T17.3 · 后端进程起来（v7.3）

**目标**：后端作为一个 systemd 服务在跑，只监听回环，`/health` 三项全绿。

| # | 交付物 | 落点 | 验收要点 |
| --- | --- | --- | --- |
| 1 | 依赖环境（可复现） | `cd backend && uv sync --frozen --no-dev` | 用 `uv.lock`，**不用** `pip install -r`（仓库里没有 `requirements.txt`） |
| 2 | 启动项清单 | `/etc/prdwright/backend.env`（600） | 由 `deploy/backend.env.example` 复制，逐项确认（§7.2） |
| 3 | 主密钥 | `/etc/prdwright/master.key`（600） | `uv run python ../scripts/keygen/generate_keys.py` 生成后移过去；⚠️ 路径必须走**进程环境** |
| 4 | 数据目录外置 | `DB_PATH=/var/lib/prdwright/prdwright.db` | 启动后 `/opt/prdwright` 下**不出现** `.db` 文件 |
| 5 | 日志目录外置 | `LOG_DIR=/var/log/prdwright`、`LOG_FORMAT=json` | `prdwright.log` 出现在 `/var/log/prdwright` |
| 6 | 技能资产 | `PRDWRIGHT_SKILLS_DIR=/opt/prdwright/skills`、`PRDWRIGHT_SKILLS_STRICT=true` | 启动日志的 `skills` 一项列出三个包与版本；**故意改错路径验证启动失败** |
| 7 | systemd unit | `/etc/systemd/system/prdwright.service` | `--workers 1` 显式写出；`Restart=always`；非 root 运行；`PrivateTmp=yes` |
| 8 | 只监听回环 | `HOST=127.0.0.1` | 从另一台机器 `curl` 8000 不通 |
| 9 | 首个管理员 | `INIT_ADMIN_USER` / `INIT_ADMIN_PASSWORD` | 能登录 `/api/admin/login`；**随后从 env 里删掉这两行** |
| 10 | 健康检查 | `curl -s http://127.0.0.1:8000/health` | `status: ok`，`database` / `llm` / `skills` 全绿 |
| 11 | 池子真的建满了 | 日志或临时探针 | 启动后 `engine.pool.checkedout() == 0` 且 `size() == DB_POOL_SIZE`（T17.1 落地的可观测证据） |

**风险（最高的一项）**

第 2 / 3 项与第 6 项的**读取方式不一样**，是本项目已经踩过一次的坑：

| 变量 | 读取方式 | 写进 `.env` 有效吗 |
| --- | --- | --- |
| `PRDWRIGHT_MASTER_KEY_PATH` | `os.environ`（`core/crypto.py`） | **无效** |
| `PRDWRIGHT_SKILLS_DIR` | `lookup_env`（`services/skill_registry.py`，先进程环境再 `.env`） | 有效 |
| 其余 `Settings` 字段 | `pydantic-settings`（读 `.env` 也读进程环境） | 有效 |

三者放在一起，最容易出现的误判是「照 `.env` 的老习惯写了，本地能跑就以为部署也行」。
所以本阶段**刻意用 `EnvironmentFile` 提供全部启动项，不保留 `.env`**（ADR-056）。

**另一个坑：`/health` 的 `llm` 项报「未配置密钥」时，先查主密钥路径。**
`crypto.is_configured()` 对「解不开」返回 `False` 而不是抛异常，所以「主密钥丢了」
与「后台没填密钥」在体检里长得一模一样（§4.5 最后一条）。
判据：`journalctl -u prdwright | grep -i 主密钥`，或直接在后台保存一次密钥看是否报错。

第 6 项的反向验证不可省：`skills_strict` 的全部价值就是「漏打包立刻炸」，
只验证「配齐时能起来」等于没验证。

---

## T17.4 · 两个前端上线 + Nginx 契约（v7.4）

**目标**：域名（或 IP）打开就能用，且**流式输出是一段段出来的**。

| # | 交付物 | 落点 | 验收要点 |
| --- | --- | --- | --- |
| 1 | 管理端构建基址 | `frontend_admin/vite.config.ts` 加 `base: '/admin/'` | ✅ **已完成**：产物里 JS / CSS / favicon 全部带 `/admin/` 前缀（已看 `dist/index.html` 确认） |
| 2 | 管理端路由基址 | `src/router.ts` 改 `createWebHistory(import.meta.env.BASE_URL)` | ✅ **已完成**：`vite preview` 下深链接 `/admin/jobs` 能正常渲染（未登录被守卫送到登录页，不是白屏） |
| 2b | 管理端图标 | `frontend_admin/public/favicon.ico` | ✅ **已完成**：没有这个文件时 Vite 不会改写 `/favicon.ico`，浏览器会去站点根找 → 404。补齐后产物里是 `/admin/favicon.ico` |
| 2c | 管理端入口不缓存 | Nginx `location = /admin/index.html` | ⏳ 随 Nginx 配置落地（T17.4 第 6 项）：`/admin/index.html` 必须 `no-store`，否则发新版后「页面能打开、点什么都 404」 |
| 3 | 用户端构建 | `npm ci && npm run build` | 产物到 `releases/<ts>/user` |
| 4 | 管理端构建 | `npm ci --legacy-peer-deps && npm run build` | 产物到 `releases/<ts>/admin`；⚠️ 必须带 `--legacy-peer-deps` |
| 5 | 原子切换 | `ln -sfn releases/<ts> current` | `ls -l /var/www/prdwright` 里 `current` 指向新目录 |
| 6 | 站点配置 | `/etc/nginx/conf.d/prdwright.conf` | `nginx -t` 通过；`/`、`/admin/`、`/api/`、`/health`、`/docs` 五段路由齐全 |
| 7 | **SSE 三件事** | `proxy_buffering off` / `proxy_read_timeout 1800s` / `gzip off` | 见下方「唯一不能省的一条」 |
| 8 | 静态缓存策略 | `assets/` 长缓存、`index.html` `no-store` | 响应头符合；发新版后不手动清缓存也能拿到新版 |
| 9 | 心跳生效 | 后端已在 T17.1 落地 | 一次长生成期间抓包 `/api/jobs/{id}/stream`，静默期能看到 `: ping` |
| 10 | `/docs` 全链路 | 域名访问 `/docs`、`/docs/oauth2-redirect`、`/openapi.json` | **经域名都是 404**（Nginx）；**本机直连 8000 未登录是 401**（AuthMiddleware 在路由之前拦）。判据是「不是 200 且没有 openapi 字段」，别写成「一律 404」 |
| 11 | 接口文档入口可用 | `/admin/api-docs` → 两个按钮 | 新标签打开 `/api/admin/docs`、`/api/admin/redoc`；未带管理员会话时是 401/403，页面能正常加载接口列表 |

**唯一不能省的一条：第 7 项中的「流式」验证**

Nginx 配错了，**所有页面都正常**，只有「AI 正在写字」这一件事会挂——要么转圈很久后
一次性刷出，要么直接超时报错。所以验收必须**真的跑一次完整生成**，看文字是不是一段段出现。
只看页面能打开就宣布部署成功，是这一阶段最容易犯的错。

**风险**

- 第 2 项如果漏了，症状是「`/admin/` 能打开、点进去就空白」，而控制台报错指向路由；
  它不会在构建期报错，所以必须**打开页面**验证。
- 第 3 / 4 项在 2 GB 机器上必须**串行**执行，并设 `NODE_OPTIONS=--max-old-space-size=1024`
  （ADR-086 的代价一栏）。
- 第 5 项切换前要确认新目录里 `user/index.html` 与 `admin/index.html` **都存在**：
  符号链接切到一个不完整的目录，表现是「整站 403 / 404」，而 `nginx -t` 依然通过。

---

## T17.5 · 域名、HTTPS 与对外收敛（v7.5）

**目标**：敢把这个地址发给别人。

| # | 交付物 | 落点 | 验收要点 |
| --- | --- | --- | --- |
| 1 | DNS 解析 | 域名 → 服务器 IP | `dig` 生效 |
| 2 | 证书签发与自动续期 | certbot + systemd timer | ⚠️ 验收不是「签发成功」，而是 `certbot renew --dry-run` 通过且 timer 在跑 |
| 3 | HTTP → HTTPS | `return 301` | http 访问自动跳 https |
| 4 | `AUTH_COOKIE_SECURE=true` | `backend.env` | 浏览器里会话 Cookie 的 `Secure` 已置位；不设的话 HTTPS 下登录态不回传 |
| 5 | `AUTH_ENABLED=true` 显式写死 | `backend.env` | 防止被误关后全站裸奔 |
| 6 | HSTS 与四个安全头 | Nginx | 响应头齐全；⚠️ **不加 CSP**（理由见 §6.2 注释） |
| 7 | `CORS_ORIGINS` 为空 | `backend.env` | 响应里不带 `Access-Control-Allow-Origin`；同源请求照常工作（ADR-054 v7 修订） |
| 8 | 全量冒烟 | `ARCHITECTURE.md` §10.3 | basic 五条 + deep 五条全过，特别是第 7 条（流式）与第 8 条（刷新恢复） |

**风险**

- 第 2 项「90 天后过期」是最典型的部署债。证书当天能用不代表三个月后还能用；
  把 dry-run 与 timer 状态一起记进验收。
- 第 4 项漏掉的表现是「登录接口 200，但之后每个请求都 401」——而本地 http 环境完全正常。
- 第 7 项是本层**唯一一处推翻 v5 素材结论**的地方。改成空串之后，如果有人在本地
  用 `localhost:5173` 直连生产 API 会发现跨域被拒——那是预期行为（本地开发连的是
  本地后端），不要因此把 `CORS_ORIGINS` 加回去。

---

## T17.6 · 数据与密钥的运维闭环（v7.6）

**目标**：从「有备份」走到「能恢复」，并让磁盘不会悄悄被写满。

| # | 交付物 | 落点 | 验收要点 |
| --- | --- | --- | --- |
| 1 | 备份脚本 | `scripts/deploy/backup.py` | 用 `VACUUM INTO`（**不 `cp`**，ADR-058）；失败非 0 退出 |
| 2 | **备份自检** | 同上 | 打开产物做 `PRAGMA integrity_check`、读 `alembic_version`、算 sha256，写进 `manifest.json` |
| 3 | 主密钥随备份同批 | 同上 | 每次备份归档 `master.key`；**按指纹命名，保留每一把** |
| 4 | 保留策略 | cron / systemd timer | 数据库近 14 份；`backend.env` 最新 1 份；密钥全部版本 |
| 5 | **恢复演练** | 临时实例 | 恢复一份备份到临时路径，用**当前代码**起只听回环的实例：`/health` 三项全绿、能登录、能看到方案列表、能打开版本抽屉 |
| 6 | 演练记录 | `docs/` 或运维手册 | 写清「用的是哪份备份、`alembic_version` 是多少、恢复后有没有跑 upgrade」 |
| 7 | 日志落盘与裁剪 | `/var/log/prdwright` | 轮转 5 MB × 5；`app_logs` 由 `log_retention_days` / `log_max_rows` 自动裁剪，无需额外定时任务 |
| 8 | 磁盘告警 | cron | 覆盖 `/var/lib`（数据库）与 `/var/log`（日志）两个目录 |
| 9 | 供应商消费告警 | 供应商控制台 | 双档：超常规用量提醒 + 达到上限停调用 |

**风险**

第 5 项是这一阶段**唯一真正有价值**的一条。备份脚本写完了、timer 配好了、文件也在增长——
这一切都不能证明「出事那天能用」。只有一次完整的恢复能证明。跳过演练的备份，
在真正需要它的那天只是一个文件。

第二个风险是**用错了代码去演练**：因为恢复后的第一次启动会再跑一次 `upgrade head`
（ADR-057 v7 修订），拿「备份那天的代码」去恢复，验证的是另一条路径。
必须用**当前 HEAD 的代码**，才能验证「我们真正会经历的那条路」。

---

## T17.7 · 发布流程与演练（v7.7）

**目标**：把「上线」从一次性手艺变成固定动作。

| # | 交付物 | 落点 | 验收要点 |
| --- | --- | --- | --- |
| 1 | 备份（复用） | `scripts/deploy/backup.py` | 发布第 1 步直接调用；失败即中止发布 |
| 2 | 发布脚本 | `scripts/deploy/release.py` | 按 §10.1 六步；任一步失败即中止并打印恢复指引 |
| 3 | 排空检查 | 同上 | `GET /api/admin/jobs?status=running` 的 `total == 0`；有在飞任务时**默认中止**（可 `--force` 跳过，但要二次确认） |
| 4 | 静态产物原子发布 | 同上 | 先建 `releases/<ts>` 再 `ln -sfn`；保留最近 3 份 |
| 5 | 冒烟脚本 | `scripts/deploy/smoke.py` | `--basic` 五条、`--deep` 五条；退出码非 0 即发布失败 |
| 6 | **回滚演练** | 演练记录 | 走一遍「恢复备份 + 回代码」，确认能回到可用状态；分「未改结构」与「已改结构」两种走法（§10.2） |
| 7 | 运维手册 | `docs/` | 含：启动/停止、看日志、备份恢复、证书续期、常见故障对照表（§4.5 四条 + §10.2 四种回滚） |

**风险**

第 6 项与 T17.6 第 5 项同源：回滚演练要真的走一遍，而不是「读一遍脚本觉得没问题」。
它的特殊价值在于验证 ADR-057 那条推论——**回滚靠恢复备份，不是反向迁移**；
而且因为迁移随重启自动跑，「恢复备份」之后服务会**再升一次结构**，
这个行为不演练一次是不会安心的。

第二个风险：**发布脚本不要试图自动回滚数据。** 它能做的是「在哪一步失败就停在哪一步，
并把恢复命令打在屏幕上」。自动恢复数据的脚本会在「其实不用恢复」的场景里造成真损失。

---

## 验收总表（v7 收口时逐条打勾）

| # | 判据 | 怎么验 |
| --- | --- | --- |
| 1 | 后端用例全绿 | `uv run pytest --junit-xml=pytest-report.xml`，基线 975 + 新增 ≥6 |
| 2 | 前端两工程用例与构建全绿 | `npm run test:unit -- --run && npm run build`；`frontend_admin` 的安装要 `npm ci --legacy-peer-deps`（与 CI 一致） |
| 3 | CI 三 job 全绿 | push 后 GitHub Actions |
| 4 | 服务自发重启后仍可用 | `systemctl restart prdwright` → `/health` 三项全绿 |
| 5 | 连接池建满 | 启动后 `size()==4`、`checkedout()==0`；并发请求不新增 connect |
| 6 | 公网无 8000 | 外网 `nc -z <ip> 8000` 失败 |
| 7 | 公开文档不可达、管理员文档可达 | 匿名 `/docs` 401 / 已登录 404（经域名 404）；管理员 `/api/admin/openapi.json` 200 |
| 8 | 流式生成 | 真跑一次，字一段段出；静默期有 `: ping` |
| 9 | 刷新恢复 | 生成中刷新，进度与已生成内容不丢 |
| 10 | 备份可恢复 | 恢复演练记录（含 `alembic_version`） |
| 11 | 回滚可执行 | 回滚演练记录（两种走法各一次） |
| 12 | 证书自动续期 | `certbot renew --dry-run` + `systemctl list-timers` |

---

## 债务与遗留（本期不做，登记备查）

| 事项 | 为什么本期不做 |
| --- | --- |
| 容器化 / 镜像 | 见 §1.3。`uv sync --frozen` + `npm ci` 已提供可复现性 |
| CI 自动发布（CD） | 无制品库与部署凭据通道；本期用「CI 绿 → 服务器跑发布脚本」，人只确认一次 |
| 多副本 / 负载均衡 / Redis / PostgreSQL | 被 ADR-055 的单 worker 约束挡住，先做那三件前置 |
| 蓝绿 / 金丝雀 / 零停机 | 与单进程冲突；改用「排空 → 短窗重启 → 冒烟」 |
| 指标平台 | 日志 + 健康检查已够本期 |
| CSP | 两个前端 `index.html` 有内联首帧主题脚本，上 CSP 要么放行 `unsafe-inline`（等于没加）要么改造构建链路，不成比例 |
| WAL 归档（更细粒度备份） | 每日一次够本期；将来需要 RPO < 24h 时再加 |
| 备份加密 / 异地备份 | 未出机器；将来备份离开本机必须一并加密 |
| 管理端 dev 基址回退 | `base: '/admin/'` 会让本地 dev 地址变成 `localhost:5174/admin/`。先接受；影响体验再用 `mode` 分支 |
| 恢复脚本自动化（一键换库 + 起服务） | 恢复是危险动作，脚本故意只做「拷到指定路径」，不替人做决定（ADR-087） |

---

## 交叉纪律（每一阶段都要遵守）

- 改后端后**必须跑一次后端用例**；改前端后**必须跑一次前端用例 + `npm run build`**。
  v7 会碰 `frontend_admin`，两条都要走。
- 后端用例退出码以 `--junit-xml` 为准（沙箱偶尔拦 `%TEMP%`，会让退出码失真）。
- 迁移用例**不要写死「第几步」**：旧链（`run_migrations() == len(MIGRATIONS)`）已随 v6.1 删除，
  现在的真假源是 `alembic_version`。新增迁移时，断言应比对「当前 revision 是不是
  `migrations/versions/` 里的 head」，而不是把 `0001` 这类字面量再抄一遍。
- 结构变更**只追加新版本号**（`backend/migrations/versions/0002_*.py`），改已应用的迁移等于没改。
- ⚠️ **重启服务 = 跑迁移**：任何时候要重启，先确认备份已做成（ADR-057 推论 1）。
- ⚠️ **不要在服务器上改代码再同步回仓库**：服务器是部署目标，不是开发环境。
- ⚠️ **文档写完必须 `ls` + `git status` 复核**：v5 那批部署文档「写入报成功但次日消失」
  就是没复核的结果。
- 提交前先 `git add -An .` 预演，且**必须经用户确认**才提交。

---

## 与仓库外 v5 部署素材的对照

> 素材位置：仓库外（`C:\Users\Jen\Desktop\v5\`，四份：README / ARCHITECTURE / ADR / roadmap）。
> 它**从未提交进本仓库**（`git log --all -- 'docs/architecture/v5/*'` 零命中，见 `../v5/ADR.md` 的 ADR-070）。
> v7 是参考其结构、按 v6 之后的实际重判，**不是照抄**。

| # | v5 素材的写法 | v7 的结论 | 原因 |
| --- | --- | --- | --- |
| 1 | `lifespan` 里 `init_schema()` + `run_migrations()`（`user_version` 20 步链） | `prepare_schema()` + Alembic，三条分支，**旧库拒绝启动** | v6 换数据层（ADR-076 / 083） |
| 2 | 内存态清单第四条是 `core/db.py` 与 444 行自建池 | `core/engine.py` 的 `QueuePool`（`pool_size=4`、`max_overflow=0`） | v6 / T16.12 删旧池 |
| 3 | 「后端业务代码预期零改动」 | 后端**三处**必改（预热、文档入口、心跳） | 前两条是 v6 之后才成立的缺口；第三条因分章生成的静默期升级为必做 |
| 4 | 备份只说「用 `VACUUM INTO`，不能 `cp`」 | 追加：**自检**、`manifest.json`、恢复要用当前代码验 | 「备份配不配得上当前代码」成为新的失败模式 |
| 5 | SSE 心跳「可选，排 T2 尾部」 | **必做** | 分章 + 审核 + 改写的静默期可长达数分钟（ADR-059 v7 修订） |
| 6 | `CORS_ORIGINS` 保持默认 | **设空串** | 同源部署不需要；默认值给了 `localhost:5173/5174` 一个用不上的跨源许可 |
| 7 | 三个 bash 运维脚本 | 四个 Python 脚本（`backup/restore/release/smoke`） | 备份自检、迁移判断、失败语义（ADR-087） |
| 8 | 静态产物「先建新目录再原子改名」 | `releases/<ts>` + `current` 符号链接 | `rename` 符号链接比 `mv` 目录安全（ADR-086） |
| 9 | 部署项清单散落在文档正文 | `deploy/backend.env.example` 逐项带理由 | 照 `backend/.env.example` 配会漏掉 `LOG_DIR` / `AUTH_COOKIE_SECURE` / `PRDWRIGHT_MASTER_KEY_PATH` |
| 10 | 目录契约里没有 `backend/migrations/` | 明确列为**必须打包**项 | 少了它启动就失败，而报错与数据库无关，极易误判 |
| 11 | 没有「启动路径」一节 | 新增 §4（九步 + 三条分支 + 四种失败形态） | v6 之后「第一次起服务会发生什么」整件事都变了 |
| 12 | 继续成立、原样保留 | 单域名 + 路径前缀 · 单 worker · 四目录分离 · 回滚 = 恢复备份 · `VACUUM INTO` · Nginx 三行 SSE · 管理端两处基址改动 · `/health` 仅本机 | — |
