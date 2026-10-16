<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useRoute, useRouter } from 'vue-router'

import AuthDialog from '@/components/AuthDialog.vue'
import AppBadge from '@/components/ui/AppBadge.vue'
import RequirementsForm from '@/components/RequirementsForm.vue'
import SolutionSidebar from '@/components/SolutionSidebar.vue'
import SummaryDiffPanel from '@/components/SummaryDiffPanel.vue'
import ChatStage from '@/components/stages/ChatStage.vue'
import DoneStage from '@/components/stages/DoneStage.vue'
import EntryStage from '@/components/stages/EntryStage.vue'
import GeneratingStage from '@/components/stages/GeneratingStage.vue'
import ProgressSteps from '@/components/stages/ProgressSteps.vue'
import PromptDebugStage from '@/components/stages/PromptDebugStage.vue'
import QuickPrdStage from '@/components/stages/QuickPrdStage.vue'
import ReviewStage from '@/components/stages/ReviewStage.vue'
import { useClarificationChat } from '@/composables/useClarificationChat'
import { useDocumentPipeline } from '@/composables/useDocumentPipeline'
import { fetchActiveJobs, onUnauthorized } from '@/services/api'
import { useAuthStore } from '@/stores/auth'
import { useDocumentsStore } from '@/stores/documents'
import { useSolutionsStore } from '@/stores/solutions'
import { useWorkflowStore } from '@/stores/workflow'
import {
  chatStageForDocument,
  docKindOfState,
  documentKindOfChatStage,
  generatingStateFor,
  normalizeChatStage,
} from '@/utils/workflow'
import {
  CHAT_STAGES,
  type DocumentKind,
  type EntryMode,
  type RequirementsSummary,
  type StepId,
} from '@/types'

/**
 * 完整应用（V2）。
 *
 * 先选入口，再走流程。三条入口（完整流程 / PRD 快捷 / 提示词调试）只是**起点**不同：
 *
 * 1. **完整流程**：结构化表单逐项录入 → AI 对话澄清 → PRD → 接口文档 → 提示词；
 * 2. **PRD 快捷**：一段话描述 → 直接逐章生成 PRD（跳过表单、对话与摘要确认闸门）；
 * 3. **提示词调试**：贴入现成 PRD → 生成提示词套件 → 用「AI 优化」反复迭代。
 *
 * 三条入口在「生成 → 审阅 → 下载」这段完全共用，模式的差异（进度条显示哪几步、
 * 完成需要哪几份文档、返回按钮回到哪里）由 `utils/workflow.ts` 与 store 推导。
 *
 * 完整流程有一道**确认闸门**：生成 PRD 前先确认「对话补充信息回填到摘要」的 Diff。
 * 这道闸门只属于完整流程。
 *
 * **未登录不允许创建任务**：选择任意入口前都会先检查登录态，拦下时弹登录窗并停在入口页；
 * 登录成功后只给一句提示，不自动进入——用户可能已经忘了刚才想选哪个入口。
 * 接口（含 SSE）返回 401 时同样弹登录窗，注册回调见 `services/api.ts` 的 `onUnauthorized`。
 *
 * **持久化**：会话状态的权威来源是后端 `solutions` 表。地址是 `/v2`（新建）或 `/v2/{id}`
 * （打开某份方案）；首次产生内容时方案才被创建，随后地址 `replace` 成带 id 的形式。
 * 具体策略（防抖、创建还是更新）在 `stores/solutions.ts`，本文件只做接线：
 * 挂载时按路由参数加载、创建成功后换地址、离开前把待保存的变更冲掉。
 *
 * 本文件做五件事：路由（阶段 → 视图）、跨流程的提示语、登录态的接线、持久化接线，
 * 以及把两个流程（对话 / 文档）拼装起来。会话数据与阶段在 `stores/workflow.ts`，
 * 方案持久化在 `stores/solutions.ts`，登录态在 `stores/auth.ts`，
 * 对话编排在 `composables/useClarificationChat.ts`，文档编排在 `composables/useDocumentPipeline.ts`，
 * 各阶段视图在 `components/stages/`。
 *
 * 对话那摊有**四条线程**（需求 / PRD / 接口 / 提示词），共用同一个对话页与同一个消息
 * 数组，靠消息上的 `stage` 分线、顶部的子标签栏切换；本文件按标签在四个
 * `useClarificationChat` 实例之间转发（见 `chats` 与 `activeChat`）。
 */

