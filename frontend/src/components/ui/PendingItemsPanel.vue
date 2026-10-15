<script setup lang="ts">
import type { PendingItem } from '@/types'

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
 * - 序号用位置下标（1、2、3…）而不是后端 `id`：`id` 是 `p-1` / `overview-1` 这种
 *   机器标识，给用户看反而不直观；它只在 `:key` 里用来保证渲染稳定。
 * - 章节 / 拍板人均为空时不渲染对应片段，避免露出「涉及章节：」「拍板人：」这种空标签。
 */
const props = defineProps<{
  /** 待确认问题列表；调用方保证非空（空列表时连面板都不挂） */
  items: PendingItem[]
}>()
</script>

<template>
  <section
    class="rounded-xl border border-line bg-surface-2 px-4 py-3"
    aria-label="待确认问题"
  >
    <h3 class="text-sm font-medium text-fg">
      待确认问题（{{ props.items.length }}）
    </h3>

    <ul class="mt-2 divide-y divide-line">
      <li
        v-for="(item, index) in props.items"
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
          <p class="text-sm leading-relaxed text-fg">{{ item.question }}</p>

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
