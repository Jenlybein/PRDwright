<script setup lang="ts">
/**
 * 下游产物「可能已过期」的提示条。
 *
 * 上游（PRD / 接口文档）换了版本后，旧的下游文档**不再被删除**：它仍然是可读、
 * 可下载的内容，只是可能对不上新上游。这里只提示、不阻断，点不点重新生成由用户决定。
 *
 * 文案不写具体版本号：`DocumentItem` 刻意不暴露 `version_no`，而且比较用的是内容指纹
 * 而不是版本号（手改不升版本），说「基于旧版」比说「基于 v2」更准确。
 */
withDefaults(
  defineProps<{
    /** 上游名称，用于文案；缺省为「上游文档」 */
    upstreamLabel?: string
  }>(),
  { upstreamLabel: '上游文档' },
)
</script>

<template>
  <p
    class="flex items-start gap-2 rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-700 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-300"
    role="status"
  >
    <svg class="mt-0.5 h-4 w-4 shrink-0" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path
        fill-rule="evenodd"
        d="M8.485 2.495c.673-1.167 2.357-1.167 3.03 0l6.28 10.875c.673 1.167-.17 2.625-1.516 2.625H3.72c-1.347 0-2.189-1.458-1.515-2.625L8.485 2.495ZM10 6a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5A.75.75 0 0 1 10 6Zm0 8a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z"
        clip-rule="evenodd"
      />
    </svg>
    <span>{{ upstreamLabel }}已更新，当前这份基于旧版。建议重新生成或手动核对。</span>
  </p>
</template>
