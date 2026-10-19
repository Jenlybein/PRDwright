<script lang="ts">
import type { ClarificationAnswerItem, ToolResultPayload } from '@/types'

/**
 * 提交回答时 emit 出去的载荷。
 *
 * 三个字段各有各的去处，**互不依赖**：
 *
 * - `text`：发给后端的**正文**。降级（拿不到 `tool_call_id`）时它就是模型唯一能
 *   读到的东西，所以照旧保留那段完整的「题干 + 选项文本」，不为了界面好看而改短；
 * - `answers`：**界面回执**用（选了哪几个标号）。只要是点选项卡提交就一定有，
 *   与能不能结构化回灌无关——用户看不到模型收到了什么，他只看得到自己的界面；
 * - `toolResult`：**给模型的结构化回灌**（多一个 `tool_call_id` 要配对）。
 *   缺 id 时干脆不發，后端按纯文本处理。
 *
 * 今天 `answers` 与 `toolResult.answers` 恰好是同一份数据，但它们是两件事：
 * 一个管「人看什么」，一个管「模型收什么」，可以独立存在。
 */
export interface AnswerPayload {
  text: string
  answers: ClarificationAnswerItem[]
  toolResult?: ToolResultPayload
}
</script>

<script setup lang="ts">
import { computed, ref } from 'vue'

import type { ClarificationAsk } from '@/types'

/**
 * 一轮澄清提问里的**结构化选择题**：把每个问题的选项渲染成可点的卡片。
 *
 * 两种作答方式，同一个「提交回答」出口：
 *
 * 1. 单选：点一个选项（`radiogroup` 语义）；
 * 2. 多选：点若干选项（切换按钮语义，`aria-pressed`）。
 *
 * 出口发出的是**结构化载荷**（见 `AnswerPayload`）：展示文本给人看，逐题的结构化
 * 答案给模型看（后端按工具结果回灌）。本轮拿不到工具调用 id 时**只发文本**，降级路径。
 *
 * **不可交互时整块不渲染**（`v-if="interactive"`）：卡片只在「还能答」的时候出现，
 * 作答后就没理由再占地方了——这正是用户要的「选项卡提交之后就该消失」。规则收在
 * 组件自己身上，调用点（`MessageList`）不必各写一遍；而「只有最后一条消息可交互」
 * 那条判据在调用点已经成立，所以提交后卡片天然消失，无需额外状态。
 *
 * **想自己写不受这里限制**：直接在页面底部的聊天输入框打字发送即可，那条路一直都在，
 * 本组件刻意不再画一个输入框——卡片里再摆一个，反倒让人以为「必须在这儿写才算数」。
 * 提交按钮旁那行「若不满意选项，可以自行在输入框输入想法」就是给它的指路。
 *
 * **选择是组件内的临时状态，不进快照**：只有当前线程的最后一条消息会被渲染成
 * 可交互的（见 `MessageList`），用户一作答它就不再是最后一条，组件随之失效——刷新、
 * 切线程、回看历史都天然回到干净状态，不需要额外清理。
 */
const props = withDefaults(
  defineProps<{
    asks: ClarificationAsk[]
    /** 是否可交互：只有「当前线程最后一条消息」为真；历史消息渲染成只读 */
    interactive?: boolean
  }>(),
  { interactive: false },
)

const emit = defineEmits<{ answer: [payload: AnswerPayload] }>()

/** 每道题选中的标号（多选时多个）；key 是题目 id */
const selections = ref<Record<string, string[]>>({})

function selectedOf(ask: ClarificationAsk): string[] {
  return selections.value[ask.id] ?? []
}

function isSelected(ask: ClarificationAsk, label: string): boolean {
  return selectedOf(ask).includes(label)
}

/** 点选 / 取消点选。单选直接替换，多选切换 */
function toggle(ask: ClarificationAsk, label: string): void {
  if (!props.interactive) return
  const current = selectedOf(ask)
  const next = ask.multiple
    ? current.includes(label)
      ? current.filter((item) => item !== label)
      : [...current, label]
    : [label]
  selections.value = { ...selections.value, [ask.id]: next }
}

/** 一道题是否已有答案；本组件只有「选中若干项」这一种作答 */
function answered(ask: ClarificationAsk): boolean {
  return selectedOf(ask).length > 0
}

/** 已经作答的题目数，用于决定「提交回答」是否可用 */
const answeredCount = computed(() => props.asks.filter(answered).length)

/**
 * 把一道题的答案写成一行。
 *
 * 选项带上标号（`A. 标题`）而不只是标题：模型要能把它对回自己给的选项，
 * 只回一句「第一版就做离线同步」在它看来说是用户自己打的字。
 */
function answerOf(ask: ClarificationAsk): string {
  // 按**选项显示顺序**取，而不是点选先后：同一组选中项必须产出同一条消息——
  // 顺序若跟着手指走，同样的答案会因为点击次序不同而长得不一样，读起来也更乱。
  return selectedOptionsOf(ask)
    .map((option) => `${option.label}. ${option.text}`)
    .join('；')
}

/**
 * 一道题被选中的选项，按**选项显示顺序**。
 *
 * 展示文本与结构化作答共用这一份顺序：两边口径一致，才不会出现「气泡里写 A、C，
 * 回灌给模型的却是 C、A」这种自己跟自己对不上的情况。
 */
function selectedOptionsOf(ask: ClarificationAsk): ClarificationAsk['options'] {
  return ask.options.filter((option) => isSelected(ask, option.label))
}