// ---------------------------------------------------------------------------
// 路由与方案参数
// ---------------------------------------------------------------------------
const route = useRoute()
const router = useRouter()

/** 路由参数里的方案 id；`/v2`（新建）时为 null */
const routeSolutionId = computed<number | null>(() => {
  const raw = route.params.id
  const value = Array.isArray(raw) ? raw[0] : raw
  if (!value) return null
  const parsed = Number.parseInt(value, 10)
  return Number.isFinite(parsed) ? parsed : null
})

const store = useWorkflowStore()
const {
  viewState,
  summary,
  // 聊天记录——四条澄清线程共用这一个数组，靠每条消息上的 `stage` 分线程
  messages,
  // 对话页当前停在哪条线程（子标签栏选中项）；这是**意图**，刷新后照原样恢复
  chatTab,
  // 子标签栏里可点的那些线程；其余照样渲染但置灰
  availableChatTabs,
  // 实际显示的那条：`chatTab` 暂时不可用时回退到第一个可用的（见 store 的说明）
  activeChatTab,
  quickInput,
  debugPrdText,
  steps,
  currentStepIndex,
  reachedStepIndex,
  canGeneratePrd,
  downgradeNotice,
} = storeToRefs(store)

/**
 * 三份产物正文的持有者。
 *
 * 正文不在 workflow store 里：所有权属于后端 `documents` 表，这里只是一份工作副本
 * （见 `stores/documents.ts`）。本文件需要它的地方只有三处——审阅页「查看」入口
 * 是否可用（`has*`）、完成页的下载列表（`contents`），以及「按澄清结果改写」是否有
 * 可改写的内容。
 */
const documents = useDocumentsStore()
const { contents: documentContents, hasPrd, hasApiDocs, hasPrompts } = storeToRefs(documents)

/** 三份产物各自的展示名（回执与提示语共用一套说法，不各写一份） */
const DOC_TITLES: Record<DocumentKind, string> = {
  prd: 'PRD',
  'api-docs': '接口文档',
  prompts: '提示词套件',
}

/**
 * 表单组件的 key。
 * 「重新开始」时自增，强制重建表单组件，确保里面的编辑器回到空状态。
 */
const formKey = ref(0)

/**
 * 打开一份已存方案时也重建表单。
 *
 * 快照是**异步**到达的（要先请求后端），而表单只在 setup 阶段读一次 `:initial`；
 * 若只靠 prop 传递，恢复出来的摘要会填不进表单——界面上表现为「PRD 回来了，
 * 但需求表单是空的」，用户再点提交就会用一份空需求覆盖掉已有内容。
 */
watch(
  () => store.restoreTick,
  () => {
    formKey.value += 1
  },
)

/** 短暂展示的提示语（草稿保存 / 提交 / 闸门确认等） */
const savedTip = ref('')

/**
 * 正在「打开并恢复」的方案 id 集合（单飞守卫，见 `loadAndResume` 的说明）。
 *
 * 必须声明在**注册 watch 之前**：登录态 watch 是 `immediate` 的，会在 setup 阶段
 * 就调用 `loadAndResume`，晚于它声明就会撞上暂时性死区。
 */
const resumingSolutionIds = new Set<number>()

/** 提示语的定时器 */
let savedTipTimer: number | undefined

/** 短暂展示提示，随后自动淡出 */
function flashSavedTip(text: string): void {
  savedTip.value = text
  window.clearTimeout(savedTipTimer)
  savedTipTimer = window.setTimeout(() => {
    savedTip.value = ''
  }, 2500)
}

// ---------------------------------------------------------------------------
// 登录态
// ---------------------------------------------------------------------------

const auth = useAuthStore()

