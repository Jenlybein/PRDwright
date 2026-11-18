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
 * ## 排版：三层信息，从上到下
 *
 * 收起时三行：标题 + 状态徽标（**总体结论**）→ 元信息小字（第几版 / 何时审 / 是否已改写）
 * → 类别计数一行弱文本（`共 N 条 · 各类几条`）。展开后每组一行标题（严重度图标 + 类别名
 * + 条数），每条缺陷两行：小字等宽定位在上、描述主体在下。
 *
 * **严重度显式标注（高 / 中 / 低）**：收起态给「高 N · 中 N · 低 N」统计，展开后每组标题
 * 与每条缺陷都带严重度徽标（文字 + 颜色双通道）。此前只在分组图标颜色里隐含严重度，
 * 用户反馈看不出来——严重度是「先修哪条」的直接依据，必须一眼可读、读屏可读。
 *
 * **前端不洗 Markdown**：`where` / `detail` 的行内标记由后端
 * `review_defects.normalize_issue` 统一抹成纯文本；前端再写一遍正则就是第二个真源，
 * 两边迟早对不上。面板按纯文本渲染（`font-mono` 只改变字体，不解析标记）。
 *
 * ## 三份产物共用同一个面板（票 #94 收口）
 *
 * PRD / 接口文档 / 提示词套件都走「整篇审一次、问题按类报」，结论都是**一版一条**：
 * 因此这里没有「逐章汇总」那种装配，也不再有「哪几章生成失败」这类分章遗留块——
 * 一条结论就是一个面板。`review.kind` 只回答「这是哪一份产物的结论」，不改变展示结构。
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
  /**
   * 有一支**修复**任务在途（票 #69）。
   *
   * 判据由上层按**任务种类**给出，与产物无关：后端判定顶替只看「同一个人 + 同一方案 +
   * 同一任务种类」，同一方案里两份修复会互相掐断。因此这里禁用的是「还能不能发起」，
   * 而不是「这一份的结论允不允许修」——两件事，两种禁用，别混成一条。
   *
   * 为 true 时两个动作都不可点，且**不挂说明文字**：灰的原因不在这一份的结论上。
   */
  fixInFlight?: boolean
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
  pass: 'bg-success-surface text-success-fg',
  revise: 'bg-warning-surface text-warning-fg',
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

/**
 * 分组标题上的那枚图标：它表达的是**严重度**，不是「这一类的图案」。
 *
 * 两个理由：类别名已经写在标题里，再画六种各不相同的图形只是装饰；而严重度才是
 * 「先看哪一组」的依据——high 两组用告警三角（`danger-fg`），medium 用信息圆
 * （`subtle`），一眼就能把「必须改」和「只报不改」分开。两者都是仓库既有的图标口径，
 * 不另起一套画风。
 */
const WARNING_ICON =
  'M8.485 2.495c.673-1.167 2.357-1.167 3.03 0l6.28 10.875c.673 1.167-.17 2.625-1.516 2.625H3.72c-1.347 0-2.189-1.458-1.515-2.625L8.485 2.495ZM10 6a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5A.75.75 0 0 1 10 6Zm0 8a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z'

const INFO_ICON =
  'M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0Zm-8-5a.75.75 0 0 1 .75.75v4.5a.75.75 0 0 1-1.5 0v-4.5A.75.75 0 0 1 10 5Zm0 10a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z'

/**
 * 严重度 → 中文标签与徽标样式（高 / 中 / 低）。
 *
 * 文字 + 颜色双通道：颜色让人一眼分档，文字保证色弱与读屏也能读出来。
 * 高=危险红、中=警示暖、低=中性灰；都不依赖单一颜色通道。
 */
type Severity = 'high' | 'medium' | 'low'
const SEVERITY_META: Record<Severity, { label: string; badge: string; dot: string }> = {
  high: {
    label: '高',
    badge: 'bg-danger-surface text-danger-fg ring-danger-line',
    dot: 'text-danger-fg',
  },
  medium: {
    label: '中',
    badge: 'bg-warning-surface text-warning-fg ring-warning-line',
    dot: 'text-warning-fg',
  },
  low: {
    label: '低',
    badge: 'bg-surface-2 text-muted ring-line',
    dot: 'text-muted',
  },
}

/** 把后端给的严重度字符串归一到三档；认不出的按「低」处理，不猜一个更重的档。 */
function severityOf(raw: string): Severity {
  if (raw === 'high') return 'high'
  if (raw === 'medium') return 'medium'
  return 'low'
}


