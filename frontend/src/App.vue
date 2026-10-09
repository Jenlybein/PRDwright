<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'

import ChatInput from '@/components/ChatInput.vue'
import DocumentReview from '@/components/DocumentReview.vue'
import FormStep from '@/components/FormStep.vue'
import MessageList from '@/components/MessageList.vue'
import {
  continueConversationStream,
  generateApiDocsStream,
  generatePrdStream,
  generatePromptsStream,
  getQuestions,
  optimizeDocumentStream,
  startConversationStream,
} from '@/services/api'
import { STEPS, STEP_OF_STATE } from '@/types'
import type { ChatMessage, QuestionsConfig, ViewState } from '@/types'

/** localStorage 中保存表单草稿的键名 */
const STORAGE_KEY = 'harnessprd:form-data'

/** localStorage 中保存对话记录的键名 */
const CHAT_STORAGE_KEY = 'harnessprd:conversation'

/** localStorage 中保存文档内容的键名 */
const DOC_STORAGE_KEY = 'harnessprd:documents'

/** 当前所处阶段 */
const viewState = ref<ViewState>('form')

const questions = ref<QuestionsConfig | null>(null)
const loading = ref(true)
const loadError = ref('')
const savedTip = ref('')

/** 表单未通过校验时的提示语 */
const formError = ref('')

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
// 本地持久化
// ---------------------------------------------------------------------------

/** 读取本地草稿；草稿缺失或损坏时返回空对象 */
function loadDraft(): Record<string, string> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    const parsed: unknown = raw ? JSON.parse(raw) : {}
    return typeof parsed === 'object' && parsed !== null
      ? (parsed as Record<string, string>)
      : {}
  } catch {
    return {}
  }
}

/** 判断任意值是否为合法的聊天消息（用于校验本地缓存的完整性） */
function isChatMessage(value: unknown): value is ChatMessage {
  if (typeof value !== 'object' || value === null) return false
  const item = value as Partial<ChatMessage>
  const roles: ChatMessage['role'][] = ['system', 'user', 'assistant']
  return typeof item.content === 'string' && roles.includes(item.role as ChatMessage['role'])
}

/** 读取本地保存的对话；缺失或损坏时返回 null */
function loadConversation(): { conversationId: string; messages: ChatMessage[] } | null {
  try {
    const raw = localStorage.getItem(CHAT_STORAGE_KEY)
    if (!raw) return null
    const parsed: unknown = JSON.parse(raw)
    if (typeof parsed !== 'object' || parsed === null) return null

    const record = parsed as { conversationId?: unknown; messages?: unknown }
    return {
      conversationId: typeof record.conversationId === 'string' ? record.conversationId : '',
      messages: Array.isArray(record.messages) ? record.messages.filter(isChatMessage) : [],
    }
  } catch {
    return null
  }
}

/** 是否已有本地草稿（用于页面提示「已为你恢复上次填写的内容」） */
const hasDraft = Object.keys(loadDraft()).length > 0

/** 读取本地保存的文档正文；缺失或损坏时返回空对象 */
function loadDocuments(): { prd: string; apiDocs: string; prompts: string } {
  try {
    const raw = localStorage.getItem(DOC_STORAGE_KEY)
    if (!raw) return { prd: '', apiDocs: '', prompts: '' }
    const parsed: unknown = JSON.parse(raw)
    if (typeof parsed !== 'object' || parsed === null) return { prd: '', apiDocs: '', prompts: '' }
    const record = parsed as { prd?: unknown; apiDocs?: unknown; prompts?: unknown }
    return {
      prd: typeof record.prd === 'string' ? record.prd : '',
      apiDocs: typeof record.apiDocs === 'string' ? record.apiDocs : '',
      prompts: typeof record.prompts === 'string' ? record.prompts : '',
    }
  } catch {
    return { prd: '', apiDocs: '', prompts: '' }
  }
}

// 刷新后恢复已生成的文档内容
const savedDocs = loadDocuments()
prdContent.value = savedDocs.prd
apiDocsContent.value = savedDocs.apiDocs
promptsContent.value = savedDocs.prompts

// 表单数据：初始取自本地草稿，填写过程中由 FormStep 通过 v-model 双向写入。
// 这里用 let 是因为 v-model 编译后需要可赋值的绑定；FormStep 只修改字段、不整体替换对象，
// 所以 formData 的响应式引用不会被改写。
let formData = reactive<Record<string, string>>(loadDraft())

