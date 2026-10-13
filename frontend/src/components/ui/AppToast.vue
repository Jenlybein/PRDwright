<script setup lang="ts">
/**
 * 统一轻提示（Toast）：固定底部居中，自动淡出由消费方控制显隐，组件只负责样式。
 * 计划替代 AppV2 里手写的 flashSavedTip 内联徽章（见 P2-2）。
 */
import AppIcon from './AppIcon.vue'

withDefaults(
  defineProps<{
    /** 显隐由父组件 v-if 控制，组件本身无定时逻辑 */
    variant?: 'success' | 'error' | 'info'
    iconPath?: string
  }>(),
  {
    variant: 'success',
    iconPath:
      'M10 18a8 8 0 1 0 0-16 8 8 0 0 0 0 16Zm3.857-9.809a.75.75 0 0 0-1.214-.882l-3.483 4.79-1.88-1.88a.75.75 0 1 1-1.06 1.061l2.5 2.5a.75.75 0 0 0 1.137-.089l4-5.5Z',
  },
)

const ringClass = {
  success: 'bg-success-50 text-success-700 ring-success-200',
  error: 'bg-danger-50 text-danger-700 ring-danger-200',
  info: 'bg-primary-50 text-primary-700 ring-primary-200',
} as const
</script>

<template>
  <Transition
    enter-active-class="transition duration-200"
    enter-from-class="opacity-0 translate-y-1"
    leave-active-class="transition duration-500"
    leave-to-class="opacity-0"
  >
    <div
      class="pointer-events-none fixed bottom-6 left-1/2 z-50 -translate-x-1/2"
      role="status"
      aria-live="polite"
    >
      <div
        class="flex items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-medium shadow-pop ring-1"
        :class="ringClass[variant]"
      >
        <AppIcon :path="iconPath" size="sm" />
        <slot />
      </div>
    </div>
  </Transition>
</template>
