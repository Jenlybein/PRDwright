/**
 * 流程阶段机（纯函数）。
 *
 * 把「阶段 → 进度 / 守卫 / 刷新降级」这些判断从组件里搬出来，做成不依赖 Vue
 * 的纯函数：既能独立单测，也能被四条入口（完整流程 / 快捷流程 / 我有 PRD /
 * 我有 PRD + 接口文档）复用。
 *
 * **入口模式（`EntryMode`）在这里体现为三件事**：进度条展示哪几步、当前在第几步、
 * 以及「进交付页」需要哪几份文档。三件事都由模式推导，组件与 store 不再各写一套判断。
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

/**
 * 「未选择」时按哪个入口模式展示。
 *
 * `null`（用户还没点入口 / 老快照没记入口名）是**合法状态**，不是「未知入口键」：
 * 它必须照常给出答案，不能报错。这条口径此前散在六处各写一次 `?? 'structured'`，
 * 改默认口径时必须逐处找，漏一处的表现只是「进度条少一格」这类不报错的错位。
 * 现在只在这里定义一次。
 */
const UNSELECTED_ENTRY_MODE: EntryMode = 'structured'

/**
 * 把入口模式归一成「可展示的取值」：未选择（`null`）落到 {@link UNSELECTED_ENTRY_MODE}。
 *
 * 这是「未选择」的**唯一出口**——凡是要回答「未选入口时该显示什么」的地方都调它，
 * 不要再写 `?? 'structured'`。它只管**合法状态**；未知入口键的失败路径属于登记表
 * 完整性校验，不在这里。
 */
export function entryModeForDisplay(mode: EntryMode | null): EntryMode {
  return mode ?? UNSELECTED_ENTRY_MODE
}

// ----------------------------------------------------------------------------
// 入口模式登记表
//
// **一行一个入口，七类资料为列**。原来按入口写死的表（进度步骤、三个布尔、完成所需文档、
// 落页、模式名、返回与改需求文案）都改成「查这一行」，旧表已退役。
// 目标形态见 `docs/architecture/ARCHITECTURE.md` §5.8；四个入口各占一行。
// ----------------------------------------------------------------------------

/** ① 键、组别与卡片文案：入口页上那张卡片怎么说 */
export interface EntryCardSpec {
  /** 组别 id（入口页分两组：从零创建 / 导入已有素材） */
  readonly groupId: string
  /** 组标题 */
  readonly groupTitle: string
  /** 组说明 */
  readonly groupDescription: string
  /** 卡片标题（也就是列表里的入口名） */
  readonly title: string
  /** 卡片上的一句说明 */
  readonly description: string
}

/**
 * ② 落页：选中入口后落在哪一页，以及两个「回去」动作跳哪一页。
 *
 * 三者都随入口变化，所以都在这一列；按**视图状态**选渲染组件的链条不在这里。
 */
export interface EntryLandingSpec {
  /** 选中这个入口后直接进入的界面 */
  readonly onSelect: ViewState
  /** 对话页「改需求 / 改描述」回到哪一页 */
  readonly editRequirements: ViewState
  /** 审阅页 / 交付页的「返回」回到哪一页（录入页） */
  readonly backToInput: ViewState
}

/** ⑥ 文案：列表里的模式名、两个「回去」按钮的文案 */
export interface EntryCopySpec {
  /** 方案列表里显示的模式名 */
  readonly modeLabel: string
  /** 审阅页 / 交付页「返回录入」按钮的文案 */
  readonly backLabel: string
  /** 对话页「改需求」按钮的文案 */
  readonly editRequirementsLabel: string
}

/**
 * ⑦ 录入字段：一个字段的登记项。
 *
 * `defaultValue` 是**未填写时的初值**；`countsAsContent` 表示它非空就算「录入页已有内容」
 * （决定要不要为这份录入建一份方案）。
 */
export interface EntryInputField {
  readonly key: string
  readonly label: string
  readonly defaultValue: string
  readonly countsAsContent: boolean
}

