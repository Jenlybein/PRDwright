<script setup lang="ts">
import { computed, ref } from 'vue'

import DocumentReview from '@/components/DocumentReview.vue'
import AppActionBar from '@/components/ui/AppActionBar.vue'
import AppButton from '@/components/ui/AppButton.vue'
import PendingItemsPanel from '@/components/ui/PendingItemsPanel.vue'
import type { PendingItem, RunSummary, ViewState } from '@/types'
import { formatDuration, formatTokens } from '@/utils/metering'

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
  /** 进入澄清页（对待确认项逐条澄清后改写这份文档） */
  clarify: []
  /** 回到主对话栏继续澄清剩余问题（PRD 专用：复用同一条聊天记录） */
  returnToChat: []
}>()

const props = defineProps<{
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
  /**
   * 最近一次生成的用量汇总；没有（如直接打开一份已完成方案、未发生本次生成）时为 null。
   *
   * **只展示，不产生任何操作**：这一行是给用户看的回执（花了多久、用了多少、出问题时报哪个号），
   * 不是「对文档做的事」，因此不放进底栏，也不加按钮。
   */
  runSummary?: RunSummary | null
  /**
   * 本次生成的待确认清单；文档生成时由后端从正文里剥离标记块算出并随正文落库，
   * 前端只读不写。空数组表示「没有待确认项」——不渲染清单区。
   */
  pendingItems?: PendingItem[]
  /**
   * 是否给出「逐条澄清」入口。
   *
   * 由上层决定而不是这里判断：能否澄清取决于「有没有正文、有没有方案 id」这类上层才知道的
   * 事（澄清最终要落库，没有方案就无处可写）。组件只负责在允许时把它渲染出来。
   */
  canClarify?: boolean
  /**
   * 是否给出「回到对话」入口（PRD 专用，与 `canClarify` 互斥）。
   *
   * 同样由上层决定：能否走这条路径取决于「是不是在审 PRD」「是不是完整流程（有对话可回）」
   * 以及「有没有待确认项」，三者都是上层才知道的事。入口落在底栏的次操作组，
   * 并**顶替**「返回」按钮（两者落点都是对话页，并排会给用户两个近乎同名的选择）。
   */
  canReturnToChat?: boolean
}>()

/** 本次生成消耗的总 token（输入 + 输出）；为 0 时该段不展示 */
const totalTokens = computed(() => {
  const summary = props.runSummary
  return summary ? summary.promptTokens + summary.completionTokens : 0
})

/**
 * 这次生成是否**没能用上技能包**（退回了内置兜底提示词）。
 *
 * 只有 PRD 生成依赖技能包，接口文档与提示词本来就不注入，所以必须先判 `kind`——
 * 否则每份接口文档的回执上都会挂一条莫须有的告警。
 */
const skillFallback = computed(
  () => props.runSummary?.kind === 'prd' && (props.runSummary.injectedSkills ?? []).length === 0,
)

/**
 * 是否展示 PRD 专属的「回到对话澄清剩余问题」入口（在底栏次操作组，并顶替「返回」）。
 *
 * 上层按阶段与入口模式给出开关（`canReturnToChat`），这里再兜一层「真有待确认项」：
 * 没有待确认项却给一个「去澄清」的入口，点进去只能闲聊。两处判据收在这一个 computed 里，
 * 免得模板里「渲染入口」与「隐藏返回」各写一遍条件、日后改一处漏一处。
 */
