# v4 路线图：管理员后台 + 日志可查

> 排期真源本文件；架构依据见 `ARCHITECTURE.md`；决策依据见 `ADR.md`。
> 任务编号用于日常排期，版本号用于对外收口（对照见 `README.md`）。

## 0. 排期总原则

1. **地基先行**：F1–F6（管理员身份 / 守卫 / 运行时配置层 / Token 落表 / 审计 / CORS）
   不落地，后面任何一块都会返工。
2. **日志采集先行、UI 后置**：Handler 在 T0 就要挂上并开始攒数据，UI 放 T4。
   这是本路线图最反直觉、也最关键的一条（ADR-040）。
3. **每阶段都能单独交付**：阶段之间只靠「后端已就绪的地基」衔接，不出现半成品互相等待。
4. **schema 变更一律追加新版本号**（v12 起），不回改已应用步骤。
   编号按**实际落地顺序**分配（迁移清单要求版本号严格连续），不代表需求优先级；
   因此下表里的编号会随实际推进与初稿不同，**以代码里的 `MIGRATIONS` 为准**。

---

## 阶段总览

| 阶段 | 版本 | 目标 | 依赖 |
| --- | --- | --- | --- |
| T0 | v4.0 | 地基 + 管理员端脚手架 + **日志采集落地** | — |
| T1 | v4.1 | 用户管理 + 邀请码管理 | T0 |
| T2 | v4.2 | 平台统计 | T0（F4 Token 落表） |
| T3 | v4.3 | 系统配置 + 模型供应商切换 | T0（F3） |
| T4 | v4.4 | **日志查看 UI**（检索 / 详情 / 实时尾随） | T0（已有数据） |
| T5 | v4.5 | 增强：任务监控 / 配额限流 / 安全 / 合规 / 审计 UI | T1–T4 |

---

## T0 · 地基与脚手架（v4.0）

**目标**：让「管理员」这个身份在服务端成立，让日志开始被留下来，让管理员端能登进去。

| # | 交付物 | 落点 | 验收要点 |
| --- | --- | --- | --- |
| 1 | `users.is_admin` 列（迁移 **v12**） | `core/migrations.py` + `repositories/auth_repo.py` | 迁移用例断言 `run_migrations() == len(MIGRATIONS)`（**不写死数字**）；既有用户默认非管理员 |
| 2 | `User` / `current_user` 带出 `is_admin` | `services/auth_service.py` | 登录与 `/me` 能读到该字段 |
| 3 | `require_admin` 依赖 | `api/deps.py` | 非管理员调任意 `/api/admin/**` 得 403；未登录得 401 |
| 4 | 首个管理员的产出自举 | `ensure_bootstrap` | `INIT_ADMIN_USER/PASSWORD` 建的首个账号 `is_admin=1` |
| 5 | 审计表 `admin_audit_log`（迁移 **v15**） | `repositories/`、`services/` | 后续每次管理员写操作都落一行 |
| 6 | `generation_jobs` 加 Token 列（迁移 **v14**） | finalize 时写入 | 「按用户 / 按日 Token」可直接 SQL 聚合 |
| 7 | **日志采集**：`LogStoreHandler` + 轮转文件 + `app_logs` 表（迁移 **v13**） | `core/logging_setup.py` + `repositories/` | 业务代码零改动；重启后历史仍在；关库开关后仍写文件 |
| 8 | 日志保留策略 | 启动 + 懒裁剪 | 超 `log_retention_days` / `log_max_rows` 自动淘汰 |
| 9 | CORS 放开管理员源 | `core/config.py::cors_origins` | 管理员 dev 源与部署源都能登录 |
| 10 | `frontend_admin/` 脚手架 | 根目录新工程 | 端口 5174、`/api` 代理 8000、`npm run build` 通过 |
| 11 | 登录页 + `is_admin` 路由守卫 + 403/401 拦截 | `frontend_admin/src` | 非管理员一律停在登录页（提示「账号不存在」，ADR-049）；未登录同样跳登录 |

**进度：1–11 已全部落地。** 迁移到 v15（v12 `is_admin`、v13 `app_logs`、v14 Token 列、
v15 `admin_audit_log`）；日志采集已挂上并在攒数据；`frontend_admin` 可登录且门禁生效。

