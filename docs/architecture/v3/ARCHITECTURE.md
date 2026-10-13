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
| **A. 生成运行时（Job）** | 优化二 + R1-T2 / T4 | 一次生成 = 一个 Job，后台执行与浏览器连接解耦；SSE 订阅 + 刷新重连；已完成章节可续 |
| **B. 观测与 Token 预算** | 优化一 + R1-T5 | LLM 调用包装层：每步 step / tokens / duration；`request_id` 串联；调用前估算并做预算管控 |
| **C. 文档生命周期** | 优化三 | `documents` + `document_versions` 两条后端自有表；三份产物各有独立版本链；Job 完成落版本 |
| **D. Skill 知识层统一** | 优化五 + R2-T6 | `config/skills.yaml` 注册表 + 三个 skill 包；三条产线共用一条注入管线；注入内容可追溯 |
| **E. 交付质量** | 优化六 | 澄清收口判断、`quality_gate` 结构校验、需求基线确认与下游追溯、交付包 |

### 本期不做（明确推迟，不是遗漏）

| 项 | 推迟理由 |
| --- | --- |
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
   A 运行时            B 观测/预算         A 章节续传          C 文档版本
 status / phase      step / tokens       completed_sections   Job 完成写
 draft_content       duration_ms         （按节不重跑）       current 版本
 job_bus 广播        request_id                              metadata 落盘
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
  → Job 完成：写 current 文档版本（含 run_summary / quality_gate 元数据）
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
| 订阅 | `GET /api/jobs/{id}/stream`；事件沿用现有 SSE 词汇（`status` / `chunk` / `part` / `stage` / `done` / `error`），对新订阅者先发一条 `snapshot` 补全已有内容 |
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
| 汇总 | 生成结束发 `run_summary`，含总 tokens、总耗时、调用次数、`injected_skills`；同时打一行 JSON 日志 |
| 预算（事前） | **两层上限**：物理顶来自模型注册表（按 provider 建模，不绑具体厂商），业务顶来自 `.env`（各 step 独立 cap，故意小于物理顶，让用户早点收束）；取两者较小值为有效预算 |
| 估算 | 发送前按字符启发式估算 messages 大小（不引入 tiktoken 依赖）；接近阈值 warn 并回传 `context_usage`，超阈值 reject 并给出中文说明，**不消耗这次调用** |
| 落点 | 计量累加只写在 `stream_reply` 这一处（v2 已把流式收敛到单一出口，这里正是回报） |

**物理顶必须按 provider 建模**：当前默认供应商是 ark、默认模型 `doubao-seed-2-1-turbo-260628`。
注册表按「provider + model」查上限，避免把某个厂商的窗口写死进代码。

### 4.3 文档生命周期

| 关注点 | 设计 |
| --- | --- |
| 表 | `documents`（`id / owner_id / solution_id / kind`，kind ∈ prd / api_docs / prompts）+ `document_versions`（`id / document_id / version_no / content / metadata / created_at`） |
| 版本链 | 三份文档各自从 v1 编号，**互不对齐**；每份文档有唯一 current 版本 |
| 升版规则 | 首次生成 → 建 v1 并设为 current；再次生成 → 版本号 +1；优化完成 / 手改 debounce 保存 → 只更新 current，不升版；用户点「保存为新版本」→ +1；「恢复此版本」→ 以该版正文新建下一版，历史不变 |
| 与快照的关系 | `solutions.data` 仍是**前端持有的不透明快照**（ADR-009 不变）；版本表是后端自有结构。Job 完成后**同步回写快照的三字段**作为兼容镜像，前端渐进迁移 |
| 生成期 | 进行中只写 Job 草稿，**不碰 current**；Job 成功才落版本 |

这里与 ADR-009 不冲突：ADR-009 约束的是「后端不解析快照 `data`」，而版本表是后端自己的表，
后端读写它天经地义；快照三字段只是给前端的镜像，保持单向同步（Job 完成 → 写版本 → 回写快照）。

### 4.4 Skill 知识层统一

现状已经比源文档描述的更进一步：`skills/prd-generator/` 已有 `skill.yaml` 契约，
`prd_skill.py` 已提供 `resolve_skill_dir(skill_name)` 与 `load_skill_artifact(skill_name, path)`（按名字解析、lru 缓存）。
**缺的是注册表和另外两个包**，不是「从硬编码里解放出来」。

