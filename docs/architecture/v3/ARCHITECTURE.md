# v3 架构文档（2026-10-14 起）

> 本文写法与 `../v1/ARCHITECTURE.md`、`../v2/ARCHITECTURE.md` 一致：**目标 → 范围 → 设计 → 契约 → 取舍**，
> 按「开发前已定好目标、按目标走开发」正向叙述。
>
> v3 的起点是外部文档《完整开发流程》里的**优化一 ~ 优化六**。那六篇描述的是另一条分支上的代码状态，
> 本文**以仓库实际为准**逐条裁定后成文，裁决明细见第 8 节（不要跳过）。
>
> 排期真源是 `./roadmap.md`；v3.0（R1）的实现细节见第 4.6 节。

## 1. 目标

v2 已经做到「能用」：三入口 → 表单 → 澄清 → 双闸门 → PRD / 接口文档 / 提示词 → 方案留存 → 有 CI 兜底。
v3 要解决的是「**敢不敢真的用它干活**」，拆成四条可以分别验收的目标：

| # | 目标 | 一句话判据 |
| --- | --- | --- |
| G1 | **中断不丢** | 生成途中刷新、断网、关标签页，回来还能接上，已完成的部分不重跑、不重复计费 |
| G2 | **过程可见** | 一次生成能回答：调了几次模型、每步多少 token、耗时多久、用了哪些团队规范、失败在哪一步 |
| G3 | **结果留得住** | 三份文档不再是一个会被覆盖的字符串，而是有版本链的文档对象，能回看、能留档 |
| G4 | **敢交付** | 生成前知道需求是否收口，生成后有结构校验与质量报告，交付是一包而不是三个散文件 |

四条不是并列堆叠，而是**同一件事的四个切面**：G1 管过程、G2 管过程的可观测、G3 管结果、G4 管结果的合格判断。

## 2. 范围

### 本期做（in scope）

| 工作线 | 来源 | 内容 |
| --- | --- | --- |
| **A. 生成运行时（Job）** | 优化二 + R1-T2 / T4 | 一次生成 = 一个 Job，后台执行与浏览器连接解耦；SSE 订阅 + 刷新重连；已完成章节可续；**单次调用的超时与有限重试自愈（T4 的本期形态）** |
| **B. 观测与 Token 预算** | 优化一 + R1-T5 | LLM 调用包装层：每步 step / tokens / duration；`request_id` 串联；调用前估算并做预算管控 |
| **C. 文档生命周期** | 优化三 | `documents` 成为三份产物的**所有权归属地**（单表 + 迁移，ADR-021 / 022），Job 成功收尾唯一落稿；**待确认清单**作为生成的第一类产物随正文同批写入（ADR-023），每条带 **P0~P6** 重要性等级（等级缺失或非法的条目直接丢弃）；清单的出口是**澄清工作流**（对话 + 就地改写，ADR-025），其中**四条澄清线程（需求 / PRD / 接口 / 提示词）收在同一个对话页**、用子标签栏切换（ADR-027 / 028）。版本链本期明确推迟，见 4.3 末节 |
| **D. Skill 知识层统一** | 优化五 + R2-T6 | **技能注册表（目录即注册，ADR-019）** + 三个技能包；三条产线共用一条注入管线；注入内容可追溯 |
| **E. 交付质量** | 优化六 | 澄清收口判断、`quality_gate` 结构校验、需求基线确认与下游追溯、交付包 |

### 本期不做（明确推迟，不是遗漏）

| 项 | 推迟理由 |
| --- | --- |
| 运行时自动供应商 failover | 三家密钥齐备，仍裁决为**不自动切换**、保留手动冷切换（ADR-016）；单次调用的可靠性改由「显式超时 + 自管有限重试」承接 |
| **R1-T2 章节续传（跳过已完成节）** | 2026-10-15 按实测推迟：用户实测「生成中刷新不丢章节」是 T0 的效果，T2 针对的「任务本身中断」日常尚未发生。中间态仍逐节落库（`generation_jobs.completed_sections`），届时它只差「跳过已完成节」这一段接线，不涉及结构调整 |
| 多实例 / 分布式 Worker（Celery + Redis） | 单进程 `asyncio` 任务已够；Job 表与 API 形状按「将来可换执行层」设计 |
| 真向量 RAG 与知识库入库 | 现有词法检索服务于「检索团队规范」这一固定场景，换成向量的收益不抵成本 |
| 两版文档 diff 对比 | 依赖版本链先落地；本版只做「回看与恢复」，不做差异渲染 |
| 定稿 / 审核工作流、多人协作 | 产品定位是单机需求工作台，不是团队协作 ALM |
| 计费与配额 | 先做到「看得见」，不做「限制与结算」 |
| 多租户、角色权限、Skill 市场 | 超出当前使用规模 |

这些推迟是**范围决策**：v3 要把复杂度集中在「一次生成可靠、可归因、留得下」上。

## 3. 架构总览

**核心判断：六项优化与 R1 的三项任务，底层缺的是同一个东西——「一次生成过程的持久化记录」。**
因此 v3 不把它们当六个独立功能做，而是先立一个共同骨架：

```text
                        generation_jobs（一张表，唯一的过程真源）
                                      │
        ┌─────────────────┬───────────┴────────┬──────────────────┐
        │                 │                    │                  │
   A 运行时            B 观测/预算         A 章节续传          C 文档落稿
 status / phase      step / tokens       completed_sections   Job 成功写
 draft_content       duration_ms         （按节不重跑）       documents 单表
 job_bus 广播        request_id                              + 清单 + 回执
        │                 │                    │                  │
        └──────── SSE 订阅 / 刷新重连 ────────┴──────────────────┘
                                      │
                              前端 AppV2 + composables
```

