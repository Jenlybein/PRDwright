<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'

import { renderMarkdown } from '@/utils/markdown'
import type { ChatMessage } from '@/types'

/**
 * 聊天消息列表。
 *
 * 只负责展示：过滤 system 消息、区分左右布局、渲染 AI 回复的 Markdown，
 * 并把滚动条保持在底部。消息的增删与流式追加全部由父组件驱动。
 */
const props = defineProps<{
  /** 完整消息列表；role 为 system 的消息不会渲染 */
  messages: ChatMessage[]
}>()

/** 滚动容器，自动滚动到底部时使用 */
const scrollRef = ref<HTMLElement | null>(null)

/** 是否跟随到底部：用户主动上翻查看历史时置为 false，避免被新内容强行拽回底部 */
const stickToBottom = ref(true)

/** 贴近底部的判定阈值（px），留出一点余量以容忍滚动惯性 */
const NEAR_BOTTOM_THRESHOLD = 80

/** 过滤掉系统消息后的可见消息 */
const visibleMessages = computed(() =>
  props.messages.filter((message) => message.role !== 'system'),
)

/** 思考中的轮换提示语：长时间等待首字时给用户可感知的进度反馈，避免干等静止文案 */
const THINKING_PHRASES = [
  '正在理解你的需求要点…',
  '正在梳理产品结构与边界…',
  '正在组织文档内容…',
  '正在撰写详细回复…',
]

/** 当前展示的思考提示语 */
const thinkingPhrase = ref(THINKING_PHRASES[0] ?? '')

/** 思考提示语的当前下标 */
const thinkingIndex = ref(0)

/** 是否存在「正在思考、尚未收到任何内容」的助手消息 */
const isThinking = computed(() =>
  visibleMessages.value.some(
    (message) => message.role === 'assistant' && message.streaming && !message.content.trim(),
  ),
)

/** 思考提示语的轮换定时器 */
let thinkingTimer: number | undefined

/** 启动提示语轮换（已运行时直接重置到首条） */
function startThinkingCycle(): void {
  thinkingIndex.value = 0
  thinkingPhrase.value = THINKING_PHRASES[thinkingIndex.value] ?? ''
  if (thinkingTimer !== undefined) return
  thinkingTimer = window.setInterval(() => {
    thinkingIndex.value = (thinkingIndex.value + 1) % THINKING_PHRASES.length
    thinkingPhrase.value = THINKING_PHRASES[thinkingIndex.value] ?? ''
  }, 2200)
}

/** 停止轮换并清理定时器 */
function stopThinkingCycle(): void {
  if (thinkingTimer !== undefined) {
    window.clearInterval(thinkingTimer)
    thinkingTimer = undefined
  }
}

// 进入思考态（开始等待首字）时启动轮换，收到首字或流结束时停止
watch(
  isThinking,
  (active) => {
    if (active) startThinkingCycle()
    else stopThinkingCycle()
  },
  { immediate: true },
)

function isNearBottom(): boolean {
  const el = scrollRef.value
  if (!el) return true
  return el.scrollHeight - el.scrollTop - el.clientHeight <= NEAR_BOTTOM_THRESHOLD
}

function scrollToBottom(): void {
  const el = scrollRef.value
  if (!el) return
  el.scrollTop = el.scrollHeight
}

/** 用户滚动时更新「是否贴底」，据此决定后续是否自动跟随 */
function handleScroll(): void {
  stickToBottom.value = isNearBottom()
}

// 消息变化时滚动到底部：flush: 'post' 保证 DOM 已更新，高度才是最新的
watch(
  () => props.messages,
  () => {
    if (!stickToBottom.value) return
    void nextTick(scrollToBottom)
  },
  { deep: true, flush: 'post' },
)

onMounted(() => {
  stickToBottom.value = true
  void nextTick(scrollToBottom)
})

/** 暴露给父组件：切换到对话阶段时可直接把历史消息定位到底部 */
defineExpose({ scrollToBottom })
</script>

<template>
  <div
    ref="scrollRef"
    class="flex-1 overflow-y-auto px-4 py-6 sm:px-6"
    @scroll="handleScroll"
  >
    <!-- 空状态 -->
    <div
      v-if="!visibleMessages.length"
      class="flex h-full items-center justify-center text-sm text-slate-400"
    >
      暂无消息，输入内容开始对话
    </div>

    <div v-else class="mx-auto flex max-w-3xl flex-col gap-5">
      <div
        v-for="(message, index) in visibleMessages"
        :key="index"
        class="flex gap-3"
        :class="message.role === 'user' ? 'flex-row-reverse' : 'flex-row'"
      >
        <!-- 头像 -->
        <div
          class="flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-xs font-semibold text-white"
          :class="
            message.role === 'user'
              ? 'bg-gradient-to-br from-primary-400 to-primary-600'
              : 'bg-gradient-to-br from-slate-600 to-slate-800'
          "
        >
          {{ message.role === 'user' ? '我' : 'AI' }}
        </div>

        <!-- 气泡 -->
        <div class="min-w-0 max-w-[78%]">
          <div
            v-if="message.role === 'user'"
            class="rounded-2xl rounded-tr-sm bg-primary-500 px-4 py-2.5 text-sm leading-relaxed whitespace-pre-wrap break-words text-white shadow-sm"
          >
            {{ message.content }}
            <span v-if="message.streaming" class="animate-pulse opacity-80">▍</span>
          </div>

          <!-- AI 消息：尚未收到任何内容时显示「思考中」动画，收到内容后走 Markdown 渲染 -->
          <template v-else-if="message.role === 'assistant'">
            <!-- 思考中：跳动圆点 + 轮换提示语，缓解首字等待期的空洞感 -->
            <div
              v-if="message.streaming && !message.content.trim()"
              class="flex items-center gap-3 rounded-2xl rounded-tl-sm bg-white px-4 py-3.5 shadow-sm ring-1 ring-slate-200/70"
            >
              <span class="flex items-end gap-1" aria-hidden="true">
                <span
                  class="h-2 w-2 animate-bounce rounded-full bg-slate-400"
                  style="animation-delay: -0.32s"
                ></span>
                <span
                  class="h-2 w-2 animate-bounce rounded-full bg-slate-400"
                  style="animation-delay: -0.16s"
                ></span>
                <span class="h-2 w-2 animate-bounce rounded-full bg-slate-400"></span>
              </span>
              <span class="text-sm text-slate-500">{{ thinkingPhrase }}</span>
            </div>

            <!-- 已有内容：Markdown 渲染 + 流式光标 -->
            <div
              v-else
              class="prose prose-sm prose-slate max-w-none rounded-2xl rounded-tl-sm bg-white px-4 py-3 shadow-sm ring-1 ring-slate-200/70"
            >
              <!-- eslint-disable-next-line vue/no-v-html -- 内容经 markdown-it 渲染且已关闭原始 HTML -->
              <div v-html="renderMarkdown(message.content)" />
              <span
                v-if="message.streaming"
                class="inline-block h-4 w-1.5 animate-pulse rounded-sm bg-primary-500 align-text-bottom"
              />
            </div>
          </template>
        </div>
      </div>
    </div>
  </div>
</template>
