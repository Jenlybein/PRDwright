<script setup lang="ts">
import { computed, ref } from 'vue'

import DocumentReview from '@/components/DocumentReview.vue'
import AppActionBar from '@/components/ui/AppActionBar.vue'
import AppButton from '@/components/ui/AppButton.vue'
import type { ViewState } from '@/types'

/**
 * 文档「待确认」阶段视图（PRD / 接口文档 / 提示词共用）。
 *
 * 三份文档的审阅区完全一致，差异只在底栏右端的后续操作：按顺序推进（PRD → 接口文档 →
 * 提示词）、回看已生成的其它文档、三份齐备后进入完成页。这些差异由 `viewState`
 * 与三个布尔量驱动，组件自身不持有流程状态。
 *
 * ## 底栏两行，各自职责单一
 *
 * - **内容行**：这份文档自己的工具——AI 优化 / 复制 / 保存，以及「预览 / 编辑」视图切换。
 *   点「AI 优化」后原地换成指令输入，文档全程可见（改成弹窗就看不到了，而写优化指令时恰恰要
 *   对着文档）。
 * - **流程行**：这份文档之后往哪走——返回、生成下一份、完成。
 *
 * 工具行的按钮是「对内容做的事」，但草稿、复制反馈、视图模式这些状态天然属于 `DocumentReview`，
 * 所以用 `ref` 取它的句柄来渲染；指令输入本身是底栏的 UI 状态，留在本组件里。
 *
 * 「预览 / 编辑」最初挂在文档卡片头部，与卡片底部的工具条隔着整篇正文——同一份文档的控件
 * 散在上下两处。现在收进底栏内容行，与文档工具排在一行，符合「文档相关的控件都在底栏」的约定。
 */
const emit = defineEmits<{
  /** 保存编辑后的正文 */
  save: [content: string]
  /** 提交 AI 优化指令 */
  optimize: [payload: { content: string; instruction: string }]
  /** 返回对话 */
  back: []
  /** 进入接口文档生成（先检索团队规范） */
  generateApiDocs: []
  /** 进入提示词套件生成 */
  generatePrompts: []
  /** 跳到某个文档的审阅阶段 */
  navigate: [state: ViewState]
  /** 进入完成页 */
  finish: []
}>()

defineProps<{
  /** 文档标题 */
  title: string
  /** 文档正文（Markdown 源码） */
  content: string
  /** 优化失败等非正常结尾的原因 */
  error?: string
  /** 当前所处的审阅阶段 */
  viewState: ViewState
  /** 所需文档齐备且无请求进行中，可进入完成页 */
  canFinish: boolean
  /** 是否已有 PRD 正文（决定「查看 PRD」入口是否出现） */
  hasPrd: boolean
  /** 是否已有接口文档正文（决定「查看接口文档」入口是否出现） */
  hasApiDocs: boolean
  /** 是否已有提示词正文（决定「查看提示词」入口是否出现） */
  hasPrompts: boolean
  /**
   * 「返回」按钮的文案。
   * 返回的目标随入口模式而变（完整流程回对话，快捷入口回描述，调试入口回 PRD），
   * 因此文案由上层给，组件不猜。
   */
  backLabel?: string
}>()

/** 文档组件句柄：底栏工具行要读它的草稿 / 复制反馈 / 视图模式，并调它的复制、保存、切换视图 */
const doc = ref<InstanceType<typeof DocumentReview> | null>(null)

/**
 * 当前文档视图模式（`preview` / `edit`）。
 *
 * 状态由 `DocumentReview` 持有（草稿也在那里），这里只读它来渲染底栏分段控件的高亮态；
 * 组件尚未挂载时按 `preview` 兜底，避免两个分段按钮同时不亮。
 */
const viewMode = computed(() => doc.value?.viewMode ?? 'preview')

/** 是否把底栏内容行切成「优化指令输入」形态 */
const optimizing = ref(false)

/** 用户输入的优化指令 */
const instruction = ref('')

/** 展开指令输入；没有正文时不展开（没有可优化的内容） */
function openOptimize(): void {
  if (!doc.value?.draftContent.trim()) return
  optimizing.value = true
}

/** 收起指令输入并清空指令 */
function cancelOptimize(): void {
  optimizing.value = false
  instruction.value = ''
}

/** 提交优化指令：原文与指令一起交给上层 */
function submitOptimize(): void {
  const text = instruction.value.trim()
  if (!text || !doc.value) return
  emit('optimize', { content: doc.value.draftContent, instruction: text })
}
</script>

