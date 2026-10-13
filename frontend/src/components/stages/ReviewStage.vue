<script setup lang="ts">
import DocumentReview from '@/components/DocumentReview.vue'
import AppButton from '@/components/ui/AppButton.vue'
import type { ViewState } from '@/types'

/**
 * 文档「待确认」阶段视图（PRD / 接口文档 / 提示词共用）。
 *
 * 三份文档的审阅区完全一致，差异只在底部后续操作：按顺序推进（PRD → 接口文档 →
 * 提示词）、回看已生成的其它文档、三份齐备后进入完成页。这些差异由 `viewState`
 * 与三个布尔量驱动，组件自身不持有流程状态。
 */
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

    <DocumentReview
      :title="title"
      :content="content"
      @save="emit('save', $event)"
      @ai-optimize="emit('optimize', $event)"
    >
      <template #actions="{ save }">
        <!-- 保存 -->
        <AppButton variant="primary" size="sm" @click="save">保存</AppButton>
      </template>
    </DocumentReview>

    <!-- 各文档对应的后续操作 -->
    <div class="flex flex-wrap items-center justify-between gap-2">
      <AppButton variant="secondary" size="sm" @click="emit('back')">
        {{ backLabel ?? '返回对话' }}
      </AppButton>

      <div class="flex flex-wrap items-center gap-2">
        <!-- 各阶段按顺序推进：PRD 完成 → 生成接口文档；接口文档完成 → 生成提示词套件 -->
        <AppButton
          v-if="viewState === 'review-prd'"
          variant="secondary"
          size="sm"
          class="!border-primary-300 !text-primary-700 hover:!bg-primary-50 dark:!border-primary-700 dark:!text-primary-300 dark:hover:!bg-primary-950/40"
          @click="emit('generateApiDocs')"
        >
          生成接口文档
        </AppButton>

        <AppButton
          v-else-if="viewState === 'review-api-docs'"
          variant="secondary"
          size="sm"
          class="!border-primary-300 !text-primary-700 hover:!bg-primary-50 dark:!border-primary-700 dark:!text-primary-300 dark:hover:!bg-primary-950/40"
          @click="emit('generatePrompts')"
        >
          生成提示词套件
        </AppButton>

        <!-- 其它已生成的文档的查看入口（当前阶段除外）。
             没有正文的文档不出现入口：点进去只会看到一片空白 -->
        <AppButton
          v-if="viewState !== 'review-prd' && hasPrd"
          variant="secondary"
          size="sm"
          @click="emit('navigate', 'review-prd')"
        >
          查看 PRD
        </AppButton>
        <AppButton
          v-if="viewState !== 'review-api-docs' && hasApiDocs"
          variant="secondary"
          size="sm"
          @click="emit('navigate', 'review-api-docs')"
        >
          查看接口文档
        </AppButton>
        <AppButton
          v-if="viewState !== 'review-prompts' && hasPrompts"
          variant="secondary"
          size="sm"
          @click="emit('navigate', 'review-prompts')"
        >
          查看提示词
        </AppButton>

        <!-- 完成入口：三份文档齐备才可用 -->
        <AppButton variant="primary" size="sm" :disabled="!canFinish" @click="emit('finish')">
          完成
        </AppButton>
      </div>
    </div>
  </section>
</template>
