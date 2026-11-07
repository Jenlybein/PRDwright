<script setup lang="ts">
import { computed, ref, useId } from 'vue'

import AppButton from '@/components/ui/AppButton.vue'
import type { ReviewConclusion } from '@/types'

/**
 * 审核结论面板（四态：通过 / 有问题 / 审核失败 / 未审核）。
 *
 * 位置由 `ReviewStage` 决定：待确认清单**之下**、正文**之上**——先看「还差什么」，
 * 再看「审出了什么」，最后才是正文本身。
 *
 * ## 数据从哪来
 *
 * `review` 是**当前版本**那一条结论（后端随文档列表返回），所以刷新、换设备之后
 * 面板照旧在，显示的还是同一条。**没有结论也渲染**：一份从未审过的文档同样需要
 * 「未审核」这个状态，否则「没有面板」会被读成「没问题」。
 *
 * `failed` 是「本次会话里最近一次审核没跑成」——审核失败不写结论（没有结论 ≠ 通过），
 * 所以库里没有这条记录，只能由调用方按亲眼所见的那次失败告诉我们。
 *
 * ## 为什么动作只在折叠头、且只有一个
 *
 * 「重新审核」是这份结论上唯一可做的动作（修复归另一票，本版不渲染任何按钮）。
 * 「有问题」时它**不可点**：先修复再重审，顺序反了只会得到同一条结论、白烧一次调用。
 *
 * 收起时留下的那一行必须仍然能回答「这份文档能不能交付」：结论徽标 + 问题条数。
 * 展开状态是**纯组件内状态**：与待确认清单同口径，界面折叠态没有持久化先例。
 */
const props = defineProps<{
  /** 当前版本的结论；`null` = 这一版没审过或没审成（**不等于通过**） */
  review: ReviewConclusion | null
  /** 本次会话里最近一次审核是否没跑成（失败不写结论，只能由调用方告知） */
  failed?: boolean
}>()

const emit = defineEmits<{
  /** 请求重新审核这一份（只审不改，调用方起 `review` 任务） */
  rereview: []
}>()

/**
 * 面板的四种状态。
 *
 * 判定顺序不能换：**失败优先于旧结论**。重审失败时库里仍留着上一次那条（它描述的是
 * 同一份正文），但「本次重审没跑成」才是用户此刻要知道的事——把它盖成「通过」，
 * 等于用一条旧记录回答一个新问题。
 */
type ReviewState = 'pass' | 'revise' | 'failed' | 'unreviewed'

const STATE_LABELS: Record<ReviewState, string> = {
  pass: '通过',
  revise: '有问题',
  failed: '审核失败',
  unreviewed: '未审核',
}

const STATE_CLASSES: Record<ReviewState, string> = {
  pass: 'bg-emerald-50 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300',
  revise: 'bg-amber-50 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300',
  failed: 'bg-red-50 text-red-700 dark:bg-red-950/40 dark:text-red-300',
  unreviewed: 'bg-surface-2 text-muted',
}

const state = computed<ReviewState>(() => {
  if (props.failed) return 'failed'
  const verdict = props.review?.verdict ?? ''
  if (verdict === 'pass') return 'pass'
  if (verdict === 'revise') return 'revise'
  // 判定为空串（没审成）或没有结论：都不算通过
  return 'unreviewed'
})

const stateLabel = computed(() => STATE_LABELS[state.value])
const stateClass = computed(() => STATE_CLASSES[state.value])

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
const issues = computed(() => props.review?.issues ?? [])
const issueCount = computed(() => issues.value.length)

/** 结论的一句话总结；「审核失败 / 未审核」时没有可展示的东西 */
const summary = computed(() => props.review?.summary ?? '')

/** 本次是否真的改写过（重审恒为 false——只审不改） */
const rewritten = computed(() => props.review?.rewritten === true)

/** 生成失败的章节（PRD 并行链路才会非空） */
const failedSections = computed(() => props.review?.failedSections ?? [])

/** 「有问题」时不可点：先按结论修复，再回来确认 */
const canRereview = computed(() => state.value !== 'revise')

const rereviewHint = computed(() =>
  canRereview.value
    ? '按当前正文重新审一遍：只更新结论，不动正文、不新增版本'
    : '这份文档还有未处理的问题——先按结论修复，再重新审核',
)
</script>