<template>
  <section class="space-y-3">
    <!-- 优化失败等非正常结尾时展示原因（原文档内容保留不变） -->
    <p
      v-if="error"
      class="flex items-start gap-2 rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-700 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-300"
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

    <!-- 文档卡片：只有头部与正文，工具全部下沉到下面的底栏 -->
    <DocumentReview
      ref="doc"
      :title="title"
      :content="content"
      @save="emit('save', $event)"
    />

    <AppActionBar>
      <!-- 内容行：常态是文档工具；点「AI 优化」原地变成指令输入，文档始终可见 -->
      <template #tools>
        <div v-if="optimizing" class="flex flex-wrap items-center gap-2">
          <input
            id="document-optimize-instruction"
            v-model="instruction"
            type="text"
            class="min-w-0 flex-1 rounded-control border border-line-strong bg-field px-3 py-1.5 text-xs text-fg outline-none transition-colors placeholder:text-subtle focus:border-accent focus:ring-2 focus:ring-accent-soft-line"
            placeholder="告诉 AI 要怎么改，例如：语气更正式 / 压缩到 500 字以内 / 把未确认的技术选型改为待确认"
            @keydown.enter.prevent="submitOptimize"
            @keydown.esc.prevent="cancelOptimize"
          />
          <AppButton
            variant="primary"
            size="sm"
            :disabled="!instruction.trim()"
            @click="submitOptimize"
          >
            开始优化
          </AppButton>
          <AppButton variant="ghost" size="sm" @click="cancelOptimize">取消</AppButton>
        </div>

        <div v-else class="flex flex-wrap items-center justify-between gap-2">
          <div class="flex flex-wrap items-center gap-2">
            <AppButton
              variant="secondary"
              size="sm"
              :disabled="doc?.busy"
              @click="openOptimize"
            >
              <svg class="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                <path
                  d="M11.3 1.046A1 1 0 0112 2v5h4a1 1 0 01.82 1.573l-7 10A1 1 0 018 18v-5H4a1 1 0 01-.82-1.573l7-10a1 1 0 011.12-.38z"
                />
              </svg>
              AI 优化
            </AppButton>

            <AppButton variant="secondary" size="sm" :disabled="doc?.busy" @click="doc?.copy()">
              <svg class="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                <path d="M7 9a2 2 0 012-2h6a2 2 0 012 2v6a2 2 0 01-2 2H9a2 2 0 01-2-2V9z" />
                <path d="M5 3a2 2 0 00-2 2v6a2 2 0 002 2h1V7a2 2 0 012-2h6V5a2 2 0 00-2-2H5z" />
              </svg>
              {{ doc?.copyTip || '复制' }}
            </AppButton>
          </div>

          <!-- 右端：视图切换 + 保存。两者都作用于「这份文档」，排在一起 -->
          <div class="flex items-center gap-2">
            <!-- 预览 / 编辑：从文档卡片头部下沉过来，与文档工具同处底栏 -->
            <div
              class="flex shrink-0 items-center gap-1 rounded-lg bg-surface-2 p-0.5 ring-1 ring-line"
              role="group"
              aria-label="文档视图"
            >
              <button
                type="button"
                class="rounded-md px-2.5 py-1 text-xs font-medium transition"
                :class="
                  viewMode === 'preview' ? 'bg-surface text-fg shadow-sm' : 'text-subtle hover:text-muted'
                "
                @click="doc?.setViewMode('preview')"
              >
                预览
              </button>
              <button
                type="button"
                class="rounded-md px-2.5 py-1 text-xs font-medium transition"
                :class="
                  viewMode === 'edit' ? 'bg-surface text-fg shadow-sm' : 'text-subtle hover:text-muted'
                "
                @click="doc?.setViewMode('edit')"
              >
                编辑
              </button>
            </div>

            <!-- 保存：只要文档有正文、且没有生成在跑，就可点。
                 曾按「原文被改过」（dirty）来决定禁用态，结果是**刚生成完的那一刻按钮是灰的**——
                 用户把文档生成出来了却存不下去，观感上像功能坏了（自动落库是后台行为，用户看不到）。
                 现在改为「有内容即可保存」，点一下至少能拿到明确的「已保存」回执。 -->
            <AppButton
              variant="secondary"
              size="sm"
              :disabled="doc?.busy || !doc?.hasContent"
              @click="doc?.save()"
            >
              保存
            </AppButton>
          </div>
        </div>
      </template>

      <!-- 流程行：左「返回」，右按顺序推进 / 查看其它文档 / 完成 -->
      <template #secondary>
        <AppButton variant="secondary" size="md" @click="emit('back')">
          {{ backLabel ?? '返回对话' }}
        </AppButton>
      </template>

      <!-- 各阶段按顺序推进：PRD 完成 → 生成接口文档；接口文档完成 → 生成提示词套件 -->
      <AppButton
        v-if="viewState === 'review-prd'"
        variant="secondary"
        size="md"
        class="!border-accent !text-primary-700 hover:!bg-primary-50 dark:!border-primary-700 dark:!text-primary-300 dark:hover:!bg-primary-950/40"
        @click="emit('generateApiDocs')"
      >
        生成接口文档
      </AppButton>

      <AppButton
        v-else-if="viewState === 'review-api-docs'"
        variant="secondary"
        size="md"
        class="!border-accent !text-primary-700 hover:!bg-primary-50 dark:!border-primary-700 dark:!text-primary-300 dark:hover:!bg-primary-950/40"
        @click="emit('generatePrompts')"
      >
        生成提示词套件
      </AppButton>

      <!-- 其它已生成的文档的查看入口（当前阶段除外）。
           没有正文的文档不出现入口：点进去只会看到一片空白 -->
      <AppButton
        v-if="viewState !== 'review-prd' && hasPrd"
        variant="secondary"
        size="md"
        @click="emit('navigate', 'review-prd')"
      >
        查看 PRD
      </AppButton>
      <AppButton
        v-if="viewState !== 'review-api-docs' && hasApiDocs"
        variant="secondary"
        size="md"
        @click="emit('navigate', 'review-api-docs')"
      >
        查看接口文档
      </AppButton>
      <AppButton
        v-if="viewState !== 'review-prompts' && hasPrompts"
        variant="secondary"
        size="md"
        @click="emit('navigate', 'review-prompts')"
      >
        查看提示词
      </AppButton>

      <!-- 完成入口：三份文档齐备才可用 -->
      <AppButton variant="primary" size="md" :disabled="!canFinish" @click="emit('finish')">
        完成
      </AppButton>
    </AppActionBar>
  </section>
</template>