**风险**：第 3 项若只做前端守卫而漏了后端，等于把管理员门开在客户端——
`require_admin` 必须**逐条接口**套上，并配「非管理员访问」的用例钉住。

---

## T1 · 用户与邀请码管理（v4.1）

**目标**：管理员能管账号与准入。低风险，几乎全是 CRUD。

| # | 交付物 | 落点 | 验收要点 |
| --- | --- | --- | --- |
| 1 | 用户列表（注册时间 / 任务数 / Token 合计 / 状态） | `/api/admin/users` + `/users` 页 | 聚合走 SQL（依赖 T0 的 Token 列），不遍历解析 JSON |
| 2 | 新建 / 删除账号 | 复用 `create_user_direct` / `delete_user` | 删除级联会话（既有语义不变）；删号记审计 |
| 3 | 启停账号（`is_active`） | 新端点 | 停用后该用户无法登录（既有 `login` 已判 `is_active`） |
| 4 | 重置密码 | 复用 `reset_password` | 走同一套密码强度校验，不因管理端而放宽 |
| 5 | 邀请码列表 / 新建 / 删除 | `/api/admin/invites` + `/invites` 页 | 表已支持多码与 `max_uses` / `expires_at` / `used_count`；删码不影响已注册用户 |
| 6 | 邀请码审计 | 审计表 | 建删码都留痕 |

> 已知简化：`invite_codes` **表结构已支持**多码、上限、过期、备注与已用计数，
> 本期主要是补接口与 UI，不是建模。

**进度：T1 全部落地**（后端 `api/admin.py` 的用户/邀请码端点 + `services/admin_service.py`
的规则与审计，`tests/test_admin_management.py` 39 条；前端 `/users` 与 `/invites` 两页
+ `/audit` 审计页）。

三条不变式写在服务层，并用反向用例钉住：
- **不能把自己锁在门外**（删/停自己的账号一律 403）；
- **不能让最后一位管理员消失**（撤销 / 删号 / 停用三条路都挡）；
- **后台与运维脚本共用同一份实现**：「不能撤销最后一位管理员」由
  `auth_service.set_admin` 独家实现，`auth_cli.py demote` 与后台都走它。
  两条路各写一遍校验，迟早有一处漏改，后台就成了绕过不变式的后门。

> 实现时修正了初稿的一处判断：初稿把「不能撤销自己」当成独立规则。实际不成立——
> 只要还有别的管理员，撤销自己是一次正常且**可逆**的「卸任」（另一位管理员随时
> 能重新任命），把它拦掉反而挡住了真实的管理员交接。真正要拦的只有一种：
> 撤销之后系统里一个管理员都不剩。所以现在「撤销自己」不被单独拦，
> 它由「最后一位」那条不变式兜住——这也让后台与 CLI 的行为完全一致。

---

## T2 · 平台统计（v4.2）

**目标**：管理员看得见「平台跑了多少、谁在用」。

| # | 交付物 | 落点 | 验收要点 |
| --- | --- | --- | --- |
| 1 | 概览：PRD 生成总量、用户数、近 7 日趋势 | `/stats/overview` + `/dashboard` | 数字与 `generation_jobs` 实计一致 |
| 2 | 按用户用量排行 | `/stats/users` | 与 T1 用户列表的口径同源（避免两处算法不一致） |
| 3 | 按日趋势（生成数 / Token / 失败率） | `/stats/daily` | 失败率按 job 终态统计 |
| 4 | 图表呈现 | `frontend_admin` 图表组件 | 空数据态要有，不渲染成空白图 |

**风险**：统计口径一旦两处各算一遍就会漂移 → 聚合逻辑**只放后端仓储层**，
前端只做展示。

**进度：T2 全部落地。**

- `GET /api/admin/stats/daily`（`days` + `tz_offset_minutes`）与
  `GET /api/admin/stats/users`（`limit`）；概览页加了趋势图（指标切换：生成数 /
  Token / 失败率）+ 用户排行。
- 仓储里把「每个用户用了多少」的聚合表达式提成常量 `_USER_USAGE_SELECT`，
  用户列表与排行共用；失败率的区间汇总也由后端给，前端不再自己加总。
- 用例：`tests/test_admin_stats.py` 16 条、`frontend_admin/src/__tests__/dashboard.spec.ts` 6 条。