/** ⑦ 录入字段：这一行的录入内容挂在哪个字段上、落到快照的哪一列、有哪些字段 */
export interface EntryInputSpec {
  /** 录入内容在 workflow store 上的持有者 */
  readonly stateField: 'summary' | 'quickInput' | 'debugPrdText' | 'pastePairInput'
  /**
   * 快照里承载它的列（`solutions.state.<列>`）；与后端契约同名。
   *
   * 一个入口的录入内容可能落多列（例如快捷流程的产品名与需求描述、双粘贴的两份材料），
   * 这里记**主内容那一列**；各字段的列见 `fields` 的顺序与后端契约。
   */
  readonly stateColumn: string
  readonly fields: readonly EntryInputField[]
}

/**
 * 一个入口模式的全部资料：七类为列，一行填齐才算一个入口。
 *
 * 填不齐不会「凑合着用」：{@link entrySpecForDisplay} 问询时逐列校验，
 * 缺行 / 缺列当场抛 {@link EntrySpecMissingError}。
 */
export interface EntrySpec {
  /** ① 键 */
  readonly mode: EntryMode
  /** ① 组别与卡片文案 */
  readonly card: EntryCardSpec
  /** ② 落页 */
  readonly landing: EntryLandingSpec
  /** ③ 进度条步骤（顺序沿用 `STEPS`） */
  readonly steps: readonly StepId[]
  /** ④ 录入页是否就在进度条上 */
  readonly inputPageOnBar: boolean
  /** ④ 是否需要需求澄清对话 */
  readonly needsRequirementChat: boolean
  /** ④ 是否需要审核 / 改写 */
  readonly needsDocumentReview: boolean
  /**
   * ④ 审阅页的澄清入口出现时，是否**保留**「返回」按钮。
   *
   * 判据是「澄清入口与『返回』是不是同一个落点」：完整流程两者都回对话页，澄清入口顶替「返回」
   * 就够了；其余入口的「返回」是回各自录入页，与澄清入口落点不同，必须并排保留——否则用户
   * 再也改不了自己贴进来的材料。
   *
   * ⚠️「未选择」与完整流程在这一列**不同**：未选择时没有这一条入口的录入页可回，旧行为是
   * 保留「返回」。登记表按入口填，`null` 会先被归一成完整流程，因此调用处显式处理 `null`
   * （见 `AppV2.vue` 的 `keepBackButton`）。
   */
  readonly keepBackButton: boolean
  /** ⑤ 完成所需文档 */
  readonly requiredDocs: readonly DocType[]
  /** ⑥ 文案 */
  readonly copy: EntryCopySpec
  /** ⑦ 录入字段 */
  readonly input: EntryInputSpec
}

/** 结构化表单的字段：与 `RequirementsForm.vue` 的分节一一对应 */
const STRUCTURED_INPUT_FIELDS: readonly EntryInputField[] = [
  { key: 'product_name', label: '产品名称', defaultValue: '', countsAsContent: true },
  { key: 'product_goal', label: '核心目标', defaultValue: '', countsAsContent: true },
  { key: 'target_users', label: '目标用户', defaultValue: '', countsAsContent: true },
  { key: 'platform', label: '平台', defaultValue: '', countsAsContent: true },
  { key: 'mvp_features', label: 'MVP 功能', defaultValue: '', countsAsContent: true },
  { key: 'v2_features', label: '未来规划', defaultValue: '', countsAsContent: true },
  { key: 'ui_pages', label: '页面结构', defaultValue: '', countsAsContent: true },
  { key: 'key_interactions', label: '关键交互', defaultValue: '', countsAsContent: true },
  { key: 'tech_auth', label: '用户认证', defaultValue: '', countsAsContent: true },
  { key: 'tech_storage', label: '数据存储', defaultValue: '', countsAsContent: true },
  { key: 'tech_frontend', label: '前端技术', defaultValue: '', countsAsContent: true },
  { key: 'tech_backend', label: '后端技术', defaultValue: '', countsAsContent: true },
  { key: 'tech_deployment', label: '部署方式', defaultValue: '', countsAsContent: true },
  { key: 'llm_usage', label: 'LLM 使用说明', defaultValue: '', countsAsContent: true },
  { key: 'nf_security', label: '安全要求', defaultValue: '', countsAsContent: true },
  { key: 'nf_performance', label: '性能要求', defaultValue: '', countsAsContent: true },
  { key: 'nf_availability', label: '可用性要求', defaultValue: '', countsAsContent: true },
  { key: 'scope_in', label: '在范围内', defaultValue: '', countsAsContent: true },
  { key: 'scope_out', label: '不在范围内', defaultValue: '', countsAsContent: true },
]

