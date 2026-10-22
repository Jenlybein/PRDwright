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
  SolutionStatePayload,
  Step,
  StepId,
  ViewState,
} from '@/types'
import type { DocType } from '@/utils/download'
import { normalizeRunSummary } from '@/utils/runSummary'

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

/**
 * 判断任意值是否为合法文档种类（生成任务的 kind）。
 *
 * ⚠️ **必须包含 `'chat'`**：`DocKind` 里有它（对话也是走同一条任务通道的一种 kind）。
 * 曾经漏掉它，后果很隐蔽——快照恢复时 `activeJobKind: 'chat'` 被这条校验判成非法、
 * 丢成 `null`，`applySnapshot` 于是退化成 `'prd'`，刷新后的对话被当成 PRD 生成塞进
 * 文档流水线：流式全丢，收尾还把聊天正文写进了 PRD 预览。不报任何错。
 */
export function isDocKind(value: unknown): value is DocKind {
  return (
    value === 'prd' ||
    value === 'api-docs' ||
    value === 'prompts' ||
    value === 'optimize' ||
    value === 'clarify' ||
    value === 'auto-clarify' ||
    value === 'chat'
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
  // 对话是**动作**（不产出文档），因此只能在这里认，不能放进 `fromBackendKind`
  // ——那个函数只回答「这是哪一份产物」，让它认 chat 会破坏「东西 / 动作」的分界。
  if (kind === 'chat') return 'chat'
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

/**
 * 把后端的传输形状（snake_case）还原成前端状态。
 *
 * 只做**形状搬运**，不做校验与降级——那是 `normalizeSessionSnapshot` 的事。
 * 分成两步是为了让「哪些值可信」只有一个判断点：搬运层不认识业务规则，
 * 校验层也不关心字段怎么拼。
 */
export function fromStatePayload(state: SolutionStatePayload | null | undefined): SessionDataV2 {
  const raw = state ?? ({} as Partial<SolutionStatePayload>)
  return {
    viewState: (raw.view_state || 'entry') as ViewState,
    entryMode: raw.entry_mode ? (raw.entry_mode as EntryMode) : null,
    summary: raw.summary ?? null,
    chatTab: (raw.chat_tab || 'requirement') as ChatStage,
    quickInput: {
      productName: raw.quick_product_name ?? '',
      requirements: raw.quick_requirements ?? '',
    },
    debugPrdText: raw.draft_prd_text ?? '',
    pastePairInput: { prd: raw.paste_prd_text ?? '', apiDocs: raw.paste_api_docs_text ?? '' },
    activeJobId: raw.active_job_id ?? null,
    activeJobKind: raw.active_job_kind ? (raw.active_job_kind as DocKind) : null,
    activeJobDocKind: raw.active_job_doc_kind
      ? (raw.active_job_doc_kind as DocumentKind)
      : null,
    // `activeChatStage` 不在传输形状里：它由 `chatTab` 与 `activeJobKind` 共同表达，
    // 由 `normalizeSessionSnapshot` 按「是不是对话任务」自行判定。
  }
}

/** 快照解析结果 */
export interface ParsedSessionSnapshot {
  session: SessionDataV2
  /** 非 null 表示上次的状态无法恢复，已降级 */
  downgrade: DowngradeKind | null
}

/**
 * 校验并规范化后端返回的方案状态。
 *
 * 每个字段单独做类型校验，脏数据只会丢弃对应字段，不会让整份状态失效——
 * 它是跨版本存下来的，用「一个字段不对就整份作废」的严格模式会让用户平白丢掉整份工作。
 *
 * **这里不再迁移旧快照**：`data` 那一列里的旧形状由后端 v10 迁移一次性搬进结构化列
 * 与 `chat_turns`。前端仍然存在的兼容点只有两个，都是「值本身过时」而不是「结构变了」：
 * - 旧阶段名 `'clarifying'`（它已不在 `ViewState` 里，直接比较会被类型系统判成不相交）；
 * - 停在 `generating-*` 但没有进行中任务 → 降级到对应的审阅态（`resolveRestoredPhase`）。
 *
 * 消息与会话 id **不在这里**：它们归服务端（`chat_turns` / `chat_sessions`），
 * 由 `stores/solutions.ts` 按方案 id 单独拉取。
 */
export function normalizeSessionSnapshot(parsed: unknown): ParsedSessionSnapshot | null {
  if (typeof parsed !== 'object' || parsed === null) return null

  const record = parsed as Partial<SessionDataV2>
  const activeJobId = parseActiveJobId(record.activeJobId)
  const hasActiveJob = activeJobId !== null

  // 旧阶段名单独按 unknown 读：它已不在 `ViewState` 里，直接拿 `record.viewState` 去比会被
  // 类型系统判成「两个不相交的类型」——而这里要的正是「认识旧值」这件事本身。
  const legacyPage: unknown = (record as { viewState?: unknown }).viewState
  const { viewState, downgrade } = resolveRestoredPhase(
    legacyPage === 'clarifying' ? 'chatting' : record.viewState,
    hasActiveJob,
  )

  return {
    downgrade,
    session: {
      viewState,
      entryMode: isEntryMode(record.entryMode) ? record.entryMode : null,
      summary:
        typeof record.summary === 'object' && record.summary !== null
          ? (record.summary as RequirementsSummary)
          : null,
      chatTab: isChatStage(record.chatTab) ? record.chatTab : 'requirement',
      quickInput: parseQuickInput(record.quickInput),
      debugPrdText: typeof record.debugPrdText === 'string' ? record.debugPrdText : '',
      pastePairInput: parsePastePairInput(record.pastePairInput),
      activeJobId,
      activeJobKind: hasActiveJob && isDocKind(record.activeJobKind) ? record.activeJobKind : null,
      // 可选字段：旧数据没有它。缺失时留 null（由使用方按阶段反推），**不要**据此丢掉整份状态。
      activeJobDocKind:
        hasActiveJob && isDocumentKind(record.activeJobDocKind) ? record.activeJobDocKind : null,
      // 对话任务的线程：状态列里没有单独一项，它就是「用户停在的那条线程」——
      // 任务发起时必然停在那条线程上。非对话任务留 null（它没有线程）。
      activeChatStage:
        hasActiveJob && record.activeJobKind === 'chat'
          ? isChatStage(record.chatTab)
            ? record.chatTab
            : 'requirement'
          : null,
    },
  }
}


/**
 * 校验并归一一份「生成用量回执」。
 *
 * 它来自 `documents.last_run_summary`——后端存的是**与 SSE 事件同一个 dict**
 * （`snake_case`）。旧实现在这里直接把对象断言成 `RunSummary` 并按要求必须有
 * `durationMs`，于是库里那份永远被判成脏数据、刷新后回执整块消失。
 * 现在统一走 `normalizeRunSummary`（两种键都认），本函数只保留入口名。
 *
 * 脏数据（非对象 / 缺 `kind` / 连耗时都没有）一律退化为 `null`，
 * 该文档就「不显示回执」，与上线前的行为一致。
 */
export function parseRunSummary(value: unknown): RunSummary | null {
  return normalizeRunSummary(value)
}