> 实现时定下了三个口径，都写进了用例（它们错了都不报错，只是数字看着合理）：
> 1. **按本地日期分组，不按 UTC**。东八区 23:30 生成的任务在 UTC 已是次日，
>    按 UTC 分组会让「昨晚用的」出现在「今天」那一格。偏移由前端传，服务端不猜。
> 2. **`CANCELLED` / `SUPERSEDED` 不算真失败**。它们在库里同样是 `status='failed'`，
>    但成因是用户取消 / 被新任务顶替。算进失败率会让平台显得比实际脆弱——
>    而管理员正是靠这个数字判断「今天要不要去查」。
> 3. **区间失败率是加权值**（区间失败 / 区间已收尾），不是「每日失败率的平均」。
>    造一个「昨天 1 个全失败、今天 9 个全成功」的例子：加权是 10%，每日平均是 50%，
>    差 5 倍。任务量少的那天不该与任务量大的那天等权。
>
> 另外**没有任务的那天会补 0 行**：折线/柱状图跳过空白日，会把「那天没人用」
> 画成一条平滑斜线，看起来像用量在缓慢变化。0 与「没有这一天」是两个结论。

---

## T3 · 系统配置与供应商切换（v4.3）

**目标**：管理员能在后台调参数、切模型供应商，不必改 `.env` 重启。

| # | 交付物 | 落点 | 验收要点 |
| --- | --- | --- | --- |
| 1 | `system_config` 表 + `core/runtime_config.py` | 迁移 **v17** + 新模块 | 解析顺序 DB > env > 默认；白名单外不查 DB |
| 2 | 白名单与校验 | `runtime_config` | 非法值 400；类型/范围与 `config.py` 的 `Field(ge=, le=)` 同约束 |
| 3 | 可调项改读 runtime_config | `llm_factory` / `job_service` / `token_budget` 等 | ⚠️ 每改一个读取点配一条「默认值不变」的回归用例 |
| 4 | 配置读写接口 + 审计 | `/api/admin/config` | 每次改配置记审计（before / after） |
| 5 | `model_providers` 表（迁移 **v16**）+ CRUD | `/api/admin/providers` | 只存 `api_key_env_var`，**不落明文** |
| 6 | 激活切换 | `/providers/{id}/activate` | 激活真源是 `system_config.active_provider_id`；切换后缓存立即失效 |
| 7 | 配置页 UI（配额 / 模型与超时 / 供应商三组） | `/config` | 危险项（如关审核）要有确认与说明文案 |

**风险（最高的一项）**：第 3 项会碰生成主链路。
缓解：白名单外的项一律不动；改动以「默认行为零变化」为验收底线。

**进度：T3 全部落地。**

- 迁移 **v16**：`system_config`（只存覆盖项）+ `model_providers`，并播种六家内置
  供应商（`INSERT OR IGNORE`，重跑不覆盖管理员改过的值）。
- `core/runtime_config.py`：白名单 17 项 + `active_provider_id`，解析顺序
  **DB > env > 默认**；5 秒 TTL + 写后 `invalidate()`；表不可用时降级到 env 且不抛。
- `core/providers.py`：**按协议**建模（`openai_compatible` / `anthropic` / `ark` /
  `google_genai` / `nvidia` / `huggingface`），加新厂商只需填 `base_url`，不改代码。
- `core/secrets.py`：按键名取密钥（先 `os.environ`、再 `.env` 快照）——补上
  「pydantic-settings 读 `.env` 但不写 `os.environ`」这个坑。
- 主链路 9 个文件改为读运行时配置；**构造期快照全部拆除**（`PrdSectionWriter`
  的 5 个属性改成只读属性、三处 LLM 缓存改按配置指纹重建）。
- `ark_llm.py` **并入 `llm_factory.py`**；分层白名单从两个文件收敛成一个。
- 接口 11 条 + 前端 `/config` 页（含「测试连接」真调）。
- 用例：`test_config_tables`(10) / `test_runtime_config`(22) / `test_config_effect`(9) /
  `test_admin_config_api`(30) + 前端 `config.spec.ts`(13)。

