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

/**
 * 一条消息属于哪条澄清线程；同时也是对话页子标签栏的标识。
 *
 * 四条线程（需求澄清 / PRD 澄清 / 接口澄清 / 提示词澄清）**共用同一个 `messages` 数组**，
 * 靠这个字段区分归属：
 *
 * - **摘要回填只吃 `requirement` 的消息**，否则文档答疑会被当成需求输入，闸门①的 Diff
 *   里凭空冒出一堆变更；
 * - 对话页也只渲染**当前标签那一条线程**的消息——切换标签就是切换过滤条件。
 *
 * 缺省视为 `requirement`：旧快照的消息没有这个字段，而它们都产生于本字段出现之前的需求澄清。
 */
export type ChatStage = 'requirement' | 'prd-clarify' | 'api-docs-clarify' | 'prompts-clarify'

/** 全部澄清线程；顺序即子标签栏的展示顺序 */
export const CHAT_STAGES: readonly ChatStage[] = [
  'requirement',
  'prd-clarify',
  'api-docs-clarify',
  'prompts-clarify',
]

/**
 * 子标签栏上的展示名。
 *
 * 标签**常驻**（未就绪的置灰、不可点），不因为它还没发生就消失——否则用户不知道
 * 后面还有哪些可以聊。
 */
export const CHAT_STAGE_LABELS: Record<ChatStage, string> = {
  requirement: '需求澄清',
  'prd-clarify': 'PRD 澄清',
  'api-docs-clarify': '接口澄清',
  'prompts-clarify': '提示词澄清',
}

/** 聊天消息，角色取值与后端 conversation_service 的 FrontendMessage 一致 */
/** 结构化选择题里的一个选项。`label`（A/B/C…）由后端分配，卡片上只显示 `text` */
export interface ClarificationAskOption {
  /** 选项标号，形如 `A` */
  label: string
  /** 选项的短标题（只写名字，≤16 字） */
  text: string
  /**
   * 一句话说明「选它意味着什么 / 代价是什么」。
   *
   * **卡片靠它让用户看出几个选项的差别**——只给标题时用户等于没得选。模型漏写时
   * 后端给空串（不让这一轮失败），历史数据里也可能整个键不存在，因此渲染前按空处理。
   */
  description?: string
}

/**
 * 澄清对话里「一次提问」的结构化形式。
 *
 * 模型通过**调用 `ask_clarification_questions` 工具**输出题目，后端从工具调用参数里取出、
 * 校验并分配 id/label，随 SSE 的 `done` 下发（见 `services/clarification_ask.py`）。
 * 有它就把选项渲染成可点的卡片；**没有它就是普通文本提问——不调工具是常态，不是异常**。
 */
export interface ClarificationAsk {
  /** 由后端分配的 id，渲染 key 用 */
  id: string
  /** 题干；**卡片自己显示**，不指望正文再复述一遍 */
  question: string
  /** 一句背景（为什么问）；可为空串。卡片显示在题干下方 */
  background: string
  /** 是否多选 */
  multiple: boolean
  options: ClarificationAskOption[]
  /**
   * 产出这道题的那次工具调用 id；**缺失时整个键不下发**（绝不给空串）。
   *
   * 作答回灌靠它把答案对回当初提问的那次工具调用。上游没给调用 id 时后端不下发这个键，
   * 前端据此**降级为纯文本作答**（不带 `tool_result`）——绝不发一个配不上的 id。
   */
  tool_call_id?: string
}

/** 作答回灌里「一道题的答案」 */
export interface ClarificationAnswerItem {
  /** 题目 id（后端分配，与 `ClarificationAsk.id` 同源） */
  ask_id: string
  /** 选中的选项标号；**按选项展示顺序**，不按点选顺序 */
  labels: string[]
  /** 题干；模型要能把它对回自己问的那道题 */
  question: string
  /** 选中选项的文本；与 `labels` 同序、逐项对应 */
  texts: string[]
}

/**
 * 作答回灌的结构化载荷：一次「回答选择题」对应一条工具结果。
 *
 * 随 `continue-stream` 的 `tool_result` 下发；不带它时就是普通续聊。
 */
