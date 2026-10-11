# HarnessPRD 架构演进与重构路线图

> 状态：Proposed
> 依据：2026-10-12 代码库快照（后端 2227 行 Python / 前端 2780 行 TS+Vue）
> 前提约束：
> 1. 项目当前处于**功能开发期**，架构目标优先服务「加功能的速度」，而非「多人部署的完备性」。
> 2. 终局形态是**少量人使用 + 简单登录**，登录系统在功能开发完成后才实施。
> 3. 因此本路线图把登录需要的 `user_id` **提前埋进 schema**，让 P4 变成「填坑」而不是「重构」。

配套文档：`ADR.md`（6 条架构决策记录及其权衡）。

---

## 一、现状评估

### 1.1 规模与形态

| 维度 | 现状 |
| --- | --- |
| 后端 | 2227 行 / 11 个模块。FastAPI + LangChain，src layout，`uv` 管理依赖 |
| 前端 | 2780 行 / 10 个文件。Vue 3 + Vite 8 + Tailwind v4，**无 router、无 store**（`pinia` 已装未用） |
| 存储 | **无数据库**。会话 = 进程内存 dict；文档正文 = 浏览器本地快照 |
| 测试 | 后端 0 用例（无 pytest 依赖）；前端 vitest 已装但 0 用例 |
| 工程化 | 无 CI、无 Docker、无结构化日志；部署靠本机 `uvicorn` + `vite dev` |
| 交付 | V1 已收尾：问卷 → 需求澄清对话 → PRD → 接口文档 → 提示词套件 |

这是一个**刚跑通主干、尚未沉淀结构的原型**。它的价值在于链路完整，风险在于每加一个功能都要在同一堆耦合里再凿一次。

### 1.2 已经做对的（明确不要动）

重构最容易犯的错是把好的一起推翻。以下几处是全项目最干净的部分，路线图只做「复用」不做「替换」：

- **`services/llm_factory.py` 的供应商抽象**：ark / anthropic / openai 三选一，统一暴露 `astream`，业务层完全无感。这是后续做 failover 的天然接入点。
- **路由层与 service 层分离**：`api/` 只做参数解析与响应封装，`schemas.py` 集中声明数据契约。
- **SSE 事件协议**：`status / chunk / done / error` + 末尾 `[DONE]`，前后端一致，且异常在流内转事件、两层兜底（`_make_sse_generator` 与服务层各一层）。
- **技能包驱动的 PRD 生成**：`skills/prd-generator` 提供字段契约、模板、写作规范，缺失时自动降级到内置提示词，服务不中断。

### 1.3 债务清单（按处理优先级排序）

| 编号 | 问题 | 位置 | 代价 | 处理阶段 |
| --- | --- | --- | --- | --- |
| D1 | **状态双真相**：后端内存 dict（重启即丢）+ 浏览器本地快照，靠回传 history 缝合 | `conversation_service._conversations`；`App.vue` 的 SESSION_KEY | 无法重启保活、无法跨设备、无法回溯历史会话；改一次后端代码就要重填问卷才能调试 | P1 |
| D2 | **流式编排被抄两遍**：`_stream_reply` 与 `_extract_text` 各两份，逻辑几乎逐行相同 | `conversation_service.py:347` vs `document_service.py:281`；`:102` vs `:325` | 改一处漏一处，且两处行为会静默漂移 | P0 |
| D3 | **死接口在线**：3 条 REST 路由调用后抛 `NotImplementedError`，线上返回 500 | `api/conversation.py:227-274`；`document_service.py:498-520` | 接口契约自相矛盾，`DocumentResponse` 是为不存在的 DB 设计的 | P0（删）→ P1（重定义） |
| D4 | **错误契约靠中文字符串匹配** | `App.vue:374` `message.includes('会话不存在')` | 后端改一个字，前端降级逻辑静默失效 | P0 |
| D5 | **App.vue 巨石**：1305 行混着编排、9 态状态机、持久化、文档生成、下载 | `frontend/src/App.vue` | 加一种文档类型要同时改状态、视图、进度条、下载四处 | P2 |
| D6 | **零测试护栏** | 后端无 pytest；前端 0 用例 | 任何重构都是裸奔，回归靠手动点全流程 | P0 |
| D7 | **长文生成不可续传**：单次 `astream` 出整篇 PRD，`ark_max_tokens` 默认 4096 | `core/config.py:73-77`（注释已自认会截断）；`document_service.generate_prd_stream` | 中断即全废；PRD 越长越危险 | P3 |
| D8 | **提示词资产定位脆弱**：靠「向上找 6 层目录」找 `skills/`，部署时目录不在同一棵树就失效 | `document_service._resolve_skill_dir` | 换部署方式就可能静默降级到内置提示词，输出质量悄悄下降 | P3 |
| D9 | **无可观测性与成本计量**：只有 `/health`，无 request_id，无 token 打点 | 全局 | 出问题时无法归因；成本不可见 | P3 |
| D10 | **技能资源常驻缓存**：`lru_cache` 缓存到进程结束，改模板必须重启 | `document_service._load_skill_artifact` | 调提示词时改一次重启一次，迭代极慢 | P3 |

