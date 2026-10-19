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
import PastePairStage from '@/components/stages/PastePairStage.vue'
import ProgressSteps from '@/components/stages/ProgressSteps.vue'
import PromptDebugStage from '@/components/stages/PromptDebugStage.vue'
import QuickPrdStage from '@/components/stages/QuickPrdStage.vue'
import ReviewStage from '@/components/stages/ReviewStage.vue'
import VersionDrawer from '@/components/ui/VersionDrawer.vue'
import type { AnswerPayload } from '@/components/ClarificationAskList.vue'
import { useClarificationChat } from '@/composables/useClarificationChat'
import { useDocumentPipeline } from '@/composables/useDocumentPipeline'
import { fetchActiveJobs, onUnauthorized } from '@/services/api'
import { useAuthStore } from '@/stores/auth'
import { useDocumentsStore } from '@/stores/documents'
import { useDocumentVersionsStore } from '@/stores/documentVersions'
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
 * 先选入口，再走流程。四条入口按**起点**分两组（见 `stages/EntryStage.vue`，ADR-031）：
 *
 * 从零创建 PRD
 *   1. **完整流程**（`structured`）：结构化表单逐项录入 → AI 对话澄清 → PRD → 接口文档 → 提示词；
 *   2. **快捷流程**（`prd-quick`）：一段话描述 → 直接逐章生成 PRD（跳过表单、对话与摘要确认闸门）；
 * 导入已有素材
 *   3. **我有 PRD**（`prompt-debug`）：贴入现成 PRD → 生成提示词套件 → 用「AI 优化」反复迭代；
 *   4. **我有 PRD + 接口文档**（`prd-and-api-paste`）：两份材料都贴进来 → 提示词套件，
 *      接口文档也进那次请求，提示词因此能对齐真实的接口路径与字段名。
 *
 * 四条入口在「生成 → 审阅 → 下载」这段完全共用，模式的差异（进度条显示哪几步、
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
  pastePairInput,
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
 * （见 `stores/documents.ts`）。本文件需要它的地方有四处——审阅页「查看」入口
 * 是否可用（`has*`）、完成页的下载列表（`contents`）、「按澄清结果改写」是否有
 * 可改写的内容，以及监听 `saveError` 给手改落库失败闪提示。
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
 * 与 `savedTip` 同生命周期的样式档位：决定是绿色成功药丸还是红色失败药丸。
 *
 * 失败态复用同一个提示位（而不是另开一处），是因为它俩本就不会同刻出现——
 * 2.5s 自动淡出，冲突窗口可以接受。
 */