export interface ToolResultPayload {
  /** 要回答的工具调用 id（随 `done` 下发） */
  tool_call_id: string
  /** 逐题作答；允许只答一部分 */
  answers: ClarificationAnswerItem[]
}

/** assistant 消息携带的一次工具调用，形如 `[{id, name, args}]` */
export interface ToolCallRef {
  id: string
  name: string
  args: Record<string, unknown>
}

export interface ChatMessage {
  /**
   * 消息角色；`system` 消息不展示在聊天界面里。
   *
   * ⚠️ 这里**没有** `tool`：工具应答（`ToolMessage`）是后端拿请求体里的 `tool_result`
   * 自己落的，前端不产生、也不需要回传这类消息。放进来只会造成「类型说可以、
   * 快照恢复时却被 `isChatMessage` 的白名单整条丢掉」的不一致——那种不一致不报错，
   * 表现为刷新一次消息就没了。
   */
  role: 'system' | 'user' | 'assistant'
  /** 消息正文；assistant 的消息为 Markdown 源码 */
  content: string
  /** 是否正在流式生成中，用于显示光标 */
  streaming?: boolean
  /**
   * 属于哪一轮澄清；**缺省视为 `requirement`**，旧快照（没有这个字段）因此照样可用。
   */
  stage?: ChatStage
  /**
   * 这条助手消息附带的**结构化选择题**；没有就**不带这个字段**（而不是空数组）。
   *
   * 挂在消息上而不是另开一个快照字段：消息经 `parseSessionSnapshot` 是整对象展开的，
   * 未知字段会原样留下，所以刷新恢复天然带着它；而给 `SessionDataV2` 加顶层字段要同步
   * 四处快照逻辑，还得改契约测试的字段清单。
   *
   * ⚠️ 「没有时省略」是有意的：几条既有用例按 `toEqual({role, content, stage})` 精确断言
   * 消息对象，多一个 `asks: []` 就会把它们弄红——而那种红会让人误以为「不带选项也行」。
   */
  asks?: ClarificationAsk[]
  /**
   * 这条助手消息发起的**工具调用**；没有就**不带这个字段**。
   *
   * 与 `asks` 同源（题目就是 `ask_clarification_questions` 这次调用的参数），
   * 原样带回是后端配对工具应答的**唯一依据**：有了它，后端才能把随后的用户文本
   * 包成工具应答、插到发起调用的那条 assistant 消息之后；裁掉它，历史里就只剩
   * 「调了工具却没人应答」的悬空调用，上游 API 直接报错。
   */
  tool_calls?: ToolCallRef[]
  /**
   * 这条**用户消息**是对哪一轮选项卡的作答；自由文本作答**不带这个字段**。
   *
   * 它与 `content` 是同一件事的两种写法：`content` 是发给后端的展示文本
   * （降级时模型读到的也是它），`answers` 只服务于**界面呈现**——气泡里那段
   * 「1. 题干 / 答：A. xxx」既啰嗦又像模型在说话，所以带了这个字段的消息
   * 改用一张轻回执卡：**题干压到两行 + 选中的「标号 + 标题」**。
   *
   * 题干与标题都要留着：卡片一作答就整块消失（见 `ClarificationAskList`），
   * 回执是用户回看时唯一的上下文来源；只留「你选了 A」，隔一会儿就没人记得
   * 「A」是什么了。
   *
   * ⚠️ 只在真有值时挂键（空数组会让既有的 `toEqual({role, content, stage})`
   * 精确形状断言变红）；它不进请求体，纯前端渲染用。
   */
  answers?: ClarificationAnswerItem[]
}

/**
 * 页面所处的阶段。
 * `generating-*` 表示文档正在生成中，`review-*` 表示文档已生成、待用户确认。
 * `summary-diff` 是一道**确认闸门**：生成 PRD 前先让用户确认「对话中补充的信息回填到摘要」的结果，
 * 确认后才继续生成。
 *
 * 四个入口相关的阶段（入口选择页把它们分成两组，见 `EntryStage.vue`）：
 * - `entry`：入口选择页（还没选走哪条入口）；
 * - `prd-quick`：「快捷流程」的录入页（一段话描述 → 直接生成 PRD）；
 * - `prompt-debug`：「我有 PRD」的录入页（贴一份 PRD → 生成提示词套件）；
 * - `prd-and-api-paste`：「我有 PRD + 接口文档」的录入页（贴两份材料 → 生成提示词套件）。
 */
