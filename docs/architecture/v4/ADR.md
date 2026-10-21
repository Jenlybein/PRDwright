# v4 决策记录（ADR）

> 编号接续 `../v2/ADR.md`（ADR-038 为止），本文件从 **ADR-039** 起。
> 每条记录的是**为什么**这样定，不只是定了什么。

---

## ADR-039：管理员后台采用「根目录独立应用」，不嵌入也不组 monorepo

### Status

Accepted（2026-10-21）

### Context

需要新建平台管理员后台。三个候选：

1. **嵌入现有 `frontend/`**：加 `/admin/*` 路由与页面，作为一个 feature module；
2. **根目录单开 `frontend_admin/`**：独立 `package.json`、独立 Vite 应用；
3. **pnpm monorepo**：`frontend/` 下拆 `userpage` / `adminpage` 两个应用 + `packages/shared`。

讨论中先后提出过三类顾虑：

- **安全**：「一起放着是不是容易被黑客找到后台」；
- **开发冲撞**：「一起放着会不会互相冲撞」；
- **工程成本**：monorepo 的双份工具链与 shared 包维护。

其中「隐藏路径能挡住攻击者」这一点经核实不成立：SPA 的路由本就打进 JS 包，
攻击者直接扫 `/api/*`，前端怎么组织都不改变 API 的暴露面；且两个前端只要请求同一个 API 源，
会话 Cookie 本来就通用。真正的安全边界是**服务端 `require_admin`**，
与代码是否分开放置无关。

### Decision

取 **方案 2（根目录独立应用 `frontend_admin/`）**。

- 与 `backend/`、`frontend/` 并列，独立构建、独立部署。
- 两个前端共享同一后端 API 源，因此共享同一套会话 Cookie，**不做任何登录态桥接**。
- 管理员端**不 import** 用户端任何模块；自带一份薄 HTTP 封装与类型。
- 服务端对所有 `/api/admin/**` 强制 `require_admin`（这是唯一硬防线）。

### Consequences

**变容易**

- 独立构建与部署：管理员端可单独发布、可放独立子域或内网（这是「分离」唯一能带来
  真实安全增益的地方，且它靠的是部署边界，不是代码位置）。
- 硬隔离：TypeScript/Vite 天然阻止跨应用 import，不会因手滑把管理员组件带进用户端。

**变难 / 代价**

- 多一套工具链（Vite / 依赖 / CI），以及少量 HTTP 封装的重复。
- **git 冲突并不会因此减少**——两个应用仍在同一仓库、同一段提交历史；
  真要消除版本控制层面的冲撞只能拆独立仓库，那代价远大于收益。
- 部署时要处理两个 SPA 共存（反代分路径或独立子域）与 `cors_origins` 放行。

---

## ADR-040：日志采集「文件 + SQLite 双写」，且采集先行于 UI

### Status

Accepted（2026-10-21）

### Context

`core/logging_setup.py` 目前只装了一个 `StreamHandler(sys.stdout)`，
**没有任何持久化**：进程停止后历史全无，管理端也就无从查看。
日志格式本身已经很规整（ts / level / logger / msg / request_id / `extra=` 字段，
`LOG_FORMAT=json` 时一行一条 JSON），采集端几乎不需要改格式化逻辑。

候选方案：

1. **纯轮转文件**：简单，但后台检索要现解析文本、处理轮转与并发读；
2. **纯 SQLite 表**：查询灵活，但每次 `logger.info` 都写库，且丢了文件这个兜底；
3. **文件 + DB 双写**；
4. **交给外部采集**（Docker logs / ELK）：运维更专业，但与「自建小工具开箱即用」相悖，
   且用户明确要求后台内自带日志查看，必须自持。

### Decision

取 **方案 3（双写）**，并附加一条排期约束：

- 新增 `LogStoreHandler`，在 `configure_logging` 里与 stdout handler 并列装上，
  **业务代码一行都不用改**（与 `request_id` 自动注入同一条设计纪律）。
- **轮转文件始终开**（`logs/prdwright.log`，5MB × 5）：保证永不丢、可脱机排查、可外喂采集。
- **`app_logs` 表可开关**（默认开，异步批量写）：保证后台能过滤 / 分页 / 排序。
- **保留策略**：`log_retention_days`（默认 7 天）与 `log_max_rows`（默认 5 万条），
  启动时执行一次 + 写入时懒触发。
- ⚠️ **采集排在 T0，UI 排在 T4**：日志是「从现在开始攒」的数据，
  采集晚一步，后台上线那天就没有历史可看。
- 三条硬约束：绝不阻塞主流程（异步批量 + 队列上限 + 失败降级丢弃）；
  必须 `require_admin`；`log_db_min_level` 默认 INFO（DEBUG 不默认入 DB）。

