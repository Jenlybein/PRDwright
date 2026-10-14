/** 单个问题的配置结构 */
export interface QuestionConfig {
  /** 字段名称，作为提交答案时的 key */
  name: string
  /** 展示给用户的标题 */
  label: string
  /** 题型：单行文本 / 多行文本 / 下拉选择 / 单选 */
  type: 'text' | 'textarea' | 'select' | 'radio'
  /** 是否必填 */
  required?: boolean
  /** 选择类题型的选项列表 */
  options?: string[]
}

/** 问卷配置：分为基础问题与进阶问题两组 */
export interface QuestionsConfig {
  base_questions: QuestionConfig[]
  advanced_questions: QuestionConfig[]
}

/** 聊天消息，角色取值与后端 conversation_service 的 FrontendMessage 一致 */
export interface ChatMessage {
  /** 消息角色；`system` 消息不展示在聊天界面里 */
  role: 'system' | 'user' | 'assistant'
  /** 消息正文；assistant 的消息为 Markdown 源码 */
  content: string
  /** 是否正在流式生成中，用于显示光标 */
  streaming?: boolean
}

/**
 * 页面所处的阶段。
 * `generating-*` 表示文档正在生成中，`review-*` 表示文档已生成、待用户确认。
 * `summary-diff` / `rag-preview` 是两道**确认闸门**：前者让用户先确认「对话中补充的
 * 信息回填到摘要」的结果，后者让用户先确认「检索到的团队接口规范」；确认后才继续生成。
 *
 * 三个入口相关的阶段：
 * - `entry`：入口选择页（还没选走哪条入口）；
 * - `prd-quick`：PRD 快捷入口的录入页（一段话描述 → 直接生成 PRD）；
 * - `prompt-debug`：提示词调试入口的录入页（贴一份 PRD → 生成提示词套件）。
 */
export type ViewState =
  | 'entry'
  | 'form'
  | 'chatting'
  | 'summary-diff'
  | 'generating-prd'
  | 'review-prd'
  | 'rag-preview'
  | 'generating-api-docs'
  | 'review-api-docs'
  | 'generating-prompts'
  | 'review-prompts'
  | 'prd-quick'
  | 'prompt-debug'
  | 'done'

/**
 * 入口模式：三条链路的起点不同，但汇合到同一套「生成 → 审阅 → 下载」流程。
 *
 * - `structured`：完整流程——结构化表单 → AI 对话澄清 → PRD → 接口文档 → 提示词；
 * - `prd-quick`：只想快速拿到 PRD——一段话描述 + 产品名，跳过表单与对话；
 * - `prompt-debug`：手头已有 PRD——贴进来直接生成提示词套件，再用「AI 优化」反复迭代。
 *
 * 模式只决定「输入怎么来、进度条显示哪几步、需要哪几份文档才算完成」，
 * 不决定文档怎么写——写作规范始终来自 `skills/prd-generator`。
 */
export type EntryMode = 'structured' | 'prd-quick' | 'prompt-debug'

/**
 * 生成任务的文档种类（与后端 `generation_jobs.kind` 一一对应）。
 *
 * - `prd` / `api-docs` / `prompts`：三份文档的生成；
 * - `optimize`：对某一份已有文档的 AI 优化（就地流式，不改变所处阶段）。
 *
 * 注意命名口径：后端用下划线（`api_docs`），前端用连字符（`api-docs`），
 * 转换收口在 `services/api.ts`。
 */
export type DocKind = 'prd' | 'api-docs' | 'prompts' | 'optimize'

/** PRD 快捷入口的录入内容 */
export interface QuickPrdInput {
  /** 产品名称；为空时文档头退化为「未命名产品」 */
  productName: string
  /** 用户用自然语言写下的一段话需求 */
  requirements: string
}

/**
 * 一份完整会话的持久化快照，用于刷新 / 关闭页面后整体恢复。
 * 覆盖当前阶段、对话、会话 ID、问卷答案与三份文档正文。
 */
export interface SessionData {
  /** 当前所处阶段 */
  viewState: ViewState
  /** 已固化的聊天消息 */
  messages: ChatMessage[]
  /** 后端会话 ID（可为空，接续时退化无状态模式） */
  conversationId: string
  /** 问卷答案（字段名 → 答案文本） */
  formData: Record<string, string>
  /** PRD 正文 */
  prdContent: string
  /** 接口文档正文 */
  apiDocsContent: string
  /** 提示词套件正文 */
  promptsContent: string
}

/** 顶部进度条展示的步骤（生成中状态不单独占一步，而是归并到对应文档的确认步骤） */
export const STEPS = [
  { id: 'form', label: '描述产品' },
  { id: 'chatting', label: 'AI 对话' },
  { id: 'review-prd', label: 'PRD' },
  { id: 'review-api-docs', label: '接口文档' },
  { id: 'review-prompts', label: '提示词' },
] as const

