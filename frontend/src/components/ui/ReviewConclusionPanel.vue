<script setup lang="ts">
import type { ReviewConclusion } from '@/types'

/**
 * 审核结论面板。
 *
 * 位置由 `ReviewStage` 决定：待确认清单**之下**、正文**之上**——先看「还差什么」，
 * 再看「审出了什么」，最后才是正文本身。
 *
 * 结论**不持久化**：这份数据只在本次生成的内存里，刷新后整个面板不再出现。
 * 因此这里不提供任何「保存/重审」按钮——它是一份回执，不是可操作的对象。
 */
defineProps<{ review: ReviewConclusion }>()

const SEVERITY_LABELS: Record<string, string> = {
  high: '高',
  medium: '中',
  low: '低',
}

function severityLabel(value: string): string {
  return SEVERITY_LABELS[value] ?? value
}
</script>

<template>
  <section class="rounded-2xl border border-line bg-surface p-4 shadow-sm">
    <div class="flex flex-wrap items-center gap-2">
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
        v-if="review.rewritten"
        class="rounded-full bg-primary-50 px-2 py-0.5 text-[11px] font-medium text-primary-700 dark:bg-primary-950/40 dark:text-primary-300"
      >
        已改写一次
      </span>
    </div>

    <p v-if="review.summary" class="mt-2 text-xs leading-relaxed text-subtle">
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
  </section>
</template>