/** 快捷流程的字段：与 `QuickPrdStage.vue` 的两栏一一对应 */
const QUICK_INPUT_FIELDS: readonly EntryInputField[] = [
  { key: 'product_name', label: '产品名称', defaultValue: '', countsAsContent: true },
  { key: 'requirements', label: '需求描述', defaultValue: '', countsAsContent: true },
]

/** 我有 PRD 的字段：与 `PromptDebugStage.vue` 的正文框一一对应 */
const DEBUG_PRD_FIELDS: readonly EntryInputField[] = [
  { key: 'prd', label: 'PRD 正文（Markdown）', defaultValue: '', countsAsContent: true },
]

/** 我有 PRD + 接口文档的字段：与 `PastePairStage.vue` 的两个文本框一一对应 */
const PASTE_PAIR_FIELDS: readonly EntryInputField[] = [
  { key: 'prd', label: 'PRD 正文（Markdown）', defaultValue: '', countsAsContent: true },
  { key: 'api_docs', label: '接口文档正文（Markdown）', defaultValue: '', countsAsContent: true },
]

/**
 * 完整流程这一行。
 *
 * 单独取名是因为它是「未选择」的展示口径落脚点：未选择（`null`）经
 * {@link entryModeForDisplay} 归到 `structured`，再走同一套查表与完整性校验。
 */
const STRUCTURED_ENTRY_SPEC: EntrySpec = {
  mode: 'structured',
  card: {
    groupId: 'from-scratch',
    groupTitle: '从零创建 PRD',
    groupDescription: '还没有任何材料，从这里把需求说清楚。',
    title: '完整流程',
    description: '逐项填表 + 和 AI 对话把细节问清楚，再依次生成三份文档。',
  },
  landing: {
    onSelect: 'form',
    // 对话页「改需求」回到结构化表单；审阅页「返回」回到对话页
    editRequirements: 'form',
    backToInput: 'chatting',
  },
  steps: ['form', 'chatting', 'review-prd', 'review-api-docs', 'review-prompts', 'done'],
  inputPageOnBar: true,
  needsRequirementChat: true,
  needsDocumentReview: true,
  // 澄清入口与「返回」都回对话页：由澄清入口顶替「返回」，不并排
  keepBackButton: false,
  requiredDocs: ['prd', 'api-docs', 'prompts'],
  copy: {
    modeLabel: '完整流程',
    backLabel: '返回对话',
    editRequirementsLabel: '改需求',
  },
  input: {
    // 结构化表单整份落在快照的 `summary` 列（`RequirementsSummary` 对象）
    stateField: 'summary',
    stateColumn: 'summary',
    fields: STRUCTURED_INPUT_FIELDS,
  },
}

/**
 * 入口模式登记表：一行一个入口，四个入口各占一行。
 *
 * 类型是 `Partial<Record<EntryMode, EntrySpec>>`：**缺行不是「默认值」，而是「还没填」**——
 * 类型上保留这种可能，是为了让缺行不被一个悄悄补上的默认值掩盖。问询一律走
 * {@link entrySpecForDisplay}：缺行 / 缺列 / 未知键当场失败，只有「未选择」按完整流程展示。
 */