/** 收起态的严重度统计：`高 2 · 中 1`（只列实际存在的档，从重到轻） */
const severityCountsText = computed(() => {
  const counts: Record<Severity, number> = { high: 0, medium: 0, low: 0 }
  for (const issue of issues.value) counts[severityOf(issue.severity)] += 1
  return (['high', 'medium', 'low'] as const)
    .filter((level) => counts[level] > 0)
    .map((level) => `${SEVERITY_META[level].label} ${counts[level]}`)
    .join(' · ')
})

/** 一组同类缺陷（`category` 为空的是「未分类」组，排在最后） */
interface IssueGroup {
  /** 类别标识；未分类组的 key 是空串（模板里 `key` 需要非空时用 `||` 兜底） */
  key: string
  label: string
  issues: ReviewIssue[]
  /** 组内是否以 high 打头（决定标题图标与条目左侧的强调线，见 `WARNING_ICON`） */
  high: boolean
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
    const sorted = [...bucket].sort(highFirst)
    groups.push({
      key: category,
      label: DEFECT_CATEGORY_LABELS[category] ?? category,
      issues: sorted,
      // 六类里前两类固定 high（后端 `CATEGORY_SEVERITY` 覆写），其余固定 medium；
      // 这里按组内首条的实际严重度取（未分类组混有旧口径条目时也能正确亮红灯）。
      high: sorted[0]?.severity === 'high',
    })
  }
  const uncategorized = buckets.get('')
  if (uncategorized?.length) {
    const sorted = [...uncategorized].sort(highFirst)
    groups.push({
      key: '',
      label: UNCATEGORIZED_LABEL,
      issues: sorted,
      high: sorted[0]?.severity === 'high',
    })
  }
  return groups
})


/** 本次是否真的改写过（重审恒为 false——只审不改） */
const rewritten = computed(() => showsReview.value && props.review?.rewritten === true)

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

/**
 * 元信息行：`第 2 版 · 审于 5 分钟前 · 已改写一次`。
 *
 * 这三样都是**追溯与噪声级**信息，收进标题下方同一行小字：不与类别计数同排，
 * 也不各占一枚 chip——否则「哪一版、什么时候审的、改没改过」会和「哪一类几条」
 * 一起抢第一眼的注意力。空的那一项直接不出现，不留分隔符。
 */
const metaText = computed(() => {
  const parts: string[] = []
  if (versionLabel.value) parts.push(versionLabel.value)
  if (reviewedLabel.value) parts.push(`审于 ${reviewedLabel.value}`)
  if (failedLabel.value) parts.push(`失败于 ${failedLabel.value}`)
  if (rewritten.value) parts.push('已改写一次')
  return parts.join(' · ')
})

/** 「有问题」时不可点：先按结论修复，再回来确认（修复在途时同样不可点，见 `canFix`） */
const canRereview = computed(() => state.value !== 'revise' && !props.fixInFlight)

const rereviewHint = computed(() => {
  if (props.fixInFlight) return undefined
  return canRereview.value
    ? '按当前正文重新审一遍：只更新结论，不动正文、不新增版本'
    : '这份文档还有未处理的问题——先按结论修复，再重新审核'
})

/**
 * 「修复」只对「有问题」开放：没有问题时它没有依据（也就无从下手）。
 *
 * 与重新审核**互斥**：两者各自守着「有问题」与「其余三态」，同一时刻只有一个可点。
 * 这样用户不会面对「两个都能按、但只能选一个」的猜测。
 *
 * **修复在途时两个都关掉**（票 #69）：后端的顶替判据按任务种类、不分产物，同一方案里
 * 两份修复会互相掐断；只拦被修的那一份，等于留了一条能掐掉自己任务的路径。
 */
const canFix = computed(() => state.value === 'revise' && !props.fixInFlight)

const fixHint = computed(() => {
  // 灰的原因是「别处有任务在跑」，不是这条结论不行——挂一句就会说错
  if (props.fixInFlight) return undefined
  return canFix.value
    ? '按这条结论重写整篇：追加一个新版本，并自动再重审一次（重审不再改写）'
    : '只有「有问题」的结论才能按结论修复'
})
</script>

