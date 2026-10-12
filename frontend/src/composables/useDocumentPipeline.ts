import { computed, ref } from 'vue'
import { storeToRefs } from 'pinia'

import {
  generateApiDocsStream,
  generatePrdFromSummaryStream,
  generatePromptsStream,
  optimizeDocumentStream,
  retrieveApiDocsRag,
  syncSummaryFromConversation,
  type GeneratePrdFromSummaryPayload,
} from '@/services/api'
import { useWorkflowStore } from '@/stores/workflow'
import type {
  ChatMessage,
  PrdGenerationStage,
  PrdStageProgress,
  RagHit,
  RequirementsSummary,
  StreamStageEvent,
  SummaryChange,
  ViewState,
} from '@/types'
import { buildDocFilename, downloadFile, type DocType } from '@/utils/download'
import { diffSummaries } from '@/utils/requirements'

/** 受管的文档 key */
type DocKey = 'prd' | 'api-docs' | 'prompts'

/** 文档流水线需要向外部借用的能力 */
export interface PipelineHooks {
  /** 展示一条短暂提示 */
  onNotice: (text: string) => void
}

/**
 * 文档生成、审阅与两道确认闸门。
 *
 * 覆盖从「点击生成 PRD」到「进入完成页」的全部编排：文档流式生成与优化、
 * 摘要回填 Diff 确认、接口规范检索确认、下载与完成守卫。
 *
 * **按入口模式分流**只体现在三个地方：需求从哪来（结构化摘要 / 一段话）、
 * 「返回」回到哪个录入页、以及完成所需的最低文档集合；生成链路本身完全共用。
 *
 * 阶段本身（`viewState`）与三份文档正文都落在 Pinia store；这里只持有
 * 「这一次请求 / 这一次确认」的临时状态，因此刷新页面不会被脏状态影响。
 */
