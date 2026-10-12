<script setup lang="ts">
import type { Step, StepId } from '@/types'

/**
 * 步骤进度条。
 *
 * 展示哪几步由外部按入口模式传入（`stepsForMode`）：完整流程五步，PRD 快捷三步，
 * 提示词调试一步。组件不关心模式，只按传进来的列表渲染，避免把模式判断散落到视图里。
 *
 * 可点击范围由「已到达的最远步骤」决定：走过的步骤可以点回去回看，
 * 当前步骤与尚未到达的步骤不可点。
 */
const props = withDefaults(
  defineProps<{
    /** 要展示的步骤（顺序即展示顺序） */
    steps: readonly Step[]
    /** 当前阶段对应的步骤下标 */
    currentIndex: number
    /** 已到达的最远步骤下标 */
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

/** 某个步骤是否可点击：已到达过，且不是当前所处步骤 */
function canClick(index: number): boolean {
  return index <= props.reachedIndex && index !== props.currentIndex
}
</script>

<template>
  <nav v-if="steps.length > 1" class="mb-8" aria-label="生成进度">
    <ol class="flex items-start">
      <li v-for="(step, index) in steps" :key="step.id" class="flex flex-1 items-center">
        <div
          class="flex w-16 shrink-0 flex-col items-center gap-1.5"
          :class="canClick(index) ? 'cursor-pointer group' : ''"
          @click="canClick(index) && emit('select', step.id)"
        >
          <!-- 已完成显示对勾；当前步骤在生成中时呼吸高亮，否则实心高亮 -->
          <span
            class="flex h-8 w-8 items-center justify-center rounded-full text-xs font-semibold transition"
            :class="[
              index === currentIndex
                ? generating
                  ? 'bg-primary-100 text-primary-700 ring-2 ring-primary-500 animate-pulse'
                  : 'bg-primary-500 text-white ring-4 ring-primary-100'
                : index <= reachedIndex
                  ? 'bg-primary-500 text-white'
                  : 'bg-slate-100 text-slate-400',
              canClick(index) ? 'group-hover:ring-4 group-hover:ring-primary-200' : '',
            ]"
          >
            <svg
              v-if="index <= reachedIndex && index !== currentIndex"
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
            <template v-else>{{ index + 1 }}</template>
          </span>
          <span
            class="whitespace-nowrap text-[11px] font-medium transition"
            :class="[
              index <= reachedIndex ? 'text-primary-700' : 'text-slate-400',
              canClick(index) ? 'group-hover:text-primary-600' : '',
            ]"
          >
            {{ step.label }}
          </span>
        </div>
        <!-- 步骤之间的连接线 -->
        <div
          v-if="index < steps.length - 1"
          class="mx-1 mb-5 h-0.5 flex-1 rounded-full transition"
          :class="index < reachedIndex ? 'bg-primary-400' : 'bg-slate-200'"
        ></div>
      </li>
    </ol>
  </nav>
</template>
