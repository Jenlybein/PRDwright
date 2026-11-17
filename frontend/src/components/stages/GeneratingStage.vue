<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'

import DocumentReview from '@/components/DocumentReview.vue'
import DocPhaseProgress from '@/components/ui/DocPhaseProgress.vue'
import PrdProgress from '@/components/stages/PrdProgress.vue'
import StageOutputStream from '@/components/ui/StageOutputStream.vue'
import AppActionBar from '@/components/ui/AppActionBar.vue'
import AppButton from '@/components/ui/AppButton.vue'
import type { DocPhase, PrdBoardState } from '@/types'
import { isWaitingRetry, retryNoticeText, retryRemainingMs } from '@/utils/backoff'
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
// ⚠️ 布尔 prop 在**未传**时会被 Vue 自动转成 `false`，因此凡「默认该开」的开关都要
// 显式给默认值：`withReview` 未传必须是 true，否则完整流程的四车道会被悄悄砍成两条。
const props = withDefaults(
  defineProps<{
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
    /**
     * 本次生成是否带「审核 / 改写」；为 false（快捷流程）时阶段条只画「生成 → 完成」。
     *
     * 判据由上层按入口模式给出：那两格永远走不到，画上去等于告诉用户「还有两步没做」。
     */
    withReview?: boolean
    /**
     * 下游文档当前阶段（审核 / 改写）已流出的**原始输出**。
     *
     * 只喂给阶段条下方的输出面板：审核意见与改写稿草稿都是过程产物，
     * 不能混进正文预览（正文由 `streamingContent` 单独负责）。
     */
    docStageText?: string
    /**
     * 大纲文本（票 #91）：`stage_chunk(outlining)` 的累计。
     *
     * 与 `docStageText` 分开传：它在正文撰写期间仍然要显示（「先定了哪些结构」），
     * 而审核 / 改写的输出换阶段就该清。
     */
    docOutlineText?: string
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
    /**
     * 是否**不显示**这条底部操作栏。
     *
     * 生成中这一栏原本只有「返回」一个按钮，落点就是录入页；而录入页在进度条上的入口
     * （完整流程 / 快捷流程）点第 1 步就能回去，留一个同名按钮纯属重复。此时整条栏都不渲染
     * ——只藏按钮会留下一条空栏，比不显示更难看。
     */
    hideBack?: boolean
    /**
     * 是否给「查看当前版本」这个出口（票 #68）。
     *
     * 只在**这张页是被一支修复任务撑着**时给：修复会整篇重写，正文一直在变，用户可能
     * 只想读没被写的那一版。别的动作（生成 / 优化 / 改写）本就以「看它写」为目的，
     * 多一个出口只是噪音。点它由上层压住兜底渲染并回到审阅页。
     */
    canViewCurrentVersion?: boolean
    /**
     * 标题旁的徽标文案（票 #67）：按动作区分「修复中 / 重审中 / 优化中 / 改写中 / 生成中」。
     *
     * 由上层给出而不是在这里按 kind 猜：这张页不知道任务种类，也不该知道。
     * 缺省回落到「生成中」由文档卡片自己兜（只在一处）。
     */
    statusLabel?: string
    /**
     * 失败退避（票 #88）：这条任务最早什么时候能被下一个执行者领走（后端 `next_run_at`）。
     *
     * 空串 / `null` 表示没在等（绝大多数生成都走这条）。非空时这张页进入**等待重试**
     * 形态：显示倒计时而不是转圈，也不显示成失败——任务没有失败，只是在等下一次尝试。
     */
    retryAt?: string | null
    /**
     * 是否**正在取消**（票 #84，七态里的 `cancelling`）。
     *
     * 用户按下停止后按钮必须立刻变成「正在取消…」并置灰：取消是两段的（请求 →
     * 执行者在下一个动作点确认），服务端要等动作点才落终态。不给即时反馈的话，
     * 这一两秒里用户只会看到按钮毫无反应，然后连点好几次。
     */
    cancelling?: boolean
  }>(),
  { withReview: true },
)

const emit = defineEmits<{
  /** 返回对话 */
  back: []
  /** 先不看了，回到审阅页读还没被重写的那一版 */
  viewCurrentVersion: []
  /** 等待重试期间按「停止生成」（退避期间取消仍然可用） */
  cancel: []
}>()

/** 已用时的可读文案；为 0（尚未开始计时）时不展示 */
const elapsedText = computed(() => formatDuration(props.elapsedMs ?? 0))

/**
 * 倒计时的本地时钟（毫秒）。
 *
 * 倒计时的一格来自 `setInterval`，而**换算本身是纯函数**（`@/utils/backoff`）：
 * 组件只负责每秒钟把「现在」推进一次，于是用例可以用假时钟把 30 秒的退避在毫秒内
 * 演示完，不需要真的等。
 */
const nowMs = ref(Date.now())
const retryAtValue = computed(() => props.retryAt ?? null)

/** 是否处于「等待重试」：有等待信息就是（已到点也算——那一刻还没被领走，不能报终态） */
const waiting = computed(() => isWaitingRetry(retryAtValue.value, nowMs.value))

/** 倒计时文案：`ceil` + clamp，永不出现负数，也不会在最后一秒反复横跳 */
const retryText = computed(() =>
  retryNoticeText(retryRemainingMs(retryAtValue.value, nowMs.value) ?? 0),
)

let ticker: ReturnType<typeof setInterval> | null = null