export const ENTRY_REGISTRY: Partial<Record<EntryMode, EntrySpec>> = {
  structured: STRUCTURED_ENTRY_SPEC,
  'prd-quick': {
    mode: 'prd-quick',
    card: {
      groupId: 'from-scratch',
      groupTitle: '从零创建 PRD',
      groupDescription: '还没有任何材料，从这里把需求说清楚。',
      title: '快捷流程',
      description: '一段话说清想法，跳过表单与对话，直接逐章生成 PRD。',
    },
    landing: {
      onSelect: 'prd-quick',
      // 「改描述」与审阅页的「返回」都回极简录入页：它就是这一条的起点
      editRequirements: 'prd-quick',
      backToInput: 'prd-quick',
    },
    // 与完整流程**同一套六步**：只差第一步的形态。「AI 对话」这一格不能省——
    // 快捷流程没有需求澄清，但文档澄清的三条线程就住在那个对话页上。
    steps: ['form', 'chatting', 'review-prd', 'review-api-docs', 'review-prompts', 'done'],
    inputPageOnBar: true,
    // 用户选它就是因为不想多聊几轮：一段话写完直接生成
    needsRequirementChat: false,
    // 「不审不改」是这一条的定义，三份产物都一样
    needsDocumentReview: false,
    // 「返回」回快捷录入页，与澄清入口（回对话）落点不同，必须并排保留
    keepBackButton: true,
    // 交付判据与完整流程对齐：三份齐备才算完成，不因「快捷」降低门槛
    requiredDocs: ['prd', 'api-docs', 'prompts'],
    copy: {
      modeLabel: '快捷流程',
      backLabel: '返回对话',
      editRequirementsLabel: '改描述',
    },
    input: {
      stateField: 'quickInput',
      stateColumn: 'quick_requirements',
      fields: QUICK_INPUT_FIELDS,
    },
  },
  'prompt-debug': {
    mode: 'prompt-debug',
    card: {
      groupId: 'with-material',
      groupTitle: '导入已有素材',
      groupDescription: '手头已经有写好的材料，别重复劳动。',
      title: '我有 PRD',
      description: '贴一份现成 PRD，直接生成提示词套件，之后可用 AI 优化反复调。',
    },
    landing: {
      onSelect: 'prompt-debug',
      // 与旧版一致；语义不理想（改需求跳结构化表单），但本期行为不变，要改另开票。
      // 导入入口没有需求澄清，对话页也就不会给「改需求」按钮；这一列只为旧快照存在
      // （那次改动之前存的方案里可能带着需求对话）。
      editRequirements: 'form',
      backToInput: 'prompt-debug',
    },
    // 产物只有提示词套件：进度条是「提示词 → 交付」两格
    steps: ['review-prompts', 'done'],
    // 录入页不在进度条上（贴 PRD 不是一格），所以审阅页必须保留「返回」，否则改不了材料
    inputPageOnBar: false,
    // 自带的 PRD 就是答案，不再追问需求
    needsRequirementChat: false,
    // 导入的正文仍要过一遍审核 / 改写
    needsDocumentReview: true,
    // 「返回」回粘贴页，与澄清入口（回对话）落点不同：不保留就成了死路
    keepBackButton: true,
    requiredDocs: ['prompts'],
    copy: {
      modeLabel: '导入 PRD',
      backLabel: '返回 PRD',
      editRequirementsLabel: '改需求',
    },
    input: {
      stateField: 'debugPrdText',
      stateColumn: 'draft_prd_text',
      fields: DEBUG_PRD_FIELDS,
    },
  },
  'prd-and-api-paste': {
    mode: 'prd-and-api-paste',
    card: {
      groupId: 'with-material',
      groupTitle: '导入已有素材',
      groupDescription: '手头已经有写好的材料，别重复劳动。',
      title: '我有 PRD + 接口文档',
      description: '两份都贴进来，提示词因此能对齐真实的接口路径与字段名。',
    },
    landing: {
      onSelect: 'prd-and-api-paste',
      // 与旧版一致；语义不理想（改需求跳结构化表单），但本期行为不变，要改另开票。
      editRequirements: 'form',
      backToInput: 'prd-and-api-paste',
    },
    // 与「我有 PRD」同属导入组：产物一样只有提示词套件，进度条口径就该一样
    steps: ['review-prompts', 'done'],
    inputPageOnBar: false,
    needsRequirementChat: false,
    needsDocumentReview: true,
    // 同「我有 PRD」：两份材料的粘贴页只能在「返回」后面，澄清入口不能顶替它
    keepBackButton: true,
    requiredDocs: ['prompts'],
    copy: {
      modeLabel: '导入 PRD + 接口文档',
      backLabel: '返回素材',
      editRequirementsLabel: '改需求',
    },
    input: {
      stateField: 'pastePairInput',
      stateColumn: 'paste_prd_text',
      fields: PASTE_PAIR_FIELDS,
    },
  },
}