```text
用户点「生成 PRD」
  → POST /api/jobs（建 Job，立刻返回 jobId）
  → 后台 asyncio task 顺序跑 Writer → Review → Rewrite
       └─ 每一步经「LLM 包装层」：估预算 → 调用 → 记 tokens/耗时
  → 每节定稿：写 completed_sections（续传依据）+ job_bus 广播
  → 前端 GET /api/jobs/{id}/stream 订阅：先收 snapshot，再收 live chunk
  → 刷新：同一 jobId 重连，已有内容整段显示，新字继续打字机
  → Job 完成：把正文 + 待确认清单 + run_summary 同批写进 documents（失败则不写）
```

```text
生成完之后的下一步（清单的出口）

三份文档的审阅页（同一个入口：和 AI 确认剩下的 N 个问题）
  → 展示只读清单（来自 documents.pending_items，每条带 P0~P6 等级）
  → 点它：进 chatting 阶段并切到那份文档的子标签
    （doc-clarification 档案；后端为这条线程另开一条会话，回复追加进同一个 messages）
  → 用户点「按澄清结果改写」：clarify 任务 → 只改涉及章节、逐字保留其余、重算清单

对话页：四条澄清线程常驻、用子标签栏切换（ADR-028）
  需求澄清 ｜ PRD 澄清 ｜ 接口澄清 ｜ 提示词澄清
  → 消息区只渲染当前标签那一条线程（同一个 messages 数组按 stage 过滤）
  → 底栏按线程给动作：需求澄清是「生成 PRD」，三条文档线程是「按澄清结果改写」

三条文档线程的收尾都写同一个 documents 行（ADR-021 的同一个写入口）
```

**为什么是这个形状**：Job 表把「过程」从内存里搬到磁盘上，A 的重连、B 的计量、续传的进度、C 的版本来源
就都从同一处取值，不必各建一套中间态。这是 v3 全部工作的地基，也是它必须第一个做的原因。

## 4. 后端设计

### 4.1 生成 Job 运行时

| 关注点 | 设计 |
| --- | --- |
| 落库 | 新增 `generation_jobs` 表：`id / owner_id / solution_id / kind / status / phase / draft_content / completed_sections / usage / error_code / created_at / updated_at` |
| 执行 | 同进程 `asyncio.create_task`，复用现有 `document_service` 的 generator，**不改 LLM pipeline** |
| 广播 | 内存事件总线 `job_bus`：chunk / phase 推给当前订阅者；连接断开只 unsubscribe，**不 cancel 任务** |
| 订阅 | `GET /api/jobs/{id}/stream`；事件沿用现有 SSE 词汇（`status` / `chunk` / `part` / `stage` / `done` / `error`），对新订阅者先发一条 `snapshot` 补全已有内容。SSE 帧的拼装统一在 `api/sse.py`（`event:` + `data:` 两行同写，与旧通道同一口径） |
| 重连 | 前端重连带同一 `jobId`；`running` → 整段显示已有内容 + 新字打字机；`completed` → 静态全文；`failed` → 保留 partial + 错误码 |
| 归属 | 强制 `owner_id`，越权返回 404（与 `solutions` 一致） |

**Job 粒度按「用户点的一次按钮」划分**，不按 pipeline 每一步拆：生成 PRD 是 1 个 Job（内含 writing / reviewing / rewriting），
接口文档、提示词、文档优化各 1 个。理由：用户心智里「我点了生成」就是一件事，拆细了只会让重连语义复杂。

**不引入 Celery / Redis**：单 FastAPI 进程即可部署，执行层将来可换，Job 表与 API 形状不变。

### 4.2 观测与 Token 预算

观测与预算**共用一条 LLM 调用包装层**，挂在 `get_llm()` 外面——业务层调用方式不变：

| 关注点 | 设计 |
| --- | --- |
| 观测（事后） | 每次 HTTP 请求一个 `request_id`（ASGI 中间件注入）；每次 LLM 调用打 `step` 标签（`prd_writer` / `prd_review` / `chat_continue` …）；记录 input / output tokens、duration_ms |
| 汇总 | 生成结束发 `run_summary`，含总 tokens、总耗时、调用次数、`failed_calls`、技能身份 `injected_skills`（R2-T6 起为 `SkillRef{name, version, fingerprint}`）。**哪些任务会填由技能包自己声明**（R2-T9 起）：注册表里该产物有技能包就有值；没有、或技能包不完整则为空列表 |
| 日志 | 结构化输出（`LOG_FORMAT` 取 `text` 人读 / `json` 采集），**每条自动带 `request_id`**——业务代码只写 `logger.info(...)`，加字段只发生在格式化器一处；`extra=` 挂的业务字段自动带出 |
| 体检 | `/health` 三项分开报：数据库连通性、供应商配置就位、**技能资产**（R2-T9 起按注册表逐个报技能包身份与其服务的产物，并汇总「元数据损坏 / 资源缺失 / 多包抢同一产物」三类问题）；`status` 取 `ok` / `degraded` |
| 预算（事前） | **两层上限**：物理顶来自模型注册表（按 provider 建模，不绑具体厂商），业务顶来自 `.env`（各 step 独立 cap，故意小于物理顶，让用户早点收束）；取两者较小值为有效预算 |
| 估算 | 发送前按字符启发式估算 messages 大小（不引入 tiktoken 依赖）；接近阈值 warn 并回传 `context_usage`，超阈值 reject 并给出中文说明，**不消耗这次调用** |
| 落点 | 计量累加只写在 `stream_reply` 这一处（v2 已把流式收敛到单一出口，这里正是回报） |

**物理顶必须按 provider 建模**：当前默认供应商是 ark、默认模型 `doubao-seed-2-1-turbo-260628`。
注册表按「provider + model」查上限，避免把某个厂商的窗口写死进代码。

**落地状态（2026-10-15）**：上表已**整体落地**——观测侧与预算侧随 R1-T5，日志与体检随 R2-T7。

