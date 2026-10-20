<script setup lang="ts">
import { computed } from 'vue'

import type { PrdBoardState, PrdSectionCard, PrdSectionPhase } from '@/types'

/**
 * PRD 三车道并行的「管道看板」。
 *
 * 整体**只有两行**：第一行是车道名 + 计数，第二行是该车道里的章节号。
 * 不写章节名、不做时间轴——空间留给正在发生的并行状态：
 *
 * ```text
 * 生成 1     审核 2     改写 1     定稿 0/6
 * ④          ②③       ①
 * ```
 *
 * 一个章节同一时刻只在一个车道；失败的章节停在其失败的车道，序号后面加 `!`。
 *
 * `withReview` 为 false（快捷流程：只撰写、不审不改）时**只剩「生成 / 定稿」两条车道**。
 * 那两条永远是 0 的列不是「还没跑到」，而是「这次根本不跑」——留着只会让人以为
 * 生成卡在审核前面。
 */
// ⚠️ 布尔 prop 在**未传**时会被 Vue 自动转成 `false`，因此必须显式给默认值 true：
// 否则「没传」等于「不复核」，完整流程的四车道会被悄悄砍成两条。
const props = withDefaults(
  defineProps<{
    board: PrdBoardState
    /** 本次生成是否带「审核 / 改写」；为 false 时只剩生成与定稿两条车道 */
    withReview?: boolean
  }>(),
  { withReview: true },
)

const CIRCLED = ['①', '②', '③', '④', '⑤', '⑥', '⑦', '⑧', '⑨', '⑩']

function chapterLabel(card: PrdSectionCard): string {
  return CIRCLED[card.index] ?? String(card.index + 1)
}

function byPhase(phase: PrdSectionPhase): PrdSectionCard[] {
  return props.board.sections.filter((section) => section.phase === phase)
}

const writing = computed(() => byPhase('writing'))
const reviewing = computed(() => byPhase('reviewing'))
const rewriting = computed(() => byPhase('rewriting'))
const doneCount = computed(
  () => props.board.sections.filter((section) => section.phase === 'done' && !section.failed).length,
)
const total = computed(() => props.board.total || props.board.sections.length)

const lanes = computed(() => {
  const writingLane = {
    id: 'writing',
    label: '生成',
    count: String(writing.value.length),
    items: writing.value,
    tone: 'text-sky-600 dark:text-sky-300',
  }
  const reviewLanes = [
    {
      id: 'reviewing',
      label: '审核',
      count: String(reviewing.value.length),
      items: reviewing.value,
      tone: 'text-violet-600 dark:text-violet-300',
    },
    {
      id: 'rewriting',
      label: '改写',
      count: String(rewriting.value.length),
      items: rewriting.value,
      tone: 'text-amber-600 dark:text-amber-300',
    },
  ]
  const doneLane = {
    id: 'done',
    label: '定稿',
    count: `${doneCount.value}/${total.value}`,
    items: [] as PrdSectionCard[],
    tone: 'text-emerald-600 dark:text-emerald-300',
  }
  // 不复核时只留首尾两条：中间两条恒为 0，画出来是假地图
  if (props.withReview === false) return [writingLane, doneLane]
  return [writingLane, ...reviewLanes, doneLane]
})

/** 车道数决定网格列数（Tailwind 要看到完整类名，因此不用拼接） */
const gridClass = computed(() =>
  lanes.value.length === 2 ? 'grid-cols-2' : 'grid-cols-4',
)

function laneAria(label: string, items: readonly PrdSectionCard[]): string {
  if (!items.length) return `${label}：无`
  return `${label}：${items.map((item) => `第 ${item.index + 1} 章`).join('、')}`
}
</script>

<template>
  <div
    class="rounded-2xl border border-line bg-surface px-4 py-3 shadow-sm"
    aria-label="PRD 生成管道"
  >
    <div class="grid gap-x-2 gap-y-1.5" :class="gridClass">
      <!-- 第一行：车道名 + 计数 -->
      <div
        v-for="lane in lanes"
        :key="`head-${lane.id}`"
        class="flex items-baseline gap-1 text-[11px]"
      >
        <span class="font-medium text-subtle">{{ lane.label }}</span>
        <span class="font-semibold tabular-nums" :class="lane.tone">{{ lane.count }}</span>
      </div>

      <!-- 第二行：该车道里的章节号 -->
      <div
        v-for="lane in lanes"
        :key="`body-${lane.id}`"
        class="flex min-h-[18px] items-center gap-1 text-[13px] font-medium leading-none"
        :class="lane.tone"
        :aria-label="laneAria(lane.label, lane.items)"
      >
        <template v-if="lane.items.length">
          <span
            v-for="card in lane.items"
            :key="card.sectionId"
            :class="card.failed ? 'text-red-600 dark:text-red-400' : ''"
            :title="card.failed ? card.note || '生成失败' : card.sectionTitle"
          >
            {{ chapterLabel(card) }}{{ card.failed ? '!' : '' }}
          </span>
        </template>
        <span v-else class="text-subtle">—</span>
      </div>
    </div>
  </div>
</template>
