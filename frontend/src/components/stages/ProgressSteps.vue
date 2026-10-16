<script setup lang="ts">
import { computed } from 'vue'

import type { Step, StepId } from '@/types'

/**
 * 步骤进度条。
 *
 * 展示哪几步由外部按入口模式传入（`stepsForMode`）：完整流程五步，快捷流程三步，
 * 两个导入入口各一步。组件不关心模式，只按传进来的列表渲染，避免把模式判断散落到视图里。
 *
 * ## 「AI 对话」为什么单独放在最左边
 *
 * 它是唯一**随时可以回去**的一步：另外四步是「产出物 milestones」，做完了就过去了；
 * 而对话是一块持续存在的工作区（补充需求、聊天、澄清都发生在那里）。放在线性链条里，
 * 它读起来像「走完就翻篇的第 2 步」，与它实际的用法不符。
 *
 * 于是呈现上把它**摘出来单列一段**，用一根竖线与其余步骤隔开：
 *
 * ```text
 * [AI 对话] │ [1 描述产品] —— [2 PRD] —— [3 接口文档] —— [4 提示词]
 * ```
 *
 * ⚠️ **摘出来只是呈现，不是流程改动**：`stepsForMode` / `stepIndexOf` / `reachedStepIndex`
 * 一行都没动，AI 对话在链条里的位置与执行顺序完全照旧。判断「当前/已到达/可点」用的始终是
 * 它在 `props.steps` 里的**原始下标**（见下面的 `index` 字段）——若改用画面上的位置，
 * 高亮与点击范围会整体错一格。
 *
 * 序号跟着**画面顺序**重新数（1~4），因为它下面的那四个才是一条有序的里程碑链；
 * 而对话那一段**不给序号、改用气泡图标**——否则左侧出现一个「2」、右边才开始「1」，
 * 看起来像进度条坏了。
 *
 * 可点击范围由「已到达的最远步骤」决定：走过的步骤可以点回去回看。
 *
 * 颜色走语义令牌。此前这一条整个没有暗色适配（未到达的步骤是 `bg-surface-2`
 * 的亮色块、文字是 `text-subtle`，在深色底上刺眼且看不清）。
 */

/** 被摘到最左边的那一步；目前只有「AI 对话」 */
const SIDE_STEP_ID: StepId = 'chatting'

const props = withDefaults(
  defineProps<{
    /** 要展示的步骤（顺序即**执行顺序**，未必是画面顺序） */
    steps: readonly Step[]
    /** 当前阶段对应的步骤下标（对 `steps` 而言） */
    currentIndex: number
    /** 已到达的最远步骤下标（对 `steps` 而言） */
    reachedIndex: number
    /** 当前是否处于「生成中」：当前步骤呼吸高亮 */
    generating?: boolean
  }>(),
  { generating: false },
)

const emit = defineEmits<{
  /** 点击某个已到达的步骤 */
  select: [stepId: StepId]
}>()

/** 一条要渲染的步骤（画面顺序）；`index` 是它在 `steps` 里的原始下标 */
interface DisplayStep {
  step: Step
  /** **原始下标**：current / reached / canClick 一律按它比较，与画面上排第几无关 */
  index: number
  /** 画面上的序号（1 起）；被摘出去的那一步为 `null` */
  ordinal: number | null
  kind: 'side' | 'milestone'
}

/**
 * 画面顺序：被摘出去的那一步排到最前，其余保持原有先后。
 * 摘走的那一步**不占**里程碑的序号——序号只在里程碑之间数。
 */
const displaySteps = computed<DisplayStep[]>(() => {
  const side: DisplayStep[] = []
  const milestones: DisplayStep[] = []

  props.steps.forEach((step, index) => {
    if (step.id === SIDE_STEP_ID) {
      side.push({ step, index, ordinal: null, kind: 'side' })
      return
    }
    milestones.push({ step, index, ordinal: milestones.length + 1, kind: 'milestone' })
  })

  return [...side, ...milestones]
})

/** 里程碑的条数：用来判断「后面还有没有下一条」，最后一条不再画连接线 */
const milestoneCount = computed(
  () => displaySteps.value.filter((item) => item.kind === 'milestone').length,
)

