import { computed, ref } from 'vue'
import { storeToRefs } from 'pinia'

import {
  syncSummaryFromConversation,
  type GeneratePrdFromSummaryPayload,
  type StreamHandlers,
} from '@/services/api'
import { useJob, type JobStartInput } from '@/composables/useJob'
import { fetchDeliveryZip } from '@/services/documentService'
import { useDocumentsStore } from '@/stores/documents'
import { useRunsStore, type ActiveRun, type RunPreview } from '@/stores/runs'
import { useSolutionsStore } from '@/stores/solutions'
import { useWorkflowStore } from '@/stores/workflow'
import { isSubscribed } from '@/services/runSubscriptions'
import type {
  ChatMessage,
  ChatStage,
  DocKind,
  DocPhase,
  DocumentKind,
  JobSnapshot,
  PendingItem,
  PrdBoardState,
  PrdGenerationStage,
  PrdSectionCard,
  PrdStageProgress,
  RequirementsSummary,
  ReviewConclusion,
  RunSummary,
  StreamDocumentPart,
  StreamStageChunk,
  StreamStageEvent,
  SummaryChange,
  ViewState,
} from '@/types'
import {
  buildBundleFilename,
  buildDocFilename,
  downloadBlob,
  downloadFile,
  type DocType,
} from '@/utils/download'
import { formatPendingItemsForChat } from '@/utils/pendingItems'
import { diffSummaries } from '@/utils/requirements'
import {
  chatStageForDocument,
  docKindOfGeneratingState,
  docKindOfState,
  documentKindOfChatStage,
  generatingStateFor,
  jobDocumentKind,
  normalizeChatStage,
  reviewStateFor,
} from '@/utils/workflow'

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
  // 会话代次的持有者：本文件要靠它判断「这次任务的回调是不是已经过期」
  const solutions = useSolutionsStore()
  // 在途任务的运行态：判据与收尾都按「哪一支任务」来，而不是一个全局布尔
  const runs = useRunsStore()
  const {
    viewState,
    entryMode,
    summary,
    messages,
    activeChatTab,
    quickInput,
    debugPrdText,
    pastePairInput,
    generating: isGeneratingPhase,
    reviewing: isReviewing,
    canFinish: canFinishTask,
    canGeneratePrd,
  } = storeToRefs(store)

  /**
   * 是否有生成任务在跑。
   *
   * 与从前的 `docGenerating` 同名同位，但判据换成了「本标签页有一支运行中的任务」——
   * 它带 jobId，收尾时按 jobId 比对，迟到的收尾清不掉新任务。
   */
  const isGenerating = computed(() => runs.hasRunning)

  /**
   * 把 `runs.preview` 上的一个字段包成**可写 computed**。
   *
   * 读：直接读那一支运行态的预览；写：落到同一处。中间没有第二份副本，因此不存在
   * 「本地 ref 还挂着上一份方案的正文」这类问题。保留可写形态只是为了让原有的
   * 「`xxx.value = …` / `+= …`」写法继续成立，不必把几百行调用点改成 store 调用。
   */
  function previewField<K extends keyof RunPreview>(key: K) {
    return computed<RunPreview[K]>({
      get: () => runs.preview[key],
      set: (value) => {
        runs.preview[key] = value
      },
    })
  }

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
  //
  // 这六份状态（正文预览 / 错误 / PRD 看板 / 下游阶段 / 阶段输出 / 审核结论）
  // **都归运行态所有**，住在 `stores/runs` 的 `preview` 里，本文件只是它的门面。
  //
  // 为什么必须搬走：它们从前是本 composable 的局部 ref，而 composable 是 app 级单例——
  // 于是「谁在写这份预览」无人回答。切方案时旧任务的流照样往这里写，用户就在新方案里
  // 看到上一份的正文（症状是「切回去，正文还是别人的」）。搬进运行态之后，`runs` 一换、
  // 预览跟着换，`detach` 一清、迟到的写入也落空。
  //
  // 保留同名同形的**可写 computed**，是为了让「写预览」的几百行调用点一字不改：
  // 读走 store、写也走 store，中间没有第二份副本。
  // -------------------------------------------------------------------------

  /**
   * 生成中实时展示在审阅区的预览文本——**流式预览的唯一出口**。
   *
   * 接口文档 / 提示词 / AI 优化直接往里追加 `chunk`；分章节 PRD 则由
   * `syncPrdPreview()` 把「已定稿部分 + 当前章缓冲」拼好写进来。两条路径互不干扰，
   * 消费方（`GeneratingStage`）也只需要读这一个值。
   */
  const docStreamingContent = previewField('content')

  /** 文档生成 / 优化过程中的错误提示 */
  const docError = previewField('error')

  // -------------------------------------------------------------------------
  // PRD 分章节生成的阶段进度（三车道并行：管道看板）
  // -------------------------------------------------------------------------

  /**
   * PRD 管道看板：每章一个卡片，4 列只呈现阶段。
   *
   * 并行后章节可能乱序到达，因此**不能**再用「一个字符串不断追加」的预览模型；
   * 这里按 `sectionId` 存卡片、按 `index` 排序渲染，`part` 到达即覆盖该章。
   */
  const prdBoard = previewField('prdBoard')

  /** 下游文档（接口 / 提示词）当前的生成阶段；idle 表示未走该链路 */
  const docPhase = previewField('docPhase')

  /**
   * 当前阶段模型的**原始输出增量**（审核意见 / 改写稿），只用于阶段条下方的输出面板。
   *
   * 为什么单独一份而不是并进 `docStreamingContent`：那是**正文预览**，审核意见混进去
   * 会污染文档。两份分开存，切换阶段时清空即可——每个阶段的输出只属于它自己，
   * 上一阶段的文本留着会让人误以为审核还在继续。
   */
  const docStageText = previewField('stageText')

  /**
   * 本次生成的审核结论。
   *
   * **只在内存里**：后端不发 `run_summary` 之外的第二份、也不落库；刷新后不会补发，
   * 结论随之消失。这样「结论不持久化」不需要任何清理逻辑，只靠不存储即可。
   */
  const reviewConclusion = previewField('review')

  /**
   * 分章节生成的看板；不是 PRD 生成态时恒为 `null`。
   *
   * 判据用**阶段**而不是「当前正在处理哪一份」：PRD 生成必然处在 `generating-prd`，
   * 而按澄清结果改写 PRD 也走这个阶段（那时看板已被显式清空，同样得 `null`）。
   */
  const prdStatus = computed<PrdBoardState | null>(() =>
    viewState.value === 'generating-prd' ? prdBoard.value : null,
  )

  /** 预览当前是否有章节在改写（供审阅区降透明度提示） */
  const prdShowingDraft = computed(
    () => prdBoard.value?.sections.some((section) => section.phase === 'rewriting') ?? false,
  )

  /** 取（或创建）某章的卡片；新卡片按 index 插到正确位置。 */
  function sectionCard(
    board: PrdBoardState,
    sectionId: string,
    index: number,
  ): PrdSectionCard {
    let card = board.sections.find((item) => item.sectionId === sectionId)
    if (!card) {
      card = {
        sectionId,
        sectionTitle: '',
        sectionHeading: '',
        index,
        phase: 'pending',
        failed: false,
        text: '',
        note: '',
        issues: [],
      }
      board.sections.push(card)
      board.sections.sort((a, b) => a.index - b.index)
    }
    return card
  }

  /**
   * 把看板里的定稿正文拼成预览，写进 `docStreamingContent`。
   *
   * 文档头单独存（`headerText`），章节按 `index` 排序——`part` 可以乱序到达，
   * 但用户看到的文档必须始终是章节顺序。
   */
  function syncPrdPreview(): void {
    const board = prdBoard.value
    if (!board) {
      docStreamingContent.value = ''
      return
    }
    const ordered = [...board.sections].sort((a, b) => a.index - b.index)
    docStreamingContent.value =
      board.headerText + ordered.map((section) => section.text).join('')
  }

  /** 清空 PRD 看板 */
  function clearPrdPreview(): void {
    prdBoard.value = null
  }

  /** 进入 PRD 生成前的复位：先建一张空看板，等后端 stage 事件再细化 */
  function beginPrdStages(): void {
    prdBoard.value = {
      total: 0,
      headerReady: false,
      headerText: '',
      sections: [],
      review: null,
      partial: false,
      failedSections: [],
    }
  }

  /**
   * 一段正文已定稿：以权威文本覆盖该章（或文档头）。
   *
   * 并行后 `part` 可能乱序、也可能同一章因重试再次到达，因此语义是**覆盖**而不是
   * 「只认第一次」。重复订阅仍然幂等——文本相同，覆盖不会产生第二份。
   */
  function handlePrdPart(part: StreamDocumentPart): void {
    if (!part.text) return
    const board = prdBoard.value
    if (!board) return
    if (!part.sectionId) {
      // 没有 sectionId 的 `part` 只可能是文档头（后端在正文最前一次性下发）
      board.headerReady = true
      board.headerText = part.text
      syncPrdPreview()
      return
    }
    const card = sectionCard(board, part.sectionId, part.index)
    card.text = part.text
    if (part.failed) {
      // 失败占位：停在此前失败的车道（生成），不到「定稿」
      card.failed = true
    } else {
      card.failed = false
      card.phase = 'done'
    }
    syncPrdPreview()
  }

  /** 并行链路不再逐字流；保留函数仅为旧后端 / 降级路径的接线兼容。 */
  function appendPrdChunk(_chunk: string): void {
    // 故意为空：多章 chunk 会串字，也会让整章重试不安全
  }

  /**
   * 处理 `stage` 阶段事件：只更新对应章节的卡片，**不清任何别的章节**。
   *
   * 并行下车道事件是交错的（第 3 章在写、第 2 章在审、第 1 章在改），
   * 「换章就清缓冲」的旧逻辑在这里会误伤别的章。
   */
  function handlePrdStage(event: StreamStageEvent): void {
    const board = prdBoard.value
    if (!board) return
    if (event.total) board.total = event.total
    if (!event.sectionId) return

    const card = sectionCard(board, event.sectionId, event.index)
    card.sectionTitle = event.sectionTitle || card.sectionTitle
    card.sectionHeading = event.sectionHeading || card.sectionHeading
    card.note = event.note
    card.issues = event.issues

    if (event.stage === 'failed') {
      // 生成失败：停在「生成」车道，红色标记；占位正文由随后的 part 带过来
      card.phase = 'writing'
      card.failed = true
      return
    }
    if (event.stage === 'done') return
    if (event.stage === 'writing' || event.stage === 'reviewing' || event.stage === 'rewriting') {
      card.phase = event.stage
      // 进入审核/改写说明正文已产出；失败标记以随后的 part 为准
      if (event.stage !== 'writing') card.failed = false
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

  /** 清掉上一次动作留下的错误回显（跳转时用；正文与看板不清） */
  function clearErrors(): void {
    docError.value = ''
    summarySyncError.value = ''
  }

  /**
   * 重入守卫：本方案已有任务在跑时，一律不再发起新的生成。
   *
   * 从前这件事靠 `goTo` 的 busy 拦——**顺手挡住的**，而不是设计出来的：按钮之所以点不动，
   * 是因为整条导航都被闸掉了。闸门拆掉之后必须显式补上这一道，否则连点两次就是两个任务
   * 同时往同一份产物里写（后端还有一层 `supersede`，但前端不该指望它兜底）。
   *
   * 判据用「有没有在途任务」而不是「哪一份在写」：本版只有一支运行态，同方案多任务并行
   * 明确不做（见计划「不做与风险」）。
   */
  function isRunInFlight(): boolean {
    return runs.hasRunning
  }

  /**
   * 统一的状态导航入口。
   *
   * 阶段赋值收口在 store 的 `goTo`（它**不再**因为「有任务在跑」而拒绝跳转——
   * 那正是「生成中所有按钮都点不动」的根因，见 `stores/workflow.ts` 的说明）。
   *
   * ⚠️ 这里**只清错误回显，不清正文预览与看板**：生成途中跳出去看一眼别的页面、
   * 再点回进度条，用户应当还能看到刚才流到哪了。清预览是「开始新一轮生成」的事，
   * 由 `resetDocTransient()` 在各条链的入口负责。
   */
  function goTo(target: ViewState): void {
    store.goTo(target)
    clearErrors()
  }

  /**
   * 某条线程的对话（只保留角色与正文），交给后端当接续上下文。
   *
   * ⚠️ **必须按线程过滤**：四条线程共用一条消息列表，把别线程的对话一并交出去，
   * 模型会以为那些也是本轮上下文（而且人设还是另一套）。
   */
  function chatHistoryOf(stage: ChatStage): ChatMessage[] {
    return messages.value
      .filter((message) => normalizeChatStage(message.stage) === stage)
      .map(({ role, content }) => ({ role, content }))
  }

  /**
   * 交给摘要回填 / PRD 生成的历史。
   *
   * **只取需求澄清那一条线程**：PRD 生成之后，同一个列表里会接着出现三条文档线程的轮次。
   * 若把它们一并交出去，文档答疑会被当成需求输入读一遍，闸门①的 Diff 里就会凭空冒出条目。
   * 这里是那道过滤闸门，别省。
   */
  function conversationHistory(): ChatMessage[] {
    return chatHistoryOf('requirement')
  }

  // -------------------------------------------------------------------------
  // 文档生成与优化
  // -------------------------------------------------------------------------

  // -------------------------------------------------------------------------
  // 生成任务接线（任务生命周期交给 useJob；预览仍由本文件负责，单一出口不变）
  // -------------------------------------------------------------------------

  /** 任务生命周期：创建 / 重连 / 退订 */
  const job = useJob()

  /** 清掉一次生成的临时展示态（正文缓冲 / 错误 / 下游阶段 / 审核结论）。 */
  function resetDocTransient(): void {
    runs.resetPreview()
  }

  /** 审核结论：只存内存，供审阅页展示；刷新后自然消失。 */
  function handleReview(review: ReviewConclusion): void {
    reviewConclusion.value = review
    if (prdBoard.value) prdBoard.value.review = review
  }

  /**
   * 把后端的原始阶段名归一成下游阶段。
   *
   * 不认识的取值返回 `null` 而不是猜一个默认档：下游阶段条只认这四个，
   * 猜错了会让进度条停在某个看起来合理、其实没发生的阶段上。
   */
  function normalizeDocPhase(raw: string): DocPhase | null {
    if (raw === 'writing' || raw === 'reviewing' || raw === 'rewriting' || raw === 'done') {
      return raw
    }
    return null
  }

  /** 下游文档的阶段事件（生成 / 审核 / 改写 / 完成） */
  function handleDocStage(event: StreamStageEvent): void {
    const stage = normalizeDocPhase(event.stage)
    if (stage) {
      // 换阶段就清空上一阶段的输出：审核意见不该留在改写阶段的面板里
      if (docPhase.value !== stage) docStageText.value = ''
      docPhase.value = stage
    }
  }

  /**
   * 阶段内输出增量：只进附属面板，**绝不碰正文预览**。
   *
   * 增量可能来自审核也可能来自改写，归属以事件自带的 `stage` 为准；万一它跳过了
   * 对应的 `stage` 事件（订阅晚了一步），这里顺手把阶段纠正过来，避免面板标题与
   * 内容对不上。
   */
  function handleDocStageChunk(chunk: StreamStageChunk): void {
    if (chunk.stage === 'reviewing' || chunk.stage === 'rewriting') {
      if (docPhase.value !== chunk.stage) {
        docStageText.value = ''
        docPhase.value = chunk.stage
      }
    }
    docStageText.value += chunk.text
  }

  /** 下游改写稿整段覆盖初稿（`replace: true`） */
  function handleDocPart(part: StreamDocumentPart): void {
    if (part.replace) docStreamingContent.value = part.text
  }

  /**
   * 判断「某一次任务所属的会话是否已经被换掉」。
   *
   * `epoch` 是**那条链自己在启动那一刻**捕获的 `solutions.sessionEpoch`。
   *
   * ⚠️ 这里刻意**不保存任何「当前任务代次」的共享变量**：用户完全可能先后发起两个任务
   * （方案 A 生成中 → 删掉 A → 新建方案 B 再生成）。若用一个共享变量记「最近一次任务的
   * 代次」，B 启动时会把它覆盖成新会话的值；此时 A 的迟到事件拿共享值自检，恰好等于
   * 新值，于是被判成「没过期」——A 的正文就写进了 B 的预览与文档。那比原 bug 更难查：
   * 内容不丢不报错，只是串到了另一份方案里。
   *
   * 因此代次只以「局部常量」的形式存在，随调用一路传下去：handlers 闭包、`await` 之后的
   * 判断、`finishDoc` 的守卫，用的都是同一条链自己那个值。
   */
  function isExpired(epoch: number): boolean {
    return solutions.sessionEpoch !== epoch
  }

  /**
   * 这次结果所属的那一支任务，是否已经被别的任务顶替。
   *
   * 与 `isExpired` 是两个正交的轴，必须**同时**判：
   * - `isExpired` 管「整份会话被换掉」（删方案 / 切方案 / 开新方案）；
   * - 本函数管「同一份会话里换了任务」——比如上一支刚收尾、用户又发了一次。
   *   这时代次没变，只有 `jobId` 能分辨，光看代次会让旧任务的迟到回调写进新任务的预览。
   *
   * `run` 为 `null` 表示任务压根没建起来（创建请求就失败了），那时没有身份可比，
   * 交给 `isExpired` 兜底——不能因为「没有身份」就把真正的失败原因也一起吞掉。
   */
  function isStaleRun(run: ActiveRun | null): boolean {
    return run !== null && !runs.holds(run.jobId)
  }

  /**
   * 给一组 handlers 套上会话代次守卫：会话被换掉之后到达的事件一律静默丢弃。
   *
   * 收口在这一处，是因为 `handlersForKind` 是**所有**流事件进入本文件的唯一入口
   * （生成 / 优化 / 澄清 / 自动澄清 / 自动重连都从这里取 handlers），套在出口上
   * 就不会漏掉任何一条链路，将来新增一种回调也只需要在这里登记一次。
   *
   * 丢弃必须是静默的：用户已经离开那个会话，这不是错误，没有人需要看到提示。
   *
   * `epoch` 由调用方传入（而不是在这里读任何共享状态），闭包只捕获这个局部值——
   * 这样「后一个任务覆盖前一个任务的代次」在结构上就不可能发生。
   */
  function guardJobHandlers(raw: StreamHandlers, epoch: number): StreamHandlers {
    const guarded: StreamHandlers = {}
    if (raw.onStatus) {
      guarded.onStatus = (text) => {
        if (!isExpired(epoch)) raw.onStatus?.(text)
      }
    }
    if (raw.onChunk) {
      guarded.onChunk = (text) => {
        if (!isExpired(epoch)) raw.onChunk?.(text)
      }
    }
    if (raw.onPart) {
      guarded.onPart = (part) => {
        if (!isExpired(epoch)) raw.onPart?.(part)
      }
    }
    if (raw.onStage) {
      guarded.onStage = (event) => {
        if (!isExpired(epoch)) raw.onStage?.(event)
      }
    }
    if (raw.onStageChunk) {
      guarded.onStageChunk = (chunk) => {
        if (!isExpired(epoch)) raw.onStageChunk?.(chunk)
      }
    }
    if (raw.onSnapshot) {
      guarded.onSnapshot = (snapshot) => {
        if (!isExpired(epoch)) raw.onSnapshot?.(snapshot)
      }
    }
    if (raw.onRunSummary) {
      guarded.onRunSummary = (summary) => {
        if (!isExpired(epoch)) raw.onRunSummary?.(summary)
      }
    }
    if (raw.onReview) {
      guarded.onReview = (review) => {
        if (!isExpired(epoch)) raw.onReview?.(review)
      }
    }
    return guarded
  }

  /**
   * 按文档种类给出事件 handlers，把任务事件映射到本文件的预览状态。
   *
   * PRD 走三段预览（已定稿 + 当前缓冲 + 上一版草稿）；其余文档只有一段流式缓冲，
   * 外加「生成 → 审核 → 改写」阶段与整段覆盖。
   * 快照回调对两者都接——刷新重连时用它补全断线前已产出的正文。
   *
   * `epoch` 由调用方在**发起任务之前**捕获并传进来：本函数不读也不写任何共享代次，
   * 每个任务的 handlers 只认自己那一个值。
   */
  function handlersForKind(kind: DocKind, epoch: number): StreamHandlers {
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
      return guardJobHandlers(
        {
          onPart: handlePrdPart,
          onStage: handlePrdStage,
          onSnapshot: applyJobSnapshot,
          onReview: handleReview,
          onRunSummary,
        },
        epoch,
      )
    }
    return guardJobHandlers(
      {
        onChunk: (chunk) => {
          docStreamingContent.value += chunk
        },
        // 改写稿整段覆盖初稿；没有审查/改写时这两个回调都不会触发
        onPart: handleDocPart,
        onStage: handleDocStage,
        // 审核 / 改写的原始输出：只进阶段条下方的面板，不进正文
        onStageChunk: handleDocStageChunk,
        onSnapshot: applyJobSnapshot,
        onReview: handleReview,
        onRunSummary,
      },
      epoch,
    )
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
      // 只在看板还空着时用快照重建：重复快照不该把已流出的内容倒退回去
      if (!prdBoard.value && snap.completedSections.length > 0) {
        const board: PrdBoardState = {
          total: snap.completedSections.length,
          headerReady: false,
          headerText: '',
          sections: [],
          review: null,
          partial: snap.partial,
          failedSections: snap.failedSections,
        }
        for (const [position, section] of snap.completedSections.entries()) {
          board.sections.push({
            sectionId: section.sectionId,
            sectionTitle: '',
            sectionHeading: section.heading,
            index: typeof section.index === 'number' ? section.index : position,
            phase: section.failed ? 'writing' : 'done',
            failed: section.failed === true,
            text: section.text,
            note: '',
            issues: [],
          })
        }
        board.sections.sort((a, b) => a.index - b.index)
        // 快照的 finalizedText 是「文档头 + 已定稿章节」；把章节部分减掉即文档头。
        // 旧快照缺 index 时按数组序兜底，拿不准就留空——落稿以 done.reply 为准。
        const sectionsText = board.sections.map((section) => section.text).join('')
        if (sectionsText && snap.finalizedText.endsWith(sectionsText)) {
          board.headerText = snap.finalizedText.slice(
            0,
            snap.finalizedText.length - sectionsText.length,
          )
          board.headerReady = board.headerText.length > 0
        }
        prdBoard.value = board
        syncPrdPreview()
      }
      return
    }
    // 其余文档只有一段流式缓冲：同样只在空着时用快照初始化，避免重复快照覆盖已流出的内容
    if (!docStreamingContent.value) {
      // 正文 =「已定稿」+「实时缓冲」。改写成功后初稿已被 `part{replace}` 整段覆盖，
      // 此时只剩 `finalizedText`、缓冲为空；生成途中的情况则相反。只看 `liveBuffer`
      // 会让「刷新时正赶上改写完成」的那一瞬把预览清空。
      const restored = `${snap.finalizedText}${snap.liveBuffer}`
      if (restored) docStreamingContent.value = restored
    }
    // 阶段与阶段输出：重连不会补发历史 `stage` / `stage_chunk` 事件，只能从快照恢复。
    // 两者都只在「还是空的」时回填——同一任务被订阅两次时，第二份快照不该把已经
    // 流出去的文字覆盖回去。
    const phase = normalizeDocPhase(snap.phase)
    if (phase) docPhase.value = phase
    else if (snap.status === 'running') docPhase.value = 'writing'
    if (!docStageText.value && snap.stageText) docStageText.value = snap.stageText
  }

  /**
   * 把某份文档的结果展示出来，并按种类推进阶段。
   *
   * **这里不落库**——后端在任务收尾时已经把同一份正文写进 `documents` 了。本函数只做
   * 两件事：把 `done.reply` 立即放进工作副本（用户不必等一次网络往返才看到内容），
   * 以及把界面带到对应的待确认阶段（优化除外，它留在原地）。
   *
   * 与后端的对齐由 `handlersForKind` 的 `onRunSummary` 负责（那一刻后端已经写完），
   * 因此这里不必再拉一次。
   *
   * ## 不抢方向盘（D1）
   *
   * 只有用户**此刻正看着这一份的生成页**，才把他带到审阅页——他正在等这个结果，
   * 跳过去正是他要的。
   *
   * 用户已经在别处（生成途中点开进度条去看 PRD、切去对话补充一句）时**不跳**：
   * 他正在做别的事，把界面拽走等于抢方向盘，而且还可能把他正在读的那一页顶掉。
   * 这种情况用右上角浮层说一声「生成好了」，去不去看由他决定。
   * 正文该写还是照写（写库与界面在哪一页无关）。
   *
   * @param kind 做了什么（生成 / 优化 / 澄清）
   * @param reply 后端产出的完整正文
   * @param target 写回**哪一份**产物；由调用方显式给出
   * @param epoch 本次任务启动时捕获的会话代次；由调用方传入，不读任何共享状态
   */
  function finishDoc(kind: DocKind, reply: string, target: DocumentKind, epoch: number): void {
    // 会话已被换掉（删除了方案 / 切到了别的方案）：这份迟到的结果属于上一份会话。
    // 写进去就会把刚清空的会话重新填满，并让自动保存以为「有内容了」。
    // 用**调用方自己那条链**的代次判断：读共享的「当前任务代次」会让后一个任务
    // 覆盖前一个任务的快照，A 的正文就串进 B（见 `isExpired`）。
    if (isExpired(epoch)) return
    const content = reply.trim()
    if (!content) return
    // 就地替换：写回 `target` 那一份。`kind` 只说「做了一件什么事」，写哪一份由 `target`
    // 回答——调用方本来就知道（生成任务就是它的产物、优化看当前审阅页、澄清看当前标签）。
    // 刻意不再用「当前正在处理的文档」那种内存变量去猜：它**在导航到审阅页时不会被赋值**，
    // 于是把接口文档的优化结果写进了 PRD（见 ADR-028 记录的那处修复）。
    documents.setLocal(target, content)
    // 后端在 `done` 之前就已经把正文写进库了（「写正文的唯一入口 = job 成功收尾」），
    // 所以这一句报的是**已经发生的事实**，不是「打算写」。方案列表的内容时间据此前进：
    // 正文不经过会话快照，`workflow` 那套计数看不到它。
    documents.markContentWritten()
    // 优化留在原地：审阅页就在眼前，跳一下反而多余
    if (kind === 'optimize') return

    const generatingState = generatingStateFor(target)
    if (generatingState !== null && viewState.value === generatingState) {
      goTo(reviewStateFor(target))
      return
    }
    // 用户已不在这一份的生成页：只提示，不跳转（见上方「不抢方向盘」）
    hooks.onNotice(`${titleOf(target)}已生成完毕，可在进度条上查看`)
  }

  /**
   * 整篇重新生成一份产物后，作废**它自己与下游**的澄清线程。
   *
   * 与「整篇重生成才作废下游」（后端 `document_store.DOWNSTREAM_KINDS`）同口径：
   * PRD 重生成 → 三份都作废；接口文档重生成 → 接口 + 提示词；提示词只作废自己。
   *
   * 为什么要作废：旧线程聊的是**上一版**的待确认项。留着它，用户点「按澄清结果改写」
   * 就会把针对旧文档的结论写进新文档——一次静默的错写，而且看起来还挺合理。
   * 只在生成**成功**后作废：失败时后端不覆写正文（下游也会被重新拉回来），线程该留着。
   */
  /**
   * 生成成功后就地作废**这一份文档自己**的澄清线程。
   *
   * ⚠️ 下游不再删除：上游换版只会在下游留下 `stale` 标记，正文与它的澄清线程都还在。
   * 因此这里只丢弃 `kind` 自己的线程——旧线程聊的是上一版待确认项，留着会让用户
   * 把针对旧文档的结论写进新文档；下游线程讨论的仍是现存的那份文档，继续有效。
   */
  function dropThreadsFor(kind: DocumentKind): void {
    store.dropChatThread(chatStageForDocument(kind))
  }

  /** 把 PRD 的请求参数折成任务输入 */
  function prdJobInput(payload: GeneratePrdFromSummaryPayload): JobStartInput {
    return {
      summary: payload.summary ?? null,
      requirements: payload.requirements ?? '',
      productName: payload.productName ?? '',
      history: payload.history ?? [],
      // 只有显式 `false`（快捷流程）才关掉审核；`undefined` 让后端取配置
      reviewEnabled: payload.reviewEnabled,
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
   * 因此「该任务的订阅已在途」时直接返回，绝不重复订阅。
   *
   * 判据是**订阅登记表**（按 jobId 记）而不是一个「最近在重连谁」的数字：
   * 后者只能记住最近那一个，切方案后两个任务各重连一次就互相覆盖了。
   */
  async function reconnectJob(jobId: number): Promise<void> {
    if (isSubscribed(jobId)) return
    await reconnectAndSettle(jobId)
  }

  /**
   * 任务种类 → 这次要落进哪一份产物。
   *
   * 三种生成任务的种类就是产物；`optimize` / `clarify` 是动作，目标另行反推：
   * - 优化**不切阶段**，用户就停在那份文档的审阅页上，`viewState` 是权威（而且它是持久化的）；
   * - 澄清改写**先切到 `generating-<kind>` 再发任务**（见 `applyClarification`），
   *   阶段本身就是答案。
   *
   * 反推而不是另存一个「任务目标」字段：这两处的阶段本来就已经因为别的理由被持久化了，
   * 再存一份就是第二个真源。
   */
  function docKeyForJob(kind: DocKind): DocKey {
    const own = jobDocumentKind(kind)
    if (own) return own
    if (kind === 'optimize') return docKindOfState(viewState.value) ?? 'prd'
    return docKindOfGeneratingState(viewState.value) ?? 'prd'
  }

  /** `reconnectJob` 的实际动作（与幂等守卫分离，便于阅读与测试） */
  async function reconnectAndSettle(jobId: number): Promise<void> {
    // 重连不改阶段（阶段已由快照恢复保留），因此产物种类必须**先**确定下来：
    // 有指针就听指针的（`doc_kind` 是后端权威，`optimize`/`clarify` 也能答），
    // 没有才退回按 `viewState` 反推。
    const pointer = runs.pointer
    const kind = pointer?.jobKind ?? store.activeJobKind ?? 'prd'
    const target = pointer?.docKind ?? docKeyForJob(kind)
    // 别的任务已经占着这一支运行态：本次要重连的任务属于更早的一轮，直接放弃。
    // （同一标签页里同时跟两支任务是本期明确不做的事，见计划「不做与风险」——
    //  最坏是看不到旧任务，绝不把两路事件写进同一份预览。）
    if (runs.activeRun !== null && !runs.holds(jobId)) return
    // 登记这一支运行态：`useJob.reconnect` 凭它认领事件，`isStaleRun` 凭它判过期
    if (!runs.holds(jobId)) {
      runs.restore({ jobId, solutionId: solutions.currentId, jobKind: kind, docKind: target })
    }
    resetDocTransient()
    if (kind === 'prd') {
      beginPrdStages()
    } else {
      prdBoard.value = null
    }

    // 重连也是一次独立订阅：代次必须在发起之前定格，并沿这条链一路带着走
    const epochAtStart = solutions.sessionEpoch
    const { ok, reply, run } = await job.reconnect(jobId, handlersForKind(kind, epochAtStart))
    // 重连期间会话被换掉、或这一支已被别的任务顶替：这次结果属于别人，不写错误、不落稿
    if (isStaleRun(run) || isExpired(epochAtStart)) return
    const snap = job.snapshot.value
    // 下游阶段与阶段输出由 `applyJobSnapshot` 在**快照那一刻**回填（那是唯一正确的时机：
    // 重连不会补发历史 `stage` 事件，而事后读 `job.snapshot.value` 拿到的是订阅开始时的
    // 旧 phase，会把已经流到改写阶段的指示器倒回生成阶段）。
    if (!ok) {
      docError.value = job.error.value || '生成失败'
      prdBoard.value = null
    } else if (snap?.status === 'failed') {
      docError.value = snap.errorMessage || '生成失败'
      prdBoard.value = null
    } else {
      // 落点由 `finishDoc` 自己决定：优化留在原审阅页，生成 / 澄清都回到那份文档的审阅页
      finishDoc(kind, reply, target, epochAtStart)
    }
  }

  /**
   * 本次生成要不要复核（后端 `review_enabled`）。
   *
   * 快捷流程一律 `false`：**不审不改**是它当初的定义（ADR-034），这一点对三份产物
   * 同样成立——接口文档与提示词在快捷流程里也是一遍过，否则「快捷」只剩 PRD 那一半。
   * 其余入口返回 `undefined`：要不要复核由后端配置决定，前端不替它挑默认值。
   */
  function reviewEnabledForJob(): boolean | undefined {
    return entryMode.value === 'prd-quick' ? false : undefined
  }

  /**
   * 按当前入口模式组装「生成 PRD」的请求体。
   *
   * 两条入口只是需求来源不同，因此这里返回同一个请求结构，交给后端汇合到
   * 同一条链路；返回 `null` 表示当前模式还没有可用的输入（如快捷入口还没写需求），
   * 调用方据此直接放弃，不要发出一个注定失败的请求。
   *
   * 两处差异都是**入口形态**造成的，不涉及「怎么写 PRD」：
   * - 快捷流程的需求是那段自由文本，结构化摘要为空；
   * - 快捷流程不跑逐章审核/改写（`reviewEnabled: false`）——输入只有一段话，
   *   逐章复核的边际收益低，省下的调用直接体现在等待时间上。
   */
  function buildPrdPayload(): GeneratePrdFromSummaryPayload | null {
    if (entryMode.value === 'prd-quick') {
      const { productName, requirements } = quickInput.value
      if (!requirements.trim()) return null
      // 快捷流程没有需求澄清对话（一段话直接进生成），`history` 正常是空数组；
      // 仍照传是为了让**旧快照**里残留的需求对话结论不白丢（那时确实聊过）。
      return { requirements, productName, history: conversationHistory(), reviewEnabled: false }
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
    if (isRunInFlight()) return
    const payload = buildPrdPayload()
    if (!payload) return
    // 重新生成 PRD **不再删除下游**：旧的接口文档 / 提示词会留着，并在收尾后由后端
    // 按 `derived_from` 的指纹比较标成「可能已过期」。因此这里不清本地副本，
    // 交给收尾的 `documents.reload()` 把 stale 标记拉回来。
    resetDocTransient()
    beginPrdStages()
    // 代次在**发起之前**定格：这条链的 handlers、await 之后的判断与 `finishDoc` 全都用它
    const epochAtStart = solutions.sessionEpoch
    const { reply, ok, run } = await job.start(
      'prd',
      prdJobInput(payload),
      handlersForKind('prd', epochAtStart),
    )
    // 订阅期间会话被换掉、或这一支已被别的任务顶替：成功 / 失败都不要再写回本文件的状态
    if (isStaleRun(run) || isExpired(epochAtStart)) return
    if (!ok) {
      docError.value = job.error.value || 'PRD 生成失败'
      // 失败时收掉阶段条：原因由错误提示说明，留着一个「进行中」的指示反而误导
      prdBoard.value = null
      return
    }
    finishDoc('prd', reply, 'prd', epochAtStart)
    // 只作废 PRD 自己的澄清线程；下游正文还在，它们的线程继续有效
    dropThreadsFor('prd')
  }

  /** 生成接口文档：以 PRD 正文为输入，团队接口规范由技能包全量注入，无需前端检索确认 */
  async function generateApiDocs(): Promise<void> {
    if (isRunInFlight()) return
    // 提示词不再删除：重新生成接口文档后，它只会被标成 stale（见 generatePrd 的说明）
    resetDocTransient()
    prdBoard.value = null
    // 先把阶段条点亮：整篇生成是最长的一步，等后端 `stage` 事件到达才显示的话，
    // 用户在这段时间里只看到一片空白（后端随后会补发同一条 writing 事件，幂等）
    docPhase.value = 'writing'
    // 同 `generatePrd`：代次在发起之前定格，供本次订阅的回调与收尾使用
    const epochAtStart = solutions.sessionEpoch
    const { reply, ok, run } = await job.start(
      'api-docs',
      // 快捷流程一遍过：整篇生成完就收，不审也不改（见 `reviewEnabledForJob`）
      { prdContent: documents.contentOf('prd'), reviewEnabled: reviewEnabledForJob() },
      handlersForKind('api-docs', epochAtStart),
    )
    if (isStaleRun(run) || isExpired(epochAtStart)) return
    if (!ok) {
      docError.value = job.error.value || '接口文档生成失败'
      return
    }
    finishDoc('api-docs', reply, 'api-docs', epochAtStart)
    // 只作废接口文档自己的线程；提示词还在，它的线程继续有效
    dropThreadsFor('api-docs')
  }

  /**
   * 生成提示词套件：以 PRD 正文为输入。
   *
   * PRD 来源随入口模式而变（完整流程用本次生成的 PRD，调试入口用用户贴入的 PRD），
   * 因此由调用方把正文传进来，而不是在这里猜该读哪份状态。
   */
  async function generatePrompts(prdText: string, apiDocsText = ''): Promise<void> {
    if (isRunInFlight()) return
    resetDocTransient()
    prdBoard.value = null
    // 与接口文档同理：阶段条立即亮起，别让用户在整篇生成期间盯着空白
    docPhase.value = 'writing'
    const epochAtStart = solutions.sessionEpoch
    const { reply, ok, run } = await job.start(
      'prompts',
      // 接口文档是可选的第二份材料：不传等于没有，生成口径与从前一致
      { prdContent: prdText, apiDocsContent: apiDocsText, reviewEnabled: reviewEnabledForJob() },
      handlersForKind('prompts', epochAtStart),
    )
    if (isStaleRun(run) || isExpired(epochAtStart)) return
    if (!ok) {
      docError.value = job.error.value || '提示词生成失败'
      return
    }
    finishDoc('prompts', reply, 'prompts', epochAtStart)
    // 新一版提示词 = 它自己的旧澄清线程作废
    dropThreadsFor('prompts')
  }

  /**
   * 优化指定文档（按用户指令）。
   *
   * `content` 由审阅区传入，可能包含用户尚未保存的编辑；必须以它为准，
   * 否则优化结果会把用户刚改的内容覆盖掉。
   *
   * **必须带 `docKind`**：`optimize` 这个种类只说明「做了一件优化」，后端要落库还得知道
   * 「优化的是哪一份」。漏传的后果不是报错，而是后端**拒绝落库**（宁可本次不写，也不猜
   * 一个默认值写错位置）。
   *
   * 目标是**当前审阅页的那一份**——判据取 `viewState` 而不是某个「当前正在处理的文档」
   * 内存变量：那个变量只在生成流程里被写入，用户打开一份旧方案直接点「AI 优化」时它还是
   * 初值 `'prd'`，于是接口文档的优化结果会被写进 PRD（见 ADR-028 记录的那处修复）。
   */
  async function optimizeCurrentDocument(content: string, instruction: string): Promise<void> {
    if (isRunInFlight()) return
    if (!content.trim() || !instruction.trim()) return
    const target = docKindOfState(viewState.value)
    if (!target) return

    resetDocTransient()
    const epochAtStart = solutions.sessionEpoch
    const { reply, ok, run } = await job.start(
      'optimize',
      { document: content, instruction, docKind: target },
      handlersForKind('optimize', epochAtStart),
    )
    if (isStaleRun(run) || isExpired(epochAtStart)) return
    if (!ok) {
      // 优化失败：保留原文档，错误已写入 docError
      docError.value = job.error.value || '优化失败'
      return
    }
    finishDoc('optimize', reply, target, epochAtStart)
  }

  // -------------------------------------------------------------------------
  // 文档澄清（PRD / 接口文档 / 提示词三条线程，共用一个对话页）
  //
  // 三条线程走**同一条路**：切到对话页（`chatting`）并把当前标签切到这份文档的线程 →
  // 若这条线程还没聊过，由调用方发开场白（材料 = 该文档正文 + 它的待确认清单）→
  // 聊够了点「按澄清结果改写」，交给后端就地改写那一份。
  //
  // 历史上它们是两条路：PRD 复用主对话栏，另两条走独立的 `clarifying` 页与
  // `clarifyMessages` 字段。ADR-028 把两条并成一条，独立澄清页与那个阶段一并退役。
  // -------------------------------------------------------------------------

  /**
   * 审阅页「回到对话」入口：切到对话页，并把当前标签设成这份文档的澄清线程。
   *
   * 只做「摆好现场」——**不清空聊天记录**（接着聊正是这个流程存在的意义）、换标签、切阶段；
   * **开场白由调用方发**，因为聊天那摊状态（流式缓冲、消息列表）归 `useClarificationChat`
   * 管，不在本文件里。
   *
   * 这条线程已经聊过就不再重新开场：用户进出几次，看到的是同一段对话在继续，而不是每次
   * 回来都被问候一遍「我们来确认剩余问题」。
   *
   * @returns 本次澄清的材料（正文 + 清单文本）；返回 `null` 表示没有可澄清的正文，
   *          或这条线程已经开过场。
   */
  function startDocumentClarification(
    kind: DocumentKind,
  ): { document: string; pendingItems: string } | null {
    const content = documents.contentOf(kind)
    if (!content.trim()) return null

    const stage = chatStageForDocument(kind)
    // 先设标签再切阶段：对话页挂载时就要渲染对的那条线程，而不是先渲染一条再跳过去
    store.chatTab = stage
    goTo('chatting')

    if (messages.value.some((message) => normalizeChatStage(message.stage) === stage)) return null

    return {
      document: content,
      // 清单摊成可读的行再交给后端：模型要在对话里逐条讨论它们，「第 2 条」这种指代才成立
      pendingItems: formatPendingItemsForChat(documents.pendingItemsOf(kind)),
    }
  }

  /**
   * 对话页主操作「按澄清结果改写」：把当前标签这条线程的对话交给后端，改写对应文档。
   *
   * 走 `clarify` 任务（而非裸 SSE）：改写可能跑很久，任务化之后刷新 / 断线只断订阅，
   * 后端照跑，重连时凭 `activeJobId` 接上——与三份文档的生成完全同一套机制。
   *
   * **先切到 `generating-<kind>` 再发起任务**：改写要跑十几秒，全程停在对话栏用户看不到
   * 任何进展（流式正文只有 `GeneratingStage` 会渲染）。切阶段必须放在 `job.start`
   * **之前**——它内部登记运行态，此后 `goTo` 会被「有任务在跑」拦下。
   * 失败时留在生成视图，由 `GeneratingStage` 的错误区说明原因（后端失败不覆写正文）。
   *
   * ⚠️ **必须带 `docKind`**：它既是「改写结果写回哪一份」的依据，也是「注入哪一套产出规范」
   * 的依据。少了它后端不会猜，而是拒绝落库——那这次澄清就白做了。
   *
   * 目标来自**当前标签**：改写期间没人能换标签（对话页那时已卸载，`busy` 也拦着），因此
   * 快照里那份标签就是无歧义的目标。从前靠一个 `clarifyTarget` 字段，还得在改写前把它
   * 「钉死」成 `'prd'` 才不出错——那个 hack 随字段一起删掉了。
   *
   * ⚠️ 读的是**实际显示**的那条（`activeChatTab`）而不是 `chatTab`：后者是用户选中的意图，
   * 在产物暂时缺席时可能指向一条点不动的线程，而屏幕上那时显示的是另一条。
   */
  async function applyClarification(): Promise<void> {
    if (isRunInFlight()) return
    const target = documentKindOfChatStage(activeChatTab.value)
    if (!target) return
    const document = documents.contentOf(target)
    if (!document.trim()) return
    const history = chatHistoryOf(activeChatTab.value)
    if (history.length === 0) return

    // 就地改写、不是分章节链路：清掉可能残留的阶段进度，否则生成页会顶出一条与本次无关的
    // 章节条（`goTo` 内部的 `clearTransient` 不碰 `prdBoard`）
    prdBoard.value = null
    // 阶段先于任务切（理由见上）；`goTo` 顺带清空上一轮的流式缓冲 / 错误 / PRD 三段预览
    goTo(generatingStateFor(target) ?? 'generating-prd')

    const epochAtStart = solutions.sessionEpoch
    const { reply, ok, run } = await job.start(
      'clarify',
      // 只回传 role / content：后端按同一形状读历史（与 `optimize` 的入参口径一致）
      { document, history, docKind: target },
      handlersForKind('clarify', epochAtStart),
    )
    if (isStaleRun(run) || isExpired(epochAtStart)) return
    if (!ok) {
      // 改写失败：留在生成视图，由 `GeneratingStage` 的错误区展示原因（后端失败不覆写正文）
      docError.value = job.error.value || '按澄清结果改写失败'
      return
    }
    // 落点（回到那份文档的审阅页）由 `finishDoc` 负责；对话留在 `messages` 里，可再回去补充
    finishDoc('clarify', reply, target, epochAtStart)
    hooks.onNotice(`已按澄清结果更新 ${titleOf(target)}`)
  }

  /**
   * 正文草稿变化 → 交给文档 store 落库。
   *
   * 由审阅页的编辑框**逐次输入**触发（`DocumentReview` 的 `draftChange`），不是「用户点了保存」。
   * 去抖（800ms）与单飞都在 store 里（见 `stores/documents.ts::save`）：连敲键盘只有最后一次
   * 生效、任一时刻至多一个 PUT 在途；本地工作副本同步更新，界面不必等往返。
   *
   * ⚠️ **这里不发任何提示**：落库是后台行为，成功也弹一次会把界面淹没。失败由 `documents.saveError`
   * 承载，`AppV2` 监听它闪一条红色提示。
   */
  function onSaveDocument(content: string): void {
    const kind = docKindOfState(viewState.value)
    if (!kind) return
    documents.save(kind, content)
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
   * 审阅页「AI 自行澄清」：让 AI 替用户把剩余待确认问题逐条拍板，并直接改写正文。
   *
   * 与「按澄清结果改写」的区别只有**谁作答**：那条要求用户先聊出结论，这条由模型按
   * 最保守的行业默认值自行决策（提示词见后端 `AUTO_CLARIFY_PROMPT`，前端只递清单数据）。
   *
   * 为什么先切到 `generating-<kind>` 再发任务：它要跑几十秒且输出整篇正文，留在审阅页
   * 只能干等。复用生成态那条流式预览，用户至少能看到字在往下走。
   * ⚠️ `goTo` 必须在 `job.start` **之前**——它登记运行态之后，`goTo` 会拦掉跳转。
   */
  async function autoClarify(): Promise<void> {
    if (isRunInFlight()) return
    const target = docKindOfState(viewState.value)
    if (!target) return
    const content = documents.contentOf(target)
    const items = documents.pendingItemsOf(target)
    if (!content.trim() || items.length === 0) return
    const generating = generatingStateFor(target)
    if (!generating) return

    resetDocTransient()
    goTo(generating)
    const epochAtStart = solutions.sessionEpoch
    const { reply, ok, run } = await job.start(
      'auto-clarify',
      { document: content, pendingItems: items, docKind: target },
      handlersForKind('auto-clarify', epochAtStart),
    )
    if (isStaleRun(run) || isExpired(epochAtStart)) return
    if (!ok) {
      docError.value = job.error.value || 'AI 自行澄清失败'
      return
    }
    finishDoc('auto-clarify', reply, target, epochAtStart)
  }

  /**
   * 我有 PRD入口：用用户贴入的 PRD 直接生成提示词套件。
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
   * 双粘贴入口：用用户贴入的 PRD（+ 可选的接口文档）生成提示词套件。
   *
   * 与 `handleGeneratePromptsFromDebugPrd` 同源，只多带一份材料。同样**不把这两份正文
   * 交付给文档 store**：那是「本工具生成的产物」的位置，混进用户自带的内容会让进度、
   * 完成条件与下载列表都失真。它们只作为本次生成的输入存在。
   */
  async function handleGeneratePromptsFromPair(): Promise<void> {
    if (entryMode.value !== 'prd-and-api-paste') return
    const { prd, apiDocs } = pastePairInput.value
    if (!prd.trim()) return
    goTo('generating-prompts')
    await generatePrompts(prd, apiDocs)
  }

  /**
   * 回到本次入口的录入页，并清空文档生成相关的错误与流式残留。
   * 返回目标随入口模式而变：完整流程回对话，快捷入口回描述，调试入口回 PRD。
   *
   * ⚠️ 两个从零创建的入口（完整流程 / 快捷流程）**不再渲染**这个按钮——它们的录入页
   * 就在进度条上，点第 1 步即可（见 `inputPageOnBar`）。这里仍保留正确的落点，
   * 因为「回到我的录入页」是一个独立于按钮是否存在的动作。
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
    if (entryMode.value === 'prd-and-api-paste') {
      goTo('prd-and-api-paste')
      return
    }
    goTo('chatting')
  }

  /**
   * 「返回」按钮的文案；与 `backToInput` 的落点保持一一对应。
   *
   * 只为两个**导入**入口而存在：它们的录入页不在进度条上，审阅页 / 交付页必须留一个
   * 出口。两个从零创建的入口不渲染这个按钮（`hideBack`），因此这里也不再给它们文案。
   */
  const backLabel = computed(() => {
    if (entryMode.value === 'prompt-debug') return '返回 PRD'
    if (entryMode.value === 'prd-and-api-paste') return '返回素材'
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

  /** 这份产物的展示名；澄清与审阅共用一套说法，不各写一份 */
  function titleOf(kind: DocumentKind): string {
    if (kind === 'api-docs') return '接口文档'
    if (kind === 'prompts') return '提示词套件'
    return 'PRD 文档'
  }

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
   *
   * `content` 给出时以它为准：审阅页必须取**组件里的草稿**（`DocumentReview` 暴露的
   * `draftContent`），它可能比 store 的工作副本更新（还在 800ms 防抖窗口里）；完成页不传，
   * 读工作副本即可。
   */
  function downloadDocument(docType: DocType, content?: string): void {
    const productName = summary.value?.product_name ?? quickInput.value.productName
    downloadFile(content ?? documents.contentOf(docType), buildDocFilename(docType, productName))
  }

  /**
   * 审阅页「下载」：把当前这份文档（含用户最新编辑）导出为 `.md`。
   *
   * 「当前是哪一份」由 `viewState` 反推（`docKindOfState`），与 `onSaveDocument` 同一口径——
   * 不另存一个「当前下载目标」字段，否则两个字段会各自漂移。空正文直接返回：`downloadFile`
   * 本来也会静默丢弃（底栏的禁用态已经先挡了一道，这里只是兜底）。
   */
  function onDownloadDocument(content: string): void {
    if (!content) return
    const kind = docKindOfState(viewState.value)
    if (!kind) return
    downloadDocument(kind, content)
  }

  /**
   * 交付页「下载交付包」：由后端把三份产物 + README 打成 ZIP。
   *
   * 为什么不在前端拼：正文真源在后端（`document_versions`），前端只有工作副本；
   * 而且交付包要按**落库版本**打，不是眼前这份草稿。
   */
  async function downloadBundle(): Promise<void> {
    const id = documents.solutionId
    if (id === null) return
    try {
      const blob = await fetchDeliveryZip(id)
      const productName = summary.value?.product_name ?? quickInput.value.productName
      downloadBlob(blob, buildBundleFilename(productName))
    } catch (error) {
      docError.value = error instanceof Error ? error.message : '导出交付包失败'
    }
  }

  // -------------------------------------------------------------------------
  // 确认闸门 1：摘要回填 Diff
  // -------------------------------------------------------------------------

  /**
   * 从对话页点击「生成 PRD」：先做摘要回填并让用户确认，确认后才真正生成。
   * 生成完成后 `generatePrd` 内部会把状态推进到 review-prd。
   */
  async function handleGeneratePrd(): Promise<void> {
    if (isRunInFlight()) return
    // 快捷流程既没有结构化摘要（闸门①无处可写），也没有需求对话（「至少一轮 AI 回复」
    // 这个前提不存在）。它的校验只有一条：那段话写了没有。校验通过就直接进生成。
    if (entryMode.value === 'prd-quick') {
      if (!quickInput.value.requirements.trim()) return
      // ⚠️ `goTo` 必须在 `generatePrd` 之前：生成态置位后 busy 守卫会拦下重复发起
      goTo('generating-prd')
      await generatePrd()
      return
    }
    if (!canGeneratePrd.value) return
    // 完整流程保留这道闸门：那边真有一份可以回填的摘要。
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
    // 「重新开始」要连预览一起归零：正文 / 看板 / 阶段 / 阶段输出 / 审核结论 / 错误
    resetDocTransient()
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
  docPhase,
  docStageText,
  reviewConclusion,
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
    autoClarify,
    handleGeneratePromptsFromDebugPrd,
    handleGeneratePromptsFromPair,
    backToInput,
    backFromForm,
    handleEndTask,
    downloadDocument,
    onDownloadDocument,
    downloadBundle,
    // 文档澄清：进入对话页的某条线程 / 按澄清结果改写
    startDocumentClarification,
    applyClarification,
    // 闸门 1
    handleGeneratePrd,
    confirmSummaryDiff,
    rejectSummaryDiff,
    // 重置
    reset,
  }
}