/** 步骤 id 的联合类型，取自 STEPS */
export type StepId = (typeof STEPS)[number]['id']

/** 单个步骤（id + 展示名）；进度条按入口模式从 `STEPS` 里筛子集 */
export type Step = (typeof STEPS)[number]

/**
 * ViewState → 所属步骤，用于定位进度条当前位置。
 *
 * 注意：这里给的是**全局**步骤归属，进度条实际展示哪几步由
 * `utils/workflow.ts` 的 `stepsForMode()` 按入口模式裁剪——
 * 例如 PRD 快捷入口没有「描述产品 / AI 对话」两步，提示词调试入口只有「提示词」一步。
 */
export const STEP_OF_STATE: Record<ViewState, StepId> = {
  // 入口页还没选模式，先按完整流程展示，避免进度条出现空档
  entry: 'form',
  form: 'form',
  chatting: 'chatting',
  // 摘要确认发生在生成 PRD 之前，仍归属「AI 对话」这一步
  'summary-diff': 'chatting',
  'generating-prd': 'review-prd',
  'review-prd': 'review-prd',
  // 规范检索发生在生成接口文档之前，仍归属「PRD」这一步
  'rag-preview': 'review-prd',
  'generating-api-docs': 'review-api-docs',
  'review-api-docs': 'review-api-docs',
  'generating-prompts': 'review-prompts',
  'review-prompts': 'review-prompts',
  // 快捷入口的录入产物就是 PRD
  'prd-quick': 'review-prd',
  // 调试入口的录入产物就是提示词套件
  'prompt-debug': 'review-prompts',
  done: 'review-prompts',
}

// ============================================================================
// V2：结构化需求（与 skills/prd-generator/references/field-schema.json 对齐）
// ============================================================================

/** MVP 功能项：对应 schema 中 `mvp_features` 的对象数组项 */
export interface MvpFeature {
  /** 功能名称（必填） */
  name: string
  /** 功能描述：做什么、给谁用 */
  description?: string
  /** 对用户的价值 */
  user_value?: string
}

/** 页面结构项：对应 schema 中 `ui_pages` 的对象数组项 */
export interface UiPage {
  /** 页面名称（必填） */
  name: string
  /** 页面包含的模块 / 区块 */
  modules: string[]
  /** 备注 */
  notes?: string
}

/**
 * 结构化需求摘要：V2 结构化表单产出的标准 JSON。
 *
 * 前五个字段对应 schema 的 required，恒定出现；其余为可选字段，仅在填写了
 * 对应内容时才出现。schema 顶层 `additionalProperties: true`，允许携带
 * `key_interactions` / `llm_usage` / `non_functional` / `scope` 这些扩展字段。
 */
export interface RequirementsSummary {
  product_name: string
  product_goal: string
  target_users: string[]
  platform: string
  mvp_features: MvpFeature[]
  v2_features?: string[]
  ui_pages?: UiPage[]
  key_interactions?: string[]
  technical_constraints?: {
    auth?: string
    storage?: string
    frontend?: string
    backend?: string
    deployment?: string
  }
  llm_usage?: string
  non_functional?: {
    security?: string
    performance?: string
    availability?: string
  }
  scope?: {
    in_scope: string[]
    out_of_scope: string[]
  }
}

/**
 * 提交给后端 `start-stream` 的问卷答案。
 * 值允许是字符串或字符串数组——后端 `format_form_data` 会把数组用「、」连接。
 */
export type RequirementAnswers = Record<string, string | string[]>

// ============================================================================
// 摘要回填 Diff 与接口规范检索（RAG）
// ============================================================================

/** 变更类型：新增 / 修改 / 删除 */
export type SummaryChangeType = 'added' | 'modified' | 'removed'

/**
 * 一条摘要字段变更，由 `diffSummaries` 逐字段比对新旧摘要得出。
 *
 * `path` 是机器可读路径（如 `mvp_features[0].user_value`），`label` 是给用户看的
 * 中文面包屑（如「MVP 功能 · 第 1 项 · 对用户的价值」）。
 */
export interface SummaryChange {
  /** 机器可读的字段路径，如 `technical_constraints.auth` */
  path: string
  /** 中文可读路径，用于展示 */
  label: string
  /** 变更类型 */
  changeType: SummaryChangeType
  /** 变更前的值；新增时为 null */
  before: unknown
  /** 变更后的值；删除时为 null */
  after: unknown
}

/**
 * 一条 RAG 检索命中（团队接口规范 / 历史接口示例）。
 * 字段与后端 `schemas.RagHit` 一致。
 */