export type ViewState =
  | 'entry'
  | 'form'
  | 'chatting'
  | 'summary-diff'
  | 'generating-prd'
  | 'review-prd'
  | 'generating-api-docs'
  | 'review-api-docs'
  | 'generating-prompts'
  | 'review-prompts'
  | 'prd-quick'
  | 'prompt-debug'
  | 'prd-and-api-paste'
  | 'done'

/**
 * 入口模式：四条链路的起点不同，但汇合到同一套「生成 → 审阅 → 下载」流程。
 *
 * 入口页把它们分成两组（`EntryStage.vue`）：
 *
 * - **从零创建 PRD**
 *   - `structured`：完整流程——结构化表单 → AI 对话澄清 → PRD → 接口文档 → 提示词；
 *   - `prd-quick`：快捷流程——一段话描述 + 产品名，跳过表单与对话，直接生成 PRD。
 * - **导入已有素材**
 *   - `prompt-debug`：我有 PRD——贴进来直接生成提示词套件，再用「AI 优化」反复迭代；
 *   - `prd-and-api-paste`：我有 PRD + 接口文档——两份材料都贴进来生成提示词套件，
 *     提示词因此能对齐真实的接口路径与字段。
 *
 * 模式只决定「输入怎么来、进度条显示哪几步、需要哪几份文档才算完成」，
 * 不决定文档怎么写——写作规范始终来自 `skills/**` 里的技能包。
 *
 * ⚠️ `prompt-debug` 这个名字是历史遗留（它起初叫「我有 PRD」，靠反复 AI 优化调参），
 * 现在对外的说法是「我有 PRD」。**键名不改**：快照里存着它，改名要配一套兼容旧值的迁移，
 * 而收益只是好看一点——真正会误导人的是界面文案，那个已经改了。
 */
export type EntryMode =
  | 'structured'
  | 'prd-quick'
  | 'prompt-debug'
  | 'prd-and-api-paste'

/**
 * 生成任务的文档种类（与后端 `generation_jobs.kind` 一一对应）。
 *
 * - `prd` / `api-docs` / `prompts`：三份文档的**整篇生成**；
 * - `optimize`：按用户指令对某一份已有文档的就地优化；
 * - `clarify`：按澄清对话对某一份已有文档的就地改写；
 * - `auto-clarify`：AI 自行拍板待确认问题后就地改写（用户不出面）。
 *
 * 后三者是「对已有文档做的一件事」，因此**必须**额外带 `docKind` 说明作用在哪一份上——
 * 少了它后端不会猜默认值，而是拒绝落库。
 *
 * 注意命名口径：后端用下划线（`api_docs` / `auto_clarify`），前端用连字符，
 * 转换收口在 `services/api.ts`。
 */
export type DocKind = 'prd' | 'api-docs' | 'prompts' | 'optimize' | 'clarify' | 'auto-clarify'

/**
 * **产物**种类：三份文档各自的身份，与 `DocKind` 的差别在于它不含 `optimize`。
 *
 * 这个区分在「文档所有权后端化」之后变得要紧：`DocKind` 描述的是「做了一件什么事」
 * （生成 / 优化是两类动作），而 `DocumentKind` 描述的是「东西是哪一份」。
 * 落库、取回执、建立入口映射用的都是后者。
 */
export type DocumentKind = 'prd' | 'api-docs' | 'prompts'

/**
 * 待确认项的「重要性等级」。
 *
 * P0 最关键（不确认会导致整份文档写错或没法开工），数字越大越可以后置，直到 P6。
 * 由模型在生成正文时随清单标出；后端**逐项校验**——等级缺失或超出 `P0` ~ `P6` 的项
 * 会被直接丢弃（不兜底），因此前端拿到的每一项都带着合法等级。排序（P0 → P6）同样由
 * 后端完成，所以**数组的顺序就是提问顺序**——越靠前的越先被问到。
 */
