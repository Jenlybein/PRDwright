<script setup lang="ts">
import { computed, ref } from 'vue'
import { storeToRefs } from 'pinia'

import RagResultPanel from '@/components/RagResultPanel.vue'
import RequirementsForm from '@/components/RequirementsForm.vue'
import SummaryDiffPanel from '@/components/SummaryDiffPanel.vue'
import ChatStage from '@/components/stages/ChatStage.vue'
import DoneStage from '@/components/stages/DoneStage.vue'
import EntryStage from '@/components/stages/EntryStage.vue'
import GeneratingStage from '@/components/stages/GeneratingStage.vue'
import ProgressSteps from '@/components/stages/ProgressSteps.vue'
import PromptDebugStage from '@/components/stages/PromptDebugStage.vue'
import QuickPrdStage from '@/components/stages/QuickPrdStage.vue'
import ReviewStage from '@/components/stages/ReviewStage.vue'
import { useClarificationChat } from '@/composables/useClarificationChat'
import { useDocumentPipeline } from '@/composables/useDocumentPipeline'
import { useWorkflowStore } from '@/stores/workflow'
import type { EntryMode, RequirementsSummary, StepId } from '@/types'

/**
 * 完整应用（V2）。
 *
 * 先选入口，再走流程。三条入口（完整流程 / PRD 快捷 / 提示词调试）只是**起点**不同：
 *
 * 1. **完整流程**：结构化表单逐项录入 → AI 对话澄清 → PRD → 接口文档 → 提示词；
 * 2. **PRD 快捷**：一段话描述 → 直接逐章生成 PRD（跳过表单、对话与摘要确认闸门）；
 * 3. **提示词调试**：贴入现成 PRD → 生成提示词套件 → 用「AI 优化」反复迭代。
 *
 * 三条入口在「生成 → 审阅 → 下载」这段完全共用，模式的差异（进度条显示哪几步、
 * 完成需要哪几份文档、返回按钮回到哪里）由 `utils/workflow.ts` 与 store 推导。
 *
 * 完整流程另有两道**确认闸门**：生成 PRD 前先确认「对话补充信息回填到摘要」的 Diff；
 * 生成接口文档前先确认「检索到的团队接口规范」。这两道闸门只属于完整流程。
 *
 * 本文件只做三件事：路由（阶段 → 视图）、跨流程的提示语、以及把两个流程
 * （对话 / 文档）拼装起来。会话数据与阶段在 `stores/workflow.ts`，对话编排在
 * `composables/useClarificationChat.ts`，文档编排在 `composables/useDocumentPipeline.ts`，
 * 各阶段视图在 `components/stages/`。
 */

const store = useWorkflowStore()
const {
  viewState,
  summary,
  quickInput,
  debugPrdText,
  steps,
  currentStepIndex,
  reachedStepIndex,
  canGeneratePrd,
  downgradeNotice,
  prdContent,
  apiDocsContent,
  promptsContent,
} = storeToRefs(store)

/**
 * 表单组件的 key。
 * 「重新开始」时自增，强制重建表单组件，确保里面的编辑器回到空状态。
 */
const formKey = ref(0)

/** 短暂展示的提示语（草稿保存 / 提交 / 闸门确认等） */
const savedTip = ref('')

/** 提示语的定时器 */
let savedTipTimer: number | undefined

/** 短暂展示提示，随后自动淡出 */
function flashSavedTip(text: string): void {
  savedTip.value = text
  window.clearTimeout(savedTipTimer)
  savedTipTimer = window.setTimeout(() => {
    savedTip.value = ''
  }, 2500)
}

// ---------------------------------------------------------------------------
// 文档流水线：生成 / 审阅 / 两道确认闸门 / 下载
// ---------------------------------------------------------------------------

const {
  isGenerating,
  isGeneratingPhase,
  isReviewing,
  canFinishTask,
  activeDocTitle,
  activeDocContent,
  backLabel,
  docStreamingContent,
  docError,
  prdStatus,
  summaryChanges,
  summarySyncing,
  summarySyncError,
  ragHits,
  ragRetrieving,
  ragError,
  goTo,
  onSaveDocument,
  onAiOptimize,
  handleGenerateApiDocs,
  handleGeneratePrompts,
  handleGenerateQuickPrd,
  handleGeneratePromptsFromDebugPrd,
  backToInput,
  handleEndTask,
  downloadDocument,
  handleGeneratePrd,
  confirmSummaryDiff,
  rejectSummaryDiff,
  confirmRag,
  cancelRag,
  reset: resetPipeline,
} = useDocumentPipeline({ onNotice: flashSavedTip })