---

## 二、目标架构

**一句话**：后端数据库成为会话与文档的唯一真相，前端只负责展示与离线缓存；流式编排全局只有一份实现；登录系统通过预留的 `user_id` 插槽后接，不改 schema。

### 2.1 分层与依赖方向

```
前端：阶段视图组件 → 状态机 + 会话 store → api 层（SSE 封装 + 错误码）
后端：api/ 路由（conversation + documents） → services（共用流式编排） → repositories → SQLite
```

**依赖规则（写进 code review 清单）**：

1. `api → services → repositories → db`，**禁止反向 import**。
2. `services` 之间**互不 import**；需要复用就下沉到新的公共模块（如 `services/streaming.py`）。
3. 前端视图组件**不得直接调 `api.ts`**，必须经过 store / composable。
4. `schemas.py` 是后端唯一的数据契约出处；前端 `types/index.ts` 是前端唯一出处。两者不一致时**改后端迁就前端**（既有约定，继续沿用）。

### 2.2 与现状的关键差异

| 关注点 | 现状 | 目标 |
| --- | --- | --- |
| 会话状态 | 内存 dict + 浏览器快照，双真相 | SQLite 唯一真相；浏览器快照降级为离线草稿 |
| 流式编排 | 两份重复实现 | `services/streaming.py` 一份，两个 service 共用 |
| 文档 | 生成后只存在前端内存 | 落库 `documents` 表，带版本号 |
| 错误 | 中文字符串 | `error_code` 枚举 + 人类可读 message |
| 阶段流转 | 散在 `goTo` / 多个 computed 里 | `workflow.ts` 纯函数，可单测 |
| 登录 | 无 | `user_id` 已预留（NULL），P4 只填值 |

---

## 三、演进路线图

排序原则：**先降「改代码的成本」，再降「跑起来的成本」，最后才是「多人用的成本」**。

---

### P0 · 护栏：让重构不再裸奔

**周期**：1 周内　**功能变化**：零　**风险**：低

**目标**：在动任何结构之前先装安全网。本阶段不修任何 bug，只让后续每个阶段敢动手。

