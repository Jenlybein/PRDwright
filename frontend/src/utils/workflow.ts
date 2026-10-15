/**
 * 流程阶段机（纯函数）。
 *
 * 把「阶段 → 进度 / 守卫 / 刷新降级」这些判断从组件里搬出来，做成不依赖 Vue
 * 的纯函数：既能独立单测，也能被不同入口（完整流程 / PRD 快捷 / 提示词调试）复用。
 *
 * **入口模式（`EntryMode`）在这里体现为三件事**：进度条展示哪几步、当前在第几步、
 * 以及「完成」需要哪几份文档。三件事都由模式推导，组件与 store 不再各写一套判断。
 */

import { STEP_OF_STATE, STEPS } from '@/types'
import type {
  ChatMessage,
  DocKind,
  EntryMode,
  QuickPrdInput,
  RequirementsSummary,
  SessionDataV2,
  Step,
  StepId,
  ViewState,
} from '@/types'
import type { DocType } from '@/utils/download'

/** 全部合法阶段；顺序与业务流程一致 */
export const VIEW_STATES: readonly ViewState[] = [
  'entry',
  'form',
  'chatting',
  'summary-diff',
  'generating-prd',
  'review-prd',
  'generating-api-docs',
  'review-api-docs',
  'generating-prompts',
  'review-prompts',
  'prd-quick',
  'prompt-debug',
  'done',
]

/** 判断任意值是否为合法阶段（用于校验本地快照，避免脏数据把界面带进未知状态） */
export function isViewState(value: unknown): value is ViewState {
  return typeof value === 'string' && (VIEW_STATES as readonly string[]).includes(value)
}

/** 判断任意值是否为合法入口模式 */
export function isEntryMode(value: unknown): value is EntryMode {
  return value === 'structured' || value === 'prd-quick' || value === 'prompt-debug'
}

/** 判断任意值是否为合法文档种类（生成任务的 kind） */
export function isDocKind(value: unknown): value is DocKind {
  return (
    value === 'prd' || value === 'api-docs' || value === 'prompts' || value === 'optimize'
  )
}

/** 各入口模式在进度条上展示的步骤（顺序沿用 `STEPS`） */
const MODE_STEP_IDS: Record<EntryMode, readonly StepId[]> = {
  // 完整流程：五步齐全
  structured: ['form', 'chatting', 'review-prd', 'review-api-docs', 'review-prompts'],
  // 快捷入口跳过了表单与对话，进度条不该显示一步都点不动的空壳
  'prd-quick': ['review-prd', 'review-api-docs', 'review-prompts'],
  // 调试入口的产物只有提示词套件
  'prompt-debug': ['review-prompts'],
}

/**
 * 某个入口模式下进度条要展示的步骤。
 * `mode` 为 `null`（还没选入口 / 旧快照）时按完整流程处理。
 */
export function stepsForMode(mode: EntryMode | null): readonly Step[] {
  const ids = MODE_STEP_IDS[mode ?? 'structured']
  return STEPS.filter((step) => ids.includes(step.id))
}

/** 各入口模式下「完成」所需的最低文档集合 */
export const REQUIRED_DOCS: Record<EntryMode, readonly DocType[]> = {
  structured: ['prd', 'api-docs', 'prompts'],
  // 快捷入口的交付物就是 PRD；后续接口文档 / 提示词属可选加成
  'prd-quick': ['prd'],
  // 调试入口的交付物只有提示词套件
  'prompt-debug': ['prompts'],
}

/** 刷新降级的类型：生成中断 / 确认步骤中断 */
export type DowngradeKind = 'generating' | 'pending'

/**
 * 刷新时无法继续的状态 → 回退到的最近可操作状态。
 *
 * - `generating-*`：流式请求随页面销毁而中断，回退到对应的「待确认」，让用户看到
 *   已生成的结果并决定下一步。
 * - `summary-diff`：这道确认闸门依赖内存里的一次性结果（Diff），刷新后无从恢复，
 *   回退到发起它的步骤让用户重新发起。
 *
 * 两个录入页（`prd-quick` / `prompt-debug`）**不需要**降级：它们的内容落在 store 里
 * 并随会话快照一起持久化，刷新后原样还在。
 */
export const STATE_DOWNGRADE: Partial<
  Record<ViewState, { state: ViewState; kind: DowngradeKind }>
> = {
  'generating-prd': { state: 'review-prd', kind: 'generating' },
  'generating-api-docs': { state: 'review-api-docs', kind: 'generating' },
  'generating-prompts': { state: 'review-prompts', kind: 'generating' },
  'summary-diff': { state: 'chatting', kind: 'pending' },
}

/** 降级说明文案 */
export const DOWNGRADE_NOTICE: Record<DowngradeKind, string> = {
  generating: '上次的生成未完成（页面已刷新），已为你恢复到最近一次的结果，可继续操作。',
  pending: '上次的确认步骤未完成（页面已刷新），已为你回到对应的步骤，可重新发起。',
}