- **观测侧**：`request_id` 由纯 ASGI 中间件（`api/deps.py::RequestIdMiddleware`）
  经 `core/request_context.py` 的 `ContextVar` 贯通到业务层；`step` 标签由「任务类型 + 阶段」派生
  （`{kind}:{phase}`，`phase` 来自既有 `stage` 事件），无需在各个调用点手写；token / duration_ms
  的累加**只写在 `stream_reply.astream_text` 一处**；`run_summary` 由 `core/contracts.py::RunSummary`
  定形，**事件与 `generation_jobs.usage` 同源**（同一份 JSON），终态任务重连会补发该事件。
  方舟侧已开 `stream_options.include_usage` 并映射到统一 `usage_metadata`（实现细节见 4.6）。
- **预算侧**：物理顶来自 `core/model_registry`（按 provider + model 两级登记，未登记的型号退到
  provider 默认值，并可用 `LLM_CONTEXT_WINDOW` 覆盖），业务顶来自 `.env`（`LLM_BUDGET_DEFAULT_CAP`
  与按 step 覆盖的 `LLM_BUDGET_CAPS`），**取两者较小值**为有效预算；发送前用字符启发式估算
  （不引 tiktoken，误差方向刻意偏高估），超顶即抛 `BudgetExceededError`——**这次调用根本不发出**，
  错误码复用 `INVALID_REQUEST` 而不新增码；逼近上限只记一条告警，随 `run_summary.warnings` 回传。
  检查点同样唯一（`astream_text` 开头），与事后计量互为表里。
- **日志侧（R2-T7）**：`core/logging_setup.py` 在 `lifespan` 里配置一次——给 root 装本项目 handler，
  并把 uvicorn 已装的 handler 一并换上同一套格式。`request_id` 由**格式化器从上下文变量现取**，
  所以业务代码那一行 `logger.info(...)` 一行都不用改，将来加字段（如 `user_id`）也只改这一处；
  调用方用 `extra=` 挂的业务字段按「排除标准属性」的规则自动带出，不必回来登记。
  访问日志记在 `RequestIdMiddleware` 里（它已是最外层），因此**被认证拦下的 401 同样留痕**——
  排查「用户说打不开」时，第一个要确认的正是「请求到底有没有到过后端」。
  `uvicorn.access` 的级别抬到 WARNING：同一次请求出两行（一行有号、一行没号）只会让人多看一眼。
- **体检侧（R2-T7）**：`api/health.py` 分三项报「数据库 / 供应商 / 技能资产」，`status` 取 `ok` 或
  `degraded`，**恒返回 200**——技能资产有缺时服务仍可降级运行，用状态码表达失败会让编排器
  踢掉还能用的容器。供应商一项**只校验配置就位、不发真实调用**（探针会高频触发，按次烧额度
  不合适），`detail` 里如实写明这一点，不假装验过。不报「备用供应商」是因为自动 failover
  已随 ADR-016 取消，预警没有了对象。
  技能资产一项**不含任何写死的技能包名单**（R2-T9 起）：报出哪些包、各自服务什么产物，全部
  来自扫描结果，新加技能包不必回来改体检。

### 4.3 文档生命周期

**目标**：三份产物不再是「会被覆盖的三个字符串」，而是**后端拥有的一份东西**——什么时候产生、
由谁写、写完之后长什么样，只在一个地方决定（ADR-021）。

#### 本期落地：单表所有权 + 迁移

| 关注点 | 设计 |
| --- | --- |
| 表 | **`documents` 单表**：`owner_id / solution_id / kind / content / pending_items / last_run_summary / content_sha / updated_at`，`(solution_id, kind)` 唯一。kind ∈ prd / api_docs / prompts |
| 归属 | 仍由 `solutions` 决定；仓储的每个查询经 `JOIN solutions` 在 **SQL 层**过滤 `owner_id`，越权与不存在一样 404（沿用 ADR-009 口径） |
| 写入口 | **唯一**：job 成功收尾时 `job_service._write_document` → `document_store.commit_generated`，正文 / 待确认清单 / 用量回执**同批替换**。失败的任务不写——半截输出不该顶掉一份完整文档 |
| 前端通道 | 只剩两条：`GET /api/solutions/{id}/documents`（读）与 `PUT .../documents/{kind}`（用户手改）。手改只动正文，**不碰**清单与回执 |
| 生成期 | 只写 `generation_jobs.draft_content`（Job 草稿），**不碰 `documents`**；成功才落稿。刷新期间的正文来自 Job 快照，不是从文档表读 |
| 下游失效 | 整篇重生成上游会让下游失效（`DOWNSTREAM_KINDS`，删 + 写同一事务）；`optimize` / `clarify` 是**就地改写同一份**，不牵动下游——判定由 `_regenerates_whole_document` 显式传入，不再默认按「重生成」处理 |
| 迁移 | 加表 / 加列走 `core/migrations.py` 的有序幂等清单（`PRAGMA user_version`，ADR-022），`init_schema` 只建基线表 |
| 与快照的关系 | `solutions.data` 仍是**前端持有的不透明快照**（ADR-009 不变）；`SessionDataV2` 退役三个 `*Content` 与三个 `*RunSummary`，旧快照里残留的**被忽略**（不做兼容读取——那等于长期维护两份真源） |

#### 待确认清单（生成的第一类产物）

清单与正文**来自同一次模型调用、同一次收尾写入**，因此结构上不存在「换了正文却忘了重算清单」
（ADR-023 详述）。落地形态：

