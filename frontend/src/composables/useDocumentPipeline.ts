import { computed, ref } from 'vue'
import { storeToRefs } from 'pinia'

import {
  syncSummaryFromConversation,
  type GeneratePrdFromSummaryPayload,
  type StreamHandlers,
} from '@/services/api'
import { useJob, type JobStartInput } from '@/composables/useJob'
import { useDocumentsStore } from '@/stores/documents'
import { useWorkflowStore } from '@/stores/workflow'
import type {
  ChatMessage,
  DocKind,
  DocumentKind,
  JobSnapshot,
  PendingItem,
  PrdGenerationStage,
  PrdStageProgress,
  RequirementsSummary,
  RunSummary,
  StreamDocumentPart,
  StreamStageEvent,
  SummaryChange,
  ViewState,
} from '@/types'
import { buildDocFilename, downloadFile, type DocType } from '@/utils/download'
import { formatPendingItemsForChat } from '@/utils/pendingItems'
import { diffSummaries } from '@/utils/requirements'
import { docKindOfState, reviewStateFor } from '@/utils/workflow'

/** 受管的文档 key（与 `DocumentKind` 同集：产物种类不含 `optimize`） */
type DocKey = DocumentKind

/** 文档流水线需要向外部借用的能力 */
export interface PipelineHooks {
  /** 展示一条短暂提示 */
  onNotice: (text: string) => void
}

/**
 * 文档生成、审阅与摘要确认闸门。
 *
 * 覆盖从「点击生成 PRD」到「进入完成页」的全部编排：文档流式生成与优化、
 * 摘要回填 Diff 确认、下载与完成守卫。
 *
 * **按入口模式分流**只体现在三个地方：需求从哪来（结构化摘要 / 一段话）、
 * 「返回」回到哪个录入页、以及完成所需的最低文档集合；生成链路本身完全共用。
 *
 * 阶段本身（`viewState`）落在 workflow store，三份产物正文落在 documents store
 * （后端 `documents` 表在前端的工作副本）。这里只持有「这一次请求 / 这一次确认」的
 * 临时状态，因此刷新页面不会被脏状态影响。
 *
 * **生成结果不由这里落库**：后端在任务收尾时已经把正文写进 `documents`，本文件只负责
 * 「即时展示 `done.reply`」+「拉一次权威版本对齐」。前端因此不需要知道「正文该存哪」，
 * 也就不可能出现「前端存了一份、后端存了另一份」。
 */
