/**
 * 流程阶段机（纯函数）。
 *
 * 把「阶段 → 进度 / 守卫 / 刷新降级」这些判断从组件里搬出来，做成不依赖 Vue
 * 的纯函数：既能独立单测，也能被四条入口（完整流程 / 快捷流程 / 我有 PRD /
 * 我有 PRD + 接口文档）复用。
 *
 * **入口模式（`EntryMode`）在这里体现为三件事**：进度条展示哪几步、当前在第几步、
 * 以及「完成」需要哪几份文档。三件事都由模式推导，组件与 store 不再各写一套判断。
 */

import { CHAT_STAGES, STEP_OF_STATE, STEPS } from '@/types'
import type {
  ChatMessage,
  ChatStage,
  DocKind,
  DocumentKind,
  EntryMode,
  PastePairInput,
  QuickPrdInput,
  RequirementsSummary,
  RunSummary,
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
  'prd-and-api-paste',
  'done',
]

/** 判断任意值是否为合法阶段（用于校验本地快照，避免脏数据把界面带进未知状态） */
export function isViewState(value: unknown): value is ViewState {
  return typeof value === 'string' && (VIEW_STATES as readonly string[]).includes(value)
}

/** 判断任意值是否为合法入口模式 */
export function isEntryMode(value: unknown): value is EntryMode {
  return (
    value === 'structured' ||
    value === 'prd-quick' ||
    value === 'prompt-debug' ||
    value === 'prd-and-api-paste'
  )
}

/**
 * 判断任意值是否为合法**产物**种类（不含 `optimize` / `clarify` 这两个动作）。
 *
 * 与 `isDocKind` 的差别就是「东西」与「做的事」的差别：校验「澄清针对哪一份」必须用这个，
 * 否则快照里一个 `clarify` 会让前端去 documents store 里找一份叫 `clarify` 的产物。
 */
export function isDocumentKind(value: unknown): value is DocumentKind {
  return value === 'prd' || value === 'api-docs' || value === 'prompts'
}

/**
 * 后端产物名（下划线）→ 前端 `DocumentKind`（连字符）；未知取值返回 `null`。
 *
 * 放在这里而不是 `services/documentService.ts`：`api.ts` 需要它解析任务快照里的
 * `doc_kind`，而 `documentService` 本身 import 了 `api` 的 axios 实例——反向 import 会成环。
 * 纯函数下沉到 utils 后两边都能用；`documentService` 仍 re-export 旧名 `fromBackendKind`，
 * 调用方不必改。
 *
 * 返回 `null` 而不是猜一个默认值：调用方据此走「按阶段反推」的旧路，而不是拿 `prd` 去
 * 覆盖一份接口文档。
 */
export function fromBackendKind(kind: unknown): DocumentKind | null {
  if (kind === 'prd') return 'prd'
  if (kind === 'api_docs' || kind === 'api-docs') return 'api-docs'
  if (kind === 'prompts') return 'prompts'
  return null
}

/** 判断任意值是否为合法文档种类（生成任务的 kind） */
export function isDocKind(value: unknown): value is DocKind {
  return (
    value === 'prd' ||
    value === 'api-docs' ||
    value === 'prompts' ||
    value === 'optimize' ||
    value === 'clarify' ||
    value === 'auto-clarify'
  )
}

/**
 * 后端**任务**种类（下划线）→ 前端 `DocKind`（连字符）；未知取值返回 `null`。
 *
 * 与 `fromBackendKind` 只差那几个「动作」：`optimize` / `clarify` / `auto_clarify`
 * 不是产物，但它们是任务的种类，重连时得靠它选对 handlers 与阶段。两者不能合并——
 * 合并之后「澄清针对哪一份」就会拿到一个 `optimize`，去 documents store 里找一份不存在的产物。
 */
export function fromBackendJobKind(kind: unknown): DocKind | null {
  if (kind === 'optimize') return 'optimize'
  if (kind === 'clarify') return 'clarify'
  if (kind === 'auto_clarify' || kind === 'auto-clarify') return 'auto-clarify'
  return fromBackendKind(kind)
}

