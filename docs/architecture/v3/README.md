# v3 · 放心用（进行中）

> **一句话**：长文生成中断可续、单次调用失败能自愈、花了多少 token 看得见，最后能一条命令部署。
>
> **下层**：`../v1/`（一次性流程原型）、`../v2/`（能用，已完成）
> 时间线真源：**`../evolution-truth.md`**（v1 / v2 的真实边界按 git 界碑 `1c65e94` 与 tag `v2.0.0` 确定）

## 本层文件

| 文件 | 内容 | 状态 |
| --- | --- | --- |
| `ARCHITECTURE.md` | **v3 架构文档**：四条目标 → 范围 → 生成 Job 骨架 → 后端 / 前端设计 → 契约 → 取舍 → **与源文档《完整开发流程》的差异裁决（第 8 节）** | 进行中（第 4.1 / 4.2 / 4.3 节已落地） |
| `roadmap.md` | R1 / R2 / R3 全景路线图（含 v1–v2 的历史盘点、债务清单、风险登记、**版本分期的唯一真源**） | 进行中 |

两份怎么看：**先看 `ARCHITECTURE.md` 了解 v3 要建成什么样**，再看 `roadmap.md` 排期；
Job 运行时（第 4.1 节）随 R1-T0 落地，观测与预算（第 4.2 节）随 R1-T5 落地，
技能资产热重载与日志体检（ADR-017 / ADR-018）随 R2-T6 / T7 落地，
**R2-T9「技能知识层统一」（ADR-019 / ADR-020）已落地**：技能注册表 + 三个技能包就位，
14 条接口规范与提示词规范全部迁入技能包并由生成线全量注入，前端 RAG 确认页与本地词法检索整条下线。
**C 层「文档所有权与清单」也已落地**（ADR-021 ~ 028，见下面落地点一节）：三份产物归后端
`documents` / `document_versions` 所有、待确认清单成为生成的第一类产物（按 P0~P6 分级）、四条澄清线程收进
同一个对话页、会话改单点登录。
**版本链随后也落地**（ADR-029 / 030，原「本期明确推迟」由此作废）：正文全搬进
`document_versions`、`documents` 只留当前版本指针，能存为新版本、回看、恢复，展示为右侧可收起抽屉。
注意 R1-T2 断点续传已裁决延后、自动切换供应商已裁决不做（ADR-016），
动手前重点读第 4.2 / 4.3 / 4.4 / 4.6 节，实施状态以 `roadmap.md` 第八节为准。

## 版本 ↔ 任务对照

同一套工作的两种视图：**任务编号用于日常排期，版本号用于对外收口。**

| 版本阶段 | 任务阶段 | 目标 |
| --- | --- | --- |
| v3.0 | R1 | 生成可靠性：生成 Job 中间态 + 成本计量与预算 / **单次调用自愈**（超时 + 有限重试）/ 断点续传 |
| v3.1 | R2 | 技能资产加固（热重载 + fail-fast）✅、可观测性基线 ✅、**技能知识层统一 / RAG 下线 ✅**（原「RAG 资产登记」被它吸收）；另含 **C 文档生命周期 ✅**（所有权 + 清单 + 澄清 + 单点登录 + **版本链**，ADR-021 ~ 030） |
| v3.2 | R3 | Docker 化、生产加固（Cookie Secure / 邀请码轮换）、部署态隔离验证 |

## 已拍板的三件事（前两件随 R1-T0 落地，第三件随 T4 决策）

| # | 当时的选项 | 决定 | 落地位置 |
| --- | --- | --- | --- |
| 1 | run 中间态落在哪里：① 新建独立 `generation_jobs` 表（推荐）；② `solutions` 表加后端自有列；③ 塞进前端快照 `data`（❌ 与 ADR-009 冲突） | **取 ①** | `1f2ad08`：`core/db.py` 建 `generation_jobs`，`repositories/generation_job_repo.py` 读写，快照保持不透明 |
| 2 | R1 内部顺序：成本计量 → failover → 续传（推荐），还是续传提前 | **取「先装仪表」**，并且在其之前先立**生成 Job 骨架**（T0） | `1f2ad08` 完成骨架；R1 余下按 T5 → T4 → T2 排，见 `roadmap.md` 第八节 |
| 3 | 自动 failover 做不做：① 按既有设计跨供应商自动切换；② 不自动切换，只做同供应商自愈（推荐） | **取 ②**（ADR-016）。三家密钥齐备不构成必须做自动切换的理由；真正的问题是单次调用能否自愈 | `../v2/ADR.md` ADR-016；实现见下面「R1-T4 单次调用自愈落地点」 |

