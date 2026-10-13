# v1 架构文档（2026-10-03 ~ 2026-10-10）

> 本文按 git 提交反推 v1 期**当时的架构设计**：先写目标与范围，再写怎么落地。
> v1 期没有留下任何架构文档（收尾提交 `1c65e94` 时 `README.md` 为空），
> 因此以下内容是从 `1c65e94` 的代码树与各提交信息还原的设计全貌。
> 时间线与方法见 `../evolution-truth.md`。

---

## 1. 目标

**验证一件事：LLM 能不能把一份需求问卷，变成可直接使用的 PRD、接口文档与提示词套件。**

围绕这个目标，v1 要交付的是一条**端到端跑得通的链路**，而不是一个可长期使用的产品：

1. 用户填一份需求问卷；
2. 系统与用户做一轮**澄清对话**，先对齐理解再追问关键点；
3. 澄清结束后**流式生成**三份文档（PRD → 接口文档 → 提示词套件），并支持按指令优化；
4. 全程 Markdown 实时渲染，随时可下载。

## 2. 范围

**本期做（in scope）**

| 项 | 说明 |
| --- | --- |
| 问卷配置驱动 | 题目写在 `core/question_config.json`，前端据此渲染，后端据此还原题目文案 |
| SSE 流式澄清对话 | 服务端维护会话历史，逐字推送模型回复 |
| 三份文档的流式生成 | PRD / 接口文档 / 提示词套件，外加文档优化 |
| 刷新恢复 | 整份会话快照存前端，`F5` 后回到原阶段 |
| 多供应商接入 | Anthropic / OpenAI / 火山方舟 三选一，支持自定义 API 地址 |

**本期不做（明确推迟，不是遗漏）**

账号与多用户、服务端持久化、文档存储与版本、多人协作、自动化测试与 CI、模板与写作规范体系。

这些推迟是**范围决策**：v1 要把复杂度集中在「主链路能不能跑通」这一个问题上，
其余每一项都会引入与验证目标无关的复杂度。它们构成了 v2 的全部目标（见 `../v2/ARCHITECTURE.md`）。

## 3. 架构总览

```text
┌── 浏览器 ────────────────────────────────────────────────┐
│  App.vue（唯一编排者，9 个 ViewState）                    │
│    ├ FormStep.vue      问卷渲染与校验                     │
│    ├ MessageList.vue   聊天气泡 + Markdown 渲染           │
│    ├ ChatInput.vue     输入框                             │
│    └ DocumentReview.vue 文档预览 / 编辑 / 下载            │
│  services/api.ts（axios + SSE 解析）  types/index.ts（契约）│
│  utils/{markdown,download}.ts                             │
│  localStorage: harnessprd:session（整份会话快照）          │
└─────────────── Vite 代理 /api ───────────────────────────┘
                          │
┌── FastAPI ──────────────┴────────────────────────────────┐
│  api/config.py        问卷配置等只读接口                  │
│  api/conversation.py  参数解析 + SSE 封装（共 9 条路由）   │
│        │                                                  │
│  services/conversation_service.py  问卷上下文 + 会话 + 澄清│
│  services/document_service.py      四份文档的流式生成      │
│  services/prompts.py               提示词模板集中放置      │
│        │                                                  │
│  services/llm_factory.py   三供应商构造                   │
│  services/ark_llm.py       火山方舟官方 SDK 封装           │
│  services/stream_guard.py  统一的截断检测                  │
│  core/config.py            环境变量 / .env                │
└──────────────────────────────────────────────────────────┘
```

**分层原则（v1 就确立、v2 延续）**：`api/` 只做参数解析与响应封装，业务逻辑一律在 `services/`，
提示词与问卷配置这类「内容」外部化到 `core/` 或 `services/prompts.py`，不散落在业务代码里。

## 4. 前端设计

| 关注点 | 设计 |
| --- | --- |
| 编排 | `App.vue` 是唯一的流程编排者（1305 行），阶段用 `ViewState` 表达：`form → chatting → generating-*/review-*`（三份文档各一对）→ `done` |
| 进度 | `types` 里的 `STEPS` 与 `STEP_OF_STATE` 把 9 个状态映射到 4 个步骤条节点 |
| 状态 | `ref` / `reactive` 直接持有；整份 `SessionData`（阶段、消息、会话 ID、问卷答案、三份文档）序列化进 `localStorage` 键 `harnessprd:session`，刷新整体恢复 |
| 契约 | `types/index.ts` 是前后端契约准绳：`QuestionsConfig`（base / advanced 两组）、`ChatMessage`、`SessionData`、`StreamEventPayload` |
| 请求 | `axios` 实例走 `/api` 同源代理；流式响应单独解析（`parseSseBlock` + `SSE_DONE` + `StreamHandlers` 回调） |
| 渲染 | `markdown-it` 渲染正文，`utils/download.ts` 负责导出 |

