# 架构决策记录（ADR）

> 配套：`roadmap.md`（R0–R3 剩余工作路线图）
> 每条记录只回答一件事：**我们为什么这么选，以及放弃了什么。**
> 2026-10-13 更新：新增 ADR-007 / 008 / 009（认证、接口可见性、持久化范式），
> 并按已落地的事实修订了 ADR-005（`user_id` 插槽）与 ADR-006（vue-router）；
> 随后新增 ADR-010（提示词归后端独占）。
> 2026-10-13 重订路线图：原 P0–P4 大半已落地，**新增 ADR-011（生成前双闸门）**；
> ADR-001 / ADR-009 状态更新为「已落地」；ADR-004 标注「分章节已落地、断点续传待 R1」。
> 2026-10-14：**新增 ADR-012（错误契约机器码化，已落地 `7be5989`）**与
> **ADR-013（流式编排收敛到 `stream_reply.py` 一处，R0-T3 实施中）**。

---

## ADR-001：会话状态的唯一真相放在后端数据库

**状态**：Accepted（已落地，P1 实施）

### Context

当前同一份会话状态存在两个地方：后端 `ConversationService._conversations` 的内存 dict（进程重启即丢），前端浏览器里的会话快照（刷新后靠它恢复）。两边靠「前端回传完整 history」缝合，靠匹配中文错误串 `会话不存在` 触发降级重试。

后果：改一次后端代码（`--reload`）会话就丢，调试 PRD 生成要重填问卷；无法跨设备；无法回溯历史会话；后端改一个字就能让前端降级逻辑静默失效。

### Options

| 方案 | 优点 | 代价 |
| --- | --- | --- |
| **A. 后端 DB 为唯一真相** | 语义清晰、可回溯、可跨设备、为登录预留 owner 维度 | 需要引入 Repository 与迁移；前端恢复逻辑要重写 |
| B. 前端为唯一真相（后端纯无状态） | 后端极简，天然支持多实例 | 历史随浏览器清理丢失；无法多端；长会话每次全量回传，token 成本高 |
| C. 维持现状双写 | 不用改 | 问题一个没解决，且会随功能增加而恶化 |

### Decision

**选 A。后端数据库是会话与文档的唯一权威来源。**

配套规则：
- 前端传入的 `history` **仅在会话不存在时**作为恢复来源，不作为覆盖依据。
- `continue-stream` 的响应 `data` 里回传 `history_source`（`server` / `client`），让前端明确知道这一轮用的是哪份历史。
- 浏览器本地快照降级为**离线草稿**：后端不可达时才展示，且明确标注。

### Consequences

- 变容易：回溯历史会话、跨设备、重启保活、按 owner 隔离。
- 变困难：`continue-stream` 需要多一次 DB 读取；需要处理「前端快照与后端不一致」的冲突（策略：以后端为准，提示用户）。
- 中立：无状态模式保留为兼容能力，不再是主路径。

---

## ADR-002：持久化用 SQLite 起步，通过 Repository 隔离

**状态**：Accepted（2026-10-13 已落地，首用于认证；P1 扩展至方案持久化）

### Context

需要持久化，但当前是单人本地开发，未来也只是「少量人使用」。选型的真实约束是**运维成本**，不是吞吐上限。

### Options

| 方案 | 优点 | 代价 |
| --- | --- | --- |
| **A. SQLite 单文件 + Repository 抽象** | 零运维、备份就是拷文件、Python 标准库自带 | 并发写能力弱；将来切 Postgres 需迁移 |
| B. 直接上 Postgres | 一步到位，无迁移成本 | 本地开发要起容器/服务，拖慢 P0-P2 的迭代；当前负载完全用不上 |
| C. 文件（JSON per session） | 最简单 | 无查询能力，「会话列表」需求一出现就崩 |

### Decision

**选 A。** 服务层只依赖 Repository 协议（`UserRepository` / `AuthSessionRepository` / `InviteCodeRepository`
已落地；P1 再补 `SolutionRepository`），具体实现默认 SQLite。开启 `journal_mode=WAL`。

落地情况（2026-10-13）：`core/db.py` 负责连接与建表，`repositories/` 是**全项目唯一出现 SQL 的地方**，
服务层与路由层一律不得 import `sqlite3`。这条约束靠 review 与目录约定维持。

### Consequences

- 变容易：本地开发零依赖；备份与调试直观。
- 变困难：真正的多用户并发写场景需要评估（少量人场景应无压力）。
- 中立：切换到 Postgres 的成本被 Repository 限制在一个文件内；**前提是业务代码不得出现任何 SQL 或 sqlite3 import**。

---

## ADR-003：前端状态承载选 Pinia，但真正的解耦靠状态机外置

**状态**：Accepted（已实施：`stores/workflow.ts` 状态机 + `stores/auth.ts` 登录态）

### Context

`App.vue` 1305 行里，状态（三份文档、消息、会话 ID、流式缓冲、错误提示）与逻辑（状态机、持久化、生成流程）混在一起。拆分的收益主要来自**逻辑外置**，而不是「换成某个状态管理库」——如果只是把 `ref` 搬进 store，巨石问题依然存在。

`pinia` 已在 `package.json` 里但从未使用。

### Options