/**
 * 任何接口返回 401（包括 SSE 流式接口）都弹登录窗并说明原因。
 *
 * 在应用根部注册一次，而不是每个调用点各自判断：漏掉一处，用户看到的就是
 * 一句莫名其妙的「请求失败」，而不是「请先登录」。
 */
onUnauthorized((message) => auth.requireLogin(message))

// 会话是 HttpOnly Cookie，前端读不到，登录态只能问后端
void auth.load()

/**
 * 登录成功只给一句提示，**不自动进入之前选的入口**。
 * 用户可能在填账号密码的过程中已经忘了自己要选哪个，交还选择权更省事。
 */
watch(
  () => auth.isLoggedIn,
  (now, before) => {
    if (now && !before) flashSavedTip('已登录，请选择入口开始')
  },
)

// ---------------------------------------------------------------------------
// 方案持久化接线
// ---------------------------------------------------------------------------

const solutions = useSolutionsStore()

/** 尚未登录时不该去拉方案列表——那只会得到 401 与一次无意义的弹窗 */
watch(
  () => auth.isLoggedIn,
  (now) => {
    if (!now) return
    void solutions.loadList()
    if (routeSolutionId.value !== null) {
      void loadAndResume(routeSolutionId.value)
    }
  },
  { immediate: true },
)

/**
 * 方案被创建后，把地址从 `/v2` 换成 `/v2/{id}`。
 *
 * 用 `replace` 而不是 `push`：用户如果按返回键，期望是「回到上一份方案」或「离开」，
 * 而不是回到「那份刚刚还不存在的新建页」——那一步在历史里没有意义。
 */
solutions.setCreatedHandler((id) => {
  if (routeSolutionId.value !== id) void router.replace(`/v2/${id}`)
})

// 打开方案后若地址与当前 id 不一致（例如方案已被删除而地址还留着旧 id），把地址归位
solutions.setOpenedHandler((id) => {
  if (id === null && routeSolutionId.value !== null) void router.replace('/v2')
})

/**
 * 恢复快照后自动「静默重连」进行中的生成任务。
 *
 * 三条路径：
 * 1. 快照里记着 `activeJobId`：直接重连该任务（后端先发 `snapshot` 补全正文，再接 live）；
 * 2. 无指针但快照原本落在 `generating-*`（已被降级为 `review-*`）：兜底查一次活跃任务，
 *    命中则把指针写回 store、撤销降级并重连；
 * 3. 都未命中：什么都不做，保持既有「上次生成未完成」的降级提示。
 *
 * 全程无需用户点击——这正是「生成与连接解耦」带来的体验。
 */
async function resumeAfterRestore(): Promise<void> {
  if (store.activeJobId !== null) {
    await reconnectJob(store.activeJobId)
    return
  }

  if (store.downgrade !== 'generating') return
  const kind = docKindOfState(store.viewState)
  if (!kind) return

  try {
    const jobs = await fetchActiveJobs({
      solutionId: solutions.currentId,
      kind,
      status: 'running',
    })
    const match = jobs[0]
    if (!match) return
    store.activeJobId = match.id
    store.activeJobKind = kind
    // 命中活跃任务：撤销降级、切回生成态，再重连
    store.downgrade = null
    const target = generatingStateFor(kind)
    if (target) store.viewState = target
    await reconnectJob(match.id)
  } catch {
    // 兜底查询失败不改变降级结果：保持既有提示即可
  }
}

