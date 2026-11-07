<script setup lang="ts">
import { computed, ref, useId } from 'vue'

import AppButton from '@/components/ui/AppButton.vue'
import type { ReviewConclusion, ReviewIssue } from '@/types'
import { relativeTime } from '@/utils/time'
import {
  DEFECT_CATEGORIES,
  DEFECT_CATEGORY_LABELS,
  UNCATEGORIZED_LABEL,
} from '@/utils/review'

/**
 * **文档缺陷**面板（四态：通过 / 有问题 / 审核失败 / 未审核）。
 *
 * 位置由 `ReviewStage` 决定：待确认清单**之下**、正文**之上**——先看「还差什么」，
 * 再看「审出了什么」，最后才是正文本身。
 *
 * ## 为什么标题是「文档缺陷」而不是「审核结论」
 *
 * 审核只报**缺陷**——可验证为错的地方（编造 / 矛盾 / 结构不符 / 空泛 / 推断未标注 /
 * 清单漏登记）；「缺什么、该怎么定」是**未决**，归待确认清单（ADR 0003 / C-R5）。
 * 标题照实说这件事，用户才不会把它读成「审核说这里少了什么」而与清单对照两遍。
 *
 * 折叠头给**分类计数**（`编造 1 · 结构 2 · 未分类 3`），收起时就能看出「有几条、哪一类」；
 * 展开按类分组、类内 `high` 在前——严重度决定「先修哪一条」，它由类别固定，不由模型升降。
 * **旧结论没有类别** → 归「未分类」，不推断、不重分类（不追溯历史）。
 *
 * ## 数据从哪来
 *
 * `review` 是**当前版本**那一条结论（后端随文档列表返回），所以刷新、换设备之后
 * 面板照旧在，显示的还是同一条。**没有结论也渲染**：一份从未审过的文档同样需要
 * 「未审核」这个状态，否则「没有面板」会被读成「没问题」。
 *
 * `failed` 是「这一版上一次审核试过但没跑成」——审核失败不写结论（没有结论 ≠ 通过），
 * 但**失败本身是落库事实**（后端 `review_failed_at`），因此刷新之后它仍在；面板据此
 * 显示「审核失败」与失败时间，而不是退化成「未审核」。
 *
 * ## 为什么失败态不展示 `review` 的内容
 *
 * 重审失败时库里仍留着上一次那条结论（它描述的是同一份正文，但**不是本次结果**）。
 * 把它逐条列在「审核失败」下面，读起来就是「这次审出来的问题」——那是最误导人的
 * 一种排法。失败态只讲「这次没跑成、可以重审」，旧结论一个字都不显示。
 *
 * ## 追溯：哪一版、什么时候审的
 *
 * 结论上带了审核时间（`reviewedAt`），文档列表还带着当前版本号（`versionNo`）：
 * 两样一起显示在折叠头，用户不必靠回忆「这条结论是什么时候的」。
 *
 * ## 为什么动作只在折叠头、且各守一种状态
 *
 * 这份结论上有两个动作，都在折叠头、「有问题」与「其余三态」各开一个：
 *
 * - **修复**（`review-fix`）：按这一版的问题重写整篇，由后端**追加新版本**。
 *   只有「有问题」时可点——没有问题时它没有依据，也就无从下手。
 * - **重新审核**（`review`）：只审不改、不追加版本。只有「审核失败 / 未审核 / 通过」时
 *   可点：有问题时先修复，顺序反了只会得到同一条结论、白烧一次调用。
 *
 * 两个按钮**恒在场**、不可点时置灰并用 `title` 说明原因（不把入口藏起来：藏起来
 * 等于让人猜「这里本来有没有东西」）。
 *
 * 收起时留下的那一行必须仍然能回答「这份文档能不能交付」：结论徽标 + 问题条数。
 * 展开状态是**纯组件内状态**：与待确认清单同口径，界面折叠态没有持久化先例。
 */
const props = defineProps<{
  /** 当前版本的结论；`null` = 这一版没审过或没审成（**不等于通过**） */
  review: ReviewConclusion | null
  /**
   * 这一版上一次审核试过但没跑成的时间（`null` = 没有失败记录）。
   *
   * 它是落库事实（后端 `document_versions.review_failed_at`），不是会话内状态：
   * 刷新、换设备之后仍然读得到，面板据此显示「审核失败（时间）」。
   */
  reviewFailedAt?: string | null
  /** 当前版本号（`DocumentRecord.versionNo`）；未知时不显示「第几版」 */
  versionNo?: number
}>()

