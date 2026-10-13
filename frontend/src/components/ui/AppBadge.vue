<script setup lang="ts">
/**
 * 统一状态标签（pill）。variant 对应语义色，默认 neutral。
 */
withDefaults(
  defineProps<{
    variant?: 'neutral' | 'primary' | 'success' | 'warning' | 'danger'
  }>(),
  { variant: 'neutral' },
)
</script>

<template>
  <span
    class="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium ring-1"
    :class="{
      'bg-slate-100 text-slate-500 ring-slate-200 dark:bg-slate-800 dark:text-slate-400 dark:ring-slate-700': variant === 'neutral',
      'bg-primary-50 text-primary-700 ring-primary-200 dark:bg-primary-950/40 dark:text-primary-300 dark:ring-primary-800': variant === 'primary',
      'bg-success-50 text-success-700 ring-success-200 dark:bg-success-950/40 dark:text-success-300 dark:ring-success-800': variant === 'success',
      'bg-warning-50 text-warning-700 ring-warning-200 dark:bg-warning-950/40 dark:text-warning-300 dark:ring-warning-800': variant === 'warning',
      'bg-danger-50 text-danger-700 ring-danger-200 dark:bg-danger-950/40 dark:text-danger-300 dark:ring-danger-800': variant === 'danger',
    }"
  >
    <slot />
  </span>
</template>
