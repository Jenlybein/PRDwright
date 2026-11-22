<script setup lang="ts">
import { computed } from 'vue'

import type { SummaryChange, SummaryChangeType } from '@/types'
import { formatSummaryValue } from '@/utils/requirements'
import AppActionBar from '@/components/ui/AppActionBar.vue'
import AppButton from '@/components/ui/AppButton.vue'
import AppSpinner from '@/components/ui/AppSpinner.vue'

/**
 * 摘要更新确认面板。
 *
 * 在「生成 PRD」之前展示「AI 从对话里回填到结构化摘要」的逐字段差异，让用户决定
 * 是否把这次更新写回摘要。面板本身不做任何数据变更，只负责展示与抛出 `confirm`
 * / `reject` 两个决策，具体如何处理由父组件承担。
 *
 * 三态：
 * - `loading`：正在回填，只显示进度提示；
 * - `error`：回填失败，主按钮降级为「跳过更新，直接生成 PRD」；
 * - `changes` 为空：没有检测到变更，主按钮为「直接生成 PRD」。
 */
const props = withDefaults(
  defineProps<{
    /** 逐字段差异列表，由 `diffSummaries` 产出 */
    changes?: SummaryChange[]
    /** 是否正在回填中 */
    loading?: boolean
    /** 回填失败原因 */
    error?: string
  }>(),
  {
    changes: () => [],
    loading: false,
    error: '',
  },
)

const emit = defineEmits<{
  /** 确认更新（或在无变更 / 出错时确认跳过） */
  confirm: []
  /** 拒绝更改 */
  reject: []
}>()

/** 变更类型 → 展示样式与文案 */
const CHANGE_META: Record<
  SummaryChangeType,
  { text: string; badge: string; card: string; label: string }
> = {
  added: {
    text: '新增',
    badge: 'bg-success-surface text-success-fg ring-success-line',
    card: 'border-success-line bg-success-surface',
    label: 'text-success-fg',
  },
  modified: {
    text: '修改',
    badge: 'bg-accent-soft text-accent ring-accent-soft-line',
    card: 'border-accent-soft-line bg-accent-soft',
    label: 'text-accent',
  },
  removed: {
    text: '删除',
    badge: 'bg-danger-surface text-danger-fg ring-danger-line',
    card: 'border-danger-line bg-danger-surface',
    label: 'text-danger-fg',
  },
}

/** 各类型的变更条数统计 */
const counts = computed(() => {
  const result: Record<SummaryChangeType, number> = { added: 0, modified: 0, removed: 0 }
  for (const change of props.changes) result[change.changeType] += 1
  return result
})

/** 是否存在实际变更 */
const hasChanges = computed(() => props.changes.length > 0)

/** 主按钮文案随状态变化，语义始终是「继续生成 PRD」 */
const confirmLabel = computed(() => {
  if (props.error) return '跳过更新，直接生成 PRD'
  if (!hasChanges.value) return '直接生成 PRD'
  return '确认更新，生成 PRD'
})

/** 按类型取样式 */
function metaOf(type: SummaryChangeType): (typeof CHANGE_META)[SummaryChangeType] {
  return CHANGE_META[type]
}
</script>

