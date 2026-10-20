<script setup lang="ts">
import { computed } from 'vue'

import DocumentReview from '@/components/DocumentReview.vue'
import DocPhaseProgress from '@/components/ui/DocPhaseProgress.vue'
import PrdProgress from '@/components/stages/PrdProgress.vue'
import AppActionBar from '@/components/ui/AppActionBar.vue'
import AppButton from '@/components/ui/AppButton.vue'
import type { DocPhase, PrdBoardState } from '@/types'
import { formatDuration } from '@/utils/metering'

/**
 * 文档「生成中 / 生成失败」阶段视图。
 *
 * PRD、接口文档、提示词套件以及 AI 优化共用同一个生成态：生成期间只读展示流式内容，
 * 失败（如被截断）时不再渲染空的审阅区，只把原因贴出来并给出返回入口。
 *
 * PRD 走的是分章节链路（撰写 → 审核 → 改写），额外用 `prd` 传入阶段进度；
 * 其它文档与降级路径不传，行为与拆分前完全一致。
 */
const props = defineProps<{
  /** 文档标题 */
  title: string
  /** 流式生成中的增量文本 */
  streamingContent: string
  /** 生成 / 优化过程中的错误原因；非空表示失败 */
  error?: string
  /** 是否仍在生成中；为 false 表示已失败，只展示错误与返回入口 */
  generating?: boolean
  /** PRD 分章节生成的管道看板；非 PRD 时为 null */
  prd?: PrdBoardState | null
  /** 下游文档（接口 / 提示词）的生成阶段；idle 表示未走该链路 */
  docPhase?: DocPhase | null
  /** 当前流式展示的是否为「上一版草稿」（章节改写期间），用于降透明度并加提示 */
  draftOnly?: boolean
  /**
   * 本次生成已用时（毫秒），由任务运行态统一维护（见 `composables/useJob.ts`）。
   *
   * 组件自己**不计时**：重连时要用服务端的已用时打底，收尾后要停在权威总耗时上，
   * 这两件事只有任务运行态做得到。
   */
  elapsedMs?: number
  /** 「返回」按钮的文案（返回目标随入口模式而变，由上层决定） */
  backLabel?: string
}>()

const emit = defineEmits<{
  /** 返回对话 */
  back: []
}>()

/** 已用时的可读文案；为 0（尚未开始计时）时不展示 */
const elapsedText = computed(() => formatDuration(props.elapsedMs ?? 0))
</script>

<template>
  <section class="space-y-3">
    <!-- 分章节生成时先给出阶段条：让用户知道卡在哪一章、是否在改写 -->
    <PrdProgress
      v-if="prd && (prd.total > 0 || prd.sections.length > 0)"
      :board="prd"
    />
    <!-- 下游文档：整篇生成 → 审核 → 改写，用同一套胶囊样式展示 -->
    <DocPhaseProgress
      v-else-if="docPhase && docPhase !== 'idle'"
      :phase="docPhase"
    />

    <!-- 已用时：长生成时给一个「还在推进」的可感知信号，比转圈动画更有信息量。
         只在确实计过时（>0）时出现，避免刚进入生成态就闪一个「已用时 0 秒」 -->
    <p
      v-if="generating && (elapsedMs ?? 0) > 0"
      class="text-xs tabular-nums text-subtle"
    >
      已用时 {{ elapsedText }}
    </p>

    <!-- 生成 / 优化出错提示：非正常结尾（如被截断）时展示原因 -->
    <p
      v-if="error"
      class="flex items-start gap-2 rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-700"
      role="alert"
    >
      <svg class="mt-0.5 h-4 w-4 shrink-0" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
        <path
          fill-rule="evenodd"
          d="M8.485 2.495c.673-1.167 2.357-1.167 3.03 0l6.28 10.875c.673 1.167-.17 2.625-1.516 2.625H3.72c-1.347 0-2.189-1.458-1.515-2.625L8.485 2.495ZM10 6a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5A.75.75 0 0 1 10 6Zm0 8a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z"
          clip-rule="evenodd"
        />
      </svg>
      <span>{{ error }}</span>
    </p>

    <!-- 生成中：只读流式展示；失败后不再展示空的审阅区，仅提示原因 -->
    <DocumentReview
      v-if="generating"
      :title="title"
      :streaming-content="streamingContent"
      :generating="true"
      :draft-only="draftOnly"
    />
    <!-- 页面级操作栏：生成中 / 失败时只有「返回」这一条退路，主操作区留空 -->
    <AppActionBar>
      <template #secondary>
        <AppButton variant="secondary" size="md" @click="emit('back')">
          {{ backLabel ?? '返回对话' }}
        </AppButton>
      </template>
    </AppActionBar>
  </section>
</template>
