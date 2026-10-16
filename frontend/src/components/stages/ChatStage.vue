<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'

import ChatInput from '@/components/ChatInput.vue'
import MessageList from '@/components/MessageList.vue'
import AppActionBar from '@/components/ui/AppActionBar.vue'
import AppButton from '@/components/ui/AppButton.vue'
import ChatThreadTabs from '@/components/ui/ChatThreadTabs.vue'
import type { ChatMessage, ChatStage } from '@/types'

/**
 * 对话阶段视图。
 *
 * 只负责呈现消息与收集输入；发请求、推进阶段由父组件决定，
 * 因此这里不持有任何流程状态。
 *
 * **同一块对话栏承担四条澄清线程**（需求澄清 / PRD 澄清 / 接口澄清 / 提示词澄清），
 * 顶部由子标签栏切换（见 `ChatThreadTabs`）。做成同一个组件的四种模式而不是四个组件，
 * 是因为它们的差异只有「标题那两句话」与「底栏那几个按钮」，而**消息展示一模一样**——
 * 拆开就会各维护一份消息区，然后慢慢长歪。
 *
 * 消息由父组件**按当前线程过滤好**再传进来：哪条消息属于哪条线程是数据层的事
 * （`ChatMessage.stage`），视图不必重复判断一遍。
 */
const props = defineProps<{
  /** 当前线程的消息（含流式接收中的那一条） */
  messages: ChatMessage[]
  /** 子标签栏的全部标签，顺序即展示顺序 */
  tabs: readonly ChatStage[]
  /** 当前选中的标签 */
  activeTab: ChatStage
  /** 可点的标签；不在其中的置灰 */
  availableTabs: ChatStage[]
  /** 是否正在流式接收回复 */
  loading?: boolean
  /** 对话过程中的错误提示 */
  error?: string
  /** 是否已满足「生成 PRD」的条件（需求澄清线程的主操作） */
  canGeneratePrd?: boolean
  /** 澄清线程的主操作「按澄清结果改写」是否可用 */
  canRewrite?: boolean
  /** 是否正在改写中 */
  rewriting?: boolean
  /** 有请求在跑；此时子标签栏全部禁用 */
  busy?: boolean
}>()

const emit = defineEmits<{
  /** 切换到另一条澄清线程 */
  selectTab: [stage: ChatStage]
  /** 点击「改需求」 */
  editRequirements: []
  /** 点击「重新开始」 */
  restart: []
  /** 发送一条消息 */
  send: [content: string]
  /** 点击「生成 PRD」 */
  generatePrd: []
  /** 点击「按澄清结果改写」（三条文档线程的主操作） */
  rewrite: []
}>()

/**
 * 是否停在**需求澄清**线程。
 *
 * 它决定两件事：底栏给「生成 PRD」还是「按澄清结果改写」，以及要不要出现「重新开始」——
 * 那个按钮会清掉整份会话（连同已生成的文档），在澄清文档时点错的代价太大。
 */
const onRequirement = computed(() => props.activeTab === 'requirement')

/** 头部标题：说清这一栏在聊的是哪份东西 */
const heading = computed(() => {
  switch (props.activeTab) {
    case 'prd-clarify':
      return 'PRD 待确认项澄清'
    case 'api-docs-clarify':
      return '接口文档待确认项澄清'
    case 'prompts-clarify':
      return '提示词套件待确认项澄清'
    default:
      return '需求澄清对话'
  }
})

/**
 * 头部副标题。
 *
 * 刻意不写具体数字：提问上限是后端提示词的口径（见 ADR-010），文案里再落一份数字
 * 就等于又多出一个需要手工同步的副本。
 */
const subheading = computed(() =>
  onRequirement.value
    ? 'AI 会追问关键细节，逐步澄清后再生成文档'
    : '按 P0 → P6 逐级确认剩余问题；聊够了随时可以写回文档',
)

/** 改写按钮的禁用态：这一轮还没聊出结论、或正在改写中，都点不动 */
const rewriteDisabled = computed(() => !props.canRewrite || Boolean(props.rewriting))

