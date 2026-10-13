<script setup lang="ts">
import { computed } from 'vue'

import type { PrdGenerationStage, PrdStageProgress } from '@/types'

/**
 * PRD 分章节生成的阶段指示条。
 *
 * 后端逐章执行「撰写 → 审核 →（有问题）改写」，这里如实展示当前在哪一步、写到第几章。
 * 后端降级为整篇生成时没有章节信息，只显示阶段、不显示计数与进度条。
 */
const props = defineProps<{
  /** 进度快照 */
  progress: PrdStageProgress
}>()

/** 展示用的步骤序列（`idle` 不占位，不会出现在这里） */
const STEPS: { id: PrdGenerationStage; label: string }[] = [
  { id: 'writing', label: '逐章撰写' },
  { id: 'reviewing', label: '章节审核' },
  { id: 'rewriting', label: '问题改写' },
  { id: 'done', label: '生成完成' },
]

/** 阶段 → 步骤下标 */
const ORDER: Record<PrdGenerationStage, number> = {
  idle: -1,
  writing: 0,
  reviewing: 1,
  rewriting: 2,
  done: 3,
}

const currentIndex = computed(() => ORDER[props.progress.stage])

/** 主提示语：尽量带上具体章节名，让用户知道正在处理哪一章 */
const headline = computed(() => {
  const { stage, sectionTitle, sectionHeading } = props.progress
  // 优先用章节名（「功能需求」）；退回标题行时去掉 `##` 前缀，避免出现「正在撰写 ## 2. 功能需求」
  const name = sectionTitle || sectionHeading.replace(/^#+\s*/, '')
  if (stage === 'done') return '全部章节已完成'
  if (stage === 'reviewing') return name ? `正在审核 ${name}` : '正在审核章节'
  if (stage === 'rewriting') return name ? `正在改写 ${name}` : '正在改写章节'
  return name ? `正在撰写 ${name}` : '正在撰写章节'
})

/**
 * 进度条百分比。
 * `done` 时后端把 `index` 置为 `total`；其余阶段给「本章进行中」半个格，
 * 否则第一章期间进度条纹丝不动，看不出在推进。
 */
const progressPercent = computed(() => {
  const { index, total, stage } = props.progress
  if (!total) return 0
  const filled = stage === 'done' ? total : index + 0.5
  return Math.min(100, Math.round((filled / total) * 100))
})

/** 步骤胶囊的样式：已完成 / 当前 / 未到达 */
function chipClass(stepIndex: number): string {
  if (stepIndex > currentIndex.value)
    return 'bg-slate-100 text-slate-400 dark:bg-slate-800 dark:text-slate-500'
  if (stepIndex === currentIndex.value)
    return 'bg-primary-500 text-white ring-4 ring-primary-100 dark:ring-primary-900/50'
  return 'bg-primary-100 text-primary-700 dark:bg-primary-900/60 dark:text-primary-300'
}
</script>

<template>
  <div class="rounded-2xl border border-slate-200/80 bg-white p-4 shadow-sm dark:border-slate-700/80 dark:bg-slate-900">
    <div class="flex items-center justify-between gap-3">
      <div class="flex min-w-0 items-center gap-2">
        <!-- 生成中转圈；完成后换成对勾，避免「已完成」还转着 -->
        <svg
          v-if="progress.stage !== 'done'"
          class="h-4 w-4 shrink-0 animate-spin text-primary-500"
          viewBox="0 0 24 24"
          fill="none"
          aria-hidden="true"
        >
          <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
          <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 0 1 8-8v4a4 4 0 0 0-4 4H4Z" />
        </svg>
        <svg
          v-else
          class="h-4 w-4 shrink-0 text-emerald-500"
          viewBox="0 0 20 20"
          fill="currentColor"
          aria-hidden="true"
        >
          <path
            fill-rule="evenodd"
            d="M10 18a8 8 0 1 0 0-16 8 8 0 0 0 0 16Zm3.857-9.809a.75.75 0 0 0-1.214-.882l-3.483 4.79-1.88-1.88a.75.75 0 1 1-1.06 1.061l2.5 2.5a.75.75 0 0 0 1.137-.089l4-5.5Z"
            clip-rule="evenodd"
          />
        </svg>
        <span class="truncate text-sm font-medium text-slate-900 dark:text-slate-100">{{ headline }}</span>
      </div>

      <span v-if="progress.total" class="shrink-0 text-xs text-slate-500 dark:text-slate-400">
        第 {{ Math.min(progress.index + 1, progress.total) }} / {{ progress.total }} 章
      </span>
    </div>

    <ol class="mt-3 flex items-center gap-1.5" aria-label="PRD 生成阶段">
      <li v-for="(step, index) in STEPS" :key="step.id" class="flex flex-1 items-center gap-1.5">
        <span
          class="shrink-0 rounded-full px-2.5 py-1 text-[11px] font-medium transition"
          :class="chipClass(index)"
        >
          {{ step.label }}
        </span>
        <span
          v-if="index < STEPS.length - 1"
          class="h-px flex-1"
          :class="index < currentIndex ? 'bg-primary-300' : 'bg-slate-200 dark:bg-slate-700'"
        ></span>
      </li>
    </ol>

    <div v-if="progress.total" class="mt-3 h-1.5 w-full overflow-hidden rounded-full bg-slate-100 dark:bg-slate-800">
      <div
        class="h-full rounded-full bg-gradient-to-r from-primary-400 to-primary-600 transition-all duration-300"
        :style="{ width: `${progressPercent}%` }"
      ></div>
    </div>

    <!-- 改写是一次「推翻重来」，必须让用户知道原因，否则会以为文档出了故障 -->
    <p
      v-if="progress.stage === 'rewriting' && progress.note"
      class="mt-3 flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs leading-relaxed text-amber-700 dark:border-amber-900/50 dark:bg-amber-950/40 dark:text-amber-300"
    >
      <svg class="mt-0.5 h-3.5 w-3.5 shrink-0" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
        <path
          fill-rule="evenodd"
          d="M8.485 2.495c.673-1.167 2.357-1.167 3.03 0l6.28 10.875c.673 1.167-.17 2.625-1.516 2.625H3.72c-1.347 0-2.189-1.458-1.515-2.625L8.485 2.495ZM10 6a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5A.75.75 0 0 1 10 6Zm0 8a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z"
          clip-rule="evenodd"
        />
      </svg>
      <span>本章审核未通过，正在重写：{{ progress.note }}</span>
    </p>
  </div>
</template>
