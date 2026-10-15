<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'

import ChatInput from '@/components/ChatInput.vue'
import MessageList from '@/components/MessageList.vue'
import AppActionBar from '@/components/ui/AppActionBar.vue'
import AppButton from '@/components/ui/AppButton.vue'
import type { ChatMessage } from '@/types'

/**
 * 文档澄清阶段视图。
 *
 * 与需求澄清（`ChatStage`）长得像，但**解决的是一件事**：把某一份已生成文档里那些标着
 * 「待确认」的地方逐条问清楚，然后按这一轮结论就地改写这份文档。差异有三处，都不是装饰：
 *
 * 1. 这里多一个「按澄清结果改写」的主操作。需求澄清的终点是「生成 PRD」（另一条链路），
 *    而澄清本身不产生文档——用户聊完必须显式触发改写，否则他会以为说完就自动改好了。
 * 2. 提示条说明**改写只动涉及的章节**。这是用户最担心的事（「别把我改好的稿子覆盖掉」），
 *    提前写出来比事后解释便宜得多。
 * 3. 聊天记录与需求澄清的 `messages` 分开存：混在一起会让「从对话回填需求摘要」那道闸门
 *    把文档答疑也读成需求输入。
 */
const props = defineProps<{
  /** 交给列表渲染的消息（含流式接收中的那一条） */
  messages: ChatMessage[]
  /** 这次澄清针对哪一份产物，如「产品需求文档」 */
  targetLabel: string
  /** 该文档当前的待确认项数量；为 0 时不鼓励改写 */
  pendingCount: number
  /** 是否正在流式接收回复 */
  loading?: boolean
  /** 是否正在按澄清结果改写（改写任务进行中） */
  rewriting?: boolean
  /** 对话框与改写过程的错误提示 */
  error?: string
}>()

const emit = defineEmits<{
  /** 发送一条消息 */
  send: [content: string]
  /** 点击「按澄清结果改写」 */
  rewrite: []
  /** 返回该文档的审阅页 */
  back: []
}>()

/** 输入框实例，用于在一轮回复结束后把焦点交还给用户 */
const inputRef = ref<InstanceType<typeof ChatInput> | null>(null)

// 回复结束（流式状态由 true 落回 false）时聚焦输入框，用户可以直接接着追问。
// 与 `ChatStage` 同一处理：焦点归还纯属视图行为，不必为它向外暴露组件句柄。
watch(
  () => props.loading,
  (now, before) => {
    if (!before || now) return
    void nextTick(() => inputRef.value?.focus())
  },
)

/** 对话里至少有一轮问答才允许改写：没有内容可依据 */
const hasConversation = computed(() => props.messages.length > 0)

/**
 * 主操作可用条件。
 *
 * `rewriting` 时禁用而不是隐藏：改写可能跑好一会儿，按钮仍在但转着，用户知道「它在干活」；
 * 藏起来只会让人以为点错了、再去找一遍入口。
 */
const canRewrite = computed(
  () => hasConversation.value && props.pendingCount > 0 && !props.rewriting && !props.loading,
)
</script>

<template>
  <section class="space-y-3">
    <div class="overflow-hidden rounded-2xl border border-line bg-surface shadow-sm">
      <div class="border-b border-line px-4 py-3 sm:px-6">
        <h2 class="text-sm font-semibold text-fg">
          澄清 {{ props.targetLabel }} 里的待确认项
        </h2>
        <!-- 刻意不写「1-2 个问题」这类具体数字：提问上限是后端提示词的口径（见 ADR-010），
             文案里再落一份数字就等于又多一个要手工同步的副本 -->
        <p class="mt-0.5 text-xs text-subtle">
          逐个聊清楚后，点「按澄清结果改写」把结论写回文档；改不到的章节会逐字保留。
        </p>
      </div>

      <!-- 出错时贴在会话区顶部，不打断已有消息 -->
      <p
        v-if="error"
        class="border-b border-red-200 bg-red-50 px-4 py-2 text-xs leading-relaxed text-red-600 dark:border-red-800 dark:bg-red-950/40 dark:text-red-300 sm:px-6"
        role="alert"
      >
        {{ error }}
      </p>

      <div class="flex h-[min(60vh,560px)] flex-col bg-surface-2">
        <MessageList :messages="props.messages" />
      </div>
    </div>

    <AppActionBar>
      <template #tools>
        <ChatInput ref="inputRef" embedded :loading="props.loading" @send="emit('send', $event)" />
      </template>

      <template #secondary>
        <AppButton variant="secondary" size="md" :disabled="props.rewriting" @click="emit('back')">
          返回审阅
        </AppButton>
      </template>
      <template #hint>
        <p v-if="props.pendingCount === 0" class="text-xs text-subtle">
          这份文档已经没有待确认项了，无需改写。
        </p>
        <p v-else-if="!hasConversation" class="text-xs text-subtle">
          先聊清楚几个关键点，再点右侧改写。
        </p>
      </template>

      <AppButton variant="primary" size="md" :disabled="!canRewrite" @click="emit('rewrite')">
        <svg class="h-4 w-4" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
          <path
            fill-rule="evenodd"
            d="M11.3 1.046A1 1 0 0112 2v5h4a1 1 0 01.82 1.573l-7 10A1 1 0 018 18v-5H4a1 1 0 01-.82-1.573l7-10a1 1 0 011.12-.38z"
            clip-rule="evenodd"
          />
        </svg>
        {{ props.rewriting ? '正在改写…' : '按澄清结果改写' }}
      </AppButton>
    </AppActionBar>
  </section>
</template>