| 关注点 | 设计 |
| --- | --- |
| 形态 | 模型在正文末尾附 HTML 注释标记块（`<!-- PENDING:BEGIN -->` … `<!-- PENDING:END -->`），块内是 JSON 数组；收尾时剥离、解析、由后端分配 `id` |
| 分级 | 每项带 `importance`（`P0` … `P6` 七档，`P0` 最要紧，判据是「不问就没法开工」）。`extract` **先按等级稳定排序、再分配 id**，因此 `p-1` 一定是最要紧那条；**等级没有兜底**：缺失或非法（如 `P7`）的条目**直接丢弃**并记 `pending.bad_importance` 告警，前端两处入口同口径（ADR-027 + 补记） |
| 为什么是 HTML 注释 | 即使抑制逻辑漏了，Markdown 渲染里也**不可见**，不会把半截 JSON 当正文闪现 |
| 单一实现 | `services/pending_items.py`（`extract` / `render_spec` / `PendingBlockSuppressor`），四条路径（三条生成 + 澄清改写）共用 |
| 流式 | 逐 chunk 抑制：块外态尾留「哨兵长度 − 1」字符应对边界切割；块内整段丢弃；流结束仍在块内则什么都不输出 |
| 降级 | 无块 / JSON 非法 / 未闭合 → 正文不受影响、生成不失败，清单为空并记一条 warning |
| 三路可达前端 | `done.data.pending_items`（实时）、`snapshot`（刷新重连，迁移 v3 给 `generation_jobs` 加列）、`GET .../documents`（打开方案即拿到） |
| 展示 | 只读——前端不解析、不重算、不编辑（`ui/PendingItemsPanel.vue`）。面板挂在**正文之上**（先看到还差什么，再看正文本身），默认只铺开前 2 条、其余折起来可展开；折叠只改「铺开多少」，不改总数与排序（标题旁是按**实际出现的等级**渲染的彩色计数芯片） |

#### 澄清工作流（清单的出口）

清单不是终点：用户可以就它和 AI 逐条聊清楚，再**按结论就地改写**那一份文档（ADR-025）。
改写只动涉及的章节，其余逐字保留，末尾附带**重算后**的清单。

**对话这一步只有一条路径：一个对话页 + 四条线程**（ADR-028）。四条线程（需求 / PRD / 接口 / 提示词）
共用同一个 `messages` 数组、每条消息带 `stage`（`ChatStage`）；**消息区只渲染当前标签那一条线程**，
子标签栏**常驻**、未就绪的置灰——标签本身就是地图，藏起来用户就不知道后面还有哪些能聊。

| 线程 | 什么时候可用 | 底栏主操作 |
| --- | --- | --- |
| 需求澄清（`requirement`） | 一直可用 | 生成 PRD（另加「重新开始 │ 改需求」） |
| PRD / 接口 / 提示词澄清 | 对应那份文档已生成 | 按澄清结果改写 |

`chatTab` 记的是**用户意图**，`activeChatTab` 才是实际显示的那条：某份文档还没生成时它的标签置灰、
显示归落到第一条可用线程，但意图**不回写**，文档生成好之后用户还能回到自己上次那条线程。
回审阅页的落点是**顶部进度条**（澄清线程的底栏刻意不并列一个「返回审阅」——两者落点相同、名字近似，
并排会让用户不知道点哪个）；某份文档被**整篇重生成**时，它这条线程与它**下游**的线程一起清掉
（旧线程聊的是上一版的待确认项，留着会把针对旧文档的结论写进新文档），而**就地改写不作废任何线程**。

四条线程共用同一个 `doc-clarification` 对话档案、同一套提问规则（`CLARIFICATION_QUESTION_RULE`），
也共用同一个 `clarify` 任务与同一个收尾写入口；每条线程在后端**各有一条会话**（`conversationIds`
按 `ChatStage` 取）。提问按 P0 → P6 逐级进行，每进入一个等级 AI 先播报该等级——这条规则写在共享常量
里而不是开场白里，否则模型聊过一轮就忘了还要播报。

**闸门①不受影响**：摘要回填用的 `conversationHistory()` 只取 `stage === 'requirement'` 的 turns；
即便不靠这层过滤，闸门①本身也只比对结构化的 `RequirementsSummary`，根本不读 `messages`。

#### 本期明确推迟：版本链

原设计里的 `document_versions`（每份文档一条版本链、可回看与恢复）**本期不做**，理由是它解决的问题
（「改坏了想回到上一版」）在日常使用中尚未出现，而它要新增的是一整套「版本号 + current 指针 +
恢复语义」的状态。本期把**结构位置留好了**：`documents` 是后端自有表，将来加版本链是往上叠一张表
（`document_id / version_no / content / metadata`）并把 `content` 换成「指向 current 版本」，
不涉及今天这份设计里任何一条约束的推翻。**它是下一版的需求来源**：
在文档已经归后端所有、且每次改写都留下清单与回执之后，「哪一版被谁改过」才成为一个答得清的问题——
今天连「改了几次」都还没有地方记。

### 4.4 Skill 知识层统一

**第一步（R2-T9-1）已落地**：产物归属与必需资源清单不再写死在 Python 里，改由技能包在自己的
`skill.yaml` 中声明，程序扫描建索引（ADR-019）：

| 声明 | 含义 |
| --- | --- |
| `applies_to: [prd]` | 本技能包服务于哪些产物（取值 = 任务类型 `kind`） |
| `references[].key` / `.path` | 生成管线要注入的资源，以及它对外暴露的语义键 |

于是**「加一个技能包」等于「加一个目录」**：`services/skill_registry.py` 扫
`<skills 根目录>/*/skill.yaml` 建索引，`job_service` 不再有任何种类判断，`/health` 与启动日志
自动跟着变。`prd_skill.py` 随之瘦身——只留 PRD 专属的材料格式化与提示词拼接，而定位 / 加载 /
指纹 / 自检这些**所有技能包共用的能力**整体搬进注册表。

| 关注点 | 设计 |
| --- | --- |
| 注册表 | **不加中心文件**：技能包自述 + 扫描建索引（ADR-019）。中心登记表意味着「加目录 + 改文件」两处同步，而那正是要消灭的东西 |
| 包 | 保留 `prd-generator`；新建 `api-docs-generator`（迁入 `core/api_doc_knowledge.json` 的接口规范）、`prompts-generator`（从 `prompts.py` 迁出提示词套件的生成规范）——各自由自己的 `applies_to` 认领产物 |
| 加载 | `skill_registry` 是**唯一读技能资产的入口**；三条产线都经它取资源拼 prompt |
| 缓存 | 资源内容与元数据都按文件戳（修改时间 + 大小）失效；**资源存在性判定不进缓存**——删文件不会改 `skill.yaml`，缓存它就会出现「文件已经没了还在报一切正常」 |
| 可追溯 | `run_summary.injected_skills` 记录 `SkillRef{name, version, fingerprint}`（ADR-017），指纹随内容变化，改一个字即变 |

