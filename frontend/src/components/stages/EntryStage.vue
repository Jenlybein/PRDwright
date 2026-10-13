<script setup lang="ts">
import type { EntryMode } from '@/types'
import AppCard from '@/components/ui/AppCard.vue'
import AppIcon from '@/components/ui/AppIcon.vue'

/**
 * 入口选择页。
 *
 * 三条入口的差别只在**起点**：完整流程从结构化表单开始，快捷入口从一段话开始，
 * 调试入口从一份现成的 PRD 开始；往后的生成、审阅、下载完全共用。因此这里只需要
 * 说清「各自适合什么场景」，不需要解释实现差异。
 *
 * 组件自身不持有任何流程状态，选中哪个入口由上层的 store 决定。
 *
 * 视觉已切换到统一基础件：卡片用 AppCard（interactive 渲染为 <button>，保留键盘可达性），
 * 图标用 AppIcon，卡片的悬浮态由 AppCard 统一提供，数据里不再需要 hover 字段。
 */
const emit = defineEmits<{
  /** 选中某个入口 */
  select: [mode: EntryMode]
}>()

/** 一张入口卡片的渲染配置 */
interface EntryCard {
  mode: EntryMode
  title: string
  /** 右侧的产出说明：一句话讲清这条入口最终会给你什么 */
  output: string
  description: string
  /** 这条入口会经过的环节，用箭头串起来 */
  path: string
  iconWrap: string
  iconColor: string
  iconPath: string
}

const CARDS: EntryCard[] = [
  {
    mode: 'structured',
    title: '完整流程',
    output: 'PRD + 接口文档 + 提示词套件',
    description:
      '用结构化表单逐项录入需求，再和 AI 对话把细节问清楚。信息最全，适合正式立项。',
    path: '描述产品 → AI 对话 → PRD → 接口文档 → 提示词',
    iconWrap: 'bg-primary-50 group-hover:bg-primary-100 dark:bg-primary-950/40 dark:group-hover:bg-primary-900/60',
    iconColor: 'text-primary-600',
    iconPath:
      'M4 4a2 2 0 0 1 2-2h5.586a1 1 0 0 1 .707.293L15.707 5.5a1 1 0 0 1 .293.707V16a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4Zm7 1.5V7h1.5L11 5.5ZM7 9a1 1 0 0 0 0 2h6a1 1 0 1 0 0-2H7Zm0 3a1 1 0 1 0 0 2h6a1 1 0 1 0 0-2H7Z',
  },
  {
    mode: 'prd-quick',
    title: 'PRD 快捷',
    output: '仅 PRD',
    description:
      '用一段话把想法说清楚，跳过表单与对话，直接逐章生成 PRD。适合先出个稿子看看。',
    path: '一段话描述 → PRD（逐章撰写 + 审核 + 改写）',
    iconWrap: 'bg-amber-50 group-hover:bg-amber-100 dark:bg-amber-950/40 dark:group-hover:bg-amber-900/60',
    iconColor: 'text-amber-600',
    iconPath:
      'M11.3 1.046A1 1 0 0 1 12 2v5h4a1 1 0 0 1 .82 1.573l-7 10A1 1 0 0 1 8 18v-5H4a1 1 0 0 1-.82-1.573l7-10a1 1 0 0 1 1.12-.381Z',
  },
  {
    mode: 'prompt-debug',
    title: '提示词调试',
    output: '仅提示词套件',
    description:
      '手头已经有一份 PRD？贴进来直接生成提示词套件，再用「AI 优化」反复迭代到你满意。',
    path: '贴入 PRD → 提示词套件 → AI 优化迭代',
    iconWrap: 'bg-emerald-50 group-hover:bg-emerald-100 dark:bg-emerald-950/40 dark:group-hover:bg-emerald-900/60',
    iconColor: 'text-emerald-600',
    // 尖括号：提示词套件本质上是给模型看的「代码」
    iconPath:
      'M6.28 5.22a.75.75 0 0 1 0 1.06L2.56 10l3.72 3.72a.75.75 0 0 1-1.06 1.06L.97 10.53a.75.75 0 0 1 0-1.06l4.25-4.25a.75.75 0 0 1 1.06 0Zm7.44 0a.75.75 0 0 1 1.06 0l4.25 4.25a.75.75 0 0 1 0 1.06l-4.25 4.25a.75.75 0 0 1-1.06-1.06L17.44 10l-3.72-3.72a.75.75 0 0 1 0-1.06Z',
  },
]

/** 卡片右侧的箭头图标 */
const ARROW_ICON = 'M7.22 4.22a.75.75 0 0 1 1.06 0l5.25 5.25a.75.75 0 0 1 0 1.06l-5.25 5.25a.75.75 0 1 1-1.06-1.06L11.94 10 7.22 5.28a.75.75 0 0 1 0-1.06Z'
</script>

<template>
  <section class="space-y-5">
    <header class="text-center">
      <h1 class="text-xl font-bold text-slate-900 dark:text-slate-100">选择一种开始方式</h1>
      <p class="mx-auto mt-2 max-w-lg text-sm text-slate-500 dark:text-slate-400">
        三条入口最终都通向同一套写作规范与生成链路，只是起点不同。选错了也没关系，
        随时可以重新开始。
      </p>
    </header>

    <div class="grid gap-3">
      <AppCard
        v-for="card in CARDS"
        :key="card.mode"
        interactive
        class="group flex items-start gap-4"
        @click="emit('select', card.mode)"
      >
        <span
          class="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl transition"
          :class="card.iconWrap"
        >
          <AppIcon :path="card.iconPath" :class="card.iconColor" />
        </span>

        <span class="min-w-0 flex-1">
          <span class="flex flex-wrap items-center gap-2">
            <span class="text-sm font-semibold text-slate-900 dark:text-slate-100">{{ card.title }}</span>
            <span
              class="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-500 dark:bg-slate-800 dark:text-slate-400"
            >
              产出：{{ card.output }}
            </span>
          </span>
          <span class="mt-1 block text-xs leading-relaxed text-slate-500 dark:text-slate-400">
            {{ card.description }}
          </span>
          <span class="mt-2 block font-mono text-[11px] text-slate-400 dark:text-slate-500">{{ card.path }}</span>
        </span>

        <svg
          class="mt-3 h-4 w-4 shrink-0 text-slate-300 transition group-hover:translate-x-0.5 group-hover:text-slate-500 dark:text-slate-600 dark:group-hover:text-slate-400"
          viewBox="0 0 20 20"
          fill="currentColor"
          aria-hidden="true"
        >
          <path fill-rule="evenodd" :d="ARROW_ICON" clip-rule="evenodd" />
        </svg>
      </AppCard>
    </div>
  </section>
</template>
