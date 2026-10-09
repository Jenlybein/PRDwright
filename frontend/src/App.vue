<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'

import ChatInput from '@/components/ChatInput.vue'
import FormStep from '@/components/FormStep.vue'
import MessageList from '@/components/MessageList.vue'
import { continueConversationStream, getQuestions, startConversationStream } from '@/services/api'
import { STEPS, STEP_OF_STATE } from '@/types'
import type { ChatMessage, QuestionsConfig, ViewState } from '@/types'

/** localStorage 中保存表单草稿的键名 */
const STORAGE_KEY = 'harnessprd:form-data'

/** localStorage 中保存对话记录的键名 */
const CHAT_STORAGE_KEY = 'harnessprd:conversation'

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
// 进度条
// ---------------------------------------------------------------------------

/** 当前阶段在进度条上的下标 */
const currentStepIndex = computed(() =>
  STEPS.findIndex((step) => step.id === STEP_OF_STATE[viewState.value]),
)

/** 是否处于文档生成中 */
const isGenerating = computed(() => viewState.value.startsWith('generating-'))

/** 是否处于文档待确认 */
const isReviewing = computed(() => viewState.value.startsWith('review-'))

/** 生成中 / 待确认阶段对应的文档名，用于占位文案 */
const docName = computed(() => {
  const map: Record<string, string> = {
    'generating-prd': 'PRD',
    'review-prd': 'PRD',
    'generating-api-docs': '接口文档',
    'review-api-docs': '接口文档',
    'generating-prompts': '提示词文档',
    'review-prompts': '提示词文档',
  }
  return map[viewState.value] ?? '文档'
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
                      ? isGenerating
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
      </section>

      <!-- 文档生成中（PRD / 接口文档 / 提示词共用） -->
      <section
        v-else-if="isGenerating"
        class="rounded-2xl border border-slate-200/80 bg-white p-8 text-center shadow-sm"
      >
        <div class="mx-auto h-10 w-10 animate-spin rounded-full border-2 border-slate-200 border-t-primary-500"></div>
        <h2 class="mt-4 text-base font-semibold text-slate-900">正在生成{{ docName }}</h2>
        <p class="mx-auto mt-2 max-w-md text-sm text-slate-500">
          AI 正在整理你的需求并撰写文档，生成完成后会在这里展示，你可以逐段确认或修改。
        </p>
      </section>

      <!-- 文档待确认（PRD / 接口文档 / 提示词共用） -->
      <section
        v-else-if="isReviewing"
        class="rounded-2xl border border-slate-200/80 bg-white p-8 text-center shadow-sm"
      >
        <div class="mx-auto flex h-12 w-12 items-center justify-center rounded-xl bg-emerald-50">
          <svg class="h-6 w-6 text-emerald-600" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
            <path
              fill-rule="evenodd"
              d="M4 4a2 2 0 0 1 2-2h5.586a1 1 0 0 1 .707.293L15.707 5.5a1 1 0 0 1 .293.707V16a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4Zm7 1.5V7h1.5L11 5.5ZM7 9a1 1 0 0 0 0 2h6a1 1 0 1 0 0-2H7Zm0 3a1 1 0 1 0 0 2h6a1 1 0 1 0 0-2H7Z"
              clip-rule="evenodd"
            />
          </svg>
        </div>
        <h2 class="mt-4 text-base font-semibold text-slate-900">{{ docName }}已生成，等待确认</h2>
        <p class="mx-auto mt-2 max-w-md text-sm text-slate-500">
          文档编辑与确认界面尚未实现，后端接口就绪后会在这里渲染 Markdown 内容。
        </p>
        <button
          type="button"
          class="mt-5 rounded-lg border border-slate-300 px-4 py-2 text-sm font-medium text-slate-600 transition hover:border-primary-400 hover:text-primary-700"
          @click="viewState = 'chatting'"
        >
          返回对话
        </button>
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