**关于 RAG 闸门（与源文档分歧最大的一处）**：源文档要求「删除 RAG、删掉前端确认页」。R2 立项时
曾裁决保留闸门 2、只把阻塞式确认降级为只读展示；**该裁决已被 ADR-020 改判为整条移除**，因为
原来的取舍把两件不同的事混在了一起：

- 闸门 1（摘要 Diff）确认的是**用户输入**——用户确实需要看见自己的话被理解成了什么，因此不动；
- 闸门 2 注入的是**固定不变的团队规范**（14 条约 6000 字），不是每次都要重审的临时素材。
  对静态资产要求逐条勾选不是「可见性」而是多余的摩擦；真正的可见性诉求由回执上的技能身份承担，
  它更完整且不阻塞。

**全量注入取代「挑前 5 条」**：规范总量远低于预算上限（12 万），而检索挑漏一条时，那次生成的
接口文档就少了对齐依据、**且从输出上看不出来**。检索解决的本来是「候选集远超上下文预算」，
这里条件不成立——将来规范真长到装不下，正确做法是重新引入检索，而不是怀念一条只挑 5 条、
还不告诉人挑了哪 5 条的实现。

落地顺序见 `./roadmap.md` 的 R2-T9：T9-2 迁规范、T9-3 换管线并删检索、T9-4 拆前端门控、T9-5 收口。

### 4.5 交付质量

| 关注点 | 设计 |
| --- | --- |
| 生成前 | 澄清收口状态可见；未收口可以生成，但按钮给出警告 + 一次确认 |
| 生成后 | `quality_gate` 服务按产物类型做**确定性**结构校验（章节、表格、范围等硬指标），结果写进 `document_versions.metadata.quality_gate` |
| 门禁力度 | PRD 维持 Writer → Review（1 次）→ 最多 Rewrite（1 次）**不叠第二轮 LLM Review**；Rewrite 后用结构校验兜底，仍不通过时照常交付并展示已知 issues |
| 接口文档 / 提示词 | **Review only，不自动 Rewrite**——它们是 PRD 的派生物，自动改稿容易偏离已确认基线 |
| 追溯 | 需求基线确认后，`derived_from` 记录上下游版本关系；PRD 更新后下游展示「可能已过期」提示 |
| 交付 | ZIP 交付包 + `manifest.json`（版本、生成参数、质量报告、所用 skill） |

### 4.6 R1 实现细节

以下四条是生成可靠性的关键实现约定（原 `r1-generation-reliability.md` 折入，该文件已删）：

**输入指纹（安全续传的闸门）**：`hash(需求文本 + 技能资源指纹 + 模板 + PRD_SECTIONS 定义)`，随 Job 一起落库
（`services/job_fingerprint.py::compute_input_fingerprint`，T0 落地时已接入 `POST /api/jobs`）。
指纹对不上说明用户改了需求或换了模板，旧中间态一律作废重来——绝不把改需求前的章节拼进新文档。

**单次调用的自愈边界**：调用出问题时的补救发生在**同一个供应商内**，不跨供应商切换（ADR-016）。
一次调用生命周期上的三个守卫按时间排布：调用前做预算检查（见 4.2）→ 帧间隔超时
（`LLM_TIMEOUT`）→ 整次调用的墙钟硬顶（`LLM_TOTAL_TIMEOUT`）。后两者必须分开：
httpx 的 read timeout 是**帧间隔**而不是总时长，一个每 59 秒吐一个字的模型能把单次调用无限拖下去，
所以总时长上限得由 `astream_text` 用 `asyncio.timeout` 自己包一层。

重试只在**首字之前**发生（尚未 yield 过任何增量），判定用白名单——连接类异常、超时、5xx 才重试，
`AppError`（含预算超顶）、4xx 与内容性质的 `OutputTruncatedError` 一律不重试。选这个边界有两条独立理由：
**安全**（增量一旦交付给前端就撤不回，中途重来会重现整章重复的老症状）与**便宜**
（首字前失败通常还没产出 output tokens，重来几乎不花钱）。

重试由本层自管而非交给 SDK：三家 SDK 默认各自重试 2 次（ark / openai / anthropic 均是），
它们的判定发生在「拿到响应」层、边界本身是安全的，但废弃的尝试对本层不可见，成本归因会系统性偏乐观
——这与「判定与记账同源」相冲突。故把 SDK 的 `max_retries` 显式设为 0，统一由
`astream_text` 计数并记入 `failed_calls`（与 `total_calls` 分开，后者只计成功）。

**计量落点的实测风险（已核实，非推断）**：`ark_llm.py` 只往 chunk 里塞 `response_metadata`（finish_reason），
**没取 token 用量**。读仓库已装的 `arkruntime` 类型定义确认：方舟的 `stream_options` 支持 `include_usage`
（定义原话「include token usage info in the final stream chunk」）与 `chunk_include_usage`，
用量挂在**流式响应顶层**的 `usage`（`prompt_tokens` / `completion_tokens` / `total_tokens`）。
**这里有一个必踩的坑**：开启 `include_usage` 后，携带用量的那一帧 `choices` 为空，
而现有循环开头就是 `if not chunk.choices: continue`——不先读 `usage` 就会把整帧吞掉，
表现为「usage 永远拿不到」。openai / anthropic 的 LangChain 集成在最后一帧自带 `usage_metadata`
（`input_tokens` / `output_tokens` / `total_tokens`），方舟侧映射到同一字段即可，三个供应商归一。
累加只写在 `stream_reply` 这一处（见 4.2）。