/**
 * 组织成**一条**消息发出去：一道题就写一行题号 + 题干 + 答案。
 *
 * 题干要带上——一轮问了两个问题时，不写题干的话模型只能靠顺序猜哪句答的是哪题，
 * 而顺序这件事没有谁保证过。只有一道题时也保持同一种写法：少一个分支，就少一处
 * 「单题与多题行为不一致」的坑。
 */
function compose(): string {
  return props.asks
    .filter(answered)
    .map((ask, index) => `${index + 1}. ${ask.question}\n   答：${answerOf(ask)}`)
    .join('\n')
}

/**
 * 本轮**已作答题**的逐题作答，按题目在选项卡里的顺序。
 *
 * 它**只服务界面**：回执上只列选中的标号，不重复题干、不显示选项文本——
 * 那些内容用户在卡片与正文里刚读过。
 *
 * ⚠️ 与「能不能结构化回灌」无关：即便本轮拿不到 `tool_call_id` 只能降级成
 * 纯文本发给模型，界面也照样用回执形式。人看不到模型收到了什么。
 *
 * `labels` / `texts` 按**选项展示顺序**给（与 `answerOf` 同一口径），
 * 并冗余带上 `question`。
 */
function selectedAnswers(): ClarificationAnswerItem[] {
  return props.asks
    .filter(answered)
    .map((ask) => {
      const options = selectedOptionsOf(ask)
      return {
        ask_id: ask.id,
        labels: options.map((option) => option.label),
        question: ask.question,
        texts: options.map((option) => option.text),
      }
    })
}

/**
 * 组织给模型的**结构化回灌**；**配不上对时返回 null**。
 *
 * 判据是「本轮每一道已作答的题都带着同一个非空的 `tool_call_id`」：
 * 只要有一道缺，就无从把答案回给当初那次调用，此时降级为纯文本作答——
 * 半截结构化答案比没有更糟（模型会看到一次只被应答了一部分的调用）。
 *
 * 注意它只决定**模型**那一路；界面那一路走 `selectedAnswers()`，不受这里影响。
 */
function composeToolResult(): ToolResultPayload | null {
  const answeredAsks = props.asks.filter(answered)
  if (!answeredAsks.length) return null

  const toolCallIds = new Set(answeredAsks.map((ask) => ask.tool_call_id ?? ''))
  const toolCallId = answeredAsks[0]?.tool_call_id
  // 缺 id、或一轮里的 id 不一致：都当作没有可配对的调用，交给纯文本降级
  if (!toolCallId || toolCallIds.size !== 1) return null

  return { tool_call_id: toolCallId, answers: selectedAnswers() }
}

function submit(): void {
  if (!props.interactive || answeredCount.value === 0) return
  const toolResult = composeToolResult()
  emit('answer', {
    text: compose(),
    answers: selectedAnswers(),
    // 只在配得上对时带这个键：降级就是「不带」
    ...(toolResult ? { toolResult } : {}),
  })
}
</script>

<template>
  <!-- 不可交互（已在历史里，或不是最后一条）时整块消失，而不是退成只读样式 -->
  <div v-if="interactive" class="mt-3 space-y-3">
    <div v-for="ask in asks" :key="ask.id">
      <!--
        只有一轮问了多道题时才显示题干：只问一道时正文里刚写过同一个问题，
        再印一遍是纯噪声。
      -->
      <p v-if="asks.length > 1 && ask.question" class="mb-1.5 text-sm font-medium text-fg">
        {{ ask.question }}
      </p>

      <div
        :role="ask.multiple ? 'group' : 'radiogroup'"
        :aria-label="ask.question"
        class="space-y-1.5"
      >
        <button
          v-for="option in ask.options"
          :key="option.label"
          type="button"
          :role="ask.multiple ? undefined : 'radio'"
          :aria-checked="ask.multiple ? undefined : isSelected(ask, option.label)"
          :aria-pressed="ask.multiple ? isSelected(ask, option.label) : undefined"
          :disabled="!interactive"
          class="flex w-full items-start gap-2.5 rounded-xl border bg-surface px-3 py-2.5 text-left text-sm shadow-sm transition"
          :class="
            isSelected(ask, option.label)
              ? 'border-primary-500 bg-accent-soft ring-1 ring-accent-soft-line'
              : 'border-line'
          "
          @click="toggle(ask, option.label)"
        >
          <span
            class="mt-px flex h-5 w-5 shrink-0 items-center justify-center rounded-md text-[11px] font-semibold"
            :class="
              isSelected(ask, option.label)
                ? 'bg-primary-500 text-white'
                : 'bg-surface-2 text-muted'
            "
            aria-hidden="true"
          >
            {{ option.label }}
          </span>
          <span class="min-w-0 flex-1 break-words text-body">{{ option.text }}</span>
        </button>
      </div>
    </div>

    <!--
      提交按钮与它右侧的提示：常驻那句指路「若不满意选项，可以自行在输入框输入想法」，
      未答题数那句是动态信息。两者都要能读断句，所以让这一行允许折行。
    -->
    <div v-if="interactive" class="flex flex-wrap items-center gap-x-3 gap-y-1">
      <button
        type="button"
        class="rounded-lg bg-primary-500 px-3.5 py-1.5 text-xs font-medium text-white transition disabled:cursor-not-allowed disabled:opacity-40"
        :disabled="answeredCount === 0"
        @click="submit"
      >
        提交回答
      </button>
      <span class="text-xs text-subtle">若不满意选项，可以自行在输入框输入想法</span>
      <span v-if="asks.length > 1 && answeredCount < asks.length" class="text-xs text-subtle">
        还有 {{ asks.length - answeredCount }} 题未作答，也可以先提交
      </span>
    </div>
  </div>
</template>
