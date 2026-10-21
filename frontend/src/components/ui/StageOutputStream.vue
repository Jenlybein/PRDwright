<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'

import type { DocPhase } from '@/types'

/**
 * 阶段输出面板：把「审核 / 改写」两步的模型原始输出实时滚出来。
 *
 * 为什么需要它：这两步都是整篇量级的调用，静默期动辄几十秒。进度条只说明
 * 「现在在哪一步」，说不出「它还在动」，用户很容易以为卡死。把模型的增量文本
 * 原样展示，等待就有了可感知的证据。
 *
 * 它与正文预览是**两个区域**：这里的文本是过程产物（审核意见 / 改写稿草稿），
 * 正文预览里的才是交付物。改写成功后由 `part{replace}` **整段覆盖**正文预览——
 * 即使只重写了几节，发过来的也必须是拼好的完整全文（否则预览会只剩片段）。
 */
const props = defineProps<{
  /** 当前阶段 */
  phase: DocPhase
  /** 当前阶段已流出的原始文本 */
  text: string
}>()

const scroller = ref<HTMLElement | null>(null)

/** 只有审核 / 改写两步需要展示；生成阶段的正文在预览区自己会流 */
const visible = computed(() => props.phase === 'reviewing' || props.phase === 'rewriting')

const title = computed(() => (props.phase === 'rewriting' ? '改写稿' : '审核意见'))

const hint = computed(() =>
  props.phase === 'rewriting'
    ? '正在重写受影响的章节，完成后会整段刷新右侧预览'
    : '正在逐条核对正文，下面是模型正在写出的内容',
)

/**
 * 跟随滚动到底部。
 *
 * 增量可能每几十毫秒来一片，这里不做节流：`scrollTop` 赋值本身很便宜，
 * 而节流反而会让「最后一片没跟上」，看起来像停住了。
 */
watch(
  () => props.text,
  async () => {
    await nextTick()
    const el = scroller.value
    if (el) el.scrollTop = el.scrollHeight
  },
)
</script>

<template>
  <div
    v-if="visible"
    class="rounded-2xl border border-line bg-surface-2 px-3 py-2.5"
    aria-label="阶段输出"
  >
    <div class="mb-1.5 flex items-baseline justify-between gap-3">
      <span class="flex items-center gap-1.5 text-xs font-medium text-fg">
        <!-- 进行中的呼吸点：即使还没有第一个字，也让人知道在等 -->
        <span class="h-1.5 w-1.5 animate-pulse rounded-full bg-primary-500"></span>
        {{ title }}
      </span>
      <span class="shrink-0 text-[11px] text-subtle">{{ hint }}</span>
    </div>
    <div
      ref="scroller"
      class="max-h-44 overflow-y-auto whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-muted"
    >
      <span v-if="!text" class="text-subtle">等待模型首个输出…</span>
      <template v-else>{{ text }}</template>
    </div>
  </div>
</template>
