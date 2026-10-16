<script setup lang="ts">
import { CHAT_STAGE_LABELS, type ChatStage } from '@/types'

/**
 * 对话页的子标签栏：四条澄清线程（需求 / PRD / 接口 / 提示词）常驻在这里。
 *
 * 为什么**常驻**而不是「没发生就隐藏」：标签本身就是地图——用户看到还有三条线程，
 * 才知道「原来接口文档也能这样聊」。不可用的置灰、给出理由（`title`），而不是消失。
 *
 * 纯展示组件：可用性与禁用态全由上层算好（`available` / `busy`），它只负责画与报点。
 */
const props = defineProps<{
  /** 全部标签（顺序即展示顺序） */
  tabs: readonly ChatStage[]
  /** 当前选中的标签 */
  active: ChatStage
  /** 可点的标签；不在其中的置灰 */
  available: readonly ChatStage[]
  /**
   * 有请求在跑时全部禁用。
   *
   * 切标签会同时换掉消息区与底栏动作：跑到一半换过去，用户看到的是一段与自己刚才操作
   * 无关的上下文，而流式回复还在往原来的线程里写。挡住比解释便宜。
   */
  busy?: boolean
}>()

const emit = defineEmits<{ select: [stage: ChatStage] }>()

/** 该标签是否不可点（请求进行中，或这条线程还没就绪） */
function isDisabled(stage: ChatStage): boolean {
  return Boolean(props.busy) || !props.available.includes(stage)
}

/**
 * 标签外观。
 *
 * 当前标签**即使处于禁用态也保持实心**：它是「你现在在哪」，淡下去会让人怀疑自己是不是
 * 掉出了当前线程。禁用靠 `cursor` 与 `disabled` 属性表达。
 */
function tabClass(stage: ChatStage): string {
  if (stage === props.active) return 'bg-surface text-fg shadow-sm'
  return isDisabled(stage)
    ? 'text-subtle/60'
    : 'text-subtle hover:bg-surface/60 hover:text-muted'
}

/** 置灰的原因：鼠标悬停时说明为什么点不动，而不是让人猜 */
function tabTitle(stage: ChatStage): string | undefined {
  if (props.busy) return '正在处理中，请稍候'
  if (!props.available.includes(stage)) return '这条澄清线程还用不了'
  return undefined
}
</script>

<template>
  <div
    class="flex flex-wrap items-center gap-1 rounded-xl bg-surface-2 p-1 ring-1 ring-line"
    role="tablist"
    aria-label="澄清线程"
  >
    <button
      v-for="stage in tabs"
      :key="stage"
      type="button"
      role="tab"
      class="rounded-lg px-3 py-1.5 text-xs font-medium transition disabled:cursor-not-allowed"
      :class="tabClass(stage)"
      :aria-selected="stage === active"
      :disabled="isDisabled(stage)"
      :title="tabTitle(stage)"
      @click="emit('select', stage)"
    >
      {{ CHAT_STAGE_LABELS[stage] }}
    </button>
  </div>
</template>
