<script setup lang="ts">
/**
 * 统一加载指示：环形 spinner，沿用项目既有的 primary 双色环样式。
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
    class="inline-block animate-spin rounded-full border-2 border-primary-300 border-t-primary-600"
    :class="sizeMap[size]"
  />
</template>