export function useDocumentPipeline(hooks: PipelineHooks) {
  const store = useWorkflowStore()
  const documents = useDocumentsStore()
  const {
    viewState,
    entryMode,
    summary,
    messages,
    quickInput,
    debugPrdText,
    clarifyMessages,
    clarifyTarget,
    docGenerating: isGenerating,
    generating: isGeneratingPhase,
    reviewing: isReviewing,
    canFinish: canFinishTask,
    canGeneratePrd,
  } = storeToRefs(store)

  /**
   * 当前审阅阶段应该显示的回执。
   *
   * 由 `viewState` 反推产物种类，从文档 store 取那一份的回执——这正是
   * 「回看 PRD 显示 PRD 回执」的落点。生成中等非审阅态一律返回 `null`
   * （审阅区那时不展示回执）。
   *
   * 回执的来源是后端 `documents.last_run_summary`，不再由前端在生成收尾时自己记：
   * 那样做需要前端知道「优化改的是哪一份」，而后端的 `doc_kind` 才是权威。
   */
  const runSummary = computed<RunSummary | null>(() => {
    const kind = docKindOfState(viewState.value)
    return kind ? documents.runSummaryOf(kind) : null
  })

  /**
   * 当前审阅阶段应该显示的待确认清单。
   *
   * 与 `runSummary` 同源：由 `viewState` 反推产物种类，从文档 store 取那份的清单。
   * 生成中等非审阅态返回空数组（审阅区那时不展示清单）。清单由后端随正文落库，
   * 前端只读取、不修改——刷新后重新拉取即可对齐，无需额外状态。
   */
  const pendingItems = computed<PendingItem[]>(() => {
    const kind = docKindOfState(viewState.value)
    return kind ? documents.pendingItemsOf(kind) : []
  })

  // -------------------------------------------------------------------------
  // 交互态
  // -------------------------------------------------------------------------

  /**
   * 生成中实时展示在审阅区的预览文本——**流式预览的唯一出口**。
   *
   * 接口文档 / 提示词 / AI 优化直接往里追加 `chunk`；分章节 PRD 则由
   * `syncPrdPreview()` 把「已定稿部分 + 当前章缓冲」拼好写进来。两条路径互不干扰，
   * 消费方（`GeneratingStage`）也只需要读这一个值。
   */
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

  /**
   * 分章节生成：**已定稿**部分的累积文本（文档头 + 各章定稿正文）。
   *
   * 内容全部来自后端的 `part` 事件——也就是最终会写进 `done.reply` 的那份文本。
   * 不用 `chunk` 自己拼的原因：`chunk` 是模型原始输出，还差「补标题行」
   * 「补章间空行」两道加工，前端看不到它们（曾表现为标题消失、与上一段粘成一行）。
   */
  const prdFinalizedText = ref('')

  /** 当前章节的实时缓冲：本章 `chunk` 的原始增量，定稿时被 `part` 整段替换 */
  const prdLiveBuffer = ref('')

  /**
   * 改写期间留着的上一版草稿。
   *
   * 改写要重新流一整章，中间隔着一次完整的模型往返（十几秒甚至更久）。若此时直接
   * 清空缓冲，用户看到的就是「这一章突然没了」——按设计它只是想表达「正在重写」。
   * 这里把旧文本留下（审阅区会降透明度并标注），新内容一流出就顶替它。
   */
  const prdRewriteGhost = ref('')

  /**
   * 已并入预览的章节 id 集合（文档头单独用一个标记）。
   *
   * 预览是**累积**出来的（`prdFinalizedText += part.text`），所以必须能识别重复投递：
   * 同一章（或文档头）的 `part` 只认第一次，重复到达直接丢弃。
   *
   * 没有这道防线时，任何让「同一个任务被订阅两次」的路径都会把两路事件交错写进
   * 同一份缓冲——表现为文档里出现两遍「1. 产品概述」，表格单元格成对重复
   * （`| 编号 | 编号 | 功能模块 | 功能模块 |`）。这两个标记只做守卫、不参与渲染，
   * 因此用普通变量而不是 `ref`，避免给每次增量都挂上无谓的响应式开销。
   */
  let deliveredSectionIds = new Set<string>()
  let headerDelivered = false

  /** 分章节生成是否处于「写入中」等阶段（非 PRD 文档恒为 false） */
  const prdStatus = computed<PrdStageProgress | null>(() =>
    activeDocKey.value === 'prd' ? prdProgress.value : null,
  )

  /** 预览当前显示的是否是「上一版草稿」（供审阅区降透明度提示） */
  const prdShowingDraft = computed(
    () => !prdLiveBuffer.value && prdRewriteGhost.value.trim().length > 0,
  )

  /**
   * 把「已定稿部分 + 当前缓冲」拼成预览，写进 `docStreamingContent`。
   *
   * 三段状态都在本文件内变更，因此由变更处主动同步，而不是派生成 computed：
   * `docStreamingContent` 同时被接口文档 / 提示词 / AI 优化直接追加使用，
   * 保持它作为「预览的唯一出口」可以不动那些路径。
   */
  function syncPrdPreview(): void {
    docStreamingContent.value =
      prdFinalizedText.value + (prdLiveBuffer.value || prdRewriteGhost.value)
  }

  /** 清空分章节预览的三段缓冲，并复位「已定稿」守卫 */
  function clearPrdPreview(): void {
    prdFinalizedText.value = ''
    prdLiveBuffer.value = ''
    prdRewriteGhost.value = ''
    deliveredSectionIds = new Set<string>()
    headerDelivered = false
  }

  /** 进入 PRD 生成前的复位：先按「撰写中」占位，等后端 stage 事件再细化 */
  function beginPrdStages(): void {
    clearPrdPreview()
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

  /**
   * 一段正文已定稿：以权威文本整段并入「已定稿部分」。
   *
   * 同时丢掉实时缓冲——它装的是这一段的原始 `chunk`，此刻已被加工后的版本取代。
   * 这样预览与最终 `reply` 逐字一致，「标题凭空消失」「与上一段粘连」这类偏差
   * 从结构上不可能再出现，而不是靠两边各自拼得一样。
   *
   * **幂等**：章节按 `sectionId`、文档头按「是否已收到」去重。重复投递的 `part`
   * 被直接丢弃——这是「同一任务被订阅两次」也污染不了预览的结构性保证。
   */
  function handlePrdPart(part: StreamDocumentPart): void {
    if (!part.text) return
    if (part.sectionId) {
      if (deliveredSectionIds.has(part.sectionId)) return
      deliveredSectionIds.add(part.sectionId)
    } else {
      // 没有 sectionId 的 `part` 只可能是文档头（后端在正文最前一次性下发）
      if (headerDelivered) return
      headerDelivered = true
    }
    prdFinalizedText.value += part.text
    prdLiveBuffer.value = ''
    prdRewriteGhost.value = ''
    syncPrdPreview()
  }

  /** PRD 正文的增量追加：只进「当前章节缓冲」，本章定稿时由 `part` 整段替换 */
  function appendPrdChunk(chunk: string): void {
    prdLiveBuffer.value += chunk
    syncPrdPreview()
  }

  /** 把后端下发的阶段名归一成前端联合类型（未知取值按「撰写中」处理） */
  function toPrdStage(stage: string): PrdGenerationStage {
    const known: PrdGenerationStage[] = ['writing', 'reviewing', 'rewriting', 'done']
    return (known as string[]).includes(stage) ? (stage as PrdGenerationStage) : 'writing'
  }

  /**
   * 处理 `stage` 阶段事件。
   *
   * 前端只维护「已定稿部分 + 当前章缓冲」，所以这里**不需要按字符数猜截断点**：
   * - `writing`：新的一章开始，丢掉上一章的缓冲（它要么已 `part` 定稿，
   *   要么是被放弃的草稿）；
   * - `rewriting`：把当前缓冲留作「上一版草稿」，避免中间十几秒的空白期。
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
      prdLiveBuffer.value = ''
      prdRewriteGhost.value = ''
      syncPrdPreview()
      return
    }

    if (stage === 'rewriting') {
      prdRewriteGhost.value = prdLiveBuffer.value
      prdLiveBuffer.value = ''
      syncPrdPreview()
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
  // 导航
  // -------------------------------------------------------------------------

  /** 清掉属于「本次请求 / 本次确认」的临时错误与半截流式内容 */
  function clearTransient(): void {
    docError.value = ''
    docStreamingContent.value = ''
    clearPrdPreview()
    summarySyncError.value = ''
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

  // -------------------------------------------------------------------------
  // 生成任务接线（任务生命周期交给 useJob；预览仍由本文件负责，单一出口不变）
  // -------------------------------------------------------------------------

  /** 任务生命周期：创建 / 重连 / 退订 */
  const job = useJob()

  /**
   * 按文档种类给出事件 handlers，把任务事件映射到本文件的预览状态。
   *
   * PRD 走三段预览（已定稿 + 当前缓冲 + 上一版草稿）；其余文档只有一段流式缓冲。
   * 快照回调对两者都接——刷新重连时用它补全断线前已产出的正文。
   */
  function handlersForKind(kind: DocKind): StreamHandlers {
    /**
     * 任务收尾时与后端对齐一次工作副本。
     *
     * 后端在**广播 `run_summary` 之前**已经写完 `documents`，所以这一刻拉回来的
     * 就是权威版本：正文（与 `done.reply` 逐字一致）、待确认清单、用量回执一次拿全。
     *
     * 成功与失败都拉：失败时后端**没有**覆写正文，拉一次正好把「本次生成前被清掉的
     * 下游产物」恢复回来（重新生成 PRD 会先清空下游的本地副本）。
     */
    const onRunSummary = (): void => {
      void documents.reload()
    }
    if (kind === 'prd') {
      return {
        onChunk: appendPrdChunk,
        onPart: handlePrdPart,
        onStage: handlePrdStage,
        onSnapshot: applyJobSnapshot,
        onRunSummary,
      }
    }
    return {
      onChunk: (chunk) => {
        docStreamingContent.value += chunk
      },
      onSnapshot: applyJobSnapshot,
      onRunSummary,
    }
  }

  /**
   * 应用任务快照：把「断线前已产出的正文」补齐到预览（刷新重连的落点）。
   *
   * PRD 还原三段状态与阶段进度；其余文档直接用快照的实时缓冲。
   *
   * **只在预览还空着时用它初始化**：快照是「订阅开始时的一次补全」，理应发生在任何
   * live 事件之前；若同一任务被订阅两次，第二份快照会在正文已经流出一段之后到达，
   * 此时无条件覆盖会把已产出的内容**倒退**回去。守卫住这一点，重复快照就退化成
   * 一次无害的 no-op，最多让阶段指示器刷新一下。
   */
  function applyJobSnapshot(snap: JobSnapshot): void {
    if (snap.kind === 'prd') {
      const untouched =
        !prdFinalizedText.value && !prdLiveBuffer.value && deliveredSectionIds.size === 0 && !headerDelivered
      if (untouched) {
        prdFinalizedText.value = snap.finalizedText
        prdLiveBuffer.value = snap.liveBuffer
        prdRewriteGhost.value = ''
        // 重建「已定稿」守卫：快照里的章节不该被随后的 live `part` 再拼一遍
        headerDelivered = snap.finalizedText.length > 0
        deliveredSectionIds = new Set(
          snap.completedSections.map((section) => section.sectionId).filter((id) => id !== ''),
        )
      }
      if (snap.phase) {
        const last = snap.completedSections[snap.completedSections.length - 1]
        prdProgress.value = {
          stage: toPrdStage(snap.phase),
          sectionId: last?.sectionId ?? '',
          sectionTitle: '',
          sectionHeading: last?.heading ?? '',
          index: snap.completedSections.length,
          total: 0,
          note: '',
        }
      }
      syncPrdPreview()
      return
    }
    // 其余文档只有一段流式缓冲：同样只在空着时用快照初始化，避免重复快照覆盖已流出的内容
    if (!docStreamingContent.value) docStreamingContent.value = snap.liveBuffer
  }

  /**
   * 把某份文档的生成结果展示出来，并按种类推进阶段。
   *
   * **这里不落库**——后端在任务收尾时已经把同一份正文写进 `documents` 了。本函数只做
   * 两件事：把 `done.reply` 立即放进工作副本（用户不必等一次网络往返才看到内容），
   * 以及在非优化场景下切到对应的待确认阶段。
   *
   * 与后端的对齐由 `handlersForKind` 的 `onRunSummary` 负责（那一刻后端已经写完），
   * 因此这里不必再拉一次。
   */
  function finishDoc(kind: DocKind, reply: string): void {
    const content = reply.trim()
    if (!content) return
    if (kind === 'optimize' || kind === 'clarify') {
      // 就地替换：写回**当前正在看的那一份**。`kind` 只说「做了一件什么事」，
      // 是哪一份由 `activeDocKey`（已在任务开始时按 doc_kind 定好）回答。
      // 也不在这里跳阶段——优化留在原地；澄清由调用方决定改写完成后去哪。
      documents.setLocal(activeDocKey.value, content)
      return
    }
    documents.setLocal(kind, content)
    goTo(reviewStateFor(kind))
  }

  /** 生成结束后清掉任务指针：任务已终态，刷新无需再重连 */
  function clearActiveJob(): void {
    store.activeJobId = null
    store.activeJobKind = null
  }

  /** 把 PRD 的请求参数折成任务输入 */
  function prdJobInput(payload: GeneratePrdFromSummaryPayload): JobStartInput {
    return {
      summary: payload.summary ?? null,
      requirements: payload.requirements ?? '',
      productName: payload.productName ?? '',
      history: payload.history ?? [],
    }
  }

  /**
   * 刷新后静默重连一个进行中的任务：先收快照补全正文，再接 live。
   *
   * 由 AppV2 在恢复快照后调用——**无需用户点击**。按快照 status 分流：
   * running 续流 / completed 落稿 / failed 保 partial + 展示错误。
   *
   * **对同一任务幂等**：一进入就要把预览复位，若同一个任务被并发重连两次，
   * 后一次会在前一路已经流出正文之后把预览清空，两路事件随即交错写入同一份缓冲。
   * 因此「已经开始重连该任务」时直接返回，绝不重复订阅。
   */
  let reconnectingJobId: number | null = null
  async function reconnectJob(jobId: number): Promise<void> {
    if (reconnectingJobId === jobId) return
    reconnectingJobId = jobId
    try {
      await reconnectAndSettle(jobId)
    } finally {
      if (reconnectingJobId === jobId) reconnectingJobId = null
    }
  }

  /**
   * 任务种类 → 这次要落进哪一份产物。
   *
   * 三种生成任务的种类就是产物；`optimize` / `clarify` 是动作，目标来自别处：
   * 优化看当前正在看的那份，澄清看快照里的 `clarifyTarget`。
   */
  function docKeyForJob(kind: DocKind): DocKey {
    if (kind === 'api-docs' || kind === 'prompts') return kind
    if (kind === 'clarify') return clarifyTarget.value ?? 'prd'
    return 'prd'
  }

  /** `reconnectJob` 的实际动作（与幂等守卫分离，便于阅读与测试） */
  async function reconnectAndSettle(jobId: number): Promise<void> {
    const kind = store.activeJobKind ?? 'prd'
    activeDocKey.value = docKeyForJob(kind)
    docStreamingContent.value = ''
    docError.value = ''
    if (kind === 'prd') {
      beginPrdStages()
    } else {
      prdProgress.value = null
    }

    const result = await job.reconnect(jobId, handlersForKind(kind))
    const snap = job.snapshot.value
    if (!result.ok) {
      docError.value = job.error.value || '生成失败'
      prdProgress.value = null
    } else if (snap?.status === 'failed') {
      docError.value = snap.errorMessage || '生成失败'
      prdProgress.value = null
    } else {
      finishDoc(kind, result.reply)
      // 澄清的落点不是 `generating-*`（那条链路不切阶段，用户一直留在澄清页），
      // 因此改写跑完之后要显式回到那份文档的审阅页——否则会停在澄清页看一份旧正文。
      if (kind === 'clarify') goTo(reviewStateFor(activeDocKey.value))
    }
    clearActiveJob()
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
    // 重新生成 PRD 会让基于它的后续文档失效。**后端在收尾落稿时会一并删掉下游**
    // （见 `document_store.DOWNSTREAM_KINDS`），这里同步清掉本地工作副本，
    // 进度条才会立刻退回 PRD 那一步，而不是等收尾回来才动。
    documents.setLocal('api-docs', '')
    documents.setLocal('prompts', '')
    activeDocKey.value = 'prd'
    docStreamingContent.value = ''
    docError.value = ''
    beginPrdStages()
    const { reply, ok } = await job.start('prd', prdJobInput(payload), handlersForKind('prd'))
    if (!ok) {
      docError.value = job.error.value || 'PRD 生成失败'
      // 失败时收掉阶段条：原因由错误提示说明，留着一个「进行中」的指示反而误导
      prdProgress.value = null
      clearActiveJob()
      return
    }
    finishDoc('prd', reply)
    clearActiveJob()
  }

  /** 生成接口文档：以 PRD 正文为输入，团队接口规范由技能包全量注入，无需前端检索确认 */
  async function generateApiDocs(): Promise<void> {
    // 重新生成接口文档会让基于它的提示词失效（后端收尾时同样会删掉下游）
    documents.setLocal('prompts', '')
    activeDocKey.value = 'api-docs'
    docStreamingContent.value = ''
    docError.value = ''
    prdProgress.value = null
    const { reply, ok } = await job.start(
      'api-docs',
      { prdContent: documents.contentOf('prd') },
      handlersForKind('api-docs'),
    )
    if (!ok) {
      docError.value = job.error.value || '接口文档生成失败'
      clearActiveJob()
      return
    }
    finishDoc('api-docs', reply)
    clearActiveJob()
  }

  /**
   * 生成提示词套件：以 PRD 正文为输入。
   *
   * PRD 来源随入口模式而变（完整流程用本次生成的 PRD，调试入口用用户贴入的 PRD），
   * 因此由调用方把正文传进来，而不是在这里猜该读哪份状态。
   */
  async function generatePrompts(prdText: string): Promise<void> {
    activeDocKey.value = 'prompts'
    docStreamingContent.value = ''
    docError.value = ''
    prdProgress.value = null
    const { reply, ok } = await job.start(
      'prompts',
      { prdContent: prdText },
      handlersForKind('prompts'),
    )
    if (!ok) {
      docError.value = job.error.value || '提示词生成失败'
      clearActiveJob()
      return
    }
    finishDoc('prompts', reply)
    clearActiveJob()
  }

  /**
   * 优化指定文档（按用户指令）。
   *
   * `content` 由审阅区传入，可能包含用户尚未保存的编辑；必须以它为准，
   * 否则优化结果会把用户刚改的内容覆盖掉。
   *
   * **必须带 `docKind`**：`optimize` 这个种类只说明「做了一件优化」，后端要落库还得知道
   * 「优化的是哪一份」。漏传的后果不是报错，而是后端**拒绝落库**（宁可本次不写，也不猜
   * 一个默认值写错位置）——所以这里的 `activeDocKey` 是必需信息，不是可选提示。
   */
  async function optimizeCurrentDocument(content: string, instruction: string): Promise<void> {
    if (!content.trim() || !instruction.trim()) return

    docStreamingContent.value = ''
    docError.value = ''
    const { reply, ok } = await job.start(
      'optimize',
      { document: content, instruction, docKind: activeDocKey.value },
      handlersForKind('optimize'),
    )
    if (!ok) {
      // 优化失败：保留原文档，错误已写入 docError
      docError.value = job.error.value || '优化失败'
      clearActiveJob()
      return
    }
    finishDoc('optimize', reply)
    clearActiveJob()
  }

  /**
   * 审阅页「逐条澄清」入口：进入澄清页，并把要澄清的那一份记下来。
   *
   * 只做「摆好现场」——记目标、清掉上一轮澄清对话、切阶段；**开场白由调用方发**，
   * 因为聊天那摊状态（流式缓冲、消息列表）归 `useClarificationChat` 管，不在本文件里。
   *
   * @returns 本次澄清的材料（正文 + 清单文本）；返回 `null` 表示当前没有可澄清的文档。
   */
  function startClarification(): { document: string; pendingItems: string } | null {
    const kind = docKindOfState(viewState.value)
    if (!kind) return null
    const content = documents.contentOf(kind)
    if (!content.trim()) return null

    clarifyTarget.value = kind
    clarifyMessages.value = []
    store.clarifyConversationId = ''
    goTo('clarifying')

    return {
      document: content,
      // 清单摊成可读的行再交给后端：模型要在对话里逐条讨论它们，「第 2 条」这种指代才成立
      pendingItems: formatPendingItemsForChat(documents.pendingItemsOf(kind)),
    }
  }

  /** 澄清页「返回」：回到这次澄清的那份文档的审阅页（对话本身留在快照里，可再进来继续） */
  function backFromClarifying(): void {
    goTo(reviewStateFor(clarifyTarget.value ?? 'prd'))
  }

  /**
   * 澄清页主操作「按澄清结果改写」：把这一轮对话交给后端改写文档。
   *
   * 走 `clarify` 任务（而非裸 SSE）：改写可能跑很久，任务化之后刷新 / 断线只断订阅，
   * 后端照跑，重连时凭 `activeJobId` 接上——与三份文档的生成完全同一套机制。
   *
   * ⚠️ **必须带 `docKind`**：它既是「改写结果写回哪一份」的依据，也是「注入哪一套产出规范」
   * 的依据。少了它后端不会猜，而是拒绝落库——那这次澄清就白做了。
   */
  async function applyClarification(): Promise<void> {
    const target = clarifyTarget.value
    if (!target) return
    const document = documents.contentOf(target)
    if (!document.trim()) return
    if (clarifyMessages.value.length === 0) return

    activeDocKey.value = target
    docError.value = ''
    const { reply, ok } = await job.start(
      'clarify',
      {
        document,
        // 只回传 role / content：后端按同一形状读历史（与 `optimize` 的入参口径一致）
        history: clarifyMessages.value.map(({ role, content }) => ({ role, content })),
        docKind: target,
      },
      handlersForKind('clarify'),
    )
    if (!ok) {
      // 改写失败：留在澄清页，原文档分毫未动（后端失败不覆写）
      docError.value = job.error.value || '按澄清结果改写失败'
      clearActiveJob()
      return
    }
    finishDoc('clarify', reply)
    clearActiveJob()
    goTo(reviewStateFor(target))
    hooks.onNotice('已按澄清结果更新文档')
  }

  /**
   * 审阅页「保存」：把编辑后的正文提交给后端。
   *
   * 走文档 store 的防抖单飞队列（见 `stores/documents.ts::save`）：连续输入会合并成一次
   * 请求，且任一时刻至多一个 PUT 在途。本地工作副本同时更新，界面不必等往返。
   */
  function onSaveDocument(content: string): void {
    const kind = docKindOfState(viewState.value)
    if (!kind) return
    documents.save(kind, content)
    hooks.onNotice('文档已保存')
  }

  /**
   * 审阅页「AI 优化」：优化指令由审阅组件的页内输入区收集，
   * 这里只负责驱动流式优化并写回结果（不使用浏览器原生 prompt）。
   */
  async function onAiOptimize(payload: { content: string; instruction: string }): Promise<void> {
    await optimizeCurrentDocument(payload.content, payload.instruction)
  }

  /** 审阅页「生成接口文档」入口：接口规范由技能包注入，直接进入生成态 */
  async function handleGenerateApiDocs(): Promise<void> {
    if (!documents.contentOf('prd').trim()) return
    goTo('generating-api-docs')
    await generateApiDocs()
  }

  /** 审阅页「生成提示词套件」入口：接口文档完成后才能生成，先切到生成中状态再生成 */
  async function handleGeneratePrompts(): Promise<void> {
    if (!documents.contentOf('api-docs').trim()) return
    goTo('generating-prompts')
    await generatePrompts(documents.contentOf('prd'))
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
   * 刻意不把这份 PRD 交付给文档 store：那是「本工具生成的 PRD」的位置，混进用户
   * 自带的内容会让进度、完成条件与下载列表都失真。它只作为本次生成的输入存在。
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
    if (viewState.value === 'clarifying') return clarifyTargetTitle.value
    return map[viewState.value] ?? '文档'
  })

  /** 这份产物的展示名；澄清页与审阅页共用一套说法，不各写一份 */
  function titleOf(kind: DocumentKind): string {
    if (kind === 'api-docs') return '接口文档'
    if (kind === 'prompts') return '提示词套件'
    return 'PRD 文档'
  }

  /**
   * 这次澄清针对的那份文档叫什么。
   *
   * 澄清页不新增步骤，进度条与标题都跟着 `clarifyTarget` 走——否则澄清接口文档时标题会
   * 显示「PRD 文档」，用户第一眼就以为自己走错了地方。
   */
  const clarifyTargetTitle = computed(() => titleOf(clarifyTarget.value ?? 'prd'))

  /** 当前审阅阶段对应的文档正文（非生成态） */
  const activeDocContent = computed(() => {
    const kind = docKindOfState(viewState.value)
    return kind ? documents.contentOf(kind) : ''
  })

  // -------------------------------------------------------------------------
  // 文档下载
  // -------------------------------------------------------------------------

  /**
   * 下载文件名统一为「产品名称-文档类型」（如 `PRDwright-PRD.md`）。
   * 产品名优先取结构化摘要；快捷入口没有摘要，退回用户在录入页填的名称；
   * 都为空或含非法字符时由 `buildDocFilename` 兜底。
   */
  function downloadDocument(docType: DocType): void {
    const productName = summary.value?.product_name ?? quickInput.value.productName
    downloadFile(documents.contentOf(docType), buildDocFilename(docType, productName))
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

  /** 清空文档流水线相关的全部临时状态（重新开始时用） */
  function reset(): void {
    clearTransient()
    activeDocKey.value = 'prd'
    prdProgress.value = null
    pendingSummary.value = null
    summaryChanges.value = []
    summarySyncing.value = false
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
  prdShowingDraft,
  // 用量观测：运行摘要（生成后展示，按当前审阅文档取对应桶）+ 已用时（生成中展示）
  runSummary,
  pendingItems,
  elapsedMs: job.elapsedMs,
    summaryChanges,
    summarySyncing,
    summarySyncError,
    // 导航
    goTo,
    // 生成与优化
    generatePrd,
    reconnectJob,
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
    // 文档澄清：进入澄清页 / 按澄清结果改写 / 从澄清页返回
    startClarification,
    applyClarification,
    backFromClarifying,
    clarifyTarget,
    clarifyTargetTitle,
    // 闸门 1
    handleGeneratePrd,
    confirmSummaryDiff,
    rejectSummaryDiff,
    // 重置
    reset,
  }
}