| 方案 | 优点 | 代价 |
| --- | --- | --- |
| **A. Pinia store** | 依赖已装、有 devtools、跨视图共享天然、易测、可做持久化插件 | 多一层概念；团队需熟悉 |
| B. `useSession()` composable + provide/inject | 更轻、无额外依赖、Vue 原生 | 跨组件调试体验差；多个组件实例时作用域易踩坑 |
| C. 维持组件内 ref | 不用改 | 巨石问题不解决 |

### Decision

**选 A，但顺序很重要**：先做 `workflow.ts` 把状态机抽成纯函数并配单测（这一步贡献 80% 的收益），再让 Pinia store 只做**状态容器 + 调用 workflow 的纯函数**。store 里不写流程判断。

若实践中觉得 Pinia 过重，退回方案 B 的成本很低——因为逻辑已经在 `workflow.ts` 里，与状态容器解耦。

### Consequences

- 变容易：跨视图共享、devtools 调试、状态机单测。
- 变困难：多一层文件；需要约定「store 不放业务逻辑」并靠 review 维持。
- 中立：`vue-router` 的取舍与本文无关，单独见 ADR-006（2026-10-13 已修订为「引入，但只做页面级导航」）。

---

## ADR-004：长文档按章节生成，但保留整篇模式的开关

**状态**：Partially Implemented（分章节生成已落地 `prd_section_writer.py`；断点续传待 R1-T2）

### Context

PRD 生成目前是一次 `astream` 出整篇。默认 `ark_max_tokens=4096`（`core/config.py` 注释已自认会截断长文），一旦网络中断或被截断，整篇作废，只能从头再来。

需要说清楚的一件事：**分章节的主要收益是可靠性，不是省钱**——每一节都要重复携带背景上下文，token 总量通常不降反升。

### Options

| 方案 | 优点 | 代价 |
| --- | --- | --- |
| **A. 分章节生成 + 整篇开关** | 单节失败只重试单节；可断点续传；不受单次 max_tokens 限制 | 章节间口径可能不一致；总 token 可能上升；需要组装逻辑 |
| B. 只做整篇，但把 max_tokens 调大 | 改动最小、文气连贯 | 中断仍全废；超长上下文质量衰减；单次失败成本极高 |
| C. 生成后自动检测截断并续写 | 改动介于两者之间 | 「续写拼接」的语义边界难保证，容易接出半句话 |

### Decision

**选 A。** 按 `field-schema.json` 的章节拆成 N 次调用，中间产物落 `documents.sections`，支持断点续传。**同时保留整篇模式**作为配置开关（默认先用整篇，P3 期间 A/B 对比后再决定默认切换）。

配套：`max_tokens` 按文档类型分档配置，PRD 用大值、澄清对话用小值。

### Consequences

- 变容易：可重试、可续传、不再怕长文截断。
- 变困难：需要章节组装与一致性校验；成本可能上升，必须配合 ADR 里的成本计量（P3-T5）观察实际数字。
- 风险：章节间口径不一致。**缓解**：生成后续章节时把已生成章节的标题与关键结论作为上下文传入；上线前用同一份问卷跑两种模式人工比对。

---

## ADR-005：现在就把 `user_id` 埋进 schema，但不做鉴权

**状态**：Superseded by ADR-007 / ADR-009（原决策已于 2026-10-13 修订，见文末）

### Context

你已明确：终局会有简单登录系统供少量人使用，但**功能开发完成后才做**。若现在不预留，P4 加登录时要改表、改所有查询、迁移数据——那就从「填坑」变成「重构」了。

### Decision

- P1 建表时 `conversations.user_id` 即存在，**允许 NULL**，当前全部写 NULL，不影响任何现有流程。
- Repository 的查询方法**现在就接受可选的 `owner_id` 参数**（传 None 表示不过滤），但不在路由层暴露。
- **不做**任何登录、会话、中间件、密码存储相关代码。

### Consequences

- 变容易：P4 只需「填上 user_id + 在路由层注入当前用户」，零 schema 变更、零数据迁移。
- 变困难：schema 里有一个当前恒为 NULL 的列，需要一个注释说明它为何存在（否则后来者会以为是遗留字段而删掉）。
- 中立：这是典型的「为可逆性多花一点点」，成本接近于零。

### 修订（2026-10-13）：插槽被提前兑现，`user_id` 改为 NOT NULL

原先「P4 才做登录、期间 `user_id` 恒为 NULL」的安排已被推翻：**登录提前到持久化之前实施**
（顺序变更见 `roadmap.md` 第四节）。于是：

- 建表时不再需要「先留 NULL、日后回填」这一步。持久化落地即 `user_id NOT NULL`，
  外键指向 `users(id)`；**没有任何历史 NULL 数据需要迁移**（当前尚无持久数据）。
- 「预留插槽」的收益没有兑现，但也**没有变成损失**——它让登录与持久化的顺序可以自由调换
  而不产生返工，这正是当初埋插槽的价值所在。

原决策的其余部分（Repository 接受 `owner_id` 参数）继续有效，并在 ADR-009 中升级为
**强制过滤**而非「可选过滤」。

---

## ADR-006：不引入 vue-router，阶段流转用状态机而非 URL