const showReturnToChat = computed(
  () => Boolean(props.canReturnToChat) && (props.pendingItems?.length ?? 0) > 0,
)

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

    <!-- 最近一次生成的只读用量回执：耗时 / token / 请求号。
         请求号是报障时「念给维护者」的那串字，用 select-all 让它可选中复制——刻意不加按钮，
         这一行不该和底栏那些「对文档做的事」抢注意力。 -->
    <div
      v-if="runSummary"
      class="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-subtle"
    >
      <span>本次生成耗时 {{ formatDuration(runSummary.durationMs) }}</span>
      <template v-if="totalTokens > 0">
        <span aria-hidden="true">·</span>
        <span>约 {{ formatTokens(totalTokens) }} tokens</span>
      </template>
      <template v-if="runSummary.totalCalls > 0">
        <span aria-hidden="true">·</span>
        <span>{{ runSummary.totalCalls }} 次调用</span>
      </template>
      <!-- 失败过的调用：生成这次成功了（否则不会走到审核页），但过程中有抖动。
           说出来是为了回答「为什么这次比平时慢」——它占用了真实的时间，
           却又不在上面的 token 数里。同样不加按钮，只陈述。 -->
      <template v-if="runSummary.failedCalls > 0">
        <span aria-hidden="true">·</span>
        <span>其中 {{ runSummary.failedCalls }} 次调用失败后重试</span>
      </template>
      <template v-if="runSummary.requestId">
        <span aria-hidden="true">·</span>
        <span>
          请求号
          <code
            class="select-all rounded bg-surface-2 px-1 py-0.5 font-mono text-[11px] text-muted"
            title="选中即可复制；报障时提供它可快速定位这次生成"
            >{{ runSummary.requestId }}</code
          >
        </span>
      </template>
      <!-- 技能包身份：回答「这份文档是按哪一套模板写的」。指纹随模板内容变化，
           所以两份回执的指纹不同，就说明它们不是同一套模板写的。 -->
      <template v-for="skill in runSummary.injectedSkills ?? []" :key="skill.name">
        <span aria-hidden="true">·</span>
        <span>
          技能包 {{ skill.name }} v{{ skill.version }}
          <code
            class="select-all rounded bg-surface-2 px-1 py-0.5 font-mono text-[11px] text-muted"
            title="三份技能资源合成的内容指纹；改了模板它就会变"
            >{{ skill.fingerprint }}</code
          >
        </span>
      </template>
    </div>

    <!-- 技能包没加载上：生成退回了内置兜底提示词，PRD 的风格可能与你有模板时不同。
         这条必须显式——以往它只是在后端留一行没人看的日志（债务 D8）。 -->
    <p
      v-if="skillFallback"
      class="rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 text-xs text-amber-700 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-300"
    >
      未加载技能包，本次使用的是内置兜底提示词（章节结构与写作规范可能与技能包版本不同）。
    </p>

    <!-- 事前预算的告警：某一步的输入逼近了预算上限（未超顶，所以生成照常完成了）。
         只提示、不拦截、不给按钮——用户据此知道「下次该精简点」，不需要现在做什么。 -->
    <ul
      v-if="runSummary?.warnings.length"
      class="space-y-1 rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 text-xs text-amber-700 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-300"
    >
      <li v-for="warning in runSummary.warnings" :key="warning">{{ warning }}</li>
    </ul>

    <!-- 待确认清单：本次生成产出的元数据，读后端随正文落库的那一份。
         放在**正文之上**——它是「这份文档还差哪些问题没敲定」，先看到还差什么、再看正文本身。
         归「内容本身」而不是「对文档做的事」，所以不进底栏；面板自身默认只铺开前 3 条。 -->
    <PendingItemsPanel v-if="pendingItems && pendingItems.length" :items="pendingItems" />

    <!-- 文档卡片：只有头部与正文，工具全部下沉到下面的底栏 -->
    <DocumentReview
      ref="doc"
      :title="title"
      :content="content"
      @save="emit('save', $event)"
    />

    <!-- 逐条澄清入口（接口文档 / 提示词专用）：**普通区块，不占底栏的主操作位**（ADR-015）。
         只在真有待确认项时出现——没有待确认项却给一个「去澄清」的入口，点进去只能闲聊。
         这两份文档的澄清要开一个独立澄清页，整块入口足以承担；PRD 那条的落点就是对话页，
         已下沉到下面的底栏由它顶替「返回」（见 `showReturnToChat`）。 -->
    <button
      v-if="canClarify && pendingItems && pendingItems.length"
      type="button"
      class="flex w-full items-center gap-3 rounded-xl border border-accent/40 bg-accent-soft px-4 py-3 text-left transition-colors hover:border-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-accent-soft-line"
      @click="emit('clarify')"
    >
      <svg
        class="h-4 w-4 shrink-0 text-accent"
        viewBox="0 0 20 20"
        fill="currentColor"
        aria-hidden="true"
      >
        <path
          fill-rule="evenodd"
          d="M18 10c0 3.866-3.582 7-8 7a8.84 8.84 0 01-2.347-.306c-.584.296-1.925.864-4.181 1.234-.2.032-.352-.176-.273-.362.354-.836.674-1.95.77-2.966C3.365 13.376 3 11.752 3 10c0-3.866 3.582-7 8-7s7 3.134 7 7Zm-4.5-.25a.75.75 0 000-1.5h-4a.75.75 0 000 1.5h4Zm0 3.5a.75.75 0 000-1.5h-2a.75.75 0 000 1.5h2Z"
          clip-rule="evenodd"
        />
      </svg>
      <span class="min-w-0 flex-1">
        <span class="block text-sm font-medium text-fg">逐条澄清这些待确认项</span>
        <span class="mt-0.5 block text-xs text-subtle">
          和 AI 把 {{ pendingItems.length }} 个问题聊清楚，再按结论改写这份文档——只动涉及的章节。
        </span>
      </span>
      <svg
        class="h-4 w-4 shrink-0 text-subtle"
        viewBox="0 0 20 20"
        fill="currentColor"
        aria-hidden="true"
      >
        <path
          fill-rule="evenodd"
          d="M7.21 14.77a.75.75 0 01.02-1.06L11.168 10 7.23 6.29a.75.75 0 111.04-1.08l4.5 4.25a.75.75 0 010 1.08l-4.5 4.25a.75.75 0 01-1.06-.02Z"
          clip-rule="evenodd"
        />
      </svg>
    </button>

    <!--
      PRD 专属的「回到对话」入口已下沉到下面的底栏（次操作组 `#secondary`）。

      它是「进入某个流程」的入口，但落脚点就是**对话页**，所以按 ADR-015「底栏是全站唯一操作落点」
      收进底栏反而更一致；只是因为不能占主操作位（那是「完成」的），所以进次操作组。
      原来的整块区块按钮与底栏里的「返回」落点相同、名字也近似，两个并排会让用户不知道该点哪个，
      因此改为：该入口出现时由它**顶替**「返回」按钮（见 `showReturnToChat`）。
    -->

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

      <!-- 流程行：左「返回 / 回到对话」，右按顺序推进 / 查看其它文档 / 完成。
           三段式顺序与「主操作永远最右且唯一」都不动（ADR-015），新入口进的是次操作组。 -->
      <template #secondary>
        <!-- 两个入口的落点都是对话页，因此互斥渲染：有待确认项时由「回到对话」顶替「返回」，
             否则并排会出现两个都叫「回到对话」的按钮，用户不知道该点哪个。 -->
        <AppButton
          v-if="showReturnToChat"
          variant="secondary"
          size="md"
          @click="emit('returnToChat')"
        >
          回到对话，确认剩下的 {{ pendingItems?.length }} 个问题
        </AppButton>
        <AppButton v-else variant="secondary" size="md" @click="emit('back')">
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
