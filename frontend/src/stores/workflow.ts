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
  EntryMode,
  QuickPrdInput,
  RequirementsSummary,
  RunSummary,
  SessionDataV2,
  Step,
  ViewState,
} from '@/types'
import {
  canGeneratePrdFrom,
  DOWNGRADE_NOTICE,
  hasRequiredDocuments,
  isDocReviewPhase,
  isGeneratingPhase,
  reachedStepIndex as computeReachedStepIndex,
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

  /** 已生成的 PRD 正文（Markdown 源码） */
  const prdContent = ref('')

  /** 已生成的接口文档正文（Markdown 源码） */
  const apiDocsContent = ref('')

  /** 已生成的提示词套件正文（Markdown 源码） */
  const promptsContent = ref('')

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

  /**
   * 各文档「最后一次生成的用量回执」；按**文档**分桶，而非按「最后一次生成动作」单例。
   *
   * 旧实现只有一份 `runSummary` 单例，谁最后生成完就覆盖谁，导致回看 PRD 时可能显示
   * 接口文档的回执（技能包名 / token 数 / 耗时都对不上）。这里改成三份独立记录，
   * 回看哪份文档就显示哪份的回执；优化（kind=optimize）改写的是 PRD，故归入 `prdRunSummary`。
   */
  const prdRunSummary = ref<RunSummary | null>(null)
  const apiDocsRunSummary = ref<RunSummary | null>(null)
  const promptsRunSummary = ref<RunSummary | null>(null)

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

  /** 导出当前会话的可持久化部分 */
  function snapshot(): SessionDataV2 {
    return {
      viewState: viewState.value,
      entryMode: entryMode.value,
      summary: summary.value,
      messages: messages.value,
      conversationId: conversationId.value,
      quickInput: quickInput.value,
      debugPrdText: debugPrdText.value,
      prdContent: prdContent.value,
      apiDocsContent: apiDocsContent.value,
      promptsContent: promptsContent.value,
      activeJobId: activeJobId.value,
      activeJobKind: activeJobKind.value,
      prdRunSummary: prdRunSummary.value,
      apiDocsRunSummary: apiDocsRunSummary.value,
      promptsRunSummary: promptsRunSummary.value,
    }
  }

  /**
   * 用一份快照覆盖当前状态。
   *
   * 调用方（`stores/solutions.ts`）负责先做校验与降级——快照可能来自旧版本的前端，
   * 或落在 `generating-*` 这种「刷新后无法继续」的中间态，那些判断在
   * `utils/workflow.ts` 里已有现成实现，不必在这里重做。
   */
  function applySnapshot(session: SessionDataV2, downgradeKind: DowngradeKind | null = null): void {
    viewState.value = session.viewState
    entryMode.value = session.entryMode
    summary.value = session.summary
    messages.value = session.messages
    conversationId.value = session.conversationId
    quickInput.value = session.quickInput
    debugPrdText.value = session.debugPrdText
    prdContent.value = session.prdContent
    apiDocsContent.value = session.apiDocsContent
    promptsContent.value = session.promptsContent
    activeJobId.value = session.activeJobId ?? null
    activeJobKind.value = session.activeJobKind ?? null
    prdRunSummary.value = session.prdRunSummary ?? null
    apiDocsRunSummary.value = session.apiDocsRunSummary ?? null
    promptsRunSummary.value = session.promptsRunSummary ?? null
    downgrade.value = downgradeKind
    // 通知视图「这是整份换上的新内容」，而不是用户的一次输入。
    // 结构化表单只在 setup 阶段读一次 `initial`，因此需要据此重建，
    // 否则异步恢复回来的摘要会填不进表单（打开旧方案时肉眼可见）。
    restoreTick.value += 1
  }

  /**
   * 把一次生成的用量回执写进「对应文档」的桶。
   *
   * 映射按文档而非任务 kind：`prd` 与 `optimize`（优化改的是 PRD）都归 `prdRunSummary`，
   * 接口文档 / 提示词各归各自。写入后由下方 watch 触发自动保存，回执随之持久化进方案。
   *
   * 注意 kind 的两套取值：调用方传的是前端 `DocKind`（`api-docs` 带连字符），
   * 而回执自身的 `kind` 是后端原始值（`api_docs` 带下划线）——这里**两者都认**，
   * 免得因为一个连字符就把接口文档的回执错投进 PRD 桶。
   */
  function setRunSummary(kind: DocKind | string, summary: RunSummary | null): void {
    if (kind === 'api-docs' || kind === 'api_docs') apiDocsRunSummary.value = summary
    else if (kind === 'prompts') promptsRunSummary.value = summary
    else prdRunSummary.value = summary
  }

  /**
   * 当前正在审阅的文档应该显示的回执。
   *
   * 由 `viewState` 反推文档种类，从对应桶取——这正是「回看 PRD 显示 PRD 回执」的落点。
   * 生成中等非审阅态一律返回 `null`（审阅区那时不展示回执）。
   */
  const runSummary = computed<RunSummary | null>(() => {
    switch (viewState.value) {
      case 'review-prd':
        return prdRunSummary.value
      case 'review-api-docs':
        return apiDocsRunSummary.value
      case 'review-prompts':
        return promptsRunSummary.value
      default:
        return null
    }
  })

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
      prdContent,
      apiDocsContent,
      promptsContent,
      activeJobId,
      activeJobKind,
      prdRunSummary,
      apiDocsRunSummary,
      promptsRunSummary,
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

  /** 当前阶段在进度条上的下标 */
  const currentStepIndex = computed(() => stepIndexOf(viewState.value, entryMode.value))

  /** 已到达的最远步骤下标（进度条可点击范围） */
  const reachedStepIndex = computed(() =>
    computeReachedStepIndex({
      mode: entryMode.value,
      hasMessages: messages.value.length > 0,
      hasPrd: prdContent.value.trim() !== '',
      hasApiDocs: apiDocsContent.value.trim() !== '',
      hasPrompts: promptsContent.value.trim() !== '',
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
      prd: prdContent.value,
      apiDocs: apiDocsContent.value,
      prompts: promptsContent.value,
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
   */
  function reset(): void {
    messages.value = []
    conversationId.value = ''
    summary.value = null
    quickInput.value = { productName: '', requirements: '' }
    debugPrdText.value = ''
    prdContent.value = ''
    apiDocsContent.value = ''
    activeJobId.value = null
    activeJobKind.value = null
    prdRunSummary.value = null
    apiDocsRunSummary.value = null
    promptsRunSummary.value = null
    downgrade.value = null
    entryMode.value = null
    viewState.value = 'entry'
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
   */
  const hasContent = computed(
    () =>
      messages.value.length > 0 ||
      summary.value !== null ||
      quickInput.value.productName.trim() !== '' ||
      quickInput.value.requirements.trim() !== '' ||
      debugPrdText.value.trim() !== '' ||
      prdContent.value.trim() !== '' ||
      apiDocsContent.value.trim() !== '' ||
      promptsContent.value.trim() !== '',
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
    prdContent,
    apiDocsContent,
    promptsContent,
    activeJobId,
    activeJobKind,
    prdRunSummary,
    apiDocsRunSummary,
    promptsRunSummary,
    runSummary,
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
    setRunSummary,
  }
})