**状态**：Amended（2026-10-13 修订：保留并启用 router，但仅用于页面级导航）

### Context

`vue-router` 已在 `package.json` 里但从未使用。当前是 Wizard 形态（问卷 → 对话 → PRD → 接口文档 → 提示词），9 个 `ViewState` 由 `App.vue` 内的状态机驱动。

### Options

| 方案 | 优点 | 代价 |
| --- | --- | --- |
| **A. 维持状态机，不用 router** | 契合 Wizard 线性推进；非法状态（无 PRD 却跳到接口文档）在状态机里天然被拦 | 无法分享/收藏到具体步骤的链接 |
| B. 引入 router，阶段 URL 化 | 可 deep link、可前进后退 | 必须处理「直接访问中间步骤但数据不存在」的非法态；刷新后需要先恢复数据再路由，复杂度转移到别处 |

### Decision

**选 A，维持现状。** 刷新恢复问题由 P1 的后端恢复接决（按 `conversation_id` 拉回完整状态），不依赖 URL。

### Consequences

- 变容易：非法状态在状态机一处收口（`workflow.ts` 可单测）；不用处理路由守卫与数据竞态。
- 变困难：将来若要「分享某个会话链接」或「多标签页独立流程」，需要重新评估——届时引入 router 的成本主要在把状态机映射到路由守卫，可控。
### 修订（2026-10-13）：引入 router，但只做页面级导航，阶段流转仍用状态机

原决策里的**行动项作废**（`vue-router` 保留并启用），但决策主体**依然成立**：

- 需要「打开某个已有方案」这一能力后，Wizard 之外出现了**第二个页面级入口**。
  若继续用 `viewState` 之外的布尔开关切换页面，`AppV2.vue` 会重新长成巨石。
- 因此引入 `vue-router`，但路由表只有**两条**：`/v2`（新建，尚无 id）、`/v2/:id`（打开某方案）。
  方案列表**不做独立页面**，而是常驻左侧边栏（顶部「新建任务」+ 下方列表）——
  列表是「工作台的一部分」而不是「另一个页面」，用户不该为了看一眼列表而离开正在做的事。
- **不变的是**：9 个 `ViewState` 的阶段流转**仍完全由 `workflow.ts` 状态机驱动，不映射到 URL**。
  用户不会看到 `/v2/xxx/chat` 这样的地址；刷新后一律按 `conversation_id` 从后端恢复数据，
  再由状态机决定展示哪个阶段，而不是试图从 URL 恢复到某个中间阶段。

一句话：**router 管「在哪一页」，状态机管「在这一页的哪一步」**，两者职责不重叠。

ADR-006 原有风险（直接访问中间步骤但数据不存在）因此**没有被引入**——URL 里从来不出现阶段。

---

## ADR-007：认证用服务端不透明会话 token + HttpOnly Cookie，不上 JWT

**状态**：Accepted（2026-10-13 已实施）

### Context

需要自建账号密码登录（少量人使用）。争议点只有一个：**会话凭据用无状态的 JWT，还是有状态的服务端 token**。

曾经考虑过 JWT，动机是「将来可能扩大用户范围」。但扩大用户范围真正带来的是**多实例部署**压力，
而当前连单机并发都不存在；为了一个尚未出现的约束提前引入密钥管理与吊销复杂度，是典型的过度设计。

### Options

| 方案 | 优点 | 代价 |
| --- | --- | --- |
| **A. 服务端不透明 token + 会话表 + HttpOnly Cookie** | 登出**即时失效**（删一行即可）；无签名密钥要管理；密码学攻击面小 | 每次请求多一次会话表读；多实例部署时会话表要共享 |
| B. JWT（无状态） | 无需查库，天然适配多实例 | 登出无法即时失效（要黑名单，等于又变回有状态）；密钥轮换复杂；当前无多实例需求 |
| C. 托管认证（邮箱 / 手机验证码） | 不用存密码 | 引入外部依赖与费用；本地开发必须联网；离线不可用 |

### Decision

**选 A，但把签发 / 校验 / 吊销封成 `TokenIssuer` 协议**（`issue` / `verify` / `revoke`），
当前实现是 `OpaqueSessionIssuer`。将来若真要换 JWT，只换一个实现类，业务代码零改动。

配套：
- 随机 token 用 `secrets.token_urlsafe(32)`，库里**只存 SHA-256**，不存明文——拖库也拿不到可用凭据。
- Cookie 设 `HttpOnly` + `SameSite=Lax` + `Path=/`；`Secure` 由 `AUTH_COOKIE_SECURE` 控制（本地 HTTP 开发默认关）。
- 密码用标准库 `hashlib.scrypt` + 每用户独立 salt，比对用 `hmac.compare_digest`；**零新增依赖**。
- 用户不存在时照常执行一次 dummy scrypt 再返回，抹平时序差异；登录失败统一文案「用户名或密码错误」，防账号枚举。
- 开放注册但需邀请码（`invite_codes` 表，支持 `max_uses` / `expires_at`）；核销走**单条原子 UPDATE**
  靠 `rowcount` 判定，防并发超发。注册顺序为「查重 → 核销邀请码 → 建号」，避免用户因重名白亏一次额度。

### Consequences

