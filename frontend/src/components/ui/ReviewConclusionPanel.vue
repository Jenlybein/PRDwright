<script setup lang="ts">
import { computed, ref, useId } from 'vue'

import type { ReviewConclusion } from '@/types'

/**
 * 审核结论面板。
 *
 * 位置由 `ReviewStage` 决定：待确认清单**之下**、正文**之上**——先看「还差什么」，
 * 再看「审出了什么」，最后才是正文本身。
 *
 * 结论**不持久化**：这份数据只在本次生成的内存里，刷新后整个面板不再出现。
 * 因此这里不提供任何「保存/重审」按钮——它是一份回执，不是可操作的对象。
 *
 * ## 默认收起，但结论始终可见
 *
 * 收起后留下的那一行必须仍然能回答「这次审出了什么」：**结论徽标**、是否改写过、
 * 有几个待处理问题。于是「被自动收起」不会变成「你错过了问题」——想细看再展开。
 *
 * 展开状态是**纯组件内状态**：与待确认清单同口径（那份面板的展开态也不进快照），
 * 全项目只有主题用 localStorage，界面折叠态没有持久化先例。
 */
const props = defineProps<{ review: ReviewConclusion }>()

const SEVERITY_LABELS: Record<string, string> = {
  high: '高',
  medium: '中',
  low: '低',
}

function severityLabel(value: string): string {
  return SEVERITY_LABELS[value] ?? value
}

const expanded = ref(false)

/** 折叠体的 id：`aria-controls` 要指向它，用 `useId()` 避免同页多份时撞号 */
const bodyId = useId()

/** 收起时也要看得到的计数：没有它，收起就等于把问题藏了 */
const issueCount = computed(() => props.review.issues.length)
</script>

<template>
  <section class="rounded-2xl border border-line bg-surface shadow-sm">
    <!-- 整行是按钮：原生可 Tab、可回车/空格，不需要自己写键盘事件 -->
    <button
      type="button"
      class="flex w-full flex-wrap items-center gap-2 rounded-2xl px-4 py-3 text-left transition-colors hover:bg-surface-2 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent-soft-line"
      :aria-expanded="expanded"
      :aria-controls="bodyId"
      @click="expanded = !expanded"
    >
      <h3 class="text-sm font-semibold text-fg">审核结论</h3>
      <span
        class="rounded-full px-2 py-0.5 text-[11px] font-medium"
        :class="
          review.verdict === 'pass'
            ? 'bg-emerald-50 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300'
            : 'bg-amber-50 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300'
        "
      >
        {{ review.verdict === 'pass' ? '通过' : '有问题' }}
      </span>
      <span
        v-if="issueCount"
        class="rounded-full bg-surface-2 px-2 py-0.5 text-[11px] font-medium text-muted"
      >
        {{ issueCount }} 个待处理问题
      </span>
      <span
        v-if="review.rewritten"
        class="rounded-full bg-primary-50 px-2 py-0.5 text-[11px] font-medium text-primary-700 dark:bg-primary-950/40 dark:text-primary-300"
      >
        已改写一次
      </span>
      <svg
        class="ml-auto h-4 w-4 shrink-0 text-subtle transition-transform"
        :class="expanded ? 'rotate-180' : ''"
        viewBox="0 0 20 20"
        fill="currentColor"
        aria-hidden="true"
      >
        <path
          fill-rule="evenodd"
          d="M5.23 7.21a.75.75 0 0 1 1.06.02L10 11.17l3.71-3.94a.75.75 0 1 1 1.08 1.04l-4.25 4.5a.75.75 0 0 1-1.08 0l-4.25-4.5a.75.75 0 0 1 .02-1.06Z"
          clip-rule="evenodd"
        />
      </svg>
    </button>

    <!--
      用 `v-if` 而不是 `v-show`：收起时干脆不渲染。
      理由是测试与读屏都更干净——`v-show` 只改 display，隐藏文本仍会被 `textContent`
      读到（「到底收起了没有」只能靠 `isVisible()` 判，而它在 jsdom 下对这个元素并不可靠）。
      这里的内容就几条 `<li>`，重建成本可以忽略。
    -->
    <div v-if="expanded" :id="bodyId" class="px-4 pb-4">
      <p v-if="review.summary" class="text-xs leading-relaxed text-subtle">
        {{ review.summary }}
      </p>

      <ul v-if="review.issues.length" class="mt-3 space-y-1.5">
        <li
          v-for="(issue, index) in review.issues"
          :key="index"
          class="flex items-start gap-2 text-xs leading-relaxed text-muted"
        >
          <span
            class="mt-0.5 shrink-0 rounded px-1 py-0.5 text-[10px] font-medium"
            :class="
              issue.severity === 'high'
                ? 'bg-red-50 text-red-700 dark:bg-red-950/40 dark:text-red-300'
                : issue.severity === 'medium'
                  ? 'bg-amber-50 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300'
                  : 'bg-surface-2 text-subtle'
            "
          >
            {{ severityLabel(issue.severity) }}
          </span>
          <span>
            <template v-if="issue.sectionTitle">{{ issue.sectionTitle }}：</template>
            {{ issue.detail }}
          </span>
        </li>
      </ul>

      <p
        v-if="review.failedSections.length"
        class="mt-3 text-xs leading-relaxed text-amber-700 dark:text-amber-300"
      >
        本次有 {{ review.failedSections.length }} 章生成失败：{{
          review.failedSections.map((item) => item.title).join('、')
        }}。可以重新生成补齐。
      </p>
    </div>
  </section>
</template>