export type PendingImportance = 'P0' | 'P1' | 'P2' | 'P3' | 'P4' | 'P5' | 'P6'

/**
 * 一条「待确认问题」。
 *
 * 由后端在生成收尾时从正文里剥离标记块、解析、按重要性排序并重新分配 id 得出——前端
 * 只负责展示，不解析、不编造。字段与 `backend.services.pending_items.extract` 产出的
 * 形状一致：
 *
 * - `id`：后端分配的全文档唯一标识（`p-1` / `overview-1` …），**按重要性排序后**分配，
 *   所以序号越小越重要；仅用于稳定渲染 key；
 * - `question`：一条待确认的问题（必填）；
 * - `sections`：受影响的章节编号数组（可为空）；
 * - `decider`：拍板人（可为空字符串）；
 * - `importance`：重要性等级；后端保证是 `P0` ~ `P6` 之一（缺失或非法的项在收尾校验时已被丢弃）。
 */
export interface PendingItem {
  /** 后端分配的全文档唯一 id（仅用于渲染 key） */
  id: string
  /** 一条待确认的问题 */
  question: string
  /** 受影响的章节编号数组，如 `["2.1", "6.1"]` */
  sections: string[]
  /** 拍板人；未指明时为空串 */
  decider: string
  /** 重要性等级；后端保证一定是 `P0` ~ `P6` 之一 */
  importance: PendingImportance
}

/**
 * 一份上游文档在被下游引用时的锚点。
 *
 * 记的是「生成那一刻的版本号 + 内容指纹」：只比版本号抓不到手改（就地改当前版本，
 * `version_no` 不变），所以真正用于判断过期的是 `contentSha`。
 */
export interface DerivedUpstream {
  versionNo: number
  contentSha: string
}

/** 下游文档的上游锚点表：键是上游产物（`prd` / `api_docs`） */
export type DerivedFrom = Record<string, DerivedUpstream>

/**
 * 从后端取回的一份文档（`GET /api/solutions/{id}/documents` 的一项）。
 *
 * 正文、回执都由后端产出：正文来自任务收尾时的 `done.reply`，回执来自同一次收尾的
 * `run_summary`。前端不再自己拼一份存起来——两份真源迟早会对不上。
 */
export interface DocumentRecord {
  /** 产物种类（前端口径，连字符） */
  kind: DocumentKind
  /** 文档正文（Markdown 原文） */
  content: string
  /** 最后一次生成的用量回执；后端尚未写过或记录不可信时为 null */
  runSummary: RunSummary | null
  /**
   * 本次生成的待确认清单。
   *
   * 与正文同源、同一次收尾落库——后端在剥离正文标记块时一并算出，前端无需自己重算，
   * 因此不存在「换了正文却忘了重算清单」的可能。
   */
  pendingItems: PendingItem[]
  /** 最近更新时间（UTC ISO 8601） */
  updatedAt: string
  /** 这份下游文档是否基于旧版上游（后端按 `derivedFrom` 的指纹比较得出） */
  stale: boolean
  /** 生成时的上游锚点；PRD 或旧数据为 null */
  derivedFrom: DerivedFrom | null
}

// ============================================================================
// 文档版本（document_versions）：存为新版本 / 看历史 / 恢复
// ============================================================================

/**
 * 一条文档版本的**摘要**（`GET .../documents/{kind}/versions` 的一项）。
 *
 * 刻意**不含正文**：版本列表只给概览（第几版、怎么来的、什么时候、多长），
 * 正文只在看单个版本详情时按需取——列表可能很长，把正文都拖出来是纯浪费。
 */
