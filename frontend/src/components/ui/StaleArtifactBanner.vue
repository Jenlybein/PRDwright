<script setup lang="ts">
import AppButton from './AppButton.vue'

/**
 * 下游产物「可能已过期」的提示条。
 *
 * 上游（PRD / 接口文档）换了版本后，旧的下游文档**不再被删除**：它仍然是可读、
 * 可下载的内容，只是可能对不上新上游。这里只提示、不阻断，并给一个「一键更新」——
 * 用当前上游重新生成这一份（先确认，走该文档现有那条整篇生成入口）。
 *
 * 只用于**有上游**的产物：PRD 没有上游，那一份审阅页不渲染本组件（见 `ReviewStage`），
 * 因此按钮与文案不必再分「这次能不能更新」。
 *
 * 文案不写具体版本号：`DocumentItem` 刻意不暴露 `version_no`，而且比较用的是内容指纹
 * 而不是版本号（手改不升版本），说「基于旧版」比说「基于 v2」更准确。
 */
withDefaults(
  defineProps<{
    /** 上游名称，用于文案；缺省为「上游文档」 */
    upstreamLabel?: string
    /** 这份文档是否正被写（生成 / 就地优化）：置灰，别在同一份上叠加第二个任务 */
    busy?: boolean
  }>(),
  { upstreamLabel: '上游文档', busy: false },
)

const emit = defineEmits<{
  /** 用户点了「一键更新」：确认与发起由上层负责，这里只报告意图 */
  update: []
}>()
</script>

<template>
  <div
    class="flex flex-wrap items-center gap-2 rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-700 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-300"
    role="status"
  >
    <svg class="h-4 w-4 shrink-0" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path
        fill-rule="evenodd"
        d="M8.485 2.495c.673-1.167 2.357-1.167 3.03 0l6.28 10.875c.673 1.167-.17 2.625-1.516 2.625H3.72c-1.347 0-2.189-1.458-1.515-2.625L8.485 2.495ZM10 6a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5A.75.75 0 0 1 10 6Zm0 8a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z"
        clip-rule="evenodd"
      />
    </svg>
    <span class="min-w-0 flex-1">{{ upstreamLabel }}已更新，当前这份基于旧版。建议重新生成或手动核对。</span>
    <AppButton variant="secondary" size="sm" :disabled="busy" @click="emit('update')">
      一键更新
    </AppButton>
  </div>
</template>