<template>
  <section
    class="overflow-hidden rounded-2xl border border-line bg-surface shadow-sm  "
    aria-label="摘要更新确认"
  >
    <!-- 头部 -->
    <div class="border-b border-line px-4 py-3  sm:px-6">
      <div class="flex items-start gap-3">
        <div class="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-primary-50 dark:bg-primary-950/40">
          <svg class="h-5 w-5 text-primary-600" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
            <path
              fill-rule="evenodd"
              d="M15.312 11.424a5.5 5.5 0 0 1-9.201 2.466l-.312-.311h2.433a.75.75 0 0 0 0-1.5H3.989a.75.75 0 0 0-.75.75v4.242a.75.75 0 0 0 1.5 0v-2.43l.31.31a7 7 0 0 0 11.712-3.138.75.75 0 0 0-1.449-.39Zm1.23-3.723a.75.75 0 0 0 .219-.53V2.929a.75.75 0 0 0-1.5 0v2.43l-.31-.31A7 7 0 0 0 3.239 8.187a.75.75 0 0 0 1.448.389A5.5 5.5 0 0 1 13.89 6.11l.311.31h-2.432a.75.75 0 0 0 0 1.5h4.243a.75.75 0 0 0 .53-.219Z"
              clip-rule="evenodd"
            />
          </svg>
        </div>
        <div class="min-w-0 flex-1">
          <h2 class="text-sm font-semibold text-fg ">需求摘要更新确认</h2>
          <p class="mt-0.5 text-xs text-subtle">
            只回填你在对话里明确补充或修正的内容。
          </p>
        </div>
      </div>

      <!-- 变更统计 -->
      <div v-if="hasChanges && !loading" class="mt-3 flex flex-wrap items-center gap-2 text-xs">
        <span class="font-medium text-muted ">共 {{ changes.length }} 处变更</span>
        <span
          v-if="counts.added"
          class="rounded-full bg-success-surface px-2 py-0.5 font-medium text-success-fg ring-1 ring-success-line"
        >
          新增 {{ counts.added }}
        </span>
        <span
          v-if="counts.modified"
          class="rounded-full bg-primary-50 px-2 py-0.5 font-medium text-primary-700 ring-1 ring-primary-200"
        >
          修改 {{ counts.modified }}
        </span>
        <span
          v-if="counts.removed"
          class="rounded-full bg-rose-50 px-2 py-0.5 font-medium text-rose-700 ring-1 ring-rose-200"
        >
          删除 {{ counts.removed }}
        </span>
      </div>
    </div>

    <!-- 回填中 -->
    <div v-if="loading" class="flex items-center justify-center gap-2 bg-surface-2 px-4 py-12 text-sm text-subtle  ">
      <AppSpinner />
      正在从对话中整理需求摘要…
    </div>

    <!-- 回填失败 -->
    <div v-else-if="error" class="bg-surface-2 px-4 py-6  sm:px-6">
      <p
        class="flex items-start gap-2 rounded-xl border border-warning-line bg-warning-surface px-4 py-3 text-sm text-warning-fg "
        role="alert"
      >
        <svg class="mt-0.5 h-4 w-4 shrink-0" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
          <path
            fill-rule="evenodd"
            d="M8.485 2.495c.673-1.167 2.357-1.167 3.03 0l6.28 10.875c.673 1.167-.17 2.625-1.516 2.625H3.72c-1.347 0-2.189-1.458-1.515-2.625L8.485 2.495ZM10 6a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5A.75.75 0 0 1 10 6Zm0 8a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z"
            clip-rule="evenodd"
          />
        </svg>
        <span>{{ error }}</span>
      </p>
    </div>

    <!-- 无变更 -->
    <div v-else-if="!hasChanges" class="space-y-2 bg-surface-2 px-4 py-10 text-center  sm:px-6">
      <p class="text-sm text-subtle">对话里没有检测到需要回填的补充信息，摘要保持不变。</p>
      <!--
        为什么要有这一句：回填**故意保守**（只把「对摘要里已有字段的明确补充/修正」写回表单，
        不从对话里凭空造内容），所以「无变更」是正常结果，不是没读到对话。
        但只说上一句，用户会以为系统没看对话——而真正的事实是：对话全文仍会随 PRD 生成
        一起交给模型，这里的回填只是让它同时落进表单。
      -->
      <p class="text-xs text-subtle">
        对话内容仍会随 PRD 生成一起交给模型；这里只负责把能写进表单的明确补充回填进去，不会凭空补全。
      </p>
    </div>

    <!-- 变更列表 -->
    <ul v-else class="max-h-[min(52vh,480px)] space-y-2 overflow-y-auto bg-surface-2 p-4  sm:p-6">
      <li
        v-for="change in changes"
        :key="change.path"
        class="rounded-xl border bg-surface px-3 py-2.5 "
        :class="metaOf(change.changeType).card"
      >
        <div class="flex flex-wrap items-center gap-2">
          <span
            class="shrink-0 rounded-full px-2 py-0.5 text-xs font-semibold ring-1"
            :class="metaOf(change.changeType).badge"
          >
            {{ metaOf(change.changeType).text }}
          </span>
          <span class="text-sm font-medium" :class="metaOf(change.changeType).label">
            {{ change.label || change.path }}
          </span>
          <code class="truncate font-mono text-xs text-subtle">{{ change.path }}</code>
        </div>

        <div class="mt-2 space-y-1 text-xs leading-relaxed">
          <p v-if="change.changeType !== 'added'" class="flex gap-1.5">
            <span class="shrink-0 text-subtle">变更前</span>
            <span class="min-w-0 whitespace-pre-wrap break-words text-subtle line-through decoration-line-strong ">
              {{ formatSummaryValue(change.before) }}
            </span>
          </p>
          <p v-if="change.changeType !== 'removed'" class="flex gap-1.5">
            <span class="shrink-0 text-subtle">变更后</span>
            <span class="min-w-0 whitespace-pre-wrap break-words font-medium text-fg ">
              {{ formatSummaryValue(change.after) }}
            </span>
          </p>
        </div>
      </li>
    </ul>

    <!--
      面板底栏，与页面级操作栏同一套三段式（说明文字在左、主操作在最右）。
      `sticky=false`：本面板自身是 `overflow-hidden` 的卡片，吸底只会粘在卡片内部而不是视口上；
      而变更列表已限高内滚，底栏本来就常驻可见，不需要吸底。
    -->
    <AppActionBar :sticky="false">
      <template #secondary>
        <AppButton variant="secondary" size="md" :disabled="loading" @click="emit('reject')">
          拒绝更改
        </AppButton>
      </template>
      <template #hint>
        <p class="text-xs text-subtle">
          拒绝后摘要不变，可回到对话继续补充。
        </p>
      </template>

      <AppButton variant="primary" size="md" :disabled="loading" @click="emit('confirm')">
        {{ confirmLabel }}
      </AppButton>
    </AppActionBar>
  </section>
</template>
