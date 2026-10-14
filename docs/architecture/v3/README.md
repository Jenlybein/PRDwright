# v3 · 放心用（进行中）

> **一句话**：长文生成中断可续、供应商挂了能切、花了多少 token 看得见，最后能一条命令部署。
>
> **下层**：`../v1/`（一次性流程原型）、`../v2/`（能用，已完成）
> 时间线真源：**`../evolution-truth.md`**（v1 / v2 的真实边界按 git 界碑 `1c65e94` 与 tag `v2.0.0` 确定）

## 本层文件

| 文件 | 内容 | 状态 |
| --- | --- | --- |
| `ARCHITECTURE.md` | **v3 架构文档**：四条目标 → 范围 → 生成 Job 骨架 → 后端 / 前端设计 → 契约 → 取舍 → **与源文档《完整开发流程》的差异裁决（第 8 节）** | 进行中（第 4.1 / 4.2 节已落地） |
| `roadmap.md` | R1 / R2 / R3 全景路线图（含 v1–v2 的历史盘点、债务清单、风险登记、**版本分期的唯一真源**） | 进行中 |

两份怎么看：**先看 `ARCHITECTURE.md` 了解 v3 要建成什么样**，再看 `roadmap.md` 排期；
Job 运行时（`ARCHITECTURE.md` 第 4.1 节）已随 R1-T0 落地，观测与事后计量（第 4.2 节的观测侧）已随 R1-T5 落地，
**下一步是供应商 failover（R1-T4）**，动手前重点读第 4.2 / 4.6 节（观测与预算、R1 实现细节），实施状态以 `roadmap.md` 第八节为准。

## 版本 ↔ 任务对照

同一套工作的两种视图：**任务编号用于日常排期，版本号用于对外收口。**

| 版本阶段 | 任务阶段 | 目标 |
| --- | --- | --- |
| v3.0 | R1 | 生成可靠性：生成 Job 中间态 + 成本计量 / 供应商 failover / 断点续传 |
| v3.1 | R2 | 技能资产加固（热重载 + fail-fast）、可观测性基线、RAG 资产登记 |
| v3.2 | R3 | Docker 化、生产加固（Cookie Secure / 邀请码轮换）、部署态隔离验证 |

## 已拍板的两件事（2026-10-14，随 R1-T0 落地）

| # | 当时的选项 | 决定 | 落地位置 |
| --- | --- | --- | --- |
| 1 | run 中间态落在哪里：① 新建独立 `generation_jobs` 表（推荐）；② `solutions` 表加后端自有列；③ 塞进前端快照 `data`（❌ 与 ADR-009 冲突） | **取 ①** | `1f2ad08`：`core/db.py` 建 `generation_jobs`，`repositories/generation_job_repo.py` 读写，快照保持不透明 |
| 2 | R1 内部顺序：成本计量 → failover → 续传（推荐），还是续传提前 | **取「先装仪表」**，并且在其之前先立**生成 Job 骨架**（T0） | `1f2ad08` 完成骨架；R1 余下按 T5 → T4 → T2 排，见 `roadmap.md` 第八节 |

论证过程见 `ARCHITECTURE.md` 第 2 / 3 / 4.1 / 9 节；这两条已不再是待决项，状态以 `roadmap.md` 第八节为准。

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

> 结构化日志与 `/health` 扩展归 R2-T7；其余（事后计量、`request_id` 贯通、事前预算）本期已全部交付。