### Consequences

**变容易**

- 后台能做级别 / 时间 / logger / event / `request_id` / 关键词的组合检索与分页。
- `request_id` 反查成为可能——用户回执上那个号粘进后台即可拉出该请求全部日志，
  把 R1 就贯通的追踪链补上最后一环。
- 文件与 DB 互为兜底：DB 出问题仍有文件，反之亦然。

**变难 / 代价**

- 多一份存储与保留策略要维护；日志写放大需要靠批量与级别下限控制。
- 日志可能含用户名、方案 id，属高权限数据，访问面必须收紧到管理员。

---

## ADR-041：系统配置「DB 覆盖 env，但只有白名单项可运行时改」

### Status

Accepted（2026-10-21）

### Context

当前所有可调项都由 `.env` 经 `get_settings()` 读成**进程内 `@lru_cache` 单例**，
进程启动后不变。要做到「管理员在后台改参数并生效」，必须有运行时可变的配置源。

三个选项：

1. 只改 `.env` + 重启：运维负担重，且不符合「后台可调」的诉求；
2. **全部搬进 DB**：看似统一，但 `db_path` / `host` / `port` / `auth_enabled` /
   `cors_origins` 这类**启动项**一旦进 DB，就会出现「改了却不生效」或「改了要不要重启」
   的认知混乱，甚至改坏之后服务起不来；
3. **DB 覆盖 env，但只有白名单项可进 DB**。

### Decision

取 **方案 3**。

- `system_config` 表 + `core/runtime_config.py`；解析顺序 **DB > env > 字段默认值**。
- **白名单**：配额、模型与调用参数、安全与会话、**日志参数**、维护模式。
- **永不进 DB**（启动项）：`db_path`、`host`、`port`、`auth_enabled`、`cors_origins`、
  供应商密钥、邀请码种子、`init_admin_*`、`skills_strict`。
- 进程内 **30 秒 TTL** 缓存；管理员写入时**立即失效**并记审计（多进程靠 TTL 兜底，
  不引入消息广播）。
- 写入在 `runtime_config` 集中校验，约束与 `config.py` 的 `Field(ge=, le=)` 同源；
  非法值 400，不静默截断。

### Consequences

**变容易**

- 绝大多数目标参数（`llm_timeout` / `llm_max_retries` / `prd_pipeline_concurrency` /
  `default_llm_provider` …）在 `config.py` 里**已经存在**，把它们从「只读 env」改成
  「优先读 runtime_config」即可，UI 是纯增量——这是整个 v4 性价比最高的一块。
- 配置变更有审计（before / after），可追溯。

**变难 / 代价**

- 让 `llm_factory` / `job_service` 等改读 runtime_config 会**碰到生成主链路**。
  缓解：只改白名单项的读取点，且每改一处配一条「默认行为零变化」的回归用例。
- 白名单本身需要维护：新增可调项时要显式登记，忘了就还是只能改 env。

---

## ADR-042：模型供应商只存「环境变量名引用」，明文密钥不落库

### Status

Accepted（2026-10-21）

### Context

v4 要建 `model_providers` 表并支持后台切换供应商。随之而来的问题是密钥放哪：

1. **明文落库**，由管理员在后台录入；
2. **库里只存环境变量名引用**（如 `ARK_API_KEY`），密钥仍由 `require_api_key()` 从环境取。

现有 `Settings.require_api_key()` 与 `API_KEY_ENV_VARS` 已经把「哪个供应商看哪个变量」
收敛在一处，密钥生命周期完全在环境里。

### Decision

取 **方案 2**。

- `model_providers` 表存 `api_key_env_var`（**变量名字符串**），不存密钥本身。
- 管理员可改：展示名、`provider_type`、`base_url`、`default_model`、`priority`、激活与否。
- 管理员**不可**在后台录入明文密钥；新增供应商仍需配置对应环境变量。
- 激活真源是 `system_config.active_provider_id`，**不靠表里加 `is_active` 列**——
  后者会出现「两行都是 1」的不一致态，前者天然只有一个值。
- `llm_factory` 改读「激活供应商行」（经 runtime_config 缓存），切换后立即失效缓存。

### Consequences

**变容易**

- 把明文凭据的泄漏面挡在数据库之外：库被读走也不会连带泄露 API 密钥。
- 与既有 `API_KEY_ENV_VARS` / `require_api_key()` 单一真源保持一致，不新增第二套密钥来源。

**变难 / 代价**

- 新增或轮换供应商仍需碰 `.env` 并重启（密钥层面），后台只能切「用哪一个」。
- 真要做密钥托管，需先解决加密存储与轮换流程——本期明确不做（见 `ARCHITECTURE.md` 1.3）。
