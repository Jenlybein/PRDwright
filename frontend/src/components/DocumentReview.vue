<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { renderMarkdown } from '@/utils/markdown'

/**
 * 文档审阅组件。
 *
 * 生成中只读展示流式内容；生成完成后切换为可编辑模式，支持「预览 / 编辑」两种视图一键切换。
 *
 * **不再自带底部操作栏**：AI 优化 / 复制 / 保存这三个工具，与「这份文档该往哪走」的流程操作
 * 一起收在页面级底栏（`AppActionBar`）里——此前卡片自带一条底栏、页面又垫一条，两条叠在一起
 * 中间还断开，用户得上下两处找按钮。工具按钮改由上层渲染，所以这里用 `defineExpose` 把
 * 「状态 + 动作」交出去，本组件只负责卡片本身（含 AI 优化的指令输入，见 `ReviewStage`）。
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
    /**
     * 流式内容是否为「上一版草稿」。
     *
     * 章节被判为需要改写时会重流整章，中间隔着一次完整的模型往返；那段时间里
     * 展示的是改写前的草稿，需要明确标出来，否则用户会以为文档出了故障。
     */
    draftOnly?: boolean
  }>(),
  {
    title: '',
    content: '',
    streamingContent: '',
    generating: false,
    draftOnly: false,
  },
)

const emit = defineEmits<{
  /** 点击底栏「保存」时触发，载荷为当前可编辑原文 */
  save: [content: string]
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

/** 原文是否被改过（相对上一次落地的 `content`）；没改过时底栏的「保存」保持禁用 */
const dirty = computed(() => draft.value !== props.content)

// ---------------------------------------------------------------------------
// 操作
// ---------------------------------------------------------------------------

/** 一键复制当前原文到剪贴板 */
async function copy(): Promise<void> {
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

/** 保存：把当前原文回传给父组件 */
function save(): void {
  emit('save', draft.value)
}

/**
 * 工具行的按钮渲染在上层底栏里，因此这里把「状态 + 动作」暴露给 `ReviewStage`：
 * `busy` / `dirty` 决定按钮禁用态，`copyTip` 决定按钮文案，`draftContent` 是 AI 优化要取的载荷。
 */
defineExpose({
  /** 生成中文档工具应处于禁用态 */
  busy: computed(() => props.generating),
  /** 原文是否被改过（决定「保存」是否可点） */
  dirty,
  /** 复制反馈文案（空串表示无提示） */
  copyTip,
  /** 当前可编辑原文 */
  draftContent: computed(() => draft.value),
  /** 一键复制 */
  copy,
  /** 保存当前原文 */
  save,
})
</script>

<template>
  <div class="flex flex-col overflow-hidden rounded-2xl border border-line bg-surface shadow-sm">
    <!-- 头部：标题 + 状态徽章 + 视图切换 -->
    <div class="flex items-center justify-between gap-3 border-b border-line px-4 py-3 sm:px-6">
      <div class="flex min-w-0 items-center gap-2">
        <h2 class="truncate text-sm font-semibold text-fg">{{ title || '文档' }}</h2>
        <span
          v-if="generating"
          class="inline-flex items-center gap-1 rounded-full bg-accent-soft px-2 py-0.5 text-xs font-medium text-accent ring-1 ring-accent-soft-line"
        >
          <span class="h-1.5 w-1.5 animate-pulse rounded-full bg-primary-500"></span>
          生成中
        </span>
      </div>

      <!-- 视图切换：仅在非生成态可用 -->
      <div v-if="!generating" class="flex shrink-0 items-center gap-1 rounded-lg bg-surface-2 p-0.5">
        <button
          type="button"
          class="rounded-md px-2.5 py-1 text-xs font-medium transition"
          :class="viewMode === 'preview' ? 'bg-surface text-fg shadow-sm' : 'text-subtle hover:text-muted'"
          @click="viewMode = 'preview'"
        >
          预览
        </button>
        <button
          type="button"
          class="rounded-md px-2.5 py-1 text-xs font-medium transition"
          :class="viewMode === 'edit' ? 'bg-surface text-fg shadow-sm' : 'text-subtle hover:text-muted'"
          @click="viewMode = 'edit'"
        >
          编辑
        </button>
      </div>
    </div>

    <!-- 正文区域 -->
    <div class="flex-1 overflow-y-auto bg-surface-2/40 p-4 dark:bg-bg/40 sm:p-6">
      <!-- 空状态 -->
      <div
        v-if="!hasContent"
        class="flex h-full min-h-[200px] items-center justify-center text-sm text-subtle"
      >
        {{ generating ? '正在准备文档…' : '暂无内容' }}
      </div>

      <!-- 生成中：只读展示流式内容（Markdown 实时渲染） -->
      <div v-else-if="generating" class="space-y-2">
        <!--
          改写期间旧草稿不撤下、只降透明度：直接清空会让用户看到「这一章突然没了」，
          而按设计这里想表达的是「正在重写」。新内容一流出就会被顶替，不会并存。
        -->
        <p v-if="draftOnly" class="flex items-center gap-1.5 text-xs text-muted">
          <span class="h-1.5 w-1.5 animate-pulse rounded-full bg-amber-500"></span>
          以下为上一版草稿，正在按审核意见重写…
        </p>
        <div
          class="prose prose-sm prose-slate max-w-none rounded-xl bg-surface px-5 py-4 shadow-sm ring-1 ring-line dark:prose-invert"
          :class="draftOnly ? 'opacity-60' : ''"
        >
          <!-- eslint-disable-next-line vue/no-v-html -- 内容经 markdown-it 渲染且已关闭原始 HTML -->
          <div v-html="renderMarkdown(streamingContent)" />
          <span
            class="inline-block h-4 w-1.5 animate-pulse rounded-sm bg-primary-500 align-text-bottom"
          ></span>
        </div>
      </div>

      <!-- 生成完成：预览 / 编辑 切换 -->
      <template v-else>
        <!-- 预览视图：渲染缓存后的 HTML -->
        <div
          v-if="viewMode === 'preview'"
          class="prose prose-sm prose-slate max-w-none rounded-xl bg-surface px-5 py-4 shadow-sm ring-1 ring-line dark:prose-invert"
        >
          <!-- eslint-disable-next-line vue/no-v-html -- 内容经 markdown-it 渲染且已关闭原始 HTML -->
          <div v-html="renderedHtml" />
        </div>

        <!-- 编辑视图：可编辑原文 -->
        <textarea
          v-else
          v-model="draft"
          rows="20"
          class="min-h-[320px] w-full resize-y rounded-xl border border-line bg-surface px-4 py-3 font-mono text-sm leading-relaxed text-fg shadow-sm outline-none transition-colors focus:border-accent focus:ring-2 focus:ring-accent-soft-line"
          placeholder="在此编辑文档的 Markdown 原文"
        ></textarea>
      </template>
    </div>
  </div>
</template>
