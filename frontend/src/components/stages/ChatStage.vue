<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'

import ChatInput from '@/components/ChatInput.vue'
import MessageList from '@/components/MessageList.vue'
import type { ChatMessage } from '@/types'

/**
 * 对话阶段视图。
 *
 * 只负责呈现消息与收集输入；发请求、推进阶段由父组件决定，
 * 因此这里不持有任何流程状态。
 */
const props = defineProps<{
  /** 交给列表渲染的消息（含流式接收中的那一条） */
  messages: ChatMessage[]
  /** 是否正在流式接收回复 */
  loading?: boolean
  /** 对话过程中的错误提示 */
  error?: string
  /** 是否已满足「生成 PRD」的条件 */
  canGeneratePrd?: boolean
}>()

const emit = defineEmits<{
  /** 点击「改需求」 */
  editRequirements: []
  /** 点击「重新开始」 */
  restart: []
  /** 发送一条消息 */
  send: [content: string]
  /** 点击「生成 PRD」 */
  generatePrd: []
}>()

/** 输入框实例，用于在一轮回复结束后把焦点交还给用户 */
const inputRef = ref<InstanceType<typeof ChatInput> | null>(null)

// 回复结束（流式状态由 true 落回 false）时聚焦输入框，用户可以直接接着追问。
// 放在这里而不是父组件：焦点归还纯属视图行为，没必要为它向外暴露组件句柄。
watch(
  () => props.loading,
  (now, before) => {
    if (!before || now) return
    void nextTick(() => inputRef.value?.focus())
  },
)
</script>

<template>
  <section class="overflow-hidden rounded-2xl border border-slate-200/80 bg-white shadow-sm">
    <div class="flex items-start justify-between gap-3 border-b border-slate-200 px-4 py-3 sm:px-6">
      <div class="min-w-0">
        <h2 class="text-sm font-semibold text-slate-900">需求澄清对话</h2>
        <p class="mt-0.5 text-xs text-slate-500">AI 会追问关键细节，信息补齐后再生成文档</p>
      </div>
      <div class="flex shrink-0 gap-2">
        <button
          type="button"
          class="rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-600 transition hover:border-primary-400 hover:text-primary-700"
          @click="emit('editRequirements')"
        >
          改需求
        </button>
        <button
          type="button"
          class="rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-600 transition hover:border-red-300 hover:text-red-600"
          @click="emit('restart')"
        >
          重新开始
        </button>
      </div>
    </div>

    <!-- 出错时把原因贴在会话区顶部，不打断已有消息 -->
    <p
      v-if="error"
      class="border-b border-red-200 bg-red-50 px-4 py-2 text-xs leading-relaxed text-red-600 sm:px-6"
      role="alert"
    >
      {{ error }}
    </p>

    <!-- 固定高度的会话区：消息列表内部滚动，输入框常驻底部 -->
    <div class="flex h-[min(68vh,620px)] flex-col bg-slate-50/60">
      <MessageList :messages="messages" />
      <ChatInput ref="inputRef" :loading="loading" @send="emit('send', $event)" />
    </div>

    <!-- 满足条件后显示「生成 PRD」入口 -->
    <div
      v-if="canGeneratePrd"
      class="flex items-center justify-between gap-3 border-t border-slate-200 bg-white px-4 py-3 sm:px-6"
    >
      <p class="text-xs text-slate-500">
        需求已澄清。生成前会先让你确认「补充信息回填到需求摘要」的变更。
      </p>
      <button
        type="button"
        class="inline-flex shrink-0 items-center gap-1.5 rounded-lg bg-gradient-to-r from-primary-500 to-primary-600 px-4 py-2 text-sm font-medium text-white shadow-sm transition hover:from-primary-600 hover:to-primary-700"
        @click="emit('generatePrd')"
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
</template>