论证过程见 `ARCHITECTURE.md` 第 2 / 3 / 4.1 / 4.6 / 9 节；这些已不再是待决项，状态以 `roadmap.md` 第八节为准。

## R1-T5 成本计量落地点（2026-10-14）

| 议题 | 落定做法 | 位置 |
| --- | --- | --- |
| 计量挂在哪 | **环境值计量器**（`ContextVar` 承载 `RunMeter`），签名与调用方式不变（满足 4.2「业务层调用方式不变」） | `services/llm_meter.py` + `services/stream_reply.py::astream_text` |
| step 怎么来 | **派生不声明**：`step = f"{kind}:{phase}"`，`phase` 由既有 `stage` 事件灌入 | `job_service._apply_event` |
| request_id 通道 | 纯 ASGI 中间件注入 `scope` + `ContextVar`（不改业务签名；`asyncio.create_task` 自带 context 拷贝，后台任务一并继承） | `core/request_context.py` + `api/deps.py::RequestIdMiddleware` |
| 契约形态 | `core/contracts.py::RunSummary` / `StepUsage` 上模型；事件与 DB 同源同一份 JSON；终态任务重连补发 `run_summary` | `core/contracts.py` / `job_service._finalize` / `open_stream` |
| 回执放哪 | **只读展示块，不加按钮**（`request_id` 用 `select-all`，遵守 ADR-015 底栏唯一操作落点） | `components/stages/ReviewStage.vue` |
| 物理顶从哪来 | **模型注册表**按 `provider + model` 两级登记（未登记的型号退到 provider 默认值），可用 `LLM_CONTEXT_WINDOW` 覆盖；可输入额度 = 窗口 − 预留输出 | `core/model_registry.py` |
| 业务顶从哪来 | `.env`：`LLM_BUDGET_DEFAULT_CAP` + 按 step 覆盖的 `LLM_BUDGET_CAPS`（完整 step → 任务类型 → 默认值） | `core/config.py` + `services/token_budget.py::step_cap` |
| 怎么估算 | 字符启发式（不引 tiktoken）：中文 1 字 ≈ 0.8 token、其余 3.5 字符 ≈ 1 token，误差方向**刻意偏高估** | `services/token_budget.py::estimate_messages_tokens` |
| 超顶怎么办 | 抛 `BudgetExceededError`，**这次调用根本不发出**；错误码**复用 `INVALID_REQUEST`**（8 码体系不膨胀） | `services/token_budget.py` + `stream_reply.error_event` |
| 逼近上限怎么办 | 只记告警、不拦截；同一步保留最严重的一条，随 `run_summary.warnings` 回传并在回执里展示 | `llm_meter.note_warning` + `ReviewStage.vue` |
| 检查点在哪 | 与事后计量**同一个唯一出口**：`stream_reply.astream_text` 开头 | `services/stream_reply.py` |

> 结构化日志与 `/health` 扩展已随 **R2-T7** 交付（见下一节）；其余（事后计量、`request_id` 贯通、事前预算）随 R1-T5 全部交付。

## R2-T6 / R2-T7 / R2-T9-1 落地点（2026-10-15）

