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
`documents` 单表所有、待确认清单成为生成的第一类产物（按 P0~P6 分级）、四条澄清线程收进
同一个对话页、会话改单点登录；其中**版本链明确推迟**（第 4.3 节末）。
注意 R1-T2 断点续传已裁决延后、自动切换供应商已裁决不做（ADR-016），
动手前重点读第 4.2 / 4.3 / 4.4 / 4.6 节，实施状态以 `roadmap.md` 第八节为准。

## 版本 ↔ 任务对照

同一套工作的两种视图：**任务编号用于日常排期，版本号用于对外收口。**

| 版本阶段 | 任务阶段 | 目标 |
| --- | --- | --- |
| v3.0 | R1 | 生成可靠性：生成 Job 中间态 + 成本计量与预算 / **单次调用自愈**（超时 + 有限重试）/ 断点续传 |
| v3.1 | R2 | 技能资产加固（热重载 + fail-fast）✅、可观测性基线 ✅、**技能知识层统一 / RAG 下线 ✅**（原「RAG 资产登记」被它吸收） |
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
| 正文归谁 | **后端 `documents` 单表**为唯一真源，`(solution_id, kind)` 唯一；前端只有工作副本 | `repositories/document_repo.py` + `services/document_store.py` |
| 写正文的入口 | **唯一**：job 成功收尾，正文 / 清单 / 回执**同批替换**；失败不写 | `job_service._write_document` → `document_store.commit_generated` |
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
| 澄清入口放哪 | 三份文档的审阅页共用**同一个入口**「和 AI 确认剩下的 N 个问题」，落点都是对话页对应的子标签、都在**底栏次操作组**；与「返回」落点相同时它**顶替**「返回」（并列会出现两个都通向对话页的名字近似的按钮），落点不同时（PRD 快捷 / 提示词调试没有需求对话）由 `keepBackButton` 让两者并存；只在真有待确认项时出现 | `components/stages/ReviewStage.vue` |
| 线程怎么作废 | 整篇重生成一份产物时，作废**它自己与下游**的线程（PRD → 三份，接口 → 接口 + 提示词，提示词 → 自己）；**就地改写不作废**。旧线程聊的是上一版的清单，留着会把结论写进新文档 | `composables/useDocumentPipeline.ts::dropThreadsFor` + `stores/workflow.ts::dropChatThread` |
| 会话回收 | **单点登录**：新登录在一条事务里「先删后插」吊销该用户全部旧会话（ADR-024），零 schema 变更、中间件零改动 | `repositories/auth_repo.py::replace_for_user` |
| 未填题目怎么交代 | 已答项之后汇总一行「以下题目用户未填写：…」——否则模型分不清「问过但留空」与「没这道题」 | `conversation_service.format_form_data` |
