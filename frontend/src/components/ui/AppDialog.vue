<script setup lang="ts">
/**
 * 统一弹窗底座：Teleport 到 body，支持背景点击关闭、Esc 关闭、标题栏关闭按钮。
 * 业务弹窗（如登录）改成基于它实现，消除各处的 Modal 样板与不一致。
 *
 * 弹窗被 Teleport 到 body，仍在 <html class="dark"> 之内，因此语义令牌照样生效
 * —— 这也是必须走令牌而不是「在弹窗里各写一遍 dark:」的原因。
 *
 * 动效与无障碍的约定：
 * - 遮罩 180ms 只是透明度变化，面板同时做 220ms 的 0.96→1 缩放；两者一起收束。
 * - 弹窗是 transform-origin 规则的合法例外：它不挂在某个触发按钮上，保持居中缩放。
 * - 打开时把焦点送进面板并在面板内循环 Tab，关闭时归还给打开前的元素。
 */
import { nextTick, onBeforeUnmount, ref, useId, watch } from 'vue'

const props = withDefaults(
  defineProps<{
    modelValue: boolean
    title?: string
    closeOnBackdrop?: boolean
  }>(),
  { title: '', closeOnBackdrop: true },
)

const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()

const titleId = useId()
const panelEl = ref<HTMLElement | null>(null)

/** 打开前的焦点落点：关闭弹窗后要能原路还回去 */
let lastFocused: HTMLElement | null = null

function close(): void {
  emit('update:modelValue', false)
}

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'

function onKey(e: KeyboardEvent): void {
  if (e.key === 'Escape') {
    close()
    return
  }
  if (e.key !== 'Tab' || !panelEl.value) return

  // 焦点陷阱：Tab 到面板内最后一个可聚焦元素时回到第一个，反之亦然。
  // 不让焦点走到背后的页面——弹窗打开时背景对键盘用户而言是「不存在」的。
  const items = Array.from(panelEl.value.querySelectorAll<HTMLElement>(FOCUSABLE))
  if (items.length === 0) {
    e.preventDefault()
    return
  }
  const first = items[0] as HTMLElement
  const last = items[items.length - 1] as HTMLElement
  const active = document.activeElement

  if (e.shiftKey && active === first) {
    e.preventDefault()
    last.focus()
  } else if (!e.shiftKey && active === last) {
    e.preventDefault()
    first.focus()
  }
}

watch(
  () => props.modelValue,
  async (open) => {
    if (open) {
      lastFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null
      window.addEventListener('keydown', onKey)
      await nextTick()
      panelEl.value?.focus()
    } else {
      window.removeEventListener('keydown', onKey)
      lastFocused?.focus()
      lastFocused = null
    }
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKey)
  lastFocused?.focus()
})
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog">
      <div
        v-if="modelValue"
        class="dialog-root fixed inset-0 z-50 flex items-center justify-center overscroll-contain p-4"
      >
        <div
          class="dialog-backdrop absolute inset-0 overscroll-contain bg-stone-950/50 backdrop-blur-sm"
          @click="closeOnBackdrop && close()"
        />
        <div
          ref="panelEl"
          class="dialog-panel relative z-10 max-h-[90vh] w-full max-w-md overflow-y-auto overscroll-contain rounded-2xl border border-line bg-surface shadow-pop outline-none"
          role="dialog"
          aria-modal="true"
          :aria-labelledby="title ? titleId : undefined"
          tabindex="-1"
        >
          <header
            v-if="title || $slots.header"
            class="flex items-center justify-between border-b border-line px-5 py-3.5"
          >
            <slot name="header">
              <h2 :id="titleId" class="text-sm font-semibold text-fg">{{ title }}</h2>
            </slot>
            <button
              type="button"
              class="rounded-lg p-1 text-subtle transition-colors hover:bg-surface-2 hover:text-fg"
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
/* 遮罩只动透明度，面板带一点缩放：两者同时开始、一起收束。
   面板是 transform-origin 的例外（弹窗不挂在触发按钮上，保持中心缩放）。 */
.dialog-enter-active,
.dialog-leave-active {
  transition: opacity 180ms var(--ease-smooth);
}
.dialog-enter-active .dialog-panel,
.dialog-leave-active .dialog-panel {
  transition: transform 220ms var(--ease-smooth);
}
.dialog-enter-from,
.dialog-leave-to {
  opacity: 0;
}
.dialog-enter-from .dialog-panel,
.dialog-leave-to .dialog-panel {
  transform: scale(0.96);
}
</style>