**测试策略（全部离线，CI 会拦）**：假模型 `tests/fakes.py::ScriptedLLM` 备好几种能力——首块之前抛错
（验重试窗口）与吐过之后抛错（验「不再重试」的守卫）、末块 `usage_metadata` 返回用量（验累加）、
先吐若干块再抛（验续传）；
再加不变量测试：业务层禁止直接 import 具体供应商 SDK。**不得破坏离线测试（CI 全程零 token）**。

**落地状态**：输入指纹的计算与落库、假模型的能力、**计量累加与 `run_summary` 汇总**均已落地
（计量随 R1-T5，累加点唯一在 `stream_reply.astream_text`，业务层禁止直连供应商 SDK 由 `tests/test_layering.py` 的不变量测试守卫）。
T4 已裁决为「同供应商自愈」（超时 + 自管重试 + 失败计量，见本节首条），
自动化方案排期与两项遗留（T2 章节续传、输入指纹的读取）见 `./roadmap.md` 第八节。

### 4.7 沿用不变的部分

v3 建立在这些已有决策之上，不推翻它们：ADR-007/008（Cookie 与默认拒绝中间件）、ADR-009（快照不透明 + owner 强制）、
ADR-011（生成前双闸门）、ADR-012（错误契约 8 码）、ADR-013（流式编排单一出口）、ADR-014/015（主题令牌与底栏唯一落点）。
新增的 Job API 同样受默认拒绝中间件保护，错误统一带 `code`。

分层方向（`api → services → repositories → core`）在 R1-T0 里补了两处具体落点：跨层契约
（`StreamEvent` / `QuestionConfig` / `QuestionsConfig`）从 `api/schemas.py` 移入 `core/contracts.py`，
`api/schemas.py` 保留同名 re-export，对外引用对象不变；并新增 `tests/test_layering.py`
（源码扫描禁止 `services/` / `repositories/` 反向 import `api/`，带检测器非空转自检）。

## 5. 前端设计

| 关注点 | 设计 |
| --- | --- |
| Job 订阅 | 新增 composable 管理 `jobId` 与订阅生命周期；刷新后从快照取回 `jobId` 重连；`generating-*` 阶段不再「降级回 review」 |
| 重连体验 | `running` → 已有内容整段显示、新字打字机；`completed` → 静态全文；`failed` → 保留 partial + 错误提示 |
| 观测 UI | 生成中显示阶段 Stepper 与已用时；完成后折叠展示「耗时 · 约 X tokens · 复制 request_id」；澄清过长给自然语言提示，**不展示 token 百分比** |
| 文档所有权 | 三份正文的工作副本在 `stores/documents.ts`（后端 `documents` 表在前端的镜像，防抖 800ms + 单飞，与 `stores/solutions.ts` 同一套记账）；刷新 / 打开方案时按方案 id 拉一次即得权威版本。**审阅页没有「保存」按钮**：手改正文由编辑框逐次上报、经 800ms 防抖自动落库（静默，仅失败闪一条红色提示），未落库的草稿在「打开方案前 / `beforeunload` / 组件卸载」三处补冲；底栏空出来的位置改为「下载」，导出的是**用户眼前那份草稿**（可能比落库版本新） |
| 待确认清单与澄清入口 | 清单面板在**正文之上**（每条带 P0~P6 等级徽标、按等级排序，默认只铺开前 2 条可展开，标题旁是按实际出现的等级渲染的彩色计数芯片）。三份文档共用**同一个入口**「和 AI 确认剩下的 N 个问题」，落点都是对话页对应的子标签；PRD 那条落在**底栏次操作组**并在出现时**顶替**「返回」按钮——两者落点都是对话页，并排会给用户两个近乎同名的选择——且只在完整流程（`entryMode === 'structured'`）下出现（PRD 快捷 / 提示词调试没有对话可回） |
| 对话页的子标签栏 | `ui/ChatThreadTabs.vue`：四条线程**常驻**，未就绪的置灰并给出理由（`title`）；切换即切换消息区与底栏动作。`chatTab` 存的是用户意图、`activeChatTab` 是实际显示的那条（文档未生成时归落到第一条可用线程，**意图不回写**）。有请求在跑时全部禁用——切过去会看到一段与自己刚才操作无关的上下文，而流还在往原线程里写 |
| 版本侧栏 | **本期推迟**（随版本链一起）：review 页的列表 + 只读预览 + 「保存为新版本」+「恢复此版本」 |
| 质量报告 | review 页展示 `quality_gate` 结果；接口文档 / 提示词展示 Review 意见 |
| 入口 | **不改**：三入口选择器（`EntryStage`）在 v2 已落地，源文档「三 Tab 改 chooser」那篇针对的是旧状态 |
| 保存 | 沿用 1.5s 防抖；落库目标从「覆盖快照三字段」逐步改为「更新 current 版本」，镜像写回保持不变直到迁移完成 |
| 操作落点 | 所有新操作（保存为新版本、恢复、导出交付包）一律进 `AppActionBar`，不新增第二处操作区（ADR-015） |

## 6. 关键契约

**Job 事件（SSE）**：沿用现有词汇，新增 `snapshot`（重连时补发已有内容）与 `run_summary`（收尾汇总）；
`part` 仍是权威正文（每节定稿整段下发）；`error` 带 `code`。

**Job API**：`POST /api/jobs`（创建）、`GET /api/jobs/{id}`（状态）、`GET /api/jobs/{id}/stream`（订阅 / 重连）、
`POST /api/jobs/{id}/cancel`（取消，可选）。

**文档 API**：`GET /api/solutions/{id}/documents`（读三份产物：正文 + 清单 + 回执）、
`PUT /api/solutions/{id}/documents/{kind}`（用户手改，只动正文）。**写正文没有第三个入口**——
生成 / 优化 / 澄清改写都由 job 收尾写入（ADR-021）。

**澄清 API**：`POST /api/conversation/start-stream` / `continue-stream` 接受 `profile_key`
（点名场景，如 `doc-clarification`；留空取默认，未知取值 400）；改写走 `POST /api/jobs` 的
`kind=clarify` + `doc_kind`（外加 `document` 与 `history`）。