function stopTicker(): void {
  if (ticker === null) return
  clearInterval(ticker)
  ticker = null
}

/**
 * 按「还在不在等」开停秒表。
 *
 * 只在**真的在等**时开表：等待态之外的每一秒都刷新一次组件是白烧渲染，而这类
 * 无谓定时器最容易在页面切走之后仍然活着（泄漏的经典来源）。到点之后就停表
 * （文案切到「即将重试」后不再变化，也就没有闪烁）。
 */
function syncTicker(): void {
  const remaining = retryRemainingMs(retryAtValue.value, Date.now())
  if (remaining === null || remaining <= 0) {
    stopTicker()
    return
  }
  if (ticker !== null) return
  nowMs.value = Date.now()
  ticker = setInterval(() => {
    nowMs.value = Date.now()
    // 到点即停：再跳一秒也只是把「即将重试」重写一遍
    if ((retryRemainingMs(retryAtValue.value, Date.now()) ?? 0) <= 0) stopTicker()
  }, 1000)
}

watch(retryAtValue, syncTicker, { immediate: true })
onBeforeUnmount(stopTicker)
</script>

<template>
  <section class="space-y-3">
    <template v-if="!waiting">
      <!-- 分章节生成时先给出阶段条：让用户知道卡在哪一章、是否在改写 -->
      <PrdProgress
        v-if="prd && (prd.total > 0 || prd.sections.length > 0)"
        :board="prd"
        :with-review="withReview"
      />
      <!-- 下游文档：整篇生成 →（审核 → 改写）→ 完成；不复核时就只有首尾两格 -->
      <DocPhaseProgress
        v-else-if="docPhase && docPhase !== 'idle'"
        :phase="docPhase"
        :with-review="withReview"
      />

      <!-- 阶段输出：紧贴进度条下方。大纲 / 审核 / 改写都是「先定结构」或整篇量级的
           静默步骤，把过程文本实时滚出来，用户才知道「它还在动、它凭什么这么定」 -->
      <StageOutputStream
        v-if="docPhase && docPhase !== 'idle'"
        :phase="docPhase"
        :text="docStageText ?? ''"
        :outline-text="docOutlineText ?? ''"
      />
    </template>

    <!-- 失败退避（票 #88）：排队中要能看见「在等、等多久」。
         三件事一起成立才算做对：不显示成失败（状态仍是执行中/排队中）、不无限转圈
         （换成确定性的倒计时）、退避期间取消仍然可用（这里保留「停止生成」）。 -->
    <div
      v-if="waiting"
      class="flex flex-col gap-2 rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-700"
      role="status"
    >
      <p class="flex items-center gap-2 font-medium tabular-nums">
        <svg class="h-4 w-4 shrink-0" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
          <path
            fill-rule="evenodd"
            d="M10 18a8 8 0 1 0 0-16 8 8 0 0 0 0 16Zm.75-13a.75.75 0 1 0-1.5 0v4c0 .198.079.389.22.53l2.5 2.5a.75.75 0 1 0 1.06-1.06l-2.28-2.28V5Z"
            clip-rule="evenodd"
          />
        </svg>
        <span>{{ retryText }}</span>
      </p>
      <p class="text-xs text-amber-700/80">
        上次失败后按退避等待，不会立刻重试；等待期间仍然可以停止这次任务。
      </p>
      <AppButton
        variant="secondary"
        size="sm"
        class="self-start"
        :disabled="cancelling"
        @click="emit('cancel')"
      >
        {{ cancelling ? '正在取消…' : '停止生成' }}
      </AppButton>
    </div>

    <!-- 已用时：长生成时给一个「还在推进」的可感知信号，比转圈动画更有信息量。
         只在确实计过时（>0）时出现，避免刚进入生成态就闪一个「已用时 0 秒」；
         等待重试期间不展示——那段时间本来就没有执行者。 -->
    <p
      v-if="!waiting && generating && (elapsedMs ?? 0) > 0"
      class="text-xs tabular-nums text-subtle"
    >
      已用时 {{ elapsedText }}
    </p>

    <!-- 出口：先不读过程，回去读还没被重写的那一版（票 #68）。
         修复是整篇重写，正文一直在长，有人此刻要的正是旧版而不是进度。 -->
    <button
      v-if="canViewCurrentVersion && !waiting"
      type="button"
      class="self-start text-xs text-brand-600 underline-offset-2 hover:underline"
      @click="emit('viewCurrentVersion')"
    >
      查看当前版本
    </button>

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

    <!-- 生成中：只读流式展示；失败后不再展示空的审阅区，仅提示原因。
         等待重试期间不渲染这一块：它自带转圈动画，而那段时间没有执行者在跑，
         转圈只会让人以为卡死（换成上面的倒计时）。 -->
    <DocumentReview
      v-if="generating && !waiting"
      :title="title"
      :streaming-content="streamingContent"
      :generating="true"
      :draft-only="draftOnly"
      :status-label="statusLabel"
    />
    <!-- 页面级操作栏：生成中 / 失败时只有「返回」这一条退路，主操作区留空。
         录入页在进度条上的入口连这条退路都是重复的，此时整条栏不渲染。 -->
    <AppActionBar v-if="!hideBack">
      <template #secondary>
        <AppButton variant="secondary" size="md" @click="emit('back')">
          {{ backLabel ?? '返回对话' }}
        </AppButton>
      </template>
    </AppActionBar>
  </section>
</template>
