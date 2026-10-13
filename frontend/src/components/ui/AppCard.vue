<script setup lang="ts">
/**
 * 统一卡片容器。interactive=true 时渲染为 <button>，保留键盘可达性（卡片整体可点），
 * 用于入口选择等场景；默认渲染 <div>。
 */
withDefaults(
  defineProps<{
    interactive?: boolean
    padded?: boolean
  }>(),
  { interactive: false, padded: true },
)
</script>

<template>
  <component
    :is="interactive ? 'button' : 'div'"
    :type="interactive ? 'button' : undefined"
    class="block w-full rounded-2xl border border-slate-200/80 bg-white text-left shadow-card transition dark:border-slate-700/80 dark:bg-slate-900"
    :class="[
      padded ? 'p-5' : '',
      interactive
        ? 'cursor-pointer hover:border-primary-300 hover:shadow-md hover:shadow-primary-500/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-300 dark:hover:border-primary-400'
        : '',
    ]"
  >
    <slot />
  </component>
</template>
