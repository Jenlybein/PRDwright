<script setup lang="ts">
/**
 * 统一加载指示：环形 spinner，沿用项目既有的双色环样式。
 *
 * 底环取 --ui-line-strong、转动的那一段取 --ui-accent：
 * 两者在明暗两种主题下都是「底色上的可见描边 + 更亮/更暗的对比段」，
 * 不像此前写死的 primary-300/600 那样在暗色下出现「顶段比底环还暗」的倒挂。
 */
withDefaults(
  defineProps<{
    size?: 'sm' | 'md' | 'lg'
  }>(),
  { size: 'md' },
)

const sizeMap = { sm: 'h-3.5 w-3.5', md: 'h-4 w-4', lg: 'h-6 w-6' } as const
</script>

<template>
  <span
    role="status"
    aria-label="加载中"
    class="inline-block animate-spin rounded-full border-2 border-line-strong border-t-accent"
    :class="sizeMap[size]"
  />
</template>