**为什么是单体 `App.vue`**：v1 的流程是严格线性的，全部状态天然属于「这一次会话」。
先让它待在一个文件里，能把跨阶段的状态传递看得最清楚；拆分留到流程被验证之后。

## 5. 后端设计

### 5.1 路由层

`api/conversation.py` 承担全部业务路由（9 条），`api/config.py` 提供问卷等只读配置。
路由层只做三件事：解析请求体、调用 service 拿到事件流、用 `_make_sse_generator` 包成 SSE 响应。

SSE 的三处工程细节（v1 就定下，沿用至今）：

- **事件双写**：每个事件同时输出 `event:` 与 `data:` 行，前端既能按类型监听，也能只解析 `data.type`；
- **末尾 `[DONE]`**：正常结束与异常结束都补发，前端收到即主动关连接；
- **响应头**：`Cache-Control: no-cache` 与 `X-Accel-Buffering: no`，避免 Nginx 缓冲把流式卡住。

### 5.2 会话服务

- 问卷答案经 `format_form_data()` 渲染成 Markdown，并按问卷配置把字段键还原成题目文案；
  配置读不到时退化成字段名——**答案绝不因为配置问题被静默丢弃**。
- 首轮开场白（`REQUIREMENTS_WRAPPER`）要求模型**先复述理解、再追问 3–5 个关键点，本轮不输出 PRD 正文**，
  把「澄清」与「生成」分成两个明确阶段。
- 角色名归一化（`_ROLE_ALIASES`）与结构化分块摊平（`_extract_text`）用于吸收各供应商的返回差异。
- 会话历史在**服务端内存中**；`conversation_id` 与前端回传的 `history` 至少提供一种，
  两者都给时以 `history` 为准（无状态模式）。

### 5.3 文档服务

四个流式方法：`generate_prd_stream` / `generate_api_docs_stream` / `generate_prompts_stream` / `optimize_document_stream`。
服务本身只做「加载模板 → 拼接变量 → 调用模型 → 包装成事件」，**提示词文本全部集中在 `prompts.py`**。
另有三个同步方法（`generate_document` / `get_document` / `update_document`）为兼容早期路由契约而保留，
约定在持久化落地时一并实现。

### 5.4 模型接入

- `llm_factory` 按 `DEFAULT_LLM_PROVIDER` 构造 Anthropic / OpenAI / 火山方舟之一，支持自定义 API 地址。
- **火山方舟走官方 SDK 而不是 OpenAI 兼容伪装**（`ark_llm`），并用 `response_metadata` 对齐 LangChain 的字段约定。
- 深度思考默认关闭：先吐一大段思考再出正文会造成「首字极慢 + 正文突然爆发」的观感。
- `max_tokens` 按文档类型分档；`stream_guard` 统一识别各供应商的截断标记（`length` / `max_tokens`），
  **截断即报错**，而不是把半截文档当成成功结果返回——长文档被静默截断是最难排查的失败。

## 6. 关键契约

**SSE 事件**（`StreamEvent`）：`type` ∈ `status` / `chunk` / `done` / `error`
（另预留 `tool_call` / `document`），携带 `content` 与 `data`。

**事件序列**：`status`（`data` 带 `conversation_id`，前端须立即记录）→ 若干 `chunk` → `done`（带完整 `reply`）；
链路中任何异常都转成 `error` 事件，随后补 `[DONE]`——**HTTP 状态码始终是 200**，错误在流内表达。

**接口清单**：`GET /questions`、`POST /start-stream`、`POST /continue-stream`、
`POST /generate-prd-stream`、`POST /generate-api-docs-stream`、`POST /generate-prompts-stream`、
`POST /optimize-document-stream`，加 `api/config.py` 的只读配置与 `/`、`/health`。

## 7. 设计取舍

| 取舍 | 理由 |
| --- | --- |
| 单体 `App.vue` 而非拆分组件树 | 流程严格线性，状态天然属于一次会话；先看清跨阶段传递，再谈拆分 |
| 会话在服务端内存、快照在前端 | 没有用户概念，也就不需要归属；先把「一次会话」跑通 |
| 全部走 SSE 而非请求-响应 | 长文本生成必须流式，否则用户面对几十秒白屏 |
| 问卷外部化为 JSON | 同一份配置同时驱动前端渲染与后端文案还原，改题目不用改代码 |
| 火山方舟直连官方 SDK | 兼容层会丢字段、行为不一致，宁可为它单独写一层薄封装 |
| 截断即报错 | 静默返回半截文档比报错更糟：用户会拿一份残缺的 PRD 去开会 |
| 提示词集中在 `prompts.py` | 提示词是内容不是逻辑，与业务代码混在一起就无法单独演进 |

## 8. 收尾状态

`1c65e94 "V1版本开发结束"`（2026-10-10）：17 个提交，44 files，+5891 / −470。
目标内的四项能力全部跑通，目标外的六项按计划未启动——**它们是 v2 的需求来源**。