// ----------------------------------------------------------------------------
// 澄清线程（对话页的四条线程）
// ----------------------------------------------------------------------------

/**
 * 判断任意值是否为合法澄清线程。
 *
 * ⚠️ **刻意不接受旧的 `'document'`**：它已改名为 `'prd-clarify'`。全仓唯一需要认识旧值的
 * 地方是快照解析（`normalizeChatStage`），其余代码一律只认新值——让「旧的也能过」等于把
 * 这次改名做了一半，将来没人知道该按哪套值判断。
 */
export function isChatStage(value: unknown): value is ChatStage {
  return typeof value === 'string' && (CHAT_STAGES as readonly string[]).includes(value)
}

/**
 * 把快照里的 `stage` 归一成当前取值。
 *
 * - 旧值 `'document'` → `'prd-clarify'`：改名之前只有「需求澄清」与「文档澄清」两种，
 *   而落在 `messages` 里的 `'document'` **一定**是 PRD 澄清（那时接口文档 / 提示词的
 *   澄清另存在 `clarifyMessages` 里）；
 * - 缺失或无法识别 → `'requirement'`：消息没有这个字段只可能是旧快照的需求澄清轮次。
 *   这里**不丢弃消息**——丢掉一条用户聊过的内容，比把它归到需求线程要糟得多。
 */
export function normalizeChatStage(value: unknown): ChatStage {
  if (value === 'document') return 'prd-clarify'
  return isChatStage(value) ? value : 'requirement'
}

/** 这条线程在澄清哪一份产物；需求澄清不对应任何产物，返回 `null` */
export function documentKindOfChatStage(stage: ChatStage): DocumentKind | null {
  switch (stage) {
    case 'prd-clarify':
      return 'prd'
    case 'api-docs-clarify':
      return 'api-docs'
    case 'prompts-clarify':
      return 'prompts'
    default:
      return null
  }
}

/** 某一份产物对应的澄清线程；与 `documentKindOfChatStage` 互为逆映射 */
export function chatStageForDocument(kind: DocumentKind): ChatStage {
  switch (kind) {
    case 'api-docs':
      return 'api-docs-clarify'
    case 'prompts':
      return 'prompts-clarify'
    default:
      return 'prd-clarify'
  }
}

/** 空的「各线程会话 id」表（新建 / 重置时的初值） */
export function emptyConversationIds(): Record<ChatStage, string> {
  const ids = {} as Record<ChatStage, string>
  for (const stage of CHAT_STAGES) ids[stage] = ''
  return ids
}

/** 各入口模式在进度条上展示的步骤（顺序沿用 `STEPS`） */
const MODE_STEP_IDS: Record<EntryMode, readonly StepId[]> = {
  // 完整流程：六步齐全（最后一步是交付）
  structured: ['form', 'chatting', 'review-prd', 'review-api-docs', 'review-prompts', 'done'],
  // 快捷流程与完整流程**同一条流水线**，连步数都一样：只差第一步长什么样（极简输入而不是
  // 结构化表单），以及它**没有需求澄清对话**（ADR-034 修订）——「AI 对话」这一格在快捷
  // 流程里对应的是**文档澄清**那三条线程（它们就在同一个对话页上）。
  //
  // ⚠️ 这一格**不能省**：省掉之后 `stepIndexOf('chatting', 'prd-quick')` 会算出 -1 并归零，
  // 用户在文档澄清页里聊天时，进度条高亮的是第 1 步「描述产品」——一个明显错位的状态。
  'prd-quick': ['form', 'chatting', 'review-prd', 'review-api-docs', 'review-prompts', 'done'],
  // 两个导入入口的产物只有提示词套件
  'prompt-debug': ['review-prompts', 'done'],
  'prd-and-api-paste': ['review-prompts', 'done'],
}