export interface RagHit {
  /** 条目 ID */
  id: string
  /** 来源，如「团队接口规范」「历史接口示例」 */
  source: string
  /** 条目标题 */
  title: string
  /** 条目正文 */
  content: string
  /** 标签 */
  tags: string[]
  /** 相似度得分，越大越相关 */
  score: number
  /** 命中的关键词，用于解释「为什么检索到这条」 */
  matched_terms: string[]
}

/**
 * PRD 分章节生成的阶段。
 *
 * 与后端 `prd_section_writer` 的 `stage` 事件一一对应：
 * 逐章撰写（writing）→ 章节审核（reviewing）→ 有问题则改写（rewriting）→ 全部完成（done）。
 * 其它文档（接口文档 / 提示词 / 优化）走整篇生成，恒为 `idle`。
 */
export type PrdGenerationStage = 'idle' | 'writing' | 'reviewing' | 'rewriting' | 'done'

/**
 * 一次分章节生成的进度快照，由 `stage` 事件累积得出。
 *
 * `sectionId` 标识当前正在处理的章节：换章（`writing`）时前端要丢弃本章的实时缓冲，
 * 改写（`rewriting`）时则把缓冲留作「上一版草稿」提示。
 */
export interface PrdStageProgress {
  /** 当前阶段 */
  stage: PrdGenerationStage
  /** 当前章节 id；未开始时为空串 */
  sectionId: string
  /** 当前章节名称，如「功能需求」 */
  sectionTitle: string
  /** 当前章节标题行，如 `## 2. 功能需求` */
  sectionHeading: string
  /** 当前章节下标（从 0 开始）；未知为 0 */
  index: number
  /** 章节总数；未知为 0（例如后端降级为整篇生成时） */
  total: number
  /** 触发改写时的原因摘要；无则为空串 */
  note: string
}

/**
 * 一段**已定稿正文**，由 `part` 事件下发。
 *
 * 后端每完成一段就把加工好的正文整段发下来：文档头一次、每一章一次。
 * `text` 已含必要的分隔空行，**可直接追加到文档末尾**——前端据此累积出与最终
 * `done.reply` 逐字一致的预览。之所以不按 `chunk` 自己拼：`chunk` 是模型原始输出，
 * 还差「补标题行」「补章间空行」两道加工，那是前端看不到的。
 */
export interface StreamDocumentPart {
  /** 可直接追加到文档末尾的定稿文本（含分隔空行） */
  text: string
  /** 所属章节 id；文档头等不属于任何章节的部分为空串 */
  sectionId: string
  /** 所属章节名称，如「功能需求」；非章节部分为空串 */
  sectionTitle: string
  /** 所属章节标题行，如 `## 2. 功能需求`；非章节部分为空串 */
  sectionHeading: string
  /** 章节下标（从 0 开始）；非章节部分为 0 */
  index: number
  /** 章节总数；非章节部分为 0 */
  total: number
}

/**
 * 一条 `stage` 事件解析出的结果。
 * 字段与后端 `prd_section_writer._section_stage_event` 的 `data` 一致。
 */
export interface StreamStageEvent {
  /** 原始阶段名，可能是后端新增的取值；未知值由消费方归一为 `writing` */
  stage: string
  sectionId: string
  sectionTitle: string
  sectionHeading: string
  index: number
  total: number
  note: string
  /** 改写原因（审核给出的问题清单） */
  issues: { severity: string; detail: string }[]
}

// ============================================================================
// 生成任务（generation_jobs）：刷新重连的状态载体
// ============================================================================

/** 已定稿章节的结构化记录（后端 `snapshot` 的 `completed_sections` 项） */
export interface CompletedSection {
  /** 章节 id，如 `overview` */
  sectionId: string
  /** 章节标题行，如 `## 1. 产品概述` */
  heading: string
  /** 该章的定稿正文（可直接追加） */
  text: string
}

/**
 * 一次生成任务在订阅开始时的**快照**（后端 `snapshot` 事件的 `data`）。
 *
 * 刷新 / 断线重连时，前端先用它把「断线前已产出的正文」补齐，再接 live 流，
 * 从而在页面恢复后**自动静默重连**、不丢进度。
 */
export interface JobSnapshot {
  /** 任务 id */
  jobId: number
  /** 文档种类（后端原始取值，如 `api_docs`） */
  kind: string
  /** 任务状态：running / completed / failed */
  status: string
  /** 当前阶段（如 writing / reviewing / rewriting） */
  phase: string
  /** 已定稿正文（文档头 + 各章，可直接作为预览前缀） */
  finalizedText: string
  /** 当前段落实时缓冲（尚未定稿的 chunk 增量） */
  liveBuffer: string
  /** 已定稿章节列表 */
  completedSections: CompletedSection[]
  /** 关联方案 id；可空 */
  solutionId: number | null
  /** 失败时的错误码；正常为空串 */
  errorCode: string
  /** 失败时的中文说明；正常为空串 */
  errorMessage: string
}