| 任务 | 具体动作 |
| --- | --- |
| T1 补后端测试（pytest） | 新增 `pytest` 依赖。首批锁住：`format_form_data`（空值/多选/配置外字段）、`_build_lc_messages`（角色归一化、user/assistant 严格交替）、`_format_sse_block`（帧格式、`[DONE]`）、技能资源加载的降级路径 |
| T2 补前端测试（vitest） | 首批锁住：`parseSseBlock`（**重点覆盖中文多字节被切断**与坏帧容错）、`STEP_OF_STATE` 映射完整性、会话快照的校验与 `generating-*` 降级 |
| T3 合并流式编排 | 新建 `services/streaming.py`，把两份 `_stream_reply` 与 `_extract_text` 收敛为一份；两个 service 改为调用它。行为必须逐字保持一致 |
| T4 删死接口 | 移除 `api/conversation.py` 的 3 条 `NotImplementedError` 路由与 `DocumentResponse`（P1 会用真实持久化重新定义） |
| T5 错误契约码化 | `StreamEvent.data` 增加 `error_code`：`conversation_not_found` / `output_truncated` / `llm_error` / `invalid_input`。前端改判 code，删掉 `includes('会话不存在')` |

**验收**：
- `uv run pytest` 与 `npm run test:unit` 全绿，且都有 CI-ready 的单条命令入口。
- 手动跑通一次完整流程（问卷 → 对话 → 三份文档），输出与重构前逐字一致。
- 故意改坏 `prd-template.md`，测试能红灯。

**不做**：不碰持久化、不改 UI、不动提示词内容。

**风险**：T3 合并时两份实现的边角行为可能不一致（例如 `document_service` 的 `last_chunk` 在异常时保留、`conversation_service` 在 `_astream_text` 内持有）。合并前先写对照测试固定两边当前行为，再统一。

---

### P1 · 状态真相统一：会话可回溯

**周期**：2-3 周　**风险**：中（改动面最大）

**目标**：会话与文档成为可持久化、可列表、可回溯的实体。这是开发期摩擦最大的一环——现在每改一次后端代码（`--reload`）会话就丢，得重填问卷才能测 PRD 生成。

| 任务 | 具体动作 |
| --- | --- |
| T1 引入 Repository 层 | 新建 `repositories/`，定义 `ConversationRepository` / `DocumentRepository` 协议（Protocol 或 ABC）。服务层只依赖接口，不依赖具体 DB |
| T2 SQLite 建表 | `conversations(id, title, user_id NULL, form_data JSON, created_at, updated_at)`、`messages(id, conversation_id, role, content, seq)`、`documents(id, conversation_id, doc_type, content, version, skill_version)`。**`user_id` 允许 NULL**，这是给 P4 的插槽 |
| T3 会话服务改造 | `ConversationService._conversations` 内存 dict 替换为 Repository 调用；`_ConversationRecord` 退化为领域对象 |
| T4 文档落库 | 三份文档生成后写入 `documents`，支持版本叠加（优化一次 +1 版） |
| T5 新增恢复接口 | `GET /api/conversations`（列表）、`GET /api/conversations/{id}`（含消息 + 文档）、`DELETE /api/conversations/{id}` |
| T6 前端恢复逻辑 | 刷新后按 `conversation_id` 从后端拉回完整状态；浏览器本地快照**降级为离线草稿**（后端不可用时才用） |
| T7 开启 WAL | SQLite 开 `journal_mode=WAL`，为 P4 的少量并发写预留 |

**验收**：
- `kill` 后端再启动，用同一个 `conversation_id` 能继续对话，上下文完整。
- 刷新页面后状态与后端一致；断网时本地快照仍能显示上次内容。
- 历史会话可列表、可点进去回溯、可删除。

**不做**：不做鉴权、不做多用户隔离（P4 再做）；不迁移任何历史数据（当前无持久数据）。

**风险**：T3 改造后，「前端传 history 的无状态模式」会与「后端真相」产生歧义。决策见 ADR-001：**后端历史为唯一权威，前端传入的 history 仅在会话不存在时作为恢复来源**，且需要在响应里明确告知前端用的是哪一份。

---

### P2 · 前端解耦：加一种文档类型只动一处

**周期**：2 周　**风险**：中

**目标**：把 `App.vue` 从「什么都做」削到「只做分派」。