/**
 * 可点击范围：**已到达**的步骤都可以点回去回看。
 *
 * 刻意不排除「当前所在的那一步」：对话页停在某条文档线程上时（澄清 PRD / 接口 / 提示词），
 * 进度条高亮的正是那一篇文档对应的步骤，而「点它 = 回到那篇文档的审阅页」是用户从澄清
 * 回到审阅的**唯一入口**（澄清那条线的底栏只有「按澄清结果改写」，ADR-028 的取舍）。
 * 排除当前步骤就会在那里形成一个死胡同：进去了出不来。
 *
 * 点其它页面上「当前」那一步没有任何副作用——`goTo` 到同一个阶段等于原地不动。
 */
function canClick(index: number): boolean {
  return index <= props.reachedIndex
}

/** 对话区的徽章图标（气泡）：它不是完成态，因此永远不显示对勾 */
const CHAT_ICON =
  'M6 1.5h8a2.5 2.5 0 0 1 2.5 2.5v6a2.5 2.5 0 0 1-2.5 2.5H9l-5.5 2.6V4A2.5 2.5 0 0 1 6 1.5Z'
</script>

<template>
  <nav v-if="displaySteps.length > 1" class="mb-8" aria-label="生成进度">
    <ol class="flex items-start">
      <li
        v-for="item in displaySteps"
        :key="item.step.id"
        :class="
          item.kind === 'side'
            ? 'mr-4 flex items-center border-r border-line-strong pr-4'
            : 'flex flex-1 items-center'
        "
      >
        <div
          class="flex w-16 shrink-0 flex-col items-center gap-1.5"
          :class="canClick(item.index) ? 'cursor-pointer group' : ''"
          @click="canClick(item.index) && emit('select', item.step.id)"
        >
          <!-- 已完成显示对勾；当前步骤在生成中时呼吸高亮，否则实心高亮 -->
          <span
            class="flex h-8 w-8 items-center justify-center rounded-full text-xs font-semibold transition"
            :class="[
              item.index === currentIndex
                ? generating
                  ? 'bg-accent-soft text-accent ring-2 ring-accent animate-pulse'
                  : 'bg-primary-500 text-on-accent ring-4 ring-accent-soft'
                : item.index <= reachedIndex
                  ? 'bg-primary-500 text-on-accent'
                  : 'bg-surface-2 text-subtle',
              canClick(item.index) ? 'group-hover:ring-4 group-hover:ring-accent-soft-line' : '',
            ]"
          >
            <!-- 对话区：固定显示气泡图标（它是「回到对话」的入口，没有「做完了」这个状态） -->
            <svg
              v-if="item.kind === 'side'"
              class="h-4 w-4"
              viewBox="0 0 20 20"
              fill="currentColor"
              aria-hidden="true"
            >
              <path fill-rule="evenodd" :d="CHAT_ICON" clip-rule="evenodd" />
            </svg>
            <svg
              v-else-if="item.index <= reachedIndex && item.index !== currentIndex"
              class="h-4 w-4"
              viewBox="0 0 20 20"
              fill="currentColor"
              aria-hidden="true"
            >
              <path
                fill-rule="evenodd"
                d="M16.704 5.29a1 1 0 0 1 .006 1.414l-7.5 7.5a1 1 0 0 1-1.414 0l-4-4a1 1 0 1 1 1.414-1.414l3.293 3.293 6.793-6.793a1 1 0 0 1 1.408 0Z"
                clip-rule="evenodd"
              />
            </svg>
            <template v-else>{{ item.ordinal }}</template>
          </span>
          <span
            class="whitespace-nowrap text-[11px] font-medium transition"
            :class="[
              item.index <= reachedIndex ? 'text-accent' : 'text-subtle',
              canClick(item.index) ? 'group-hover:text-accent' : '',
            ]"
          >
            {{ item.step.label }}
          </span>
        </div>
        <!-- 步骤之间的连接线；对话那一段后面不画——它与里程碑之间由上面那根竖线分隔 -->
        <div
          v-if="item.kind === 'milestone' && item.ordinal !== null && item.ordinal < milestoneCount"
          class="mx-1 mb-5 h-0.5 flex-1 rounded-full transition"
          :class="item.index < reachedIndex ? 'bg-primary-500' : 'bg-line-strong'"
        ></div>
      </li>
    </ol>
  </nav>
</template>