/**
 * 任务收尾的最小摘要（后端 `run_summary` 事件的 `data`）。
 *
 * 本期只含 `{kind, status, duration_ms}`；token 计量字段留待后续阶段。
 * 前端**只把它存进 ref，不做任何 UI**（见 `composables/useJob.ts`）。
 */
export interface RunSummary {
  /** 文档种类（后端原始取值） */
  kind: string
  /** 终态：completed / failed */
  status: string
  /** 本次生成耗时（毫秒） */
  durationMs: number
}

// ============================================================================
// 认证（登录 / 注册）
// ============================================================================

/**
 * 当前登录用户。
 *
 * 只含展示所需的字段——后端响应模型（`AuthUserResponse`）刻意不带密码哈希与 salt，
 * 前端也就不该有地方能拿到它们。
 */
export interface AuthUser {
  /** 用户 ID */
  id: number
  /** 用户名，顶栏展示的就是它 */
  username: string
}

/** 登录请求参数 */
export interface LoginPayload {
  username: string
  password: string
}

/** 注册请求参数 */
export interface RegisterPayload {
  /** 3-32 个字符 */
  username: string
  /** 至少 8 位 */
  password: string
  /** 邀请码。后端用限量 / 限时码控制注册，前端不做本地校验 */
  inviteCode: string
}

// ============================================================================
// 方案持久化（solutions）
// ============================================================================

/**
 * 方案摘要，用于左侧边栏列表。
 *
 * 刻意**不含快照正文**：一份快照含三份文档，几十 KB 起步，列表页要的只是标题与进度。
 * 后端也是按这个思路设计的——列表查询根本不读 `data` 列。
 */
export interface SolutionSummary {
  /** 方案 ID，也是路由参数 `/v2/:id` */
  id: number
  /** 方案标题；未取到产品名时自动生成的默认标题 */
  title: string
  /** 入口模式；空串表示未选（兼容旧数据） */
  mode: string
  /** 当前阶段（`ViewState` 的字面值），用于显示进度 */
  stage: string
  /** 创建时间（UTC ISO 8601 字符串） */
  created_at: string
  /** 最近更新时间（UTC ISO 8601 字符串），列表按它倒序 */
  updated_at: string
}

/** 方案详情：摘要 + 快照正文 */
export interface SolutionDetail extends SolutionSummary {
  /** `SessionDataV2` 的 JSON 文本，由前端自行反序列化 */
  data: string
}

/**
 * 保存方案的请求体。
 *
 * `title` / `mode` / `stage` 是**冗余摘要**，由前端随快照一起算好提交——
 * 「产品名该从 `summary` 还是 `quickInput` 取」只有前端清楚，后端不解析快照内容。
 */
export interface SolutionSavePayload {
  title: string
  mode: string
  stage: string
  /** `SessionDataV2` 序列化后的 JSON 文本 */
  data: string
}

/**
 * V2 完整流程的本地快照。
 * 与 V1 的 `SessionData` 分开存储，避免两套流程共用同一份缓存互相覆盖。
 *
 * 「还没提交、但已经敲了字」的两处录入内容（快捷入口的一段话、调试入口的 PRD）
 * 也一并落盘：它们是用户的手工输入，刷新一次就丢干净最让人恼火。
 */
export interface SessionDataV2 {
  /** 当前所处阶段 */
  viewState: ViewState
  /** 所选入口模式；未选择为 null（视为完整流程，兼容旧快照） */
  entryMode: EntryMode | null
  /** 结构化需求摘要；未提交过为 null */
  summary: RequirementsSummary | null
  /** 已固化的聊天消息 */
  messages: ChatMessage[]
  /** 后端会话 ID（可为空，接续时退化无状态模式） */
  conversationId: string
  /** PRD 快捷入口的录入内容 */
  quickInput: QuickPrdInput
  /** 提示词调试入口里用户贴入的 PRD 正文 */
  debugPrdText: string
  /** PRD 正文 */
  prdContent: string
  /** 接口文档正文 */
  apiDocsContent: string
  /** 提示词套件正文 */
  promptsContent: string
  /**
   * 当前进行中的生成任务 id；`null` 表示没有任务。
   *
   * 有了它，刷新后即使正文只到一半，前端也能凭这个指针**静默重连**到同一任务，
   * 先收 `snapshot` 补全正文再接 live。
   */
  activeJobId: number | null
  /** 当前进行中任务的文档种类；与 `activeJobId` 成对出现，无任务时为 null */
  activeJobKind: DocKind | null
}