每条文档澄清线程都会在**需求澄清之后另开一条会话**，id 按线程存在 `conversationIds`
（`Record<ChatStage, string>`）里——后端是多条会话，前端是一条时间线（ADR-028）。`history` 一律按
`stage`（`requirement` / `prd-clarify` / `api-docs-clarify` / `prompts-clarify`）过滤后再提交：
摘要回填只取 `requirement`，文档澄清只取自己那条。`ChatMessage.stage` 缺省视为 `requirement`
（旧快照的消息都产生于这个字段出现之前，只可能是需求澄清轮次），旧值 `'document'` 在快照解析时
归一为 `'prd-clarify'`。

**版本 API**：`GET /api/documents/{kind}/versions`、`GET /api/document-versions/{id}`（只读预览）、
`POST /api/documents/{kind}/checkpoint`（保存为新版本）、`POST /api/document-versions/{id}/restore`（恢复为新的一版）。
**本期推迟**（随版本链一起，见 4.3）。

**run_summary**：`{ total_calls, failed_calls, prompt_tokens, completion_tokens, duration_ms, steps[], injected_skills[], warnings[], provider, model, request_id }`。
`injected_skills` 的元素是 `SkillRef{ name, version, fingerprint }`（ADR-017）。它是否为空由
**技能包的声明**决定（R2-T9）：该产物在注册表里有对应技能包、且它完整，就有值；否则为空数组，
意味着这次走的是内置兜底提示词——前端会把这件事说给用户听，而不是静默过去。

**quality_gate**：`{ passed, issues[], checked_at, rules_version }`，随版本 metadata 落盘。

**错误**：一律 `{detail, code}`，Job 失败时码落进 `generation_jobs.error_code`（ADR-012 的 8 个码复用，不新增）。

## 7. 设计取舍

| 取舍 | 理由 |
| --- | --- |
| Job 后台执行 + 内存广播，而不是「轮询式异步队列」 | 保留 v2 的真流式体感；同时让生成不再绑在浏览器连接上。轮询会让逐字反馈消失 |
| 同进程 `asyncio` 任务，而不是 Celery + Redis | 单实例部署足够；Job 表与 API 形状按可换执行层设计，将来升级不动上层 |
| 章节级续传，而不是 token 级续传 | LLM 无状态，token 级续传做不到；但「已完成章节不重跑」是真实收益，且现成的 `part` 事件与 `_join_prior` 就是为此准备的 |
| 观测与预算共用一条 LLM 包装层 | 一次调用同时拿到「事前估算」与「事后计量」，避免两处埋点漂移 |
| 物理顶按 provider + model 建模，不写死 DeepSeek 之类 | 当前默认是 ark；写死厂商会让换模型变成改代码 |
| 文档正文归后端单表，而不是继续留在前端快照里 | 「一份内容两个真源」没有裁决者；写入口分散在多条路径上，而「改的是哪一份」只有前端知道（ADR-021） |
| 版本链另建表、本期推迟，而不是往 `solutions.data` 里塞 | ADR-009 要求后端不解析快照；塞进去等于为了版本管理破坏这条约束。而它解决的问题（改坏了想回退）日常尚未出现——先落地「单表所有权」这一步，版本链往上叠即可 |
| 待确认清单由模型随正文一起产出，而不是前端从正文里解析 | 清单与正文同一次调用、同一次写入，因此不需要任何同步机制（ADR-023）；前端解析的做法把口径写在两端，措辞一改就静默失配 |
| 澄清做成「对话 + 就地改写」，而不是生成时一次问完或让用户直接改正文 | 生成时用户还没看到文档；直接改正文则丢掉了推理过程，且改完清单过期（ADR-025） |
| 澄清复用主对话线程（前端单线程 / 后端多会话），而不是继续用独立澄清栏 | 独立栏会让历史断裂——PRD 的问题正是从那份 PRD 里长出来的，最需要前面的上下文；而当初分开的理由（闸门①会读到文档答疑）经核实不成立：闸门①只比对结构化摘要，根本不读 `messages`。后端保持多条会话则让 `conversation_service` 一行都不用改（ADR-027） |
| 四条澄清线程收进**同一个对话页 + 子标签栏**，而不是让另两份继续住独立页、也不是把四条挤进一条时间线 | 两套实现意味着同一件事写两遍、刷新行为与入口位置还不一致；共挤一条时间线则「这段在聊 PRD 还是接口文档」无从分辨、底栏主操作也没法跟着线程切。标签常驻 + 未就绪置灰，本身就是地图——用户才知道后面还有哪些能聊（ADR-028） |
| 待确认项按 P0 ~ P6 分级并排序，而不是按出现顺序平铺 | 清单的意义就是「不问就没法开工」，理应按这个程度排序；`extract` 先排序再分配 id，因此 `p-1` 天然是最要紧那条，前端序号与提问顺序一致。**等级缺失或非法即丢弃**、不设兜底档——「拿不准就标 P1」会让大量条目挤在同一档，分级形同虚设（ADR-027 + 补记） |
| 会话用单点登录，而不是 revision 乐观锁 | 多端同时在线不是真实场景；为它维护版本号 + 冲突重试 + 冲突 UI，是拿复杂度换一个用不到的能力。代价（多设备互相踢下线）明确接受并登记（ADR-024） |
| RAG 闸门整条移除，而不是降级为只读 | 原裁决把「用户输入的确认」与「静态资产的注入」混为一谈；闸门 1 保留不动，闸门 2 注入的是固定不变的团队规范，不该每次要求逐条勾选（ADR-020） |
| 接口文档 / 提示词只 Review 不自动 Rewrite | 它们是 PRD 的派生物，自动改稿会偏离已确认基线 |
| 结构校验兜底，而不是再加一轮 LLM Review | 确定性规则不会「无输出让用户以为卡死」，也不额外增加半分钟等待 |
| 新操作一律进底栏 | ADR-015 的「唯一操作落点」不能因为功能变多就破例 |