/**
 * 打开一份方案并在成功后尝试重连进行中的任务。
 *
 * 一次硬刷新会**从两个入口同时**触发「打开并恢复同一份方案」：
 * 1. 路由从初始空路由解析到 `/v2/{id}`（`routeSolutionId` 由 `null` 变为 id）；
 * 2. 登录态探测完成（`isLoggedIn` 由 `false` 变为 `true`）。
 *
 * 两个入口都是异步的，各自 `await` 一串网络请求。不折叠的话就会并发跑两遍
 * `openSolution`，进而跑两遍静默重连——**同一个任务被订阅两次**，两路 SSE 事件
 * 交错写进同一份预览缓冲（表现为整段章节重复、表格单元格成对出现：
 * `| 编号 | 编号 | 功能模块 | 功能模块 |`）。而最终落稿取的是 `done.reply`，
 * 所以「预览全乱、存下来的正文却是好的」正是这个路径的特征。
 *
 * 单测里 `await router.push()` 先于挂载，路由在挂载时已经就位、不会再变化，
 * 因此这条路径在单测中走不到；生产环境 `main.ts` 未等待 `router.isReady()` 就挂载，
 * 才会命中——这也是它一直没被测出来的原因。
 *
 * 守卫按方案 id 单飞，两道叠加：
 * - 正在打开/恢复该方案时直接返回（覆盖并发窗口——重连会 `await` 到任务流结束）；
 * - 该方案已经是「当前打开的」时直接返回（覆盖两次触发**先后**发生的情形，
 *   后一次的前一次已经落地）。
 *
 * 这与路由 watcher 里的 `id === solutions.currentId` 是同一口径，只是登录态那条
 * 触发路径没有这个短路，需要在入口处补齐。
 */
async function loadAndResume(id: number): Promise<void> {
  if (resumingSolutionIds.has(id)) return
  if (solutions.currentId === id) return
  resumingSolutionIds.add(id)
  try {
    const ok = await solutions.openSolution(id)
    if (ok) await resumeAfterRestore()
  } finally {
    resumingSolutionIds.delete(id)
  }
}

/**
 * 加载完后若地址带了 id 但方案打不开（被删 / 越权），退回新建页并说明原因，
 * 否则用户会停在一片空白里，不知道发生了什么。
 */
watch(
  () => solutions.saveError,
  (message) => {
    if (message && routeSolutionId.value !== null && solutions.currentId === null) {
      flashSavedTip(message)
    }
  },
)

/**
 * 地址里的 id 变化时切换方案（含浏览器前进后退）。
 *
 * 注意判断 `currentId`：创建后我们自己也 `replace` 了一次地址，那一次不需要重新加载——
 * 内容就在内存里，重新拉一遍纯属浪费，还会把刚填的东西冲掉。
 */
watch(routeSolutionId, (id) => {
  if (id === null) {
    // 回到新建页：若当前有打开的方案（浏览器后退），开一份干净的
    if (solutions.currentId !== null) void solutions.startNew()
    return
  }
  if (id === solutions.currentId) return
  void loadAndResume(id)
})

/**
 * 离开页面前把防抖窗口里压着的那次保存冲掉。
 *
 * 只在 `beforeunload` 里做：它无法等待 async 完成，能起作用是「请求已经发出」这件事本身。
 * 真正可靠的落库时机是「进入下一阶段」与「路由切换」，`beforeunload` 只是兜底。
 */
function handleBeforeUnload(): void {
  void solutions.flushPendingSave()
}

onMounted(() => {
  window.addEventListener('beforeunload', handleBeforeUnload)
})

onBeforeUnmount(() => {
  window.removeEventListener('beforeunload', handleBeforeUnload)
  void solutions.flushPendingSave()
})

// ---------------------------------------------------------------------------
// 文档流水线：生成 / 审阅 / 两道确认闸门 / 下载
// ---------------------------------------------------------------------------

const {
  isGenerating,
  isGeneratingPhase,
  isReviewing,
  canFinishTask,
  activeDocTitle,
  activeDocContent,
  backLabel,
  formBackLabel,
  docStreamingContent,
  docError,
  prdStatus,
  prdShowingDraft,
  runSummary,
  pendingItems,
  elapsedMs,
  summaryChanges,
  summarySyncing,
  summarySyncError,
  goTo,
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
  handleGeneratePrd,
  confirmSummaryDiff,
  rejectSummaryDiff,
  reconnectJob,
  // 文档澄清：切到对话页的某条线程 / 按澄清结果改写那一份
  startDocumentClarification,
  applyClarification,
  reset: resetPipeline,
} = useDocumentPipeline({ onNotice: flashSavedTip })

