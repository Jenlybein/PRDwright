<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import AppActionBar from '@/components/ui/AppActionBar.vue'
import AppButton from '@/components/ui/AppButton.vue'

/**
 * 我有 PRD入口的录入页。
 *
 * 场景是「PRD 已经有了」：可能是别的工具写的、别人给的，也可能是本次流程生成后
 * 手工改过的版本。所以这里只收一份 PRD 正文，不做任何解析——提示词套件本来就是
 * 从 PRD 推导出来的，中间不需要再插一步结构化。
 *
 * 录入内容会随会话快照持久化，刷新不丢；生成完进入审阅页后可以用「AI 优化」
 * 反复迭代，这才构成「调试」的闭环。
 *
 * 空正文时**按钮直接禁用**，缺失提示常驻在字段下方：禁用态点不动，指望点击后再
 * 报错是收不到任何反馈的。
 */
const props = defineProps<{
  /** 用户贴入的 PRD 正文 */
  prdText: string
}>()

const emit = defineEmits<{
  /** PRD 正文变化 */
 'update:prdText': [value: string]
  /** 生成提示词套件 */
  generate: []
  /** 返回入口选择页 */
  back: []
}>()

/** PRD 文本框；本页只有这一个必填项，挂载时直接聚焦 */
const textareaRef = ref<HTMLTextAreaElement | null>(null)

onMounted(() => textareaRef.value?.focus())

/** 正文去掉空白后是否为空 */
const prdMissing = computed(() => props.prdText.trim() === '')

/** 校验通过才允许生成 */
const canGenerate = computed(() => !prdMissing.value)

/**
 * 看起来「像不像一份 PRD」的粗略提示。
 * 只是提示，不拦提交：用户的 PRD 长什么样不该由我们规定。
 */
const looksLikeMarkdown = computed(() => /^#{1,3}\s/m.test(props.prdText))

/** 清空正文 */
function clearAll(): void {
  emit('update:prdText', '')
}

/** 把过长的正文收成一行摘要，用于确认条 */
const previewLine = computed(() => {
  const firstLine = props.prdText.trim().split('\n')[0] ?? ''
  return firstLine.replace(/^#+\s*/, '').slice(0, 40)
})
</script>

<template>
  <section class="space-y-4">
    <header>
      <h1 class="text-lg font-bold text-fg ">从现有 PRD 生成提示词套件</h1>
      <p class="mt-1 text-sm text-subtle">
        把手头这份 PRD 贴进来，直接生成配套的提示词套件；生成后可继续用「AI 优化」
        逐轮调整，直到提示词可用为止。
      </p>
    </header>

    <div class="space-y-3 rounded-2xl border border-line bg-surface p-5 shadow-sm  ">
      <div>
        <label for="field-debug-prd" class="mb-1.5 block text-xs font-medium text-muted ">
          PRD 正文（Markdown）
          <span class="ml-1 text-rose-500">*</span>
        </label>
        <textarea
          id="field-debug-prd"
          ref="textareaRef"
          rows="14"
          class="w-full resize-y rounded-lg border px-3 py-2 font-mono text-xs leading-relaxed text-fg outline-none transition placeholder:text-subtle focus:ring-2  "
          :class="
            prdMissing
              ? 'border-line-strong focus:border-accent focus:ring-accent-soft-line  '
              : 'border-accent focus:border-accent focus:ring-accent-soft-line  '
 "
          placeholder="# 小账本 产品需求文档（PRD）&#10;&#10;## 1. 产品概述&#10;…"
          :value="prdText"
          @input="emit('update:prdText', ($event.target as HTMLTextAreaElement).value)"
        ></textarea>

        <div class="mt-1.5 flex flex-wrap items-start justify-between gap-2">
          <p v-if="prdMissing" class="text-xs text-amber-600 dark:text-amber-400" role="status">
            请先贴入 PRD 正文，否则没有可依据的内容。
          </p>
          <p v-else-if="!looksLikeMarkdown" class="text-xs text-amber-600 dark:text-amber-400">
            没检测到 Markdown 标题（`#`）。纯文本也能生成，只是章节结构可能不如原文清晰。
          </p>
          <p v-else class="text-xs text-subtle">支持任意 Markdown 文本，不做格式校验。</p>
          <span class="shrink-0 text-xs text-subtle">{{ prdText.length }} 字</span>
        </div>
      </div>

      <!-- 已录入内容的确认条：给一个「我刚贴进来的是什么」的回执 -->
      <p
        v-if="previewLine"
        class="rounded-lg border border-line bg-surface-2 px-3 py-2 text-xs text-subtle   "
      >
        已录入：{{ previewLine }}{{ prdText.length > 40 ? '…' : '' }}
      </p>
    </div>

    <!-- 页面级操作栏：左「清空」│「返回入口」，右主操作 -->
    <AppActionBar>
      <template #danger>
        <AppButton variant="danger" size="md" :disabled="!prdText" @click="clearAll">
          清空
        </AppButton>
      </template>
      <template #secondary>
        <AppButton variant="secondary" size="md" @click="emit('back')">返回入口</AppButton>
      </template>

      <AppButton variant="primary" size="md" :disabled="!canGenerate" @click="emit('generate')">
        生成提示词套件
      </AppButton>
    </AppActionBar>
  </section>
</template>