| 议题 | 落定做法 | 位置 |
| --- | --- | --- |
| 技能资源怎么缓存 | **按文件戳**（修改时间 + 大小）比对，改模板下一次生成即生效、不必重启；只有读成功才写缓存，文件消失时连缓存条目一起丢（ADR-017）。R2-T9 起这套缓存由注册表统一提供 | `services/skill_registry.py` |
| 「用了技能包还是兜底」怎么看 | 每次生成把 `SkillRef{name, version, fingerprint}` 写进 `run_summary.injected_skills`；为空即表示退回了内置提示词，前端据此显示告警 | `core/contracts.py` + `components/stages/ReviewStage.vue` |
| 缺失要不要拦住启动 | `.env` 的 `PRDWRIGHT_SKILLS_STRICT`（默认 false）：本地只告警，部署设 true 则**任何问题**（元数据损坏 / 资源缺失 / 多包抢同一产物）即中止启动 | `core/config.py` + `main.py::lifespan` |
| **哪个产物用哪个技能包** | 技能包在 `skill.yaml` 里用 `applies_to` 自述，程序扫 `<skills 根>/*/skill.yaml` 建索引——**加技能包 = 加目录**（ADR-019） | `services/skill_registry.py` |
| **包里有哪些必需资源** | 同一份 `skill.yaml` 的 `references` 段声明，`key` 是生成管线取用的语义键；不再有第二份硬编码清单 | `services/skill_registry.py` + `services/prd_skill.py` |
| 日志怎么带上请求号 | 格式化器从上下文变量**现取**，业务代码一行不改；`extra=` 挂的业务字段按「排除标准属性」的规则自动带出（ADR-018） | `core/logging_setup.py` |
| 访问日志记在哪 | 记在最外层 `RequestIdMiddleware` 里，**被认证拦下的 401 同样留痕**；`uvicorn.access` 的级别抬到 WARNING，避免同一次请求出两行（一行有号一行没号） | `api/deps.py` |
| 体检报什么 | 数据库 / 供应商 / **技能资产**三项分开报，`status` 取 `ok` / `degraded` 且**恒返回 200**；供应商一项只校验配置就位、**不发真实调用**（不假装验过）；技能资产一项按注册表逐个报技能包身份，**没有写死的名单** | `api/health.py` |

## C 层「文档所有权与清单」落地点（2026-10-15）

三份产物归后端所有（ADR-021）+ 待确认清单成为生成的第一类产物（ADR-023）+ 澄清工作流（ADR-025）
+ 四条澄清线程收进同一个对话页（ADR-027 / 028）+ 单点登录（ADR-024）。逐项落点：

