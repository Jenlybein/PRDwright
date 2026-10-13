<script setup lang="ts">
/**
 * 统一状态标签（pill）。variant 对应语义色，默认 neutral。
 *
 * 此前这里写的是 `dark:bg-success-950/40 dark:text-success-300 …`，
 * 但 success/warning/danger 这几个色阶根本没在 @theme 里定义过 —— Tailwind 不会
 * 生成这些类，`ring-1` 的 ring 色于是回落到 currentColor，深浅两套配色都失真。
 * 现在改为语义令牌，取值由 index.css 的 :root / .dark 两段集中给出。
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
      'bg-surface-2 text-muted ring-line': variant === 'neutral',
      'bg-accent-soft text-accent ring-accent-soft-line': variant === 'primary',
      'bg-success-surface text-success-fg ring-success-line': variant === 'success',
      'bg-warning-surface text-warning-fg ring-warning-line': variant === 'warning',
      'bg-danger-surface text-danger-fg ring-danger-line': variant === 'danger',
    }"
  >
    <slot />
  </span>
</template>