| 关注点 | 设计 |
| --- | --- |
| 注册表 | `config/skills.yaml`：声明 prd / api_docs / prompts 各绑哪些 skill 包、加载哪些 references |
| 包 | 保留 `prd-generator`；新建 `api-docs-generator`（迁入 `core/api_doc_knowledge.json` 的接口规范）、`prompts-generator`（从 `prompts.py` 迁出提示词套件的生成规范） |
| 加载 | `skill_loader` 作为**唯一读 skill 的入口**；三条产线都走它拼 prompt |
| 可追溯 | `run_summary.injected_skills` 记录本次注入了哪些规范文件 |

**关于 RAG 闸门（与源文档分歧最大的一处）**：源文档要求「删除 RAG、删掉前端确认页」。
本项目不这么做——闸门 2 是 ADR-011 明确的设计决策，它的价值是**让用户知道模型参考了什么**（可审计），
真正的摩擦来自「必须点确认才能继续」。裁决如下：

- 接口规范收进 `skills/api-docs-generator`，注入方式统一走 loader（**采纳源文档的工程目标**）；
- 闸门从「阻塞式确认」降级为「只读展示」：生成前展示本次将注入哪些规范、命中了哪几条，用户可直接继续（**保留可见性，去掉阻塞**）；
- 本地词法检索（`core/api_doc_knowledge.json`，TF-IDF）保留作为检索实现，将来换向量只需替换检索函数。

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

**输入指纹（安全续传的闸门）**：`hash(需求文本 + 技能资源指纹 + 模板 + PRD_SECTIONS 定义)`，随 Job 一起落库。
指纹对不上说明用户改了需求或换了模板，旧中间态一律作废重来——绝不把改需求前的章节拼进新文档。

**failover 的切换边界**：只在「一次完整调用」的边界切换，切后**整节重跑**；绝不在流中途接着上一个供应商的半个输出继续（那样会产出半截章节）。
前端已有的「丢弃实时缓冲、等权威 `part` 覆盖」语义（`stage: rewriting` 就是这么干的）直接复用，不引入新机制。
触发条件是连接异常 / 超时 / 5xx，**不因内容质量切换**；同一 Job 内切换次数设上限，避免费用翻倍。最终供应商与切换次数写进 Job，成本计量才有归因。

**计量落点的实测风险**：`ark_llm.py` 现在只往 chunk 里塞 `response_metadata`（finish_reason），**没取 token 用量**——
第一步是改 ark 封装把 usage 带上；火山流式是否返回 usage 需实测确认（可能要开 `stream_options.include_usage`），
openai / anthropic 的 LangChain 集成在最后一帧自带 `usage_metadata`。累加只写在 `stream_reply` 这一处（见 4.2）。

**测试策略（全部离线，CI 会拦）**：给 FakeLLM 加三种能力——抛连接异常（验 failover 切换与整节重跑）、返回 usage（验累加）、中途断流（验续传）；
再加不变量测试：业务层禁止直接 import 具体供应商 SDK。**不得破坏 FakeLLM 离线测试（CI 全程零 token）**。

### 4.7 沿用不变的部分

v3 建立在这些已有决策之上，不推翻它们：ADR-007/008（Cookie 与默认拒绝中间件）、ADR-009（快照不透明 + owner 强制）、
ADR-011（生成前双闸门）、ADR-012（错误契约 8 码）、ADR-013（流式编排单一出口）、ADR-014/015（主题令牌与底栏唯一落点）。
新增的 Job API 同样受默认拒绝中间件保护，错误统一带 `code`。

## 5. 前端设计

| 关注点 | 设计 |
| --- | --- |
| Job 订阅 | 新增 composable 管理 `jobId` 与订阅生命周期；刷新后从快照取回 `jobId` 重连；`generating-*` 阶段不再「降级回 review」 |
| 重连体验 | `running` → 已有内容整段显示、新字打字机；`completed` → 静态全文；`failed` → 保留 partial + 错误提示 |
| 观测 UI | 生成中显示阶段 Stepper 与已用时；完成后折叠展示「耗时 · 约 X tokens · 复制 request_id」；澄清过长给自然语言提示，**不展示 token 百分比** |
| 版本侧栏 | review 页增版本侧栏：列表 + 只读预览 + 「保存为新版本」+「恢复此版本」 |
| 质量报告 | review 页展示 `quality_gate` 结果；接口文档 / 提示词展示 Review 意见 |
| 入口 | **不改**：三入口选择器（`EntryStage`）在 v2 已落地，源文档「三 Tab 改 chooser」那篇针对的是旧状态 |
| 保存 | 沿用 1.5s 防抖；落库目标从「覆盖快照三字段」逐步改为「更新 current 版本」，镜像写回保持不变直到迁移完成 |
| 操作落点 | 所有新操作（保存为新版本、恢复、导出交付包）一律进 `AppActionBar`，不新增第二处操作区（ADR-015） |