// ---------------------------------------------------------------------------
// 澄清对话
// ---------------------------------------------------------------------------

const {
  streaming,
  chatError,
  displayMessages,
  sendMessage: handleSendMessage,
  startConversation: handleStartConversation,
  reset: resetChat,
} = useClarificationChat({
  onNotice: flashSavedTip,
  onEnterChat: () => goTo('chatting'),
})

/** 是否已有 PRD / 接口文档 / 提示词正文，决定审阅页的「查看」入口是否出现 */
const hasPrd = computed(() => prdContent.value.trim().length > 0)
const hasApiDocs = computed(() => apiDocsContent.value.trim().length > 0)
const hasPrompts = computed(() => promptsContent.value.trim().length > 0)

/** 是否处于入口选择页：这一步还没有进度可言，进度条不显示 */
const isChoosingEntry = computed(() => viewState.value === 'entry')

// ---------------------------------------------------------------------------
// 表单与全局操作
// ---------------------------------------------------------------------------

/** 表单内容变化：实时更新摘要，store 的持久化 watch 会把它写进本地快照作为草稿 */
function handleSummaryChange(value: RequirementsSummary): void {
  summary.value = value
}

/** 选择入口：记下模式并进入该入口的第一个界面 */
function handleSelectEntry(mode: EntryMode): void {
  store.chooseEntry(mode)
}

