<script setup lang="ts">
import { computed } from 'vue'

import type { ViewState } from '@/types'
import type { DocType } from '@/utils/download'
import AppButton from '@/components/ui/AppButton.vue'

/**
 * 全部完成阶段视图。
 *
 * 三份文档的卡片结构一致、只有图标与配色不同，因此用一份配置数组驱动渲染，
 * 避免三段几乎相同的模板各自漂移。
 *
 * 不同入口模式的交付物数量不同（完整流程三份、快捷入口只有 PRD、调试入口只有
 * 提示词套件），因此空卡片会被过滤掉，标题也按实际数量措辞——不留「未生成」的
 * 空壳卡片，也就不需要解释为什么它是空的。
 */
const props = defineProps<{
  /** PRD 正文，用于展示字符数 */
  prdContent: string
  /** 接口文档正文，用于展示字符数 */
  apiDocsContent: string
  /** 提示词套件正文，用于展示字符数 */
  promptsContent: string
  /** 「返回」按钮的文案（返回目标随入口模式而变，由上层决定） */
  backLabel?: string
}>()

const emit = defineEmits<{
  /** 跳到某份文档的审阅阶段 */
  view: [state: ViewState]
  /** 下载某份文档 */
  download: [docType: DocType]
  /** 返回对话 */
  back: []
  /** 重新开始 */
  restart: []
}>()

/** 一张文档卡片的渲染配置 */
interface DoneCard {
  docType: DocType
  title: string
  state: ViewState
  iconWrap: string
  iconColor: string
  iconPath: string
  length: number
}

/** 三份文档卡片 */
const ALL_CARDS = computed<DoneCard[]>(() => [
  {
    docType: 'prd',
    title: 'PRD 文档',
    state: 'review-prd',
    iconWrap: 'bg-primary-50 dark:bg-primary-950/40',
    iconColor: 'text-primary-600',
    iconPath:
 'M4 4a2 2 0 0 1 2-2h5.586a1 1 0 0 1 .707.293L15.707 5.5a1 1 0 0 1 .293.707V16a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4Zm7 1.5V7h1.5L11 5.5ZM7 9a1 1 0 0 0 0 2h6a1 1 0 1 0 0-2H7Zm0 3a1 1 0 1 0 0 2h6a1 1 0 1 0 0-2H7Z',
    length: props.prdContent.length,
  },
  {
    docType: 'api-docs',
    title: '接口文档',
    state: 'review-api-docs',
    iconWrap: 'bg-primary-50 dark:bg-primary-950/40',
    iconColor: 'text-primary-600',
    iconPath:
 'M2 4.75A2.75 2.75 0 0 1 4.75 2h6.5A.75.75 0 0 1 12 2v3.25h3.25a.75.75 0 0 1 .75.75v9.25A2.75 2.75 0 0 1 13.25 18h-8.5A2.75 2.75 0 0 1 2 15.25V4.75Zm8.5 3.5h3.5v-2.5h-3.5v2.5ZM6 7.75A.75.75 0 0 0 5.25 8.5v3a.75.75 0 0 0 1.5 0v-3A.75.75 0 0 0 6 7.75Zm3 0a.75.75 0 0 0-.75.75v3a.75.75 0 0 0 1.5 0v-3A.75.75 0 0 0 9 7.75Z',
    length: props.apiDocsContent.length,
  },
  {
    docType: 'prompts',
    title: '提示词套件',
    state: 'review-prompts',
    iconWrap: 'bg-emerald-50 dark:bg-emerald-950/40',
    iconColor: 'text-emerald-600',
    iconPath:
 'M9.25 2.5a.75.75 0 0 1 .75.75v6.94l1.22-1.22a.75.75 0 1 1 1.06 1.06l-2.5 2.5a.75.75 0 0 1-1.06 0l-2.5-2.5a.75.75 0 1 1 1.06-1.06l1.22 1.22V3.25a.75.75 0 0 1 .75-.75Zm-6.5 10a.75.75 0 0 1 .75.75v1h13v-1a.75.75 0 0 1 1.5 0v1.75a.75.75 0 0 1-.75.75h-14.5a.75.75 0 0 1-.75-.75v-1.75a.75.75 0 0 1 .75-.75Z',
    length: props.promptsContent.length,
  },
])