/**
 * 这个入口有没有「需求澄清」那条对话。
 *
 * 只有**完整流程**有：它的起点是「先把需求交给 AI 逐题追问」。快捷流程刻意不要这一环——
 * 用户选它就是因为不想多聊几轮，一段话写完直接进生成；两个导入入口自带的 PRD 已经是答案。
 *
 * 写成白名单而不是「不等于某些入口」：将来加新入口时，默认「没有需求对话」比默认「有」安全
 * 得多（多给一条点进去只能自说自话的线程，比少给一条更糟）。
 */
export function hasRequirementChat(mode: EntryMode | null): boolean {
  return (mode ?? 'structured') === 'structured'
}

/**
 * 这个入口的产物是否带「审核 / 改写」环节。
 *
 * 只有**快捷流程**不要：它的定义就是「不审不改」（ADR-034），三份产物都一样。
 * 除了跳过调用，界面上也不能留这两个阶段——进度条上画两格永远走不到的路，
 * 用户会读成「还没审到」而不是「这次压根不审」。
 *
 * 写成黑名单（`prd-quick` 是例外）而不是白名单：将来加新入口默认**带上复核**，
 * 漏配的代价只是多等一会儿；反过来则悄悄把产出质量降了一档。
 */
export function hasDocumentReview(mode: EntryMode | null): boolean {
  return (mode ?? 'structured') !== 'prd-quick'
}

/**
 * 这个入口的「录入页」是不是就在进度条上（点第 1 步就能回去）。
 *
 * 两个从零创建的入口是：`structured` 与 `prd-quick` 的第 1 步都是「描述产品」，只是
 * 录入页长什么样由模式决定（结构化表单 / 一段话）。
 *
 * 两个**导入**入口不是——它们的录入页（贴 PRD / 贴两份材料）根本不在进度条上，
 * 因此审阅页必须保留一个「返回」按钮，否则用户改不了贴进来的材料，直接成了死路。
 */