> 实现时定下四个口径，都写进了用例：
> 1. **一次生成 = 冻结一份配置快照**（`use_config`，与 `use_meter` 同一套 ContextVar）。
>    它同时满足「改了下次生成生效」与「跑到一半不漂移」——后者会让章节乱序、正文重复，
>    且极难归因。直连 SSE 的路由在 `make_sse_generator` 里统一冻结（那才是消费时机）。
> 2. **启动类配置完全不进白名单**（日志级别/落库开关/db 路径等）。它们改了必须重启，
>    放进来只会制造「显示已保存、实际没变」。`db_path` 尤其不能进——配置层若参与
>    「连哪个库」就会与 `connect()` 形成递归。
> 3. **`prd_section_review_max_tokens` 的死配置被修好**：它此前被 `doc_review_max_tokens`
>    顶掉（`_get_section_writer` 传了整篇审核的实例），改它没有任何效果。现在章节审核
>    用自己的实例；**默认值同时对齐到 2048**，所以默认行为零变化。
> 4. **失败率/配置校验复用同一份定义**：约束从 `Settings.model_fields` 反射而来，
>    不另写一套；删内置行、删激活项两条规则只在服务层写一次（UI 与脚本共用）。

> 顺带修掉 `backend/.env.example` 的一处死配置：`ARK_API_BASE_URL` 应为 `ARK_BASE_URL`
> （字段名 `ark_base_url`，pydantic 按字段名大写匹配）。原写法被 `extra="ignore"`
> 静默忽略，自定义地址从未生效过。

**补记（v4.6，ADR-050）：T3 的「一家一个固定空位」被换成「配置条目」**

上面第 5 项与「播种六家内置供应商」都已被推翻，不是缺陷修补而是模型换代：

- `model_providers.id` 不再等于厂商名，改由后端生成 `cfg_xxx`（短 uuid）；
  `name` 必填、**允许重名**；同一协议可以有多条配置（生产 / 测试各一条）。
- **协议创建后不可改**（显式 400）；删除只拦「使用中」那一条，不再有「内置行」
  与「回到出厂状态」；`builtin` 列保留但一律 0、代码不再读取。
- 迁移 **v19 `provider_config_entries`** 清掉六条播种行（四条件 AND，含「不是使用中」），
  并给 `generation_jobs` 补 4 列供应商快照、回填历史 `usage`。
- `PRESETS` 降级为新增表单的模板，字段 `id → key`（它不再是任何东西的 id）。
- 用户端回执显示「配置名 · 模型」；解析入口收敛为 `frontend/src/utils/runSummary.ts`
  ——顺带修掉「刷新后回执整块消失」（库里那份是 snake_case，旧解析按 camelCase 判）。
- 用例：`test_config_tables`(+5) / `test_admin_config_api`(36) / `runSummary.spec.ts`(5)，
  前端 `config.spec.ts` 改列表形态（15）。

**补记（v4.6，ADR-051）：`.env` 里不再留任何模型配置**

ADR-050 只改了「一条配置长什么样」，env 里那份旧的默认值还在（`DEFAULT_LLM_PROVIDER` /
`DEFAULT_LLM_MODEL` / 各家 `*_API_URL` / `*_MAX_TOKENS` / `ARK_THINKING_ENABLED`），
并由 `_synthesize_from_env` 在「表空 / 表坏」时合成 `env:ark` 继续跑。这一轮把它撤了：

- `Settings` 删掉那 9 个字段（连 `api_key_env_var()` / `require_api_key()` 一起）——
  **写进 `.env` 也不再有效果**，结构上没有读取点。
- 删 `_synthesize_from_env` / `ENV_SPEC_PREFIX`；无指针时取**第一条启用中**的配置，
  一条都没有就是 `None`，`get_llm()` / `/health` 直接说「还没有任何模型配置，请先在后台新增一条」。
- **第一条配置自动设为使用中**（已有指针时不抢）——否则「配好了却生成说没配置」。
- v16 迁移改成**常量播种**（不再读 `Settings`）：老库不重跑、新库那六行随即被 v19 删掉。
- `.env.example` 只剩三把密钥，并写明其余都不在这里配。
- 表格上方与空状态只留**一个**新增入口（「新增第一条配置」已删）。
- 用例：一批靠 env 默认值的用例改为显式造配置（`test_llm_resilience` 3 条、
  `test_observability` 3 条 + 新增无配置分支、`test_generation_job` 1 条、
  `test_token_budget` 1 条、`test_runtime_config` 6 条重写 + 1 条新增、
  `test_admin_config_api` 新增「首条自动使用中 / 新建不抢」2 条）；后端 **852 passed**，
  管理端 **86 passed**（`config.spec.ts` 21）。