- 变容易：登出即时生效（已有测试覆盖：登出后偷回 cookie 仍 401）；无密钥轮换负担；`AUTH_ENABLED=false`
  时回落内置 dev 用户，保证 `user_id` 恒非空，将来开启鉴权**零迁移**。
- 变困难：每次请求一次会话表读（SQLite 本地读，成本可忽略）；横向扩展时会话表需共享——
  那正是换掉 `TokenIssuer` 实现的时机。
- 中立：不做角色权限体系。少量人场景，owner 过滤足够（见 ADR-009）。

---

## ADR-008：接口可见性用「默认拒绝 + 白名单」中间件，而不是逐接口挂依赖

**状态**：Accepted（2026-10-13 已实施）

### Context

需要一个「哪些接口要登录」的规则。**逐个接口挂 `Depends(get_current_user)` 的写法，漏一个就是裸奔，
而且没有任何机制能发现漏了**——接口越多，review 越容易疲劳，风险单调上升。

### Options

| 方案 | 优点 | 代价 |
| --- | --- | --- |
| **A. 中间件默认拦截 + 白名单** | 新增接口**自动受保护**；漏保护在结构上不可能发生 | 白名单是唯一改动点，放开接口必须显式改它 |
| B. 逐接口 `Depends` | 意图显式，读代码即知 | 漏挂即裸奔且无法自动检测；接口变多后 review 必然疲劳 |
| C. 路由前缀分组（`/api/public` vs `/api/private`） | 结构清晰 | 现有路径已定型，改名成本高；且仍依赖开发者放对位置 |

### Decision

**选 A。** 中间件必须用**纯 ASGI** 实现，不能用 `BaseHTTPMiddleware`——后者会把下游响应体重新包装成
`StreamingResponse`，而本项目有 6 条 SSE 流式接口（`/api/conversation/*-stream`），这是额外风险。
纯 ASGI 只在返回 401 时自己 `send` 两条消息，**放行时完全不碰响应体**。

配套约定（都是踩过坑才定下来的）：

- 白名单 = 精确路径集合（`/`、`/health`、`/api/conversation/questions`、`/docs`、`/redoc`、`/openapi.json`）
  + 前缀集合（`/api/auth`）。
- `OPTIONS` 预检**直接放行**：预检按规范不带凭据，拦它的表现是「接口凭空 401」，极难定位。
- 中间件注册顺序：`add_middleware(AuthMiddleware)` **在前**、`add_middleware(CORSMiddleware)` **在后**。
  Starlette 是「后加的在外层」，这样 CORS 才在最外层先应答预检。
- 中间件把用户写进 `scope["state"]["user"]`，路由层用 `request.state.user` 读。
- 前端 axios 加 `withCredentials: true`；**SSE 的 `fetch` 必须加 `credentials: 'same-origin'`**
  （`fetch` 默认不发 Cookie，漏了的表现是「普通接口没事、流式一直 401」）。
- 全局 401 拦截器**排除 `/api/auth/*` 前缀自身**：登录失败与 `me` 探测登录态都会正常返回 401，
  一并触发弹窗会导致「登录失败后又弹一次登录窗」。

**接口可见性分级**：
- 公开：`/health`、问卷配置、`/api/auth/*`。
- 需登录：**其余全部，包括 RAG 检索**（它返回的是团队内部接口规范资产，属内部资料，不应对匿名开放）。

**测试护栏**：遍历 `app.openapi()["paths"]`，断言任何非白名单路径未登录必须返回 401。
新增接口若忘了考虑鉴权，会被这条不变量测试直接拦下。

### Consequences

- 变容易：新增接口默认安全；可用性由测试保证，**不依赖 review 自觉**。
- 变困难：放开一个接口必须显式改白名单（这是刻意的摩擦）；排查 401 时第一直觉要想到白名单。
- 踩坑已固化：FastAPI 0.142 起 `app.routes` 里的 `include_router` 是惰性的 `_IncludedRouter`，
  **列路由必须用 `app.openapi()["paths"]`**，直接遍历 `app.routes` 看不到业务路由。

---

## ADR-009：方案持久化用「单表全量快照」，不拆 messages / documents 表

**状态**：Accepted（已落地，P1 实施）

### Context

原路线图 P1 计划建 `conversations` / `messages` / `documents` 三张表。但 V2 前端的状态模型本来就是
**一份全量快照** `SessionDataV2`（`viewState` / `entryMode` / `summary` / `messages` / `conversationId`
/ 四份文档内容）。拆表意味着每次自动保存都要做差集同步，而它带来的收益（按消息 / 文档粒度查询）
**当前没有任何需求用到**。

### Options

| 方案 | 优点 | 代价 |
| --- | --- | --- |
| **A. 单表 `solutions` 存全量 JSON + 冗余摘要列** | 保存就是一次 UPSERT，天然契合前端快照模型；防抖自动保存实现简单 | 无法按消息粒度查询统计；单行较大（一份 PRD 数十 KB） |
| B. 三表范式化 | 查询灵活，可做消息级统计 | 每次保存做差集同步；自动保存逻辑复杂；当前无查询需求 |
| C. 每方案一个 JSON 文件 | 最简单 | 无列表 / 排序 / 权限过滤能力，与「方案列表页」需求直接冲突 |