## 6. 关键契约

**Job 事件（SSE）**：沿用现有词汇，新增 `snapshot`（重连时补发已有内容）与 `run_summary`（收尾汇总）；
`part` 仍是权威正文（每节定稿整段下发）；`error` 带 `code`。

**Job API**：`POST /api/jobs`（创建）、`GET /api/jobs/{id}`（状态）、`GET /api/jobs/{id}/stream`（订阅 / 重连）、
`POST /api/jobs/{id}/cancel`（取消，可选）。

**版本 API**：`GET /api/documents/{kind}/versions`、`GET /api/document-versions/{id}`（只读预览）、
`POST /api/documents/{kind}/checkpoint`（保存为新版本）、`POST /api/document-versions/{id}/restore`（恢复为新的一版）。

**run_summary**：`{ total_calls, prompt_tokens, completion_tokens, duration_ms, steps[], injected_skills[], provider, model }`。

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
| 版本表另建，而不是往 `solutions.data` 里塞 | ADR-009 要求后端不解析快照；塞进去等于为了版本管理破坏这条约束 |
| RAG 闸门降级为只读，而不是删除 | ADR-011 的可审计价值要保留；摩擦来自阻塞式确认，去掉阻塞即可 |
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
| 8 | 优化五：skill 路径写死、只认 `prd-generator` | 已有 `skill.yaml` + `resolve_skill_dir(name)` + `load_skill_artifact(name, path)` | **改**：基础设施已就位，缺的是注册表与另外两个包，工作量比源文档估的小 |
| 9 | 优化五：接口规范在 `backend/knowledge/api_docs/` | 实际是 `core/api_doc_knowledge.json`，TF-IDF 词法检索 | **改**（路径与实现均按实际） |
| 10 | 优化五：删除 RAG、删掉前端确认页 | 闸门 2 是 ADR-011 的核心决策，v2 才落地 | **否决下线**：改为降级为只读展示；统一注入管线的目标保留 |
| 11 | 优化六：四层质量门禁 | 方向正确；澄清收口与 Review 已有部分基础（闸门 1 摘要 Diff、Review Agent） | **采纳并去重**，不重复造已有的判断 |

另有一处与 `./roadmap.md` 的冲突需同步：roadmap 的 R1 写着「不做异步任务队列（当前同步流式体感更好）」，
而本版采纳 Job 化。**两者的分歧其实不在「要不要后台执行」，而在「会不会失去流式」**——
Job + 内存广播 + SSE 订阅保留真流式，所以应修订的是那条「不做」的措辞（排除轮询式异步），而不是放弃 Job。

## 9. 分期

⚠️ 版本分期的唯一真源是 **`./roadmap.md` 第九节**；下表已同步回那里（此处复述一份以便就近阅读）。
相对旧稿的主要调整：**观测与预算从 v3.1 提到 v3.0**，因为它与 Job 的 `run_summary` 是同一条链路上不可分割的两半。

| 版本 | 内容 | 依据 |
| --- | --- | --- |
| **v3.0** | A 生成运行时（Job + 重连 + 章节续传）+ B 观测与预算 + 供应商 failover | 先立骨架与仪表，再动其余 |
| **v3.1** | C 文档生命周期 + D Skill 注册表 + E 交付质量 + 技能资产加固（热重载 / fail-fast） | 依赖 v3.0 的 Job 完成事件与 run_summary |
| **v3.2** | Docker 化、生产加固（Cookie Secure / 邀请码轮换）、部署态隔离验证 | 与 A–E 正交，可随时做 |

顺序理由：**A 是地基**（其余三项都要读 Job 记录）；**B 是仪表**（动主链路前先能看见代价）；
C / D / E 都建立在「Job 完成」这个事件上；v3.2 与业务正交。

开工前仍需拍板的两件事见 `./README.md`（中间态落点、R1 内部顺序）。