/** 输入框实例，用于在一轮回复结束后把焦点交还给用户 */
const inputRef = ref<InstanceType<typeof ChatInput> | null>(null)

// 回复结束（流式状态由 true 落回 false）时聚焦输入框，用户可以直接接着追问。
// 放在这里而不是父组件：焦点归还纯属视图行为，没必要为它向外暴露组件句柄。
watch(
  () => props.loading,
  (now, before) => {
    if (!before || now) return
    void nextTick(() => inputRef.value?.focus())
  },
)
</script>

<template>
  <section class="space-y-3">
    <!-- 子标签栏：四条澄清线程常驻，切换即切换对话历史与底栏动作 -->
    <ChatThreadTabs
      :tabs="tabs"
      :active="activeTab"
      :available="availableTabs"
      :busy="busy"
      @select="emit('selectTab', $event)"
    />

    <!--
      对话卡片只负责消息区。输入框已下沉到页面级底栏的内容行里——
      与「重新开始 / 改需求 / 生成 PRD」同处一条栏，用户不必在卡片底部与页面底部之间来回找。
    -->
    <div class="overflow-hidden rounded-2xl border border-line bg-surface shadow-sm">
      <div class="border-b border-line px-4 py-3 sm:px-6">
        <h2 class="text-sm font-semibold text-fg">{{ heading }}</h2>
        <p class="mt-0.5 text-xs text-subtle">{{ subheading }}</p>
      </div>

      <!-- 出错时把原因贴在会话区顶部，不打断已有消息 -->
      <p
        v-if="error"
        class="border-b border-red-200 bg-red-50 px-4 py-2 text-xs leading-relaxed text-red-600 dark:border-red-800 dark:bg-red-950/40 dark:text-red-300 sm:px-6"
        role="alert"
      >
        {{ error }}
      </p>

      <!-- 消息区：内部滚动。输入框移出后，这里就是一个纯粹的滚动容器 -->
      <div class="flex h-[min(60vh,560px)] flex-col bg-surface-2">
        <MessageList :messages="messages" />
      </div>
    </div>

    <!--
      页面级操作栏。
      需求澄清线程：左「重新开始 │ 改需求」、右「生成 PRD」；
      三条文档澄清线程：只有右端的主操作「按澄清结果改写」——回审阅页靠顶部进度条，
      不必再占一个按钮（ADR-028 的取舍）。
    -->
    <AppActionBar>
      <template #tools>
        <ChatInput ref="inputRef" embedded :loading="loading" @send="emit('send', $event)" />
      </template>

      <!-- 「重新开始」会连同前面的需求澄清与已生成的文档一起清掉，点错的代价太大，
           因此只在需求澄清线程里出现 -->
      <template #danger>
        <AppButton v-if="onRequirement" variant="danger" size="md" @click="emit('restart')">
          重新开始
        </AppButton>
      </template>
      <template #secondary>
        <AppButton v-if="onRequirement" variant="secondary" size="md" @click="emit('editRequirements')">
          改需求
        </AppButton>
      </template>

      <!-- 需求澄清线程的主操作：聊够了去生成 PRD -->
      <AppButton
        v-if="onRequirement && canGeneratePrd"
        variant="primary"
        size="md"
        @click="emit('generatePrd')"
      >
        <svg class="h-4 w-4" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
          <path
            fill-rule="evenodd"
            d="M11.3 1.046A1 1 0 0112 2v5h4a1 1 0 01.82 1.573l-7 10A1 1 0 018 18v-5H4a1 1 0 01-.82-1.573l7-10a1 1 0 011.12-.38z"
            clip-rule="evenodd"
          />
        </svg>
        生成 PRD
      </AppButton>

      <!-- 文档线程的主操作：把这一轮的结论写回文档（进行中时由 AppButton 承载 loading 态） -->
      <AppButton
        v-else-if="!onRequirement"
        variant="primary"
        size="md"
        :disabled="rewriteDisabled"
        @click="emit('rewrite')"
      >
        {{ rewriting ? '正在改写…' : '按澄清结果改写' }}
      </AppButton>
    </AppActionBar>
  </section>
</template>
