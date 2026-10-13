<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'

import AppButton from '@/components/ui/AppButton.vue'

/**
 * 聊天输入框。
 *
 * 回车发送、Shift+Enter 换行；高度随内容自适应，超过上限后内部滚动。
 * 只负责收集输入并把内容抛给父组件，不关心消息如何发送。
 */
const props = withDefaults(
  defineProps<{
    /** 是否正在等待模型回复；为 true 时禁用输入与发送 */
    loading?: boolean
    /** 占位提示文案 */
    placeholder?: string
  }>(),
  {
    loading: false,
    placeholder: '输入你的想法，回车发送，Shift+Enter 换行',
  },
)

const emit = defineEmits<{
  /** 回车或点击发送时触发，载荷为去除首尾空白后的内容 */
  send: [content: string]
}>()

/** 输入框当前内容 */
const text = ref('')

/** 输入框元素，用于自适应高度与聚焦 */
const textareaRef = ref<HTMLTextAreaElement | null>(null)

/** 高度上限（px），超过后文本域内部滚动，避免输入框无限撑高 */
const MAX_HEIGHT = 160

/** 是否可以发送：有内容且不在加载中 */
const canSend = computed(() => text.value.trim().length > 0 && !props.loading)

/** 重新计算高度：先归零再取 scrollHeight，否则内容变少时高度不会回缩 */
function resize(): void {
  const el = textareaRef.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT)}px`
}

watch(text, () => {
  void nextTick(resize)
})

function submit(): void {
  if (!canSend.value) return
  const content = text.value.trim()
  // 先抛事件再清空，避免父组件拿到的内容被清空影响
  emit('send', content)
  text.value = ''
  void nextTick(resize)
}

function handleKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Enter') return
  // Shift+Enter 换行；输入法组合状态下的回车用于确认候选词，不能当作发送
  if (event.shiftKey || event.isComposing) return
  event.preventDefault()
  submit()
}

function focus(): void {
  textareaRef.value?.focus()
}

/** 暴露给父组件：一轮回复结束后可把焦点交还给用户 */
defineExpose({ focus })
</script>

<template>
  <div class="border-t border-slate-200 bg-white/80 px-4 py-3 backdrop-blur-sm dark:border-slate-800 dark:bg-slate-900/80 sm:px-6">
    <div
      class="mx-auto flex max-w-3xl items-end gap-2 rounded-2xl border bg-white p-2 shadow-sm transition-colors dark:bg-slate-800"
      :class="props.loading ? 'border-slate-200 dark:border-slate-700' : 'border-slate-300 focus-within:border-primary-400 dark:border-slate-700'"
    >
      <textarea
        ref="textareaRef"
        v-model="text"
        rows="1"
        :placeholder="props.placeholder"
        :disabled="props.loading"
        class="max-h-40 flex-1 resize-none bg-transparent px-2 py-1.5 text-sm leading-relaxed text-slate-800 outline-none placeholder:text-slate-400 disabled:cursor-not-allowed disabled:opacity-60 dark:text-slate-100"
        @keydown="handleKeydown"
      />

      <AppButton
        type="button"
        variant="primary"
        :disabled="!canSend"
        :loading="props.loading"
        class="h-9 shrink-0"
        @click="submit"
      >
        <span v-if="!props.loading">发送</span>
      </AppButton>
    </div>

    <p class="mx-auto mt-1.5 max-w-3xl px-1 text-xs text-slate-400 dark:text-slate-500">
      回车发送，Shift+Enter 换行
    </p>
  </div>
</template>