<template>
  <section class="rounded-2xl border border-line bg-surface shadow-sm">
    <!--
      折叠头一行装两件事：左边整块是「展开 / 收起」开关（原生可 Tab、可回车/空格），
      右边是这份结论上唯一的动作。

      ⚠️ 两个 button **不能嵌套**（HTML 不允许、读屏也读不清）：因此外层是 div，
      开关占满剩余宽度，动作按钮独立一枚。开关仍是这一行里的**第一个** button。
    -->
    <div class="flex flex-wrap items-center gap-2 rounded-2xl px-4 py-3">
      <button
        type="button"
        class="flex min-w-0 flex-1 flex-wrap items-center gap-2 rounded-control text-left transition-colors hover:bg-surface-2 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent-soft-line"
        :aria-expanded="expanded"
        :aria-controls="bodyId"
        @click="expanded = !expanded"
      >
        <h3 class="text-sm font-semibold text-fg">审核结论</h3>
        <span class="rounded-full px-2 py-0.5 text-[11px] font-medium" :class="stateClass">
          {{ stateLabel }}
        </span>
        <span
          v-if="issueCount"
          class="rounded-full bg-surface-2 px-2 py-0.5 text-[11px] font-medium text-muted"
        >
          {{ issueCount }} 个待处理问题
        </span>
        <span
          v-if="rewritten"
          class="rounded-full bg-primary-50 px-2 py-0.5 text-[11px] font-medium text-primary-700 dark:bg-primary-950/40 dark:text-primary-300"
        >
          已改写一次
        </span>
        <svg
          class="h-4 w-4 shrink-0 text-subtle transition-transform"
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

      <!-- 只审不改：正文与版本都不变，因此这个动作随时可以再来一次（「有问题」除外）。
           样式走统一按钮件 `AppButton`：这里不手写一套描边样式，否则暗色主题又多一处。 -->
      <AppButton
        variant="secondary"
        size="sm"
        class="shrink-0"
        :disabled="!canRereview"
        :title="rereviewHint"
        @click="emit('rereview')"
      >
        重新审核
      </AppButton>
    </div>

    <!--
      用 `v-if` 而不是 `v-show`：收起时干脆不渲染。
      理由是测试与读屏都更干净——`v-show` 只改 display，隐藏文本仍会被 `textContent`
      读到（「到底收起了没有」只能靠 `isVisible()` 判，而它在 jsdom 下对这个元素并不可靠）。
      这里的内容就几条 `<li>`，重建成本可以忽略。
    -->
    <div v-if="expanded" :id="bodyId" class="px-4 pb-4">
      <!-- 没审成：把「为什么没有结论」说清楚，别让空白被读成「没问题」 -->
      <p v-if="state === 'failed'" class="text-xs leading-relaxed text-red-700 dark:text-red-300">
        这次审核没有跑成，因此<strong class="font-semibold">没有结论</strong>——它不算通过。
        可以直接点「重新审核」再试一次。
      </p>
      <p v-else-if="state === 'unreviewed'" class="text-xs leading-relaxed text-subtle">
        这一版还没有审核过。点「重新审核」可以补一次（只审不改，正文与版本都不会变）。
      </p>

      <p v-if="summary" class="text-xs leading-relaxed text-subtle">{{ summary }}</p>

      <ul v-if="issues.length" class="mt-3 space-y-1.5">
        <li
          v-for="(issue, index) in issues"
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

      <!-- 「有问题」时把这个动作不可点的原因写在面板里：只靠 title 太隐蔽 -->
      <p
        v-if="state === 'revise'"
        class="mt-3 text-xs leading-relaxed text-amber-700 dark:text-amber-300"
      >
        还有 {{ issueCount }} 个未处理的问题。把它们修好之后再来重新审核——现在就审，只会
        得到同一条结论。
      </p>

      <p
        v-if="failedSections.length"
        class="mt-3 text-xs leading-relaxed text-amber-700 dark:text-amber-300"
      >
        本次有 {{ failedSections.length }} 章生成失败：{{
          failedSections.map((item) => item.title).join('、')
        }}。可以重新生成补齐。
      </p>
    </div>
  </section>
</template>
