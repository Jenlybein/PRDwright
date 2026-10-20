<script setup lang="ts">
import { computed } from 'vue'

import type { DocPhase } from '@/types'

/**
 * 下游文档（接口文档 / 提示词）生成中的阶段条。
 *
 * 与 `PrdProgress` 的管道看板不同：这两份产物是整篇生成、只审一次，没有章节维度，
 * 因此只展示四个阶段胶囊：生成 → 审核 → 改写 → 完成。
 *
 * `withReview` 为 false（快捷流程）时**只剩「生成 → 完成」两格**：那两格永远走不到，
 * 画在地图上的路走不通，比不画更糟——用户会以为「它还没审到」而不是「这次不审」。
 */
// ⚠️ 必须显式给默认值：布尔 prop 在**未传**时会被 Vue 自动转成 `false`，
// 于是「没传」等于「关掉复核」，完整流程会被悄悄降级。
const props = withDefaults(
  defineProps<{
    phase: DocPhase
    /** 本次生成是否带「审核 / 改写」；为 false 时只画「生成 → 完成」 */
    withReview?: boolean
  }>(),
  { withReview: true },
)

const STEPS: { id: DocPhase; label: string }[] = [
  { id: 'writing', label: '生成' },
  { id: 'reviewing', label: '审核' },
  { id: 'rewriting', label: '改写' },
  { id: 'done', label: '完成' },
]

/** 带复核时的下标 */
const ORDER: Record<DocPhase, number> = {
  idle: -1,
  writing: 0,
  reviewing: 1,
  rewriting: 2,
  done: 3,
}

/**
 * 不带复核时的下标。
 *
 * `reviewing` / `rewriting` 理论上不会出现（后端都不跑），但仍然显式映射成「生成」
 * 而不是留空：万一哪天有别的路径塞了一个这类阶段进来，阶段条会显示「还在生成」，
 * 而不是把「完成」点亮——后者是**假信号**，比不亮更糟。
 */
const ORDER_NO_REVIEW: Record<DocPhase, number> = {
  idle: -1,
  writing: 0,
  reviewing: 0,
  rewriting: 0,
  done: 1,
}

const steps = computed(() =>
  props.withReview === false
    ? [
        { id: 'writing' as DocPhase, label: '生成' },
        { id: 'done' as DocPhase, label: '完成' },
      ]
    : STEPS,
)

const currentIndex = computed(() =>
  props.withReview === false ? ORDER_NO_REVIEW[props.phase] : ORDER[props.phase],
)

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
      <li v-for="(step, index) in steps" :key="step.id" class="flex flex-1 items-center gap-1.5">
        <span
          class="shrink-0 rounded-full px-2.5 py-1 text-[11px] font-medium transition"
          :class="chipClass(index)"
        >
          {{ step.label }}
        </span>
        <span
          v-if="index < steps.length - 1"
          class="h-px flex-1"
          :class="index < currentIndex ? 'bg-primary-300' : 'bg-line'"
        ></span>
      </li>
    </ol>
  </div>
</template>