<template>
  <section class="rounded-2xl border border-line bg-surface shadow-sm">
    <!--
      折叠头**三层结构**，从上到下就是阅读顺序：

      1. 标题 + 状态徽标：这一条结论的总体判定（通过 / 有问题 / 审核失败 / 未审核）；
      2. 元信息小字：第几版、什么时候审的、是否已改写——追溯用的噪声级信息，收成一行；
      3. 类别计数：`共 N 条 · 编造 1 · 结构不符 1`，收起时也能看出「几条、分在哪几类」。

      右侧是这份结论上的两个动作（修复 / 重新审核），与「展开 / 收起」分开：
      两个 button **不能嵌套**（HTML 不允许、读屏也读不清），因此外层是 div，
      开关占满剩余宽度，动作按钮独立成组；开关仍是这一行里的**第一个** button。
    -->
    <div class="rounded-2xl px-4 py-3">
      <!-- 标题行：文档缺陷 | 严重度统计（原状态徽标位置）| 右侧追溯信息 | 展开箭头 -->
      <button
        type="button"
        class="flex w-full min-w-0 flex-wrap items-center gap-2 rounded-control py-0.5 text-left transition-colors hover:bg-surface-2 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent-soft-line"
        :aria-expanded="expanded"
        :aria-controls="bodyId"
        @click="expanded = !expanded"
      >
        <h3 class="shrink-0 text-sm font-semibold text-fg">文档缺陷</h3>
        <!-- 状态徽标：只显示「有问题」以外的状态；「有问题」由右侧严重度统计代替，
             不再单独挂一枚「有问题」（与统计重复）。通过 / 审核失败 / 未审核仍要显示。 -->
        <span
          v-if="stateLabel !== '有问题'"
          class="rounded-full px-2 py-0.5 text-[11px] font-medium"
          :class="stateClass"
        >
          {{ stateLabel }}
        </span>
        <!-- 严重度统计：放在原「有问题」徽标的位置，一眼看出几条高几条中 -->
        <span v-if="severityCountsText" class="flex flex-wrap items-center gap-1.5">
          <span
            v-for="level in (['high', 'medium', 'low'] as const)"
            :key="level"
            v-show="issues.some((i) => severityOf(i.severity) === level)"
            class="inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[11px] font-medium ring-1"
            :class="SEVERITY_META[level].badge"
          >
            {{ SEVERITY_META[level].label }}
            <span class="font-normal opacity-75">{{ issues.filter((i) => severityOf(i.severity) === level).length }}</span>
          </span>
        </span>
        <!-- 追溯信息放到标题行右侧 -->
        <span v-if="metaText" class="ml-auto text-[11px] leading-snug text-subtle">{{ metaText }}</span>
        <svg
          class="h-4 w-4 shrink-0 text-subtle transition-transform"
          :class="[expanded ? 'rotate-180' : '', metaText ? '' : 'ml-auto']"
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

      <!-- 动作行：修复 / 重新审核 -->
      <div class="mt-2 flex shrink-0 flex-wrap items-center gap-2">
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
        这次审核没有跑成，<strong class="font-semibold">没有结论</strong>不等于通过。可以点「重新审核」再试一次。
      </p>
      <p v-else-if="state === 'unreviewed'" class="text-xs leading-relaxed text-subtle">
        这一版还没审核过。点「重新审核」补一次，只审不改。
      </p>

      <!-- 展开：按类分组，类内 high 在前（严重度由类别固定，见 ADR 0003）。未分类组在最后。 -->
      <div v-if="issueGroups.length" class="mt-3 space-y-4">
        <section v-for="group in issueGroups" :key="group.key || 'uncategorized'">
          <h4 class="flex items-center gap-1.5">
            <svg
              class="h-3.5 w-3.5 shrink-0"
              :class="group.high ? 'text-danger-fg' : 'text-subtle'"
              viewBox="0 0 20 20"
              fill="currentColor"
              aria-hidden="true"
            >
              <path
                fill-rule="evenodd"
                :d="group.high ? WARNING_ICON : INFO_ICON"
                clip-rule="evenodd"
              />
            </svg>
            <span class="text-xs font-semibold text-fg">{{ group.label }}</span>
            <span class="text-[11px] font-normal text-subtle">{{ group.issues.length }} 条</span>
          </h4>
          <ul
            class="mt-1.5 space-y-2 border-l pl-3"
            :class="group.high ? 'border-danger-line' : 'border-line'"
          >
            <!--
              一条缺陷：定位（小字等宽）+ 严重度徽标一行，描述主体一行。
              严重度按每条自身标注——同组里也可能轻重不一，逐条标才不会让人猜；
              文字 + 颜色双通道，色弱与读屏都能读出来。
            -->
            <li
              v-for="(issue, index) in group.issues"
              :key="index"
              class="flex flex-col gap-0.5"
            >
              <p class="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                <span
                  class="inline-flex shrink-0 items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-semibold ring-1"
                  :class="SEVERITY_META[severityOf(issue.severity)].badge"
                >
                  {{ SEVERITY_META[severityOf(issue.severity)].label }}
                </span>
                <span
                  v-if="issue.where"
                  class="break-words font-mono text-[11px] leading-snug text-subtle"
                >
                  {{ issue.where }}
                </span>
              </p>
              <p class="break-words text-sm leading-relaxed text-body">{{ issue.detail }}</p>
            </li>
          </ul>
        </section>
      </div>

    </div>
  </section>
</template>
