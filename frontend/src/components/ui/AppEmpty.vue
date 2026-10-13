<script setup lang="ts">
/**
 * 统一空状态占位：图标 + 标题 + 说明，用于列表/结果区无数据时的引导。
 */
import AppIcon from './AppIcon.vue'

withDefaults(
  defineProps<{
    title?: string
    description?: string
    /** 图标 path 的 d 值，缺省为通用「文档」图标 */
    iconPath?: string
  }>(),
  {
    title: '暂无内容',
    description: '',
    iconPath:
      'M4 4a2 2 0 0 1 2-2h5.586a1 1 0 0 1 .707.293L15.707 5.5a1 1 0 0 1 .293.707V16a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4Zm7 1.5V7h1.5L11 5.5Z',
  },
)
</script>

<template>
  <div class="flex flex-col items-center justify-center gap-2 px-4 py-12 text-center">
    <span class="flex h-11 w-11 items-center justify-center rounded-xl bg-surface-2 text-subtle">
      <AppIcon :path="iconPath" size="lg" />
    </span>
    <p class="text-sm font-medium text-body">{{ title }}</p>
    <p v-if="description" class="max-w-xs text-xs text-subtle">{{ description }}</p>
    <slot />
  </div>
</template>
