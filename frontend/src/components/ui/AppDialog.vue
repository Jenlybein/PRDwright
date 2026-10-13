<script setup lang="ts">
/**
 * 统一弹窗底座：Teleport 到 body，支持背景点击关闭、Esc 关闭、标题栏关闭按钮。
 * 业务弹窗（如登录）改成基于它实现，消除各处的 Modal 样板与不一致。
 *
 * 弹窗被 Teleport 到 body，仍在 <html class="dark"> 之内，因此语义令牌照样生效
 * —— 这也是必须走令牌而不是「在弹窗里各写一遍 dark:」的原因。
 */
import { watch, onBeforeUnmount } from 'vue'

const props = withDefaults(
  defineProps<{
    modelValue: boolean
    title?: string
    closeOnBackdrop?: boolean
  }>(),
  { title: '', closeOnBackdrop: true },
)

const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()

function close(): void {
  emit('update:modelValue', false)
}

function onKey(e: KeyboardEvent): void {
  if (e.key === 'Escape') close()
}

watch(
  () => props.modelValue,
  (open) => {
    if (open) window.addEventListener('keydown', onKey)
    else window.removeEventListener('keydown', onKey)
  },
)

onBeforeUnmount(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog">
      <div v-if="modelValue" class="fixed inset-0 z-50 flex items-center justify-center p-4">
        <div
          class="absolute inset-0 bg-slate-950/50 backdrop-blur-sm"
          @click="closeOnBackdrop && close()"
        />
        <div
          class="relative z-10 max-h-[90vh] w-full max-w-md overflow-y-auto rounded-2xl border border-line bg-surface shadow-pop"
          role="dialog"
          aria-modal="true"
        >
          <header
            v-if="title || $slots.header"
            class="flex items-center justify-between border-b border-line px-5 py-3.5"
          >
            <slot name="header">
              <h2 class="text-sm font-semibold text-fg">{{ title }}</h2>
            </slot>
            <button
              type="button"
              class="rounded-lg p-1 text-subtle transition hover:bg-surface-2 hover:text-fg"
              aria-label="关闭"
              @click="close()"
            >
              <svg class="h-5 w-5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                <path
                  fill-rule="evenodd"
                  d="M6.28 5.22a.75.75 0 0 0-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 1 0 1.06 1.06L10 11.06l3.72 3.72a.75.75 0 1 0 1.06-1.06L11.06 10l3.72-3.72a.75.75 0 0 0-1.06-1.06L10 8.94 6.28 5.22Z"
                  clip-rule="evenodd"
                />
              </svg>
            </button>
          </header>
          <div class="px-5 py-4"><slot /></div>
          <footer v-if="$slots.footer" class="border-t border-line px-5 py-3.5">
            <slot name="footer" />
          </footer>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.dialog-enter-active,
.dialog-leave-active {
  transition: opacity 0.2s ease;
}
.dialog-enter-from,
.dialog-leave-to {
  opacity: 0;
}
</style>