/** 恢复结果：合法的阶段 + 是否发生了降级 */
export interface RestoredPhase {
  viewState: ViewState
  downgrade: DowngradeKind | null
}

/**
 * 依据快照里记录的阶段算出恢复后的阶段；非法值一律回到入口页。
 *
 * `hasActiveJob` 为真表示快照里记着一个**还在跑**的生成任务——此时 `generating-*`
 * 不再降级：任务在后端继续跑，前端重连后由 `snapshot` 决定落点（续流 / 落稿）。
 * 无任务时才按原规则降级并给出说明。
 */
export function resolveRestoredPhase(last: unknown, hasActiveJob = false): RestoredPhase {
  if (!isViewState(last)) return { viewState: 'entry', downgrade: null }
  if (hasActiveJob && isGeneratingPhase(last)) {
    return { viewState: last, downgrade: null }
  }
  const fallback = STATE_DOWNGRADE[last]
  if (fallback) return { viewState: fallback.state, downgrade: fallback.kind }
  return { viewState: last, downgrade: null }
}

/** 是否处于「文档生成中」阶段（进度条呼吸高亮用） */
export function isGeneratingPhase(state: ViewState): boolean {
  return state.startsWith('generating-')
}

/**
 * 文档种类 → 对应的「生成中」阶段；`optimize` 就地流式、不改变阶段，故返回 `null`。
 *
 * 供「刷新后兜底找回任务」把种类还原成阶段（重连时要切回生成态视图）。
 */
export function generatingStateFor(kind: DocKind): ViewState | null {
  switch (kind) {
    case 'prd':
      return 'generating-prd'
    case 'api-docs':
      return 'generating-api-docs'
    case 'prompts':
      return 'generating-prompts'
    default:
      return null
  }
}

/**
 * 「待确认」阶段 → 文档种类。
 *
 * 用于「刷新后无 activeJobId、但快照落在 generating-*」的兜底：降级后落到 review-*，
 * 由它反推原本在生成哪份文档，再去查活跃任务。
 */
export function docKindOfState(state: ViewState): DocKind | null {
  switch (state) {
    case 'review-prd':
      return 'prd'
    case 'review-api-docs':
      return 'api-docs'
    case 'review-prompts':
      return 'prompts'
    default:
      return null
  }
}

/** 是否处于「文档待确认」阶段 */
export function isDocReviewPhase(state: ViewState): boolean {
  return state.startsWith('review-')
}

/** 阶段在进度条上的下标（口径随入口模式变化）；未知阶段归零，保证进度条永远有可渲染的位置 */
export function stepIndexOf(state: ViewState, mode: EntryMode | null = null): number {
  const steps = stepsForMode(mode)
  const index = steps.findIndex((step) => step.id === STEP_OF_STATE[state])
  return index < 0 ? 0 : index
}

/** 计算「已到达的最远步骤」所需的输入 */
export interface ReachedStepInput {
  /** 入口模式：决定进度条展示哪几步，也就决定了「第几步」的口径 */
  mode: EntryMode | null
  /** 是否已有对话记录 */
  hasMessages: boolean
  /** PRD 正文是否已生成 */
  hasPrd: boolean
  /** 接口文档是否已生成 */
  hasApiDocs: boolean
  /** 提示词套件是否已生成 */
  hasPrompts: boolean
  /** 当前阶段对应的步骤下标（已按模式裁剪） */
  currentStepIndex: number
}

/**
 * 历史中实际到达过的最远步骤下标。
 *
 * 进度条的可点击范围以「已生成内容 + 当前所处」的最远位置为准，而非当前所处步骤——
 * 这样回看前面的阶段后，已经生成好的后续阶段依然能点回去。
 *
 * 判断口径按模式裁剪后的步骤来数（而不是写死 1/2/3/4）：快捷入口的「PRD」是它的
 * 第 1 步，用全局下标会算出越界的位置。
 */
export function reachedStepIndex(input: ReachedStepInput): number {
  const unlocked: Record<StepId, boolean> = {
    // 第一步永远可达：随时可以回去改描述
    form: true,
    chatting: input.hasMessages,
    'review-prd': input.hasPrd,
    'review-api-docs': input.hasApiDocs,
    'review-prompts': input.hasPrompts,
  }

  let reached = 0
  stepsForMode(input.mode).forEach((step, index) => {
    if (unlocked[step.id]) reached = Math.max(reached, index)
  })
  return Math.max(reached, input.currentStepIndex)
}

/** 对话是否已具备「生成 PRD」的条件：至少一轮 AI 回复，且当前没有进行中的请求 */
export function canGeneratePrdFrom(messages: readonly ChatMessage[], busy: boolean): boolean {
  if (busy) return false
  return messages.some((message) => message.role === 'assistant' && message.content.trim() !== '')
}

