# v2 架构文档（2026-10-12 ~ 2026-10-14）

> 本文按 git 提交反推 v2 期**当时的架构设计**：v1 已经证明主链路跑得通，
> v2 要做的是把它变成**能长期使用、多人可用、出错可诊断**的产品。
> 收尾于 tag `v2.0.0`（`b78de90`）。时间线与方法见 `../evolution-truth.md`。
>
> 本文只回答「**v2 是什么**」；「**为什么这么选**」见 `./ADR.md`（ADR-001 ~ 015，文中按主题标注了对应编号）。
> 施工期的一次性设计单据收在 `./archive/`，不代表当前状态。

---

## 1. 目标

v1 验证了「需求 → 文档」这条链路，v2 围绕四条目标做产品化：

| # | 目标 | 要解决的问题 |
| --- | --- | --- |
| 1 | **生成质量** | 输出要贴模板、要能自审——靠技能包、分章节撰写审核改写、生成前的两道闸门 |
| 2 | **可留存** | 关掉浏览器不能丢——靠账号、方案快照持久化、owner 隔离 |
| 3 | **可维护** | 1305 行的单体不能再长——靠组件库、状态机外置、主题令牌、统一底栏 |
| 4 | **可守护** | 改坏了要有人拦——靠 CI、机器可读的错误契约、流式编排的单一实现 |

## 2. 范围

**本期做**：技能包驱动的 PRD 生成、分章节撰写 + 审核 + 改写、生成前双闸门（摘要确认 + 接口规范检索）、
三入口（完整流程 / 快捷入口 / 调试入口）、账号与默认拒绝鉴权、方案持久化与 owner 隔离、
前端解耦（组件库 / 状态机 / composables）、亮暗主题、底栏统一、CI、错误契约机器码化、流式编排收敛。

**本期不做（明确推迟到 v3，见 `../v3/`）**：长文生成的断点续传、多供应商 failover、
token 成本计量、技能资源热重载与启动校验、结构化日志与 `/health` 增强、容器化部署。

v2 的重心是**把产品做出来**；护栏是最后一天才补的，可靠性议题整体推迟到 v3——
这个顺序本身就是 v3 要把可靠性前置的理由。

## 3. 架构总览

```text
┌── 浏览器 ──────────────────────────────────────────────────────┐
│  AppV2.vue（只做分派）   router: /v2 · /v2/:id                  │
│  stores/  workflow（状态机 + 快照） solutions auth               │
│  composables/ useClarificationChat useDocumentPipeline useTheme │
│  components/ stages/(9)  form/  ui/(11 基础件)                  │
│  services/ api.ts（fetch 流式） errors.ts auth solutionService   │
└────────────────────────── /api ────────────────────────────────┘
                             │
┌── FastAPI ─────────────────┴────────────────────────────────────┐
│  中间件：AuthMiddleware（默认拒绝 + 白名单，纯 ASGI）→ CORS      │
│  api/       auth · conversation · config · solutions · deps      │
│  services/  conversation document prd_skill prd_section_writer   │
│             stream_reply stream_guard llm_factory ark_llm        │
│             auth_service solution_service llm_utils prompts      │
│  repositories/ auth_repo solution_repo（唯一写 SQL 的地方）      │
│  core/      config db errors question_config api_doc_knowledge   │
└─────────────────────────────────────────────────────────────────┘
  外部资产：skills/prd-generator/{skill.yaml, references/}
```

**依赖方向**：`api → services → repositories → db`，无反向 import；
SQL 只出现在 `repositories/`；`schemas.py` 集中契约。这条规则从 v1 延续并在 v2 被写死为 review 红线。

## 4. 后端设计

### 4.1 认证与接口可见性

- 凭据是**服务端不透明 token + HttpOnly Cookie**，不上 JWT：库里只存 token 的 SHA-256，
  密码用 `scrypt` + 独立 salt。
- 可见性靠**默认拒绝中间件**：白名单之外的接口自动受保护，新增接口无需逐个挂依赖。
  中间件写成纯 ASGI 类而不是 `BaseHTTPMiddleware`——后者会重新包装响应体，对 SSE 有风险。
- 注册开放但需邀请码；未登录不允许创建任务。

### 4.2 持久化

- SQLite 开 WAL + `busy_timeout`，四张表：`users` / `auth_sessions` / `invite_codes` / `solutions`。
- 方案采用**单表全量快照**：`solutions.data` 存整份 `SessionDataV2` 的 JSON 文本，
  **后端不解析其内容**；`title` / `mode` / `stage` 是冗余列，供列表页免反序列化展示。
- 所有查询强制带 `owner_id` 且不接受 `None`，越权一律返回 404（不泄露资源是否存在）。

### 4.3 生成链路

1. **`prd_skill`**：加载技能包（`skill.yaml` + `references/` 的模板、字段契约、写作规范），
   把结构化摘要或自由文本格式化成需求材料；资源缺失时降级到内置提示词。
2. **双闸门（生成前）**：
   - 闸门一 `sync-summary-from-conversation`：把对话结论回填成结构化摘要，前端做 Diff 确认后才回写；
   - 闸门二 `retrieve-api-docs-rag`：用本地词法检索（BM25 风格）从 `core/api_doc_knowledge.json`
     检索接口规范，用户勾选后作为生成依据。
   - 两者失败或返回非法 JSON 时，一律**返回原摘要的深拷贝**，绝不弄丢用户已填的需求。
