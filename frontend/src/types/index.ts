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
 */
export type ViewState =
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
  | 'done'

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

/** ViewState → 所属步骤，用于定位进度条当前位置 */
export const STEP_OF_STATE: Record<ViewState, StepId> = {
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
 * V2 完整流程的本地快照。
 * 与 V1 的 `SessionData` 分开存储，避免两套流程共用同一份缓存互相覆盖。
 */
export interface SessionDataV2 {
  /** 当前所处阶段 */
  viewState: ViewState
  /** 结构化需求摘要；未提交过为 null */
  summary: RequirementsSummary | null
  /** 已固化的聊天消息 */
  messages: ChatMessage[]
  /** 后端会话 ID（可为空，接续时退化无状态模式） */
  conversationId: string
  /** PRD 正文 */
  prdContent: string
  /** 接口文档正文 */
  apiDocsContent: string
  /** 提示词套件正文 */
  promptsContent: string
}