/**
 * 登记表完整性校验失败。
 *
 * 三种「没有」里只有「未选择」（`null` / 空串）是合法的；**未知入口键、缺行、缺列**
 * 都是维护者漏干活，必须在问询那一刻炸出来。从前查不到就按完整流程兜住，于是
 * 「加了第 5 个入口却忘了填一列」的表现是「悄悄按完整流程跑下去」，没有任何报错。
 *
 * 消息里带**入口键**与**缺的那一列**（列名用代码里的字段路径，例如 `requiredDocs`、
 * `copy.modeLabel`），维护者一眼能定位到该补哪一行哪一列。
 */
export class EntrySpecMissingError extends Error {
  /** 问询时用的入口键 */
  readonly key: string
  /** 缺的是哪一列；缺整行时为 `null` */
  readonly column: string | null

  constructor(key: string, column: string | null, reason: string) {
    super(
      column === null
        ? `入口登记表「${key}」缺这一行：${reason}`
        : `入口登记表「${key}」缺列 ${column}：${reason}`,
    )
    this.name = 'EntrySpecMissingError'
    this.key = key
    this.column = column
  }
}

/** 七类资料里「某一列」的规范取值（枚举列用它校验，避免拼错一个字符串就当成配好了） */
const ENTRY_SPEC_STATE_FIELDS: readonly EntryInputSpec['stateField'][] = [
  'summary',
  'quickInput',
  'debugPrdText',
  'pastePairInput',
]

/** 完成所需文档只能是这三份产物 */
const ENTRY_SPEC_DOC_TYPES: readonly DocType[] = ['prd', 'api-docs', 'prompts']

/** 报「缺列」并终止；`column` 为 `null` 表示缺的是整行 */
function missingEntrySpec(key: EntryMode, column: string | null, reason: string): never {
  throw new EntrySpecMissingError(key, column, reason)
}

/** 某一列必须是非空文案：空串、全空白、`undefined` 都算没填 */
function requireEntrySpecText(key: EntryMode, column: string, value: unknown): void {
  if (typeof value !== 'string' || value.trim() === '') {
    missingEntrySpec(key, column, '必须是非空文案；空串等于没填')
  }
}

/** 某一列必须是布尔：用 `undefined` / `null` 糊过去会让「要不要这一环」变成碰运气 */
function requireEntrySpecBoolean(key: EntryMode, column: string, value: unknown): void {
  if (typeof value !== 'boolean') {
    missingEntrySpec(key, column, '必须是 true / false；不能留空')
  }
}

/** 某一列必须是非空数组（步骤、完成所需文档、录入字段） */
function requireEntrySpecList(key: EntryMode, column: string, value: unknown): readonly unknown[] {
  if (!Array.isArray(value) || value.length === 0) {
    missingEntrySpec(key, column, '必须是非空数组；空数组等于没填')
  }
  return value
}

/**
 * 校验一行登记表填齐了没有；填齐了原样返回，缺任何一列都抛 {@link EntrySpecMissingError}。
 *
 * 七类资料逐列检查，判据都是**「这一列有没有真的填上」**，不涉及业务对错：
 * 步骤不能是空数组、完成所需文档不能是空数组、文案不能是空串、三个布尔必须是布尔、
 * 落页必须是已定义的视图状态、录入字段不能空。
 *
 * 这是本票守卫用例的注入点：喂一份只填一半的假第 5 入口进来，它必须当场拒绝。
 */
