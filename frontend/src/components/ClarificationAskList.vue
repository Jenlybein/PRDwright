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

const emit = defineEmits<{ answer: [text: string] }>()

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
  return ask.options
    .filter((option) => isSelected(ask, option.label))
    .map((option) => `${option.label}. ${option.text}`)
    .join('；')
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

function submit(): void {
  if (!props.interactive || answeredCount.value === 0) return
  emit('answer', compose())
}
</script>

<template>
  <div class="mt-3 space-y-3">
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