### Decision

**选 A。** `solutions(id, user_id NOT NULL 外键, title, mode, stage, data, created_at, updated_at)`，
其中 `data` 是整份 `SessionDataV2` 的 JSON 文本。

`title` / `mode` / `stage` 冗余成独立列，是为了**侧边栏列表不反序列化大 JSON** 就能渲染——
一份快照含三份文档正文，几十 KB 起步，列 20 条时差别很明显。这三个冗余列由前端随快照一起
提交（前端才理解 `product_name` 该从 `summary` 还是 `quickInput` 取），后端只做 trim 与长度校验；
后端**不解析 `data` 的内容**，保持「不透明快照」这个好处。

配套：
- 越权访问返回 **404 而非 403**——403 会泄露「这个资源确实存在」这一信息。
- 数据隔离**在 Repository 层强制 `owner_id` 过滤**，而不是靠每个路由记得检查：漏一次就是越权，
  不能把安全边界寄托在「每个写接口的人都记得」。Repository 方法**不接受 `owner_id=None`**，
  不给「不过滤」留逃生口。
- **方案「有内容了才建」**：快照的内容字段（`summary` / `messages` / `quickInput` /
  `debugPrdText` / 三份文档）任一非空时才 `POST` 创建，随后 `router.replace` 到 `/v2/{id}`；
  此后一律 `PUT`。因此保存天然分**创建 / 更新两条路径**（5 个接口而非 4 个）。
  代价是用户敲一个字就走会留下一项，靠侧边栏删除解决；收益是「填了半天刷新全丢」不会发生。
- **不改动 `conversation_service` 的内存 dict**：对话进行中的临时态仍在内存，只在用户保存 /
  进入下一阶段时落库，避免把流式生成的热路径绑上 DB 写。
- **列表不做独立页面**，改常驻左侧边栏（顶部「新建任务」+ 下方列表）。列表是工作台的一部分，
  不是另一个目的地。
- **顺带删掉** `stores/workflow.ts` 的 localStorage 快照：后端是唯一权威，留离线副本只会带来
  「恢复时该听谁的」的歧义。但它的快照校验与降级逻辑保留，改用于后端恢复。
- 接口前缀用 **`/api/solutions/*` 而非 `/api/session/*`**：引入登录后，「session」会和
  「登录会话（auth session）」语义撞车，两者是完全不同的东西。前端 service 文件同步命名
  `solutionService.ts`。

### Consequences

- 变容易：自动保存（防抖 UPSERT）实现直接；列表页查询轻量；权限只在一处强制。
- 变困难：将来若要「跨方案搜索某句话」需要扫描 JSON（届时再考虑 FTS5 或拆表）。
- 中立：单行体积可控——SQLite 单行上限远超需求，且少量人使用的场景下方案数量级有限。

---

## ADR-010：提示词归后端独占，前端只能「点名」场景，不能「递剧本」文本

**状态**：Accepted（2026-10-13 已实施）

### Context

提示词资产的分布极不对称：后端有 `services/prompts.py`（13 个模板）、`skills/prd-generator/**`
（4 份技能资源）、`core/question_config.json`；**前端只有 1 处**——`useClarificationChat.ts` 里的
澄清人设常量。

但这一处是通过 `system_prompt_override: str` 下发的，即**接口接受任意提示词文本**。三个后果：

1. **提示词成了公开 API 的一部分**：改一句措辞要动前端并重新部署，而它本质是后端的领域资产。
2. **同一份口径有两个家，必然漂移——并且已经真实发生**：前端人设写「每次只提 1-3 个问题」，
   后端开场白写「一次问 3-5 个」。开场白是对话里**最后、最具体**的一条 user 消息，实际把上限
   盖成了 5，表现为「首轮复述完理解后紧接一大串问题」。
3. **失败是静默的**：传错或传空会悄悄退回另一个人设，日志、响应里没有任何信号。

### Options

| 方案 | 优点 | 代价 |
| --- | --- | --- |
| **A. 后端注册表，前端零提示词** | 提示词只有一个家；改提示词不动前端；口径漂移在结构上不可能 | 前端无法临时换人设（当前无此需求） |
| B. 前端传 profile key，后端解析 | 保留「点名」能力，为多场景留门 | 仍是跨端契约（要处理拼错 key）；当前只有一个场景，字段是纯粹的负担 |
| C. 维持 `system_prompt_override` | 不改 | 问题一个不解决，且已经咬过人 |

### Decision

**选 A。** 对话类接口不再接受任何提示词参数，人设与开场白由后端按「对话档案」解析。

- 新增 `ConversationProfile`（frozen dataclass），把**系统人设 + 首轮开场白打包成同一个
  不可分割的对象**。它们是同一份口径的两半，必须一起改；打包之后由类型结构来保证，
  而不是靠人的记忆和 review。
- 注册表 `CONVERSATION_PROFILES` + `DEFAULT_CONVERSATION_PROFILE = "clarification"`。
  当前只有一个档案，但**保留注册表结构**：将来新增对话场景（如需求评审）只需登记一条，
  接口契约与前端都不必变动。
- **前端连 profile key 都不传**：对话接口在语义上只服务「澄清」这一件事，让前端再传一个
  恒为 `"clarification"` 的常量，只是把同一个默认值抄两遍（这也是否决 B 的核心理由）。