export interface DocumentVersionSummary {
  /** 版本 id；恢复时要引用它 */
  versionId: number
  /** 版本序号，从 1 开始、单调递增 */
  versionNo: number
  /**
   * 这一版的来源（后端原始取值）。
   *
   * 整篇生成是产物类型（`prd` / `api_docs` / `prompts`），就地改写是动作类型
   * （`optimize` / `clarify`），此外还有 `manual`（手改）/ `checkpoint`（存为新版本）/
   * `restore`（恢复）/ `migrated`（迁移回填）。类型保持 `string`：后端可能新增取值，
   * 前端展示层对认不出的来源原样显示，不因此丢掉这一条。
   */
  source: string
  /** 「存为新版本」时用户写的备注；其它来源为空串 */
  note: string
  /** 内容指纹（排查用，不展示） */
  contentSha: string
  /** 正文字符数（概览用） */
  contentLength: number
  /** 创建时间（UTC ISO 8601） */
  createdAt: string
  /** 是否为当前版本 */
  isCurrent: boolean
}

/** 一条文档版本的**完整内容**（`GET .../versions/{version_id}`）。 */
export interface DocumentVersionDetail {
  /** 版本 id */
  versionId: number
  /** 版本序号 */
  versionNo: number
  /** 该版本文档正文（Markdown 原文） */
  content: string
  /** 该版本当时的待确认清单 */
  pendingItems: PendingItem[]
  /** 该版本当时的用量回执；缺失为 null */
  runSummary: RunSummary | null
  /** 这一版的来源（见 `DocumentVersionSummary.source`） */
  source: string
  /** 备注 */
  note: string
  /** 创建时间（UTC ISO 8601） */
  createdAt: string
  /** 最近更新时间（UTC ISO 8601） */
  updatedAt: string
  /** 是否为当前版本 */
  isCurrent: boolean
}

/** 快捷流程入口（「快捷流程」）的录入内容 */
export interface QuickPrdInput {
  /** 产品名称；为空时文档头退化为「未命名产品」 */
  productName: string
  /** 用户用自然语言写下的一段话需求 */
  requirements: string
}

/**
 * 「我有 PRD + 接口文档」入口的两份粘贴内容。
 *
 * 收成一个对象而不是两个散字段，与 `QuickPrdInput` 同一套写法：刷快照时少一个字段
 * 就少一次「改了这个忘了存那个」，解析时也能在一处兜底。
 */
