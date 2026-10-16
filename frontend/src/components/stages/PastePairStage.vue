<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import AppActionBar from '@/components/ui/AppActionBar.vue'
import AppButton from '@/components/ui/AppButton.vue'

/**
 * 「我有 PRD + 接口文档」入口的录入页（双粘贴向导）。
 *
 * 与「我有 PRD」的差别只有**材料多一份**：那条链路只读 PRD，这条把接口文档也带上，
 * 于是提示词里提到的接口路径、字段名与职责划分能对齐一份真实存在的接口契约，而不是
 * 模型自己猜的。生成的产物仍是**一份提示词套件**——自带的那两份材料不会成为本工具的
 * 产物（它们不是这里生成的，混进文档列表会让进度、完成条件与下载列表都失真，所以
 * 只作为本次生成的输入存在）。
 *
 * 两份材料的地位不同：**PRD 必填**（没有它就没有可依据的需求），接口文档选填。
 * 录入页据此把校验做成不对称的——接口文档为空不影响提交，但 PRD 为空时按钮直接禁用，
 * 且缺失提示常驻在字段下方（禁用态点不动，指望点击后再报错是收不到任何反馈的）。
 *
 * 录入内容随会话快照持久化，刷新不丢。
 */
const props = defineProps<{
  /** 用户贴入的 PRD 正文（必填） */
  prdText: string
  /** 用户贴入的接口文档正文（选填） */
  apiDocsText: string
}>()

const emit = defineEmits<{
  /** PRD 正文变化 */
  'update:prdText': [value: string]
  /** 接口文档正文变化 */
  'update:apiDocsText': [value: string]
  /** 生成提示词套件 */
  generate: []
  /** 返回入口选择页 */
  back: []
}>()

/** PRD 文本框；本页唯一必填项，挂载时直接聚焦 */
const prdRef = ref<HTMLTextAreaElement | null>(null)

onMounted(() => prdRef.value?.focus())

/** PRD 去掉空白后是否为空 */
const prdMissing = computed(() => props.prdText.trim() === '')

/** 是否有接口文档（只影响提示语，不影响能否提交） */
const hasApiDocs = computed(() => props.apiDocsText.trim() !== '')

/** 校验通过才允许生成：PRD 必填，接口文档不管 */
const canGenerate = computed(() => !prdMissing.value)

/**
 * 看起来「像不像一份 PRD」的粗略提示。
 * 只是提示，不拦提交：用户的 PRD 长什么样不该由我们规定。
 */
const looksLikeMarkdown = computed(() => /^#{1,3}\s/m.test(props.prdText))

/** 清空两份材料 */
function clearAll(): void {
  emit('update:prdText', '')
  emit('update:apiDocsText', '')
}

/** 把过长的正文收成一行摘要，用于确认条 */
function previewLine(text: string): string {
  const firstLine = text.trim().split('\n')[0] ?? ''
  return firstLine.replace(/^#+\s*/, '').slice(0, 40)
}
</script>

<template>
  <section class="space-y-4">
    <header>
      <h1 class="text-lg font-bold text-fg">从现有 PRD 与接口文档生成提示词套件</h1>
      <p class="mt-1 text-sm text-subtle">
        两份材料都贴进来，提示词会同时参考它们——涉及的接口路径与字段名因而与真实契约对齐。
        只有 PRD 是必填；接口文档没有也可以生成，只是提示词只能依据 PRD。
      </p>
    </header>

    <div class="space-y-4 rounded-2xl border border-line bg-surface p-5 shadow-sm">
      <div class="grid gap-4 sm:grid-cols-2">
        <!-- 左：PRD（必填） -->
        <div>
          <label for="field-pair-prd" class="mb-1.5 block text-xs font-medium text-muted">
            PRD 正文（Markdown）
            <span class="ml-1 text-rose-500">*</span>
          </label>
          <textarea
            id="field-pair-prd"
            ref="prdRef"
            rows="14"
            class="w-full resize-y rounded-lg border px-3 py-2 font-mono text-xs leading-relaxed text-fg outline-none transition placeholder:text-subtle focus:ring-2"
            :class="
              prdMissing
                ? 'border-line-strong focus:border-accent focus:ring-accent-soft-line'
                : 'border-accent focus:border-accent focus:ring-accent-soft-line'
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

        <!-- 右：接口文档（选填） -->
        <div>
          <label for="field-pair-api-docs" class="mb-1.5 block text-xs font-medium text-muted">
            接口文档正文（Markdown）
            <span class="ml-1 text-subtle">选填</span>
          </label>
          <textarea
            id="field-pair-api-docs"
            rows="14"
            class="w-full resize-y rounded-lg border border-line-strong px-3 py-2 font-mono text-xs leading-relaxed text-fg outline-none transition placeholder:text-subtle focus:border-accent focus:ring-2 focus:ring-accent-soft-line"
            placeholder="# 小账本 接口文档&#10;&#10;## 1. 记账&#10;POST /api/ledgers&#10;…"
            :value="apiDocsText"
            @input="emit('update:apiDocsText', ($event.target as HTMLTextAreaElement).value)"
          ></textarea>

          <div class="mt-1.5 flex flex-wrap items-start justify-between gap-2">
            <p v-if="hasApiDocs" class="text-xs text-subtle">
              提示词里涉及的接口路径与字段名会与它保持一致。
            </p>
            <p v-else class="text-xs text-subtle">
              留空即按「只有 PRD」生成——和上一条入口（我有 PRD）效果一致。
            </p>
            <span class="shrink-0 text-xs text-subtle">{{ apiDocsText.length }} 字</span>
          </div>
        </div>
      </div>

      <!-- 已录入内容的确认条：给一个「我刚贴进来的是什么」的回执 -->
      <p
        v-if="prdText.trim()"
        class="rounded-lg border border-line bg-surface-2 px-3 py-2 text-xs text-subtle"
      >
        已录入：{{ previewLine(prdText) }}{{ prdText.length > 40 ? '…' : '' }}
        <span v-if="hasApiDocs" class="text-muted">
          ｜接口文档：{{ previewLine(apiDocsText) }}{{ apiDocsText.length > 40 ? '…' : '' }}
        </span>
        <span v-else class="text-muted">｜接口文档：未提供</span>
      </p>
    </div>

    <!-- 页面级操作栏：左「清空」│「返回入口」，右主操作 -->
    <AppActionBar>
      <template #danger>
        <AppButton
          variant="danger"
          size="md"
          :disabled="!prdText && !apiDocsText"
          @click="clearAll"
        >
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