3. **`prd_section_writer`**：按模板的 7 个一级章节串行生成，每章三步——
   **撰写 → 审核（返回 JSON 判定 pass/revise）→ 必要时改写**（改写轮数有上限）。
   后续章节带上「已定稿的前序章节」作为上下文，保证全文口径一致（如 6.1 引用 2.1 的功能编号）。
4. **`stream_reply`**：全项目唯一的流式编排出口（`astream_text` / `iter_reply_events` /
   `collect_reply` / `error_event`），`stream_guard` 只管截断检测。

### 4.4 错误契约

错误响应统一为 `{"detail": <中文原文>, "code": "<UPPER_SNAKE>"}`，8 个机器码集中在 `core/errors.py`；
**SSE 的 `error` 事件也带顶层 `code`**（v1 起错误就在流内表达，HTTP 状态码恒为 200）。
前端据此判断（`isApiError(error, 'SESSION_NOT_FOUND')`），不再匹配中文文案。

### 4.5 模型接入

延续 v1：三供应商抽象不变，`max_tokens` 分档，`ark` 走官方 SDK。v2 未引入 failover（推迟到 v3.0）。

## 5. 前端设计

| 关注点 | 设计 |
| --- | --- |
| 外壳 | `AppV2.vue` 只做分派；`router.ts` 只有 `/v2` 与 `/v2/:id` 两条；**阶段流转走状态机，不进 URL** |
| 状态 | `stores/workflow.ts`（阶段机 + 快照）、`stores/solutions.ts`（列表 + 1.5s 防抖保存）、`stores/auth.ts` |
| 复用逻辑 | `composables/`：`useClarificationChat`（澄清对话）、`useDocumentPipeline`（文档流水线）、`useTheme`（主题） |
| 组件（ADR-015） | `stages/` 9 个阶段组件、`form/` 表单编辑器、`ui/` 11 个基础件（按钮 / 卡片 / 对话框 / 图标 / Toast…）；其中 `AppDialog` / `AppToast` / `AppEmpty` / `AppInput` **已建未接线**（`AuthDialog` 仍手写 Teleport，表单输入仍自绘） |
| 主题（ADR-014） | 三层语义令牌：`@theme` → `@theme inline` → `:root` / `.dark`；组件只用语义令牌，`dark:` 只留给结构性判断、状态色 tint、模态遮罩三类例外（残留 101 处 / 19 文件，是已知的未完成收敛） |
| 操作落点（ADR-015） | `ui/AppActionBar.vue` 是全站唯一操作栏：内容行（文档工具 / 输入框）+ 流程行（永远存在），主操作永远最右且唯一；`DocumentReview.vue` 只渲染卡片、自带零控件，能力经 `defineExpose` 交给底栏 |
| 流式读取 | 原生 `fetch` + `ReadableStream`（axios 不支持流式读）；**正文以 `part` 事件为准**——每章定稿整段下发，因此预览与最终 `done.reply` 逐字同源 |
| 契约 | `types/index.ts` 是准绳，后端迁就前端字段名 |
| 错误 | `services/errors.ts` 独立成文件（`ApiError` / `isApiError`），避免被测试里的模块级 mock 替换 |

**保存策略**：严格「有内容了才建」——无 id 时 `POST` 后 `router.replace('/v2/{id}')`，有 id 时 `PUT` 整份覆盖；
`generating-*` 阶段不触发保存（避免把半截文档写进去）。

## 6. 关键契约

- **SSE 事件**：`status` / `chunk` / `part` / `stage` / `done` / `error`，`error` 带 `code`。
  `part` 的 `data.text` 是**可直接追加到文档末尾的权威文本**（含补写的标题行与章间空行）；
  `stage` 报告「写到第几章、在写还是在审」。
- **错误响应**：`{detail, code}`；`detail` 的措辞与结构不变，只是并列多出一个 `code`。
- **方案保存**：`{title, mode, stage, data}`，`data` 对后端不透明。
- **认证**：Cookie `prdwright_session`（HttpOnly）。

## 7. 设计取舍

| 取舍 | 理由 |
| --- | --- |
| 单表全量快照而非拆表 | 方案内容会持续变形，差集同步的复杂度远超整份覆盖；后端当不透明字符串存取 |
| 默认拒绝 + 白名单，而非逐接口挂依赖 | 新增接口天然受保护，漏挂依赖不再是安全隐患 |
| 纯 ASGI 中间件 | `BaseHTTPMiddleware` 会重包响应体，对 SSE 有风险 |
| 分章节串行 + 前序上下文 | 章节间有跨引用（6.1 引 2.1 编号、附录汇总全部待确认），并行会破坏一致性 |
| 定稿后整段下发 `part` | 让后端成为正文的唯一来源，前端不必复现补标题/补空行的加工 |
| 主题只用语义令牌（ADR-014） | 组件里写 `dark:` 分支会随组件数量爆炸，令牌层一处切换即可 |
| 底栏为唯一操作落点（ADR-015） | 操作分散在各处时，用户找不到「下一步该点哪」 |
| 错误带机器码 | 前端匹配中文文案是活 bug，文案一改判断就失效 |

## 8. 收尾状态

v2 共 25 个提交，120 files，+22776 / −2099（约为 v1 全部代码的 4 倍，是一次重写而非增补）。
收尾提交 `b78de90`，标注为 tag `v2.0.0`；护栏状态：CI 双 job 已跑绿，后端 117 例 / 前端 222 例，全程离线零 token。
