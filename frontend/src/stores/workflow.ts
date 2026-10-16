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

import { CHAT_STAGES } from '@/types'
import type {
  ChatMessage,
  ChatStage,
  DocKind,
  EntryMode,
  PastePairInput,
  QuickPrdInput,
  RequirementsSummary,
  SessionDataV2,
  Step,
  ViewState,
} from '@/types'
import { useDocumentsStore } from '@/stores/documents'
import {
  canGeneratePrdFrom,
  documentKindOfChatStage,
  DOWNGRADE_NOTICE,
  emptyConversationIds,
  farthestContentStepIndex,
  hasRequiredDocuments,
  isDocReviewPhase,
  isGeneratingPhase,
  normalizeChatStage,
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
  'prd-and-api-paste': 'prd-and-api-paste',
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

  /**
   * 全部澄清线程的消息；正在流式生成的那条不在其中。
   *
   * 四条线程（需求 / PRD / 接口 / 提示词）**共用这一个数组**，按 `ChatMessage.stage`
   * 区分归属（见 `ChatStage`）。对话页只渲染当前标签那一条，摘要回填只读 `requirement`
   * 那一条——两处过滤都由 `stage` 这一道标记兜住。
   */
  const messages = ref<ChatMessage[]>([])

  /**
   * 对话页当前停留在哪条线程。
   *
   * 它决定渲染哪一段对话、底栏给哪个主操作（需求澄清给「生成 PRD」，其余给「按澄清结果改写」）。
   * 随快照持久化：刷新回来应当仍在原来那条线程里，而不是莫名其妙地又能再生成一次 PRD。
   */
  const chatTab = ref<ChatStage>('requirement')

  /**
   * 各线程的后端会话 id（按 `ChatStage` 取）。
   *
   * 四条线程是**四个后端会话**：需求澄清与文档澄清的人设不同（`clarification` vs
   * `doc-clarification`），三条文档线程又各自带着不同文档的开场白——共用一个会话会让
   * 上下文串味。前端合并展示、后端分开登记，各取所需。
   *
   * 收成一个表而不是四个字段：刷快照要同步的字段少一个，就少一次「改了这个忘了存那个」。
   */
  const conversationIds = ref<Record<ChatStage, string>>(emptyConversationIds())

  /** 快捷流程入口的录入内容（产品名 + 一段话描述） */
  const quickInput = ref<QuickPrdInput>({ productName: '', requirements: '' })

  /** 我有 PRD入口（「我有 PRD」）里用户贴入的 PRD 正文 */
  const debugPrdText = ref('')

  /**
   * 「我有 PRD + 接口文档」入口里用户贴入的两份正文。
   *
   * 与 `debugPrdText` **分开存**：两条入口虽然都是「贴材料」，但它们在快照里是两页各自的
   * 草稿——合用一个字段会让「在这页贴了、退回入口、又从另一页进去」时看到上一页的内容，
   * 而用户的理解是「我换了一条路，输入框该是空的」。
   */
  const pastePairInput = ref<PastePairInput>({ prd: '', apiDocs: '' })

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

  // 澄清线程的历史沿革（免得下一个人重蹈覆辙）：
  // ADR-025 为了让闸门①读不到文档答疑，曾把接口文档 / 提示词的澄清做成**完全独立的一套**
  // 字段（`clarifyMessages` / `clarifyConversationId` / `clarifyTarget`）并配一个独立页面；
  // ADR-027 先让 PRD 澄清回到主对话栏（共用 `messages` + `stage` 标记）；本版把剩下两条
  // 也并了进来。分开的理由（防闸门①污染）经核实不成立——闸门①只比对结构化摘要，
  // 回填也只读 `requirement` 的消息。于是「一套字段 + 一个开关 + 一个页面」全部退役，
  // 换成「一个消息数组 + 一个 stage 标记 + 一个对话页的四条线程」。

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
      chatTab: chatTab.value,
      conversationIds: conversationIds.value,
      quickInput: quickInput.value,
      debugPrdText: debugPrdText.value,
      pastePairInput: pastePairInput.value,
      activeJobId: activeJobId.value,
      activeJobKind: activeJobKind.value,
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
    quickInput.value = session.quickInput
    debugPrdText.value = session.debugPrdText
    pastePairInput.value = session.pastePairInput
    activeJobId.value = session.activeJobId ?? null
    activeJobKind.value = session.activeJobKind ?? null
    // 澄清线程随快照一起恢复，因此刷新后能接着聊（也就**不需要**为它们降级）。
    // `conversationIds` 是个对象，缺字段的旧快照要逐键兜底，不能整个替换。
    chatTab.value = session.chatTab ?? 'requirement'
    conversationIds.value = { ...emptyConversationIds(), ...(session.conversationIds ?? {}) }
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
      chatTab,
      conversationIds,
      quickInput,
      debugPrdText,
      pastePairInput,
      activeJobId,
      activeJobKind,
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
   * 对话页子标签栏里**可用**的那些线程。
   *
   * 判据是「这条线程要澄清的东西准备好了没有」：
   * - 需求澄清只存在于完整流程——另外两条入口压根没有那条对话；
   * - 三条文档线程看对应产物有没有正文（没正文就没什么可澄清的）。
   *
   * 不可用的线程**照样渲染**（置灰、不可点）：标签常驻，用户才知道后面还有哪些可以聊。
   */
  const availableChatTabs = computed<ChatStage[]>(() =>
    CHAT_STAGES.filter((stage) => {
      const kind = documentKindOfChatStage(stage)
      if (!kind) return (entryMode.value ?? 'structured') === 'structured'
      if (kind === 'prd') return documents.hasPrd
      if (kind === 'api-docs') return documents.hasApiDocs
      return documents.hasPrompts
    }),
  )

  /**
   * 对话页**实际显示**的那条线程。
   *
   * `chatTab` 是「用户选中的那条」（进快照、刷新后照原样恢复），本值是「此刻真的能显示的
   * 那条」——两者在产物还没到位时会短暂分叉。
   *
   * 做成派生值，而不是「发现不可用就改写 `chatTab`」，是因为改写会引入一个**时序依赖**：
   * 打开一份停在「接口澄清」的方案时，快照先落地、`documents` 后到达，中间那一小段时间里
   * 「接口文档还没有正文」是事实，于是标签被改写成需求澄清——等正文到了也回不去了，
   * 用户上次停在哪条线程上就这么静默丢了。派生之后没有写入，也就没有时序问题；
   * 而 `chatTab` 里的**意图**留着，正文一回来视图自动回到那条线程上。
   */
  const activeChatTab = computed<ChatStage>(() => {
    const tabs = availableChatTabs.value
    if (tabs.includes(chatTab.value)) return chatTab.value
    return tabs[0] ?? 'requirement'
  })

  /**
   * 当前阶段在进度条上的下标。
   *
   * 停在对话页的**文档线程**上时算个例外：那是在「对某一篇文档做澄清」，而
   * `STEP_OF_STATE` 是静态表，只能按 `chatting` 给一格。澄清接口文档时进度条若跳回
   * 「AI 对话」，用户会以为走错了流程——因此按当前标签对应的产物精确算一次（与原
   * `clarifying` 阶段的口径一致）。
   */
  const currentStepIndex = computed(() => {
    const clarifyKind =
      viewState.value === 'chatting' ? documentKindOfChatStage(activeChatTab.value) : null
    if (clarifyKind) return stepIndexOf(reviewStateFor(clarifyKind), entryMode.value)
    return stepIndexOf(viewState.value, entryMode.value)
  })

  /**
   * 已到达的最远步骤下标（进度条可点击范围），解锁依据是**产物有没有内容**。
   *
   * `hasMessages` 的语义是「**任意**线程聊过」——从前只有需求澄清一条线程时它与
   * 「需求聊过」等价；四条线程合流后依然等价：文档线程必然发生在需求线程之后
   * （要先有对话才谈得上生成文档）。
   */
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

  /**
   * 方案列表要显示的「进度」：由**产物**推出的最远里程碑，而不是「此刻在看的这一页」。
   *
   * 两处刻意的例外：
   * - 还没选入口（`entryMode === null`）时直通 `viewState`，保留「未开始」这类录入期说法；
   * - `done` 直通——「已完成」是比「已出提示词」更靠后的终态，不该被后者覆盖掉。
   *
   * ⚠️ 不要退回成 `viewState`：那会让「点回 PRD 回看」把列表里的进度**倒退**成「已出 PRD」
   * （三份都做完了却显示只做到 PRD），也会让「切一下阶段」就重写一次列表。
   * 另外，生成期间它显示的是**上一个做好的里程碑**——「生成中」不是一个已完成的事实，
   * 侧栏是「这份做到哪了」的地方，不是「现在在忙什么」的地方。
   */
  const progressStage = computed(() => {
    if (entryMode.value === null) return viewState.value
    if (viewState.value === 'done') return 'done'
    const modeSteps = stepsForMode(entryMode.value)
    const index = farthestContentStepIndex({
      mode: entryMode.value,
      hasMessages: messages.value.length > 0,
      hasPrd: documents.hasPrd,
      hasApiDocs: documents.hasApiDocs,
      hasPrompts: documents.hasPrompts,
    })
    // step id 恰好与 `STAGE_LABELS` 的键同形（form / chatting / review-prd / …），无需再映射
    return modeSteps[index]?.id ?? viewState.value
  })

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

  /**
   * 对话阶段是否满足「生成 PRD」条件。
   *
   * ⚠️ **只认需求澄清那一条线程**：`messages` 现在装着四条线程的消息，若拿整数组去数，
   * 文档澄清里随便一条 AI 回复都会把「生成 PRD」点亮——而那时用户可能压根没聊过需求。
   */
  const canGeneratePrd = computed(() =>
    canGeneratePrdFrom(
      messages.value.filter((message) => normalizeChatStage(message.stage) === 'requirement'),
      busy.value,
    ),
  )

  // ------------------------------------------------------------------
  // 动作
  // ------------------------------------------------------------------

  /**
   * 关掉「刷新降级说明」那个浮层（用户点了上面的「×」）。
   *
   * `downgrade` 是运行态：不进快照、也不在 `changeTick` 的 watch 列表里，
   * 因此清它不会触发一次保存，更不会影响恢复逻辑。
   */
  function clearDowngrade(): void {
    downgrade.value = null
  }

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
    chatTab.value = 'requirement'
    conversationIds.value = emptyConversationIds()
    summary.value = null
    quickInput.value = { productName: '', requirements: '' }
    debugPrdText.value = ''
    pastePairInput.value = { prd: '', apiDocs: '' }
    activeJobId.value = null
    activeJobKind.value = null
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
   * 作废一条澄清线程：清掉它的消息与后端会话 id。
   *
   * 触发点是**整篇重新生成**那份产物（见 `useDocumentPipeline.handlersForKind`）。为什么必须
   * 作废：旧线程聊的是**上一版**的待确认项，留着它，用户点「按澄清结果改写」就会把针对旧
   * 文档的结论写进新文档——一次静默的错写，而且看起来还挺合理。
   *
   * 只动被点名的那条线程，其余线程原样保留。
   */
  function dropChatThread(stage: ChatStage): void {
    messages.value = messages.value.filter(
      (message) => normalizeChatStage(message.stage) !== stage,
    )
    conversationIds.value = { ...conversationIds.value, [stage]: '' }
  }

  /**
   * 快照里是否存在「值得保存」的内容。
   *
   * 只看内容字段，**不看 `viewState` / `entryMode` / `chatTab` / `conversationIds`**——
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
      debugPrdText.value.trim() !== '' ||
      pastePairInput.value.prd.trim() !== '' ||
      pastePairInput.value.apiDocs.trim() !== '',
  )

  return {
    // 会话数据
    viewState,
    entryMode,
    summary,
    messages,
    chatTab,
    conversationIds,
    quickInput,
    debugPrdText,
    pastePairInput,
    activeJobId,
    activeJobKind,
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
    progressStage,
    generating,
    reviewing,
    requiredDocsReady,
    canFinish,
    canGeneratePrd,
    availableChatTabs,
    activeChatTab,
    hasContent,
    // 动作
    goTo,
    clearDowngrade,
    chooseEntry,
    backToEntry,
    dropChatThread,
    reset,
    snapshot,
    applySnapshot,
  }
})