// 刷新后若本地存有对话，直接回到对话阶段继续，不丢上下文
const savedConversation = loadConversation()
if (savedConversation && savedConversation.messages.length > 0) {
  messages.value = savedConversation.messages
  conversationId.value = savedConversation.conversationId
  viewState.value = 'chatting'
}

// 消息或会话 ID 变化时落盘；流式过程中的临时文本不写，避免高频写存储
watch(
  [messages, conversationId],
  () => {
    try {
      localStorage.setItem(
        CHAT_STORAGE_KEY,
        JSON.stringify({ conversationId: conversationId.value, messages: messages.value }),
      )
    } catch {
      // 隐私模式或存储超限时静默跳过，不影响对话本身
    }
  },
  { deep: true },
)

// 表单内容变化时写入 localStorage，刷新或误关页面都不丢草稿
watch(formData, (value) => {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(value))
})

// 文档正文变化时落盘，刷新后能恢复已生成的文档
watch([prdContent, apiDocsContent, promptsContent], () => {
  try {
    localStorage.setItem(
      DOC_STORAGE_KEY,
      JSON.stringify({
        prd: prdContent.value,
        apiDocs: apiDocsContent.value,
        prompts: promptsContent.value,
      }),
    )
  } catch {
    // 隐私模式或存储超限时静默跳过
  }
})

/** 「已保存」提示的定时器 */
let savedTipTimer: number | undefined

/** 短暂展示「已保存」提示，随后自动淡出 */
function flashSavedTip(text = '表单已保存到本地'): void {
  savedTip.value = text
  window.clearTimeout(savedTipTimer)
  savedTipTimer = window.setTimeout(() => {
    savedTip.value = ''
  }, 2000)
}

/** 拉取问卷配置 */
async function fetchQuestions(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    questions.value = await getQuestions()
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : String(error)
  } finally {
    loading.value = false
  }
}

onMounted(fetchQuestions)

// ---------------------------------------------------------------------------
// 表单校验
// ---------------------------------------------------------------------------