// ---------------------------------------------------------------------------
// 澄清对话：四条线程，各一个实例
//
// 四条线程（需求 / PRD / 接口 / 提示词）**跑的是同一套骨架**，差异只有两点：
// 消息归哪条线程（`stage`）、后端用哪套人设（`profileKey`）。因此这里是四个
// `useClarificationChat` 实例而不是四份实现——「断线重试」「半截内容保留」这些
// 容易写歪的细节只维护一处。
//
// 每个实例持有**自己的**流式缓冲与错误提示，所以 A 线程出错不会冒到 B 线程上；
// 消息本身仍共用 store 里那一个 `messages` 数组，靠 `stage` 分线程。
// ---------------------------------------------------------------------------

/** 四条线程共用的外部能力：一句提示、切到对话页 */
const chatHooks = {
  onNotice: flashSavedTip,
  onEnterChat: () => goTo('chatting'),
}

const chats = {
  // 需求澄清：后端默认档案（不点名任何 `profileKey`）
  requirement: useClarificationChat(chatHooks, { stage: 'requirement' }),
  // 三条文档澄清共用后端 `doc-clarification` 档案。
  // `clearHistory: false`——它们是「在同一条线程上继续」，清掉历史正好是用户最反感的体验。
  'prd-clarify': useClarificationChat(chatHooks, {
    profileKey: 'doc-clarification',
    stage: 'prd-clarify',
    clearHistory: false,
  }),
  'api-docs-clarify': useClarificationChat(chatHooks, {
    profileKey: 'doc-clarification',
    stage: 'api-docs-clarify',
    clearHistory: false,
  }),
  'prompts-clarify': useClarificationChat(chatHooks, {
    profileKey: 'doc-clarification',
    stage: 'prompts-clarify',
    clearHistory: false,
  }),
}

/**
 * 对话栏当前该听哪一套。
 *
 * 按**当前标签**选实例：同一个组件承担四条线程，切换标签就是换一套发送 / 渲染 / 加载态。
 *
 * ⚠️ 这里有意 `.value` 取成了**普通值**：对象内部的 ref 不会在模板里被自动解包，
 * 直接把 Ref 塞进 props 就会把包装对象当消息数组传下去。外层仍是 computed，
 * 底层 ref 变化时照样重新求值。
 */
const activeChat = computed(() => {
  const chat = chats[store.activeChatTab]
  return {
    streaming: chat.streaming.value,
    chatError: chat.chatError.value,
    displayMessages: chat.displayMessages.value,
    sendMessage: chat.sendMessage,
  }
})

/**
 * 审阅页「回到对话澄清剩余问题」的入口是否出现。
 *
 * 两个条件缺一不可：这份文档真有正文（没正文就无处可改写），且真有待确认项
 * （清单为空时点进去只能闲聊，那不如不给入口）。
 *
 * 三种审阅页**一视同仁**——PRD 从前走的是另一条路（复用主对话栏），现在三条线程
 * 落在同一个对话页上，靠子标签栏切换（ADR-028）。
 */
const canReturnToChat = computed(
  () => isReviewing.value && pendingItems.value.length > 0 && activeDocContent.value.trim() !== '',
)

/**
 * 澄清入口出现时是否**保留**「返回」按钮。
 *
 * 澄清入口与「返回」落点相同时（完整流程：两者都回对话页）由它顶替「返回」；
 * 落点不同时（PRD 快捷 / 提示词调试：「返回」回的是录入页，那一页不在进度条上、
 * 没有别的入口）必须保留，否则用户再也回不去。
 */
const keepBackButton = computed(() => store.entryMode !== 'structured')

/**
 * 当前标签这条线程是否聊到了可以改写的程度。
 *
 * 判据是**这一轮有没有聊过**：第一轮追问出来之前没有结论可用，按钮点不动。
 * 与流水线里 `applyClarification` 的前置校验同口径（它还会再兜一次）。
 */
const canRewrite = computed(() => {
  const target = documentKindOfChatStage(activeChatTab.value)
  if (!target) return false
  if (!documentContents.value[target].trim()) return false
  return messages.value.some(
    (message) => normalizeChatStage(message.stage) === activeChatTab.value,
  )
})

