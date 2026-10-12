/**
 * 会话流程 store。
 *
 * 这里只放「跨刷新要留住、且多个阶段共用」的状态，以及由它派生的进度与守卫。
 * 纯交互态（流式文本缓冲、错误提示、确认闸门的临时结果、表单重建 key）留在组件里，
 * 避免 store 变成杂物间。
 *
 * 阶段判断全部委托给 `utils/workflow.ts` 的纯函数——store 只负责持有状态，
 * 让这些规则可以被独立单测，也能被后续新增的入口模式复用。
 */

import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'

import type {
  ChatMessage,
  EntryMode,
  QuickPrdInput,
  RequirementsSummary,
  Step,
  ViewState,
} from '@/types'
import {
  canGeneratePrdFrom,
  DOWNGRADE_NOTICE,
  hasRequiredDocuments,
  isDocReviewPhase,
  isGeneratingPhase,
  parseSessionSnapshot,
  reachedStepIndex as computeReachedStepIndex,
  stepIndexOf,
  stepsForMode,
  type DowngradeKind,
} from '@/utils/workflow'

/** 本地会话快照的存储键 */
export const SESSION_STORAGE_KEY = 'harnessprd:v2:session'

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

  // ------------------------------------------------------------------
  // 持久化
  // ------------------------------------------------------------------

  /** 把当前会话数据落盘；写入失败（隐私模式 / 存储超限）时静默跳过 */
  function persist(): void {
    try {
      localStorage.setItem(
        SESSION_STORAGE_KEY,
        JSON.stringify({
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
        }),
      )
    } catch {
      // 隐私模式或存储超限时静默跳过，不影响使用
    }
  }

  /** 读取本地快照并回填 */
  function restore(): void {
    let raw: string | null = null
    try {
      raw = localStorage.getItem(SESSION_STORAGE_KEY)
    } catch {
      // 隐私模式下 localStorage 不可用，静默跳过
      return
    }

    const parsed = parseSessionSnapshot(raw)
    if (!parsed) return

    const { session } = parsed
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
    downgrade.value = parsed.downgrade
  }

  // 先恢复再挂 watch，避免恢复动作本身触发一次多余落盘。
  // 恢复必须在 setup 阶段同步完成，否则首帧会闪一下空状态。
  restore()

  // 状态变化时整体落盘。流式过程中的临时文本不在此列，因此不会高频写存储。
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
    ],
    persist,
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

  /** 清空会话数据并回到入口页，同时删除本地快照 */
  function reset(): void {
    messages.value = []
    conversationId.value = ''
    summary.value = null
    quickInput.value = { productName: '', requirements: '' }
    debugPrdText.value = ''
    prdContent.value = ''
    apiDocsContent.value = ''
    promptsContent.value = ''
    downgrade.value = null
    entryMode.value = null
    viewState.value = 'entry'

    try {
      localStorage.removeItem(SESSION_STORAGE_KEY)
    } catch {
      // 隐私模式等场景下静默忽略
    }
  }

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
    // 运行态
    chatStreaming,
    docGenerating,
    busy,
    downgrade,
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
    // 动作
    goTo,
    chooseEntry,
    backToEntry,
    reset,
  }
})
