<script setup lang="ts">
import { computed, ref } from 'vue'

import DocumentReview from '@/components/DocumentReview.vue'
import AppActionBar from '@/components/ui/AppActionBar.vue'
import AppButton from '@/components/ui/AppButton.vue'
import PendingItemsPanel from '@/components/ui/PendingItemsPanel.vue'
import type { PendingItem, RunSummary, ViewState } from '@/types'
import { DOWNLOAD_ICON } from '@/utils/icons'
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
 * - **内容行**：这份文档自己的工具——左端是 AI 优化 / 版本（对文档本身做事），右端是
 *   「预览 / 编辑」视图切换 + 复制 / 下载（把文档拿出去：一个进剪贴板、一个存成文件，
 *   因此并邻、复制在下载左边）。
 *   点「AI 优化」后原地换成指令输入，文档全程可见（改成弹窗就看不到了，而写优化指令时恰恰要
 *   对着文档）。
 * - **流程行**：这份文档之后往哪走——返回、生成下一份、完成。
 *
 * 内容行**没有「保存」按钮**：手改正文由 `DocumentReview` 的草稿变化事件驱动、自动防抖落库
 * （800ms，见 `stores/documents.ts`），不需要用户记得点一下；原来那个位置改成「下载」，
 * 把当前这份文档（含最新编辑）导出为 `.md`。
 *
 * 工具行的按钮是「对内容做的事」，但草稿、复制反馈、视图模式这些状态天然属于 `DocumentReview`，
 * 所以用 `ref` 取它的句柄来渲染；指令输入本身是底栏的 UI 状态，留在本组件里。
 *
 * 「预览 / 编辑」最初挂在文档卡片头部，与卡片底部的工具条隔着整篇正文——同一份文档的控件
 * 散在上下两处。现在收进底栏内容行，与文档工具排在一行，符合「文档相关的控件都在底栏」的约定。
 */