| 任务 | 具体动作 |
| --- | --- |
| T1 状态机外置 | 把 `goTo` / `canClickStep` / `reachedStepIndex` / `STEP_OF_STATE` / `GENERATING_TO_REVIEW` 抽成 `workflow.ts` 的**纯函数**（`next()` / `canGoTo()` / `reachedIndex()` / `degradeGenerating()`），配单测 |
| T2 阶段视图拆分 | `views/FormView.vue` / `ChatView.vue` / `ReviewView.vue` / `DoneView.vue`。`App.vue` 只保留 header、进度条、按 `viewState` 分派 |
| T3 状态收进 store | 三份文档、消息、会话 ID 收进 Pinia store（依赖已装未用），或 `useSession()` composable。二选一，见 ADR-003 |
| T4 api 层统一 | `api.ts` 里 SSE（fetch）与 axios 两套收口到一个客户端；错误统一转 `AppError { code, message }`，视图层不再做字符串判断 |
| T5 文档类型参数化 | 三份文档的生成/审阅/下载目前是三套近似代码，改为按 `docType` 参数驱动。新增第四种文档只加配置 |

**验收**：
- `App.vue` < 250 行（现 1305 行）。
- `workflow.ts` 测试覆盖所有 9 个状态与降级路径。
- 新增一种文档类型只改 `workflow.ts` 配置 + 加一个视图，不碰编排逻辑。

**不做**：不引入 `vue-router`（理由见 ADR-006）；不做组件库抽象，Tailwind class 继续内联。

**风险**：T2 拆分时，`App.vue` 里散落的 `activeDocKey` / `activeDocTitle` / `activeDocContent` 是隐式耦合点，拆之前先用测试固定它们的行为。

---

### P3 · 生成链路工业化：长文不再一断全废

**周期**：3-4 周　**风险**：中高（依赖 P1 的文档落库）

**目标**：把「能生成」升级成「可靠地生成」。

| 任务 | 具体动作 |
| --- | --- |
| T1 分章节生成 | 依据 `field-schema.json` 的章节拆成 N 次调用，逐节流式 + 组装；单节失败只重试该节 |
| T2 断点续传 | 中间产物落 `documents.sections`，中断后可续；已完成的章节不重复消耗 token |
| T3 max_tokens 分档 | 按文档类型配置（PRD 用大值，澄清对话用小值），把默认 4096 改成不截断的值 |
| T4 供应商 failover | `llm_factory` 增加主备切换、超时与有限重试。业务层依然无感 |
| T5 成本计量 | 每次生成记录 token 用量与供应商模型名，落库并可查询 |
| T6 提示词资产工程化 | 技能资源加版本号；开发态热重载（替换现有常驻缓存），生产态打包进镜像；每次生成记录所用版本 |
| T7 定位方式加固 | `HARNESSPRD_SKILLS_DIR` 从「可选兜底」提升为「生产必填」，启动时校验，避免静默降级 |

**验收**：
- 在生成中途断开网络，恢复后能从断点续，已完成章节不重复计费。
- 一次 PRD 生成的 token 成本与所用提示词版本可查。
- 把主供应商的 key 置空，自动切到备用供应商，业务代码零改动。

**不做**：不做多轮自评/批判式生成（先把单次可靠性做扎实）；不做异步任务队列（当前同步流式的体感更好，除非并发上来）。

**风险**：T1 分章节会改变 PRD「一气呵成」的连贯性，可能出现章节间口径不一致。**必须保留「整篇生成」作为可切换的 fallback**，两种模式由配置开关控制，对比后再决定默认用哪个。

---

### P4 · 登录与少量人使用

**周期**：功能开发完成后　**依赖**：P1 已预留 `user_id`

**目标**：几个人能各自用自己的会话，互不干扰。

