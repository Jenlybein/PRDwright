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
import { useRunsStore } from '@/stores/runs'
import {
  canGeneratePrdFrom,
  documentKindOfChatStage,
  DOWNGRADE_NOTICE,
  farthestContentStepIndex,
  hasRequiredDocuments,
  hasRequirementChat,
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

  /**
   * 在途任务的运行态。
   *
   * 「哪个任务在跑、它写哪一份产物、跑了多久」全在这里，本 store 只**派生**出指针与忙碌判据。
   * 从前这里放的是一个无归属的布尔 `docGenerating`，它同时兼任「渲染归属」「重入守卫」和
   * 「导航闸门」，第三种用法把整个界面锁死了（见 `stores/runs.ts` 的说明）。
   */
  const runs = useRunsStore()

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
   * 全部澄清线程的消息——**服务端轮次的只读镜像**，正在流式生成的那条不在其中。
   *
   * 它不是真源：真源是后端 `chat_turns`（写者只有服务端一个）。本 store 只持有它的
   * 一份展示副本，由 `setTurns()` 整体灌入、由 `dropChatThread()` 局部清掉。
   *
   * 为什么要镜像而不是「每次渲染现拉」：消息要参与「能不能生成 PRD」「进度到哪一格」
   * 这些**同步**判断，而 Vue 的渲染路径不能等一次网络往返。镜像坏了最多显示旧内容，
   * 不会写错东西——因为没有一条写入路径从这里出发。
   *
   * 四条线程共用这一个数组，按 `ChatMessage.stage` 区分归属（见 `ChatStage`）。
   */
  const messages = ref<ChatMessage[]>([])

  /**
   * 对话页当前停留在哪条线程。
   *
   * 它决定渲染哪一段对话、底栏给哪个主操作（需求澄清给「生成 PRD」，其余给「按澄清结果改写」）。
   * 随快照持久化：刷新回来应当仍在原来那条线程里，而不是莫名其妙地又能再生成一次 PRD。
   */
  const chatTab = ref<ChatStage>('requirement')

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
   *
   * ⚠️ 它是 `stores/runs` 里那一支运行态的**派生值**，不是独立字段：两处各存一份就一定会
   * 漂移（典型症状是 `activeJobId` 还指着已收尾的任务，刷新后去重连一个死任务）。
   */
  const activeJobId = computed<number | null>(() => runs.pointer?.jobId ?? null)

  /** 当前进行中任务的文档种类；与 `activeJobId` 成对派生 */
  const activeJobKind = computed<DocKind | null>(() => runs.pointer?.jobKind ?? null)

  /**
   * 当前进行中任务**写的那一份产物**。
   *
   * 与 `activeJobKind` 分开：`optimize` / `clarify` 这类动作凭 kind 定不了产物，
   * 得由任务创建时记下的目标决定（后端 `doc_kind` 的同一口径）。
   */
  const activeJobDocKind = computed(() => runs.pointer?.docKind ?? null)

  /**
   * 对话任务停留的线程；非对话任务为 null。
   *
   * 它是**运行态**的一部分（随指针存、随重连读），不是「用户在翻哪一页」——
   * 后者是 `chatTab`，两者刻意分开：用户可以在 A 线程生成时翻到 B 线程去看历史。
   */
  const activeChatStage = computed<ChatStage | null>(() => runs.pointer?.chatStage ?? null)

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

  /**
   * 本方案是否有生成任务正在跑。
   *
   * 它**只是信息性判据**（能不能再发起、界面要不要表现「忙」），不再兼任导航闸门——
   * 那个用法见 `stores/runs.ts` 顶部的说明。真正的归属判据是 `runs.isRunningFor(kind)`。
   */
  const hasActiveRun = computed(() => runs.hasRunning)

  /** 是否有任何请求在进行中 */
  const busy = computed(() => hasActiveRun.value)

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
   * **内容**变更计数：只统计「作品本身变了」的那些改动。
   *
   * 与 `changeTick` 并存是刻意的——后者把 `viewState` / `chatTab` 这类「我翻到哪一页」的
   * 相位也算作一次改动（切一下进度条就是一次），而列表要回答的是「这份东西最后一次真的
   * 变了是什么时候」。把翻页也记成内容变更，那根时间轴就没有意义了
   * （见 `solutions.content_updated_at`）。
   *
   * ⚠️ 它只覆盖**快照里**的内容字段（需求摘要、对话、粘贴的两份材料），见下方那个 watch。
   * 三份产物的正文**不在快照里**（住在后端 `documents` 表），改写它们一个字段都不会动到本
   * store——那一路由 `stores/documents.ts` 的 `contentWriteTick` 单独记数，两者在
   * `stores/solutions.ts` 里合并成「这次要不要推进内容时间」。
   *
   * 分成两个计数而不是让 `documents` 反过来调用本 store，是为了不让两个 store 互相引用：
   * 本 store 已经依赖 `documents`（要按产物判断进度），反向再依赖就成环。
   */
  const contentTick = ref(0)

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
      chatTab: chatTab.value,
      quickInput: quickInput.value,
      debugPrdText: debugPrdText.value,
      pastePairInput: pastePairInput.value,
      activeJobId: activeJobId.value,
      activeJobKind: activeJobKind.value,
      // 目标产物：`optimize` / `clarify` 凭 kind 定不了它，重连时又要靠它知道改的是哪一份
      activeJobDocKind: activeJobDocKind.value,
      // 对话任务：重连时靠它知道这轮回复该记到哪条线程
      activeChatStage: activeChatStage.value,
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
  function applySnapshot(
    session: SessionDataV2,
    downgradeKind: DowngradeKind | null = null,
    solutionId: number | null = null,
  ): void {
    viewState.value = session.viewState
    entryMode.value = session.entryMode
    summary.value = session.summary
    // 消息不随状态一起来：它住在后端 `chat_turns`，由调用方随后用 `setTurns()` 灌入。
    // 这里先清空，避免上一份方案的对话在新方案里短暂出现（视觉上是「串稿」）。
    messages.value = []
    quickInput.value = session.quickInput
    debugPrdText.value = session.debugPrdText
    pastePairInput.value = session.pastePairInput
    // 任务指针属于 `stores/runs`，这里只把「快照说有一支在跑」翻译成一支运行态。
    // 没有指针就**显式清空**：否则上一份方案的运行态会留在内存里，让新方案无端变「忙」。
    if (session.activeJobId !== null && session.activeJobId !== undefined) {
      runs.restore({
        jobId: session.activeJobId,
        solutionId,
        // 旧快照可能只有 id 没有 kind（那时这个字段还不存在）；重连链本身就按 `'prd'` 兜底，
        // 这里保持同一口径，免得「有指针却不重连」这种更糟的结果。
        jobKind: session.activeJobKind ?? 'prd',
        docKind: session.activeJobDocKind ?? null,
        chatStage: session.activeChatStage ?? null,
      })
    } else {
      runs.clear()
    }
    // 停留的线程要恢复（它在状态里，属于「用户翻到哪一页」）；
    // 但**消息与会话 id 都不恢复**：它们归服务端，随后按方案 id 拉取。
    chatTab.value = session.chatTab ?? 'requirement'
    downgrade.value = downgradeKind
    // 通知视图「这是整份换上的新内容」，而不是用户的一次输入。
    // 结构化表单只在 setup 阶段读一次 `initial`，因此需要据此重建，
    // 否则异步恢复回来的摘要会填不进表单（打开旧方案时肉眼可见）。
    restoreTick.value += 1
  }

  /**
   * 状态变化时自增 `changeTick`。
   *
   * 注意这里 watch 的是「快照里包含的字段」，而**不是**运行态那几个
   * （`hasActiveRun` / `downgrade`）——它们不该触发保存。
   * 流式过程中的临时文本留在组件里，因此也不会形成高频保存。
   */
  watch(
    [
      viewState,
      entryMode,
      summary,
      chatTab,
      quickInput,
      debugPrdText,
      pastePairInput,
      activeJobId,
      activeJobKind,
      activeJobDocKind,
      activeChatStage,
    ],
    () => {
      changeTick.value += 1
    },
    { deep: true },
  )

  /**
   * `changeTick` 的**内容子集**：只盯真正构成「作品」的那几个字段。
   *
   * 上面那个 watch 里的另外几个——`viewState`（翻页）、`entryMode`（选入口）、
   * `chatTab`（切对话标签）、`activeJobId` / `activeJobKind` / `activeJobDocKind`
   * （在途任务指针）——都会变，但作品一个字都没变，它们不该推进内容时间。
   *
   * 两个 watch 并存而不是「在一个 watch 里判断是谁变了」：后者要维护一份「哪些字段算内容」
   * 的判断，而列表本身就写在这里（Vue 直接按依赖区分），漏改一个字段的代价是对不上账。
   */
  watch(
    [summary, quickInput, debugPrdText, pastePairInput],
    () => {
      contentTick.value += 1
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
   * - 需求澄清存在于**所有从零创建**的入口（完整流程与快捷流程）——两者的起点都是
   *   「把需求交给 AI 追问」，只是录入形态不同；两个导入入口自带需求，压根没有那条对话；
   * - 三条文档线程看对应产物有没有正文（没正文就没什么可澄清的）。
   *
   * 不可用的线程**照样渲染**（置灰、不可点）：标签常驻，用户才知道后面还有哪些可以聊。
   */
  const availableChatTabs = computed<ChatStage[]>(() =>
    CHAT_STAGES.filter((stage) => {
      const kind = documentKindOfChatStage(stage)
      if (!kind) return hasRequirementChat(entryMode.value)
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
   * 是否已经「聊过」——进度条第二格的解锁依据。
   *
   * 判据是「有轮次」**或**「已提交需求摘要」：消息现在从服务端异步取回，
   * 刚提交完表单、第一轮回复还在路上时 `messages` 仍是空的，而用户显然已经过了
   * 第一格。只看消息会让进度条在正确的位置上退回去一格。
   */
  const hasChat = computed(() => messages.value.length > 0 || summary.value !== null)

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
      hasMessages: hasChat.value,
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
      hasMessages: hasChat.value,
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
   *
   * 第二个入参是「PRD 是否正在生成」：按**产物**判，不按「有没有任务在跑」判——
   * 正在生成接口文档时，PRD 这一份并不忙，没必要把「生成 PRD」也灰掉。
   */
  const canGeneratePrd = computed(() =>
    canGeneratePrdFrom(
      messages.value.filter((message) => normalizeChatStage(message.stage) === 'requirement'),
      runs.isRunningFor('prd'),
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
   * 统一的状态导航入口，收口所有阶段赋值。
   *
   * ## 为什么这里**不再**拦「有请求在跑」
   *
   * 从前第一行是 `if (busy) return false`，于是所有走 `goTo` 的跳转（进度条、底栏「返回」、
   * 入口卡）在生成期间全是**死按钮**——点了没反应，也不报错。那是把「有一个任务在跑」
   * 误当成「整个应用不能动」：用户想看别的一页、想回到对话补充一句，都不该被禁止。
   *
   * 真正需要护栏的是三件事，它们各自有正确的落点，都不该由导航入口兼任：
   * - 「不能再发一次」→ 各生成链入口的重入守卫（按产物判）；
   * - 「不能换入口模式」（换了会改掉进度条口径与完成条件）→ `chooseEntry` 里拦；
   * - 「别把结果写进别人的界面」→ `finishDoc` 的收尾守卫 + 运行态身份比对。
   *
   * 跳转本身是安全的：阶段只是一个相位字段，任务事件按 `jobId` 认领，写在预览与文档上，
   * 与当前停留在哪一页无关。
   */
  function goTo(target: ViewState): void {
    viewState.value = target
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  /**
   * 选择入口：记下模式并直接进入该模式的第一个界面。
   *
   * 模式只影响「进度条展示哪几步、需要哪几份文档才算完成」以及流水线从哪个输入取需求，
   * 生成链路本身完全共用，因此这里不需要清理任何已生成内容。
   *
   * **生成中拦下**（这是本版唯一保留的「忙则不许」）：换模式会改写进度条口径与完成条件，
   * 而正在跑的那份产物是按旧模式发起的——放过去会让同一份会话里两套口径并存。
   * 返回 `false` 由调用方给红色提示（见 `AppV2.handleSelectEntry`），不再静默吞掉点按。
   */
  function chooseEntry(mode: EntryMode): boolean {
    if (hasActiveRun.value) return false
    entryMode.value = mode
    goTo(MODE_LANDING[mode])
    return true
  }

  /** 回到入口选择页（保留已选模式，用户点「上一步」时不至于丢掉产物） */
  function backToEntry(): void {
    goTo('entry')
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
    summary.value = null
    quickInput.value = { productName: '', requirements: '' }
    debugPrdText.value = ''
    pastePairInput.value = { prd: '', apiDocs: '' }
    downgrade.value = null
    entryMode.value = null
    viewState.value = 'entry'
    // 运行态与预览一起清：这是「整份会话换掉」，上一支任务哪怕还在后端跑，
    // 它的指针与半截正文也都不属于这里了
    runs.clear()
    documents.clear()
    // 与 `applySnapshot` 同理：这也是一次「整份换掉」，
    // 视图（尤其是只在 setup 读一次 `initial` 的表单）要据此重建，
    // 否则「重新开始」后表单里还留着上一轮的内容。
    restoreTick.value += 1
  }

  /**
   * 用服务端的轮次整体替换本地镜像。
   *
   * 调用方是 `stores/solutions.ts`（打开方案、任务收尾后各拉一次）。**只接受整体**
   * 而不是增删单条：服务端是唯一写者，前端任何「局部改一下」都会在没有对应写事务的
   * 情况下与它漂移，而漂移的表现是「刷新后某条消息回来了」这种最难解释的现象。
   */
  function setTurns(turns: ChatMessage[]): void {
    messages.value = turns
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
    // 只清本地镜像；服务端那一份由调用方（`stores/solutions.clearChatThread`）删，
    // 否则刷新一次旧对话又回来了——「删了但没删掉」比不删更难解释。
    messages.value = messages.value.filter(
      (message) => normalizeChatStage(message.stage) !== stage,
    )
  }

  /**
   * 快照里是否存在「值得保存」的内容。
   *
   * 只看内容字段（摘要 / 两段粘贴稿），**不看 `viewState` / `entryMode` / `chatTab`**——
   * 它们描述的是「走到哪一步」而不是「有什么」；用户只是点进入口看一眼就又退回，
   * 不该因此留下一份空方案。
   *
   * 也**不看产物正文**：正文属于后端 `documents` 表，而「要不要建一份方案」这件事
   * 发生在正文产生之前——第一份正文的生成本身就需要方案已经存在（任务要挂 `solution_id`）。
   */
  const hasContent = computed(
    () =>
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
    quickInput,
    debugPrdText,
    pastePairInput,
    activeJobId,
    activeJobKind,
    activeJobDocKind,
    activeChatStage,
    // 运行态
    hasActiveRun,
    busy,
    downgrade,
    changeTick,
    contentTick,
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
    setTurns,
    dropChatThread,
    reset,
    snapshot,
    applySnapshot,
  }
})