export function inputPageOnBar(mode: EntryMode | null): boolean {
  return (mode ?? 'structured') === 'structured' || mode === 'prd-quick'
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
  // 快捷流程复用完整流程的进度条，交付判据也一并对齐：三份齐备才算完成
  'prd-quick': ['prd', 'api-docs', 'prompts'],
  // 两个导入入口的交付物都只有提示词套件（自带的那两份材料不算本工具的产物）
  'prompt-debug': ['prompts'],
  'prd-and-api-paste': ['prompts'],
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
 * 三个录入页（`prd-quick` / `prompt-debug` / `prd-and-api-paste`）**不需要**降级：
 * 它们的内容落在 store 里并随会话快照一起持久化，刷新后原样还在。
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
 * 「生成中」阶段 → 产物种类；与 `generatingStateFor` 互为逆映射（非生成态返回 `null`）。
 *
 * 用来回答「这次任务正在写哪一份文档」——澄清改写（`clarify`）的目标就靠它反推：
 * 改写**先切到 `generating-<kind>` 再发任务**（见 `useDocumentPipeline.applyClarification`），
 * 因此阶段本身就是权威答案，不必再单独持久化一个「改写目标」字段。
 */
export function docKindOfGeneratingState(state: ViewState): DocumentKind | null {
  switch (state) {
    case 'generating-prd':
      return 'prd'
    case 'generating-api-docs':
      return 'api-docs'
    case 'generating-prompts':
      return 'prompts'
    default:
      return null
  }
}

/**
 * 「待确认」阶段 → 产物种类。
 *
 * 用于「刷新后无 activeJobId、但快照落在 generating-*」的兜底：降级后落到 review-*，
 * 由它反推原本在生成哪份文档，再去查活跃任务。
 *
 * 返回类型刻意收窄为 `DocumentKind`（不含 `optimize`）：审阅阶段描述的永远是
 * 「在看哪一份产物」，而 `optimize` 是动作、不是产物。调用方拿它去查文档时，
 * 类型系统因此能挡住「拿 optimize 当产物用」这类错。
 */
export function docKindOfState(state: ViewState): DocumentKind | null {
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

/**
 * 任务种类自带的产物身份：三种**整篇生成**任务的种类就是它写的那份产物。
 *
 * `optimize` / `clarify` 是动作而非产物，返回 `null`——它们的目标另有来源
 * （优化看 `docKindOfState(viewState)`，澄清看 `docKindOfGeneratingState(viewState)`）。
 */
export function jobDocumentKind(kind: DocKind): DocumentKind | null {
  return kind === 'prd' || kind === 'api-docs' || kind === 'prompts' ? kind : null
}

/** 是否处于「文档待确认」阶段 */
export function isDocReviewPhase(state: ViewState): boolean {
  return state.startsWith('review-')
}

/**
 * 产物种类 → 对应的「待确认」阶段。
 *
 * 与 `generatingStateFor` 互为逆映射，用于「在别处处理完一份文档后回到它的审阅页」
 * （按澄清结果改写、按 `doc_kind` 恢复落点等）。返回类型是 `DocumentKind`——
 * `optimize` / `clarify` 是动作，它们各有自己的目标产物，不在此映射之列。
 */
export function reviewStateFor(kind: DocumentKind): ViewState {
  switch (kind) {
    case 'api-docs':
      return 'review-api-docs'
    case 'prompts':
      return 'review-prompts'
    default:
      return 'review-prd'
  }
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
 * 由**产物内容**推出的「最远里程碑」下标——**不含**「此刻在看哪一步」。
 *
 * 判断口径按模式裁剪后的步骤来数（而不是写死 1/2/3/4）：两个导入入口的「提示词」是它的
 * 第 1 步，用全局下标会算出越界的位置。
 *
 * 单独拆出来是因为它有第二个用处：方案列表要显示「这份做到哪了」——那个语义**只该看
 * 产物**，不能把「用户正在翻看哪一页」算进去，否则回看旧文档时列表里的进度会倒退。
 */
export function farthestContentStepIndex(
  input: Omit<ReachedStepInput, 'currentStepIndex'>,
): number {
  const unlocked: Record<StepId, boolean> = {
    // 第一步永远可达：随时可以回去改描述
    form: true,
    chatting: input.hasMessages,
    'review-prd': input.hasPrd,
    'review-api-docs': input.hasApiDocs,
    'review-prompts': input.hasPrompts,
    // 「交付」不按产物解锁：文档齐备只代表“可以完成”，不代表已经进了交付页。
    // 它由 `currentStepIndex` 在用户点「完成」后打开（`reachedStepIndex` 取两者较大者），
    // 保持「最远里程碑」仍然描述产物流水线，而不是把“可交付”说成“已交付”。
    done: false,
  }

  let reached = 0
  stepsForMode(input.mode).forEach((step, index) => {
    if (unlocked[step.id]) reached = Math.max(reached, index)
  })
  return reached
}

/**
 * 历史中实际到达过的最远步骤下标（进度条的可点击范围）。
 *
 * 以「已生成内容 + 当前所处」的最远位置为准，而非当前所处步骤——这样回看前面的阶段后，
 * 已经生成好的后续阶段依然能点回去。
 */
export function reachedStepIndex(input: ReachedStepInput): number {
  return Math.max(farthestContentStepIndex(input), input.currentStepIndex)
}

/** 对话是否已具备「生成 PRD」的条件：至少一轮 AI 回复，且当前没有进行中的请求 */
export function canGeneratePrdFrom(messages: readonly ChatMessage[], busy: boolean): boolean {
  if (busy) return false
  return messages.some((message) => message.role === 'assistant' && message.content.trim() !== '')
}

/**
 * 当前入口模式下「完成」所需的最低文档是否齐备。
 *
 * 各模式的交付物不同：完整流程与快捷流程都要三份齐全，两个导入入口只要提示词。
 * 用统一的口径判断，可以避免「只想拿一份文档的人被另外两份挡住」。
 *
 * 完整流程 / 快捷流程的判定与旧版完全等价，
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

/** 解析快照里的「我有 PRD + 接口文档」录入内容；字段缺失或类型不符时退化为空 */
function parsePastePairInput(value: unknown): PastePairInput {
  if (typeof value !== 'object' || value === null) return { prd: '', apiDocs: '' }
  const record = value as Partial<PastePairInput>
  return {
    prd: typeof record.prd === 'string' ? record.prd : '',
    apiDocs: typeof record.apiDocs === 'string' ? record.apiDocs : '',
  }
}

/** 解析快照里的进行中任务 id；缺失 / 非法 / 非正数一律退化为 null */
function parseActiveJobId(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : null
}

/** 解析「各线程会话 id」；缺失 / 类型不符的项退化为空串（那条线程接续时走无状态模式） */
function parseConversationIds(value: unknown): Record<ChatStage, string> {
  const ids = emptyConversationIds()
  if (typeof value !== 'object' || value === null) return ids
  const record = value as Record<string, unknown>
  for (const stage of CHAT_STAGES) {
    if (typeof record[stage] === 'string') ids[stage] = record[stage]
  }
  return ids
}

/**
 * 快照里那些**已改名 / 已退役**的字段，只在迁移时读一次，永不写回。
 *
 * 单独列出而不是塞进 `SessionDataV2`：后者描述的是当前形态，把历史字段混进去，
 * 下一个人就分不清哪个才是真要写的。
 */
interface LegacyChatFields {
  /** 旧的「需求澄清」会话 id */
  conversationId?: unknown
  /** 旧的独立澄清消息（接口文档 / 提示词那条链路，消息标记与 PRD 澄清共用 `'document'`） */
  clarifyMessages?: unknown
  clarifyConversationId?: unknown
  /** 旧的澄清目标产物；迁移时靠它把那批消息落到正确的线程上 */
  clarifyTarget?: unknown
  prdClarifyConversationId?: unknown
  prdClarifying?: unknown
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
 *
 * **产物正文与用量回执不在这里解析**：它们已迁到后端 `documents` 表，由
 * `stores/documents.ts` 按方案 id 单独取回。旧快照里残留的 `prdContent` 等字段
 * 会被自然忽略——不是「兼容读取」，而是它们从此不再是快照的一部分。
 *
 * **澄清线程的迁移**（四条线程合流那次改名）在这里一次做完，而不是让全仓代码去认识旧值：
 * `'clarifying'` 阶段 → `'chatting'`、消息的 `stage: 'document'` → `'prd-clarify'`、
 * 旧 `clarifyMessages` 按其 `clarifyTarget` 并入对应线程、旧会话 id 落到 `conversationIds`
 * 对应键。**消息一条都不丢**——聊过的内容比字段整洁要紧得多。
 */
export function normalizeSessionSnapshot(parsed: unknown): ParsedSessionSnapshot | null {
  if (typeof parsed !== 'object' || parsed === null) return null

  const record = parsed as Partial<SessionDataV2> & LegacyChatFields
  const activeJobId = parseActiveJobId(record.activeJobId)
  const hasActiveJob = activeJobId !== null

  // ---- 迁移第一步：把「独立澄清页」换算成新形态 ----
  // 旧的 `clarifying` 页面本质上就是一个对话页，因此阶段换算成 `chatting`；
  // **停在哪条线程**才需要 `clarifyTarget`（见下面的 `chatTab` 推断）。
  // 若不换算，`isViewState('clarifying')` 判定失败，用户打开一份正在澄清的方案会被直接丢回入口页。
  const legacyStage = isDocumentKind(record.clarifyTarget)
    ? chatStageForDocument(record.clarifyTarget)
    : null
  // 旧阶段名单独按 unknown 读：它已不在 `ViewState` 里，直接拿 `record.viewState` 去比会被
  // 类型系统判成「两个不相交的类型」——而这里要的正是「认识旧值」这件事本身。
  const legacyPage: unknown = (record as { viewState?: unknown }).viewState
  const legacyClarifying = legacyPage === 'clarifying'
  const { viewState, downgrade } = resolveRestoredPhase(
    legacyClarifying ? 'chatting' : record.viewState,
    hasActiveJob,
  )

  // 消息：四条线程合在一张表里，`stage` 归一到当前取值
  const messages: ChatMessage[] = Array.isArray(record.messages)
    ? record.messages.filter(isChatMessage).map((message) => ({
        ...message,
        stage: normalizeChatStage(message.stage),
      }))
    : []

  // 旧的独立澄清消息按当时的 `clarifyTarget` 并入对应线程。**不能一律当 PRD 澄清**：
  // 那会把「接口文档澄清」的结论塞进 PRD 线程，之后点「按澄清结果改写」就会把错的结论
  // 送进错的文档——两条线程串味的后果比丢消息还糟。
  if (legacyStage && Array.isArray(record.clarifyMessages)) {
    for (const message of record.clarifyMessages.filter(isChatMessage)) {
      messages.push({ ...message, stage: legacyStage })
    }
  }

  // ---- 迁移第二步：三个旧会话 id 各归各位 ----
  const conversationIds = parseConversationIds(record.conversationIds)
  if (typeof record.conversationId === 'string') conversationIds.requirement = record.conversationId
  if (typeof record.prdClarifyConversationId === 'string') {
    conversationIds['prd-clarify'] = record.prdClarifyConversationId
  }
  if (legacyStage && typeof record.clarifyConversationId === 'string') {
    conversationIds[legacyStage] = record.clarifyConversationId
  }

  // ---- 迁移第三步：当前标签 ----
  // 新字段优先；旧快照按「当时停在哪」推断——停在澄清页就是对应的文档线程，
  // `prdClarifying` 为真就是 PRD 线程，其余都是需求澄清。
  let chatTab: ChatStage = 'requirement'
  if (isChatStage(record.chatTab)) chatTab = record.chatTab
  else if (legacyClarifying && legacyStage) chatTab = legacyStage
  else if (record.prdClarifying === true) chatTab = 'prd-clarify'

  return {
    downgrade,
    session: {
      viewState,
      entryMode: isEntryMode(record.entryMode) ? record.entryMode : null,
      summary:
        typeof record.summary === 'object' && record.summary !== null
          ? (record.summary as RequirementsSummary)
          : null,
      messages,
      chatTab,
      conversationIds,
      quickInput: parseQuickInput(record.quickInput),
      debugPrdText: typeof record.debugPrdText === 'string' ? record.debugPrdText : '',
      pastePairInput: parsePastePairInput(record.pastePairInput),
      activeJobId,
      activeJobKind: hasActiveJob && isDocKind(record.activeJobKind) ? record.activeJobKind : null,
      // 可选字段：旧快照没有它。缺失时留 null（由使用方按阶段反推），**不要**据此丢掉整份快照。
      activeJobDocKind:
        hasActiveJob && isDocumentKind(record.activeJobDocKind) ? record.activeJobDocKind : null,
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

/**
 * 校验一份「生成用量回执」是否可信。
 *
 * 只核对几个**不可能由脏数据伪造**的字段（`kind` / `durationMs` / `requestId`），
 * 其余字段原样透传——回执是后端产出的诊断信息，结构一旦变了我们更愿意直接报错而不是静默吞掉，
 * 所以这里不做深校验。脏数据（非对象 / 缺关键字段 / 旧版本快照里根本没有这一项）一律退化为 `null`，
 * 该文档就「不显示回执」，与上线前的行为一致。
 */
export function parseRunSummary(value: unknown): RunSummary | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null
  const record = value as Record<string, unknown>
  if (
    typeof record.kind !== 'string' ||
    typeof record.durationMs !== 'number' ||
    typeof record.requestId !== 'string'
  ) {
    return null
  }
  return value as RunSummary
}