**补记（v4.6，ADR-052）：可热改参数也不再出现在 `.env.example`**

模板里还留着 16 个白名单键（`LLM_BUDGET_*`、`LLM_TIMEOUT*`、`PRD_*`、`DOC_REVIEW_*`、
`REVIEW_REWRITE_MIN_SEVERITY`、`AUTH_SESSION_TTL_HOURS`、`AUTH_COOKIE_SECURE`），
它们后台早就能改，留在模板里只会被当成「配置入口」。

- 模板现在是**纯说明**：一行生效配置都没有。密钥与全部启动项都以注释形式给出
  （`AUTH_ENABLED` / `AUTH_COOKIE_NAME` / `INVITE_CODE` / `INIT_ADMIN_*` / `DB_PATH` /
  skills / 日志 / CORS / `HOST` / `PORT`），取消注释才生效，不写就用默认值。
  空值密钥行（`ARK_API_KEY=`）也是噪音——空值本来就等于没写，改成注释照样答得出
  「要准备哪几把钥匙」。
- 逐项核对过：删掉的 16 行里，代码默认值与模板所写**完全一致**，所以删除不改变运行行为。
  唯一不一致的是 `PRD_SECTION_REVIEW_MAX_TOKENS`（模板 1024 / 代码 2048）——那是修死配置时
  代码改了、模板没跟上，正好借此收敛。
- 本地 `.env` 里手动调过的 `PRD_SECTION_MAX_REWRITES=2` 先写进 `system_config` 再删行：
  覆盖层在 env 之前，行为不变，且这一项从此能在后台看到。
- 守门用例 `test_env_example_has_no_effective_lines`：模板出现**任何**生效行即红
  （`assert keys == set()`），比「只该有三把密钥」更严。反向验证过：加回 `LLM_TIMEOUT=60` → 变红。
- 顺手修掉两个「模板给了开关却不生效」的死配置：`PRDWRIGHT_SKILLS_STRICT`
  从来没被 pydantic 认（字段名是 `skills_strict`，现用 `AliasChoices` 两个键名都认）；
  `PRDWRIGHT_SKILLS_DIR` 走 `os.environ`，写进 `.env` 无效（改走 `secrets.lookup_env`）。
  各配一条守门用例。
- 后台 `_FIELD_META` 补两条只存在于旧注释里的说明（帧间隔≠总时长、有效预算=min(物理顶,业务顶)）。

---

## T4 · 日志查看 UI（v4.4）

**目标**：把 T0 起就在攒的日志变成可用的排障工具。

| # | 交付物 | 落点 | 验收要点 |
| --- | --- | --- | --- |
| 1 | 日志检索接口 | `GET /api/admin/logs` | 支持级别 / 时间 / logger / event / request_id / 关键词 + 分页 |
| 2 | 日志页：过滤条 + 表格 | `/logs` | 默认最近 24h；级别用颜色区分；空结果有明确文案 |
| 3 | 详情展开 | 同上 | `extra_json` 与 `exc`（traceback）可读 |
| 4 | **`request_id` 反查** | 同上 | 粘一个号能拉出该请求全部日志（排障主路径） |
| 5 | 实时尾随（SSE） | `GET /logs/stream` | 复用 `api/sse.py` 既有模式；心跳保活；可按级别过滤；断线降级为轮询 |
| 6 | 日志配置入口 | `/config` 日志分组 | 级别 / 格式 / 保留天数 / 是否落库 可改（走 T3 的 runtime_config） |

**进度**

- **T4a（补内容）已落地**：`job.*` / `llm.*` / `prd.*` / `doc.*` 四组 INFO/WARNING 事件，
  覆盖任务起止、阶段推进、章节定稿、模型重试、各类降级路径（提交 `9154c6a`）。
  没有这一步，检索页做出来会是空的——管道早就通了，但主链路几乎不打 INFO。
- **T4b 的 1–4 项已落地**：`GET /api/admin/logs`（级别 / 时间 / logger 前缀 / event /
  `request_id` / 用户 / 关键词 + 排除事件 + 分页）、`/logs` 页（过滤条 + 级别配色 +
  详情展开 + 点请求号反查 + 分页）。