export function assertEntrySpecComplete(
  key: EntryMode,
  spec: EntrySpec | undefined | null,
): EntrySpec {
  if (!spec) {
    missingEntrySpec(key, null, '未登记这个入口键；不猜默认值，请补上这一行')
  }

  // ① 键：行里的键必须就是查它的那个键，否则「查 A 得 B」比缺行更难查
  if (spec.mode !== key) {
    missingEntrySpec(key, 'mode', `这一行登记的键是「${String(spec.mode)}」，与查的键对不上`)
  }

  // ① 组别与卡片文案：入口页那张卡片的四段文字
  const card = spec.card
  if (!card) missingEntrySpec(key, 'card', '整列缺失')
  requireEntrySpecText(key, 'card.groupId', card.groupId)
  requireEntrySpecText(key, 'card.groupTitle', card.groupTitle)
  requireEntrySpecText(key, 'card.groupDescription', card.groupDescription)
  requireEntrySpecText(key, 'card.title', card.title)
  requireEntrySpecText(key, 'card.description', card.description)

  // ② 落页：三个「去哪一页」都必须是已定义的视图状态
  const landing = spec.landing
  if (!landing) missingEntrySpec(key, 'landing', '整列缺失')
  for (const field of ['onSelect', 'editRequirements', 'backToInput'] as const) {
    if (!isViewState(landing[field])) {
      missingEntrySpec(key, `landing.${field}`, '必须是已定义的视图状态')
    }
  }

  // ③ 进度条步骤：非空，且每个 id 都得是全局 `STEPS` 里定义过的
  const steps = requireEntrySpecList(key, 'steps', spec.steps)
  const knownStepIds = new Set(STEPS.map((step) => step.id))
  for (const stepId of steps) {
    if (!knownStepIds.has(stepId as StepId)) {
      missingEntrySpec(key, 'steps', `「${String(stepId)}」不是已定义的步骤`)
    }
  }

  // ④ 四个布尔：录入页是否在进度条上 / 是否需需求澄清 / 是否需审核改写 / 澄清入口旁是否保留「返回」
  requireEntrySpecBoolean(key, 'inputPageOnBar', spec.inputPageOnBar)
  requireEntrySpecBoolean(key, 'needsRequirementChat', spec.needsRequirementChat)
  requireEntrySpecBoolean(key, 'needsDocumentReview', spec.needsDocumentReview)
  requireEntrySpecBoolean(key, 'keepBackButton', spec.keepBackButton)

  // ⑤ 完成所需文档：非空，且只能是三份产物之一
  const requiredDocs = requireEntrySpecList(key, 'requiredDocs', spec.requiredDocs)
  for (const docType of requiredDocs) {
    if (!ENTRY_SPEC_DOC_TYPES.includes(docType as DocType)) {
      missingEntrySpec(key, 'requiredDocs', `「${String(docType)}」不是已定义的产物`)
    }
  }

  // ⑥ 文案：列表模式名 + 两个「回去」按钮
  const copy = spec.copy
  if (!copy) missingEntrySpec(key, 'copy', '整列缺失')
  requireEntrySpecText(key, 'copy.modeLabel', copy.modeLabel)
  requireEntrySpecText(key, 'copy.backLabel', copy.backLabel)
  requireEntrySpecText(key, 'copy.editRequirementsLabel', copy.editRequirementsLabel)

  // ⑦ 录入字段：内容挂在哪个字段上、落快照哪一列、有哪些字段
  const input = spec.input
  if (!input) missingEntrySpec(key, 'input', '整列缺失')
  if (!ENTRY_SPEC_STATE_FIELDS.includes(input.stateField)) {
    missingEntrySpec(key, 'input.stateField', '不是已登记的录入内容持有者')
  }
  requireEntrySpecText(key, 'input.stateColumn', input.stateColumn)
  const fields = requireEntrySpecList(key, 'input.fields', input.fields)
  for (const field of fields as readonly EntryInputField[]) {
    requireEntrySpecText(key, 'input.fields.key', field?.key)
    requireEntrySpecText(key, 'input.fields.label', field?.label)
    requireEntrySpecBoolean(key, 'input.fields.countsAsContent', field?.countsAsContent)
  }

  return spec
}

