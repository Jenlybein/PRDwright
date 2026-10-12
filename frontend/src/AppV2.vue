<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'

import ChatInput from '@/components/ChatInput.vue'
import DocumentReview from '@/components/DocumentReview.vue'
import MessageList from '@/components/MessageList.vue'
import RequirementsForm from '@/components/RequirementsForm.vue'
import {
  continueConversationStream,
  generateApiDocsStream,
  generatePrdStream,
  generatePromptsStream,
  optimizeDocumentStream,
  startConversationStream,
} from '@/services/api'
import { STEP_OF_STATE, STEPS } from '@/types'
import type { ChatMessage, RequirementsSummary, SessionDataV2, StepId, ViewState } from '@/types'
import { downloadFile } from '@/utils/download'
import { buildAnswers, buildRequirementsText } from '@/utils/requirements'

/**
 * 完整应用（V2）。
 *
 * 流程与 V1 一致：描述产品 → AI 对话澄清 → 生成 PRD → 接口文档 → 提示词 → 完成；
 * 区别只在**第一步**：不再拉取后端问卷做动态渲染，而是用固定的结构化录入表单
 * （`RequirementsForm`）逐项收集，前端直接产出与 `field-schema.json` 对齐的 JSON。
 */

/** localStorage 中保存整份会话快照的键名（与 V1 分开，避免两套流程互相覆盖） */
const SESSION_KEY = 'harnessprd:v2:session'

/** 当前所处阶段 */
const viewState = ref<ViewState>('form')

/** 结构化需求摘要；为空表示还没提交过需求 */
const summary = ref<RequirementsSummary | null>(null)

/**
 * 表单组件的 key。
 * 「重新开始」时自增，强制重建表单组件，确保里面的编辑器回到空状态。
 */
const formKey = ref(0)

/** 短暂展示的提示语（草稿保存 / 提交等） */
const savedTip = ref('')

// ---------------------------------------------------------------------------
// 对话状态
// ---------------------------------------------------------------------------

/** 已固化的聊天消息；正在流式生成的那条不在其中 */
const messages = ref<ChatMessage[]>([])

/** 后端会话 ID；为空表示后端没有可用会话，接续时退化为无状态模式 */
const conversationId = ref('')

/** 正在流式生成的回复文本，作为列表末尾那条气泡的实时内容 */
const streamingContent = ref('')

/** 是否有一轮流式请求正在进行 */
const streaming = ref(false)

/** 对话过程中的错误提示 */
const chatError = ref('')

/** 聊天输入框，一轮回复结束后把焦点交还给用户 */
const inputRef = ref<InstanceType<typeof ChatInput> | null>(null)

// ---------------------------------------------------------------------------
// 文档状态
// ---------------------------------------------------------------------------

/** 已生成的 PRD 正文（Markdown 源码） */
const prdContent = ref('')

/** 已生成的接口文档正文（Markdown 源码） */
const apiDocsContent = ref('')

/** 已生成的提示词套件正文（Markdown 源码） */
const promptsContent = ref('')

/** 是否有文档正在生成中（PRD / 接口文档 / 提示词 / 优化共用） */
const isGenerating = ref(false)

/** 文档生成中的流式增量文本，实时展示在审阅区 */
const docStreamingContent = ref('')

/** 文档生成 / 优化过程中的错误提示 */
const docError = ref('')

/** 当前正在处理的文档 key，用于定位流式内容与错误应落到哪一份 */
const activeDocKey = ref<'prd' | 'api-docs' | 'prompts'>('prd')

// ---------------------------------------------------------------------------
// 本地持久化（统一会话快照）
// ---------------------------------------------------------------------------

/** 判断任意值是否为合法的聊天消息（用于校验本地缓存的完整性） */
function isChatMessage(value: unknown): value is ChatMessage {
  if (typeof value !== 'object' || value === null) return false
  const item = value as Partial<ChatMessage>
  const roles: ChatMessage['role'][] = ['system', 'user', 'assistant']
  return typeof item.content === 'string' && roles.includes(item.role as ChatMessage['role'])
}

/** 判断一个值是否为合法的阶段 */
function isViewState(value: unknown): value is ViewState {
  return (
    typeof value === 'string' &&
    [
      'form',
      'chatting',
      'generating-prd',
      'review-prd',
      'generating-api-docs',
      'review-api-docs',
      'generating-prompts',
      'review-prompts',
      'done',
    ].includes(value)
  )
}

/**
 * 生成中状态刷新后无法继续（流式请求已随页面销毁），需要降级为对应的「待确认」状态，
 * 让用户看到已生成 / 未生成的结果并决定下一步。
 */
const GENERATING_TO_REVIEW: Partial<Record<ViewState, ViewState>> = {
  'generating-prd': 'review-prd',
  'generating-api-docs': 'review-api-docs',
  'generating-prompts': 'review-prompts',
}

/** 把当前所有相关状态组装成一份会话快照 */
function collectSession(): SessionDataV2 {
  return {
    viewState: viewState.value,
    summary: summary.value,
    messages: messages.value,
    conversationId: conversationId.value,
    prdContent: prdContent.value,
    apiDocsContent: apiDocsContent.value,
    promptsContent: promptsContent.value,
  }
}

/** 把整份会话快照写入 localStorage；写入失败（隐私模式 / 超限）时静默跳过 */
function saveSession(): void {
  try {
    localStorage.setItem(SESSION_KEY, JSON.stringify(collectSession()))
  } catch {
    // 隐私模式或存储超限时静默跳过，不影响使用
  }
}

/**
 * 从 localStorage 读取会话快照；缺失或损坏时返回 null。
 * 读取时会对 `viewState` 做降级：若上次停留在 `generating-*`，归位到对应的 `review-*`，
 * 并把这一次降级记录在 `downgraded` 里，供界面解释为什么不是原状态。
 */