const emit = defineEmits<{
  /**
   * 草稿变化：用户改动了这份文档的可编辑原文。
   *
   * 载荷是**全文**。上层（`AppV2`）直接交给管线 → `documents.save` 落库，防抖与单飞都在 store 里。
   * 这里**逐次输入如实上报**、不自己攒批——攒批会让「防抖窗口内切方案 / 关页面」丢掉最后一次编辑。
   */
  draftChange: [content: string]
  /**
   * 下载当前这份文档。
   *
   * 载荷是**组件里的草稿**（`draftContent`），而不是 store 的工作副本——防抖窗口内草稿可能更新，
   * 下载必须拿到用户眼前这一版。
   */
  download: [content: string]
  /** 打开版本历史抽屉 */
  openVersions: []
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
  /**
   * 去对话页澄清这份文档的待确认项（三种审阅页共用）。
   *
   * 三条文档线程现在都落在同一个对话页上，靠子标签栏切换，因此这里只需要「去哪一份」——
   * 由上层按当前阶段决定，组件不必知道。
   */
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
   * 是否给出「去对话页澄清这份文档」的入口。
   *
   * 由上层决定而不是这里判断：能否澄清取决于「有没有正文」这类上层才知道的事
   * （澄清最终要落库，没有正文就无处可写）。组件只负责在允许时把它渲染出来。
   *
   * 入口落在底栏的次操作组，**并顶替「返回」按钮**——条件是两者的落点相同
   * （完整流程下都是对话页，并排会给用户两个近乎同名的选择）。落点不同的入口模式
   * （快捷流程 / 两个导入入口：返回的是录入页，而录入页不在进度条上）由上层传
   * `keepBackButton` 要求保留返回，两者并排。
   */
  canReturnToChat?: boolean
  /**
   * 澄清入口出现时，是否**保留**「返回」按钮。
   *
   * 只在澄清入口与「返回」落点不同时才传。典型场景是快捷流程与两个导入入口：它们没有
   * 需求对话，但同样会产出待确认项；若让澄清入口顶替「返回」，用户就再也回不到
   * 描述 / 材料录入页（那一页不在进度条上，没有别的入口）。
   */
  keepBackButton?: boolean
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
 * 是否展示「去对话页澄清剩余问题」入口（在底栏次操作组）。
 *
 * 上层按阶段与入口模式给出开关（`canReturnToChat`），这里再兜一层「真有待确认项」：
 * 没有待确认项却给一个「去澄清」的入口，点进去只能闲聊。两处判据收在这一个 computed 里，
 * 免得模板里「渲染入口」与「隐藏返回」各写一遍条件、日后改一处漏一处。
 */
const showReturnToChat = computed(
  () => Boolean(props.canReturnToChat) && (props.pendingItems?.length ?? 0) > 0,
)

/**
 * 澄清入口的文案。
 *
 * 完整流程下它是「回到对话」——那里真有一段对话等着他；另外两条入口没有需求对话，
 * 进去是**新开**一条澄清线程，说「回到对话」会让人以为自己的东西丢了，
 * 因此换成「和 AI 确认」。
 */
const returnToChatLabel = computed(() => {
  const count = props.pendingItems?.length ?? 0
  return props.keepBackButton
    ? `和 AI 确认剩下的 ${count} 个问题`
    : `回到对话，确认剩下的 ${count} 个问题`
})

/** 是否渲染「返回」按钮：澄清入口在与「返回」落点相同处会顶替它，落点不同处则两者并存 */
const showBack = computed(() => !showReturnToChat.value || Boolean(props.keepBackButton))

/** 文档组件句柄：底栏工具行要读它的草稿 / 复制反馈 / 视图模式，并调它的复制与切换视图 */
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
      @draft-change="emit('draftChange', $event)"
    />

    <!--
      「去对话页澄清这些待确认项」的入口已下沉到下面的底栏（次操作组 `#secondary`）。

      它曾经是接口文档 / 提示词专用的**普通区块**——那时它们的澄清落在一个独立页面上，
      整块入口足以承担；PRD 那条的落点就是对话页，所以先一步进了底栏。三条线程合并到同一个
      对话页之后（ADR-028），三者落点完全一致，于是统一收进底栏，三种审阅页共用一套。
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

            <!-- 版本历史：抽屉的**入口**落在底栏内容行（ADR-015 的唯一操作落点），
                 抽屉内只有内容操作（存为新版本 / 恢复），不出现流程动作（见 ADR-030）。 -->
            <AppButton variant="secondary" size="sm" @click="emit('openVersions')">
              <svg class="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                <path
                  fill-rule="evenodd"
                  d="M10 18a8 8 0 1 0 0-16 8 8 0 0 0 0 16Zm.75-13a.75.75 0 0 0-1.5 0v5c0 .414.336.75.75.75h4a.75.75 0 0 0 0-1.5h-3.25V5Z"
                  clip-rule="evenodd"
                />
              </svg>
              版本
            </AppButton>
          </div>

          <!-- 右端：视图切换 + 复制 / 下载。后两者是同一类事——「把这份文档拿出去」
               （一个进剪贴板、一个存成文件），因此排在一起、复制紧挨在下载左边 -->
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

            <!-- 复制：把当前这份文档整段塞进剪贴板，与下载并邻（同类动作），在下载左边。
                 文案随复制结果变（`copyTip`），所以它天然属于 `DocumentReview` 的状态。 -->
            <AppButton variant="secondary" size="sm" :disabled="doc?.busy" @click="doc?.copy()">
              <svg class="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                <path d="M7 9a2 2 0 012-2h6a2 2 0 012 2v6a2 2 0 01-2 2H9a2 2 0 01-2-2V9z" />
                <path d="M5 3a2 2 0 00-2 2v6a2 2 0 002 2h1V7a2 2 0 012-2h6V5a2 2 0 00-2-2H5z" />
              </svg>
              {{ doc?.copyTip || '复制' }}
            </AppButton>

            <!-- 下载：把当前这份文档（含用户改过的稿）导出为 .md，与完成页的下载是同一套视觉。
                 原来这里是「保存」——现在手改已自动防抖落库，不需要用户点一下了，按钮改作下载。
                 载荷取 `doc?.draftContent` 而不是 store 的工作副本：防抖窗口内用户刚敲的字还没落库，
                 下载必须给出眼前这一版。
                 「有正文才可点」是因为 downloadFile 对空内容会**静默 return**（utils/download.ts:21），
                 禁用态避免一次「点了没反应」的死点击。 -->
            <AppButton
              variant="secondary"
              size="sm"
              :disabled="doc?.busy || !doc?.hasContent"
              @click="emit('download', doc?.draftContent ?? '')"
            >
              <svg class="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                <path fill-rule="evenodd" :d="DOWNLOAD_ICON" clip-rule="evenodd" />
              </svg>
              下载
            </AppButton>
          </div>
        </div>
      </template>

      <!-- 流程行：左「返回 / 去对话澄清」，右按顺序推进 / 查看其它文档 / 完成。
           三段式顺序与「主操作永远最右且唯一」都不动（ADR-015），新入口进的是次操作组。 -->
      <template #secondary>
        <!-- 澄清入口与「返回」落点相同时由它顶替「返回」——否则并排会出现两个都通向对话页、
             名字也近似的按钮，用户不知道该点哪个。落点不同时（快捷流程 / 两个导入入口：
             「返回」回的是录入页）两者并存，顺序是「先退后进」。 -->
        <AppButton v-if="showBack" variant="secondary" size="md" @click="emit('back')">
          {{ backLabel ?? '返回对话' }}
        </AppButton>
        <AppButton
          v-if="showReturnToChat"
          variant="secondary"
          size="md"
          @click="emit('returnToChat')"
        >
          {{ returnToChatLabel }}
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