/**
 * 查这一行登记表；查不到返回 `undefined`。
 *
 * 未选择（`null`）先经 {@link entryModeForDisplay} 归一成完整流程再查——「未选择」是
 * 合法状态，不是缺行。
 *
 * 返回 `undefined` 只表示**没有这一行**（留给「有没有登记」这类内部判断）；真查到一行时
 * 仍会跑一遍完整性校验，残缺的行与缺行一样当场失败，不会让半个配置流出去。
 */
export function entrySpecFor(mode: EntryMode | null): EntrySpec | undefined {
  const key = entryModeForDisplay(mode)
  const spec = ENTRY_REGISTRY[key]
  return spec ? assertEntrySpecComplete(key, spec) : undefined
}

/**
 * 取这一行登记表，**一定给出一行**；给不出就当场失败。
 *
 * 这是「按入口模式问答案」的唯一出口，三态口径在这里收口：
 * - `null`（「未选择」）= 合法，经 {@link entryModeForDisplay} 落到完整流程那一行；
 * - **未知入口键 / 缺行** → 抛 {@link EntrySpecMissingError}，绝不拿默认值顶替；
 * - **缺列**（这一行在，但某一列没填）→ 同样抛错，消息指出缺的是哪一列。
 *
 * 从前这里查不到就按「未选择」兜住，于是维护者加第 5 个入口漏填一列时，程序会
 * 悄悄按完整流程跑下去——正是本票要根治的那种「漏改不报错」。
 */
export function entrySpecForDisplay(mode: EntryMode | null): EntrySpec {
  const key = entryModeForDisplay(mode)
  return assertEntrySpecComplete(key, ENTRY_REGISTRY[key])
}

/**
 * 这个入口有没有「需求澄清」那条对话。
 *
 * 只有**完整流程**有：它的起点是「先把需求交给 AI 逐题追问」。快捷流程刻意不要这一环——
 * 用户选它就是因为不想多聊几轮，一段话写完直接进生成；两个导入入口自带的 PRD 已经是答案。
 *
 * 写成白名单（只有完整流程是 `true`）而不是「不等于某些入口」：将来加新入口时，默认「没有需求
 * 对话」比默认「有」安全得多（多给一条点进去只能自说自话的线程，比少给一条更糟）。
 *
 * 答案在登记表的 `needsRequirementChat` 列。
 */
export function hasRequirementChat(mode: EntryMode | null): boolean {
  return entrySpecForDisplay(mode).needsRequirementChat
}

/**
 * 这个入口的产物是否带「审核 / 改写」环节。
 *
 * 只有**快捷流程**不要：它的定义就是「不审不改」，三份产物都一样。
 * 除了跳过调用，界面上也不能留这两个阶段——进度条上画两格永远走不到的路，
 * 用户会读成「还没审到」而不是「这次压根不审」。
 *
 * 写成黑名单（只有 `prd-quick` 是 `false`）而不是白名单：将来加新入口默认**带上复核**，
 * 漏配的代价只是多等一会儿；反过来则悄悄把产出质量降了一档。
 *
 * 答案在登记表的 `needsDocumentReview` 列。
 */
export function hasDocumentReview(mode: EntryMode | null): boolean {
  return entrySpecForDisplay(mode).needsDocumentReview
}

/**
 * 这个入口的「录入页」是不是就在进度条上（点第 1 步就能回去）。
 *
 * 两个从零创建的入口是：`structured` 与 `prd-quick` 的第 1 步都是「描述产品」，只是
 * 录入页长什么样由模式决定（结构化表单 / 一段话）。
 *
 * 两个**导入**入口不是——它们的录入页（贴 PRD / 贴两份材料）根本不在进度条上，
 * 因此审阅页必须保留一个「返回」按钮，否则用户改不了贴进来的材料，直接成了死路。
 *
 * 答案在登记表的 `inputPageOnBar` 列。
 */