| 议题 | 落定做法 | 位置 |
| --- | --- | --- |
| 正文归谁 | **后端**为唯一真源：`documents` 一行表示「这份文档的身份 + 当前版本指针」（`(solution_id, kind)` 唯一），正文住在 `document_versions`（版本链落地后，见下一节）；前端只有工作副本 | `repositories/document_repo.py` + `services/document_store.py` |
| 写正文的入口 | **唯一**：job 成功收尾，正文 / 清单 / 回执**同批写入一版**（整篇生成追加新版本，`optimize` / `clarify` 就地改当前版本）；失败不写 | `job_service._write_document` → `document_store.commit_generated` |
| 前端通道 | 只剩两条：`GET /api/solutions/{id}/documents`（读）、`PUT .../documents/{kind}`（手改，**不碰**清单与回执） | `services/documentService.ts` + `stores/documents.ts` |
| 快照里的正文 | `SessionDataV2` 退役三个 `*Content` 与三个 `*RunSummary`；旧快照残留**被忽略**（不做兼容读） | `types/index.ts` + `utils/workflow.ts::normalizeSessionSnapshot` |
| 加表 / 加列 | **有序幂等迁移**（`PRAGMA user_version`，每步独立事务、只跑增量）；`init_schema` 只建基线表 | `core/migrations.py` |
| 待确认清单怎么来 | 模型在正文末尾附 **HTML 注释标记块**（渲染里不可见），收尾剥离解析、`id` 由后端按「章节 + 序号」分配 | `services/pending_items.py` |
| 清单怎么分级 | 每项带 `importance`（**P0~P6** 七档，判据是「不问就没法开工」）；`extract` **先按等级稳定排序、再分配 id**，故 `p-1` 恒为最要紧那条。**等级没有兜底**——缺失或非法（如 `P7`）的条目**直接丢弃**并记 `pending.bad_importance` 告警，前端两处入口同口径 | `services/pending_items.py` + `services/api.ts` / `services/documentService.ts` |
| 流式不漏标记 | 有状态逐 chunk 抑制器：块外态尾留「哨兵长度 − 1」字符应对边界切割，块内整段丢弃，流结束仍在块内则什么都不输出 | `services/pending_items.py::PendingBlockSuppressor` |
| 清单怎么到前端 | 三路：`done.data.pending_items`（实时）、`snapshot`（刷新重连，迁移 v3 加列）、`GET .../documents`（打开方案即拿到） | `job_service`（两路快照）+ `api/solutions.py` |
| 清单长什么样给用户看 | 只读面板（不解析、不重算、不编辑），序号用位置下标而非后端 `id`；挂在**正文之上**、默认只铺开前 2 条（折叠只改「铺开多少」，不动总数与排序），标题旁是按**实际出现的等级**渲染的彩色计数芯片 | `components/ui/PendingItemsPanel.vue` |
| 澄清怎么聊 | 新增对话档案 `doc-clarification`（占位符 `{document}` / `{pending_items}`），与需求澄清**共用同一段提问规则常量**；未注册档案名 400、**不静默回退默认人设** | `services/prompts.py` + `api/conversation.py` |
| 澄清怎么改写 | `clarify` 任务（复用 job 框架）：只改涉及章节、其余**逐字保留**、末尾附**重算后**的清单；按目标产物注入产出规范 | `services/document_service.py::apply_clarification_stream` |
| 改写落哪一份 | 由请求体的 `doc_kind` 决定（缺它既不知道写哪、也不知道注入哪套规范 → 400）；回执的技能包身份与落库目标**共用** `_target_product_kind()` | `services/job_service.py` |
| 就地改写与下游 | `optimize` / `clarify` **不作废下游**（它们动的是同一份文档的局部）；只有整篇重生成才删下游——这条以前默认为「总是重生成」，会静默删掉提示词 | `job_service._regenerates_whole_document` |
| 澄清对话存哪 | 四条线程（需求 / PRD / 接口 / 提示词）共用**同一个 `messages` 数组**（`ChatMessage.stage`），对话页只渲染当前标签那一条。`chatTab` 存**用户意图**、`activeChatTab` 是实际显示的那条——文档未生成时归落到第一条可用线程，但意图**不回写**（ADR-027 / 028） | `stores/workflow.ts` + `SessionDataV2` |
| 各线程的会话 id | 每条线程在后端**各有一条会话**，id 按线程存在 `conversationIds`（`Record<ChatStage, string>`）；`stage` 旧值 `'document'` 归一为 `'prd-clarify'`，过期快照按 `clarifyTarget` 把 `clarifyMessages` / `clarifyConversationId` 重分发进四条线程 | `utils/workflow.ts::normalizeSessionSnapshot` |
| 澄清阶段怎么摆 | 四条线程都在 `chatting` 阶段里，用子标签栏常驻展示（`ui/ChatThreadTabs.vue`，未就绪的置灰并给理由）——**`clarifying` 这个状态已删除**。步骤归属**按当前线程现算**，否则澄清接口文档时进度条会跳回 PRD 那格 | `utils/workflow.ts` + `stores/workflow.ts` |
| 澄清入口放哪 | 三份文档的审阅页共用**同一个入口**「和 AI 确认剩下的 N 个问题」，落点都是对话页对应的子标签、都在**底栏次操作组**；与「返回」落点相同时它**顶替**「返回」（并列会出现两个都通向对话页的名字近似的按钮），落点不同时（快捷流程 / 两个导入入口没有需求对话）由 `keepBackButton` 让两者并存；只在真有待确认项时出现 | `components/stages/ReviewStage.vue` |
| 线程怎么作废 | 整篇重生成一份产物时，作废**它自己与下游**的线程（PRD → 三份，接口 → 接口 + 提示词，提示词 → 自己）；**就地改写不作废**。旧线程聊的是上一版的清单，留着会把结论写进新文档 | `composables/useDocumentPipeline.ts::dropThreadsFor` + `stores/workflow.ts::dropChatThread` |
| 会话回收 | **单点登录**：新登录在一条事务里「先删后插」吊销该用户全部旧会话（ADR-024），零 schema 变更、中间件零改动 | `repositories/auth_repo.py::replace_for_user` |
| 未填题目怎么交代 | 已答项之后汇总一行「以下题目用户未填写：…」——否则模型分不清「问过但留空」与「没这道题」 | `conversation_service.format_form_data` |

## 文档版本管理落地点（2026-10-16）

版本链兑现（ADR-029）+ 抽屉例外（ADR-030）。正文从「单表一行」变成「一条只增不改的历史」：