/**
 * 审阅页「回到对话」：摆好现场（切标签 + 切阶段）后，必要时让 AI 开第一轮追问。
 *
 * 首轮材料（文档正文 + 待确认清单）由流水线从 `documents` 工作副本取——那是后端随正文
 * 落库的权威版本，不是前端另拼一份。已经聊过就不再开场，用户看到的是上一次那段对话继续。
 */
async function handleReturnToChat(): Promise<void> {
  const kind = docKindOfState(viewState.value)
  if (!kind) return
  const material = startDocumentClarification(kind)
  if (!material) return
  await chats[chatStageForDocument(kind)].startConversationWithAnswers(
    { document: material.document, pending_items: material.pendingItems },
    `正在梳理这份${DOC_TITLES[kind]}的待确认项`,
  )
}

/** 是否处于入口选择页：这一步还没有进度可言，进度条不显示 */
const isChoosingEntry = computed(() => viewState.value === 'entry')

// ---------------------------------------------------------------------------
// 表单与全局操作
// ---------------------------------------------------------------------------

/** 表单内容变化：实时更新摘要，store 的持久化 watch 会把它写进本地快照作为草稿 */
function handleSummaryChange(value: RequirementsSummary): void {
  summary.value = value
}

/**
 * 结构化表单提交：把摘要交给**需求澄清**线程，让 AI 开第一轮追问。
 *
 * 三条文档线程不从这里进——它们的入口是各自审阅页的「回到对话」。
 */
async function handleStartConversation(value: RequirementsSummary): Promise<void> {
  await chats.requirement.startConversation(value)
}

/**
 * 选择入口：未登录时拦下，登录后才真正进入。
 *
 * 创建任务会调用模型，属于必须登录的操作，因此这里**停在入口页**而不是先放行再报错。
 *
 * 刻意不把选中的入口暂存下来等登录完自动进入：填完账号密码多半已经忘了刚才想选哪个，
 * 登录完让他重新选一次反而更清楚。
 */
function handleSelectEntry(mode: EntryMode): void {
  if (!auth.isLoggedIn) {
    auth.requireLogin('创建任务需要先登录，登录后回到这里重新选择入口')
    return
  }
  store.chooseEntry(mode)
}

/**
 * 「重新开始」：开一份新方案。
 *
 * 走 `solutions.startNew()` 而不是 `store.reset()`——它会先把待保存的变更冲掉，
 * 再把 id 与地址归零；**不会删除**刚写完的那份，它仍在侧边栏里等着被找回来。
 */