export function inputPageOnBar(mode: EntryMode | null): boolean {
  return entrySpecForDisplay(mode).inputPageOnBar
}

/**
 * 某个入口模式下进度条要展示的步骤。
 * `mode` 为 `null`（还没选入口 / 旧快照）时经 {@link entryModeForDisplay} 按完整流程处理。
 *
 * 步骤清单在登记表的 `steps` 列；顺序由全局的 `STEPS` 决定，这一列只回答「要不要」。
 */
export function stepsForMode(mode: EntryMode | null): readonly Step[] {
  const ids = entrySpecForDisplay(mode).steps
  return STEPS.filter((step) => ids.includes(step.id))
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
  /**
   * 本入口所需文档是否齐备（判据见 {@link hasRequiredDocuments}）。
   *
   * 它是「交付」那一格的**唯一**解锁条件：齐备即可进入交付页，未齐备时那一格点不动。
   * 缺省为 `false`，因此只关心「产物走到哪了」的调用方不必知道它。
   */
  deliveryReady?: boolean
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
    // 「交付」不按产物解锁：文档齐备只代表“可以交付”，不代表已经进了交付页。
    // 它由 {@link reachedStepIndex} 按「所需文档齐备」单独放开——这样列表里的
    // 「这份做到哪了」仍停在最后一份产物上，而进度条那一格已经能点。
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
 *
 * 另有两处例外：
 * - 当前就在 `done`（用户已经进了交付页）时当然可点；
 * - **所需文档齐备时「交付」那一格就可点**（`deliveryReady`）。交付页不再由「完成」按钮
 *   把关，进度条那一格就是它的入口，所以它的解锁条件不是「产物走到哪」而是「齐备没有」。
 *   齐备却没去过交付页时，`farthestContentStepIndex` 仍停在最后一份产物上——列表里的
 *   「这份做到哪了」不该因为“可以交付”就自称“已交付”。
 */
export function reachedStepIndex(input: ReachedStepInput): number {
  const content = farthestContentStepIndex(input)
  const reached = Math.max(content, input.currentStepIndex)
  if (!input.deliveryReady) return reached
  const deliveryIndex = stepsForMode(input.mode).findIndex((step) => step.id === 'done')
  return Math.max(reached, deliveryIndex)
}

/** 对话是否已具备「生成 PRD」的条件：至少一轮 AI 回复，且当前没有进行中的请求 */
export function canGeneratePrdFrom(messages: readonly ChatMessage[], busy: boolean): boolean {
  if (busy) return false
  return messages.some((message) => message.role === 'assistant' && message.content.trim() !== '')
}

/**
 * 当前入口模式下「交付」所需的文档是否齐备。
 *
 * 各模式的交付物不同：完整流程与快捷流程都要三份齐全，两个导入入口只要提示词。
 * 用统一的口径判断，可以避免「只想拿一份文档的人被另外两份挡住」。
 *
 * 它是**交付页的判据**（进度条「交付」格的可点条件），不再是「完成按钮」的判据：
 * 齐备即可进入交付页，没有别的关卡。
 *
 * 要哪几份文档在登记表的 `requiredDocs` 列。
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
  return entrySpecForDisplay(mode).requiredDocs.every((type) => byType[type].trim() !== '')
}

/**
 * 「所需文档已齐备，可以进交付页了」这句提示的文案。
 *
 * 三份齐备的入口说得出口「三份」；两个导入入口只要一份提示词，对它们说「三份文档已齐备」
 * 就是假的，因此按登记表 `requiredDocs` 的实际份数措辞。
 *
 * 这份文案只出现在**全应用浮层**里（`AppV2` 的那次提示），不改变当前页面。
 */
export function deliveryReadyNotice(mode: EntryMode | null): string {
  return entrySpecForDisplay(mode).requiredDocs.length > 1
    ? '三份文档已齐备，可在进度条进入交付页'
    : '提示词套件已生成，可在进度条进入交付页'
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