export interface PastePairInput {
  /** 贴入的 PRD 正文（Markdown） */
  prd: string
  /** 贴入的接口文档正文（Markdown）；可以为空——两份材料里只有 PRD 是必填 */
  apiDocs: string
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
  { id: 'done', label: '交付' },
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
 * 例如 我有 PRD入口只有「提示词」这一步（它自带 PRD，不需要「描述产品 / AI 对话」）。
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
  'generating-api-docs': 'review-api-docs',
  'review-api-docs': 'review-api-docs',
  'generating-prompts': 'review-prompts',
  'review-prompts': 'review-prompts',
  // 快捷入口和完整流程的起点是同一件事——「录入需求」，只是录入的形态不同
  // （极简一段话 / 结构化表单）。它因此同样落在进度条第一步。
  'prd-quick': 'form',
  // 两个导入入口的录入产物都是提示词套件（它们只差「带了哪些材料」）
  'prompt-debug': 'review-prompts',
  'prd-and-api-paste': 'review-prompts',
  done: 'done',
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
// 摘要回填 Diff
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

/** 一条审核问题（PRD 单章审核与下游整篇审核共用展示结构） */
export interface ReviewIssue {
  severity: string
  /** 问题所在章节 id（PRD 用；下游整篇审核可能为空） */
  sectionId?: string
  /** 问题所在章节名（展示用） */
  sectionTitle?: string
  detail: string
}

/** 一次失败的章节（PRD 并行生成用；下游恒为空） */
export interface FailedSection {
  sectionId: string
  title: string
  reason: string
}

/**
 * 一次审核结论。
 *
 * **不持久化**：只在本次生成订阅内展示，刷新即失。因此不进 `runSummary`、不进
 * `documents.metadata`——那会变成第二个真源，且与“刷新后结论消失”的预期打架。
 */
export interface ReviewConclusion {
  /** `prd` / `api_docs` / `prompts` */
  kind: string
  /** `pass` / `revise`（空串表示未审成） */
  verdict: string
  summary: string
  issues: ReviewIssue[]
  /** 本次是否真的执行了改写（下游「有问题 → 改写一次」） */
  rewritten: boolean
  failedSections: FailedSection[]
}

/** 下游文档（接口文档 / 提示词）生成中的阶段 */
export type DocPhase = 'idle' | 'writing' | 'reviewing' | 'rewriting' | 'done'

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
  /**
   * 是否整段替换当前预览。
   *
   * 下游文档「审核发现问题 → 改写一次」时，改写稿用 `replace: true` 覆盖初稿；
   * PRD 的定稿段不带这个标记（按章节归位，见 `index`）。
   */
  replace?: boolean
  /** 是否为失败占位（并行链路）；前端据此停在失败车道并标红 */
  failed?: boolean
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

/**
 * 阶段内的**原始输出增量**，由 `stage_chunk` 事件下发。
 *
 * 与正文 `chunk` 是两条通道：`chunk` 描述产物正文（会拼进文档预览），
 * `stage_chunk` 描述当前这一步模型正在打的字（审核意见 / 改写稿），
 * 前端只在阶段条下方的一个附属面板里滚动展示，让「正在审核」有可感知的进展。
 */
export interface StreamStageChunk {
  /** 属于哪个阶段：`reviewing` / `rewriting` */
  stage: string
  /** 本次增量文本 */
  text: string
}

// ============================================================================
// 生成任务（generation_jobs）：刷新重连的状态载体
// ============================================================================

/** 一章在并行流水线里的阶段（4 列看板只按它分组） */
export type PrdSectionPhase =
  | 'pending'
  | 'writing'
  | 'reviewing'
  | 'rewriting'
  | 'done'
  | 'failed'

/** 管道看板里的一张章节卡片（按 index 排序渲染） */
export interface PrdSectionCard {
  sectionId: string
  sectionTitle: string
  sectionHeading: string
  /** 章节下标；文档头为 -1 */
  index: number
  phase: PrdSectionPhase
  /** 是否为失败占位（仍停在失败时的车道） */
  failed: boolean
  /** 定稿正文；未定稿为空串 */
  text: string
  /** 改写原因 / 失败原因 */
  note: string
  issues: { severity: string; detail: string }[]
}

/** PRD 生成的管道看板状态（并行与串行共用） */
export interface PrdBoardState {
  /** 章节总数；未知为 0 */
  total: number
  /** 文档头是否已下发 */
  headerReady: boolean
  /** 文档头正文（不属于任何章节） */
  headerText: string
  /** 章节卡片，恒按 index 升序 */
  sections: PrdSectionCard[]
  /** 本次审核结论；不持久化 */
  review: ReviewConclusion | null
  /** 是否有章节失败后被占位 */
  partial: boolean
  failedSections: FailedSection[]
}

/** 已定稿章节的结构化记录（后端 `snapshot` 的 `completed_sections` 项） */
export interface CompletedSection {
  /** 章节 id，如 `overview` */
  sectionId: string
  /** 章节标题行，如 `## 1. 产品概述` */
  heading: string
  /** 该章的定稿正文（可直接追加） */
  text: string
  /** 章节下标；旧快照可能缺失，前端按数组序兜底 */
  index?: number
  /** 该章是否为失败占位 */
  failed?: boolean
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
  /**
   * 当前阶段模型的原始输出（审核意见 / 改写稿）。
   *
   * 断线重连时由任务运行时补回，面板因此不会在刷新后空着。它**不落库**：
   * 进程重启后的兜底快照里恒为空串。
   */
  stageText: string
  /** 已定稿章节列表 */
  completedSections: CompletedSection[]
  /** 关联方案 id；可空 */
  solutionId: number | null
  /** 失败时的错误码；正常为空串 */
  errorCode: string
  /** 失败时的中文说明；正常为空串 */
  errorMessage: string
  /**
   * 本次生成的待确认清单。
   *
   * 刷新重连时从任务记录（`generation_jobs.pending_items`）一并恢复，前端无需另发一次请求。
   */
  pendingItems: PendingItem[]
  /** 是否有章节失败后被占位（仅 PRD 并行链路） */
  partial: boolean
  /** 失败章节清单 */
  failedSections: FailedSection[]
  /**
   * 服务端算的任务已用时（毫秒）。
   *
   * 重连时用它给本地计时器打底：客户端计时器归零也不影响「已用时」的连续性。
   */
  elapsedMs: number
}

/**
 * 一个生成步骤的用量（后端 `StepUsage`）。
 *
 * 步骤名由后端按「任务类型 + 阶段」推出（`prd:writing` / `prd:reviewing` / `api_docs` …），
 * 前端只是展示，不参与命名。
 */
export interface StepUsage {
  /** 步骤名，形如 `prd:writing` */
  step: string
  /** 该步骤内**拿到结果**的模型调用次数 */
  calls: number
  /**
   * 该步骤内失败的调用次数（超时、断连、5xx，含自愈重试前的废弃尝试）。
   *
   * 与 `calls` 分开是为了不被互相掩盖：两者都真实发生过，混在一起会让
   * 「总共发了几次请求」读不出来。它们耗掉的时间都计在 `durationMs` 里。
   */
  failedCalls: number
  /** 输入 token 累计 */
  promptTokens: number
  /** 输出 token 累计 */
  completionTokens: number
  /** 该步骤累计耗时（毫秒） */
  durationMs: number
}

/**
 * 一次生成里实际注入的技能包身份。
 *
 * 回答的是「这份 PRD 是按哪一套模板写的」：改了 template 之后指纹就会变，
 * 两份文档的生成条件对不对得上，回执上直接看得到。
 */
export interface SkillRef {
  /** 技能包目录名，如 prd-generator */
  name: string
  /** skill.yaml 声明的版本号；读不到时为 unknown */
  version: string
  /** 三份资源内容合成的整体指纹（前 12 位） */
  fingerprint: string
}

/**
 * 一次生成任务的用量汇总（后端 `run_summary` 事件的 `data`，也是 `generation_jobs.usage` 的结构）。
 *
 * 事件与落库共用同一份结构，因此「流里看到的」与「重连后读到的」必然一致。
 * 终态任务重连时后端会补发一份，前端无需额外查询即可显示用量。
 */
export interface RunSummary {
  /** 文档种类（后端原始取值：prd / api_docs / prompts / optimize） */
  kind: string
  /** 终态：completed / failed */
  status: string
  /** 本次生成总耗时（毫秒） */
  durationMs: number
  /** 模型调用总次数（只统计拿到结果的调用） */
  totalCalls: number
  /**
   * 失败的调用次数。
   *
   * 与 `totalCalls` 分开：两者都是真实发生的请求，但含义不同。看到它不为 0 说明
   * 生成过程中有过抖动并被自愈回来了（或重试也没成功），这是排查「为什么偏慢」的线索。
   */
  failedCalls: number
  /** 输入 token 总量 */
  promptTokens: number
  /** 输出 token 总量 */
  completionTokens: number
  /** 按步骤拆分的用量 */
  steps: StepUsage[]
  /** 本次注入的技能包身份；只有 PRD 生成不为空，其他类型恒为空数组（不代表降级） */
  injectedSkills: SkillRef[]
  /**
   * 事前预算的告警（如「某步的输入已达预算上限的 92%」），每个 step 至多一条。
   *
   * 文案由后端生成、**仅供展示**：展示即可，不要拿它做分支判断或重试决策——
   * 真正的拦截发生在超顶时，那时请求根本没发出，会直接转成 `error` 事件。
   */
  warnings: string[]
  /** 是否有章节失败后被占位（仅 PRD 并行链路） */
  partial: boolean
  /** 失败章节清单 */
  failedSections: FailedSection[]
  /** 实际使用的供应商 */
  provider: string
  /** 实际使用的模型名 */
  model: string
  /** 该次生成所属请求的追踪号；可念给维护者定位问题 */
  requestId: string
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
  /**
   * 到达过的**最远里程碑**（`ViewState` / `StepId` 的字面值），用于在列表里显示进度。
   *
   * 刻意不是「当前所在的阶段」：列表回答的是「这份做到哪了」，而不是「你上次翻到哪一页」。
   */
  stage: string
  /** 创建时间（UTC ISO 8601 字符串） */
  created_at: string
  /**
   * 最近一次**写库**的时间（UTC ISO 8601 字符串）。
   *
   * 注意它包含「只是翻看」这类保存（切阶段、切对话标签都会写一次库），所以它不适合
   * 直接展示给用户——要回答「这份东西最后一次真的变了」请用 `content_updated_at`。
   */
  updated_at: string
  /**
   * 最后一次**内容**变更的时间（UTC ISO 8601 字符串）。列表的时间显示与排序都用它。
   *
   * 只在作品本身被写入时前进：需求摘要 / 对话 / 粘贴的材料变了，或三份产物被生成、
   * AI 优化、手改、版本恢复时。翻页与切标签不动它——那样列表会给出一串「刚刚」，
   * 却什么信息都没说。
   */
  content_updated_at: string
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
  /**
   * 这次改动是否动了**作品内容**（而不是只切了阶段 / 翻了页）。
   *
   * 字段名跟 API 走（`content_changed`，与 `created_at` 等同一种命名），因此这个载荷是
   * **原样提交**的、没有第二层转换——改名会静默丢掉这个标志，进而让内容时间永不前进。
   *
   * 由前端判定、后端转手存下：快照里同时装着内容（摘要、对话、粘贴的材料）与相位
   * （`viewState` / `activeJobId`），而后端不解析 `data`，它无从分辨这两者。
   */
  content_changed: boolean
}

/**
 * V2 完整流程的本地快照。
 * 与 V1 的 `SessionData` 分开存储，避免两套流程共用同一份缓存互相覆盖。
 *
 * 「还没提交、但已经敲了字」的三处录入内容（快捷入口的一段话、我有 PRD 的正文、
 * 我有 PRD + 接口文档 的两份正文）也一并落盘：它们是用户的手工输入，
 * 刷新一次就丢干净最让人恼火。
 *
 * **三份产物正文与三份用量回执不在这里。** 它们的所有权属于后端 `documents` 表
 * （见 `DocumentRecord`），随方案一起存在同一份数据库里，刷新后由
 * `stores/documents.ts` 按方案 id 取回。快照只留「走到哪一步、键盘敲了什么」这类
 * 只属于本次会话的状态——把这些和几十 KB 的正文混在一起，正是「同一份内容两个真源」
 * 的老问题。
 */
export interface SessionDataV2 {
  /** 当前所处阶段 */
  viewState: ViewState
  /** 所选入口模式；未选择为 null（视为完整流程，兼容旧快照） */
  entryMode: EntryMode | null
  /** 结构化需求摘要；未提交过为 null */
  summary: RequirementsSummary | null
  /**
   * 全部澄清线程的消息。
   *
   * 四条线程（需求 / PRD / 接口 / 提示词）**共用这一条列表**，按 `ChatMessage.stage`
   * 区分归属（见 `ChatStage`）。数组顺序就是发生顺序，按 `stage` 过滤后即为该线程的顺序。
   */
  messages: ChatMessage[]
  /**
   * 对话页当前停留在哪条线程。
   *
   * 它决定渲染哪一段对话、底栏给哪个主操作（需求澄清给「生成 PRD」，其余给「按澄清结果改写」）。
   * 随快照持久化：刷新回来应当仍在原来那条线程里。
   */
  chatTab: ChatStage
  /**
   * 各线程的后端会话 id（按 `ChatStage` 取）。
   *
   * 四条线程是**四个后端会话**：需求澄清与文档澄清的人设不同（`clarification` vs
   * `doc-clarification`），三条文档线程又各自带着不同文档的开场白——共用一个会话会让
   * 上下文串味。前端合并展示、后端分开登记，各取所需。
   * 空串表示该线程还没开过会话，接续时退化为无状态模式。
   */
  conversationIds: Record<ChatStage, string>
  /** 快捷流程入口的录入内容 */
  quickInput: QuickPrdInput
  /** 我有 PRD入口（「我有 PRD」）里用户贴入的 PRD 正文 */
  debugPrdText: string
  /** 「我有 PRD + 接口文档」入口里用户贴入的两份正文 */
  pastePairInput: PastePairInput
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