const savedTipVariant = ref<'success' | 'danger'>('success')

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
function flashSavedTip(text: string, variant: 'success' | 'danger' = 'success'): void {
  savedTip.value = text
  savedTipVariant.value = variant
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
 * 方案保存失败一律浮层提示（红色）。
 *
 * 从前这里只管「地址指向的方案打不开」，其余保存失败靠内容区那一行内联红字兜着；那一行
 * 撤掉之后（提示改成了全局浮层，见模板），这里就得把**所有**失败接住——自动保存成功可以
 * 静默，失败不能没有交代。
 *
 * `message !== previous` 是防刷屏：同一原因每 1.5 秒重试一次，文案没变就不再闪。
 */
watch(
  () => solutions.saveError,
  (message, previous) => {
    if (message && message !== previous) flashSavedTip(message, 'danger')
  },
)

/**
 * 文档手改的落库是**静默**的：成功不提示——每敲几秒弹一次会把界面淹没。
 * 只有**失败**才闪一次红色提示；同一原因反复重试（网络抖动）不再刷屏。
 *
 * 注意 `documents.saveError` 也承载「打开方案时读取文档失败」，这里不区分来源：
 * 两者对用户都是「这份文档没对上」，共用一句提示可以接受。
 */
watch(
  () => documents.saveError,
  (message, previous) => {
    if (message && message !== previous) flashSavedTip(message, 'danger')
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
 * 两份都要冲：方案的整包快照（`solutions`）与文档手改（`documents`）各有自己的队列，
 * 漏掉一边就会丢掉一边最后几秒的编辑。
 *
 * 只在 `beforeunload` 里做：它无法等待 async 完成，能起作用是「请求已经发出」这件事本身。
 * 真正可靠的落库时机是「切换方案」与「组件卸载」，`beforeunload` 只是兜底。
 */
function handleBeforeUnload(): void {
  void solutions.flushPendingSave()
  void documents.flushPendingSave()
}

onMounted(() => {
  window.addEventListener('beforeunload', handleBeforeUnload)
})

onBeforeUnmount(() => {
  window.removeEventListener('beforeunload', handleBeforeUnload)
  void solutions.flushPendingSave()
  void documents.flushPendingSave()
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
  handleGeneratePromptsFromPair,
  backToInput,
  backFromForm,
  handleEndTask,
  downloadDocument,
  onDownloadDocument,
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
// 文档版本抽屉：存为新版本 / 看历史 / 恢复
//
// 入口在审阅页底栏的内容行（ADR-015 的唯一操作落点），抽屉本身只承载**内容操作**、
// 不出现流程动作，并以底栏高度为下边距避免遮挡（见 ADR-030）。版本列表与两个写动作
// 都在 `stores/documentVersions.ts`，本文件只做接线。
// ---------------------------------------------------------------------------

const versions = useDocumentVersionsStore()

/** 版本抽屉是否展开 */
const versionDrawerOpen = ref(false)

/** 当前审阅的是哪一份产物；非审阅阶段为 null（抽屉只在审阅页可用） */
const activeDocKind = computed(() => docKindOfState(viewState.value))

/** 打开抽屉：先摆出当前文档的展示名，再拉一次版本列表 */
function openVersions(): void {
  const kind = activeDocKind.value
  if (!kind) return
  versionDrawerOpen.value = true
  void versions.load(kind)
}

/** 收起抽屉 */
function closeVersions(): void {
  versionDrawerOpen.value = false
  // 顺手退出预览：下次打开应当从列表开始，而不是停在上次看的那一版
  versions.clearPreview()
}

/** 预览某一版：拉它的完整内容（正文 + 清单 + 回执），抽屉随之加宽显示只读正文 */
function handlePreviewVersion(versionId: number): void {
  const kind = activeDocKind.value
  if (!kind) return
  void versions.preview(kind, versionId)
}

/**
 * 删除某一**历史**版本。
 *
 * 当前版本后端会拒（400），原因写在 `detail` 里——由 `versions.error` 兜住并闪一条提示，
 * 因此这里只处理成功那一支。删的是历史、不动当前正文，所以不必重新拉文档。
 */
async function handleRemoveVersion(versionId: number): Promise<void> {
  const kind = activeDocKind.value
  if (!kind) return
  const ok = await versions.remove(kind, versionId)
  if (ok) flashSavedTip('已删除该版本')
}

/**
 * 存为新版本：正文取 `documents` 的工作副本。
 *
 * 工作副本在每次输入时就已同步（`documents.save` 先改本地再排队落库），因此它就是
 * 用户眼前那一版；抽屉是纯展示组件，不持有编辑器草稿。
 */
async function handleCheckpoint(note: string): Promise<void> {
  const kind = activeDocKind.value
  if (!kind) return
  const ok = await versions.checkpoint(kind, documentContents.value[kind], note)
  if (ok) flashSavedTip('已存为新版本')
}

/** 恢复历史版本：成功后当前文档会换成被恢复的那一版 */
async function handleRestore(versionId: number): Promise<void> {
  const kind = activeDocKind.value
  if (!kind) return
  const ok = await versions.restore(kind, versionId)
  if (ok) flashSavedTip('已恢复到所选版本')
}

/**
 * 离开审阅页、或换了另一份文档，就把抽屉收起来。
 *
 * 不收的话它会浮在那儿，标题与列表却已经不是它描述的那份文档了——比看不见更糟。
 */
watch(activeDocKind, () => {
  versionDrawerOpen.value = false
  versions.clearPreview()
})

/** 换方案时清掉上一个方案的版本列表（否则打开新方案会先闪一下旧方案的版本） */
watch(
  () => documents.solutionId,
  () => versions.clear(),
)

/** 版本操作失败（读取 / 写入）闪一条红色提示；抽屉列表区也会显示同一原因 */
watch(
  () => versions.error,
  (message, previous) => {
    if (message && message !== previous) flashSavedTip(message, 'danger')
  },
)

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
 * 落点不同时（快捷流程 / 我有 PRD / 我有 PRD + 接口文档：「返回」回的是录入页，
 * 那一页不在进度条上、没有别的入口）必须保留，否则用户再也回不去。
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

/**
 * 提交结构化选择题的回答。
 *
 * 与「自己敲一段字」走同一条发送链路，只是多带一份结构化作答：
 *
 * - `payload.text` 照旧发给后端——它是后端存下的正文，也是降级（拿不到
 *   `tool_call_id`）时模型唯一能读到的东西；
 * - `payload.toolResult` 让后端把它作为工具结果回灌给当初提问的那次工具调用；
 *   拿不到（题目没带 `tool_call_id`）时就不带，后端按纯文本处理；
 * - `payload.answers` 由 `sendMessage` 挂到推入的消息上，界面据此把这轮渲染成
 *   一行轻回执，而不是把上面那段文本原样念回给用户。**它与 `toolResult`
 *   无关**——即便降级，用户看到的也仍是回执，因为他看不到模型收到了什么。
 */
function handleClarificationAnswer(payload: AnswerPayload): void {
  void activeChat.value.sendMessage(payload.text, payload.toolResult, payload.answers)
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
      <!--
        全局浮层：保存 / 操作提示 + 「刷新降级说明」。放在 `<main>` 里只是就近——它是
        `fixed` 的，脱离文档流，与这里的布局无关。

        ⚠️ 它**必须**不占布局：这两条从前是顶在进度条上方的一行，而 `solutions.saving`
        每次翻转都会建 / 删整行，把下面的内容整体推下去再弹回来（1.5 秒防抖保存一次就
        抖一次）。现在这里出现什么，正文都不会挪动。
      -->
      <div class="fixed right-4 top-4 z-50 flex w-72 flex-col items-end gap-2">
        <!-- 刷新降级说明：比普通提示「重」，所以常驻到用户自己关掉 -->
        <div
          v-if="downgradeNotice"
          class="flex w-full items-start gap-2 rounded-xl border border-line bg-surface px-3 py-2 text-xs text-subtle shadow-card dark:shadow-none"
          role="status"
        >
          <span class="flex-1 leading-relaxed">{{ downgradeNotice }}</span>
          <button
            type="button"
            class="-mr-1 shrink-0 rounded-md p-1 text-subtle transition hover:bg-surface-2 hover:text-fg"
            aria-label="关闭降级说明"
            @click="store.clearDowngrade()"
          >
            <svg class="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
              <path
                d="M6.28 5.22a.75.75 0 0 0-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 1 0 1.06 1.06L10 11.06l3.72 3.72a.75.75 0 1 0 1.06-1.06L11.06 10l3.72-3.72a.75.75 0 0 0-1.06-1.06L10 8.94 6.28 5.22Z"
              />
            </svg>
          </button>
        </div>

        <!-- 保存 / 操作提示：失败红、动作确认绿，2.5s 自动淡出（`flashSavedTip`） -->
        <Transition
          enter-active-class="transition duration-200"
          enter-from-class="opacity-0 translate-y-1"
          leave-active-class="transition duration-500"
          leave-to-class="opacity-0"
        >
          <AppBadge v-if="savedTip" :variant="savedTipVariant" class="shadow-card">
            <svg class="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
              <!-- 失败用叹号、成功用对勾：红色药丸顶着一个对勾，用户会以为「保存成功了」 -->
              <path
                v-if="savedTipVariant === 'danger'"
                fill-rule="evenodd"
                d="M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0Zm-8-5a.75.75 0 0 1 .75.75v4.5a.75.75 0 0 1-1.5 0v-4.5A.75.75 0 0 1 10 5Zm0 10a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z"
                clip-rule="evenodd"
              />
              <path
                v-else
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

      <!-- 提示：刷新降级说明已上移到 <main> 顶部的全局浮层——它不再占正文布局 -->

      <!-- ============ 第零步：选择入口（四条链路的起点，分两组） ============ -->
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

      <!-- ============ 入口 B：快捷流程（一段话 → 直接生成 PRD） ============ -->
      <QuickPrdStage
        v-else-if="viewState === 'prd-quick'"
        v-model:product-name="quickInput.productName"
        v-model:requirements="quickInput.requirements"
        @generate="handleGenerateQuickPrd"
        @back="goTo('entry')"
      />

      <!-- ============ 入口 C：我有 PRD（贴入 PRD → 生成提示词套件） ============ -->
      <PromptDebugStage
        v-else-if="viewState === 'prompt-debug'"
        v-model:prd-text="debugPrdText"
        @generate="handleGeneratePromptsFromDebugPrd"
        @back="goTo('entry')"
      />

      <!-- ============ 入口 B2：我有 PRD + 接口文档（贴两份材料 → 生成提示词套件） ============ -->
      <PastePairStage
        v-else-if="viewState === 'prd-and-api-paste'"
        v-model:prd-text="pastePairInput.prd"
        v-model:api-docs-text="pastePairInput.apiDocs"
        @generate="handleGeneratePromptsFromPair"
        @back="goTo('entry')"
      />

      <!--
        ================= 四条澄清线程共用同一块对话栏 =================

        需求澄清（生成 PRD 之前）与三条文档澄清（PRD / 接口 / 提示词生成之后）落在同一个页面上，
        顶部子标签栏切换。因此这里把「当前标签那条线程」的消息、加载态与发送动作一起传下去
        （见 `activeChat`），组件本身不判断自己是谁。

        底栏动作随标签而变：需求澄清给「生成 PRD」，三条文档线程给「按澄清结果改写」。
      -->
      <!--
        澄清对话页。`@answer` 是结构化选择题的提交出口：选项卡攒出来的回答也走
        **同一条发送链路**（带 stage、进本线程历史、`chatStreaming` 期间禁止再发）——
        它就是一封普通的用户消息，分两条通道只会多出一处要同步的守卫。
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
        @answer="handleClarificationAnswer"
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
        @draft-change="onSaveDocument"
        @download="onDownloadDocument"
        @optimize="onAiOptimize"
        @open-versions="openVersions"
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

    <!--
      版本历史抽屉：固定在右侧、默认收起，只在审阅页有内容可看时才展开。
      挂在根部（`main` 之外）：它是 `fixed` 浮层，不该受内容区 `max-w-3xl` 的宽度约束。
    -->
    <VersionDrawer
      :open="versionDrawerOpen"
      :title="activeDocTitle"
      :versions="activeDocKind ? versions.versionsByKind[activeDocKind] : []"
      :loading="versions.loading"
      :error="versions.error"
      :busy="isGenerating"
      :preview="versions.previewDetail"
      :preview-loading="versions.previewLoading"
      :preview-error="versions.previewError"
      @close="closeVersions"
      @checkpoint="handleCheckpoint"
      @restore="handleRestore"
      @preview="handlePreviewVersion"
      @close-preview="versions.clearPreview()"
      @remove="handleRemoveVersion"
    />

    <!-- 登录 / 注册弹窗。挂在根部，顶栏点击与接口 401 都通过 store 打开它 -->
    <AuthDialog />
  </div>
</template>