## 8. 与源文档《完整开发流程》的差异裁决

源文档描述的是另一条分支上的代码状态（那里还是 `session_data`、`SessionList`、`/api/session/save`、RAG 确认页、
三 Tab 入口、DeepSeek）。下表逐条裁定，**本文以仓库实际为准**：

| # | 源文档说法 | 仓库实际 | 裁决 |
| --- | --- | --- | --- |
| 1 | 优化一：无 request_id / 无 token / 无耗时 | 属实（全仓 grep 零命中） | **采纳** |
| 2 | 优化一：物理顶先支持 DeepSeek v4-flash | 默认供应商是 ark，模型 `doubao-seed-2-1-turbo-260628` | **改**：注册表按 provider + model 建模，不绑厂商 |
| 3 | 优化二：快照 debounce 约 1 秒 | 实际 1.5s，保存主体是 `solutions` 表而非 session | **改**（细节，不影响方案） |
| 4 | 优化二：Job 表 + 内存广播 + 同进程任务 | 仓库无 Job，方案本身成立 | **采纳**，并与 R1 的 run 合并为同一张表 |
| 5 | 优化二：「流式续传行不通，因为 LLM 无状态」 | token 级续传确实做不到；但**章节级续传可以**，且 `part` 事件已为此准备 | **纠正**：两者解决不同问题——Job 管「连接断了」，续传管「重跑浪费」，不冲突 |
| 6 | 优化三：三字符串覆盖即丢失 | 属实（`prdContent` / `apiDocsContent` / `promptsContent`） | **采纳**，版本表另建以守住 ADR-009 |
| 7 | 优化四：三 Tab 改成路径 chooser | `EntryStage` + `isChoosingEntry` 已在 v2 落地；`entryMode` 取值是 `prd-quick` / `prompt-debug` | **整篇作废**（源文档里的 `structured` / `prd` / `prompts` 三值与本仓库不符） |
| 8 | 优化五：skill 路径写死、只认 `prd-generator` | `skill.yaml` 契约已在；R2-T9-1 起注册表就位，硬编码种类判断已消除 | **采纳并已完成第一步**：产物归属与资源清单改由技能包自述（ADR-019），**加技能包 = 加目录** |
| 9 | 优化五：接口规范在 `backend/knowledge/api_docs/` | 实际是 `core/api_doc_knowledge.json`，TF-IDF 词法检索 | **改**（路径与实现均按实际）；R2-T9-2 会把它迁成 `skills/api-docs-generator` 下的 md |
| 10 | 优化五：删除 RAG、删掉前端确认页 | 闸门 2 是 ADR-011 的核心决策，v2 才落地 | **改判为采纳**（ADR-020）：原裁决「降级为只读展示」把「用户输入的确认」与「静态资产的注入」混为一谈——闸门 1 保留不动，闸门 2 整条移除 |
| 11 | 优化六：四层质量门禁 | 方向正确；澄清收口与 Review 已有部分基础（闸门 1 摘要 Diff、Review Agent） | **采纳并去重**，不重复造已有的判断 |

另有一处与 `./roadmap.md` 的冲突需同步：roadmap 的 R1 写着「不做异步任务队列（当前同步流式体感更好）」，
而本版采纳 Job 化。**两者的分歧其实不在「要不要后台执行」，而在「会不会失去流式」**——
Job + 内存广播 + SSE 订阅保留真流式，所以应修订的是那条「不做」的措辞（排除轮询式异步），而不是放弃 Job。

## 9. 分期

⚠️ 版本分期的唯一真源是 **`./roadmap.md` 第九节**；下表已同步回那里（此处复述一份以便就近阅读）。
相对旧稿的主要调整：**观测与预算从 v3.1 提到 v3.0**，因为它与 Job 的 `run_summary` 是同一条链路上不可分割的两半。

| 版本 | 内容 | 依据 |
| --- | --- | --- |
| **v3.0** | A 生成运行时（Job + 重连 + **已随 T0 落地的刷新恢复**）+ B 观测与预算 + **C 单次调用自愈（超时 + 有限重试）**。~~章节续传~~ 按实测推迟，见下表下说明 | 先立骨架与仪表，再动其余；三者共用 `stream_reply` 同一个出口 |
| **v3.1** | **C 文档生命周期（单表所有权 + 迁移 + 待确认清单（P0~P6 分级）+ 澄清工作流，已完成；**四条澄清线程收进同一个对话页，ADR-027 / 028，已完成**；版本链推迟）** + D Skill 注册表 + E 交付质量 + **技能资产加固（热重载 / fail-fast / 回执可追溯，ADR-017，已完成）** + **可观测性基线（结构化日志 + `/health` 三项体检，ADR-018，已完成）** | 依赖 v3.0 的 Job 完成事件与 run_summary |
| **v3.2** | Docker 化、生产加固（Cookie Secure / 邀请码轮换）、部署态隔离验证 | 与 A–E 正交，可随时做 |

顺序理由：**A 是地基**（其余三项都要读 Job 记录）；**B 是仪表**（动主链路前先能看见代价）；
C / D / E 都建立在「Job 完成」这个事件上；v3.2 与业务正交。

> **「章节续传」为何从 v3.0 移出**：它不是做不了，而是做完暂时也用不上。用户实测「生成中途
> 刷新、章节一份不少」——那由 T0 承担（任务活在服务端，刷新只断 SSE 连线）；而续传真正针对的
> **任务本身被杀死**（进程重启、调用彻底失败）在日常使用中尚未出现，仅开发期会因改代码重启而
> 触发。中间态早已随 T0 逐节落库，届时把「跳过已完成节」接上即可，不必重做地基。

开工前需拍板的两件事（中间态落点、R1 内部顺序）**已拍板并随 R1-T0 落地**；
R1-T5 的计量落地点另见 `./README.md`「R1-T5 成本计量落地点」一节，实施状态以 `./roadmap.md` 第八节为准。
