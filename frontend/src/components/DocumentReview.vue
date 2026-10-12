<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { renderMarkdown } from '@/utils/markdown'

/**
 * 文档审阅组件。
 *
 * 生成中只读展示流式内容；生成完成后切换为可编辑模式，支持「预览 / 编辑」
 * 两种视图一键切换、AI 优化、一键复制，底部提供自定义操作按钮。
 *
 * Markdown 渲染做了缓存：只有当原文（可编辑的 draft）真正发生变化时才重新
 * 调用 `renderMarkdown`，流式滚动刷新或反复切换视图不会触发重复解析。
 */
const props = withDefaults(
  defineProps<{
    /** 文档标题，展示在头部 */
    title?: string
    /** 文档最终正文（Markdown 源码）；生成完成后据此初始化编辑内容 */
    content?: string
    /** 流式生成中的增量内容（只读展示） */
    streamingContent?: string
    /** 是否正在生成中；为 true 时展示只读流式内容 */
    generating?: boolean
  }>(),
  {
    title: '',
    content: '',
    streamingContent: '',
    generating: false,
  },
)

const emit = defineEmits<{
  /** 提交 AI 优化指令时触发，载荷为当前可编辑原文与用户输入的指令 */
  'ai-optimize': [payload: { content: string; instruction: string }]
  /** 点击底部「保存」按钮时触发，载荷为当前可编辑原文 */
  save: [content: string]
  /** 底部自定义按钮被点击时触发，载荷为按钮标识 */
  action: [action: string]
}>()

// ---------------------------------------------------------------------------
// 可编辑原文（draft）
// ---------------------------------------------------------------------------

/**
 * 当前可编辑的文档原文。
 * 生成完成后用 `content` 初始化；用户编辑时直接改这个 ref，仅在保存时才
 * 通过 `save` 事件回传，避免父组件在输入过程中被频繁触发。
 */
const draft = ref(props.content)

// 生成完成（content 落地）时同步到可编辑区
watch(
  () => props.content,
  (value) => {
    draft.value = value
  },
)

/** 是否处于「编辑原文」视图；false 时为 Markdown 预览 */
const viewMode = ref<'preview' | 'edit'>('preview')

/** 复制成功 / 失败的提示文案 */
const copyTip = ref('')

// ---------------------------------------------------------------------------
// Markdown 渲染（带缓存：仅在 draft 变化时才重新渲染）
// ---------------------------------------------------------------------------

/** 缓存后的 HTML，避免在未修改原文时反复解析 */
const renderedHtml = ref('')

// 只有 draft 真正变化时才重算，满足「原文被修改才重新触发渲染」的要求
watch(
  draft,
  (value) => {
    renderedHtml.value = renderMarkdown(value)
  },
  { immediate: true },
)

/** 当前是否已有正文内容（流式内容或已落地的原文） */
const hasContent = computed(() => {
  if (props.generating) return props.streamingContent.trim().length > 0
  return draft.value.trim().length > 0
})

// ---------------------------------------------------------------------------
// 操作
// ---------------------------------------------------------------------------

/** 一键复制当前原文到剪贴板 */
async function copyContent(): Promise<void> {
  const text = props.generating ? props.streamingContent : draft.value
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
    copyTip.value = '已复制'
  } catch {
    // 剪贴板不可用（如非安全上下文）时降级为选中提示，不打断流程
    copyTip.value = '复制失败，请手动选择文本'
  }
  window.setTimeout(() => {
    copyTip.value = ''
  }, 2000)
}

/** 是否展开优化指令输入区 */
const showOptimize = ref(false)

/** 用户输入的优化指令 */
const optimizeInstruction = ref('')

/**
 * 展开优化指令输入区。
 *
 * 此前用浏览器原生 `prompt()` 收集指令，样式脱离页面、移动端体验差，
 * 且在 jsdom 里无法直接测试；改为页内输入区。
 */
function requestOptimize(): void {
  if (!draft.value.trim()) return
  showOptimize.value = true
}

/** 提交优化指令：把原文与指令一起交给父组件 */
function submitOptimize(): void {
  const instruction = optimizeInstruction.value.trim()
  if (!instruction || props.generating) return
  emit('ai-optimize', { content: draft.value, instruction })
}

