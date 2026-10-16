<script setup lang="ts">
import { computed, ref } from 'vue'

import type { PendingImportance, PendingItem } from '@/types'
import { normalizeImportance, sortPendingItems } from '@/utils/pendingItems'

/** 全部等级，从最要紧到最次要——计数芯片与副标题都按这个顺序排列。 */
const LEVELS: readonly PendingImportance[] = ['P0', 'P1', 'P2', 'P3', 'P4', 'P5', 'P6']

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
 * - 等级徽标同样沿用中性口径，**用强弱而不是颜色-gradient 表达优先级**：七档共用一个
 *   由「实心主色 → 浅主色 → 冷色 → 中性描边 → 近乎纯文本」的单调递减阶梯，
 *   越要紧的越显眼，又不会把内容面板读成告警区。
 * - 序号用位置下标（1、2、3…）而不是后端 `id`：`id` 是 `p-1` / `overview-1` 这种
 *   机器标识，给用户看反而不直观；它只在 `:key` 里用来保证渲染稳定。
 * - 顺序由 `sortPendingItems` 保证是 P0 → P6，即**序号越小越要紧**；
 *   这点要让用户看得懂，所以面板副标题明确写出排列顺序含义。
 * - 章节 / 拍板人均为空时不渲染对应片段，避免露出「涉及章节：」「拍板人：」这种空标签。
 * - **默认只铺开前 2 条**（`VISIBLE_LIMIT`），其余折起来由用户主动展开：清单可能长到十几条，
 *   而它是要和正文一起看的（面板就在正文之上），全铺开会把正文挤到屏幕外。折叠只是**展示口径**，
 *   不动数据：标题里的总数、后端算好的排序与等级都不变，展开状态也不进快照（纯会话内状态）。
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
      'bg-primary-600 text-on-accent ring-primary-600 dark:bg-primary-300 dark:text-primary-950 dark:ring-primary-300',
    hint: '最要紧',
  },
  P1: {
    badge:
      'bg-primary-100 text-primary-800 ring-primary-200 dark:bg-primary-900 dark:text-primary-100 dark:ring-primary-800',
    hint: '很要紧',
  },
  P2: {
    badge:
      'bg-primary-50 text-primary-700 ring-primary-200 dark:bg-primary-950/40 dark:text-primary-300 dark:ring-primary-800',
    hint: '偏要紧',
  },
  P3: {
    badge: 'bg-accent-soft text-accent ring-accent-soft-line',
    hint: '中等',
  },
  P4: {
    badge: 'bg-surface text-muted ring-line',
    hint: '可以后置',
  },
  P5: {
    badge: 'bg-surface text-subtle ring-line',
    hint: '比较次要',
  },
  P6: {
    badge: 'bg-surface text-subtle ring-line-strong',
    hint: '末位细节',
  },
}

/** 后端已排好序，这里再兜一次底：确保渲染顺序始终是从最重要到最次要。 */
const sortedItems = computed(() => sortPendingItems(props.items))

/** 折叠时最多铺开几条；超出的收起来，由用户主动展开 */
const VISIBLE_LIMIT = 2

/** 是否展开查看全部；初始收起——先给最要紧的前几条，需要时再展开 */
const expanded = ref(false)

/** 总条数是否超过折叠上限（不超过就不出现展开控件，避免一个点了没变化的按钮） */
const hasMore = computed(() => sortedItems.value.length > VISIBLE_LIMIT)

/** 实际渲染的条目：展开时全量，收起时只取前 VISIBLE_LIMIT 条 */
const visibleItems = computed(() =>
  expanded.value ? sortedItems.value : sortedItems.value.slice(0, VISIBLE_LIMIT),
)

/** 各级数量，用于标题旁的彩色计数芯片（例如「P0 ×2」「P3 ×1」）。 */
const counts = computed(() => {
  const result: Record<PendingImportance, number> = {
    P0: 0,
    P1: 0,
    P2: 0,
    P3: 0,
    P4: 0,
    P5: 0,
    P6: 0,
  }
  for (const item of sortedItems.value) {
    const level = normalizeImportance(item.importance)
    // 认不出等级的项不进任何一档：宁可不统计，也不替它挑一档
    if (level !== null) result[level] += 1
  }
  return result
})

/** 哪些等级实际存在（按从重到轻），避免展示一堆 0。 */
const presentLevels = computed(() => LEVELS.filter((level) => counts.value[level] > 0))

/** 取某等级的样式配置；认不出的等级按末档渲染，不替它挑一档。 */
function metaOf(level: PendingImportance) {
  return LEVEL_META[level] ?? LEVEL_META.P6
}
</script>

<template>
  <section
    class="rounded-xl border border-line bg-surface-2 px-4 py-3"
    aria-label="待确认问题"
  >
    <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
      <h3 class="text-sm font-medium text-fg">
        待确认问题（{{ sortedItems.length }}）
      </h3>
      <!-- 按等级统计：彩色计数芯片，与条目标上的等级徽标同款配色，一眼看出各档几条 -->
      <p v-if="presentLevels.length" class="flex flex-wrap items-center gap-1">
        <span
          v-for="level in presentLevels"
          :key="level"
          class="inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[11px] font-semibold ring-1"
          :class="metaOf(level).badge"
          :title="`${level}：${metaOf(level).hint}`"
        >
          {{ level }}
          <span class="font-normal opacity-75">×{{ counts[level] }}</span>
        </span>
      </p>
    </div>
    <p class="mt-0.5 text-xs text-subtle">
      按重要性从高到低排列，越靠前的问题越先问到你。
    </p>

    <ul class="mt-2 divide-y divide-line">
      <li
        v-for="(item, index) in visibleItems"
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

    <!-- 展开 / 收起：只在真的被折叠过时才出现。文案带上总数，用户知道展开后能拿到多少 -->
    <button
      v-if="hasMore"
      type="button"
      class="mt-2 w-full rounded-lg border border-line px-3 py-1.5 text-xs font-medium text-muted transition-colors hover:border-line-strong hover:text-fg focus:outline-none focus-visible:ring-2 focus-visible:ring-accent-soft-line"
      :aria-expanded="expanded"
      @click="expanded = !expanded"
    >
      {{ expanded ? '收起' : `展开全部 ${sortedItems.length} 条` }}
    </button>
  </section>
</template>