/** 清空全部数据并回到入口选择页（含清除本地持久化的会话快照） */
function handleRestart(): void {
  // 会话数据与本地快照交由 store 统一重置（入口模式与两处录入内容也一并清掉）
  store.reset()
  // 两条流程各自的临时状态分别归零
  resetPipeline()
  resetChat()

  // 表单是 v-show 常驻组件，自增 key 强制重建，保证编辑器回到空状态
  formKey.value += 1
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

/**
 * 点击进度条中已到达的步骤，跳回对应阶段查看之前的内容。
 * 五个步骤 id 与同名的阶段一一对应（`StepId` 是 `ViewState` 的子集），因此无需映射表。
 */
function handleStepClick(stepId: StepId): void {
  goTo(stepId)
}
</script>

<template>
  <div class="min-h-screen bg-gradient-to-b from-sky-50 via-slate-50 to-slate-100 text-slate-900">
    <!-- 顶部导航栏 -->
    <header class="sticky top-0 z-10 border-b border-slate-200/70 bg-white/80 backdrop-blur">
      <div class="mx-auto flex max-w-3xl items-center justify-between px-4 py-3">
        <div class="flex items-center gap-3">
          <div
            class="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-primary-500 to-sky-600 text-sm font-bold text-white shadow-md shadow-sky-500/30"
          >
            H
          </div>
          <div>
            <div class="text-sm font-bold leading-tight text-slate-900">HarnessPRD</div>
            <div class="text-xs leading-tight text-slate-500">
              填写需求 · 生成 PRD / 接口文档 / 提示词
            </div>
          </div>
        </div>

        <!-- 状态提示徽章，自动淡出 -->
        <Transition
          enter-active-class="transition duration-200"
          enter-from-class="opacity-0 translate-y-1"
          leave-active-class="transition duration-500"
          leave-to-class="opacity-0"
        >
          <span
            v-if="savedTip"
            class="flex items-center gap-1 rounded-full bg-emerald-50 px-3 py-1 text-xs font-medium text-emerald-600 ring-1 ring-emerald-200"
          >
            <svg class="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
              <path
                fill-rule="evenodd"
                d="M10 18a8 8 0 1 0 0-16 8 8 0 0 0 0 16Zm3.857-9.809a.75.75 0 0 0-1.214-.882l-3.483 4.79-1.88-1.88a.75.75 0 1 1-1.06 1.061l2.5 2.5a.75.75 0 0 0 1.137-.089l4-5.5Z"
                clip-rule="evenodd"
              />
            </svg>
            {{ savedTip }}
          </span>
        </Transition>
      </div>
    </header>

    <main class="mx-auto max-w-3xl px-4 py-10">
      <!-- 步骤进度条：展示哪几步由入口模式决定，因此整条由 store 推导后传入 -->
      <ProgressSteps
        v-if="!isChoosingEntry"
        :steps="steps"
        :current-index="currentStepIndex"
        :reached-index="reachedStepIndex"
        :generating="isGeneratingPhase"
        @select="handleStepClick"
      />

      <!-- 刷新时落在无法恢复的状态：说明为什么不是原来的阶段 -->
      <p
        v-if="downgradeNotice"
        class="mb-4 rounded-xl border border-slate-200 bg-white px-4 py-2 text-xs text-slate-500"
      >
        {{ downgradeNotice }}
      </p>

      <!-- ============ 第零步：选择入口（三条链路的起点） ============ -->
      <EntryStage v-if="isChoosingEntry" @select="handleSelectEntry" />

      <!-- ============ 确认闸门 1：摘要回填结果 Diff（仅完整流程，生成 PRD 之前） ============ -->
      <SummaryDiffPanel
        v-else-if="viewState === 'summary-diff'"
        :changes="summaryChanges"
        :loading="summarySyncing"
        :error="summarySyncError"
        @confirm="confirmSummaryDiff"
        @reject="rejectSummaryDiff"
      />

      <!-- ============ 确认闸门 2：接口规范检索结果（生成接口文档之前） ============ -->
      <RagResultPanel
        v-else-if="viewState === 'rag-preview'"
        :hits="ragHits"
        :loading="ragRetrieving"
        :error="ragError"
        @confirm="confirmRag"
        @cancel="cancelRag"
      />

      <!-- ============ 入口 B：PRD 快捷（一段话 → 直接生成 PRD） ============ -->
      <QuickPrdStage
        v-else-if="viewState === 'prd-quick'"
        v-model:product-name="quickInput.productName"
        v-model:requirements="quickInput.requirements"
        @generate="handleGenerateQuickPrd"
        @back="goTo('entry')"
      />

      <!-- ============ 入口 C：提示词调试（贴入 PRD → 生成提示词套件） ============ -->
      <PromptDebugStage
        v-else-if="viewState === 'prompt-debug'"
        v-model:prd-text="debugPrdText"
        @generate="handleGeneratePromptsFromDebugPrd"
        @back="goTo('entry')"
      />

      <!-- ================= 完整流程：与 AI 对话补充需求 ================= -->
      <ChatStage
        v-else-if="viewState === 'chatting'"
        :messages="displayMessages"
        :loading="streaming"
        :error="chatError"
        :can-generate-prd="canGeneratePrd"
        @edit-requirements="goTo('form')"
        @restart="handleRestart"
        @send="handleSendMessage"
        @generate-prd="handleGeneratePrd"
      />

      <!-- 文档生成中 / 生成失败（PRD / 接口文档 / 提示词 / 优化共用） -->
      <GeneratingStage
        v-else-if="isGeneratingPhase"
        :title="activeDocTitle"
        :streaming-content="docStreamingContent"
        :error="docError"
        :generating="isGenerating"
        :prd="prdStatus"
        :back-label="backLabel"
        @back="backToInput"
      />

      <!-- 文档待确认（PRD / 接口文档 / 提示词共用） -->
      <ReviewStage
        v-else-if="isReviewing"
        :title="activeDocTitle"
        :content="activeDocContent"
        :error="docError"
        :view-state="viewState"
        :can-finish="canFinishTask"
        :has-prd="hasPrd"
        :has-api-docs="hasApiDocs"
        :has-prompts="hasPrompts"
        :back-label="backLabel"
        @save="onSaveDocument"
        @optimize="onAiOptimize"
        @back="backToInput"
        @generate-api-docs="handleGenerateApiDocs"
        @generate-prompts="handleGeneratePrompts"
        @navigate="goTo"
        @finish="handleEndTask"
      />

      <!-- 全部完成 -->
      <DoneStage
        v-else-if="viewState === 'done'"
        :prd-content="prdContent"
        :api-docs-content="apiDocsContent"
        :prompts-content="promptsContent"
        :back-label="backLabel"
        @view="goTo"
        @download="downloadDocument"
        @back="backToInput"
        @restart="handleRestart"
      />

      <!--
        完整流程的结构化表单。
        单独放在链尾且用 v-show 常驻：它是唯一需要「离开后仍保留编辑内容」的视图，
        而 `v-if/v-else-if` 链里的元素一旦失配就会被卸载、内容随之丢失。
      -->
      <RequirementsForm
        v-show="viewState === 'form'"
        :key="formKey"
        :initial="summary"
        @submit="handleStartConversation"
        @update:summary="handleSummaryChange"
      />
    </main>

    <footer class="pb-10 text-center text-xs text-slate-400">
      HarnessPRD · 由 AI 驱动的产品文档生成器
    </footer>
  </div>
</template>
