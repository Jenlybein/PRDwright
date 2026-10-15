<script setup lang="ts">
import { computed } from 'vue'

import type { PendingImportance, PendingItem } from '@/types'
import { sortPendingItems } from '@/utils/pendingItems'

/**
 * 待确认清单（只读展示）。
 *
 * 一次生成产出的第一类元数据：由后端在剥离正文标记块时一并算出、随正文落库，
 * 前端只负责把它陈列出来，**不解析、不编辑、不重新计算**。它与正文同源、同一次收尾写入，
 * 因此「换了正文却忘了重算清单」没有结构落脚点——这正是把它做成「后端产出、前端只读」的原因。
 *
 * 设计口径：
 * - 中性表面（`bg-surface-2`）：它描述的是「这份文档还差哪些问题没敲定」，属于内容本身，
 *   不是报错（不用 warning / danger 的暖色），也不是「对文档做的操作」（不进底栏）。
 * - 等级徽标同样沿用中性口径，**用强弱而不是颜色-gradient 表达优先级**：P0 用主色、
 *   P1 用次强调色、P2 用弱化色。这样「越重要越显眼」成立，又不会把内容面板读成告警区。
 * - 序号用位置下标（1、2、3…）而不是后端 `id`：`id` 是 `p-1` / `overview-1` 这种
 *   机器标识，给用户看反而不直观；它只在 `:key` 里用来保证渲染稳定。
 * - 顺序由 `sortPendingItems` 保证是 P0 → P1 → P2，即**序号越小越要紧**；
 *   这点要让用户看得懂，所以面板副标题明确写出排列顺序含义。
 * - 章节 / 拍板人均为空时不渲染对应片段，避免露出「涉及章节：」「拍板人：」这种空标签。
 */
const props = defineProps<{
  /** 待确认问题列表；调用方保证非空（空列表时连面板都不挂） */
  items: PendingItem[]
}>()

/** 重要性等级 → 徽标样式与说明文案。用强弱分级，不用告警色。 */
const LEVEL_META: Record<
  PendingImportance,
  { badge: string; hint: string }
> = {
  P0: {
    badge:
      'bg-primary-50 text-primary-700 ring-primary-200 dark:bg-primary-950/40 dark:text-primary-300 dark:ring-primary-800',
    hint: '最要紧',
  },
  P1: {
    badge: 'bg-accent-soft text-accent',
    hint: '次之',
  },
  P2: {
    badge: 'bg-surface text-subtle ring-line',
    hint: '可后置',
  },
}

/** 后端已排好序，这里再兜一次底：确保渲染顺序始终是从最重要到最次要。 */
const sortedItems = computed(() => sortPendingItems(props.items))

/** 各级数量，用于头部一行说明（例如「P0 2 · P1 3」）。 */
const counts = computed(() => {
  const result: Record<PendingImportance, number> = { P0: 0, P1: 0, P2: 0 }
  for (const item of sortedItems.value) {
    const level: PendingImportance = item.importance ?? 'P1'
    result[level] += 1
  }
  return result
})

/** 哪些等级实际存在（按从重到轻），避免展示一堆 0。 */
const presentLevels = computed(
  () => (['P0', 'P1', 'P2'] as PendingImportance[]).filter((level) => counts.value[level] > 0),
)

/** 取某等级的样式配置；未知等级按 P1 兜底。 */
function metaOf(level: PendingImportance) {
  return LEVEL_META[level] ?? LEVEL_META.P1
}
</script>

<template>
  <section
    class="rounded-xl border border-line bg-surface-2 px-4 py-3"
    aria-label="待确认问题"
  >
    <div class="flex flex-wrap items-baseline gap-x-2 gap-y-1">
      <h3 class="text-sm font-medium text-fg">
        待确认问题（{{ sortedItems.length }}）
      </h3>
      <!-- 按等级统计：让用户一眼看出「要紧的有几条」 -->
      <p v-if="presentLevels.length" class="text-xs text-subtle">
        <span v-for="(level, index) in presentLevels" :key="level">
          {{ level }} {{ counts[level] }}{{ level === 'P0' ? '（最要紧）' : '' }}
          <span v-if="index < presentLevels.length - 1" class="text-subtle">· </span>
        </span>
      </p>
    </div>
    <p class="mt-0.5 text-xs text-subtle">
      按重要性从高到低排列，越靠前的问题越先问到你。
    </p>

    <ul class="mt-2 divide-y divide-line">
      <li
        v-for="(item, index) in sortedItems"
        :key="item.id"
        class="flex gap-3 py-2 first:pt-0"
      >
        <!-- 序号徽标：位置下标，纯展示 -->
        <span
          class="mt-0.5 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-accent-soft text-[11px] font-medium text-accent"
          aria-hidden="true"
        >
          {{ index + 1 }}
        </span>

        <div class="min-w-0 flex-1">
          <div class="flex flex-wrap items-center gap-2">
            <!-- 重要性等级徽标 -->
            <span
              class="shrink-0 rounded-full px-1.5 py-0.5 text-[11px] font-semibold ring-1"
              :class="metaOf(item.importance).badge"
              :title="`${item.importance}：${metaOf(item.importance).hint}`"
            >
              {{ item.importance }}
            </span>
            <p class="min-w-0 flex-1 text-sm leading-relaxed text-fg">{{ item.question }}</p>
          </div>

          <p
            v-if="item.sections.length || item.decider"
            class="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-subtle"
          >
            <span v-if="item.sections.length">涉及章节：{{ item.sections.join('、') }}</span>
            <span v-if="item.sections.length &amp;&amp; item.decider" aria-hidden="true">·</span>
            <span v-if="item.decider">拍板人：{{ item.decider }}</span>
          </p>
        </div>
      </li>
    </ul>
  </section>
</template>