- **5、6 未做**：第 6 项（日志配置入口）**依赖不成立**——`log_retention_days` /
  `log_max_rows` 是在 `install_log_persistence()` 里构造期读给 handler 的
  （`logging_setup.py:470-471`），改了要重挂 handler，与「启动类配置不显示」
  的决策一致。要做它得先让 handler 支持热重配。第 5 项（实时尾随）风险最高、
  收益最低（日志页已能手动刷新），建议放到最后或不做。

> 实现时补了两处不在初稿里的判断：
> 1. **默认排掉访问日志**（`exclude_event=request.completed`）。它每个请求一条，
>    量级压过业务事件——不排的话打开页面是一屏「请求完成」，而人找的是「刚才那次
>    生成怎么了」。但**按请求号反查时会自动取消这个排掉**：那一条「请求完成」
>    正是「这个请求到没到过后端」的证据。
> 2. **时间必须转本地时区**。后端存 UTC，直接截字符串会让东八区用户看到 8 小时前
>    的时刻，而时间正是排障时最先看的一列（用例里钉住了这条）。

**风险**：实时尾随若过滤条件在服务端未生效，会全量推流 → 客户端卡死。
必须**服务端过滤 + 限流**，而不是前端拿到再筛。

---

## T5 · 增强（v4.5）

**目标**：把「能运营」补成「运营得安心」。按价值排序，可裁剪。

| # | 交付物 | 说明 |
| --- | --- | --- |
| 1 | ~~任务监控~~ | **已完成**：在飞 / 最近失败 / 全部三页 + 单条与批量取消 + 卡死识别（ADR-046） |
| 2 | ~~配额限流~~ | **已完成**：每日次数 / Token / 并发三维度，三态上限（ADR-045） |
| 3 | ~~安全设置~~ | **已完成**：会话有效期、注册开关、登录失败**渐进延时**（见 ADR-047） |
| 4 | ~~数据合规~~ | **已完成**：账号数据 ZIP 导出 + 删除预览 + 手打用户名确认（ADR-048） |
| 5 | ~~审计 UI~~ | **已完成**：`/audit` 页与 `AuditView.vue` 早在 v4.1（`dcb88d5`）就有 |
| 6 | ~~强制下线~~ | **已完成**：用户页「强制下线」，返回吊销条数 |
| 7 | 维护模式 | 临时停新生成、保留只读（供应商故障时用）。**与 `quota_enforce` 功能重叠，价值最低** |

**进度**

- **第 1 项已完成**：`GET /api/admin/jobs`（按状态 / 用户名 / 类型筛）、
  `POST /api/admin/jobs/{id}/cancel`、`POST /api/admin/jobs/cancel-stuck` +
  `/jobs` 页（在飞 / 最近失败 / 全部、卡死标红、单条与批量取消、自动刷新、详情展开）。
  口径见 **ADR-046**：卡死看 `updated_at` 而非 `created_at`、取消复用用户侧同一条链路、
  `cancelled=false` 表示「点击前已结束」而不是失败。
- **第 2 项已完成**（提交见 git log「配额限流」）。实现口径：
  - 三个维度：每人每日**次数**、每人每日 **Token**、每人同时**运行数**。
  - 上限三态：`-1` 跟随全局（配置页「每日额度」分组）、`0` 不限、`N` 具体值。
    用户级覆盖在 `users` 的三列（v17），全局默认在 `system_config`（热生效）。
  - **次数只算「已收尾 + 在飞」**：失败与被取消的不吃额度——一次上游故障
    就把所有人当天额度清零，用户无从申诉。**Token 算全部状态**（跑了就跑掉了）。
  - **「每天」由服务端定时区**（`quota_timezone_offset_minutes`，默认东八区）。
    不接受前端传值，否则改浏览器时区就能把日界挪走。
  - `kind=chat` **不受配额管**：对话是生成的前置，限了连「为什么被拒」都问不了。
  - `quota_enforce=false` 时只记 `quota.exceeded` 日志、不拦截，用于先观察后收紧。
  - 拦截在建任务**之前**，否则被拒的请求会留下 `failed` 行，污染失败率统计。