function handleRestart(): void {
  // `startNew()` 自己会先 flush，这里不必再调一次
  void solutions.startNew()
  void router.replace('/v2')

  // 两条流程各自的临时状态分别归零。
  // 对话那摊要**四条线程逐个复位**：每个实例各持一份流式缓冲与错误提示，
  // 只重置当前那条，切到别的标签就会看到上一次残留的半截内容或旧报错。
  resetPipeline()
  for (const chat of Object.values(chats)) chat.reset()

  // 表单不必在这里手动重建：`workflow.reset()` 会自增 `restoreTick`，
  // 上面那个 watch 会据此把 `formKey` 顶掉，保证编辑器回到空状态。
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

/**
 * 点击进度条中已到达的步骤，跳回对应阶段查看之前的内容。
 * 五个步骤 id 与同名的阶段一一对应（`StepId` 是 `ViewState` 的子集），因此无需映射表。
 */
function handleStepClick(stepId: StepId): void {
  goTo(stepId)
}
</script>

<template>
  <div class="flex min-h-screen bg-gradient-to-b from-accent-soft via-bg to-bg text-body">
    <!-- 左侧：方案侧边栏（窄屏为抽屉；lg 以上常驻，含品牌 / 主题 / 列表 / 登录） -->
    <SolutionSidebar />

    <div class="flex min-w-0 flex-1 flex-col">
    <!--
      `w-full` 不能省：父级是 `flex-col`，而 `mx-auto` 的横向 auto 外边距会让 `stretch`
      失效，`main` 于是退化成「收缩到内容宽度」。结果各页宽度不一（需求表单只有 ~608px，
      对话/审阅却是满额 768px），同一套表单在两种宽度间跳。显式 `w-full` + `max-w-3xl`
      把宽度钉死为 `min(容器宽, 768px)`，居中仍由 `mx-auto` 负责。

      `pb-[calc(var(--shell-action-bar-h)+2.5rem)]` 对应吸底的 `AppActionBar`：它用 `fixed`
      脱离文档流，不再自己占位，内容末端必须靠这里留白。底栏现在最多两行（内容工具 + 流程操作），
      高度不固定，因此不能写死 padding——`AppActionBar` 会把实时高度写进
      `--shell-action-bar-h`，这里跟着算，长文档的最后一行才不会被压住。
    -->
    <main class="mx-auto w-full max-w-3xl px-4 pt-10 pb-[calc(var(--shell-action-bar-h)+2.5rem)]">
      <!-- 保存状态 / 提示：原顶栏的信息下沉到内容区右上角，无状态时不占空间 -->
      <div
        v-if="solutions.saving || solutions.saveError || savedTip"
        class="mb-4 flex items-center justify-end gap-3 text-xs"
      >
        <span v-if="solutions.saving" class="text-subtle">保存中…</span>
        <span v-else-if="solutions.saveError" class="text-danger">{{ solutions.saveError }}</span>
        <Transition
          enter-active-class="transition duration-200"
          enter-from-class="opacity-0 translate-y-1"
          leave-active-class="transition duration-500"
          leave-to-class="opacity-0"
        >
          <AppBadge v-if="savedTip" variant="success">
            <svg class="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
              <path
                fill-rule="evenodd"
                d="M10 18a8 8 0 1 0 0-16 8 8 0 0 0 0 16Zm3.857-9.809a.75.75 0 0 0-1.214-.882l-3.483 4.79-1.88-1.88a.75.75 0 1 1-1.06 1.061l2.5 2.5a.75.75 0 0 0 1.137-.089l4-5.5Z"
                clip-rule="evenodd"
              />
            </svg>
            {{ savedTip }}
          </AppBadge>
        </Transition>
      </div>
      <!-- 步骤进度条：展示哪几步由入口模式决定，因此整条由 store 推导后传入 -->
      <ProgressSteps
        v-if="!isChoosingEntry"
        :steps="steps"
        :current-index="currentStepIndex"
        :reached-index="reachedStepIndex"
        :generating="isGeneratingPhase"
        @select="handleStepClick"
      />

      <!-- 刷新时落在无法恢复的状态：说明为什么不是原来的阶段 -->
      <p
        v-if="downgradeNotice"
        class="mb-4 rounded-xl border border-line bg-surface px-4 py-2 text-xs text-subtle"
      >
        {{ downgradeNotice }}
      </p>

      <!-- ============ 第零步：选择入口（三条链路的起点） ============ -->
      <EntryStage v-if="isChoosingEntry" @select="handleSelectEntry" />

      <!-- ============ 确认闸门 1：摘要回填结果 Diff（仅完整流程，生成 PRD 之前） ============ -->
      <SummaryDiffPanel
        v-else-if="viewState === 'summary-diff'"
        :changes="summaryChanges"
        :loading="summarySyncing"
        :error="summarySyncError"
        @confirm="confirmSummaryDiff"
        @reject="rejectSummaryDiff"
      />

      <!-- ============ 入口 B：PRD 快捷（一段话 → 直接生成 PRD） ============ -->
      <QuickPrdStage
        v-else-if="viewState === 'prd-quick'"
        v-model:product-name="quickInput.productName"
        v-model:requirements="quickInput.requirements"
        @generate="handleGenerateQuickPrd"
        @back="goTo('entry')"
      />

      <!-- ============ 入口 C：提示词调试（贴入 PRD → 生成提示词套件） ============ -->
      <PromptDebugStage
        v-else-if="viewState === 'prompt-debug'"
        v-model:prd-text="debugPrdText"
        @generate="handleGeneratePromptsFromDebugPrd"
        @back="goTo('entry')"
      />

      <!--
        ================= 四条澄清线程共用同一块对话栏 =================

        需求澄清（生成 PRD 之前）与三条文档澄清（PRD / 接口 / 提示词生成之后）落在同一个页面上，
        顶部子标签栏切换。因此这里把「当前标签那条线程」的消息、加载态与发送动作一起传下去
        （见 `activeChat`），组件本身不判断自己是谁。

        底栏动作随标签而变：需求澄清给「生成 PRD」，三条文档线程给「按澄清结果改写」。
      -->
      <ChatStage
        v-else-if="viewState === 'chatting'"
        :messages="activeChat.displayMessages"
        :tabs="CHAT_STAGES"
        :active-tab="activeChatTab"
        :available-tabs="availableChatTabs"
        :loading="activeChat.streaming"
        :error="activeChat.chatError"
        :can-generate-prd="canGeneratePrd"
        :can-rewrite="canRewrite"
        :rewriting="isGenerating"
        :busy="store.busy"
        @select-tab="chatTab = $event"
        @edit-requirements="goTo('form')"
        @restart="handleRestart"
        @send="activeChat.sendMessage"
        @generate-prd="handleGeneratePrd"
        @rewrite="applyClarification"
      />

      <!-- 文档生成中 / 生成失败（PRD / 接口文档 / 提示词 / 优化共用） -->
      <GeneratingStage
        v-else-if="isGeneratingPhase"
        :title="activeDocTitle"
        :streaming-content="docStreamingContent"
        :error="docError"
        :generating="isGenerating"
        :prd="prdStatus"
        :draft-only="prdShowingDraft"
        :elapsed-ms="elapsedMs"
        :back-label="backLabel"
        @back="backToInput"
      />

      <!-- 文档待确认（PRD / 接口文档 / 提示词共用） -->
      <ReviewStage
        v-else-if="isReviewing"
        :title="activeDocTitle"
        :content="activeDocContent"
        :error="docError"
        :view-state="viewState"
        :can-finish="canFinishTask"
        :has-prd="hasPrd"
        :has-api-docs="hasApiDocs"
        :has-prompts="hasPrompts"
        :run-summary="runSummary"
        :pending-items="pendingItems"
        :can-return-to-chat="canReturnToChat"
        :keep-back-button="keepBackButton"
        :back-label="backLabel"
        @save="onSaveDocument"
        @optimize="onAiOptimize"
        @back="backToInput"
        @generate-api-docs="handleGenerateApiDocs"
        @generate-prompts="handleGeneratePrompts"
        @navigate="goTo"
        @finish="handleEndTask"
        @return-to-chat="handleReturnToChat"
      />

      <!-- 全部完成 -->
      <DoneStage
        v-else-if="viewState === 'done'"
        :prd-content="documentContents.prd"
        :api-docs-content="documentContents['api-docs']"
        :prompts-content="documentContents.prompts"
        :back-label="backLabel"
        @view="goTo"
        @download="downloadDocument"
        @back="backToInput"
        @restart="handleRestart"
      />

      <!--
        完整流程的结构化表单。
        单独放在链尾且用 v-show 常驻：它是唯一需要「离开后仍保留编辑内容」的视图，
        而 `v-if/v-else-if` 链里的元素一旦失配就会被卸载、内容随之丢失。
      -->
      <RequirementsForm
        v-show="viewState === 'form'"
        :key="formKey"
        :initial="summary"
        :back-label="formBackLabel"
        @submit="handleStartConversation"
        @update:summary="handleSummaryChange"
        @back="backFromForm"
      />
    </main>

    <!--
      原先这里有一行品牌落款（「PRDwright · 由 AI 驱动的产品文档生成器」）。
      改成吸底操作栏后它与操作栏直接重叠，而品牌在侧栏顶端已有，故移除。
    -->
    </div>

    <!-- 登录 / 注册弹窗。挂在根部，顶栏点击与接口 401 都通过 store 打开它 -->
    <AuthDialog />
  </div>
</template>
