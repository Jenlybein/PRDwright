<script setup lang="ts">
import { computed } from 'vue'

import type { DocPhase } from '@/types'

/**
 * 下游文档（接口文档 / 提示词）生成中的阶段条。
 *
 * 与 `PrdProgress` 的管道看板不同：这两份产物是整篇生成、只审一次，没有章节维度，
 * 因此只展示四个阶段胶囊：生成 → 审核 → 改写 → 完成。
 */
const props = defineProps<{ phase: DocPhase }>()

const STEPS: { id: DocPhase; label: string }[] = [
  { id: 'writing', label: '生成' },
  { id: 'reviewing', label: '审核' },
  { id: 'rewriting', label: '改写' },
  { id: 'done', label: '完成' },
]

const ORDER: Record<DocPhase, number> = {
  idle: -1,
  writing: 0,
  reviewing: 1,
  rewriting: 2,
  done: 3,
}

const currentIndex = computed(() => ORDER[props.phase])

function chipClass(stepIndex: number): string {
  if (stepIndex > currentIndex.value) return 'bg-surface-2 text-subtle'
  if (stepIndex === currentIndex.value)
    return 'bg-primary-500 text-white ring-4 ring-primary-100 dark:ring-primary-900/50'
  return 'bg-primary-100 text-primary-700 dark:bg-primary-900/60 dark:text-primary-300'
}
</script>

<template>
  <div class="rounded-2xl border border-line bg-surface p-3 shadow-sm">
    <ol class="flex items-center gap-1.5" aria-label="文档生成阶段">
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
          :class="index < currentIndex ? 'bg-primary-300' : 'bg-line'"
        ></span>
      </li>
    </ol>
  </div>
</template>