function loadSession(): { session: SessionDataV2; downgraded: boolean } | null {
  let raw: string | null
  try {
    raw = localStorage.getItem(SESSION_KEY)
  } catch {
    return null
  }
  if (!raw) return null

  try {
    const parsed: unknown = JSON.parse(raw)
    if (typeof parsed !== 'object' || parsed === null) return null
    const record = parsed as Partial<SessionDataV2>

    // 阶段校验 + 生成中状态降级
    let viewState: ViewState = 'form'
    let downgraded = false
    if (isViewState(record.viewState)) {
      const target = GENERATING_TO_REVIEW[record.viewState]
      viewState = target ?? record.viewState
      downgraded = target !== undefined
    }

    return {
      downgraded,
      session: {
        viewState,
        summary:
          typeof record.summary === 'object' && record.summary !== null
            ? (record.summary as RequirementsSummary)
            : null,
        messages: Array.isArray(record.messages) ? record.messages.filter(isChatMessage) : [],
        conversationId: typeof record.conversationId === 'string' ? record.conversationId : '',
        prdContent: typeof record.prdContent === 'string' ? record.prdContent : '',
        apiDocsContent: typeof record.apiDocsContent === 'string' ? record.apiDocsContent : '',
        promptsContent: typeof record.promptsContent === 'string' ? record.promptsContent : '',
      },
    }
  } catch {
    return null
  }
}

/** 初始化：加载会话快照并回填各状态；无快照时保持默认（第一步） */
const restored = loadSession()
const restoredSession = restored?.session ?? null
if (restoredSession) {
  viewState.value = restoredSession.viewState
  summary.value = restoredSession.summary
  messages.value = restoredSession.messages
  conversationId.value = restoredSession.conversationId
  prdContent.value = restoredSession.prdContent
  apiDocsContent.value = restoredSession.apiDocsContent
  promptsContent.value = restoredSession.promptsContent
}

/** 刷新时正处于生成中：给用户一句解释，说明为什么落到了待确认态 */
const wasGenerating = restored?.downgraded ?? false

// 状态变化时整体落盘：阶段、需求摘要、对话、会话 ID、三份文档统一写入一份快照。
// 流式过程中的临时文本（streamingContent / docStreamingContent）不在此列，
// 因此不会高频写存储。
watch(
  [viewState, summary, messages, conversationId, prdContent, apiDocsContent, promptsContent],
  () => saveSession(),
  { deep: true },
)

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
// 第一步：结构化表单
// ---------------------------------------------------------------------------

/** 表单内容变化：实时更新摘要，watch 会把它写进本地快照作为草稿 */
function handleSummaryChange(value: RequirementsSummary): void {
  summary.value = value
}

/**
 * 提交需求并开启会话：切到对话阶段 → 调流式接口 → 实时展示 AI 的第一轮追问。
 *
 * 表单内部已完成校验，这里只负责推进流程；`answers` 由结构化摘要映射到后端
 * 已有题目的字段名（见 `utils/requirements.ts`）。
 */
async function handleStartConversation(value: RequirementsSummary): Promise<void> {
  summary.value = value

  // 先切阶段再发请求：用户立刻看到「思考中」，而不是停在表单上干等
  messages.value = []
  conversationId.value = ''
  chatError.value = ''
  goTo('chatting')
  flashSavedTip('需求已提交，正在生成回复')

  await runStream(() =>
    startConversationStream({ answers: buildAnswers(value) }, { onChunk: handleChunk }),
  )
}

// ---------------------------------------------------------------------------
// 对话流程
// ---------------------------------------------------------------------------

/**
 * 交给 MessageList 渲染的消息列表。
 * 流式生成期间在末尾挂一条 assistant 气泡：还没收到增量内容时 content 为空，
 * 由 MessageList 渲染「思考中」动画指示；首字到达后自动切换为 Markdown 实时渲染。
 */
const displayMessages = computed<ChatMessage[]>(() => {
  if (!streaming.value) return messages.value
  return [
    ...messages.value,
    {
      role: 'assistant',
      content: streamingContent.value,
      streaming: true,
    },
  ]
})

/** 把增量文本追加到流式缓冲，由 MessageList 实时渲染 */
function handleChunk(chunk: string): void {
  streamingContent.value += chunk
}

/**
 * 执行一次流式请求并收尾。
 *
 * 增量文本先落到 `streamingContent` 实时展示，请求结束后再固化成 `messages`
 * 里的一条 assistant 消息；中途出错时保留已经流出的内容，避免整段凭空消失。
 *
 * @param request 实际发起请求的函数；接续对话需要在内部做重试，因此以闭包传入。
 */
async function runStream(
  request: () => Promise<{ conversationId: string; reply: string }>,
): Promise<void> {
  streaming.value = true
  streamingContent.value = ''
  chatError.value = ''

  let reply = ''
  try {
    const result = await request()
    if (result.conversationId) conversationId.value = result.conversationId
    reply = result.reply
  } catch (error) {
    chatError.value = error instanceof Error ? error.message : String(error)
    // 报错时把已经流出的半截内容保留下来
    reply = streamingContent.value
  } finally {
    // 先结束流式状态再固化消息，否则列表里会短暂多出一条「思考中」气泡
    streaming.value = false
    const content = reply.trim()
    if (content) messages.value.push({ role: 'assistant', content })
    streamingContent.value = ''
    void nextTick(() => inputRef.value?.focus())
  }
}

/**
 * 发送一条消息并接续对话。
 *
 * 后端会话存在内存里，服务重启后 ID 会失效；收到「会话不存在」时清掉 ID，
 * 改用前端回传的完整历史重试一次（无状态模式）。
 */