export function useDocumentPipeline(hooks: PipelineHooks) {
  const store = useWorkflowStore()
  const {
    viewState,
    entryMode,
    summary,
    messages,
    quickInput,
    debugPrdText,
    prdContent,
    apiDocsContent,
    promptsContent,
    docGenerating: isGenerating,
    generating: isGeneratingPhase,
    reviewing: isReviewing,
    canFinish: canFinishTask,
    canGeneratePrd,
  } = storeToRefs(store)

  // -------------------------------------------------------------------------
  // 交互态
  // -------------------------------------------------------------------------

  /** 文档生成中的流式增量文本，实时展示在审阅区 */
  const docStreamingContent = ref('')

  /** 文档生成 / 优化过程中的错误提示 */
  const docError = ref('')

  /** 当前正在处理的文档 key，用于定位流式内容与错误应落到哪一份 */
  const activeDocKey = ref<DocKey>('prd')

  // -------------------------------------------------------------------------
  // PRD 分章节生成的阶段进度
  // -------------------------------------------------------------------------

  /** 分章节生成的进度快照；null 表示未走该链路（整篇生成或其它文档） */
  const prdProgress = ref<PrdStageProgress | null>(null)

  /** 当前章节已接收的字符数：改写时据此把流式缓冲回退到本章之前 */
  let currentSectionChars = 0

  /** 是否已有章节开始。文档头不属于任何章节，不能计入 `currentSectionChars` */
  let sectionStarted = false

  /** 分章节生成是否处于「写入中」等阶段（非 PRD 文档恒为 false） */
  const prdStatus = computed<PrdStageProgress | null>(() =>
    activeDocKey.value === 'prd' ? prdProgress.value : null,
  )

  /** 进入 PRD 生成前的复位：先按「撰写中」占位，等后端 stage 事件再细化 */
  function beginPrdStages(): void {
    currentSectionChars = 0
    sectionStarted = false
    prdProgress.value = {
      stage: 'writing',
      sectionId: '',
      sectionTitle: '',
      sectionHeading: '',
      index: 0,
      total: 0,
      note: '',
    }
  }

  /** PRD 正文的增量追加：同时按「当前章节」计数，供改写时回退使用 */
  function appendPrdChunk(chunk: string): void {
    docStreamingContent.value += chunk
    if (sectionStarted) currentSectionChars += chunk.length
  }

  /** 把后端下发的阶段名归一成前端联合类型（未知取值按「撰写中」处理） */
  function toPrdStage(stage: string): PrdGenerationStage {
    const known: PrdGenerationStage[] = ['writing', 'reviewing', 'rewriting', 'done']
    return (known as string[]).includes(stage) ? (stage as PrdGenerationStage) : 'writing'
  }

  /**
   * 处理 `stage` 阶段事件。
   *
   * 改写（rewriting）时会**丢弃当前章节已接收的文本**——这一章马上会被重新流一遍。
   * 若不做丢弃，用户会看到同一章出现两次、或内容凭空被追加，观感上像是文档错乱。
   */
  function handlePrdStage(event: StreamStageEvent): void {
    const stage = toPrdStage(event.stage)
    prdProgress.value = {
      stage,
      sectionId: event.sectionId,
      sectionTitle: event.sectionTitle,
      sectionHeading: event.sectionHeading,
      index: event.index,
      total: event.total,
      note: event.note,
    }

    if (stage === 'writing') {
      // 新的一章开始：从这里起接收的文本才算本章的
      sectionStarted = true
      currentSectionChars = 0
      return
    }

    if (stage === 'rewriting') {
      docStreamingContent.value = docStreamingContent.value.slice(
        0,
        docStreamingContent.value.length - currentSectionChars,
      )
      currentSectionChars = 0
    }
  }

  // -------------------------------------------------------------------------
  // 确认闸门 1：摘要回填 Diff（生成 PRD 之前）
  // -------------------------------------------------------------------------

  /** 从对话里回填出的候选摘要；用户确认后才会写回 store 里的摘要 */
  const pendingSummary = ref<RequirementsSummary | null>(null)

  /** 逐字段差异，交给摘要确认面板展示 */
  const summaryChanges = ref<SummaryChange[]>([])

  /** 是否正在回填摘要 */
  const summarySyncing = ref(false)

  /** 摘要回填失败原因 */
  const summarySyncError = ref('')

  // -------------------------------------------------------------------------
  // 确认闸门 2：接口规范检索（生成接口文档之前）
  // -------------------------------------------------------------------------

  /** 检索到的团队接口规范与历史接口示例 */
  const ragHits = ref<RagHit[]>([])

  /** 是否正在检索 */
  const ragRetrieving = ref(false)

  /** 检索失败原因 */
  const ragError = ref('')

  // -------------------------------------------------------------------------
  // 导航
  // -------------------------------------------------------------------------

  /** 清掉属于「本次请求 / 本次确认」的临时错误与半截流式内容 */
  function clearTransient(): void {
    docError.value = ''
    docStreamingContent.value = ''
    summarySyncError.value = ''
    ragError.value = ''
  }

  /**
   * 统一的状态导航入口。
   *
   * 阶段赋值收口在 store 的 `goTo`（内含「请求进行中禁止跳转」的守卫），
   * 这里只额外清掉属于本轮交互的临时状态。
   */
  function goTo(target: ViewState): void {
    if (!store.goTo(target)) return
    clearTransient()
  }

  /** 交给后端的历史消息（只保留角色与正文） */
  function conversationHistory(): ChatMessage[] {
    return messages.value.map(({ role, content }) => ({ role, content }))
  }

  // -------------------------------------------------------------------------
  // 文档生成与优化
  // -------------------------------------------------------------------------

  /**
   * 执行一次文档流式生成，并把增量实时写入 `docStreamingContent`。
   *
   * @param docKey 目标文档 key，用于在出错时定位提示
   * @param request 实际发起请求的闭包
   * @returns `{ reply, ok }`：`ok` 为 false 表示生成失败（如被截断），
   *   此时 `reply` 为已流出的半截内容，调用方不应把它当作完整文档推进流程。
   */
  async function runDocumentStream(
    docKey: DocKey,
    request: () => Promise<{ reply: string }>,
  ): Promise<{ reply: string; ok: boolean }> {
    activeDocKey.value = docKey
    isGenerating.value = true
    docStreamingContent.value = ''
    docError.value = ''

    let reply = ''
    let ok = true
    try {
      const result = await request()
      reply = result.reply
    } catch (error) {
      ok = false
      docError.value = error instanceof Error ? error.message : String(error)
      // 报错时保留已经流出的半截内容，供用户查看已生成到哪一步
      reply = docStreamingContent.value
    } finally {
      isGenerating.value = false
      if (ok) {
        // 成功才清空流式缓冲；失败时保留，让失败分支里仍能看到半截内容
        docStreamingContent.value = ''
      }
    }
    return { reply, ok }
  }

  /**
   * 按当前入口模式组装「生成 PRD」的请求体。
   *
   * 两条入口只是需求来源不同，因此这里返回同一个请求结构，交给后端汇合到
   * 同一条链路；返回 `null` 表示当前模式还没有可用的输入（如快捷入口还没写需求），
   * 调用方据此直接放弃，不要发出一个注定失败的请求。
   */
  function buildPrdPayload(): GeneratePrdFromSummaryPayload | null {
    if (entryMode.value === 'prd-quick') {
      const { productName, requirements } = quickInput.value
      if (!requirements.trim()) return null
      // 快捷入口没有对话环节，历史恒为空
      return { requirements, productName, history: [] }
    }

    const current = summary.value
    if (!current) return null
    return { summary: current, history: conversationHistory() }
  }

  /**
   * 生成 PRD：把需求（结构化摘要或自由文本）与对话历史交给后端，由技能包驱动生成。
   *
   * 走 `/generate-prd-from-summary-stream`——两条入口共用同一份请求契约，需求文本
   * 由后端统一格式化，前端不需要知道「摘要怎么变成文本」。
   */
  async function generatePrd(): Promise<void> {
    const payload = buildPrdPayload()
    if (!payload) return
    // 重新生成 PRD 会让基于它的后续文档失效，进度回退到 PRD
    apiDocsContent.value = ''
    promptsContent.value = ''
    beginPrdStages()
    const { reply, ok } = await runDocumentStream('prd', () =>
      generatePrdFromSummaryStream(payload, {
        onChunk: appendPrdChunk,
        onStage: handlePrdStage,
      }),
    )
    if (!ok) {
      // 失败时收掉阶段条：原因由错误提示说明，留着一个「进行中」的指示反而误导
      prdProgress.value = null
      return
    }
    const content = reply.trim()
    if (content) {
      prdContent.value = content
      goTo('review-prd')
    }
  }

  /** 生成接口文档：以 PRD 正文为输入，可带上用户确认过的检索命中作为参考规范 */
  async function generateApiDocs(hits: RagHit[] = []): Promise<void> {
    // 重新生成接口文档会让基于它的提示词失效，进度回退到接口文档
    promptsContent.value = ''
    const { reply, ok } = await runDocumentStream('api-docs', () =>
      generateApiDocsStream(
        { prdContent: prdContent.value, ragHits: hits },
        { onChunk: (chunk) => (docStreamingContent.value += chunk) },
      ),
    )
    if (!ok) return
    const content = reply.trim()
    if (content) {
      apiDocsContent.value = content
      goTo('review-api-docs')
    }
  }

  /**
   * 生成提示词套件：以 PRD 正文为输入。
   *
   * PRD 来源随入口模式而变（完整流程用本次生成的 PRD，调试入口用用户贴入的 PRD），
   * 因此由调用方把正文传进来，而不是在这里猜该读哪份状态。
   */
  async function generatePrompts(prdText: string): Promise<void> {
    const { reply, ok } = await runDocumentStream('prompts', () =>
      generatePromptsStream(
        { prdContent: prdText },
        { onChunk: (chunk) => (docStreamingContent.value += chunk) },
      ),
    )
    if (!ok) return
    const content = reply.trim()
    if (content) {
      promptsContent.value = content
      goTo('review-prompts')
    }
  }

  /**
   * 优化指定文档（按用户指令）。
   *
   * `content` 由审阅区传入，可能包含用户尚未保存的编辑；必须以它为准，
   * 否则优化结果会把用户刚改的内容覆盖掉。
   */
  async function optimizeCurrentDocument(content: string, instruction: string): Promise<void> {
    const docKey = activeDocKey.value
    if (!content.trim() || !instruction.trim()) return

    const { reply, ok } = await runDocumentStream(docKey, () =>
      optimizeDocumentStream(
        { document: content, instruction },
        { onChunk: (chunk) => (docStreamingContent.value += chunk) },
      ),
    )
    if (!ok) return // 优化失败：保留原文档，错误已写入 docError
    const optimized = reply.trim()
    if (!optimized) return
    if (docKey === 'prd') prdContent.value = optimized
    else if (docKey === 'api-docs') apiDocsContent.value = optimized
    else promptsContent.value = optimized
  }

  /** 审阅页「保存」：把编辑后的原文写回对应的文档状态 */
  function onSaveDocument(content: string): void {
    if (viewState.value === 'review-prd') prdContent.value = content
    else if (viewState.value === 'review-api-docs') apiDocsContent.value = content
    else if (viewState.value === 'review-prompts') promptsContent.value = content
    hooks.onNotice('文档已保存到本地')
  }

  /**
   * 审阅页「AI 优化」：优化指令由审阅组件的页内输入区收集，
   * 这里只负责驱动流式优化并写回结果（不使用浏览器原生 prompt）。
   */
  async function onAiOptimize(payload: { content: string; instruction: string }): Promise<void> {
    await optimizeCurrentDocument(payload.content, payload.instruction)
  }

  /** 审阅页「生成接口文档」入口：先去检索团队规范，用户确认后再生成 */
  async function handleGenerateApiDocs(): Promise<void> {
    if (!prdContent.value.trim()) return
    goTo('rag-preview')
    await runRagRetrieve()
  }

  /** 审阅页「生成提示词套件」入口：接口文档完成后才能生成，先切到生成中状态再生成 */
  async function handleGeneratePrompts(): Promise<void> {
    if (!apiDocsContent.value.trim()) return
    goTo('generating-prompts')
    await generatePrompts(prdContent.value)
  }

  /**
   * PRD 快捷入口：直接生成 PRD，不经过摘要 Diff 闸门。
   *
   * 那道闸门的作用是把「对话里补充的信息」回填进结构化摘要，而快捷入口压根没有
   * 表单与对话——用户写的那段话本身就是唯一输入，没有任何东西需要回填与确认。
   */
  async function handleGenerateQuickPrd(): Promise<void> {
    if (entryMode.value !== 'prd-quick') return
    if (!buildPrdPayload()) return
    goTo('generating-prd')
    await generatePrd()
  }

  /**
   * 提示词调试入口：用用户贴入的 PRD 直接生成提示词套件。
   *
   * 刻意不把这份 PRD 写进 `prdContent`：那是「本工具生成的 PRD」的位置，混进用户
   * 自带的内容会让进度、完成条件与下载列表都失真。它只作为生成的输入存在。
   */
  async function handleGeneratePromptsFromDebugPrd(): Promise<void> {
    if (entryMode.value !== 'prompt-debug') return
    if (!debugPrdText.value.trim()) return
    goTo('generating-prompts')
    await generatePrompts(debugPrdText.value)
  }

  /**
   * 回到本次入口的录入页，并清空文档生成相关的错误与流式残留。
   * 返回目标随入口模式而变：完整流程回对话，快捷入口回描述，调试入口回 PRD。
   */
  function backToInput(): void {
    if (entryMode.value === 'prd-quick') {
      goTo('prd-quick')
      return
    }
    if (entryMode.value === 'prompt-debug') {
      goTo('prompt-debug')
      return
    }
    goTo('chatting')
  }

  /** 「返回」按钮的文案；与 `backToInput` 的落点保持一一对应 */
  const backLabel = computed(() => {
    if (entryMode.value === 'prd-quick') return '返回描述'
    if (entryMode.value === 'prompt-debug') return '返回 PRD'
    return '返回对话'
  })

  /**
   * 结构化表单的返回落点。
   *
   * 表单不只出现在流程开头——对话页有「修改需求」会回到这里。所以已经进过对话时
   * 要回到对话（否则用户会被莫名其妙送回入口页、丢掉对话位置），
   * 还没开始对话时才回到入口选择页。
   */
  function backFromForm(): void {
    if (messages.value.length > 0) {
      backToInput()
      return
    }
    goTo('entry')
  }

  /** 表单返回按钮的文案，与 `backFromForm` 的落点保持一一对应 */
  const formBackLabel = computed(() => (messages.value.length > 0 ? '返回对话' : '返回入口'))

  /**
   * 进入完成页：校验三份文档齐备后切到 `done`。
   * 不满足条件时不跳转（前置守卫），保证不会落到空白的完成态。
   */
  function handleEndTask(): void {
    if (!canFinishTask.value) return
    goTo('done')
  }

  /** 当前审阅阶段对应的文档标题 */
  const activeDocTitle = computed(() => {
    const map: Record<string, string> = {
      'generating-prd': 'PRD 文档',
      'review-prd': 'PRD 文档',
      'generating-api-docs': '接口文档',
      'review-api-docs': '接口文档',
      'generating-prompts': '提示词套件',
      'review-prompts': '提示词套件',
    }
    return map[viewState.value] ?? '文档'
  })

  /** 当前审阅阶段对应的文档正文（非生成态） */
  const activeDocContent = computed(() => {
    if (viewState.value === 'review-prd') return prdContent.value
    if (viewState.value === 'review-api-docs') return apiDocsContent.value
    if (viewState.value === 'review-prompts') return promptsContent.value
    return ''
  })

  // -------------------------------------------------------------------------
  // 文档下载
  // -------------------------------------------------------------------------

  /**
   * 下载文件名统一为「产品名称-文档类型」（如 `HarnessPRD-PRD.md`）。
   * 产品名优先取结构化摘要；快捷入口没有摘要，退回用户在录入页填的名称；
   * 都为空或含非法字符时由 `buildDocFilename` 兜底。
   */
  function downloadDocument(docType: DocType): void {
    const content =
      docType === 'prd'
        ? prdContent.value
        : docType === 'api-docs'
          ? apiDocsContent.value
          : promptsContent.value
    const productName = summary.value?.product_name ?? quickInput.value.productName
    downloadFile(content, buildDocFilename(docType, productName))
  }

  // -------------------------------------------------------------------------
  // 确认闸门 1：摘要回填 Diff
  // -------------------------------------------------------------------------

  /**
   * 从对话页点击「生成 PRD」：先做摘要回填并让用户确认，确认后才真正生成。
   * 生成完成后 `generatePrd` 内部会把状态推进到 review-prd。
   */
  async function handleGeneratePrd(): Promise<void> {
    if (!canGeneratePrd.value) return
    goTo('summary-diff')
    await runSummarySync()
  }

  /**
   * 调用回填接口，拿到「更新后的完整摘要」，再在前端算出逐字段 Diff。
   *
   * Diff 放在前端算而不是后端算：后端只负责产出完整结构，前端掌握自己表单的字段
   * 语义，能给出中文路径面包屑，也便于把变更定位到具体子输入框。
   */
  async function runSummarySync(): Promise<void> {
    if (!summary.value) return

    summarySyncing.value = true
    summarySyncError.value = ''
    summaryChanges.value = []
    pendingSummary.value = null

    try {
      const result = await syncSummaryFromConversation({
        summary: summary.value,
        history: conversationHistory(),
      })
      pendingSummary.value = result.summary
      summaryChanges.value = diffSummaries(summary.value, result.summary)
    } catch (error) {
      summarySyncError.value = error instanceof Error ? error.message : String(error)
    } finally {
      summarySyncing.value = false
    }
  }

  /**
   * 确认更新：把回填出的摘要写回 store，再进入 PRD 生成流程。
   * 回填失败（没有候选摘要）时跳过写入，直接用当前摘要生成——不能因为一步锦上添花的
   * 回填卡住主流程。
   */
  async function confirmSummaryDiff(): Promise<void> {
    if (summarySyncing.value) return

    if (pendingSummary.value) {
      summary.value = pendingSummary.value
      hooks.onNotice('需求摘要已更新')
    }
    pendingSummary.value = null
    summaryChanges.value = []

    goTo('generating-prd')
    await generatePrd()
  }

  /** 拒绝更改：摘要保持原样，也不进入生成流程，回到对话继续补充 */
  function rejectSummaryDiff(): void {
    if (summarySyncing.value) return
    pendingSummary.value = null
    summaryChanges.value = []
    summarySyncError.value = ''
    goTo('chatting')
    hooks.onNotice('已拒绝本次摘要更新，摘要保持不变')
  }

  // -------------------------------------------------------------------------
  // 确认闸门 2：接口规范检索（RAG）
  // -------------------------------------------------------------------------

  /** 按当前 PRD 与对话检索团队接口规范与历史接口示例 */
  async function runRagRetrieve(): Promise<void> {
    ragRetrieving.value = true
    ragError.value = ''
    ragHits.value = []

    try {
      const result = await retrieveApiDocsRag({
        prdContent: prdContent.value,
        history: conversationHistory(),
      })
      ragHits.value = result.hits
    } catch (error) {
      ragError.value = error instanceof Error ? error.message : String(error)
    } finally {
      ragRetrieving.value = false
    }
  }

  /** 确认：带着勾选的命中条目去生成接口文档 */
  async function confirmRag(hits: RagHit[]): Promise<void> {
    if (ragRetrieving.value) return
    goTo('generating-api-docs')
    await generateApiDocs(hits)
  }

  /** 取消：不调用生成接口，回到 PRD 待确认状态 */
  function cancelRag(): void {
    if (ragRetrieving.value) return
    ragHits.value = []
    ragError.value = ''
    goTo('review-prd')
    hooks.onNotice('已取消生成接口文档')
  }

  /** 清空文档流水线相关的全部临时状态（重新开始时用） */
  function reset(): void {
    clearTransient()
    activeDocKey.value = 'prd'
    prdProgress.value = null
    currentSectionChars = 0
    sectionStarted = false
    pendingSummary.value = null
    summaryChanges.value = []
    summarySyncing.value = false
    ragHits.value = []
    ragRetrieving.value = false
  }

  return {
    // 派生视图
    isGenerating,
    isGeneratingPhase,
    isReviewing,
    canFinishTask,
    activeDocTitle,
    activeDocContent,
    backLabel,
    formBackLabel,
    // 临时状态
    docStreamingContent,
    docError,
    prdStatus,
    summaryChanges,
    summarySyncing,
    summarySyncError,
    ragHits,
    ragRetrieving,
    ragError,
    // 导航
    goTo,
    // 生成与优化
    generatePrd,
    onSaveDocument,
    onAiOptimize,
    handleGenerateApiDocs,
    handleGeneratePrompts,
    handleGenerateQuickPrd,
    handleGeneratePromptsFromDebugPrd,
    backToInput,
    backFromForm,
    handleEndTask,
    downloadDocument,
    // 闸门 1
    handleGeneratePrd,
    confirmSummaryDiff,
    rejectSummaryDiff,
    // 闸门 2
    confirmRag,
    cancelRag,
    // 重置
    reset,
  }
}