/** 实际有内容的卡片；空文档不占位 */
const cards = computed<DoneCard[]>(() => ALL_CARDS.value.filter((card) => card.length > 0))

/** 完成提示的标题：按实际交付了几份文档措辞 */
const headline = computed(() =>
  cards.value.length > 1 ? `${cards.value.length} 份文档已全部完成` : '文档已生成完毕',
)

/** 下载图标路径（三张卡片共用） */
const DOWNLOAD_ICON =
 'M10 2a.75.75 0 0 1 .75.75v7.94l1.72-1.72a.75.75 0 1 1 1.06 1.06l-3 3a.75.75 0 0 1-1.06 0l-3-3a.75.75 0 1 1 1.06-1.06l1.72 1.72V2.75A.75.75 0 0 1 10 2ZM5.25 12.5a.75.75 0 0 1 .75.75v2.25h8v-2.25a.75.75 0 0 1 1.5 0v3a.75.75 0 0 1-.75.75h-9.5a.75.75 0 0 1-.75-.75v-3a.75.75 0 0 1 .75-.75Z'
</script>

<template>
  <section class="space-y-4">
    <!-- 完成提示头 -->
    <div class="rounded-2xl border border-line bg-surface p-6 text-center shadow-sm  ">
      <div class="mx-auto flex h-12 w-12 items-center justify-center rounded-xl bg-primary-50 dark:bg-primary-950/40">
        <svg class="h-6 w-6 text-primary-600" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
          <path
            fill-rule="evenodd"
            d="M10 18a8 8 0 1 0 0-16 8 8 0 0 0 0 16Zm3.857-9.809a.75.75 0 0 0-1.214-.882l-3.483 4.79-1.88-1.88a.75.75 0 1 1-1.06 1.061l2.5 2.5a.75.75 0 0 0 1.137-.089l4-5.5Z"
            clip-rule="evenodd"
          />
        </svg>
      </div>
      <h2 class="mt-3 text-base font-semibold text-fg ">{{ headline }}</h2>
      <p class="mx-auto mt-1 max-w-md text-sm text-subtle">
        以下文档已生成完毕，可分别下载为 Markdown 文件，或返回查看与修改。
      </p>
    </div>

    <!-- 三份文档卡片 -->
    <div class="grid gap-4">
      <article
        v-for="card in cards"
        :key="card.docType"
        class="flex items-center justify-between gap-3 rounded-2xl border border-line bg-surface p-4 shadow-sm  "
      >
        <div class="flex min-w-0 items-center gap-3">
          <div
            class="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg"
            :class="card.iconWrap"
          >
            <svg class="h-5 w-5" :class="card.iconColor" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
              <path fill-rule="evenodd" :d="card.iconPath" clip-rule="evenodd" />
            </svg>
          </div>
          <div class="min-w-0">
            <h3 class="text-sm font-semibold text-fg ">{{ card.title }}</h3>
            <p class="truncate text-xs text-subtle">{{ card.length }} 字符 · Markdown</p>
          </div>
        </div>
        <div class="flex shrink-0 gap-2">
          <AppButton variant="secondary" size="sm" @click="emit('view', card.state)">
            查看
          </AppButton>
          <AppButton variant="primary" size="sm" @click="emit('download', card.docType)">
            <svg class="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
              <path fill-rule="evenodd" :d="DOWNLOAD_ICON" clip-rule="evenodd" />
            </svg>
            下载
          </AppButton>
        </div>
      </article>
    </div>

    <!-- 底部操作 -->
    <div class="flex flex-wrap items-center justify-center gap-3 pt-1">
      <AppButton variant="secondary" @click="emit('back')">
        {{ backLabel ?? '返回对话' }}
      </AppButton>
      <AppButton variant="primary" @click="emit('restart')">
        重新开始
      </AppButton>
    </div>
  </section>
</template>
