<script setup lang="ts">
/**
 * 统一图标：消费方传入 SVG path 的 d 值（沿用项目既有的 iconPath 数据），
 * 由组件统一 viewBox / fill / aria-hidden 与尺寸、颜色（颜色由外部 class 控制）。
 */
withDefaults(
  defineProps<{
    /** SVG path 的 d 属性 */
    path: string
    /** 尺寸预设；也可由外部 class 覆盖 */
    size?: 'sm' | 'md' | 'lg'
  }>(),
  { size: 'md' },
)

const sizeMap = { sm: 'h-4 w-4', md: 'h-5 w-5', lg: 'h-6 w-6' } as const
</script>

<template>
  <svg
    viewBox="0 0 20 20"
    fill="currentColor"
    aria-hidden="true"
    class="shrink-0"
    :class="sizeMap[size]"
  >
    <path fill-rule="evenodd" :d="path" clip-rule="evenodd" />
  </svg>
</template>