/**
 * 当前入口模式下「完成」所需的最低文档是否齐备。
 *
 * 三种模式的交付物不同：完整流程要三份齐全，快捷入口只要 PRD，调试入口只要提示词。
 * 用统一的口径判断，可以避免「只想拿一份文档的人被另外两份挡住」。
 *
 * 完整流程（`structured`）的判定与旧版的「三份齐备」完全等价，
 * 因此这里只保留这一个函数，不再另开一个只服务单一口径的封装。
 */
export function hasRequiredDocuments(
  mode: EntryMode | null,
  docs: { prd: string; apiDocs: string; prompts: string },
): boolean {
  const byType: Record<DocType, string> = {
    prd: docs.prd,
    'api-docs': docs.apiDocs,
    prompts: docs.prompts,
  }
  return REQUIRED_DOCS[mode ?? 'structured'].every((type) => byType[type].trim() !== '')
}

/** 判断任意值是否为合法聊天消息 */
export function isChatMessage(value: unknown): value is ChatMessage {
  if (typeof value !== 'object' || value === null) return false
  const item = value as Partial<ChatMessage>
  const roles: ChatMessage['role'][] = ['system', 'user', 'assistant']
  return typeof item.content === 'string' && roles.includes(item.role as ChatMessage['role'])
}

/** 解析快照里的快捷入口录入内容；字段缺失或类型不符时退化为空 */
function parseQuickInput(value: unknown): QuickPrdInput {
  if (typeof value !== 'object' || value === null) return { productName: '', requirements: '' }
  const record = value as Partial<QuickPrdInput>
  return {
    productName: typeof record.productName === 'string' ? record.productName : '',
    requirements: typeof record.requirements === 'string' ? record.requirements : '',
  }
}

/** 解析快照里的进行中任务 id；缺失 / 非法 / 非正数一律退化为 null */
function parseActiveJobId(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : null
}

/** 快照解析结果 */
export interface ParsedSessionSnapshot {
  session: SessionDataV2
  /** 非 null 表示上次的状态无法恢复，已降级 */
  downgrade: DowngradeKind | null
}

/**
 * 校验并规范化一份会话快照。
 *
 * 每个字段单独做类型校验，脏数据只会丢弃对应字段，不会让整份快照失效——
 * 快照是跨版本存下来的，用「一个字段不对就整份作废」的严格模式会让用户平白丢掉整份工作。
 *
 * 阶段若落在无法恢复的中间态（如 `generating-prd`），这里会一并给出降级结果；
 * 但若快照里记着一个进行中的任务（`activeJobId`），则 `generating-*` **不降级**——
 * 任务在后端继续跑，重连即可续上。
 */
export function normalizeSessionSnapshot(parsed: unknown): ParsedSessionSnapshot | null {
  if (typeof parsed !== 'object' || parsed === null) return null

  const record = parsed as Partial<SessionDataV2>
  const activeJobId = parseActiveJobId(record.activeJobId)
  const hasActiveJob = activeJobId !== null
  const { viewState, downgrade } = resolveRestoredPhase(record.viewState, hasActiveJob)

  return {
    downgrade,
    session: {
      viewState,
      entryMode: isEntryMode(record.entryMode) ? record.entryMode : null,
      summary:
        typeof record.summary === 'object' && record.summary !== null
          ? (record.summary as RequirementsSummary)
          : null,
      messages: Array.isArray(record.messages) ? record.messages.filter(isChatMessage) : [],
      conversationId: typeof record.conversationId === 'string' ? record.conversationId : '',
      quickInput: parseQuickInput(record.quickInput),
      debugPrdText: typeof record.debugPrdText === 'string' ? record.debugPrdText : '',
      prdContent: typeof record.prdContent === 'string' ? record.prdContent : '',
      apiDocsContent: typeof record.apiDocsContent === 'string' ? record.apiDocsContent : '',
      promptsContent: typeof record.promptsContent === 'string' ? record.promptsContent : '',
      activeJobId,
      activeJobKind: hasActiveJob && isDocKind(record.activeJobKind) ? record.activeJobKind : null,
    },
  }
}

/**
 * 解析后端返回的快照 JSON 文本（`solutions.data`）。
 *
 * 保留这一层而不是让调用方自己 `JSON.parse`：后端把 `data` 当作不透明字符串存取，
 * 它里面究竟是不是合法 JSON、字段是否齐全，只有前端知道，也就只能在这里校验。
 * 非法 JSON、结构不符时返回 `null`，由调用方决定是新建还是报错。
 */
export function parseSessionSnapshot(raw: string | null): ParsedSessionSnapshot | null {
  if (!raw) return null

  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return null
  }
  return normalizeSessionSnapshot(parsed)
}