| 议题 | 落定做法 | 位置 |
| --- | --- | --- |
| 正文住在哪 | **`document_versions`**（`UNIQUE(document_id, version_no)`，`document_id` 外键 `ON DELETE CASCADE`）；`documents` 退化为「身份 + 当前版本指针」（`current_version_id`，`ON DELETE SET NULL`）。**正文只有一个家** | `core/migrations.py` v4 + `repositories/document_repo.py` |
| 版本何时产生 | 整篇生成**追加新版本**；`optimize` / `clarify` / 用户手改**就地改当前版本**（不新增条目）；checkpoint / restore **追加新版本**。判定与「是否删下游」共用 `_regenerates_whole_document`，收敛成一个 `append_version` 标志（两个语义恒等） | `job_service._write_document` + `document_store.commit_generated` |
| 恢复怎么做 | 读源版本 → **追加一条 `source='restore'` 的新版本**，**不拨指针**：历史只增不改，恢复这个动作本身也留痕 | `document_store.restore` |
| 清单 / 回执跟着版本吗 | 跟着。它们是版本行上的**显式列**（不塞进 `metadata` 口袋），恢复某一版时**清单一起还原**，不会还原出一份与正文对不上的旧清单 | `document_versions` 列 + `document_store._to_version_detail` |
| 迁移三步 | v4 建表 + 加指针 → v5 逐行容错回填成 `version_no=1, source='migrated'`（先插入空指针的文档行 → 插版本 → 回填指针，环形外键决定的顺序）→ v6 删 `documents` 的四个正文列（**单向门**） | `core/migrations.py` |
| 为什么用原生删列 | `PRAGMA foreign_keys` **在事务内是空操作**，而迁移运行器把每步包在 `BEGIN ... COMMIT` 里 → 「关外键 + 重建表」那套在步骤内部关不掉外键；四列都是普通列，原生 `ALTER TABLE ... DROP COLUMN` 直接可用 | `core/migrations.py::_drop_documents_content_columns` |
| 对外结构变了吗 | **没变**。`DocumentItem` 仍返回 `kind / content / pending_items / last_run_summary / updated_at`，读文档的调用方（路由、前端 store）**一行未改**；版本是一组**新增**能力 | `document_store._to_item` |
| 版本路由挂哪 | **solution 作用域**：`GET .../documents/{kind}/versions`、`.../versions/{version_id}`、`POST .../{kind}/checkpoint`、`POST .../{kind}/restore`。草稿里的 `/api/documents/{kind}/...` 没法在 SQL 层过滤归属（版本链只能经两级 JOIN 判归属），会破坏「越权不可区分」的口径 | `api/documents.py` |
| 版本不存在怎么办 | **复用 `SOLUTION_NOT_FOUND`（404）**，不新增错误码（ADR-012）；「方案不可见」是 404、「还没有任何版本」是 200 + 空列表 | `api/documents.py` |
| 前端状态放哪 | 另开 `stores/documentVersions.ts`（**不挂 `documents`**——后者已背 800ms 防抖 + 单飞 + `changeEpoch`，再叠一套状态会互相干扰）；归属直接读 `documents.solutionId`，不另存一份 | `stores/documentVersions.ts` |
| 写动作之后必须做什么 | 先 `flushPendingSave`（否则 `loadAll` 会把未落库的旧草稿盖在恢复结果之上，用户以为「恢复没生效」），成功后**重新拉文档 + 重拉版本列表**（否则编辑器里还是旧正文） | `stores/documentVersions.ts::runMutation` |
| 抽屉怎么摆 | 右侧**可收起浮层**：默认收起、`fixed`、以 `--shell-action-bar-h` 作下边距**不遮底栏**、不挤中部布局（正文仍 768px 居中）；入口在底栏内容行。**ADR-015 的有意例外**：抽屉里只放**内容操作**，流程动作（生成下一份 / 完成 / 返回）永远只在底栏 | `components/ui/VersionDrawer.vue` + `ADR-030` |
| 怎么预览某一版 | 点条目即预览：抽屉**就地加宽**到 640px 显示该版只读 Markdown + 元信息（第几版 / 来源 / 时间 / 字数 / 备注），带「返回列表」与「恢复此版本」。**不在主区域开新阶段**——那要把 `ViewState` / 进度条 / 底栏都改一遍，而这件事不需要它们参与（ADR-030 补记）。详情按需单取（`fetchVersion`），不随列表下发 | `stores/documentVersions.ts::preview` + `utils/markdown.ts` |
| 怎么删某一版 | 每条**历史**版本自带「删除」，**二次确认**（先展开「删除这一版？」再确认）。**当前版本不给删**——它是这份文档正在用的正文，换版本请用「恢复」（追加而非删除）；后端回 400 `INVALID_REQUEST` 并在 `detail` 里说明。守卫写在仓储 SQL 的 `WHERE` 里（`v.id != d.current_version_id`），不是「先查后删」 | `repositories/document_repo.py::delete_version` + `api/documents.py` |
| 删除后要不要重拉当前文档 | **不要**。删的是历史、当前正文一点没变，只重拉版本列表；多拉一次当前文档只会平白闪一下编辑器（与「存为新版本 / 恢复」相反——那两者改变当前版本，必须重拉） | `stores/documentVersions.ts::remove` |
| 本版不做什么 | **两版 diff**（差异渲染）不做——版本链落地后它不再是「依赖未完成」，但「回看 + 恢复」已经能回答用户的问题 | `ARCHITECTURE.md` §2 本期不做 |

