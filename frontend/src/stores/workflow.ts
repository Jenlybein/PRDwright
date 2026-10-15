/**
 * 会话流程 store。
 *
 * 这里只放「跨刷新要留住、且多个阶段共用」的状态，以及由它派生的进度与守卫。
 * 纯交互态（流式文本缓冲、错误提示、确认闸门的临时结果、表单重建 key）留在组件里，
 * 避免 store 变成杂物间。
 *
 * 阶段判断全部委托给 `utils/workflow.ts` 的纯函数——store 只负责持有状态，
 * 让这些规则可以被独立单测，也能被后续新增的入口模式复用。
 *
 * **持久化不在这里**：会话状态的权威来源是后端 `solutions` 表，
 * 由 `stores/solutions.ts` 负责加载与保存。本 store 只提供 `snapshot()` / `applySnapshot()`
 * 两个出入口，外加一个 `changeTick` 供上层 watch 到「有东西变了」。
 * 这样存储策略（防抖、创建还是更新、失败怎么办）与状态本身互不纠缠。
 */

import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'

import type {
  ChatMessage,
  DocKind,
  DocumentKind,
  EntryMode,
  QuickPrdInput,
  RequirementsSummary,
  SessionDataV2,
  Step,
  ViewState,
} from '@/types'
import { useDocumentsStore } from '@/stores/documents'
import {
  canGeneratePrdFrom,
  DOWNGRADE_NOTICE,
  hasRequiredDocuments,
  isDocReviewPhase,
  isGeneratingPhase,
  reachedStepIndex as computeReachedStepIndex,
  reviewStateFor,
  stepIndexOf,
  stepsForMode,
  type DowngradeKind,
} from '@/utils/workflow'

/** 各入口模式的落点：选中某个入口后直接进入该入口的第一个界面 */
const MODE_LANDING: Record<EntryMode, ViewState> = {
  structured: 'form',
  'prd-quick': 'prd-quick',
  'prompt-debug': 'prompt-debug',
}