const emit = defineEmits<{
  /** 请求按这条结论修复这一份（重写并追加版本，调用方起 `review_fix` 任务） */
  fix: []
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
  if (props.reviewFailedAt) return 'failed'
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

/**
 * 失败态是否还展示 `review` 的内容。
 *
 * 只有「通过 / 有问题」这两种状态是**本次看得出的事实**；失败态下 `review` 里那条
 * 是上一次的记录（失败不写结论，见 C-R1），它不属于这次结果。
 */
const showsReview = computed(() => state.value !== 'failed')

/** 收起时也要看得到的计数：没有它，收起就等于把问题藏了 */
const issues = computed(() => (showsReview.value ? props.review?.issues ?? [] : []))
const issueCount = computed(() => issues.value.length)

/**
 * 严重度排序权重。改写门槛只认 `high`（`REVIEW_REWRITE_MIN_SEVERITY`），所以「先看 high」
 * 与「先修哪一条」是同一件事。
 */
const SEVERITY_RANK: Record<string, number> = { high: 0, medium: 1, low: 2 }

/** 组内排序：`high` 在前，未知严重度垫底；同档保持结论里的原序（`sort` 是稳定的） */
function highFirst(a: ReviewIssue, b: ReviewIssue): number {
  return (SEVERITY_RANK[a.severity] ?? 9) - (SEVERITY_RANK[b.severity] ?? 9)
}

/** 一组同类缺陷（`category` 为空的是「未分类」组，排在最后） */
interface IssueGroup {
  /** 类别标识；未分类组的 key 是空串（模板里 `key` 需要非空时用 `||` 兜底） */
  key: string
  label: string
  issues: ReviewIssue[]
}

/**
 * 类别按固定顺序分桶，类内 `high` 在前。
 *
 * 只认契约里的六类：解析器已把不认识的字符串归一成 `null`，这里再兜一次底——分组
 * 的取值域是**有限的**，否则一条错别字就会在界面上多出一个分组。
 */
const issueGroups = computed<IssueGroup[]>(() => {
  const known = new Set<string>(DEFECT_CATEGORIES)
  const buckets = new Map<string, ReviewIssue[]>()
  for (const issue of issues.value) {
    const raw = issue.category
    const key = typeof raw === 'string' && known.has(raw) ? raw : ''
    const bucket = buckets.get(key)
    if (bucket) bucket.push(issue)
    else buckets.set(key, [issue])
  }

  const groups: IssueGroup[] = []
  for (const category of DEFECT_CATEGORIES) {
    const bucket = buckets.get(category)
    if (!bucket?.length) continue
    groups.push({
      key: category,
      label: DEFECT_CATEGORY_LABELS[category] ?? category,
      issues: [...bucket].sort(highFirst),
    })
  }
  const uncategorized = buckets.get('')
  if (uncategorized?.length) {
    groups.push({
      key: '',
      label: UNCATEGORIZED_LABEL,
      issues: [...uncategorized].sort(highFirst),
    })
  }
  return groups
})

/**
 * 折叠头的分类计数：`编造 1 · 结构 2 · 未分类 3`。
 *
 * 各项之和就是问题总数，因此不必再单独写一个「N 个待处理问题」——两个数字摆在一起
 * 只会让人核对一遍它们对不对。
 */
const categoryCounts = computed(() =>
  issueGroups.value.map((group) => `${group.label} ${group.issues.length}`).join(' · '),
)

/** 结论的一句话总结；「审核失败 / 未审核」时没有可展示的东西 */
const summary = computed(() => (showsReview.value ? props.review?.summary ?? '' : ''))

/** 本次是否真的改写过（重审恒为 false——只审不改） */
const rewritten = computed(() => showsReview.value && props.review?.rewritten === true)

/** 生成失败的章节（PRD 并行链路才会非空） */
const failedSections = computed(() =>
  showsReview.value ? props.review?.failedSections ?? [] : [],
)

/** 「第几版」：结论归属的版本号（未知时不显示，别猜一个） */
const versionLabel = computed(() =>
  typeof props.versionNo === 'number' && props.versionNo > 0 ? `第 ${props.versionNo} 版` : '',
)

/**
 * 「什么时候审的」：只对**本次读得到的那条结论**显示。
 *
 * 失败 / 未审核时 `reviewedAt` 为空或不属于本次结果——此时不显示时间，
 * 免得把上一次的审核时间读成「刚刚审过」。
 */
const reviewedLabel = computed(() => {
  if (!showsReview.value) return ''
  return relativeTime(props.review?.reviewedAt ?? '')
})

/**
 * 「什么时候失败的」：失败态专属。
 *
 * 失败不写结论，所以没有 `reviewedAt` 可显示；把失败时间摆在徽标旁边，用户才能回答
 * 「这次是刚试过、还是昨天试过」。时间来自落库的 `reviewFailedAt`，刷新后照旧。
 */
const failedLabel = computed(() =>
  state.value === 'failed' && props.reviewFailedAt ? relativeTime(props.reviewFailedAt) : '',
)

/** 「有问题」时不可点：先按结论修复，再回来确认 */
const canRereview = computed(() => state.value !== 'revise')

const rereviewHint = computed(() =>
  canRereview.value
    ? '按当前正文重新审一遍：只更新结论，不动正文、不新增版本'
    : '这份文档还有未处理的问题——先按结论修复，再重新审核',
)

/**
 * 「修复」只对「有问题」开放：没有问题时它没有依据（也就无从下手）。
 *
 * 与重新审核**互斥**：两者各自守着「有问题」与「其余三态」，同一时刻只有一个可点。
 * 这样用户不会面对「两个都能按、但只能选一个」的猜测。
 */
const canFix = computed(() => state.value === 'revise')

const fixHint = computed(() =>
  canFix.value
    ? '按这条结论重写整篇：追加一个新版本，并自动再重审一次（重审不再改写）'
    : '只有「有问题」的结论才能按结论修复',
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
        <h3 class="text-sm font-semibold text-fg">文档缺陷</h3>
        <span class="rounded-full px-2 py-0.5 text-[11px] font-medium" :class="stateClass">
          {{ stateLabel }}
        </span>
        <span
          v-if="issueCount"
          class="rounded-full bg-surface-2 px-2 py-0.5 text-[11px] font-medium text-muted"
          :title="`按类别统计：${categoryCounts}`"
        >
          {{ categoryCounts }}
        </span>
        <span
          v-if="rewritten"
          class="rounded-full bg-primary-50 px-2 py-0.5 text-[11px] font-medium text-primary-700 dark:bg-primary-950/40 dark:text-primary-300"
        >
          已改写一次
        </span>
        <!-- 追溯：这条结论属于哪一版、什么时候审的（收起时也看得到） -->
        <span v-if="versionLabel" class="text-[11px] text-subtle">{{ versionLabel }}</span>
        <span v-if="reviewedLabel" class="text-[11px] text-subtle">· 审于 {{ reviewedLabel }}</span>
        <!-- 失败也有时间：它落在版本行上（`review_failed_at`），刷新后照旧 -->
        <span v-if="failedLabel" class="text-[11px] text-subtle">· 失败于 {{ failedLabel }}</span>
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

      <!-- 修复：按这一版的问题重写整篇并**追加新版本**。它排在重审之前——「有问题」时
           它才是可点的那个，两个按钮的可用条件互斥，不会让用户猜该按哪个。 -->
      <AppButton
        variant="secondary"
        size="sm"
        class="shrink-0"
        :disabled="!canFix"
        :title="fixHint"
        @click="emit('fix')"
      >
        修复
      </AppButton>

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

      <!-- 展开：按类分组，类内 high 在前（严重度由类别固定，见 ADR 0003）。未分类组在最后。 -->
      <div v-if="issueGroups.length" class="mt-3 space-y-3">
        <section v-for="group in issueGroups" :key="group.key || 'uncategorized'">
          <h4 class="flex items-baseline gap-1.5 text-xs font-semibold text-fg">
            {{ group.label }}
            <span class="text-[11px] font-normal text-subtle">{{ group.issues.length }}</span>
          </h4>
          <ul class="mt-1.5 space-y-1.5">
            <li
              v-for="(issue, index) in group.issues"
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
        </section>
      </div>

      <!-- 「有问题」时把两个动作的分工写在面板里：只靠 title 太隐蔽 -->
      <p
        v-if="state === 'revise'"
        class="mt-3 text-xs leading-relaxed text-amber-700 dark:text-amber-300"
      >
        还有 {{ issueCount }} 个未处理的问题。点「修复」会按这条结论重写整篇并追加一个新版本；
        把它们修好之后再来重新审核——现在就审，只会得到同一条结论。
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