## 入口页与四条入口落地点（2026-10-16）

入口从「三张平铺卡片」改成「两组四叶子」，并把缺的第四条补成真流程（ADR-031）：

| 议题 | 落定做法 | 位置 |
| --- | --- | --- |
| 为什么分两组 | 分组的两轴是**起点**而不是功能：`从零创建 PRD`（自己把需求说清楚）与 `导入已有素材`（材料已经有了）。先答「我属于哪种情况」比先答「我要哪个功能」快；而**四个叶子仍同屏可见**——分组加的是语义层级，不是多一屏要点的关卡 | `components/stages/EntryStage.vue` + `ADR-031` |
| 四个叶子各自是什么 | `完整流程`（`structured`，表单 + 对话 + 摘要 Diff + 三份文档）、`快捷流程`（`prd-quick`，一段话 → PRD）、`我有 PRD`（`prompt-debug`，贴 PRD → 提示词套件）、`我有 PRD + 接口文档`（`prd-and-api-paste`，贴两份 → 提示词套件） | `types/index.ts::EntryMode` |
| 模式影响什么 | 只有三件事：输入从哪来、进度条显示哪几步（`MODE_STEP_IDS`）、「完成」需要哪几份文档（`REQUIRED_DOCS`）。两个导入入口的产物都只有提示词，因此那两项各多一行**同值**数据 | `utils/workflow.ts` |
| 第二份材料怎么进模型 | `JobCreateRequest` 新增**可选** `api_docs_content` → `job_service` 的 `prompts` 分支透传 → `generate_prompts_stream` 多收一个参数 → 模板里的 `{api_docs_content}`。留空时填 `NO_API_DOCS_NOTE`（一句「本次未提供」），**不是留空**——那一节在模板里始终存在，留空会让模型以为漏读了、转头自己编一份接口契约 | `api/schemas.py` + `services/document_service.py` + `services/prompts.py` |
| 模板放哪（两份必须同步） | 技能包版 `skills/prompts-generator/references/prompts-guide.md`（首选，`skill.yaml` 里 `inputs.optional` 同步声明）与 `prompts.py::PROMPTS_GENERATION_PROMPT`（兜底）。两份的**占位符集合是一份跨文件契约**：少一个不是「那一节空着」而是 `KeyError` | `skills/prompts-generator/**` + `services/prompts.py` |
| 贴进去的材料会变成产物吗 | **不会**。两个导入入口的粘贴内容只作为本次生成的输入存在，不写 `documents`——那是「本工具生成的产物」的位置，混进去会让进度、完成条件与下载列表全部失真 | `composables/useDocumentPipeline.ts::handleGeneratePromptsFromPair` |
| 键名与文案不一致怎么办 | `prompt-debug` 这个名字**不改**（快照里存着它，改名要配一套兼容旧值的解析），对外说法改为「我有 PRD」；不一致由该类型的 docstring 与 ADR-031 备查 | `types/index.ts::EntryMode` |
| 两份材料存哪 | 分开存（`pastePairInput`），**不共用** `debugPrdText`：两条入口是两个页面各自的草稿，合用一个字段会让「在这页贴了、退回入口、又从另一页进去」看到上一页的内容 | `stores/workflow.ts` + `SessionDataV2` |
| 校验口径 | **不对称且明写**：PRD 必填（空则主操作禁用 + 缺失提示常驻在字段下方），接口文档选填，页面上直说「留空即按『只有 PRD』生成，与上一条入口效果一致」——不让用户猜那个框能不能空着 | `components/stages/PastePairStage.vue` |