| 任务 | 具体动作 |
| --- | --- |
| T1 极简账号 | 账号密码 + Session Cookie。或直接用托管后端（邮箱/手机验证码）省掉自建 |
| T2 填上 user_id | 登录写入 `user_id`；Repository 查询加 owner 过滤。既有 NULL 数据归属到一个默认用户 |
| T3 部署打包 | Docker Compose 起全栈；`skills/` 目录随镜像打包，消除「向上找 6 层目录」的脆弱定位 |
| T4 CI | GitHub Actions 跑 `pytest` + `vitest` + `vue-tsc` 类型检查 |
| T5 健康检查加料 | `/health` 返回 DB 连通性与当前供应商可用性 |

**验收**：两个账号互不可见对方会话；一条命令起全栈；CI 红灯能拦住破坏性改动。

**不做**：不做计费、不做角色权限体系、不做租户隔离（少量人场景，owner 过滤足够）。

---

## 四、为什么是这个顺序

| 决策 | 理由 |
| --- | --- |
| **P0 最先** | 它是后面所有重构的保险，且**零功能变化、当天可见效**。没有护栏就动 P1/P2，等于在没有网的情况下走钢丝 |
| **P1 先于 P2** | 持久化会**改变后端接口形态**（新增会话列表/恢复/删除接口）。先定后端契约再拆前端，否则前端刚拆完就要因为接口变动再改一遍 |
| **P2 紧随** | 你正处于功能开发期，会不断加文档类型、改流程。巨石组件此时的改动成本最高，越早拆越省 |
| **P3 在 P1 之后** | 断点续传依赖「文档中间产物落库」，而落库是 P1 的产出。顺序不可颠倒 |
| **P4 最后** | 你已明确「功能开发完再做登录」。且 P1 已把 `user_id` 埋好，届时是填坑不是重构——这正是**可逆性优先**的价值 |

**替代方案及其被否理由**：

- **先拆前端再搞持久化**：接口未定就拆组件，前端会返工两次。否决。
- **直接上 Postgres + Docker**：当前单用户本地开发，SQLite 单文件的运维成本近乎为零，且通过 Repository 隔离后切换成本很低（见 ADR-002）。否决。
- **现在就做登录**：功能开发期每次调试都要登录，拖慢迭代；且 `user_id` 已预留，晚做不产生额外成本。否决。

---

## 五、贯穿始终的检查点

每个阶段结束前自查：

1. **行为未变**：P0/P2 结束后，完整跑一遍 V1 流程，输出与重构前逐字比对。
2. **测试增长**：每个阶段结束时用例数只增不减。
3. **依赖方向**：`api → services → repositories`，无反向 import；services 之间无横向 import。
4. **契约一致**：后端 `schemas.py` 与前端 `types/index.ts` 字段名一致。
5. **错误码化**：新增错误必须带 `error_code`，前端不得新增字符串匹配。

---

## 六、风险登记

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| P1 改造后无状态模式与后端历史冲突 | 上下文错乱、重复回复 | ADR-001 明确权威来源；响应里回传所用历史来源 |
| P3 分章节导致章节间口径不一致 | PRD 质量下降 | 保留整篇模式开关，A/B 对比后再定默认值 |
| SQLite 在 P4 少量并发写时锁竞争 | 写入卡顿 | 开 WAL；真不够用时经 Repository 切 Postgres |
| 重构期长，功能开发被阻塞 | 拖慢交付 | P0/P1/P2 每条都可独立合入，不与功能开发互斥 |
| 提示词模板改动无法热生效 | 调提示词极慢 | P3-T6 解决；过渡期可在 `.env` 加开关关掉资源缓存 |

---

## 七、明确不做

防止过度设计，以下**不在本路线图范围内**：

- 微服务拆分。单体足够，拆了只会增加部署负担。
- CQRS / 事件溯源。读写负载都很轻，引入是纯负担。
- GraphQL。当前 9 条 REST + SSE 接口完全够用。
- 前端引入 `vue-router`。当前是 Wizard 形态，状态机比 URL 更契合（ADR-006）。
- 自研 Agent 编排框架。LangChain 的抽象当前够用，不要为了「可控」提前造轮子。
- 多租户、计费、角色权限。少量人场景，owner 过滤足够。