- 会话记录存 **profile key** 而非解析后的文本：改提示词对进行中的会话即时生效，库里也不会
  留下「冻结的旧人设」。代价是进行中的会话下一轮会换上新提示词——这正是想要的行为。
- 服务构造时先 `resolve_profile()` 校验一次：档案名写错在**启动阶段**就炸，而不是某次对话
  中途静默退回默认人设。
- `_build_lc_messages()` 的 `system_prompt` 参数**取消默认值**：忘记传时立刻 `TypeError`，
  而不是悄悄用上另一个人设（默认值是这类 bug 的温床）。

### Consequences

- 变容易：改提示词只动后端一个文件；「跨端口径漂移」这类 bug 从结构上消失；无状态重试不再
  需要前端补发人设，也不再存在「忘了补发 → 悄悄换人设」。
- 变困难：前端若真要临时换人设，得先在后端登记一个档案（当前无此需求，这是**有意的摩擦**）。
- 中立：接口从「客户端可配置」变成「后端决定」——但在单产品、自家前端的场景下，
  那份可配置性本来就没有消费者。
- **残留风险（已收口）**：`ChatStage.vue` 的副标题曾写作「每轮只问 1-2 个关键细节」，是同一份
  口径在 UI 层的副本。已改为不含数字的「AI 会追问关键细节，逐步澄清后再生成文档」——提问数量
  属于后端提示词的领域知识，用户可见文案不该复述具体数字，否则改上限又要手工同步。今后同类
  文案一律走「行为描述」而非「数值复述」。
- **测试护栏**（`backend/tests/test_conversation_prompt.py`）：断言 ① 同一档案内人设与开场白
  口径一致且不残留旧数字；② 两个请求模型不含提示词字段；③ **前端源码里搜不到任何提示词正文**
  （扫描 `frontend/src` 的全部 `.ts` / `.vue`，`__tests__` 除外——它需要引用片段做「不存在」断言）。
  以后谁把提示词写回前端，会被这条不变量测试直接拦下。

---

## ADR-011：生成前「双闸门」——先确认摘要、先检索再生成

**状态**：Accepted（2026-10-13 已落地，5766e7d）

### Context

PRD 生成之前，历史上直接拿「对话结果」去生成，带来两个隐患：

1. **对话理解可能跑偏，但用户看不到就直接生成了**：澄清对话产出一份「需求摘要」，若它与用户本意有偏差，下游三份文档会整体跑偏，且用户没有「在花钱生成前纠偏」的机会。
2. **接口文档生成缺乏依据**：PRD → 接口文档这一步，模型需要「团队内部的接口规范 / 约定」作为检索素材，但之前没有把这份资产接入生成链路，产出质量不稳定且不可控。

这两件事的本质是同一个决策：**「生成」不是一步动作，而是「先收敛输入、再消费输入」的两段式**。把收敛点显式化成闸门，用户能在每个闸门确认/勾选，而不是等产物出来才发现方向错了。

### Options

| 方案 | 优点 | 代价 |
| --- | --- | --- |
| **A. 双闸门（摘要 Diff + RAG 检索）** | 用户在「花钱生成前」纠偏；接口文档有可勾选的检索依据；每道闸门可独立测试 | 多两个前端面板（`SummaryDiffPanel` / `RagResultPanel`）+ 两个后端动作；流程状态机多两个节点 |
| B. 生成后整体审阅 | 改动最小 | 纠偏太晚——三份文档已生成，重来成本高；接口文档无依据 |
| C. 去掉摘要环节，直接生成 | 步骤最少 | 对话跑偏零兜底；与「用户体感最差的一环是生成不可靠」的目标相悖 |

### Decision

**选 A。** 在两处落闸：

- **闸门 1（摘要 Diff）**：澄清对话结束后，后端 `sync-summary-from-conversation` 产出结构化摘要；前端 `diffSummaries()` 把它与现有 `summary` 做 diff，经 `SummaryDiffPanel` **用户确认才回写**。回填失败 / 非法 JSON → 返回原摘要深拷贝，绝不弄丢用户已填需求。
- **闸门 2（RAG 检索）**：进入接口文档生成前，后端 `retrieve-api-docs-rag` 对 `core/api_doc_knowledge.json` 做**本地词法检索**（中文二元组 + TF-IDF 余弦，不依赖外部向量库），返回候选条目；前端 `RagResultPanel` 勾选后随生成请求下发，作为接口文档的依据。

配套约定：

- 两道闸门都**可跳过但默认呈现**——闸门是「确认点」而非「阻塞墙」，用户可一键接受默认。
- 检索是**本地词法**而非向量库：知识库体量小（单 JSON）、部署要零额外服务（ADR-002 的精神延伸），且避免引入 embedding 服务的运维负担。代价是召回精度弱于语义检索，但接口规范类文本词面命中率高，够用。
- `api_doc_knowledge.json` 因此成为**部署资产**（见路线图 R2-T8 / R3）：Docker 化时必须随镜像打包，否则检索静默失效。

### Consequences