/** 校验必填题目：返回第一条错误提示，全部通过时返回空串 */
function validateForm(): string {
  const all = [
    ...(questions.value?.base_questions ?? []),
    ...(questions.value?.advanced_questions ?? []),
  ]
  for (const question of all) {
    if (!question.required) continue
    if (!String(formData[question.name] ?? '').trim()) {
      return `请先填写「${question.label}」`
    }
  }
  return ''
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
 * 提交问卷并开启对话：校验表单 → 切到对话阶段 → 调流式接口 → 实时展示回复。
 */
async function handleStartConversation(data: Record<string, string>): Promise<void> {
  Object.assign(formData, data)
  localStorage.setItem(STORAGE_KEY, JSON.stringify(formData))

  const invalid = validateForm()
  if (invalid) {
    formError.value = invalid
    return
  }
  formError.value = ''

  // 先切阶段再发请求：用户立刻看到「思考中」，而不是停在表单上干等
  messages.value = []
  conversationId.value = ''
  chatError.value = ''
  viewState.value = 'chatting'
  flashSavedTip('需求已提交，正在生成回复')
  window.scrollTo({ top: 0, behavior: 'smooth' })

  await runStream(() =>
    startConversationStream({ answers: { ...formData } }, { onChunk: handleChunk }),
  )
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

/** 清空当前对话，回到问卷阶段重新开始 */
function restart(): void {
  messages.value = []
  conversationId.value = ''
  streamingContent.value = ''
  chatError.value = ''
  streaming.value = false
  formError.value = ''
  viewState.value = 'form'
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

// ---------------------------------------------------------------------------
// 文档生成与优化
// ---------------------------------------------------------------------------

/**
 * 把问卷答案整理成 PRD 生成所需的「需求澄清」文本。
 * 逐条拼成 `问题：答案` 的行，供模型理解用户的产品想法。
 */
function buildRequirements(): string {
  const all = [
    ...(questions.value?.base_questions ?? []),
    ...(questions.value?.advanced_questions ?? []),
  ]
  return all
    .map((question) => {
      const answer = String(formData[question.name] ?? '').trim()
      return answer ? `${question.label}：${answer}` : ''
    })
    .filter(Boolean)
    .join('\n')
}

/**
 * 执行一次文档流式生成，并把增量实时写入 `docStreamingContent`。
 *
 * @param docKey 目标文档 key，用于在出错时定位提示
 * @param request 实际发起请求的闭包
 */
async function runDocumentStream(
  docKey: 'prd' | 'api-docs' | 'prompts',
  request: () => Promise<{ reply: string }>,
): Promise<string> {
  activeDocKey.value = docKey
  isGenerating.value = true
  docStreamingContent.value = ''
  docError.value = ''

  let reply = ''
  try {
    const result = await request()
    reply = result.reply
  } catch (error) {
    docError.value = error instanceof Error ? error.message : String(error)
    // 报错时保留已经流出的半截内容
    reply = docStreamingContent.value
  } finally {
    isGenerating.value = false
    docStreamingContent.value = ''
  }
  return reply
}

/** 生成 PRD：需求澄清 + 对话历史一起交给模型 */
async function generatePrd(): Promise<void> {
  const history = messages.value.map(({ role, content }) => ({ role, content }))
  const reply = await runDocumentStream('prd', () =>
    generatePrdStream(
      { requirements: buildRequirements(), history },
      { onChunk: (chunk) => (docStreamingContent.value += chunk) },
    ),
  )
  const content = reply.trim()
  if (content) {
    prdContent.value = content
    viewState.value = 'review-prd'
  }
}

/** 生成接口文档：以 PRD 正文为输入 */
async function generateApiDocs(): Promise<void> {
  const reply = await runDocumentStream('api-docs', () =>
    generateApiDocsStream(
      { prdContent: prdContent.value },
      { onChunk: (chunk) => (docStreamingContent.value += chunk) },
    ),
  )
  const content = reply.trim()
  if (content) {
    apiDocsContent.value = content
    viewState.value = 'review-api-docs'
  }
}

/** 生成提示词套件：以 PRD 正文为输入 */
async function generatePrompts(): Promise<void> {
  const reply = await runDocumentStream('prompts', () =>
    generatePromptsStream(
      { prdContent: prdContent.value },
      { onChunk: (chunk) => (docStreamingContent.value += chunk) },
    ),
  )
  const content = reply.trim()
  if (content) {
    promptsContent.value = content
    viewState.value = 'review-prompts'
  }
}

/** 优化当前正在审阅的文档（按用户指令） */
async function optimizeCurrentDocument(instruction: string): Promise<void> {
  const docKey = activeDocKey.value
  const current = docKey === 'prd' ? prdContent.value : docKey === 'api-docs' ? apiDocsContent.value : promptsContent.value
  if (!current.trim() || !instruction.trim()) return

  const reply = await runDocumentStream(docKey, () =>
    optimizeDocumentStream(
      { document: current, instruction },
      { onChunk: (chunk) => (docStreamingContent.value += chunk) },
    ),
  )
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
  viewState.value = 'generating-api-docs'
  window.scrollTo({ top: 0, behavior: 'smooth' })
  await generateApiDocs()
}

/** 审阅页「生成提示词套件」入口：先切到生成中状态再生成 */
async function handleGeneratePrompts(): Promise<void> {
  if (!prdContent.value.trim()) return
  viewState.value = 'generating-prompts'
  window.scrollTo({ top: 0, behavior: 'smooth' })
  await generatePrompts()
}

// ---------------------------------------------------------------------------
// 进度条
// ---------------------------------------------------------------------------

/** 当前阶段在进度条上的下标 */
const currentStepIndex = computed(() =>
  STEPS.findIndex((step) => step.id === STEP_OF_STATE[viewState.value]),
)

/** 当前阶段是否为「文档生成中」（进度条呼吸高亮用） */
const isGeneratingPhase = computed(() => viewState.value.startsWith('generating-'))

/** 是否处于文档待确认 */
const isReviewing = computed(() => viewState.value.startsWith('review-'))

/** 对话阶段是否满足「生成 PRD」条件：已有至少一轮 AI 回复 */
const canGeneratePrd = computed(() => {
  if (streaming.value) return false
  return messages.value.some((message) => message.role === 'assistant' && message.content.trim())
})

/**
 * 从对话页点击「生成 PRD」：切到生成中状态并开始生成。
 * 生成完成后 `generatePrd` 内部会把状态推进到 review-prd。
 */
async function handleGeneratePrd(): Promise<void> {
  if (!canGeneratePrd.value) return
  viewState.value = 'generating-prd'
  window.scrollTo({ top: 0, behavior: 'smooth' })
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

/** 判断某个步骤是否已走完 */
function isFinished(index: number): boolean {
  return index < currentStepIndex.value
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
            <div class="text-xs leading-tight text-slate-500">需求澄清 · 生成 PRD / 接口文档 / 提示词</div>
          </div>
        </div>

        <!-- 草稿保存状态徽章，自动淡出 -->
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
                d="M10 18a8 8 0 1 0 0-16 8 8 0 0 0 0 16Zm3.857-9.809a.75.75 0 0 0-1.214-.882l-3.483 4.79-1.88-1.88a.75.75 0 1 0-1.06 1.061l2.5 2.5a.75.75 0 0 0 1.137-.089l4-5.5Z"
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
            <div class="flex w-16 shrink-0 flex-col items-center gap-1.5">
              <!-- 已完成显示对勾；当前步骤在生成中时呼吸高亮，否则实心高亮 -->
              <span
                class="flex h-8 w-8 items-center justify-center rounded-full text-xs font-semibold transition"
                :class="[
                  isFinished(index)
                    ? 'bg-primary-500 text-white'
                    : index === currentStepIndex
                      ? isGeneratingPhase
                        ? 'bg-primary-100 text-primary-700 ring-2 ring-primary-500 animate-pulse'
                        : 'bg-primary-500 text-white ring-4 ring-primary-100'
                      : 'bg-slate-100 text-slate-400',
                ]"
              >
                <svg
                  v-if="isFinished(index)"
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
                :class="
                  index <= currentStepIndex ? 'text-primary-700' : 'text-slate-400'
                "
              >
                {{ step.label }}
              </span>
            </div>
            <!-- 步骤之间的连接线 -->
            <div
              v-if="index < STEPS.length - 1"
              class="mx-1 mb-5 h-0.5 flex-1 rounded-full transition"
              :class="isFinished(index) ? 'bg-primary-400' : 'bg-slate-200'"
            ></div>
          </li>
        </ol>
      </nav>

      <!-- ================= 阶段一：填写问卷 ================= -->
      <template v-if="viewState === 'form'">
        <div class="mb-8">
          <h1 class="text-2xl font-bold tracking-tight text-slate-900 sm:text-3xl">
            告诉我们你的产品想法
          </h1>
          <p class="mt-2 max-w-xl text-sm leading-relaxed text-slate-600">
            花几分钟填写下面的问卷，AI 会据此生成
            <span class="font-medium text-slate-800">PRD</span>、
            <span class="font-medium text-slate-800">接口文档</span>与
            <span class="font-medium text-slate-800">提示词文档</span>。
            <template v-if="hasDraft">已为你恢复上次填写的草稿，可以继续作答。</template>
          </p>
        </div>

        <!-- 必填项缺失的提示（FormStep 会先做行内校验，这里是兜底） -->
        <div
          v-if="formError"
          class="mb-6 flex items-start gap-2 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-600"
          role="alert"
        >
          <svg class="mt-0.5 h-4 w-4 shrink-0" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
            <path
              fill-rule="evenodd"
              d="M18 10A8 8 0 1 1 2 10a8 8 0 0 1 16 0Zm-8-4a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5A.75.75 0 0 1 10 6Zm0 8a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z"
              clip-rule="evenodd"
            />
          </svg>
          <span>{{ formError }}</span>
        </div>

        <!-- 加载中：骨架屏 -->
        <div v-if="loading" class="space-y-6" aria-busy="true" aria-label="正在加载问卷">
          <div class="rounded-2xl border border-slate-200/80 bg-white p-6 shadow-sm">
            <div class="h-4 w-28 animate-pulse rounded bg-slate-200"></div>
            <div class="mt-6 space-y-5">
              <div v-for="i in 3" :key="i" class="space-y-2">
                <div class="h-3.5 w-2/5 animate-pulse rounded bg-slate-100"></div>
                <div class="h-10 w-full animate-pulse rounded-xl bg-slate-100"></div>
              </div>
            </div>
          </div>
        </div>

        <!-- 加载失败，提供重试 -->
        <div
          v-else-if="loadError"
          class="rounded-2xl border border-red-200 bg-red-50 p-6 text-center"
        >
          <div class="mx-auto flex h-10 w-10 items-center justify-center rounded-full bg-red-100">
            <svg class="h-5 w-5 text-red-500" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
              <path
                fill-rule="evenodd"
                d="M18 10A8 8 0 1 1 2 10a8 8 0 0 1 16 0Zm-8-4a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5A.75.75 0 0 1 10 6Zm0 8a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z"
                clip-rule="evenodd"
              />
            </svg>
          </div>
          <p class="mt-3 text-sm font-medium text-red-700">问卷加载失败</p>
          <p class="mt-1 text-xs text-red-500">{{ loadError }}（请确认后端服务已启动）</p>
          <button
            type="button"
            class="mt-4 rounded-lg bg-red-500 px-4 py-2 text-sm font-medium text-white shadow-sm transition hover:bg-red-600 focus:outline-none focus:ring-4 focus:ring-red-200"
            @click="fetchQuestions"
          >
            重新加载
          </button>
        </div>

        <FormStep
          v-else
          v-model="formData"
          :questions="questions!"
          @submit="handleStartConversation"
        />
      </template>

      <!-- ================= 阶段二：与 AI 对话补充需求 ================= -->
      <section
        v-else-if="viewState === 'chatting'"
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
              @click="viewState = 'form'"
            >
              改问卷
            </button>
            <button
              type="button"
              class="rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-600 transition hover:border-red-300 hover:text-red-600"
              @click="restart"
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
        <div v-if="canGeneratePrd" class="flex items-center justify-between gap-3 border-t border-slate-200 bg-white px-4 py-3 sm:px-6">
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

      <!-- 文档生成中（PRD / 接口文档 / 提示词 / 优化共用） -->
      <section v-else-if="isGenerating" class="space-y-3">
        <!-- 生成 / 优化出错提示 -->
        <p
          v-if="docError"
          class="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-600"
          role="alert"
        >
          {{ docError }}
        </p>
        <DocumentReview
          :title="activeDocTitle"
          :streaming-content="docStreamingContent"
          :generating="true"
        />
        <button
          type="button"
          class="rounded-lg border border-slate-300 px-4 py-2 text-sm font-medium text-slate-600 transition hover:border-primary-400 hover:text-primary-700"
          @click="viewState = 'chatting'"
        >
          返回对话
        </button>
      </section>

      <!-- 文档待确认（PRD / 接口文档 / 提示词共用） -->
      <section v-else-if="isReviewing" class="space-y-3">
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
            @click="viewState = 'chatting'"
          >
            返回对话
          </button>

          <!-- PRD 完成后：生成接口文档 / 提示词 -->
          <template v-if="viewState === 'review-prd'">
            <button
              type="button"
              class="rounded-lg border border-primary-300 px-3 py-1.5 text-xs font-medium text-primary-700 transition hover:bg-primary-50"
              @click="handleGenerateApiDocs"
            >
              生成接口文档
            </button>
            <button
              type="button"
              class="rounded-lg border border-primary-300 px-3 py-1.5 text-xs font-medium text-primary-700 transition hover:bg-primary-50"
              @click="handleGeneratePrompts"
            >
              生成提示词套件
            </button>
          </template>

          <!-- 接口文档 / 提示词完成后：回到 PRD 或标记完成 -->
          <template v-else>
            <button
              type="button"
              class="rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-600 transition hover:border-primary-400 hover:text-primary-700"
              @click="viewState = 'review-prd'"
            >
              查看 PRD
            </button>
          </template>
        </div>
      </section>

      <!-- 全部完成 -->
      <section
        v-else-if="viewState === 'done'"
        class="rounded-2xl border border-slate-200/80 bg-white p-8 text-center shadow-sm"
      >
        <div class="mx-auto flex h-12 w-12 items-center justify-center rounded-xl bg-primary-50">
          <svg class="h-6 w-6 text-primary-600" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
            <path
              fill-rule="evenodd"
              d="M10 18a8 8 0 1 0 0-16 8 8 0 0 0 0 16Zm3.857-9.809a.75.75 0 0 0-1.214-.882l-3.483 4.79-1.88-1.88a.75.75 0 1 0-1.06 1.061l2.5 2.5a.75.75 0 0 0 1.137-.089l4-5.5Z"
              clip-rule="evenodd"
            />
          </svg>
        </div>
        <h2 class="mt-4 text-base font-semibold text-slate-900">三份文档已全部完成</h2>
        <p class="mx-auto mt-2 max-w-md text-sm text-slate-500">
          你可以继续修改需求重新生成，或导出已有内容。
        </p>
      </section>
    </main>

    <footer class="pb-10 text-center text-xs text-slate-400">
      HarnessPRD · 由 AI 驱动的产品文档生成器
    </footer>
  </div>
</template>