/** 收起优化输入区并清空指令 */
function cancelOptimize(): void {
  showOptimize.value = false
  optimizeInstruction.value = ''
}

// 优化结束后（无论成功或失败）自动收起输入区，避免指令残留导致误触重复提交
watch(
  () => props.generating,
  (now, before) => {
    if (before && !now) cancelOptimize()
  },
)

/** 保存：把当前原文回传给父组件 */
function save(): void {
  emit('save', draft.value)
}

/** 底部自定义按钮：把按钮标识抛给父组件 */
function runAction(action: string): void {
  emit('action', action)
}
</script>

<template>
  <div class="flex flex-col overflow-hidden rounded-2xl border border-slate-200/80 bg-white shadow-sm">
    <!-- 头部：标题 + 状态徽章 + 视图切换 -->
    <div class="flex items-center justify-between gap-3 border-b border-slate-200 px-4 py-3 sm:px-6">
      <div class="flex min-w-0 items-center gap-2">
        <h2 class="truncate text-sm font-semibold text-slate-900">{{ title || '文档' }}</h2>
        <span
          v-if="generating"
          class="inline-flex items-center gap-1 rounded-full bg-primary-50 px-2 py-0.5 text-xs font-medium text-primary-700 ring-1 ring-primary-200"
        >
          <span class="h-1.5 w-1.5 animate-pulse rounded-full bg-primary-500"></span>
          生成中
        </span>
      </div>

      <!-- 视图切换：仅在非生成态可用 -->
      <div v-if="!generating" class="flex shrink-0 items-center gap-1 rounded-lg bg-slate-100 p-0.5">
        <button
          type="button"
          class="rounded-md px-2.5 py-1 text-xs font-medium transition"
          :class="viewMode === 'preview' ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-500 hover:text-slate-700'"
          @click="viewMode = 'preview'"
        >
          预览
        </button>
        <button
          type="button"
          class="rounded-md px-2.5 py-1 text-xs font-medium transition"
          :class="viewMode === 'edit' ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-500 hover:text-slate-700'"
          @click="viewMode = 'edit'"
        >
          编辑
        </button>
      </div>
    </div>

    <!-- 正文区域 -->
    <div class="flex-1 overflow-y-auto bg-slate-50/40 p-4 sm:p-6">
      <!-- 空状态 -->
      <div
        v-if="!hasContent"
        class="flex h-full min-h-[200px] items-center justify-center text-sm text-slate-400"
      >
        {{ generating ? '正在准备文档…' : '暂无内容' }}
      </div>

      <!-- 生成中：只读展示流式内容（Markdown 实时渲染） -->
      <div
        v-else-if="generating"
        class="prose prose-sm prose-slate max-w-none rounded-xl bg-white px-5 py-4 shadow-sm ring-1 ring-slate-200/70"
      >
        <!-- eslint-disable-next-line vue/no-v-html -- 内容经 markdown-it 渲染且已关闭原始 HTML -->
        <div v-html="renderMarkdown(streamingContent)" />
        <span class="inline-block h-4 w-1.5 animate-pulse rounded-sm bg-primary-500 align-text-bottom"></span>
      </div>

      <!-- 生成完成：预览 / 编辑 切换 -->
      <template v-else>
        <!-- 预览视图：渲染缓存后的 HTML -->
        <div
          v-if="viewMode === 'preview'"
          class="prose prose-sm prose-slate max-w-none rounded-xl bg-white px-5 py-4 shadow-sm ring-1 ring-slate-200/70"
        >
          <!-- eslint-disable-next-line vue/no-v-html -- 内容经 markdown-it 渲染且已关闭原始 HTML -->
          <div v-html="renderedHtml" />
        </div>

        <!-- 编辑视图：可编辑原文 -->
        <textarea
          v-else
          v-model="draft"
          rows="20"
          class="min-h-[320px] w-full resize-y rounded-xl border border-slate-200 bg-white px-4 py-3 font-mono text-sm leading-relaxed text-slate-800 shadow-sm outline-none transition-colors focus:border-primary-400 focus:ring-2 focus:ring-primary-100"
          placeholder="在此编辑文档的 Markdown 原文"
        ></textarea>
      </template>
    </div>

    <!-- AI 优化指令输入区：点击「AI 优化」后展开 -->
    <div v-if="showOptimize" class="border-t border-primary-200 bg-primary-50/70 px-4 py-3 sm:px-6">
      <label
        for="document-optimize-instruction"
        class="mb-1.5 block text-xs font-medium text-primary-900"
      >
        告诉 AI 要怎么改，例如：语气更正式 / 压缩到 500 字以内 / 把未确认的技术选型改为待确认
      </label>
      <textarea
        id="document-optimize-instruction"
        v-model="optimizeInstruction"
        rows="2"
        :disabled="generating"
        class="w-full resize-y rounded-lg border border-primary-200 bg-white px-3 py-2 text-sm leading-relaxed text-slate-800 shadow-sm outline-none transition-colors focus:border-primary-400 focus:ring-2 focus:ring-primary-100 disabled:bg-slate-50 disabled:text-slate-400"
        placeholder="输入优化指令，Ctrl / ⌘ + Enter 直接提交"
        @keydown.ctrl.enter.prevent="submitOptimize"
        @keydown.meta.enter.prevent="submitOptimize"
      ></textarea>
      <div class="mt-2 flex flex-wrap items-center gap-2">
        <button
          type="button"
          :disabled="generating || !optimizeInstruction.trim()"
          class="rounded-lg bg-gradient-to-r from-primary-500 to-primary-600 px-3 py-1.5 text-xs font-medium text-white shadow-sm transition hover:from-primary-600 hover:to-primary-700 disabled:cursor-not-allowed disabled:from-slate-200 disabled:to-slate-300 disabled:text-slate-400"
          @click="submitOptimize"
        >
          {{ generating ? '正在优化…' : '开始优化' }}
        </button>
        <button
          type="button"
          :disabled="generating"
          class="rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-600 transition hover:border-slate-400 disabled:cursor-not-allowed disabled:opacity-50"
          @click="cancelOptimize"
        >
          取消
        </button>
      </div>
    </div>

    <!-- 底部操作栏 -->
    <div class="flex flex-wrap items-center justify-between gap-2 border-t border-slate-200 bg-white px-4 py-3 sm:px-6">
      <!-- 左侧：AI 优化 + 复制 -->
      <div class="flex items-center gap-2">
        <button
          type="button"
          :disabled="generating"
          class="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-600 transition hover:border-primary-400 hover:text-primary-700 disabled:cursor-not-allowed disabled:opacity-50"
          @click="requestOptimize"
        >
          <svg class="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
            <path
              d="M11.3 1.046A1 1 0 0112 2v5h4a1 1 0 01.82 1.573l-7 10A1 1 0 018 18v-5H4a1 1 0 01-.82-1.573l7-10a1 1 0 011.12-.38z"
            />
          </svg>
          AI 优化
        </button>
        <button
          type="button"
          class="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-600 transition hover:border-primary-400 hover:text-primary-700"
          @click="copyContent"
        >
          <svg class="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
            <path
              d="M7 9a2 2 0 012-2h6a2 2 0 012 2v6a2 2 0 01-2 2H9a2 2 0 01-2-2V9z"
            />
            <path d="M5 3a2 2 0 00-2 2v6a2 2 0 002 2h1V7a2 2 0 012-2h6V5a2 2 0 00-2-2H5z" />
          </svg>
          {{ copyTip || '复制' }}
        </button>
      </div>

      <!-- 右侧：自定义操作按钮（父组件通过默认插槽注入） -->
      <div class="flex items-center gap-2">
        <slot name="actions" :run-action="runAction" :save="save" :content="draft">
          <button
            type="button"
            :disabled="generating"
            class="rounded-lg bg-gradient-to-r from-primary-500 to-primary-600 px-4 py-1.5 text-xs font-medium text-white shadow-sm transition hover:from-primary-600 hover:to-primary-700 disabled:cursor-not-allowed disabled:from-slate-200 disabled:to-slate-300 disabled:text-slate-400"
            @click="save"
          >
            保存
          </button>
        </slot>
      </div>
    </div>
  </div>
</template>