async function handleSendMessage(content: string): Promise<void> {
  const text = content.trim()
  if (!text || streaming.value) return

  messages.value.push({ role: 'user', content: text })
  chatError.value = ''

  // 历史只取本轮之前的消息，本轮输入单独通过 content 传递
  const history = messages.value
    .slice(0, -1)
    .map((message) => ({ role: message.role, content: message.content }))

  await runStream(async () => {
    try {
      return await continueConversationStream(
        { content: text, conversationId: conversationId.value || undefined, history },
        { onChunk: handleChunk },
      )
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      if (!conversationId.value || !message.includes('会话不存在')) throw error
      // 会话已失效：丢掉旧 ID 与半截输出，退回无状态模式重试
      conversationId.value = ''
      streamingContent.value = ''
      return continueConversationStream({ content: text, history }, { onChunk: handleChunk })
    }
  })
}

/**
 * 统一的状态导航入口，收口所有 viewState 赋值，做流程安全校验：
 * - 流式生成 / 对话进行中时禁止跳转（防止中途切换导致状态错乱）。
 * - 跳转后统一回到页面顶部。
 */
function goTo(target: ViewState): void {
  // 流式进行中禁止跳转，避免请求与界面状态脱节
  if (streaming.value || isGenerating.value) return
  docError.value = ''
  docStreamingContent.value = ''
  viewState.value = target
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

/** 清空全部数据并回到第一步重新开始（含清除本地持久化的会话快照） */
function handleRestart(): void {
  messages.value = []
  conversationId.value = ''
  streamingContent.value = ''
  chatError.value = ''
  streaming.value = false
  summary.value = null
  prdContent.value = ''
  apiDocsContent.value = ''
  promptsContent.value = ''
  docError.value = ''
  docStreamingContent.value = ''
  isGenerating.value = false
  activeDocKey.value = 'prd'
  viewState.value = 'form'
  // 表单是 v-show 常驻组件，自增 key 强制重建，保证编辑器回到空状态
  formKey.value += 1

  // 清空本地快照，避免刷新后再次恢复旧会话
  try {
    localStorage.removeItem(SESSION_KEY)
  } catch {
    // 隐私模式等场景下静默忽略
  }
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

// ---------------------------------------------------------------------------
// 文档生成与优化
// ---------------------------------------------------------------------------

/**
 * 执行一次文档流式生成，并把增量实时写入 `docStreamingContent`。
 *
 * @param docKey 目标文档 key，用于在出错时定位提示
 * @param request 实际发起请求的闭包
 * @returns `{ reply, ok }`：`ok` 为 false 表示生成失败（如被截断），
 *   此时 `reply` 为已流出的半截内容，调用方不应把它当作完整文档推进流程。
 */
async function runDocumentStream(
  docKey: 'prd' | 'api-docs' | 'prompts',
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
 * 生成 PRD：结构化需求 + 对话历史一起交给模型。
 *
 * 需求以与 `field-schema.json` 一致的 JSON 形式传入，技能包据此做归一化与渲染。
 */
async function generatePrd(): Promise<void> {
  // 重新生成 PRD 会让基于它的后续文档失效，进度回退到 PRD
  apiDocsContent.value = ''
  promptsContent.value = ''
  const history = messages.value.map(({ role, content }) => ({ role, content }))
  const requirements = summary.value ? buildRequirementsText(summary.value) : ''
  const { reply, ok } = await runDocumentStream('prd', () =>
    generatePrdStream(
      { requirements, history },
      { onChunk: (chunk) => (docStreamingContent.value += chunk) },
    ),
  )
  if (!ok) return // 失败时留在当前状态，由调用方处理错误展示
  const content = reply.trim()
  if (content) {
    prdContent.value = content
    goTo('review-prd')
  }
}

/** 生成接口文档：以 PRD 正文为输入 */
async function generateApiDocs(): Promise<void> {
  // 重新生成接口文档会让基于它的提示词失效，进度回退到接口文档
  promptsContent.value = ''
  const { reply, ok } = await runDocumentStream('api-docs', () =>
    generateApiDocsStream(
      { prdContent: prdContent.value },
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

/** 生成提示词套件：以 PRD 正文为输入 */
async function generatePrompts(): Promise<void> {
  const { reply, ok } = await runDocumentStream('prompts', () =>
    generatePromptsStream(
      { prdContent: prdContent.value },
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

/** 优化当前正在审阅的文档（按用户指令） */
async function optimizeCurrentDocument(instruction: string): Promise<void> {
  const docKey = activeDocKey.value
  const current =
    docKey === 'prd'
      ? prdContent.value
      : docKey === 'api-docs'
        ? apiDocsContent.value
        : promptsContent.value
  if (!current.trim() || !instruction.trim()) return

  const { reply, ok } = await runDocumentStream(docKey, () =>
    optimizeDocumentStream(
      { document: current, instruction },
      { onChunk: (chunk) => (docStreamingContent.value += chunk) },
    ),
  )
  if (!ok) return // 优化失败：保留原文档，错误已写入 docError
  const content = reply.trim()
  if (!content) return
  if (docKey === 'prd') prdContent.value = content
  else if (docKey === 'api-docs') apiDocsContent.value = content
  else promptsContent.value = content
}

/** 审阅页「保存」：把编辑后的原文写回对应的文档状态 */
function onSaveDocument(content: string): void {
  if (viewState.value === 'review-prd') prdContent.value = content
  else if (viewState.value === 'review-api-docs') apiDocsContent.value = content
  else if (viewState.value === 'review-prompts') promptsContent.value = content
  flashSavedTip('文档已保存到本地')
}

/**
 * 审阅页「AI 优化」：弹窗让用户输入优化指令，再走流式优化。
 * 这里用浏览器 prompt 保持轻量，无需额外弹窗组件。
 */
async function onAiOptimize(content: string): Promise<void> {
  if (!content.trim()) return
  const instruction = window.prompt('请输入优化指令（例如：把语气改得更正式、压缩到 500 字以内）')
  if (!instruction?.trim()) return
  await optimizeCurrentDocument(instruction)
}

/** 审阅页「生成接口文档」入口：先切到生成中状态再生成 */
async function handleGenerateApiDocs(): Promise<void> {
  if (!prdContent.value.trim()) return
  goTo('generating-api-docs')
  await generateApiDocs()
}

/** 审阅页「生成提示词套件」入口：接口文档完成后才能生成，先切到生成中状态再生成 */
async function handleGeneratePrompts(): Promise<void> {
  if (!apiDocsContent.value.trim()) return
  goTo('generating-prompts')
  await generatePrompts()
}

/** 回到对话页，并清空文档生成相关的错误与流式残留 */
function backToChat(): void {
  goTo('chatting')
}

// ---------------------------------------------------------------------------
// 进度条
// ---------------------------------------------------------------------------

/** 当前阶段在进度条上的下标 */
const currentStepIndex = computed(() =>
  STEPS.findIndex((step) => step.id === STEP_OF_STATE[viewState.value]),
)

/**
 * 历史中实际到达过的最远步骤下标。
 * 进度条的可点击范围以「已生成内容 + 当前所处」的最远位置为准，而非当前所处
 * 步骤——这样回看前面的阶段后，已经生成好的后续阶段依然能点回去。
 */
const reachedStepIndex = computed<number>(() => {
  let reached = 0
  if (messages.value.length > 0) reached = 1 // 已对话
  if (prdContent.value.trim()) reached = 2 // 已生成 PRD
  if (apiDocsContent.value.trim()) reached = 3 // 已生成接口文档
  if (promptsContent.value.trim()) reached = 4 // 已生成提示词
  return Math.max(reached, currentStepIndex.value)
})

/** 当前阶段是否为「文档生成中」（进度条呼吸高亮用） */
const isGeneratingPhase = computed(() => viewState.value.startsWith('generating-'))

/** 是否处于文档待确认 */
const isReviewing = computed(() => viewState.value.startsWith('review-'))

/** 对话阶段是否满足「生成 PRD」条件：已有至少一轮 AI 回复且当前未在流式中 */
const canGeneratePrd = computed(() => {
  if (streaming.value) return false
  return messages.value.some((message) => message.role === 'assistant' && message.content.trim())
})

/** 三份文档是否都已生成（用于「完成」入口的可用性判断） */
const allDocsReady = computed(
  () =>
    prdContent.value.trim() !== '' &&
    apiDocsContent.value.trim() !== '' &&
    promptsContent.value.trim() !== '',
)

/** 是否允许进入完成页：三份文档齐备且无正在进行的流式请求 */
const canFinishTask = computed(() => allDocsReady.value && !streaming.value && !isGenerating.value)

/**
 * 进入完成页：校验三份文档齐备后切到 `done`。
 * 不满足条件时不跳转（前置守卫），保证不会落到空白的完成态。
 */
function handleEndTask(): void {
  if (!canFinishTask.value) return
  goTo('done')
}

// ---------------------------------------------------------------------------
// 文档下载
// ---------------------------------------------------------------------------

/** 各文档下载时的默认文件名（带日期后缀，避免同名覆盖） */
function buildFilename(docType: 'prd' | 'api-docs' | 'prompts'): string {
  const date = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  const stamp = `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}`
  const base: Record<typeof docType, string> = {
    prd: 'PRD',
    'api-docs': 'API文档',
    prompts: '提示词套件',
  }
  return `${base[docType]}_${stamp}.md`
}

/** 下载 PRD 文档 */
function downloadPrd(): void {
  downloadFile(prdContent.value, buildFilename('prd'))
}

/** 下载接口文档 */
function downloadApiDocs(): void {
  downloadFile(apiDocsContent.value, buildFilename('api-docs'))
}

/** 下载提示词套件 */
function downloadPrompts(): void {
  downloadFile(promptsContent.value, buildFilename('prompts'))
}

/**
 * 从对话页点击「生成 PRD」：切到生成中状态并开始生成。
 * 生成完成后 `generatePrd` 内部会把状态推进到 review-prd。
 */
async function handleGeneratePrd(): Promise<void> {
  if (!canGeneratePrd.value) return
  goTo('generating-prd')
  await generatePrd()
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

/** 步骤 id → 点击进度条时应跳转到的 ViewState（已完成的步骤回看用） */
const STEP_TO_STATE: Record<StepId, ViewState> = {
  form: 'form',
  chatting: 'chatting',
  'review-prd': 'review-prd',
  'review-api-docs': 'review-api-docs',
  'review-prompts': 'review-prompts',
}

/**
 * 某个步骤是否可点击跳转：必须已经到达过（`reachedStepIndex` 之内），且不是
 * 当前所在步骤。尚未到达的步骤与当前步骤都不可点，避免跳到空白或原地刷新。
 */
function canClickStep(index: number): boolean {
  return index <= reachedStepIndex.value && index !== currentStepIndex.value
}

/** 点击进度条中已完成的步骤，跳回对应阶段查看之前的内容 */
function handleStepClick(stepId: StepId): void {
  goTo(STEP_TO_STATE[stepId])
}
</script>

<template>
  <div class="min-h-screen bg-gradient-to-b from-sky-50 via-slate-50 to-slate-100 text-slate-900">
    <!-- 顶部导航栏 -->
    <header class="sticky top-0 z-10 border-b border-slate-200/70 bg-white/80 backdrop-blur">
      <div class="mx-auto flex max-w-3xl items-center justify-between px-4 py-3">
        <div class="flex items-center gap-3">
          <div
            class="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-primary-500 to-sky-600 text-sm font-bold text-white shadow-md shadow-sky-500/30"
          >
            H
          </div>
          <div>
            <div class="text-sm font-bold leading-tight text-slate-900">HarnessPRD</div>
            <div class="text-xs leading-tight text-slate-500">
              填写需求 · 生成 PRD / 接口文档 / 提示词
            </div>
          </div>
        </div>

        <!-- 状态提示徽章，自动淡出 -->
        <Transition
          enter-active-class="transition duration-200"
          enter-from-class="opacity-0 translate-y-1"
          leave-active-class="transition duration-500"
          leave-to-class="opacity-0"
        >
          <span
            v-if="savedTip"
            class="flex items-center gap-1 rounded-full bg-emerald-50 px-3 py-1 text-xs font-medium text-emerald-600 ring-1 ring-emerald-200"
          >
            <svg class="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
              <path
                fill-rule="evenodd"
                d="M10 18a8 8 0 1 0 0-16 8 8 0 0 0 0 16Zm3.857-9.809a.75.75 0 0 0-1.214-.882l-3.483 4.79-1.88-1.88a.75.75 0 1 1-1.06 1.061l2.5 2.5a.75.75 0 0 0 1.137-.089l4-5.5Z"
                clip-rule="evenodd"
              />
            </svg>
            {{ savedTip }}
          </span>
        </Transition>
      </div>
    </header>

    <main class="mx-auto max-w-3xl px-4 py-10">
      <!-- 步骤进度条 -->
      <nav class="mb-8" aria-label="生成进度">
        <ol class="flex items-start">
          <li v-for="(step, index) in STEPS" :key="step.id" class="flex flex-1 items-center">
            <div
              class="flex w-16 shrink-0 flex-col items-center gap-1.5"
              :class="canClickStep(index) ? 'cursor-pointer group' : ''"
              @click="canClickStep(index) && handleStepClick(step.id)"
            >
              <!-- 已完成显示对勾；当前步骤在生成中时呼吸高亮，否则实心高亮 -->
              <span
                class="flex h-8 w-8 items-center justify-center rounded-full text-xs font-semibold transition"
                :class="[
                  index === currentStepIndex
                    ? isGeneratingPhase
                      ? 'bg-primary-100 text-primary-700 ring-2 ring-primary-500 animate-pulse'
                      : 'bg-primary-500 text-white ring-4 ring-primary-100'
                    : index <= reachedStepIndex
                      ? 'bg-primary-500 text-white'
                      : 'bg-slate-100 text-slate-400',
                  canClickStep(index) ? 'group-hover:ring-4 group-hover:ring-primary-200' : '',
                ]"
              >
                <svg
                  v-if="index <= reachedStepIndex && index !== currentStepIndex"
                  class="h-4 w-4"
                  viewBox="0 0 20 20"
                  fill="currentColor"
                  aria-hidden="true"
                >
                  <path
                    fill-rule="evenodd"
                    d="M16.704 5.29a1 1 0 0 1 .006 1.414l-7.5 7.5a1 1 0 0 1-1.414 0l-4-4a1 1 0 1 1 1.414-1.414l3.293 3.293 6.793-6.793a1 1 0 0 1 1.408 0Z"
                    clip-rule="evenodd"
                  />
                </svg>
                <template v-else>{{ index + 1 }}</template>
              </span>
              <span
                class="whitespace-nowrap text-[11px] font-medium transition"
                :class="[
                  index <= reachedStepIndex ? 'text-primary-700' : 'text-slate-400',
                  canClickStep(index) ? 'group-hover:text-primary-600' : '',
                ]"
              >
                {{ step.label }}
              </span>
            </div>
            <!-- 步骤之间的连接线 -->
            <div
              v-if="index < STEPS.length - 1"
              class="mx-1 mb-5 h-0.5 flex-1 rounded-full transition"
              :class="index < reachedStepIndex ? 'bg-primary-400' : 'bg-slate-200'"
            ></div>
          </li>
        </ol>
      </nav>

      <!-- 刷新时正处于生成中：说明为什么停在了待确认态 -->
      <p
        v-if="wasGenerating"
        class="mb-4 rounded-xl border border-slate-200 bg-white px-4 py-2 text-xs text-slate-500"
      >
        上次的生成未完成（页面已刷新），已为你恢复到最近一次的结果，可继续操作。
      </p>

      <!-- ============ 第一步：填写结构化需求（常驻组件，离开时仅隐藏，来回切换不丢内容） ============ -->
      <RequirementsForm
        v-show="viewState === 'form'"
        :key="formKey"
        :initial="summary"
        @submit="handleStartConversation"
        @update:summary="handleSummaryChange"
      />

      <!-- ================= 第二步：与 AI 对话补充需求 ================= -->
      <section
        v-if="viewState === 'chatting'"
        class="overflow-hidden rounded-2xl border border-slate-200/80 bg-white shadow-sm"
      >
        <div class="flex items-start justify-between gap-3 border-b border-slate-200 px-4 py-3 sm:px-6">
          <div class="min-w-0">
            <h2 class="text-sm font-semibold text-slate-900">需求澄清对话</h2>
            <p class="mt-0.5 text-xs text-slate-500">AI 会追问关键细节，信息补齐后再生成文档</p>
          </div>
          <div class="flex shrink-0 gap-2">
            <button
              type="button"
              class="rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-600 transition hover:border-primary-400 hover:text-primary-700"
              @click="goTo('form')"
            >
              改需求
            </button>
            <button
              type="button"
              class="rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-600 transition hover:border-red-300 hover:text-red-600"
              @click="handleRestart"
            >
              重新开始
            </button>
          </div>
        </div>

        <!-- 出错时把原因贴在会话区顶部，不打断已有消息 -->
        <p
          v-if="chatError"
          class="border-b border-red-200 bg-red-50 px-4 py-2 text-xs leading-relaxed text-red-600 sm:px-6"
          role="alert"
        >
          {{ chatError }}
        </p>

        <!-- 固定高度的会话区：消息列表内部滚动，输入框常驻底部 -->
        <div class="flex h-[min(68vh,620px)] flex-col bg-slate-50/60">
          <MessageList :messages="displayMessages" />
          <ChatInput ref="inputRef" :loading="streaming" @send="handleSendMessage" />
        </div>

        <!-- 满足条件后显示「生成 PRD」入口 -->
        <div
          v-if="canGeneratePrd"
          class="flex items-center justify-between gap-3 border-t border-slate-200 bg-white px-4 py-3 sm:px-6"
        >
          <p class="text-xs text-slate-500">需求已澄清，可以据此生成 PRD 文档了。</p>
          <button
            type="button"
            class="inline-flex shrink-0 items-center gap-1.5 rounded-lg bg-gradient-to-r from-primary-500 to-primary-600 px-4 py-2 text-sm font-medium text-white shadow-sm transition hover:from-primary-600 hover:to-primary-700"
            @click="handleGeneratePrd"
          >
            <svg class="h-4 w-4" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
              <path
                fill-rule="evenodd"
                d="M11.3 1.046A1 1 0 0112 2v5h4a1 1 0 01.82 1.573l-7 10A1 1 0 018 18v-5H4a1 1 0 01-.82-1.573l7-10a1 1 0 011.12-.38z"
                clip-rule="evenodd"
              />
            </svg>
            生成 PRD
          </button>
        </div>
      </section>

      <!-- 文档生成中 / 生成失败（PRD / 接口文档 / 提示词 / 优化共用） -->
      <section v-else-if="isGeneratingPhase" class="space-y-3">
        <!-- 生成 / 优化出错提示：非正常结尾（如被截断）时展示原因 -->
        <p
          v-if="docError"
          class="flex items-start gap-2 rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-700"
          role="alert"
        >
          <svg class="mt-0.5 h-4 w-4 shrink-0" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
            <path
              fill-rule="evenodd"
              d="M8.485 2.495c.673-1.167 2.357-1.167 3.03 0l6.28 10.875c.673 1.167-.17 2.625-1.516 2.625H3.72c-1.347 0-2.189-1.458-1.515-2.625L8.485 2.495ZM10 6a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5A.75.75 0 0 1 10 6Zm0 8a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z"
              clip-rule="evenodd"
            />
          </svg>
          <span>{{ docError }}</span>
        </p>

        <!-- 生成中：只读流式展示；失败后不再展示空的审阅区，仅提示原因 -->
        <DocumentReview
          v-if="isGenerating"
          :title="activeDocTitle"
          :streaming-content="docStreamingContent"
          :generating="true"
        />
        <button
          type="button"
          class="rounded-lg border border-slate-300 px-4 py-2 text-sm font-medium text-slate-600 transition hover:border-primary-400 hover:text-primary-700"
          @click="backToChat"
        >
          返回对话
        </button>
      </section>

      <!-- 文档待确认（PRD / 接口文档 / 提示词共用） -->
      <section v-else-if="isReviewing" class="space-y-3">
        <!-- 优化失败等非正常结尾时展示原因（原文档内容保留不变） -->
        <p
          v-if="docError"
          class="flex items-start gap-2 rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-700"
          role="alert"
        >
          <svg class="mt-0.5 h-4 w-4 shrink-0" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
            <path
              fill-rule="evenodd"
              d="M8.485 2.495c.673-1.167 2.357-1.167 3.03 0l6.28 10.875c.673 1.167-.17 2.625-1.516 2.625H3.72c-1.347 0-2.189-1.458-1.515-2.625L8.485 2.495ZM10 6a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5A.75.75 0 0 1 10 6Zm0 8a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z"
              clip-rule="evenodd"
            />
          </svg>
          <span>{{ docError }}</span>
        </p>
        <DocumentReview
          :title="activeDocTitle"
          :content="activeDocContent"
          @save="onSaveDocument"
          @ai-optimize="onAiOptimize"
        >
          <template #actions="{ save }">
            <!-- 保存 -->
            <button
              type="button"
              class="rounded-lg bg-gradient-to-r from-primary-500 to-primary-600 px-4 py-1.5 text-xs font-medium text-white shadow-sm transition hover:from-primary-600 hover:to-primary-700"
              @click="save"
            >
              保存
            </button>
          </template>
        </DocumentReview>

        <!-- 各文档对应的后续操作 -->
        <div class="flex flex-wrap items-center justify-between gap-2">
          <button
            type="button"
            class="rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-600 transition hover:border-primary-400 hover:text-primary-700"
            @click="backToChat"
          >
            返回对话
          </button>

          <div class="flex flex-wrap items-center gap-2">
            <!-- 各阶段按顺序推进：PRD 完成 → 生成接口文档；接口文档完成 → 生成提示词套件 -->
            <button
              v-if="viewState === 'review-prd'"
              type="button"
              class="rounded-lg border border-primary-300 px-3 py-1.5 text-xs font-medium text-primary-700 transition hover:bg-primary-50"
              @click="handleGenerateApiDocs"
            >
              生成接口文档
            </button>

            <button
              v-else-if="viewState === 'review-api-docs'"
              type="button"
              class="rounded-lg border border-primary-300 px-3 py-1.5 text-xs font-medium text-primary-700 transition hover:bg-primary-50"
              @click="handleGeneratePrompts"
            >
              生成提示词套件
            </button>

            <!-- 其它已生成的文档的查看入口（当前阶段除外） -->
            <button
              v-if="viewState !== 'review-prd'"
              type="button"
              class="rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-600 transition hover:border-primary-400 hover:text-primary-700"
              @click="goTo('review-prd')"
            >
              查看 PRD
            </button>
            <button
              v-if="viewState !== 'review-api-docs' && apiDocsContent.trim()"
              type="button"
              class="rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-600 transition hover:border-primary-400 hover:text-primary-700"
              @click="goTo('review-api-docs')"
            >
              查看接口文档
            </button>
            <button
              v-if="viewState !== 'review-prompts' && promptsContent.trim()"
              type="button"
              class="rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-600 transition hover:border-primary-400 hover:text-primary-700"
              @click="goTo('review-prompts')"
            >
              查看提示词
            </button>

            <!-- 完成入口：三份文档齐备才可用 -->
            <button
              type="button"
              class="rounded-lg px-3 py-1.5 text-xs font-medium text-white shadow-sm transition"
              :class="
                canFinishTask
                  ? 'bg-gradient-to-r from-primary-500 to-primary-600 hover:from-primary-600 hover:to-primary-700'
                  : 'cursor-not-allowed bg-slate-300'
              "
              :disabled="!canFinishTask"
              @click="handleEndTask"
            >
              完成
            </button>
          </div>
        </div>
      </section>

      <!-- 全部完成 -->
      <section v-else-if="viewState === 'done'" class="space-y-4">
        <!-- 完成提示头 -->
        <div class="rounded-2xl border border-slate-200/80 bg-white p-6 text-center shadow-sm">
          <div class="mx-auto flex h-12 w-12 items-center justify-center rounded-xl bg-primary-50">
            <svg class="h-6 w-6 text-primary-600" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
              <path
                fill-rule="evenodd"
                d="M10 18a8 8 0 1 0 0-16 8 8 0 0 0 0 16Zm3.857-9.809a.75.75 0 0 0-1.214-.882l-3.483 4.79-1.88-1.88a.75.75 0 1 1-1.06 1.061l2.5 2.5a.75.75 0 0 0 1.137-.089l4-5.5Z"
                clip-rule="evenodd"
              />
            </svg>
          </div>
          <h2 class="mt-3 text-base font-semibold text-slate-900">三份文档已全部完成</h2>
          <p class="mx-auto mt-1 max-w-md text-sm text-slate-500">
            以下文档已生成完毕，可分别下载为 Markdown 文件，或返回查看与修改。
          </p>
        </div>

        <!-- 三份文档卡片 -->
        <div class="grid gap-4">
          <article
            class="flex items-center justify-between gap-3 rounded-2xl border border-slate-200/80 bg-white p-4 shadow-sm"
          >
            <div class="flex min-w-0 items-center gap-3">
              <div class="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-primary-50">
                <svg class="h-5 w-5 text-primary-600" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                  <path
                    fill-rule="evenodd"
                    d="M4 4a2 2 0 0 1 2-2h5.586a1 1 0 0 1 .707.293L15.707 5.5a1 1 0 0 1 .293.707V16a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4Zm7 1.5V7h1.5L11 5.5ZM7 9a1 1 0 0 0 0 2h6a1 1 0 1 0 0-2H7Zm0 3a1 1 0 1 0 0 2h6a1 1 0 1 0 0-2H7Z"
                    clip-rule="evenodd"
                  />
                </svg>
              </div>
              <div class="min-w-0">
                <h3 class="text-sm font-semibold text-slate-900">PRD 文档</h3>
                <p class="truncate text-xs text-slate-500">{{ prdContent.length }} 字符 · Markdown</p>
              </div>
            </div>
            <div class="flex shrink-0 gap-2">
              <button
                type="button"
                class="rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-600 transition hover:border-primary-400 hover:text-primary-700"
                @click="goTo('review-prd')"
              >
                查看
              </button>
              <button
                type="button"
                class="inline-flex items-center gap-1 rounded-lg bg-gradient-to-r from-primary-500 to-primary-600 px-3 py-1.5 text-xs font-medium text-white shadow-sm transition hover:from-primary-600 hover:to-primary-700"
                @click="downloadPrd"
              >
                <svg class="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                  <path
                    fill-rule="evenodd"
                    d="M10 2a.75.75 0 0 1 .75.75v7.94l1.72-1.72a.75.75 0 1 1 1.06 1.06l-3 3a.75.75 0 0 1-1.06 0l-3-3a.75.75 0 1 1 1.06-1.06l1.72 1.72V2.75A.75.75 0 0 1 10 2ZM5.25 12.5a.75.75 0 0 1 .75.75v2.25h8v-2.25a.75.75 0 0 1 1.5 0v3a.75.75 0 0 1-.75.75h-9.5a.75.75 0 0 1-.75-.75v-3a.75.75 0 0 1 .75-.75Z"
                    clip-rule="evenodd"
                  />
                </svg>
                下载
              </button>
            </div>
          </article>

          <article
            class="flex items-center justify-between gap-3 rounded-2xl border border-slate-200/80 bg-white p-4 shadow-sm"
          >
            <div class="flex min-w-0 items-center gap-3">
              <div class="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-sky-50">
                <svg class="h-5 w-5 text-sky-600" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                  <path
                    fill-rule="evenodd"
                    d="M2 4.75A2.75 2.75 0 0 1 4.75 2h6.5A.75.75 0 0 1 12 2v3.25h3.25a.75.75 0 0 1 .75.75v9.25A2.75 2.75 0 0 1 13.25 18h-8.5A2.75 2.75 0 0 1 2 15.25V4.75Zm8.5 3.5h3.5v-2.5h-3.5v2.5ZM6 7.75A.75.75 0 0 0 5.25 8.5v3a.75.75 0 0 0 1.5 0v-3A.75.75 0 0 0 6 7.75Zm3 0a.75.75 0 0 0-.75.75v3a.75.75 0 0 0 1.5 0v-3A.75.75 0 0 0 9 7.75Z"
                    clip-rule="evenodd"
                  />
                </svg>
              </div>
              <div class="min-w-0">
                <h3 class="text-sm font-semibold text-slate-900">接口文档</h3>
                <p class="truncate text-xs text-slate-500">{{ apiDocsContent.length }} 字符 · Markdown</p>
              </div>
            </div>
            <div class="flex shrink-0 gap-2">
              <button
                type="button"
                class="rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-600 transition hover:border-primary-400 hover:text-primary-700"
                @click="goTo('review-api-docs')"
              >
                查看
              </button>
              <button
                type="button"
                class="inline-flex items-center gap-1 rounded-lg bg-gradient-to-r from-primary-500 to-primary-600 px-3 py-1.5 text-xs font-medium text-white shadow-sm transition hover:from-primary-600 hover:to-primary-700"
                @click="downloadApiDocs"
              >
                <svg class="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                  <path
                    fill-rule="evenodd"
                    d="M10 2a.75.75 0 0 1 .75.75v7.94l1.72-1.72a.75.75 0 1 1 1.06 1.06l-3 3a.75.75 0 0 1-1.06 0l-3-3a.75.75 0 1 1 1.06-1.06l1.72 1.72V2.75A.75.75 0 0 1 10 2ZM5.25 12.5a.75.75 0 0 1 .75.75v2.25h8v-2.25a.75.75 0 0 1 1.5 0v3a.75.75 0 0 1-.75.75h-9.5a.75.75 0 0 1-.75-.75v-3a.75.75 0 0 1 .75-.75Z"
                    clip-rule="evenodd"
                  />
                </svg>
                下载
              </button>
            </div>
          </article>

          <article
            class="flex items-center justify-between gap-3 rounded-2xl border border-slate-200/80 bg-white p-4 shadow-sm"
          >
            <div class="flex min-w-0 items-center gap-3">
              <div class="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-emerald-50">
                <svg class="h-5 w-5 text-emerald-600" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                  <path
                    fill-rule="evenodd"
                    d="M9.25 2.5a.75.75 0 0 1 .75.75v6.94l1.22-1.22a.75.75 0 1 1 1.06 1.06l-2.5 2.5a.75.75 0 0 1-1.06 0l-2.5-2.5a.75.75 0 1 1 1.06-1.06l1.22 1.22V3.25a.75.75 0 0 1 .75-.75Zm-6.5 10a.75.75 0 0 1 .75.75v1h13v-1a.75.75 0 0 1 1.5 0v1.75a.75.75 0 0 1-.75.75h-14.5a.75.75 0 0 1-.75-.75v-1.75a.75.75 0 0 1 .75-.75Z"
                    clip-rule="evenodd"
                  />
                </svg>
              </div>
              <div class="min-w-0">
                <h3 class="text-sm font-semibold text-slate-900">提示词套件</h3>
                <p class="truncate text-xs text-slate-500">{{ promptsContent.length }} 字符 · Markdown</p>
              </div>
            </div>
            <div class="flex shrink-0 gap-2">
              <button
                type="button"
                class="rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-600 transition hover:border-primary-400 hover:text-primary-700"
                @click="goTo('review-prompts')"
              >
                查看
              </button>
              <button
                type="button"
                class="inline-flex items-center gap-1 rounded-lg bg-gradient-to-r from-primary-500 to-primary-600 px-3 py-1.5 text-xs font-medium text-white shadow-sm transition hover:from-primary-600 hover:to-primary-700"
                @click="downloadPrompts"
              >
                <svg class="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                  <path
                    fill-rule="evenodd"
                    d="M10 2a.75.75 0 0 1 .75.75v7.94l1.72-1.72a.75.75 0 1 1 1.06 1.06l-3 3a.75.75 0 0 1-1.06 0l-3-3a.75.75 0 1 1 1.06-1.06l1.72 1.72V2.75A.75.75 0 0 1 10 2ZM5.25 12.5a.75.75 0 0 1 .75.75v2.25h8v-2.25a.75.75 0 0 1 1.5 0v3a.75.75 0 0 1-.75.75h-9.5a.75.75 0 0 1-.75-.75v-3a.75.75 0 0 1 .75-.75Z"
                    clip-rule="evenodd"
                  />
                </svg>
                下载
              </button>
            </div>
          </article>
        </div>

        <!-- 底部操作 -->
        <div class="flex flex-wrap items-center justify-center gap-3 pt-1">
          <button
            type="button"
            class="rounded-lg border border-slate-300 px-4 py-2 text-sm font-medium text-slate-600 transition hover:border-primary-400 hover:text-primary-700"
            @click="goTo('chatting')"
          >
            返回对话
          </button>
          <button
            type="button"
            class="rounded-lg bg-gradient-to-r from-primary-500 to-primary-600 px-4 py-2 text-sm font-medium text-white shadow-sm transition hover:from-primary-600 hover:to-primary-700"
            @click="handleRestart"
          >
            重新开始
          </button>
        </div>
      </section>
    </main>

    <footer class="pb-10 text-center text-xs text-slate-400">
      HarnessPRD · 由 AI 驱动的产品文档生成器
    </footer>
  </div>
</template>