export const useWorkflowStore = defineStore('workflow', () => {
  /**
   * 三份产物正文的持有者。
   *
   * 它们**不在本 store 里**：所有权在后端 `documents` 表，前端只有一份工作副本
   * （见 `stores/documents.ts`）。本 store 只借用它来判断「走到哪一步」——
   * 进度条要不要解锁某一格、完成条件是否满足，都取决于产物有没有内容。
   */
  const documents = useDocumentsStore()

  // ------------------------------------------------------------------
  // 会话数据（写入本地快照）
  // ------------------------------------------------------------------

  /** 当前所处阶段；初始为入口选择页 */
  const viewState = ref<ViewState>('entry')

  /** 所选入口模式；为空表示还没选（按完整流程展示进度条，兼容旧快照） */
  const entryMode = ref<EntryMode | null>(null)

  /** 结构化需求摘要；为空表示还没提交过需求 */
  const summary = ref<RequirementsSummary | null>(null)

  /** 已固化的聊天消息；正在流式生成的那条不在其中 */
  const messages = ref<ChatMessage[]>([])

  /** 后端会话 ID；为空表示后端没有可用会话，接续时退化为无状态模式 */
  const conversationId = ref('')

  /** PRD 快捷入口的录入内容（产品名 + 一段话描述） */
  const quickInput = ref<QuickPrdInput>({ productName: '', requirements: '' })

  /** 提示词调试入口里用户贴入的 PRD 正文 */
  const debugPrdText = ref('')

  /**
   * 当前进行中的生成任务 id；`null` 表示没有任务。
   *
   * 生成与浏览器连接解耦后，刷新 / 断线只断订阅；凭这个指针即可在页面恢复后
   * **静默重连**到同一任务，先收 `snapshot` 补全正文再接 live。
   * 它是**持久化字段**（随快照存后端）：正是跨刷新才需要它。
   */
  const activeJobId = ref<number | null>(null)

  /** 当前进行中任务的文档种类；与 `activeJobId` 成对写入 / 清空 */
  const activeJobKind = ref<DocKind | null>(null)

  // ------------------------------------------------------------------
  // 文档澄清会话（与需求澄清完全独立的一套字段）
  //
  // 为什么不复用 `messages` / `conversationId`：那是「表单 → PRD」那条链路的需求澄清记录，
  // 混在一起会让「回填摘要」的输入凭空多出一段文档答疑（闸门①的 Diff 会莫名其妙地变），
  // 而且用户在两个场景之间来回切换时对话会被互相冲掉。两套记录、两个会话 id，各说各的。
  // ------------------------------------------------------------------

  /** 文档澄清的聊天记录（含首轮 AI 追问） */
  const clarifyMessages = ref<ChatMessage[]>([])

  /** 文档澄清的后端会话 ID；为空时接续退化无状态模式 */
  const clarifyConversationId = ref('')

  /**
   * 本次澄清针对哪一份产物；为空表示还没进入澄清。
   *
   * 它决定三件事：聊天开场白里放哪份正文、改写任务写回哪一份、改写完成后回到哪个审阅页。
   */
  const clarifyTarget = ref<DocumentKind | null>(null)

  // ------------------------------------------------------------------
  // 运行态（不写入快照）
  // ------------------------------------------------------------------

  /** 对话流式请求进行中 */
  const chatStreaming = ref(false)

  /** 文档生成 / 优化进行中 */
  const docGenerating = ref(false)

  /** 是否有任何请求在进行中；导航守卫据此禁止跳转 */
  const busy = computed(() => chatStreaming.value || docGenerating.value)

  /** 刷新时发生降级的类型；供界面解释为什么不是原状态 */
  const downgrade = ref<DowngradeKind | null>(null)

  /**
   * 状态变更计数。
   *
   * `stores/solutions.ts` 靠 watch 它来决定何时保存——这样保存方不必逐个订阅本 store 的
   * 十个字段（漏订阅一个就是「这个字段改了但没存」这种极难发现的 bug）。
   * 计数不参与快照，也不参与「有没有内容」的判断。
   */
  const changeTick = ref(0)

  /**
   * 「整份快照被换上」的计数。
   *
   * 与 `changeTick` 的区别在于**谁引起的**：`changeTick` 是用户/流程让状态变了
   * （该去保存），而本计数是**外部内容盖了上来**（该去重建那些只在 setup 读一次
   * 初始值的子组件）。两者不能混用——若用它触发保存，打开一份旧方案就会立刻
   * 原样存回去，白跑一趟请求。
   */
  const restoreTick = ref(0)

  // ------------------------------------------------------------------
  // 快照出入口：存储策略由 stores/solutions.ts 负责
  // ------------------------------------------------------------------

  /**
   * 导出当前会话的可持久化部分。
   *
   * **不含三份产物正文与用量回执**：它们属于后端 `documents` 表，随方案一起存在
   * 同一份数据库里，刷新后按方案 id 取回即可（`stores/documents.ts::loadAll`）。
   * 快照里放一份副本只会制造第二个真源。
   */
  function snapshot(): SessionDataV2 {
    return {
      viewState: viewState.value,
      entryMode: entryMode.value,
      summary: summary.value,
      messages: messages.value,
      conversationId: conversationId.value,
      quickInput: quickInput.value,
      debugPrdText: debugPrdText.value,
      activeJobId: activeJobId.value,
      activeJobKind: activeJobKind.value,
      clarifyMessages: clarifyMessages.value,
      clarifyConversationId: clarifyConversationId.value,
      clarifyTarget: clarifyTarget.value,
    }
  }

  /**
   * 用一份快照覆盖当前状态。
   *
   * 调用方（`stores/solutions.ts`）负责先做校验与降级——快照可能来自旧版本的前端，
   * 或落在 `generating-*` 这种「刷新后无法继续」的中间态，那些判断在
   * `utils/workflow.ts` 里已有现成实现，不必在这里重做。
   *
   * 产物正文不在这里恢复：快照里本就没有它们，正文由 `stores/documents.ts` 单独按
   * 方案 id 拉取。
   */
  function applySnapshot(session: SessionDataV2, downgradeKind: DowngradeKind | null = null): void {
    viewState.value = session.viewState
    entryMode.value = session.entryMode
    summary.value = session.summary
    messages.value = session.messages
    conversationId.value = session.conversationId
    quickInput.value = session.quickInput
    debugPrdText.value = session.debugPrdText
    activeJobId.value = session.activeJobId ?? null
    activeJobKind.value = session.activeJobKind ?? null
    // 澄清会话随快照一起恢复，因此刷新后能接着聊（也就**不需要**降级）
    clarifyMessages.value = session.clarifyMessages ?? []
    clarifyConversationId.value = session.clarifyConversationId ?? ''
    clarifyTarget.value = session.clarifyTarget ?? null
    downgrade.value = downgradeKind
    // 通知视图「这是整份换上的新内容」，而不是用户的一次输入。
    // 结构化表单只在 setup 阶段读一次 `initial`，因此需要据此重建，
    // 否则异步恢复回来的摘要会填不进表单（打开旧方案时肉眼可见）。
    restoreTick.value += 1
  }

  /**
   * 状态变化时自增 `changeTick`。
   *
   * 注意这里 watch 的是「快照里包含的字段」，而**不是**那三个运行态 ref
   * （`chatStreaming` / `docGenerating` / `downgrade`）——它们不该触发保存。
   * 流式过程中的临时文本留在组件里，因此也不会形成高频保存。
   */
  watch(
    [
      viewState,
      entryMode,
      summary,
      messages,
      conversationId,
      quickInput,
      debugPrdText,
      activeJobId,
      activeJobKind,
      clarifyMessages,
      clarifyConversationId,
      clarifyTarget,
    ],
    () => {
      changeTick.value += 1
    },
    { deep: true },
  )

  // ------------------------------------------------------------------
  // 派生状态
  // ------------------------------------------------------------------

  /** 刷新降级的说明文案；无降级时为空串 */
  const downgradeNotice = computed(() =>
    downgrade.value ? DOWNGRADE_NOTICE[downgrade.value] : '',
  )

  /** 进度条要展示的步骤：由入口模式决定（完整流程五步，调试入口只有一步） */
  const steps = computed<readonly Step[]>(() => stepsForMode(entryMode.value))

  /**
   * 当前阶段在进度条上的下标。
   *
   * `clarifying` 是个例外：它是「对某一篇文档做澄清」，而 `STEP_OF_STATE` 是静态表，
   * 只能给一个兜底值（`review-prd`）。澄清接口文档时进度条若跳回 PRD 那一格，用户会以为
   * 自己走错了流程——因此这里按 `clarifyTarget` 精确算一次。
   */
  const currentStepIndex = computed(() => {
    if (viewState.value === 'clarifying') {
      return stepIndexOf(reviewStateFor(clarifyTarget.value ?? 'prd'), entryMode.value)
    }
    return stepIndexOf(viewState.value, entryMode.value)
  })

  /** 已到达的最远步骤下标（进度条可点击范围），解锁依据是**产物有没有内容** */
  const reachedStepIndex = computed(() =>
    computeReachedStepIndex({
      mode: entryMode.value,
      hasMessages: messages.value.length > 0,
      hasPrd: documents.hasPrd,
      hasApiDocs: documents.hasApiDocs,
      hasPrompts: documents.hasPrompts,
      currentStepIndex: currentStepIndex.value,
    }),
  )

  /** 当前是否处于「文档生成中」阶段 */
  const generating = computed(() => isGeneratingPhase(viewState.value))

  /** 当前是否处于「文档待确认」阶段 */
  const reviewing = computed(() => isDocReviewPhase(viewState.value))

  /**
   * 当前入口模式「完成」所需的文档是否齐备。
   * 完整流程要三份齐全，快捷入口只要 PRD，调试入口只要提示词。
   */
  const requiredDocsReady = computed(() =>
    hasRequiredDocuments(entryMode.value, {
      prd: documents.contents.prd,
      apiDocs: documents.contents['api-docs'],
      prompts: documents.contents.prompts,
    }),
  )

  /** 是否允许进入完成页：所需文档齐备且没有进行中的请求 */
  const canFinish = computed(() => requiredDocsReady.value && !busy.value)

  /** 对话阶段是否满足「生成 PRD」条件 */
  const canGeneratePrd = computed(() => canGeneratePrdFrom(messages.value, busy.value))

  // ------------------------------------------------------------------
  // 动作
  // ------------------------------------------------------------------

  /**
   * 统一的状态导航入口，收口所有阶段赋值并做流程安全校验：
   * 请求进行中时拒绝跳转，避免请求与界面状态脱节。
   *
   * @returns 是否真的发生了跳转（被守卫拦下时为 `false`）
   */
  function goTo(target: ViewState): boolean {
    if (busy.value) return false
    viewState.value = target
    window.scrollTo({ top: 0, behavior: 'smooth' })
    return true
  }

  /**
   * 选择入口：记下模式并直接进入该模式的第一个界面。
   *
   * 模式只影响「进度条展示哪几步、需要哪几份文档才算完成」以及流水线从哪个输入取需求，
   * 生成链路本身完全共用，因此这里不需要清理任何已生成内容。
   */
  function chooseEntry(mode: EntryMode): boolean {
    if (busy.value) return false
    entryMode.value = mode
    return goTo(MODE_LANDING[mode])
  }

  /** 回到入口选择页（保留已选模式，用户点「上一步」时不至于丢掉产物） */
  function backToEntry(): boolean {
    return goTo('entry')
  }

  /**
   * 清空会话数据并回到入口页。
   *
   * 只清内存——后端那份方案由 `stores/solutions.ts` 决定删不删。
   * 「重新开始」的语义是「开一份新的」，因此那边是**清掉当前 id 而不是删库**，
   * 旧方案仍留在侧边栏里可随时回来。
   *
   * 产物正文同步交给 `documents.clear()`：它们不属于本 store，但都属于「当前会话」，
   * 一起清掉才不会出现「入口页回来了、上一份的正文还挂在内存里」。
   */
  function reset(): void {
    messages.value = []
    conversationId.value = ''
    summary.value = null
    quickInput.value = { productName: '', requirements: '' }
    debugPrdText.value = ''
    activeJobId.value = null
    activeJobKind.value = null
    clarifyMessages.value = []
    clarifyConversationId.value = ''
    clarifyTarget.value = null
    downgrade.value = null
    entryMode.value = null
    viewState.value = 'entry'
    documents.clear()
    // 与 `applySnapshot` 同理：这也是一次「整份换掉」，
    // 视图（尤其是只在 setup 读一次 `initial` 的表单）要据此重建，
    // 否则「重新开始」后表单里还留着上一轮的内容。
    restoreTick.value += 1
  }

  /**
   * 快照里是否存在「值得保存」的内容。
   *
   * 只看内容字段，**不看 `viewState` / `entryMode` / `conversationId`**——
   * 它们描述的是「走到哪一步」而不是「有什么」；用户只是点进入口看一眼就又退回，
   * 不该因此留下一份空方案。
   *
   * 也**不看产物正文**：正文属于后端 `documents` 表，而「要不要建一份方案」这件事
   * 发生在正文产生之前——第一份正文的生成本身就需要方案已经存在（任务要挂 `solution_id`）。
   */
  const hasContent = computed(
    () =>
      messages.value.length > 0 ||
      summary.value !== null ||
      quickInput.value.productName.trim() !== '' ||
      quickInput.value.requirements.trim() !== '' ||
      debugPrdText.value.trim() !== '',
  )

  return {
    // 会话数据
    viewState,
    entryMode,
    summary,
    messages,
    conversationId,
    quickInput,
    debugPrdText,
    activeJobId,
    activeJobKind,
    clarifyMessages,
    clarifyConversationId,
    clarifyTarget,
    // 运行态
    chatStreaming,
    docGenerating,
    busy,
    downgrade,
    changeTick,
    restoreTick,
    // 派生状态
    downgradeNotice,
    steps,
    currentStepIndex,
    reachedStepIndex,
    generating,
    reviewing,
    requiredDocsReady,
    canFinish,
    canGeneratePrd,
    hasContent,
    // 动作
    goTo,
    chooseEntry,
    backToEntry,
    reset,
    snapshot,
    applySnapshot,
  }
})
