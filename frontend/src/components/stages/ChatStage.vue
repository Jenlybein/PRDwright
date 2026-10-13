<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'

import ChatInput from '@/components/ChatInput.vue'
import MessageList from '@/components/MessageList.vue'
import AppActionBar from '@/components/ui/AppActionBar.vue'
import AppButton from '@/components/ui/AppButton.vue'
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
  <section class="space-y-3">
    <!--
      对话卡片只负责消息区。输入框已下沉到页面级底栏的内容行里——
      与「重新开始 / 改需求 / 生成 PRD」同处一条栏，用户不必在卡片底部与页面底部之间来回找。
    -->
    <div class="overflow-hidden rounded-2xl border border-line bg-surface shadow-sm">
      <div class="border-b border-line px-4 py-3 sm:px-6">
        <h2 class="text-sm font-semibold text-fg">需求澄清对话</h2>
        <!-- 刻意不写具体数字：提问上限是后端提示词的口径（见 ADR-010），
             文案里再落一份数字就等于又多出一个需要手工同步的副本 -->
        <p class="mt-0.5 text-xs text-subtle">
          AI 会追问关键细节，逐步澄清后再生成文档
        </p>
      </div>

      <!-- 出错时把原因贴在会话区顶部，不打断已有消息 -->
      <p
        v-if="error"
        class="border-b border-red-200 bg-red-50 px-4 py-2 text-xs leading-relaxed text-red-600 dark:border-red-800 dark:bg-red-950/40 dark:text-red-300 sm:px-6"
        role="alert"
      >
        {{ error }}
      </p>

      <!-- 消息区：内部滚动。输入框移出后，这里就是一个纯粹的滚动容器 -->
      <div class="flex h-[min(60vh,560px)] flex-col bg-surface-2">
        <MessageList :messages="messages" />
      </div>
    </div>

    <!-- 页面级操作栏：内容行是输入框，流程行左「重新开始 │ 改需求」、右「生成 PRD」 -->
    <AppActionBar>
      <template #tools>
        <ChatInput ref="inputRef" embedded :loading="loading" @send="emit('send', $event)" />
      </template>

      <template #danger>
        <AppButton variant="danger" size="md" @click="emit('restart')">重新开始</AppButton>
      </template>
      <template #secondary>
        <AppButton variant="secondary" size="md" @click="emit('editRequirements')">
          改需求
        </AppButton>
      </template>
      <template #hint>
        <p v-if="canGeneratePrd" class="text-xs text-subtle">
          需求已澄清。生成前会先让你确认「补充信息回填到需求摘要」的变更。
        </p>
      </template>

      <AppButton v-if="canGeneratePrd" variant="primary" size="md" @click="emit('generatePrd')">
        <svg class="h-4 w-4" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
          <path
            fill-rule="evenodd"
            d="M11.3 1.046A1 1 0 0112 2v5h4a1 1 0 01.82 1.573l-7 10A1 1 0 018 18v-5H4a1 1 0 01-.82-1.573l7-10a1 1 0 011.12-.38z"
            clip-rule="evenodd"
          />
        </svg>
        生成 PRD
      </AppButton>
    </AppActionBar>
  </section>
</template>
