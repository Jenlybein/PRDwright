# ADR-052：`.env` 只剩密钥与启动项，可热改参数一律不在模板里出现

> 源：v4 层（原 `docs/architecture/v4/ADR.md`，已并入本目录）｜ [索引](./README.md)

### Status

Accepted（2026-10-22）

### Context

ADR-051 把**模型接入**从 `.env` 撤走了，但 `.env.example` 里还留着另外十六个键：
`LLM_BUDGET_*`、`LLM_TIMEOUT` / `LLM_TOTAL_TIMEOUT` / `LLM_MAX_RETRIES` / `LLM_RETRY_DELAY`、
`PRD_SECTION_*` / `PRD_PIPELINE_CONCURRENCY`、`DOC_REVIEW_MAX_TOKENS` /
`PRD_SECTION_REVIEW_MAX_TOKENS` / `REVIEW_REWRITE_MIN_SEVERITY` / `DOC_SECTION_REWRITE_ENABLED`、
`AUTH_SESSION_TTL_HOURS` / `AUTH_COOKIE_SECURE`。

它们**全部**在 `runtime_config.WHITELIST` 里，也就是后台早就能改。问题不是「能不能改」，
而是「两个入口」：

1. **模板被读成配置入口。** 它是所有人第一次接触配置的地方，注释写得越细，越像
   「正确答案在这里」。改完重启却发现没生效——因为 DB 覆盖在 env 之前，
   而那个覆盖是自己在后台点过的，早忘了。
2. **默认值会漂移。** 修「章节审核复用整篇 `review_llm`」那个死配置时，代码默认值
   从 1024 改成了 2048，模板里还写着 1024。**两处写同一件事，就一定有一处是错的**，
   而且错的那处不报错。这次就是这么发现的：把模板的值当真值核对时对不上。
3. **验收清单与部署脚本会照模板抄。** 抄到的是一份「看起来有、实际不生效」的配置。

### Decision

1. **模板只保留两类键**：
   - **密钥值**（`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `ARK_API_KEY`）——它们必须留在环境里（ADR-042）；
   - **启动项**——改了要重启、后台也改不了的那些：`AUTH_ENABLED`、`AUTH_COOKIE_NAME`、
     `INVITE_CODE`、`INIT_ADMIN_*`、`DB_PATH`、`PRDWRIGHT_SKILLS_DIR` /
     `PRDWRIGHT_SKILLS_STRICT`、`LOG_LEVEL` / `LOG_FORMAT`、`CORS_ORIGINS`、`HOST`、`PORT`。
2. **26 个白名单键一个都不出现**。代码里的 `Field(default=...)` 是它们的唯一真源，
   而且默认值就取「推荐值」（所以删掉模板行**不改变**任何运行行为——已逐项核对）。
3. **解释文案搬到 `_FIELD_META.help`**。模板删掉的那些注释里，只有两条是别处没有的
   ——「`llm_timeout` 是帧间隔、不是总时长」与「有效预算 = min(物理顶, 业务顶)」，
   已补进后台说明。其余在 help 里本来就有。
4. **用用例钉住模板**：`test_env_example_carries_only_startup_keys` 断言模板不含任何
   白名单键，**同时**断言三把密钥与启动项仍在——后一半是防「矫枉过正」：
   把 `DB_PATH` / `PORT` 也删掉，照模板建出来的 `.env` 就起不起来了。
5. **本地被手动调过的值先搬家再删行**。`PRD_SECTION_MAX_REWRITES=2`（默认 1）是先写进
   `system_config` 才删的：覆盖层本来就在 env 之上，所以**行为不变**，而且这一项从此
   出现在后台面板上——这正是它该在的地方。直接删行会静默把它变回 1。

### 本期不做

| 本期不做 | 理由 |
| --- | --- |
| 把启动项也搬进后台 | 它们「改了必须重启」，进白名单就是认知陷阱（ADR-041 已定过这条边界） |
| 给白名单项做一层「导出当前生效配置为 .env」 | 又造一个出口，且导出的那一刻就可能过期；要看生效值用 `GET /api/admin/config/effective` |
| 把默认值也挪进数据库 | 默认值的价值就是「表空时仍然有一套能跑的值」；挪进库等于把启动依赖又加一环 |

### Consequences

- **找配置只有一处**：后台「配置」页 / 「模型配置」页。模板顶部已经写明这一点。
- **模板短了很多**，但短的部分不是「没配就不好用」——那些默认值本来就是推荐值。
- **模板漂移由用例兜住**：谁再往里「顺手补个默认值」，CI 立刻红。
- **风险**：有人会以为「模板变短 = 功能变少」。所以模板开头那句必须一直留着：
  本文件只有密钥值与启动项，其余在后台。

### 补充（同日第二次）：连密钥行也退成注释，模板里一行生效配置都没有

第一版删完白名单项后，模板还留着三行**空值密钥**（`ANTHROPIC_API_KEY=` 等）。
被问到「这三行留着有什么用」——它确实不是配置：空值等于没写，写不写都能起服务。
它的唯一作用是**告诉部署者「这台机器要准备哪几把钥匙」**，而那件事用注释也能表达。

于是改为：三行一律注释，配一段说明讲清「密钥为什么不能进后台」：

- 面板管的是「用哪条配置、去哪个变量取钥匙」；**钥匙值本身是凭据**，进库就会跟着
  备份、导出包、拷给别人排查的库副本一起流动（导出功能明确不导 `.env`）。
- 钥匙要过页面就会经过请求体 / access log / 前端内存，白多几个泄露面。
- 权限边界不同：能看后台 ≠ 能登服务器。
- 附带好处：改 `.env` 里的值**不用重启**（按 mtime 重读）。

守门用例随之从「只该有三把密钥」收紧为 **`keys == set()`**：`.env.example` 里
一个生效行都不许有。这条断言比文档里的「请勿添加」有力——它会在 CI 上红。

同一次里还修了两个「模板给了开关但实际不生效」的死配置（都属于「多余」的另一种形态）：

1. `PRDWRIGHT_SKILLS_STRICT` 从来没生效过：字段叫 `skills_strict`，pydantic 按字段名
   匹配 `SKILLS_STRICT`，而模板与运维口径写的都是 `PRDWRIGHT_SKILLS_STRICT`
   （与 `PRDWRIGHT_SKILLS_DIR` 成对），于是被 `extra="ignore"` 静默吃掉——
   部署时「设了 fail-fast 却仍在悄悄降级」，正是这个开关要防的事。
   改为 `validation_alias=AliasChoices("PRDWRIGHT_SKILLS_STRICT", "SKILLS_STRICT")`，两个都认。
2. `PRDWRIGHT_SKILLS_DIR` 写在 `.env` 里无效：`skills_root()` 走 `os.environ`，
   而 pydantic 读 `.env` **不写进 `os.environ`**。只有 Docker / systemd 那种真环境变量才行，
   而 `cp .env.example .env` 恰恰是最常见的使用方式。改为走 `core/secrets.lookup_env`
   （先进程环境、再按 mtime 读 `.env`），与密钥同一条路。

---