- 变容易：生成方向在「花钱前」纠偏；接口文档有可追溯的依据；每道闸门有独立面板与状态，可单测。
- 变困难：流程状态机多两个节点（`workflow.ts` 的 `ViewState` 与 `STEP_OF_STATE` 需同步），且闸门 1 的「摘要回填」与闸门 2 的「检索勾选」都要在持久化快照里保存（否则刷新后闸门状态丢失）。
- 中立：闸门不改变「后端为唯一真相」的前提（ADR-001）——摘要与勾选结果最终都随方案快照落库。
- 风险点：若 `api_doc_knowledge.json` 缺失，检索会空手而归而非报错，接口文档质量悄悄下降。R2-T6/T8 通过「资源缺失显式告警 + 打包登记」收口。

---

## ADR-012：错误契约用「机器码 + 人类消息」，`code` 是唯一判据

**状态**：Accepted（2026-10-14 已落地，`7be5989`；R0-T5）

### Context

前端要靠**匹配后端返回的中文文案**来决定行为：

```ts
// frontend/src/composables/useClarificationChat.ts:128（改造前）
if (!conversationId.value || !message.includes('会话不存在')) throw error
```

这段判据有两个致命性质：

1. **静默失效**：后端只要把「会话不存在」改一个字，前端降级逻辑（清掉失效会话 ID 后无状态重试）就不再触发，用户直接卡死，**且没有任何测试会发现**——测试不会去改后端文案。
2. **来源被误判**：按表面看它像是在匹配 HTTP 响应的 `detail`，实测**该文案来自 HTTP 200 的 SSE 流内 `error` 事件**（`conversation_service.py:487` 的 `StreamEvent(type="error", ...)`）。只给 REST 响应加码**完全解决不了这条链路**。

同时后端错误有 **4 个产出来源**，只改一处会漏：`HTTPException` → `{"detail":...}`、`RequestValidationError`（422，`detail` 是列表）、中间件裸写的 ASGI JSON（`deps.py::_send_unauthorized`）、SSE `error` 事件。全仓库当时 grep `error_code` 零命中。

### Options

| 方案 | 优点 | 代价 |
| --- | --- | --- |
| **A. `detail` 一字不改，并列新增 `code`** | 向后兼容：现有 93 个后端测试与前端 `readDetail()` 全部不受影响；`detail` 从此可随时改写而不影响逻辑 | 响应体多一个字段（冗余但无害） |
| B. 改成 `{"error": {"code":..,"message":..}}` 规范结构 | 结构上更"标准" | 推翻现有 93 个测试与前端读取逻辑，**收益与 A 相同**，纯破坏性 |
| C. 维持字符串匹配，只加测试 | 零改动 | 测试无法阻止后端改文案；契约仍然脆 |

### Decision

**选 A。**

- 响应体：`{"detail": <原文一字不改>, "code": "<UPPER_SNAKE_CASE>"}`；422 的 `detail` 仍保持 FastAPI 的列表结构。
- SSE `error` 事件：`code` 放在**顶层字段**（不塞进 `data`），`content` 仍是中文原文。
- 后端单一真源 `core/errors.py`（`ErrorCode` 8 个码 + `AppError` + `DEFAULT_STATUS` + `error_payload`）。放 `core/` 而非 `api/`，避免 services 反向依赖 api。
- **必须注册自定义异常处理器**：`HTTPException(detail={"detail":..,"code":..})` 会被 FastAPI 渲染成 `{"detail":{"detail":...}}` 嵌套结构，`register_error_handlers(app)` 是唯一能「一处保证所有错误响应都带 code」的做法。
- 前端单一真源 `services/errors.ts`（`BACKEND_ERROR_CODES` / `ApiError` / `isApiError` / `toErrorCode`）。**必须放独立模块**：`AppV2.spec.ts` 用 `vi.mock('../services/api', ...)` 整体替换 api 模块，若 `ApiError` 定义在 `api.ts` 里，`instanceof` 会抛 `TypeError`。
- 只定义 **8 个真正用得上的码**，不搞大而全枚举（错误码体系最容易膨胀成没人维护的摆设）。
- 已做对的部分**不动**：401 → `UnauthorizedError`、404 → `SolutionNotFoundError` 本就基于 HTTP 状态码，是正确的。

### Consequences

- 变容易：后端文案可随时改写（产品措辞调整不再牵动前端逻辑）；新增机器判据只需加码。
- 变困难：新增错误码必须**前后端同步**，否则不变量测试 `test_frontend_error_codes_match_backend`（正则解析 `errors.ts` 断言与后端枚举**双向严格相等**）立刻报红。这是刻意的——漂移应在 CI 暴露，而非运行时。
- 中立：框架级 404/405（如访问不存在的路径）仍无 `code`——为守住 8 个码的最小集合，未注册 `StarletteHTTPException` 兜底处理器。已知边界，接受。
- 防回退由 4 条不变量测试守护（每个错误响应带 `code`、前后端码值相等、前端源码禁中文串判断、`api/` 层禁裸 `HTTPException`），并用**变异测试**验证守卫不空转（删码 / 加裸 `HTTPException` / 加中文匹配 / 退回字符串判据 → 均正确报红）。

---

## ADR-013：流式编排收敛到 `stream_reply.py` 一处

**状态**：Accepted（2026-10-14 已落地，R0-T3）

### Context