- **第 3 项已完成**（口径见 **ADR-047**）。落地的是：
  - **登录失败限流**：窗口内失败超过 `login_max_attempts`(5) 后，每次校验前
    递增延时 2/4/8 秒（封顶）。刻意**不锁账号**——硬锁能被人故意用来把别人挡在门外。
    不存在的用户名同样计数、同样延时（否则「有延时」就是新的枚举信号）。
  - **关闭自助注册** `auth_allow_register`：关闭后注册 403，已有账号登录不受影响。
  - **会话有效期** `auth_session_ttl_hours` 进后台可改（只影响新登录）。
  - 配置读取一律走 `get_runtime_config().settings`；`get_settings()` 只反映环境变量，
    用它会出现「后台改了、界面也显示改了、接口照旧」。
  - 顺手修掉死代码：`delete_expired()` 从未被调用，过期会话只增不减。
- **第 6 项已完成**：`POST /api/admin/users/{id}/sessions/revoke` +
  用户页「强制下线」（二次确认）。返回吊销条数——单点登录下每人至多一条，
  `revoked=0` 表示「他当时并未登录」。吊销不影响在飞任务。
- **第 3 项的取舍**：不做 IP 维度限流（用户明确取舍），因此「撞库」——
  每账号只试 1 次、打一万个账号——**未被覆盖**，见 ADR-047 的残留风险。

- **第 4 项已完成**（口径见 **ADR-048**）。落地的是：
  - `GET /api/admin/users/{id}/export`：账号数据打成 ZIP（清单 / 账号 / 方案摘要 /
    三份正文 md / 对话转录 md / 任务 CSV），内存拼包上限 50 MB，审计 `user.export`。
  - `GET /api/admin/users/{id}/deletion-preview`：删前先算「删什么、留什么」；
    用户页的确认面板据此列出条数，并要求**手打用户名**才能点确认。
  - 删除链路补两处：`job_service.cancel_for_user`（删前停在飞任务）、
    `auth_service.clear_login_failures`（清掉按用户名记的失败计数，
    否则重建同名账号会继承旧账）。
  - **级联范围**：`users` → 会话 / 方案（→ 文档 → 版本 / 对话）/ 任务全部级联；
    `admin_audit_log` 与 `app_logs` **有意保留**。
  - ⚠️ 删账号会让平台历史统计变小（任务记录级联删除），确认面板已明示。
  - 本项**只做管理员侧**（用户明确取舍）：没有「用户自助导出」入口。

---

## 债务与遗留（本期不做，登记备查）

| 事项 | 为什么本期不做 |
| --- | --- |
| 管理员角色分级 RBAC | YAGNI；审计表已留 `actor/action`，将来只加列不改结构 |
| 明文密钥托管 | 需先解决加密存储与轮换 |
| 日志全文检索 | SQLite + LIKE 在本量级够用 |
| 管理员在页面上直接浏览用户正文 | 仍不做。但 T5-4 的导出包里**有正文**（合规要求「把数据带走」），这条从「拿不到」变成「**拿得到但留痕**」：每次导出写 `user.export` 审计。刻意不做的是「在后台逐篇翻阅」——那既没有合规理由，又让隐私边界变得模糊 |
| 两版 diff | v3 遗留，与 v4 无关 |
| `SOLUTION_NOT_FOUND` 兼作「用户不存在」 | 后台用户类 404 全走 `_row_or_404`，错误码语义不对；但它是既有对外契约，改它要动枚举与两侧映射，风险大于收益 |

## 交叉纪律（每一阶段都要遵守）

- 改后端后**必须跑一次后端用例**；改前端后**必须跑一次前端用例 + `npm run build`**。
- 迁移用例的断言**不写死版本号**（`run_migrations() == len(MIGRATIONS)`）。
- 结构变更**只追加新版本号**，改已应用的步骤等于没改（旧库按 `user_version` 跳过）。
- 提交前先 `git add -An .` 预演，且**必须经用户确认**才提交。

### 密钥加密入库（ADR-053，已实施）

- `model_providers.api_key_env_var` → `value_token`（迁移 v20，删旧列）。
- 新模块 `core/crypto.py`：主密钥 `backend/secrets/master.key`（600、gitignore），
  Fernet 加解密；`scripts/keygen/generate_keys.py` 负责生成与 `--rotate`。
- 后台「模型配置」直接录入密钥（密码框），列表只回 `key_present`；
  查看明文走独立 `reveal-key` 接口并写审计。
- `.env.example` 第一节改为「密钥不在这里」；导出不涉及该表，无需额外 redact。
- 旧的 `PROTOCOL_KEY_ENV_VARS` / `key_env_var` / `key_env_hint` 一并删除。