## 方案列表的两根时间轴落地点（2026-10-16）

用户在列表里看到的「多久没动」，过去由 `solutions.updated_at` 决定——而它是**最后一次写库**，
切一下进度条也会刷新它。这一批把「内容变更」单独记成一根轴（ADR-032）：

| 议题 | 落定做法 | 位置 |
| --- | --- | --- |
| 为什么需要第二根轴 | 快照里同时住着**内容**（摘要 / 对话 / 粘贴材料 / 三份产物）与**相位**（`viewState` / `chatTab` / `activeJobId`）。相位的变化频率高一个数量级，于是列表时间永远是「刚刚」、排序把「刚点开看过的那份」顶到最前——而作品一个字都没变 | `ADR-032` |
| 两根轴各答什么 | `updated_at` = 「这份记录最近一次被写」（诊断）；`content_updated_at` = 「这份**东西**最近一次真的变了」（给人看）。**列表的时间显示与排序都用后者**，前者照旧写、照旧在接口里 | `repositories/solution_repo.py` |
| 「哪些改动算内容」 | 用户输入（表单 / 对话 / 粘贴的两份材料）＋ 三份产物的生成、AI 优化、手改、版本恢复与删除。**切阶段、翻看、切对话标签都不算**——这个口径是产品决定，写在这里备查 | `ADR-032` §Decision 6 |
| 谁判定这次是不是内容变更 | **前端**，随载荷提交 `content_changed`（**缺省为假**）。后端不解析 `data`，分不出「翻了一页」与「改了内容」；让它去猜只能靠解析或 diff，两条路都把前端契约焊死在后端 | `api/schemas.py::SolutionSaveRequest` |
| 前端怎么算出来 | 两个来源分开记数、在 `solutions` 里合并：`workflow.contentTick`（快照里的内容字段）＋ `documents.contentWriteTick`（正文写入）。**分开是为了不成环**——`workflow` 已依赖 `documents`，反向再依赖就循环了 | `stores/workflow.ts` + `stores/documents.ts` + `stores/solutions.ts` |
| 为什么正文要单独记数 | 三份产物正文住在后端 `documents` 表里，**不经过快照**，快照那条计数完全看不到它们。少了它，「生成完 PRD，列表时间还停在填表单那一刻」会成为常态 | `stores/documents.ts::markContentWritten` |
| 报告点在哪（三处） | ① 生成 / 优化 / 澄清收尾（`finishDoc`）；② 手改落库成功（`documents.saveNow`）；③ 版本抽屉的存为新版本 / 恢复 / 删历史版本。**都在写成功之后**，失败不报 | `composables/useDocumentPipeline.ts` + `stores/documents.ts` + `stores/documentVersions.ts` |
| 基准什么时候推进 | **只在保存成功之后**。失败也推进的话，那次变更会被算作「已经记过」，下一次保存报 `content_changed=false`——时间轴静默丢掉一次内容变更 | `stores/solutions.ts::saveNow` |
| 打开方案会不会误报 | 不会。`restoreTick` 那条守卫在跳过保存的同时把两个基准一起对齐——否则读完一份旧方案，之后任何一次保存都会声称「内容变了」 | `stores/solutions.ts` 的 watch |
| 迁移怎么回填 | v7 加列并回填成 `updated_at`（**不是迁移时刻**）：旧行没有依据可考，取「最后一次写库」保住它们原有的相对顺序；全部并列会让列表看起来像被重排过 | `core/migrations.py` v7 |
| 排序改在哪 | Repository 的列表查询改 `ORDER BY content_updated_at DESC, id DESC`，并建 `(user_id, content_updated_at DESC)` 复合索引。排序落在 SQL 里，因此刷新、换机器都成立，不再依赖前端的原位刷新 | `repositories/solution_repo.py::list_by_owner` |
| 显示回落 | 侧栏那行时间取 `content_updated_at`，**为空串时回落到 `updated_at`**——「新前端配旧后端」这种混布下至少还显示得出时间 | `components/SolutionSidebar.vue` |