「把模型流变成 SSE 事件」这件事在系统里有 **四份实现**（2026-10-14 实测）：

| # | 位置 | 规模 | 说明 |
| --- | --- | --- | --- |
| 1 | `conversation_service._stream_reply` | 366–408（44 行） | 带 `on_finish` 回调 |
| 2 | `document_service._stream_reply` | 626–668（48 行） | 无 `on_finish` |
| 3 | `prd_section_writer._error_event` | 775–786 | **第三份**「异常 → error 事件」转换，其注释自认「与 `document_service._stream_reply` 的口径一致」——**靠注释保证一致** |
| 4 | `conversation_service._extract_text` | 101–118 | 与 `llm_utils.extract_text`（21 行）**逐行相同**的副本 |

先前已抽出 `stream_guard.py::ensure_not_truncated`（截断检测），但**核心编排的重复没有消除**。三处「异常 → error 事件」各自维护的后果是：改错误码口径要改三处，漏一处则前端对同一种故障收到两种分类。

两份 `_stream_reply` 的差异仅三处：`on_finish`（conversation 用于回写历史，452/524 两处调用）、文本提取用哪个函数、截断检查放在 `_astream_text` 内还是内联（**两者都落在 `try` 内 → 语义等价**）。

> 附带更正：旧文档一度把「`conversation_service.py:366` / `document_service.py:626`」的**行号**当成行数，将本任务描述为「365 / 625 行的大手术」。真实规模约 90 行。

### Options

| 方案 | 优点 | 代价 |
| --- | --- | --- |
| **A. 新建 `services/stream_reply.py`，三处改为调用** | 单一实现；错误口径不可能漂移；`stream_guard.py` 保持只管截断检测 | 多一个模块 |
| B. 并入现有 `stream_guard.py` | 不新增文件 | `stream_guard` 的语义是「截断检测」，混入编排会让该文件失去单一职责 |
| C. 抽成基类/混入由两个 service 继承 | 不改调用点 | 引入继承只为复用一段函数，收益不抵复杂度；且两 service 并无真实 is-a 关系 |

### Decision

**选 A。** 新增 `services/stream_reply.py`，提供统一的事件流编排（`messages` / `status_text` / 可选 `on_finish` / `extra_data` / 错误码）与**唯一的**「异常 → error 事件」转换，供三个 service 复用；`stream_guard.py` 保持只管截断检测。

实施顺序是硬约束：**先写对照测试（`backend/tests/test_stream_reply.py`，用现有 `FakeLLM` 范式）钉死当前行为，再统一**。理由是两份 `_stream_reply` 当时**零直接单测**、`stream_guard` 无专属测试文件，只有 `test_prd_section_stream.py` 5 例覆盖分章节那条流——没有网不能动。

对照测试必须覆盖：事件序列 `status → chunk* → done`；异常 → `error` 事件（`code=LLM_UPSTREAM_ERROR`）**且之后不再发 `done`**；截断 → `error` 事件；`on_finish` 在 `done` 之前调用且**仅在成功时**调用；`extra_data` 透传进 `done.data`。conversation / document 两条路径参数化各跑一遍。

**不做**：不改事件序列、文案、错误码（对外契约零变化）；不合并 `_collect_reply`（语义是"收拢成整段文本"供审核解析 JSON，不是事件流）；不合并 `prd_section_writer._stream_text` / `_collect`（分章节有自己的 `stage`/`part` 语义，硬并会抹平差异）。

### Consequences

- 变容易：错误口径只有一处；加 `request_id` 打点（R2-T7）、加 token 计量（R1-T5）都有了唯一落点——这是本决策**最大的隐藏收益**。
- 变困难：统一实现的参数列表要同时满足三个调用方，若将来差异扩大需警惕「配置项膨胀」——届时宁可拆成两个具名函数，也不要往一个函数里堆开关。
- 中立：`document_service._collect_reply` 与 `prd_section_writer._collect` 保留为具名方法（语义是"收拢成整段文本"，不是事件流），但内部改为复用共享的 `collect_reply`，不再各自维护 `last_chunk` / 截断检查那段循环。

### 实施记录（2026-10-14）

- **先差分、后替换**：改动前用一次性脚本对旧实现与新实现跑了 **12 组场景**（正常多块 / 单块 / 截断 `length` 与 `max_tokens` / 空输出 / 中途异常 / 结构化列表分块 / 省略 `status_text`），比对逐事件的 `(type, content, data, code)`，**全部一致**后才删除旧代码（脚本已删除，不入库）。
- 删除：`conversation_service._stream_reply` / `_astream_text` / `_extract_text`、`document_service._stream_reply`、`prd_section_writer._error_event` 包装；6 处调用点改为直接调用共享函数。
- 新增 `backend/tests/test_stream_reply.py`（10 例）：事件序列、异常转 `error` 且不发 `done`、`on_finish` 仅成功时调用、结构化分块摊平、截断抛错、默认错误码；外加一条**源码扫描不变量**——`services/` 下（除 `stream_reply.py`）不许再出现 `async def _stream_reply` / `def _extract_text` / `def _astream_text`，防止重复悄悄复活。
- 回归：后端 **117**（107 + 10）、前端 **222**、`npm run build` 全绿。


