import { flushPromises, mount, type DOMWrapper, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia, type Pinia } from 'pinia'
import { nextTick, defineComponent, h } from 'vue'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import AppV2 from '../AppV2.vue'
import AppBadge from '../components/ui/AppBadge.vue'
import ChatStage from '../components/stages/ChatStage.vue'
import EntryStage from '../components/stages/EntryStage.vue'
import ReviewStage from '../components/stages/ReviewStage.vue'
import ReviewConclusionPanel from '../components/ui/ReviewConclusionPanel.vue'
import * as api from '../services/api'
import * as authService from '../services/authService'
import * as documentApi from '../services/documentService'
import * as solutionApi from '../services/solutionService'
import { useAuthStore } from '../stores/auth'
import { DOCUMENT_SAVE_DEBOUNCE_MS, useDocumentsStore } from '../stores/documents'
import { useSolutionsStore } from '../stores/solutions'
import { useWorkflowStore } from '../stores/workflow'
import { downloadFile } from '../utils/download'
import { ENTRY_REGISTRY, type EntrySpec } from '../utils/workflow'
import type {
  ChatMessage,
  DocumentRecord,
  DocumentVersionDetail,
  DocumentVersionSummary,
  EntryMode,
  JobSnapshot,
  RequirementsSummary,
  ReviewConclusion,
  RunSummary,
  StreamDocumentPart,
  StreamStageEvent,
} from '../types'
import { toState } from './support/solutionState'

/**
 * 生成任务撮合器。
 *
 * 流水线已从「一次请求」升级为「一个后端任务」：`useJob` 先 `createJob`、再
 * `readJobStream`。测试要按 **kind** 决定这次任务该产出哪一类事件，而
 * `readJobStream` 只拿得到 `jobId`——因此这里用一张 `jobId → kind` 的表把两者接上，
 * 让每个用例只需配置「prd 这条流怎么跑」，不必关心它拿到的是哪个 id。
 */
/**
 * 对话轮次的登记表：`readJobStream` 产出回复时写进来，`fetchTurns` 再读出去。
 *
 * 为什么要这张表：消息的真源已经搬到服务端 `chat_turns`，前端不再自己 push。
 * 因此「AI 说了一句话」在测试里要分两步表达——任务产出它、轮次接口返回它，
 * 缺任何一步都说明链路真的断了。
 */
const turnHarness = vi.hoisted(() => ({ items: [] as unknown[] }))

/**
 * 对话开场白的造数开关。
 *
 * 对话已经是一个**后端任务**（见 `useClarificationChat`）：「AI 说了什么」要分两步表达——
 * 任务产出回复、服务端把它写成轮次，前端再靠 `fetchTurns` 读回来。因此这里只提供「这条
 * 线程该说什么」这一个句柄，写入轮次那一步由 `defaultJobStream` 统一做（见下方 chat 分支），
 * 免得每个用例各拼一份轮次、慢慢长歪。
 *
 * 默认为空 = 用那句通用的需求澄清开场白。
 */
const chatHarness = vi.hoisted(() => ({
  /**
   * 按任务载荷决定这条线程的开场白。
   *
   * 载荷里能拿到三件关键信息：`chatStage`（哪条线程）、`chatMode`（开场 / 续聊）、
   * `formData`（文档澄清提交的正文与待确认清单）。部分用例还要让助手带上
   * 结构化选择题（`asks`）——它就是服务端轮次上的那个字段。
   */
  opening: null as null | ((payload: Record<string, unknown>) => {
    reply: string
    asks?: unknown[]
  }),
}))

const jobHarness = vi.hoisted(() => {
  const kinds = new Map<number, string>()
  let nextId = 1
  return {
    kinds,
    register(kind: string): number {
      const id = nextId
      nextId += 1
      kinds.set(id, kind)
      return id
    },
    reset(): void {
      kinds.clear()
      nextId = 1
    },
  }
})

/**
 * 编排层测试：把流式接口整体 mock 掉，专注验证「第一步换成结构化表单」后
 * 阶段流转、载荷映射、本地快照恢复，以及摘要确认闸门
 * 是否按「先确认、后生成」的约定工作。
 */
vi.mock('../services/api', () => ({
  // ⚠️ 对话已经走任务通道（`createJob` + `readJobStream`），下面这两个裸流式接口
  // 应用里已经没有任何调用点。仍然列出来是为了让 `import * as api` 的形状与真实模块一致，
  // 同时也让「有没有人误用回旧通道」这件事可以被断言（`not.toHaveBeenCalled`）。
  startConversationStream: vi.fn(),
  continueConversationStream: vi.fn(),
  createJob: vi.fn(async (payload: { kind: string }) => ({
    jobId: jobHarness.register(payload.kind),
    status: 'running',
  })),
  readJobStream: vi.fn(),
  fetchActiveJobs: vi.fn(async () => []),
  fetchJob: vi.fn(),
  syncSummaryFromConversation: vi.fn(),
  onUnauthorized: vi.fn(),
}))

/**
 * 登录态 mock：默认**已登录**。
 *
 * 本文件测的是流程编排，登录门禁单独在 `auth.spec.ts` 里覆盖；
 * 这里若按未登录跑，所有用例都会被「请先登录」拦在第一关。
 */
vi.mock('../services/authService', () => ({
  fetchCurrentUser: vi.fn(async () => ({ id: 1, username: 'tester' })),
  login: vi.fn(async () => ({ id: 1, username: 'tester' })),
  register: vi.fn(async () => ({ id: 1, username: 'tester' })),
  logout: vi.fn(async () => undefined),
}))

/**
 * 方案持久化 mock。
 *
 * 编排测试关心的是**流程流转**，不是「什么时候把快照存到后端」——那有专门的
 * `solutions` 测试。这里给一组无害实现，避免每挂载一次就真的发请求。
 */
vi.mock('../services/solutionService', () => ({
  SolutionNotFoundError: class SolutionNotFoundError extends Error {
    constructor() {
      super('方案不存在或已被删除')
      this.name = 'SolutionNotFoundError'
    }
  },
  fetchSolutions: vi.fn(async () => []),
  // ⚠️ 必须返回**新数组**（真实实现也是 `data.turns.map(...)`）：`setTurns` 是
  // `messages.value = turns` 整体替换，若每次给同一个数组对象，这次赋值等于没变，
  // 依赖 `store.messages` 的计算属性（「按澄清结果改写」是否可点）就不会重算
  fetchTurns: vi.fn(async () => [...turnHarness.items]),
  // 作废一条线程：服务端把那一轮的轮次删掉，前端随后重拉时才不会「删了又长回来」
  clearTurns: vi.fn(async (_id: number, stage: string) => {
    turnHarness.items = turnHarness.items.filter(
      (item) => (item as { stage?: string }).stage !== stage,
    )
  }),
  fetchSolution: vi.fn(async () => {
    throw new Error('本测试不加载已有方案')
  }),
  createSolution: vi.fn(async () => ({
    id: 1,
    title: '测试方案',
    mode: 'structured',
    stage: 'form',
    created_at: '',
    updated_at: '',
  })),
  updateSolution: vi.fn(async () => null),
  deleteSolution: vi.fn(async () => undefined),
}))

/**
 * 产物正文 mock。
 *
 * 三份文档的权威来源是后端 `documents` 表：打开方案时会拉一次，生成收尾时也会
 * 拉一次。编排测试不关心这张表本身（那有 `documents-store` 与 `document-service`
 * 各自的用例），这里只保证「打开方案」这条路径不会去打真实网络。
 * 需要断言正文/回执的用例会用 `mountExistingSolution` 的第三个参数喂具体数据。
 */
vi.mock('../services/documentService', () => ({
  DocumentsNotFoundError: class DocumentsNotFoundError extends Error {
    constructor() {
      super('方案不存在或已被删除')
      this.name = 'DocumentsNotFoundError'
    }
  },
  DocumentVersionConflictError: class DocumentVersionConflictError extends Error {
    constructor(message = '正文已被更新，请刷新后重试') {
      super(message)
      this.name = 'DocumentVersionConflictError'
    }
  },
  fetchDocuments: vi.fn(async () => []),
  saveDocument: vi.fn(),
  fetchVersions: vi.fn(async () => []),
  fetchVersion: vi.fn(),
  checkpointDocument: vi.fn(),
  restoreVersion: vi.fn(),
  deleteVersion: vi.fn(),
}))

/**
 * 下载工具的**部分** mock：只把 `downloadFile` 换成探针（用来断言「导出的是哪一版正文、
 * 叫什么文件名」），`buildDocFilename` / `DOC_TYPE_LABELS` 仍走真实实现——文件名拼装本身
 * 有 `download.spec.ts` 覆盖，这里不必再验一遍它的规则。
 *
 * 必须 mock：jsdom 不实现 `URL.createObjectURL`，真跑一次下载会直接抛异常。
 */
vi.mock('../utils/download', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../utils/download')>()
  return { ...actual, downloadFile: vi.fn() }
})

/**
 * 子组件渲染失败的注入开关。
 *
 * 「失败可见」那条验收要验证的是**错误边界**：后代组件在渲染时抛错，界面必须给出一句
 * 明确的「暂时不可用」，而不是整页空白。注入点选「我有 PRD」的录入页——本文件没有
 * 任何用例拿它的组件实例做断言（只按屏幕上的文字断言），因此平时原样透传，
 * 只有那一条用例把它打开。
 *
 * 它模拟的正是维护者漏填一列的情形：界面要读的东西在查表那一刻就没了。
 */
const stageRenderFault = vi.hoisted(() => ({ failingStage: '' }))

vi.mock('../components/stages/PromptDebugStage.vue', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../components/stages/PromptDebugStage.vue')>()
  return {
    default: defineComponent({
      name: 'FaultInjectingPromptDebugStage',
      // 本组件不声明任何 props：props 与事件都按「未声明的属性」透传到它渲染的根节点上，
      // 因此平时它就是真实录入页的一层薄壳，行为与直接渲染真实组件一致。
      setup() {
        if (stageRenderFault.failingStage === 'prompt-debug') {
          throw new Error('假第 5 入口：这一行没填全，渲染当场失败')
        }
        return () => h(actual.default)
      },
    }),
  }
})

/**
 * 默认任务流：按 kind 产出一段可断言的最小正文。
 *
 * 三类文档各自有可辨识的标题，断言「PRD 文档 / 提示词套件」时就不必再各自配置流；
 * 需要观察中间态的用例再用 `mockJobStream` 覆盖对应的那条。
 */
async function defaultJobStream(
  kind: string,
  handlers: api.StreamHandlers,
): Promise<api.StreamResult> {
  if (kind === 'prompts') {
    handlers.onChunk?.({ text: '# 提示词套件\n' })
    return { conversationId: '', reply: '# 提示词套件\n\n## 一、角色设定\n内容', asks: [] }
  }
  if (kind === 'api-docs') {
    handlers.onChunk?.({ text: '# 接口文档\n' })
    return { conversationId: '', reply: '# 接口文档\n\n## 一、概述\n统一响应结构', asks: [] }
  }
  if (kind === 'optimize') {
    return { conversationId: '', reply: '优化后的内容', asks: [] }
  }
  if (kind === 'chat') {
    const payload = jobPayload('chat')
    const asRecord = (payload ?? {}) as unknown as Record<string, unknown>
    const opening = chatHarness.opening?.(asRecord)
    const reply = opening?.reply ?? '我先复述一下理解：你想做一个周报工具。'
    const stage = payload?.chatStage ?? 'requirement'
    handlers.onChunk?.({ text: reply })
    // 续聊时服务端会先落一条「用户这一轮」的轮次（作答时它还带着 `answers`，界面据此
    // 渲染回执卡），再落助手那条。两条都要造：前端不再自己拼消息，只读服务端轮次。
    if (payload?.chatMode === 'continue') {
      const toolResult = payload.toolResult as { answers?: unknown[] } | null | undefined
      turnHarness.items.push({
        id: turnHarness.items.length + 1,
        role: 'user',
        content: payload.content ?? '',
        stage,
        ...(toolResult?.answers?.length ? { answers: toolResult.answers } : {}),
      })
    }
    // 服务端在收尾时把这一轮写成轮次——前端随后靠 `fetchTurns` 读到它
    turnHarness.items.push({
      id: turnHarness.items.length + 1,
      role: 'assistant',
      content: reply,
      stage,
      ...(opening?.asks?.length ? { asks: opening.asks } : {}),
    })
    return { conversationId: '', reply, asks: [] }
  }
  handlers.onChunk?.({ text: '# 产品需求文档\n' })
  return { conversationId: '', reply: '# 产品需求文档\n\n## 1. 项目背景\n内容', asks: [] }
}

/**
 * 覆盖某些 kind 的任务流；未覆盖的 kind 沿用 `defaultJobStream`。
 *
 * 单参数设计让用例只需声明「prd 这条流怎么跑」，其余文档仍按默认走完，
 * 不会因为覆盖了一个 kind 而把别的链路一并打断。
 */
function mockJobStream(
  overrides: Partial<Record<string, (handlers: api.StreamHandlers) => Promise<api.StreamResult>>> = {},
): void {
  vi.mocked(api.readJobStream).mockImplementation(async (jobId, handlers) => {
    const kind = jobHarness.kinds.get(jobId) ?? 'prd'
    const override = overrides[kind]
    if (override) return override(handlers ?? {})
    return defaultJobStream(kind, handlers ?? {})
  })
}

/** 某一类文档被创建了几次 */
function jobCount(kind: string): number {
  return vi.mocked(api.createJob).mock.calls.filter((call) => call[0]?.kind === kind).length
}

/** 取某一类文档最近一次的任务创建载荷（同种文档可能被重跑，取最后一次） */
function jobPayload(kind: string): api.CreateJobPayload | undefined {
  const calls = vi.mocked(api.createJob).mock.calls.filter((call) => call[0]?.kind === kind)
  return calls[calls.length - 1]?.[0]
}

/**
 * 落库过的方案状态（新建 + 更新的全部载荷，按发生顺序）。
 *
 * 上游正文不再随任务下发（spec #51 / 票 #54）：贴入的 PRD / 接口文档先落进方案状态，
 * 任务创建时后端按方案自己取。因此「材料真的送到了后端」这条断言的新落点是**这里**，
 * 而不是 `createJob` 的载荷——后者只应该出现「做什么、对哪一份」。
 */
function savedSolutionStates(): Record<string, unknown>[] {
  const states: Record<string, unknown>[] = []
  for (const call of vi.mocked(solutionApi.createSolution).mock.calls) {
    const state = call[0]?.state
    if (state) states.push({ ...state } as Record<string, unknown>)
  }
  for (const call of vi.mocked(solutionApi.updateSolution).mock.calls) {
    const state = call[1]?.state
    if (state) states.push({ ...state } as Record<string, unknown>)
  }
  return states
}

/** 方案状态里某一列是否出现过这段正文（对象列按 JSON 串比对） */
function savedStateContains(field: string, fragment: string): boolean {
  return savedSolutionStates().some((state) => {
    const value = state[field]
    if (value == null) return false
    const text = typeof value === 'string' ? value : JSON.stringify(value)
    return text.includes(fragment)
  })
}

/** 造一份任务快照（刷新重连时后端首发的事件） */
function makeJobSnapshot(overrides: Partial<JobSnapshot> = {}): JobSnapshot {
  return {
    jobId: 1,
    kind: 'prd',
    // 目标产物：后端 `doc_kind` 的权威值。前端靠它知道「这次写的是哪一份」，
    // 不再需要按当前阶段去猜（`optimize` / `clarify` 猜不出来）。
    docKind: 'prd',
    status: 'running',
    phase: '',
    finalizedText: '',
    liveBuffer: '',
    stageText: '',
    solutionId: 1,
    errorCode: '',
    errorMessage: '',
    elapsedMs: 0,
    retryAt: null,
    pendingItems: [],
    partial: false,
    failedSections: [],
    ...overrides,
  }
}

/** 造一份用量回执（`run_summary` 事件的载荷） */
function makeRunSummary(overrides: Partial<RunSummary> = {}): RunSummary {
  return {
    kind: 'prd',
    status: 'succeeded',
    durationMs: 1000,
    totalCalls: 1,
    failedCalls: 0,
    promptTokens: 10,
    completionTokens: 20,
    steps: [],
    injectedSkills: [],
    warnings: [],
    partial: false,
    failedSections: [],
    protocol: 'ark',
    providerConfigId: 'cfg_ark0001',
    providerName: '火山方舟',
    model: 'doubao',
    requestId: 'req-default',
    ...overrides,
  }
}

/** 造一条「本方案此刻在跑的任务」摘要（`GET /api/jobs` 的返回项），供恢复路径用例使用 */
function makeJobStatus(overrides: Partial<api.JobStatusSummary> = {}): api.JobStatusSummary {
  return {
    id: 1,
    kind: 'prd',
    docKind: 'prd',
    chatStage: null,
    status: 'running',
    phase: 'writing',
    solutionId: 1,
    errorCode: '',
    errorMessage: '',
    createdAt: '2026-11-08T00:00:00',
    updatedAt: '2026-11-08T00:00:00',
    usage: null,
    ...overrides,
  }
}

/** 按 id 查找元素（结构化行级 id 含点号，须用属性选择器） */
function byId(wrapper: VueWrapper | DOMWrapper<Element>, id: string): DOMWrapper<Element> {
  return wrapper.find(`[id="${id}"]`)
}

/**
 * 点击按钮。
 * 默认按「包含」匹配（编辑器里的按钮文案常带前后缀），传 `exact` 时要求文案完全相等——
 * 用于消歧，例如摘要面板的「确认更新，生成 PRD」也包含「生成 PRD」。
 */
async function clickButton(
  wrapper: VueWrapper | DOMWrapper<Element>,
  text: string,
  exact = false,
): Promise<void> {
  const buttons = wrapper.findAll('button')
  const button = exact
    ? buttons.find((item) => item.text().trim() === text)
    : buttons.find((item) => item.text().includes(text))
  if (!button) throw new Error(`未找到按钮：${text}`)
  await button.trigger('click')
}

/**
 * 审阅页的澄清入口按钮文案。
 *
 * 四种入口下都是这一个（旁边并列的「AI 自行澄清」是另一个动作：让 AI 自己拍板），
 * 点击统一落到当前那份文档的澄清线程上。
 */
const CLARIFY_ENTRY_TEXT = '前往澄清问题'

/** 点击进度条上的某个步骤（步骤是带 @click 的 div，不是 button） */
async function clickStep(wrapper: VueWrapper, label: string): Promise<void> {
  const item = wrapper.findAll('nav li').find((li) => li.text().includes(label))
  if (!item) throw new Error(`未找到进度步骤：${label}`)
  await item.find('div').trigger('click')
}

/**
 * 点击弹窗（Teleport 到 `document.body`）里文案完全等于 `text` 的按钮。
 *
 * 弹窗不在 `wrapper.element` 之内，`wrapper.find` 搜不到，只能直接查 DOM。
 */
async function clickButtonInDialog(text: string): Promise<void> {
  const button = Array.from(document.body.querySelectorAll('button')).find(
    (item) => item.textContent?.trim() === text,
  )
  if (!button) throw new Error(`弹窗里未找到按钮：${text}`)
  button.click()
  await flushPromises()
}

/** 构造一份可用于提交的完整摘要 */
const SAMPLE_SUMMARY: RequirementsSummary = {
  product_name: 'PRDwright',
  product_goal: '把模糊想法整理成可开工的 PRD',
  target_users: ['独立开发者'],
  platform: '网页应用',
  mvp_features: [{ name: '问卷填写', description: '收集产品信息' }],
}

/**
 * 逐项填出与 `SAMPLE_SUMMARY` 完全一致的表单。
 * 不用「填充示例」按钮，是为了让字段映射断言有确定的期望值。
 */
async function fillSampleForm(wrapper: VueWrapper): Promise<void> {
  await byId(wrapper, 'field-product_name').setValue(SAMPLE_SUMMARY.product_name)
  await byId(wrapper, 'field-product_goal').setValue(SAMPLE_SUMMARY.product_goal)
  await byId(wrapper, 'field-platform').setValue(SAMPLE_SUMMARY.platform)

  await clickButton(wrapper, '添加用户角色')
  await byId(wrapper, 'field-target_users.0').setValue('独立开发者')

  await clickButton(wrapper, '添加功能')
  await byId(wrapper, 'field-mvp_features.0.name').setValue('问卷填写')
  await byId(wrapper, 'field-mvp_features.0.description').setValue('收集产品信息')
}

/** 让「提交需求 → 对话」这条链路走通：填样例表单并提交 */
async function submitSampleForm(wrapper: VueWrapper): Promise<void> {
  await fillSampleForm(wrapper)
  await clickButton(wrapper, '提交需求')
  await flushPromises()
}

/** 在摘要确认面板上点确认（文案随有无变更而变，用正则兜住） */
async function confirmSummaryPanel(wrapper: VueWrapper): Promise<void> {
  const button = wrapper
    .findAll('button')
    .find((item) => /确认更新|直接生成 PRD|跳过更新/.test(item.text()))
  if (!button) throw new Error('未找到摘要确认按钮')
  await button.trigger('click')
  await flushPromises()
}

/** 走完「提交需求 → 确认摘要 → 生成 PRD」，停在 PRD 待确认 */
async function reachPrdReview(wrapper: VueWrapper): Promise<void> {
  await submitSampleForm(wrapper)
  await clickButton(wrapper, '生成 PRD', true)
  await flushPromises()
  await confirmSummaryPanel(wrapper)
}

/** 每个用例前重置 mock，并给出各接口的安全默认实现 */
beforeEach(() => {
  // 任务 id 从头开始，避免跨用例串号（登记表只在 `createJob` 成功时增长）
  jobHarness.reset()
  // `turnHarness` 是**普通数组**，`vi.clearAllMocks()` 清不掉它（那只清 mock 的调用记录）：
  // 不重置的话上一个用例产出的轮次会漏进下一个用例的对话里，表现为「单独跑通过、整文件跑挂」
  turnHarness.items.length = 0
  chatHarness.opening = null
  vi.clearAllMocks()

  // 默认：回填结果与当前摘要一致（无变更）
  vi.mocked(api.syncSummaryFromConversation).mockImplementation(async ({ summary }) => ({
    summary,
    changed: false,
  }))
  // 默认：三类文档各自生成成功（按任务 kind 派发，见 `defaultJobStream`）
  mockJobStream()
  // 默认：没有还在跑的活跃任务（兜底查询落空，保持降级提示）
  vi.mocked(api.fetchActiveJobs).mockResolvedValue([])
  // 默认：轮次接口读的就是本用例造的那份轮次表。
  // ⚠️ 必须**重新装实现**而不只是 `clearAllMocks()`——后者只清调用记录，动不了
  // `mockResolvedValue`；某个用例一旦钉死返回值，后面所有用例都会读到它的对话
  // （表现为「单独跑通过、整文件跑挂」的经典形状）。
  vi.mocked(solutionApi.fetchTurns).mockImplementation(
    async () => [...turnHarness.items] as ChatMessage[],
  )
  vi.mocked(api.startConversationStream).mockReset()
  vi.mocked(api.continueConversationStream).mockReset()

  // 方案持久化：默认给一组无害实现，避免每挂载一次就真的发请求。
  // 需要「打开已有方案」的用例会各自覆盖 `fetchSolution`。
  vi.mocked(solutionApi.fetchSolutions).mockResolvedValue([])
  vi.mocked(solutionApi.fetchSolution).mockRejectedValue(
    new solutionApi.SolutionNotFoundError(),
  )
  vi.mocked(solutionApi.createSolution).mockResolvedValue({
    id: 1,
    title: '测试方案',
    mode: 'structured',
    stage: 'form',
    created_at: '2026-10-13T00:00:00',
    updated_at: '2026-10-13T00:00:00',
    content_updated_at: '2026-10-13T00:00:00',
  })
  vi.mocked(solutionApi.updateSolution).mockImplementation(async (id, payload) => ({
    id,
    title: payload.title,
    mode: payload.mode,
    stage: payload.stage,
    created_at: '2026-10-13T00:00:00',
    updated_at: '2026-10-13T00:00:00',
    content_updated_at: '2026-10-13T00:00:00',
    state: payload.state,
  }))
  vi.mocked(solutionApi.deleteSolution).mockResolvedValue(undefined)

  // 文档接口：默认「这份方案还没有任何文档」。打开方案的用例会各自喂具体内容。
  vi.mocked(documentApi.fetchDocuments).mockResolvedValue([])
})

/**
 * 造一个**已登录**的 Pinia 实例。
 *
 * 登录态探测（`auth.load()`）是异步的，若让每个用例都等它落地，挂载辅助函数
 * 就得全部改成 async。这里直接预置「已探测完 + 已登录」，把登录门禁这一层
 * 从编排测试中摘出去——它有自己的 `auth.spec.ts`。
 */
function loggedInPinia(): Pinia {
  const pinia = createPinia()
  setActivePinia(pinia)
  const auth = useAuthStore(pinia)
  auth.user = { id: 1, username: 'tester' }
  auth.ready = true
  return pinia
}

/**
 * 造一个「尚未探测登录态」的 Pinia（用户为空、探测未完成）。
 *
 * 登录态探测是首屏触发「恢复方案」的**第二条路径**：`auth.load()` 由 AppV2 在 setup
 * 阶段发起，探测完成后 `isLoggedIn` 才由 false 翻成 true。`loggedInPinia()` 预置了
 * 登录态，这条路径不会发生；要复现「首屏同时被两个入口触发」就必须用它。
 */
function pendingLoginPinia(): Pinia {
  const pinia = createPinia()
  setActivePinia(pinia)
  return pinia
}

/**
 * 造一个真实的 router（内存 history）。
 *
 * AppV2 现在从路由参数里读方案 id，因此挂载时必须提供 router——
 * 用 mock 一个假的 `useRoute` 会让「地址真的变了」这件事测不到，
 * 而地址变化正是「首次保存后拿到 id」的表现。
 */
function makeRouter(): Router {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/v2', component: AppV2, props: { solutionId: null } },
      { path: '/v2/:id', component: AppV2, props: true },
    ],
  })
}

/** 挂载应用，停在入口选择页 */
async function mountEntry(initialPath = '/v2'): Promise<VueWrapper> {
  const router = makeRouter()
  // 必须在挂载**之前**把导航落地：AppV2 在 setup 阶段（`immediate` watch）就会读
  // `route.params.id` 去拉方案，若导航还在 pending，读到的永远是 undefined，
  // 看起来就像「没走恢复逻辑」——而「走没走恢复逻辑」正是这批用例要验证的。
  await router.push(initialPath)
  const wrapper = mount(AppV2, { global: { plugins: [loggedInPinia(), router] } })
  await router.isReady()
  await flushPromises()
  return wrapper
}

/**
 * 挂载应用并选择「完整流程」入口。
 * 绝大多数用例验证的是完整流程内部的行为，入口选择对它只是前置动作。
 */
async function mountApp(): Promise<VueWrapper> {
  const wrapper = await mountEntry()
  await clickButton(wrapper, '完整流程')
  return wrapper
}

/**
 * 造一份后端方案详情的 mock 返回值。
 *
 * 快照现在是**后端**存的（`solutions.data`），因此「刷新后恢复」这类用例的造数方式
 * 从「往 localStorage 写一份」改成了「让 `fetchSolution` 返回一份」——恢复路径本身
 * 也从同步读改成了异步请求，用例必须 `await` 挂载后的那次请求。
 */
function makeSolutionDetail(
  id: number,
  snapshot: Record<string, unknown>,
  overrides: Partial<{ title: string; mode: string; stage: string }> = {},
) {
  return {
    id,
    title: overrides.title ?? '测试方案',
    mode: overrides.mode ?? 'structured',
    stage: overrides.stage ?? 'form',
    created_at: '2026-10-13T00:00:00',
    updated_at: '2026-10-13T00:00:00',
    content_updated_at: '2026-10-13T00:00:00',
    state: toState(snapshot),
  }
}

/**
 * 挂载一份「已存在」的方案：让 `fetchSolution` 返回指定快照，并从 `/v2/{id}` 进入。
 * AppV2 在登录后按地址拉详情，所以恢复动作发生在挂载后的异步阶段，这里等它就绪。
 *
 * 第三个参数是这份方案在三份产物上的内容（正文 + 各自回执）。它们**不在快照里**——
 * 打开方案会另外拉一次 `documents` 接口，所以这里随快照一起把那次响应也备好。
 */
async function mountExistingSolution(
  snapshot: Record<string, unknown>,
  id = 1,
  documents: DocumentRecord[] = [],
): Promise<VueWrapper> {
  const detail = makeSolutionDetail(id, snapshot)
  vi.mocked(solutionApi.fetchSolution).mockResolvedValue(detail)
  vi.mocked(documentApi.fetchDocuments).mockResolvedValue(documents)
  return mountEntry(`/v2/${id}`)
}

/** 造一条文档记录（正文 + 可选回执 + 可选待确认清单），供「打开方案」类用例使用 */
function makeDocumentRecord(
  kind: DocumentRecord['kind'],
  content: string,
  runSummary: RunSummary | null = null,
  pendingItems: DocumentRecord['pendingItems'] = [],
  review: DocumentRecord['review'] = null,
): DocumentRecord {
  return {
    kind,
    content,
    runSummary,
    pendingItems,
    review,
    // 「这一版上次重审没跑成」的时间：落库事实（US3），默认没有
    reviewFailedAt: null,
    updatedAt: '2026-10-13T00:00:00',
    stale: false,
    versionNo: 0,
    // 「这份正文可用但不完整」（票 #85）：默认完整；要测部分完成时显式覆盖
    partial: false,
    derivedFrom: null,
  }
}

/**
 * 从刚挂载的那棵应用树上取 workflow store。
 *
 * 不用裸 `useWorkflowStore()`：它取的是模块级的「当前活跃 pinia」，而本文件里每个用例
 * 都会挂载自己的应用、把活跃 pinia 换成新的；一旦别的用例有异步副作用跨越了用例边界，
 * 断言时取的就可能不是这个应用的 store（表现为「单独跑通过、整文件跑失败」）。
 * 直接从组件的应用上下文里取，断言才只反映这一次挂载。
 */
function storeOf(wrapper: VueWrapper): ReturnType<typeof useWorkflowStore> {
  const pinia = wrapper.vm.$.appContext.config.globalProperties.$pinia as Pinia
  return useWorkflowStore(pinia)
}

/**
 * 同 `storeOf`，取这个应用自己的文档 store。
 *
 * 与 workflow store 一样，不能裸调 `useDocumentsStore()`——那取的是模块级的活跃 pinia。
 */
function documentsOf(wrapper: VueWrapper): ReturnType<typeof useDocumentsStore> {
  const pinia = wrapper.vm.$.appContext.config.globalProperties.$pinia as Pinia
  return useDocumentsStore(pinia)
}

/** 同 `storeOf`，取这个应用自己的方案 store */
function solutionsOf(wrapper: VueWrapper): ReturnType<typeof useSolutionsStore> {
  const pinia = wrapper.vm.$.appContext.config.globalProperties.$pinia as Pinia
  return useSolutionsStore(pinia)
}

/** 一份「停在某阶段、没有任何进行中任务」的最小快照 */
function reviewSnapshot(viewState: string): Record<string, unknown> {
  return {
    viewState,
    entryMode: 'structured',
    summary: null,
    messages: [],
    chatTab: 'requirement',
    conversationIds: {
      requirement: '',
      'prd-clarify': '',
      'api-docs-clarify': '',
      'prompts-clarify': '',
    },
    quickInput: { productName: '', requirements: '' },
    debugPrdText: '',
    pastePairInput: { prd: '', apiDocs: '' },
    activeJobId: null,
    activeJobKind: null,
  }
}

describe('AppV2 完整流程编排', () => {
  it('默认停在入口选择页：两组四个入口都可见，且尚未发起任何请求', async () => {
    const wrapper = await mountEntry()

    expect(wrapper.text()).toContain('新建 PRD 方案')
    // 两组：先回答「我属于哪种情况」，再挑具体入口
    expect(wrapper.text()).toContain('从零创建 PRD')
    expect(wrapper.text()).toContain('导入已有素材')
    // 四个叶子**同时可见**：分组是加一层语义，不是多一屏要点的关卡
    for (const label of ['完整流程', '快捷流程', '我有 PRD', '我有 PRD + 接口文档']) {
      expect(wrapper.text(), label).toContain(label)
    }
    // 入口页还没有进度可言，进度条整体不出现
    expect(wrapper.find('nav[aria-label="生成进度"]').exists()).toBe(false)
    expect(api.startConversationStream).not.toHaveBeenCalled()
  })

  it('四个叶子各自落到自己的录入页（分组只是入口页的排布，不改变落点）', async () => {
    const expected: Array<[string, string]> = [
      ['完整流程', '告诉我们你的产品想法'],
      ['快捷流程', '直接生成 PRD'],
      ['我有 PRD', '从现有 PRD 生成提示词套件'],
      ['我有 PRD + 接口文档', '从现有 PRD 与接口文档生成提示词套件'],
    ]

    for (const [label, marker] of expected) {
      const wrapper = await mountEntry()
      await clickButton(wrapper, label)
      expect(wrapper.text(), label).toContain(marker)
    }
  })

  it('入口页四个叶子的 DOM 顺序固定：导入组里「我有 PRD」先于「我有 PRD + 接口文档」', async () => {
    const wrapper = await mountEntry()

    // 既有用例用「文本包含」找按钮，而「我有 PRD」正是「我有 PRD + 接口文档」的前缀：
    // 导入组里两条的顺序一旦颠倒，includes 会先命中后者，点错卡片（且文字断言仍可能蒙混过关）。
    // 这里直接按叶子卡片的渲染顺序钉死，顺序问题不再靠间接推断。
    const leaves = wrapper.findComponent(EntryStage).findAll('button')
    // 一屏四叶：两组各两个，全部可见——分组是语义层级，不是「先点组再进第二屏」的关卡
    expect(leaves).toHaveLength(4)
    expect(leaves[0]?.text()).toMatch(/^完整流程/)
    expect(leaves[1]?.text()).toMatch(/^快捷流程/)
    // 短名在前：其标题后紧跟的不是「+」（负向先行断言把它们分开）
    expect(leaves[2]?.text()).toMatch(/^我有 PRD(?!\s*\+)/)
    expect(leaves[3]?.text()).toMatch(/^我有 PRD \+ 接口文档/)
  })

  it('选择「完整流程」后显示进度条与结构化表单，不显示对话区', async () => {
    const wrapper = await mountApp()

    expect(wrapper.text()).toContain('描述产品')
    expect(wrapper.text()).toContain('AI 对话')
    expect(wrapper.text()).toContain('告诉我们你的产品想法')
    expect(wrapper.text()).not.toContain('需求澄清对话')
    expect(wrapper.find('pre').exists()).toBe(false)
    expect(api.startConversationStream).not.toHaveBeenCalled()
  })

  it('提交需求后进入对话阶段：对话走任务通道，回复来自服务端轮次', async () => {
    const wrapper = await mountApp()
    await submitSampleForm(wrapper)

    // 进得了对话页
    expect(wrapper.text()).toContain('需求澄清对话')
    // 关键：AI 的第一句话能看到。它来自 `fetchTurns`（服务端轮次），
    // 前端不再自己把增量拼成消息——这一条同时守住「消息不再有第二个写者」
    expect(wrapper.text()).toContain('我先复述一下理解')

    // 对话是一个任务，而不是一条裸的流式请求
    expect(jobCount('chat')).toBe(1)
    const payload = jobPayload('chat')
    expect(payload?.chatStage).toBe('requirement')
    expect(payload?.chatMode).toBe('start')
    expect(solutionApi.fetchTurns).toHaveBeenCalled()

    // 提示词归后端独占：前端只提交问卷答案，不带任何人设文案
    expect(JSON.stringify(payload?.formData ?? {})).not.toContain('资深产品经理')
  })

  it('澄清提问带结构化选项时渲染成选项卡，提交后作为一条用户消息发出、卡片随之消失', async () => {
    // 选项卡来自**服务端轮次上的 `asks`**（`ChatMessage.asks`）：任务产出它、`fetchTurns`
    // 把它读回来。只有开场那一轮带题目，作答之后助手那条不带，卡片因此整块消失。
    chatHarness.opening = (payload) =>
      payload.chatMode === 'start'
        ? {
            reply: '你更倾向哪种做法？',
            asks: [
              {
                id: 'ask-1',
                question: '第一版要不要做离线同步？',
                background: '你提到用户常在弱网环境使用。',
                multiple: false,
                options: [
                  { label: 'A', text: '第一版就做离线同步' },
                  { label: 'B', text: '第一版只做在线' },
                ],
              },
            ],
          }
        : { reply: '好，那就先做离线草稿。' }

    const wrapper = await mountApp()
    await submitSampleForm(wrapper)

    const optionA = wrapper.findAll('button').find((item) => item.text().startsWith('A'))
    expect(optionA, '后端下发了选项就应当渲染出可点的卡片').toBeTruthy()
    expect(optionA?.attributes('disabled')).toBeUndefined()

    await optionA?.trigger('click')
    await clickButton(wrapper, '提交回答')
    await flushPromises()

    // 提交走的是**普通发送链路**：载荷里就是一条用户消息（带题干，模型才知道答的是哪题）
    const continueCalls = vi
      .mocked(api.createJob)
      .mock.calls.filter((call) => call[0]?.chatMode === 'continue')
    expect(continueCalls).toHaveLength(1)
    expect(continueCalls[0]?.[0]?.content).toBe(
      '1. 第一版要不要做离线同步？\n   答：A. 第一版就做离线同步',
    )

    // 作答之后它不再是最后一条消息：卡片整块消失，而不是留下点不动的只读卡片
    const after = wrapper.findAll('button').find((item) => item.text().startsWith('A'))
    expect(after).toBeUndefined()
  })

  it('选项卡的作答沿 emit 链原样透传到接口的结构化 tool_result', async () => {
    chatHarness.opening = (payload) =>
      payload.chatMode === 'start'
        ? {
            reply: '你更倾向哪种做法？',
            asks: [
              {
                id: 'ask-1',
                question: '第一版要不要做离线同步？',
                background: '',
                multiple: false,
                options: [
                  { label: 'A', text: '第一版就做离线同步' },
                  { label: 'B', text: '第一版只做在线' },
                ],
                // 后端随 done 下发的来源工具调用 id
                tool_call_id: 'call-1',
              },
            ],
          }
        : { reply: '好，那就先做离线草稿。' }

    const wrapper = await mountApp()
    await submitSampleForm(wrapper)

    await wrapper.findAll('button').find((item) => item.text().startsWith('A'))?.trigger('click')
    await clickButton(wrapper, '提交回答')
    await flushPromises()

    const payload = vi
      .mocked(api.createJob)
      .mock.calls.filter((call) => call[0]?.chatMode === 'continue')[0]?.[0]
    // 结构化作答：把答案对回当初提问的那次工具调用
    expect(payload?.toolResult).toEqual({
      tool_call_id: 'call-1',
      answers: [
        {
          ask_id: 'ask-1',
          labels: ['A'],
          question: '第一版要不要做离线同步？',
          texts: ['第一版就做离线同步'],
      },
      ],
    })
    // 展示文本照旧发给后端：它是降级时模型唯一能读到的东西
    expect(payload?.content).toBe('1. 第一版要不要做离线同步？\n   答：A. 第一版就做离线同步')

    // 但界面上不再念那段拼装文本：「1. 题干 / 答：A. xxx」换成轻回执卡——
    // 题干摘要 + 选中项的标号与标题（回看时要能认得出选了什么）
    const receipt = wrapper.find('[aria-label*="你选了 A 第一版就做离线同步"]')
    expect(receipt.exists()).toBe(true)
    expect(receipt.text()).toContain('第一版要不要做离线同步？')
    expect(receipt.text()).toContain('第一版就做离线同步')
    // 拼装格式（给后端看的）不该出现在界面上
    expect(receipt.text()).not.toContain('答：')
  })

  it('对话完成后可生成 PRD，并在生成完成时进入待确认阶段', async () => {
    // 开场白由**任务 + 服务端轮次**两步表达（对话已经是一个任务）
    chatHarness.opening = () => ({ reply: '已理解需求。' })

    const wrapper = await mountApp()
    await reachPrdReview(wrapper)

    expect(wrapper.text()).toContain('PRD 文档')
    expect(wrapper.text()).toContain('项目背景')

    // PRD 任务只带「要不要复核」：摘要与对话历史由后端按方案取（票 #54），
    // 前端不再把自己手里那份副本递过去（上游改过时它可能已经落后一版）
    const payload = jobPayload('prd')
    expect(payload?.kind).toBe('prd')
    expect(payload).not.toHaveProperty('summary')
    expect(payload).not.toHaveProperty('history')
    // 与 field-schema 对齐的结构化 JSON 随方案状态落库，后端按方案取
    expect(savedStateContains('summary', 'PRDwright')).toBe(true)
  })

  it('点「生成 PRD」先弹摘要确认面板，确认后把回填结果写回摘要再生成', async () => {
    vi.mocked(api.startConversationStream).mockImplementation(async () => ({
      conversationId: 'conv-1',
      reply: '已理解需求。',
      asks: [],
    }))
    // 回填接口声称用户在对话里补了目标用户与一个 MVP 功能的价值
    vi.mocked(api.syncSummaryFromConversation).mockImplementation(async ({ summary }) => ({
      summary: {
        ...summary,
        target_users: [...summary.target_users, '小团队产品经理'],
        mvp_features: summary.mvp_features.map((feature) => ({
          ...feature,
          user_value: '省时间',
        })),
      },
      changed: true,
    }))

    const wrapper = await mountApp()
    await submitSampleForm(wrapper)
    await clickButton(wrapper, '生成 PRD', true)
    await flushPromises()

    // 停在第 1 道闸门：展示 Diff，尚未调用生成接口
    expect(wrapper.text()).toContain('需求摘要更新确认')
    expect(wrapper.text()).toContain('目标用户 · 第 2 项')
    expect(wrapper.text()).toContain('MVP 功能 · 第 1 项 · 对用户的价值')
    expect(wrapper.text()).toContain('小团队产品经理')
    expect(wrapper.text()).toContain('省时间')
    expect(wrapper.text()).toContain('共 2 处变更')
    expect(jobCount('prd')).toBe(0)

    // 回填请求带上了当前摘要与完整对话历史
    const syncPayload = vi.mocked(api.syncSummaryFromConversation).mock.calls[0]?.[0]
    expect(syncPayload?.summary.product_name).toBe('PRDwright')
    expect(syncPayload?.history?.[0]?.role).toBe('assistant')

    await clickButton(wrapper, '确认更新')
    await flushPromises()

    // 确认后摘要被写回，PRD 需求文本里带上了新增的目标用户
    expect(wrapper.text()).toContain('PRD 文档')
    expect(jobCount('prd')).toBe(1)
    const prdPayload = jobPayload('prd')
    // PRD 任务只带「要不要复核」；回填后的摘要随方案状态落库，由后端按方案取（票 #54）
    expect(prdPayload).not.toHaveProperty('summary')
    expect(savedStateContains('summary', '小团队产品经理')).toBe(true)
    expect(savedStateContains('summary', '省时间')).toBe(true)
  })

  /**
   * 闸门① 的向下游防漏：PRD 澄清的对话和它共用同一个 `messages` 列表。
   *
   * 回填摘要时若连文档答疑一起交给后端，模型会把「讨论 PRD 里某个待确认项」的片段
   * 当成「用户补充的需求」，Diff 里凭空冒出条目。这里钉住：回填只吃 `requirement` 那一轮。
   */
  it('回填摘要只取需求澄清那一轮：PRD 澄清的对话不会被当成需求输入', async () => {
    vi.mocked(api.syncSummaryFromConversation).mockImplementation(async ({ summary }) => ({
      summary,
      changed: false,
    }))

    // 消息的真源是服务端 `chat_turns`（快照里那份 `messages` 已经不再恢复），
    // 因此这里按新链路造数：两条轮次分别落在需求线程与 PRD 澄清线程上。
    turnHarness.items.push(
      { id: 1, role: 'assistant', content: '我先确认几个关键点', stage: 'requirement' },
      { id: 2, role: 'assistant', content: '准备开始 P0 级问题的确认', stage: 'prd-clarify' },
    )

    const wrapper = await mountExistingSolution(
      {
        viewState: 'chatting',
        entryMode: 'structured',
        summary: SAMPLE_SUMMARY,
        quickInput: { productName: '', requirements: '' },
        debugPrdText: '',
        pastePairInput: { prd: '', apiDocs: '' },
        activeJobId: null,
        activeJobKind: null,
      },
      1,
    )
    await flushPromises()

    await clickButton(wrapper, '生成 PRD')
    await flushPromises()

    const syncPayload = vi.mocked(api.syncSummaryFromConversation).mock.calls[0]?.[0]
    // 需求澄清那条在；PRD 澄清那条不在——两条共用列表，靠 stage 分开
    expect(syncPayload?.history?.map((item) => item.content)).toEqual(['我先确认几个关键点'])
  })

  it('拒绝更改：摘要保持不变并回到对话页，不进入生成流程', async () => {
    vi.mocked(api.startConversationStream).mockImplementation(async () => ({
      conversationId: 'conv-1',
      reply: '已理解需求。',
      asks: [],
    }))
    vi.mocked(api.syncSummaryFromConversation).mockImplementation(async ({ summary }) => ({
      summary: { ...summary, platform: '命令行工具' },
      changed: true,
    }))

    const wrapper = await mountApp()
    await submitSampleForm(wrapper)
    await clickButton(wrapper, '生成 PRD', true)
    await flushPromises()

    expect(wrapper.text()).toContain('运行平台')

    await clickButton(wrapper, '拒绝更改')
    await flushPromises()

    expect(wrapper.text()).toContain('需求澄清对话')
    expect(jobCount('prd')).toBe(0)
    // 摘要未被改动：回到第一步看到的仍是原平台
    expect((byId(wrapper, 'field-platform').element as HTMLInputElement).value).toBe('网页应用')
  })

  it('点击「生成接口文档」直接进入生成态并产出接口文档（无检索确认闸门）', async () => {
    vi.mocked(api.startConversationStream).mockImplementation(async () => ({
      conversationId: 'conv-1',
      reply: '已理解需求。',
      asks: [],
    }))
    mockJobStream({
      'api-docs': async (handlers) => {
        handlers.onChunk?.({ text: '# 接口文档\n' })
        return { conversationId: '', reply: '# 接口文档\n\n## 一、概述\n统一响应结构', asks: [] }
      },
    })

    const wrapper = await mountApp()
    await reachPrdReview(wrapper)
    await clickButton(wrapper, '生成接口文档')
    await flushPromises()

    // 不再经过检索确认闸门：直接进入生成、任务已创建
    expect(jobCount('api-docs')).toBe(1)
    expect(wrapper.text()).toContain('接口文档')
    // 完整流程不关复核：`reviewEnabled` 连同字段一起缺省，由后端按配置决定
    expect(jobPayload('api-docs')?.reviewEnabled).toBeUndefined()
  })

  it('用量回执按文档各自留存：切到哪份文档就显示哪份的回执', async () => {
    // 回执不再由前端在生成收尾时顺手记下，而是随正文一起存在后端的 `documents` 表里
    // （`last_run_summary`）。因此这里让「打开方案」那次读取返回两份各自带着回执的文档，
    // 再验证审阅页取的是**当前这份文档**的回执，而不是「最后一次生成动作」的那份。
    const prdRecord = makeDocumentRecord(
      'prd',
      '# 产品需求文档\n\n## 1. 项目背景\n内容',
      makeRunSummary({
        kind: 'prd',
        requestId: 'prd-req',
        durationMs: 1200,
        injectedSkills: [{ name: 'prd-generator', version: '0.1.0', fingerprint: 'e4f928df7be9' }],
      }),
    )
    const apiRecord = makeDocumentRecord(
      'api-docs',
      '# 接口文档\n\n## 一、概述\n内容',
      makeRunSummary({
        kind: 'api_docs',
        requestId: 'api-req',
        durationMs: 9000,
        injectedSkills: [
          { name: 'api-docs-generator', version: '0.1.0', fingerprint: 'd0582f1cbe0f' },
        ],
      }),
    )

    const wrapper = await mountExistingSolution(
      {
        viewState: 'review-prd',
        entryMode: 'structured',
        summary: SAMPLE_SUMMARY,
        messages: [
          { role: 'user', content: '需求如下' },
          { role: 'assistant', content: '已理解需求。' },
        ],
        conversationId: 'conv-1',
      },
      1,
      [prdRecord, apiRecord],
    )

    // PRD 待确认：显示 PRD 自己的回执
    expect(wrapper.text()).toContain('prd-req')
    expect(wrapper.text()).toContain('prd-generator')
    expect(wrapper.text()).not.toContain('api-req')

    // 切到接口文档：换成接口文档自己的回执
    await clickStep(wrapper, '接口文档')
    await flushPromises()
    expect(wrapper.text()).toContain('api-req')
    expect(wrapper.text()).toContain('api-docs-generator')
    expect(wrapper.text()).not.toContain('prd-req')

    // 回看 PRD 阶段：仍是 PRD 的回执，不再被「最后一次生成动作」顶掉
    await clickStep(wrapper, 'PRD')
    await flushPromises()
    expect(wrapper.text()).toContain('prd-req')
    expect(wrapper.text()).not.toContain('api-req')
  })

  it('「改需求」回到第一步，表单内容与对话记录都不丢', async () => {
    chatHarness.opening = () => ({ reply: '已理解需求。' })

    const wrapper = await mountApp()
    await submitSampleForm(wrapper)
    await clickButton(wrapper, '改需求')

    expect(wrapper.text()).toContain('告诉我们你的产品想法')
    // 表单是常驻组件，切换阶段不会丢内容
    expect((byId(wrapper, 'field-product_name').element as HTMLInputElement).value).toBe('PRDwright')

    // 从进度条点回「AI 对话」：上一轮对话记录仍在，不需要重新提交需求
    await clickStep(wrapper, 'AI 对话')
    expect(wrapper.text()).toContain('需求澄清对话')
    expect(wrapper.text()).toContain('已理解需求。')
  })

  it('重新打开已有方案：按后端快照恢复，回到原阶段并回填表单', async () => {
    const wrapper = await mountExistingSolution(
      {
        viewState: 'review-prd',
        entryMode: 'structured',
        summary: SAMPLE_SUMMARY,
        messages: [
          { role: 'user', content: '需求如下' },
          { role: 'assistant', content: '已理解需求。' },
        ],
        conversationId: 'conv-1',
      },
      1,
      [makeDocumentRecord('prd', '# 产品需求文档\n历史内容')],
    )

    // 地址带了 id，AppV2 会去拉详情；快照把阶段定在 review-prd，直接落在那一步
    expect(solutionApi.fetchSolution).toHaveBeenCalledWith(1)
    // 正文不在快照里，是「打开方案」时另外拉回来的
    expect(documentApi.fetchDocuments).toHaveBeenCalledWith(1)
    expect(wrapper.text()).toContain('PRD 文档')
    expect(wrapper.text()).toContain('历史内容')
    expect((byId(wrapper, 'field-product_name').element as HTMLInputElement).value).toBe(
      'PRDwright',
    )
    // 恢复的是快照，不该顺手重跑一遍对话
    expect(api.startConversationStream).not.toHaveBeenCalled()
  })

  it('恢复时若快照正处于生成中，降级为待确认并给出说明', async () => {
    const wrapper = await mountExistingSolution({
      viewState: 'generating-prd',
      entryMode: 'structured',
      summary: SAMPLE_SUMMARY,
      messages: [],
      conversationId: '',
    })

    expect(wrapper.text()).toContain('上次的生成未完成')
    expect(wrapper.text()).toContain('PRD 文档')
  })

  it('降级说明是可关闭的浮层：点「×」即消失，且不影响阶段本身', async () => {
    const wrapper = await mountExistingSolution({
      viewState: 'generating-prd',
      entryMode: 'structured',
      summary: SAMPLE_SUMMARY,
      messages: [],
      conversationId: '',
    })
    expect(wrapper.text()).toContain('上次的生成未完成')

    // 它是浮层、且带关闭按钮（不是会自动消失的 toast：这条说明要留到用户看完）
    const close = wrapper.find('button[aria-label="关闭降级说明"]')
    expect(close.exists()).toBe(true)
    await close.trigger('click')
    await flushPromises()

    expect(wrapper.text()).not.toContain('上次的生成未完成')
    // 关掉的是「说明」，不是「降级」这件事本身：阶段仍是降级后的可操作状态
    expect(storeOf(wrapper).downgrade).toBeNull()
    expect(storeOf(wrapper).viewState).toBe('review-prd')
  })

  it('方案保存失败弹红色提示（不再只在「方案打不开」时才说）', async () => {
    const wrapper = await mountEntry()

    // 自动保存成功 / 保存中都静默，失败是唯一该让用户看见的信号
    solutionsOf(wrapper).saveError = '后端不可用'
    await nextTick()
    await flushPromises()

    const tip = wrapper
      .findAllComponents(AppBadge)
      .find((item) => item.props('variant') === 'danger')
    expect(tip?.text()).toContain('后端不可用')
  })

  it('恢复时若快照停在确认闸门，回到发起该步骤的阶段并给出说明', async () => {
    const wrapper = await mountExistingSolution({
      viewState: 'summary-diff',
      entryMode: 'structured',
      summary: SAMPLE_SUMMARY,
      messages: [{ role: 'assistant', content: '已理解需求。' }],
      conversationId: 'conv-1',
    })

    expect(wrapper.text()).toContain('上次的确认步骤未完成')
    expect(wrapper.text()).toContain('需求澄清对话')
    // 闸门依赖内存里的一次性 Diff，刷新后无从恢复，因此不该再把面板弹出来
    expect(wrapper.text()).not.toContain('需求摘要更新确认')
  })

  it('恢复路径 1：快照带 activeJobId 时静默重连该任务，先收快照补全正文再接 live', async () => {
    vi.mocked(api.readJobStream).mockImplementation(async (jobId, handlers) => {
      expect(jobId).toBe(42)
      // 后端订阅时首发 snapshot：用它把断线前已产出的正文补齐
      handlers?.onSnapshot?.(
        makeJobSnapshot({
          jobId: 42,
          kind: 'prd',
          status: 'running',
          phase: 'writing',
          finalizedText: '# 产品需求文档\n\n## 1. 项目背景\n',
          liveBuffer: '断线前的半句',
        }),
      )
      handlers?.onChunk?.({ text: '续写' })
      return { conversationId: '', reply: '# 产品需求文档\n\n## 1. 项目背景\n断线前的半句续写', asks: [] }
    })

    const wrapper = await mountExistingSolution({
      viewState: 'generating-prd',
      entryMode: 'structured',
      summary: SAMPLE_SUMMARY,
      messages: [],
      conversationId: '',
      activeJobId: 42,
      activeJobKind: 'prd',
    })

    // 有任务指针时 generating-* 不降级：任务在后端继续跑，重连即可
    expect(wrapper.text()).not.toContain('上次的生成未完成')
    expect(api.readJobStream).toHaveBeenCalledWith(42, expect.anything(), expect.anything())
    // 不新建任务，只是订阅既有任务
    expect(jobCount('prd')).toBe(0)
    // 快照补全的正文 + live 续写一起落稿
    expect(wrapper.text()).toContain('PRD 文档')
    expect(wrapper.text()).toContain('断线前的半句续写')
  })

  it('恢复路径：对话任务走对话自己的重连，刷新后流式内容仍然看得见', async () => {
    let release: () => void = () => {}
    vi.mocked(api.readJobStream).mockImplementation(async (jobId, handlers) => {
      expect(jobId).toBe(77)
      handlers?.onSnapshot?.(makeJobSnapshot({ jobId: 77, kind: 'chat', status: 'running' }))
      handlers?.onChunk?.({ text: '断线前已经说出的一半' })
      await new Promise<void>((resolve) => {
        release = resolve
      })
      return { conversationId: '', reply: '断线前已经说出的一半', asks: [] }
    })
    // 刷新后服务端轮次里已有这条回复（对话任务收尾时会重拉一次）
    turnHarness.items.push({
      id: 9,
      role: 'assistant',
      content: '断线前已经说出的一半',
      stage: 'requirement',
    })

    const wrapper = await mountExistingSolution({
      viewState: 'chatting',
      entryMode: 'structured',
      summary: SAMPLE_SUMMARY,
      chatTab: 'requirement',
      activeJobId: 77,
      activeJobKind: 'chat',
    })

    // 重连既有任务，而不是新建一个
    expect(jobCount('chat')).toBe(0)
    expect(api.readJobStream).toHaveBeenCalledWith(77, expect.anything(), expect.anything())
    await flushPromises()
    const shown = wrapper.findComponent(ChatStage).props('messages') as ChatMessage[]
    // 关键：`streaming: true` 的那一条来自**预览气泡**，而服务端拉回来的轮次没有这个标志。
    // 所以这条断言不会被「收尾后 fetchTurns 拿到的权威轮次」蒙混过去——若对话被塞进
    // 文档流水线，预览写进了看不见的地方，这里就只剩那条没有 streaming 的轮次。
    expect(shown.map((m) => m.content).join('')).toContain('断线前已经说出的一半')
    expect(shown[shown.length - 1]?.streaming).toBe(true)

    release()
    await flushPromises()
  })

  it('恢复路径 1b：刷新时正赶上审核，阶段输出随快照接回来', async () => {
    vi.mocked(api.readJobStream).mockImplementation(async (jobId, handlers) => {
      expect(jobId).toBe(43)
      // 重连**不会补发**历史 `stage_chunk`：审核意见只能靠快照的 `stageText` 带回来
      handlers?.onSnapshot?.(
        makeJobSnapshot({
          jobId: 43,
          kind: 'api_docs',
          // 后端 `doc_kind` 与 kind 必须自洽：前端以它为准决定「正在写哪一份」
          docKind: 'api-docs',
          status: 'running',
          phase: 'reviewing',
          finalizedText: '',
          liveBuffer: '# 用户接口\n\n## 1. 登录',
          stageText: '{"verdict":"revise","issues":[]}',
        }),
      )
      // 不结束：真实生成会跑很久，断言就在「还在审核」这个窗口里做
      return new Promise<api.StreamResult>(() => {})
    })

    const wrapper = await mountExistingSolution({
      viewState: 'generating-api-docs',
      entryMode: 'structured',
      summary: SAMPLE_SUMMARY,
      messages: [],
      conversationId: '',
      activeJobId: 43,
      activeJobKind: 'api-docs',
    })

    // 正文：`finalizedText + liveBuffer` 一起补回来，不是只看实时缓冲
    // （预览区按 Markdown 渲染，标题标记被剥掉，断言正文本身）
    expect(wrapper.text()).toContain('用户接口')
    expect(wrapper.text()).toContain('1. 登录')
    // 阶段输出：审核面板接着展示，而不是刷新后空着
    expect(wrapper.text()).toContain('审核意见')
    expect(wrapper.text()).toContain('{"verdict":"revise","issues":[]}')
  })

  /**
   * D1：生成途中用户可以自由离开那一页，收尾时**不抢方向盘**。
   *
   * 从前的第一步就跳不动——`goTo` 首行是 `if (busy) return false`，于是生成全程里
   * 进度条与底栏「返回」都是死按钮（点了没反应，也不报错，看起来像界面坏了）。
   * 拆掉闸门之后要同时成立两件事：**跳得动**，且收尾**不把人拽回**审阅页。
   */
  it('生成中可以点进度条离开，收尾不把用户拽回审阅页（只给浮层提示）', async () => {
    let release!: () => void
    mockJobStream({
      prd: async (handlers) => {
        handlers.onChunk?.({ text: '# 产品需求文档\n' })
        // 卡住：断言就在「还在生成」这个窗口里做
        await new Promise<void>((resolve) => {
          release = resolve
        })
        return { conversationId: '', reply: '# 产品需求文档\n\n## 1. 项目背景\n内容', asks: [] }
      },
    })

    const wrapper = await mountApp()
    await submitSampleForm(wrapper)
    await clickButton(wrapper, '生成 PRD', true)
    await flushPromises()
    await confirmSummaryPanel(wrapper)

    const store = storeOf(wrapper)
    expect(store.viewState).toBe('generating-prd')

    // 生成途中点进度条上的「AI 对话」——从前这一步会被静默吞掉
    await clickStep(wrapper, 'AI 对话')
    await flushPromises()
    expect(store.viewState).toBe('chatting')

    // 收尾：用户仍在对话页，界面不被拽走，只多一条浮层提示
    release()
    await flushPromises()
    expect(store.viewState).toBe('chatting')
    expect(wrapper.text()).toContain('PRD 文档已生成完毕')
    // 正文照写（写库与界面停在哪一页无关）
    expect(documentsOf(wrapper).contents.prd).toContain('项目背景')
  })

  it('恢复路径 2：无指针但快照落在生成中时兜底查到活跃任务，撤销降级并重连', async () => {
    vi.mocked(api.fetchActiveJobs).mockResolvedValue([
      {
        id: 7,
        kind: 'prd',
        // 后端在新版本里会给出目标产物；前端据此重建运行态，不再按阶段反推
        docKind: 'prd',
      chatStage: null,
        status: 'running',
        phase: 'writing',
        solutionId: 1,
        errorCode: '',
        errorMessage: '',
        createdAt: '2026-10-13T00:00:00',
        updatedAt: '2026-10-13T00:00:00',
        usage: null,
      },
    ])

    const wrapper = await mountExistingSolution({
      viewState: 'generating-prd',
      entryMode: 'structured',
      summary: SAMPLE_SUMMARY,
      messages: [],
      conversationId: '',
    })

    // 兜底查询带上了当前方案与「在跑」状态；**不再传 kind**（票 #70）：
    // 后端 `kind` 是任务种类（`review_fix` / `review` …），这里手上只有产物种类，
    // 旧写法把产物当任务传，对动作类任务永远不命中
    expect(api.fetchActiveJobs).toHaveBeenCalledWith({
      solutionId: 1,
      status: 'pending,running,cancelling',
    })
    // 命中活跃任务：撤销降级提示，切回生成态并重连
    expect(wrapper.text()).not.toContain('上次的生成未完成')
    expect(api.readJobStream).toHaveBeenCalledWith(7, expect.anything(), expect.anything())
    expect(wrapper.text()).toContain('PRD 文档')
  })

  it('恢复路径 3：兜底查询落空时保持降级提示，不发起任何订阅', async () => {
    vi.mocked(api.fetchActiveJobs).mockResolvedValue([])

    const wrapper = await mountExistingSolution({
      viewState: 'generating-prd',
      entryMode: 'structured',
      summary: SAMPLE_SUMMARY,
      messages: [],
      conversationId: '',
    })

    expect(api.fetchActiveJobs).toHaveBeenCalledTimes(1)
    expect(api.readJobStream).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('上次的生成未完成')
  })

  it('恢复路径 4：刷新时指针还在 → 直接回到运行视图并重连那支修复任务（#70）', async () => {
    // 快照停在审阅页，但指针还在：这正是「修复中切走、然后刷新」的形状
    vi.mocked(api.fetchActiveJobs).mockResolvedValue([])
    jobHarness.kinds.set(7, 'review-fix')
    mockJobStream({
      'review-fix': () => new Promise<api.StreamResult>(() => {}),
    })

    const wrapper = await mountExistingSolution({
      viewState: 'review-api-docs',
      entryMode: 'structured',
      summary: SAMPLE_SUMMARY,
      messages: [],
      conversationId: '',
      activeJobId: 7,
      activeJobKind: 'review-fix',
      activeJobDocKind: 'api-docs',
    })

    // 视图跟随在途任务（#68 的判据）：没走到审阅页，直接是运行视图
    expect(wrapper.text()).not.toContain('文档缺陷')
    expect(api.readJobStream).toHaveBeenCalledWith(7, expect.anything(), expect.anything())
  })

  it('恢复路径 5：指针丢了但本方案有在跑的修复任务 → 照样归位到运行视图（#70）', async () => {
    jobHarness.kinds.set(9, 'review-fix')
    vi.mocked(api.fetchActiveJobs).mockResolvedValue([
      makeJobStatus({ id: 9, kind: 'review_fix', docKind: 'api-docs' }),
    ])
    mockJobStream({
      'review-fix': () => new Promise<api.StreamResult>(() => {}),
    })

    const wrapper = await mountExistingSolution({
      viewState: 'review-api-docs',
      entryMode: 'structured',
      summary: SAMPLE_SUMMARY,
      messages: [],
      conversationId: '',
      // 刻意**不给** activeJobId：这就是「指针还没落库的那个窗口」
    })

    // 兜底按方案 + 在跑状态查，不再把产物种类当任务种类传（旧写法对动作类任务永远不命中）
    expect(api.fetchActiveJobs).toHaveBeenCalledWith({
      solutionId: 1,
      status: 'pending,running,cancelling',
    })
    // 命中后按后端给的 docKind 归位，屏幕上是运行视图而不是审阅页
    expect(wrapper.text()).not.toContain('文档缺陷')
    expect(api.readJobStream).toHaveBeenCalledWith(9, expect.anything(), expect.anything())
  })

  it('恢复路径 6：这次兜底查询只发生一次，不轮询（#70）', async () => {
    vi.mocked(api.fetchActiveJobs).mockResolvedValue([])

    const wrapper = await mountExistingSolution({
      viewState: 'review-api-docs',
      entryMode: 'structured',
      summary: SAMPLE_SUMMARY,
      messages: [],
      conversationId: '',
    })
    await flushPromises()

    expect(api.fetchActiveJobs).toHaveBeenCalledTimes(1)
    // 查不到就老实停在审阅页，不发起任何订阅
    expect(api.readJobStream).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('文档缺陷')
  })

  it('恢复路径 7：刷新时停在别的产物上，在途的修复仍然登记并解开入口禁用（#70）', async () => {
    jobHarness.kinds.set(11, 'review-fix')
    // 在途的是**接口文档**的修复，而刷新后人停在 PRD 的审阅页
    vi.mocked(api.fetchActiveJobs).mockResolvedValue([
      makeJobStatus({ id: 11, kind: 'review_fix', docKind: 'api-docs' }),
    ])
    mockJobStream({
      'review-fix': () => new Promise<api.StreamResult>(() => {}),
    })

    const wrapper = await mountExistingSolution(reviewSnapshot('review-prd'), 1, [
      makeDocumentRecord('prd', '# PRD\n\n## 一、背景\n内容', null, [], {
        kind: 'prd',
        verdict: 'revise',
        summary: '2 个待处理问题',
        issues: [],
        rewritten: false,
        failedSections: [],
        reviewedAt: '2026-11-08T00:00:00',
      }),
      makeDocumentRecord('api-docs', '# 接口文档\n\n## 一、概述\n统一响应结构', null, [], {
        kind: 'api_docs',
        verdict: 'revise',
        summary: '2 个待处理问题',
        issues: [],
        rewritten: false,
        failedSections: [],
        reviewedAt: '2026-11-08T00:00:00',
      }),
    ])
    await flushPromises()
    // 说明「人不在这份产物上」：屏幕停在 PRD 的审阅页
    await clickStep(wrapper, 'PRD')
    expect(storeOf(wrapper).viewState).toBe('review-prd')

    // 人在 PRD 页：不被打断，但这支修复**已被登记**——PRD 的入口同样禁用（#69 的粒度是任务种类）
    expect(wrapper.text()).toContain('文档缺陷')
    const fix = wrapper.findAll('button').find((item) => item.text().trim() === '修复')
    expect(fix?.attributes('disabled')).toBeDefined()

    // 切到被修的那一份：运行视图照常出现（兜底渲染靠的就是这份运行态）
    await clickStep(wrapper, '接口文档')
    expect(wrapper.text()).not.toContain('文档缺陷')
  })

  it('恢复路径 8：同时有几支在跑时，优先接修复那一支（kind 是下划线口径，须经转换）（#70）', async () => {
    jobHarness.kinds.set(20, 'prd')
    jobHarness.kinds.set(21, 'review-fix')
    // 两条并行（对话之外的场景：本票之后同一方案可能同时有一支生成与一支修复）
    vi.mocked(api.fetchActiveJobs).mockResolvedValue([
      makeJobStatus({ id: 20, kind: 'prd', docKind: 'prd' }),
      makeJobStatus({ id: 21, kind: 'review_fix', docKind: 'api-docs' }),
    ])
    mockJobStream({
      prd: () => new Promise<api.StreamResult>(() => {}),
      'review-fix': () => new Promise<api.StreamResult>(() => {}),
    })

    const wrapper = await mountExistingSolution(reviewSnapshot('review-api-docs'), 1, [
      makeDocumentRecord('api-docs', '# 接口文档\n\n## 一、概述\n统一响应结构', null, [], {
        kind: 'api_docs',
        verdict: 'revise',
        summary: '2 个待处理问题',
        issues: [],
        rewritten: false,
        failedSections: [],
        reviewedAt: '2026-11-08T00:00:00',
      }),
    ])

    // 接的是修复那一支，不是列表里的第一条
    expect(api.readJobStream).toHaveBeenCalledWith(21, expect.anything(), expect.anything())
    expect(wrapper.text()).not.toContain('文档缺陷')
    expect(wrapper.text()).toContain('修复中')
  })

  it('首屏「路由落位」与「登录态探测」同时触发恢复时只订阅一次', async () => {
    // 快照里带着任务指针：两条触发路径都会走到「静默重连该任务」
    vi.mocked(solutionApi.fetchSolution).mockResolvedValue(
      makeSolutionDetail(1, {
        viewState: 'generating-prd',
        entryMode: 'structured',
        summary: SAMPLE_SUMMARY,
        messages: [],
        conversationId: '',
        quickInput: { productName: '', requirements: '' },
        debugPrdText: '',
        pastePairInput: { prd: '', apiDocs: '' },
        activeJobId: 42,
        activeJobKind: 'prd',
      }),
    )
    // 任务流**不结束**：真实生成会跑几分钟，守卫必须覆盖整个生成窗口
    mockJobStream({
      prd: (handlers) => {
        handlers.onSnapshot?.(
          makeJobSnapshot({
            jobId: 42,
            kind: 'prd',
            status: 'running',
            phase: 'writing',
            finalizedText: '# 产品需求文档\n',
          }),
        )
        return new Promise<api.StreamResult>(() => {})
      },
    })

    // 登录态探测「卡住」：等路由先落位，再放行，制造两条路径先后触发
    let releaseAuth: () => void = () => {}
    vi.mocked(authService.fetchCurrentUser).mockImplementation(
      () =>
        new Promise((resolve) => {
          releaseAuth = () => resolve({ id: 1, username: 'tester' })
        }),
    )

    const router = makeRouter()
    // 刻意「先挂载、后落位路由」：生产环境 `main.ts` 未等待 `router.isReady()` 就挂载，
    // 于是 `routeSolutionId` 会由 null 变为 id，与登录态探测一起触发两次加载
    mount(AppV2, { global: { plugins: [pendingLoginPinia(), router] } })
    await router.push('/v2/1')
    await flushPromises()

    releaseAuth()
    await flushPromises()

    // 同一份方案只加载一次、同一个任务只订阅一次——两路 SSE 事件不会交错写进同一份预览。
    // 产物正文那次拉取同理只能有一次：重复装载会把用户尚未提交的手改盖掉。
    expect(solutionApi.fetchSolution).toHaveBeenCalledTimes(1)
    expect(documentApi.fetchDocuments).toHaveBeenCalledTimes(1)
    expect(api.readJobStream).toHaveBeenCalledTimes(1)
    expect(api.readJobStream).toHaveBeenCalledWith(42, expect.anything(), expect.anything())
  })

  it('重新开始：回到入口页并清空表单、入口模式与会话内容', async () => {
    vi.mocked(api.startConversationStream).mockImplementation(async () => ({
      conversationId: 'conv-1',
      reply: '已理解需求。',
      asks: [],
    }))

    const wrapper = await mountApp()
    // 断言必须落在**这个应用自己的 pinia** 上。`useWorkflowStore()` / `useDocumentsStore()`
    // 取的是模块级的「当前活跃 pinia」，它未必是刚挂载那个应用的——一旦别的用例有异步
    // 副作用跨过用例边界，这里就会读到别的用例遗留的状态（表现为「单独跑通、整文件跑挂」）。
    // 因此两个 store 都从组件的应用上下文里取。
    const workflow = storeOf(wrapper)
    const documents = documentsOf(wrapper)
    await submitSampleForm(wrapper)
    await clickButton(wrapper, '重新开始')
    // 「重新开始」会先 flush 一次待保存的改动，再清空——因此要多等几拍
    await flushPromises()
    await nextTick()

    // 重新开始回到入口页，而不是直接落回表单
    expect(wrapper.text()).toContain('新建 PRD 方案')

    // 会话内容确实被清空了——断言 store 里的空态，而不是磁盘上某个键的内容：
    // 持久化的权威来源已从 localStorage 换成后端，重开一次页面必须回到入口页，
    // 而不是把上一轮的产物又拉回来。
    expect(workflow.viewState).toBe('entry')
    expect(workflow.entryMode).toBeNull()
    expect(workflow.quickInput).toEqual({ productName: '', requirements: '' })
    expect(workflow.debugPrdText).toBe('')
    // 产物正文归文档 store（后端 documents 表的工作副本），同样要清干净
    expect(documents.hasAny).toBe(false)
    expect(workflow.hasContent).toBe(false)

    // 再进完整流程时，表单是重建后的空表单（依赖 formKey 自增强制重建）
    await clickButton(wrapper, '完整流程')
    expect(wrapper.text()).toContain('告诉我们你的产品想法')
    expect((byId(wrapper, 'field-product_name').element as HTMLInputElement).value).toBe('')
    expect(wrapper.text()).toContain('还没有功能项')
  })

  it('提交失败时保留错误提示，不进入空白对话页', async () => {
    // 失败来自**任务**：对话已经是一个任务，上游报错落在 `runs.preview.error` 上，
    // 由对话页顶部的红条呈现（不是「一片空白，没有任何提示」）
    mockJobStream({
      chat: async () => {
        throw new Error('后端不可用')
      },
    })

    const wrapper = await mountApp()
    await submitSampleForm(wrapper)

    expect(wrapper.text()).toContain('需求澄清对话')
    expect(wrapper.text()).toContain('后端不可用')
  })
})

/**
 * 审阅页的「手改自动落库」与「下载」。
 *
 * 这两件事是一次改动里互为因果的两面：底栏原来那个「保存」按钮被撤掉，落库改成
 * 「编辑即防抖上报」，空出来的位置换成「下载」。用例要同时钉住——
 *
 * 1. 不点任何按钮，编辑也会落库，而且**真的等满防抖窗口**（不是每敲一个字发一次）；
 * 2. 下载拿到的是**组件里的草稿**，而不是 store 里可能还落后半拍的工作副本；
 * 3. 落库失败**闪一次红色提示**，同一原因反复失败不再刷屏——否则一次网络抖动会把界面刷满。
 *
 * 断言必须落在这个应用自己的 pinia 上（`documentsOf`），不能用裸的 `useDocumentsStore()`。
 */
describe('AppV2 的文档手改自动落库与下载', () => {
  /** 「停在 PRD 待确认」且带结构化摘要的快照——摘要里要有产品名，下载文件名才可预期 */
  function prdReviewSnapshot(): Record<string, unknown> {
    return { ...reviewSnapshot('review-prd'), summary: SAMPLE_SUMMARY }
  }

  /** 在「PRD 待确认」阶段挂载一份已存在的方案 */
  async function mountPrdReview(): Promise<VueWrapper> {
    return mountExistingSolution(prdReviewSnapshot(), 1, [
      makeDocumentRecord('prd', '# 产品需求文档\n\n## 1. 项目背景\n历史内容'),
    ])
  }

  /** 切到「编辑」视图并写入一段新正文 */
  async function editContent(wrapper: VueWrapper, content: string): Promise<void> {
    await clickButton(wrapper, '编辑', true)
    await wrapper.findComponent(ReviewStage).find('textarea').setValue(content)
  }

  /** 推进一个完整的防抖窗口，并等保存请求落地 */
  async function advanceDebounce(): Promise<void> {
    await vi.advanceTimersByTimeAsync(DOCUMENT_SAVE_DEBOUNCE_MS)
    await flushPromises()
  }

  /**
   * 当前状态条上的失败提示（红色药丸）。
   *
   * `AppBadge` 全仓只在 AppV2 这一处提示位上用，所以「找 variant=danger 的那个」不会误伤。
   * 返回 null 表示此刻没有失败提示。
   */
  function failureTip(wrapper: VueWrapper) {
    return (
      wrapper.findAllComponents(AppBadge).find((item) => item.props('variant') === 'danger') ?? null
    )
  }

  it('编辑正文后不点任何按钮，防抖到时自动落库', async () => {
    const wrapper = await mountPrdReview()

    // 挂载本身要等一串异步（拉方案详情 → 拉文档 → 应用快照），必须在这些落定**之后**
    // 才接管定时器；反过来说，一开始就 fake，挂载期的那些 await 会等不到定时器。
    vi.useFakeTimers()
    try {
      await editContent(wrapper, '# 改过的正文')

      // 防抖窗口内一次请求都不该发：否则等于回到「每敲一个字发一次」的老路
      await vi.advanceTimersByTimeAsync(DOCUMENT_SAVE_DEBOUNCE_MS / 2)
      expect(documentApi.saveDocument).not.toHaveBeenCalled()

      // 窗口走满 → 恰好一次 PUT，载荷是编辑后的**全文**
      await advanceDebounce()
      expect(documentApi.saveDocument).toHaveBeenCalledTimes(1)
      // 版本锁（#81）：第三参是正文，第四参是本 store 以为的当前版本号（文档未带版本时为 0）
      expect(documentApi.saveDocument).toHaveBeenCalledWith(1, 'prd', '# 改过的正文', 0)
    } finally {
      vi.useRealTimers()
    }
  })

  it('点「下载」导出的是编辑后的草稿，文件名带产品名与文档类型', async () => {
    const wrapper = await mountPrdReview()
    await editContent(wrapper, '# 改过的正文')
    await nextTick()

    await clickButton(wrapper, '下载', true)

    // 此刻防抖窗口还没走完，store 的工作副本仍是历史版本——下载必须取组件里的草稿
    expect(vi.mocked(downloadFile)).toHaveBeenCalledTimes(1)
    expect(vi.mocked(downloadFile).mock.calls[0]?.[0]).toBe('# 改过的正文')
    expect(vi.mocked(downloadFile).mock.calls[0]?.[1]).toBe('PRDwright-PRD.md')
  })

  it('自动落库失败闪一条红色提示，同一原因重试不刷屏', async () => {
    vi.mocked(documentApi.saveDocument).mockRejectedValue(new Error('网络断了'))
    const wrapper = await mountPrdReview()

    vi.useFakeTimers()
    try {
      await editContent(wrapper, '# 第一版')
      await advanceDebounce()

      // 落库是静默的，失败是整条链路里唯一该让用户看见的信号
      expect(failureTip(wrapper)?.text()).toContain('网络断了')
      expect(failureTip(wrapper)?.props('variant')).toBe('danger')

      // 2.5s 后自动淡出
      await vi.advanceTimersByTimeAsync(2600)
      await flushPromises()
      await nextTick()
      expect(failureTip(wrapper)).toBeNull()

      // 同一原因再失败一次：`saveError` 没变，不重新闪（否则网络抖动会把界面刷满）
      await editContent(wrapper, '# 第二版')
      await advanceDebounce()
      expect(documentApi.saveDocument).toHaveBeenCalledTimes(2)
      expect(failureTip(wrapper)).toBeNull()

      // 成功一次（`saveError` 被清空）之后再失败，算新问题，重新提示
      vi.mocked(documentApi.saveDocument).mockImplementation(async (_id, kind, content) =>
        makeDocumentRecord(kind, content),
      )
      await editContent(wrapper, '# 第三版')
      await advanceDebounce()
      expect(failureTip(wrapper)).toBeNull()

      vi.mocked(documentApi.saveDocument).mockRejectedValue(new Error('又断了'))
      await editContent(wrapper, '# 第四版')
      await advanceDebounce()
      expect(failureTip(wrapper)?.text()).toContain('又断了')

      // 改动始终留在本地工作副本里：失败不该把用户刚敲的字丢掉
      expect(documentsOf(wrapper).contentOf('prd')).toBe('# 第四版')
    } finally {
      vi.useRealTimers()
    }
  })

  it('手改撞上版本锁（409）：提示「正文已被更新，请刷新后重试」，且不把旧内容重试回去', async () => {
    vi.mocked(documentApi.saveDocument).mockRejectedValue(
      new documentApi.DocumentVersionConflictError('正文已被更新，请刷新后重试'),
    )
    const wrapper = await mountPrdReview()

    vi.useFakeTimers()
    try {
      await editContent(wrapper, '# 基于旧版的手改')
      await advanceDebounce()

      // 明确提示（红色浮层），不是静默丢改动
      expect(failureTip(wrapper)?.text()).toContain('正文已被更新，请刷新后重试')
      expect(failureTip(wrapper)?.props('variant')).toBe('danger')
      // 用户的字还在工作副本里
      expect(documentsOf(wrapper).contentOf('prd')).toBe('# 基于旧版的手改')

      // 冲突未解除期间再输入：不再发一次注定被拒、且可能覆盖新正文的写
      await editContent(wrapper, '# 又改了一笔')
      await advanceDebounce()
      expect(documentApi.saveDocument).toHaveBeenCalledTimes(1)
    } finally {
      vi.useRealTimers()
    }
  })
})

/**
 * 四条入口（第三步）的前端表现。
 *
 * 四条入口共用「生成 → 审阅 → 下载」链路，差异只有三处：需求从哪来、进度条显示哪几步、
 * 完成需要哪几份文档。因此这里重点钉住这三处，外加一条硬约束——
 * **快捷入口与完整流程共用同一条链路**（同一个进度条、同样的三份齐备才算交付），
 * 但它**一路不问问题**：一段话提交后直接进生成，既没有需求澄清对话，也不经过摘要确认闸门
 * （那道闸门要把对话里的补充信息回填进结构化摘要，而快捷入口根本没有摘要可回填）。
 */
describe('AppV2 入口模式', () => {
  /** 进入 快捷流程入口并填好一段话 */
  async function fillQuickPrd(wrapper: VueWrapper): Promise<void> {
    await clickButton(wrapper, '快捷流程')
    await byId(wrapper, 'field-quick-product-name').setValue('小账本')
    await byId(wrapper, 'field-quick-requirements').setValue('做一个记账 App，支持多人共享账本。')
  }

  /** 提交极简输入，等 PRD 生成落地 */
  async function submitQuickPrd(wrapper: VueWrapper): Promise<void> {
    await fillQuickPrd(wrapper)
    await clickButton(wrapper, '生成 PRD', true)
    await flushPromises()
  }

  it('完整流程：落页、六格进度条、模式名与返回文案都由登记表这一行给出', async () => {
    const wrapper = await mountEntry()
    await clickButton(wrapper, '完整流程')

    const spec = ENTRY_REGISTRY.structured
    if (!spec) throw new Error('登记表缺「完整流程」一行')

    // 落页：结构化表单（登记表说落在哪儿，屏幕就该落在哪儿，不读 store 内部字段）
    expect(wrapper.text()).toContain('告诉我们你的产品想法')
    // 进度条：六格，且与这一行登记的步骤数一致
    const bars = wrapper.findAll('nav[aria-label="生成进度"] li')
    expect(bars).toHaveLength(6)
    expect(bars).toHaveLength(spec.steps.length)
    // 模式名（侧栏 / 方案列表里那几个字）不在这里断言：那是 store 层的取值，
    // 屏幕上看不见它，越界断言只会让这条用例在重构时白挂一次。口径由
    // `solutions-store.spec.ts` 覆盖。
    // 表单的返回落点是入口页
    await clickButton(wrapper, '返回入口', true)
    expect(wrapper.text()).toContain('新建 PRD 方案')
  })

  it('快捷流程：一段话直接生成 PRD，不问问题也不经过摘要确认闸门', async () => {
    const wrapper = await mountEntry()
    await fillQuickPrd(wrapper)

    // 录入页是「描述产品」那一步的极简形态；进度条与完整流程同一套，六步齐全——
    // 「AI 对话」那一格留给文档澄清（需求澄清那条线程没有，但对话页本身要用）
    expect(wrapper.text()).toContain('直接生成 PRD')
    expect(wrapper.text()).toContain('描述产品')
    expect(wrapper.findAll('nav[aria-label="生成进度"] li')).toHaveLength(6)

    await clickButton(wrapper, '生成 PRD', true)
    await flushPromises()

    // 关键：一步对话都没发生，一次摘要回填也没发生
    expect(api.startConversationStream).not.toHaveBeenCalled()
    expect(api.syncSummaryFromConversation).not.toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('需求澄清对话')
    expect(wrapper.text()).not.toContain('需求摘要更新确认')
    expect(jobCount('prd')).toBe(1)

    const payload = jobPayload('prd')
    // 需求不随任务下发（票 #54）：它先随方案状态落库，后端按方案取
    expect(payload).not.toHaveProperty('requirements')
    expect(payload).not.toHaveProperty('productName')
    expect(payload).not.toHaveProperty('summary')
    expect(payload).not.toHaveProperty('history')
    expect(savedStateContains('quick_requirements', '做一个记账 App，支持多人共享账本。')).toBe(true)
    expect(savedStateContains('quick_product_name', '小账本')).toBe(true)
    // 输入只有一段话，逐章复核的边际收益低：生成时只撰写、不审不改
    expect(payload?.reviewEnabled).toBe(false)

    expect(wrapper.text()).toContain('PRD 文档')
    expect(wrapper.text()).toContain('项目背景')
  })

  it('快捷流程：接口文档与提示词同样一遍过（不审不改）', async () => {
    const wrapper = await mountEntry()
    await submitQuickPrd(wrapper)

    // 「快捷」不能只在 PRD 那一站生效：后面两份也审一次、改一轮，省下的时间又还回去了
    await clickButton(wrapper, '生成接口文档', true)
    await flushPromises()
    expect(jobPayload('api-docs')?.reviewEnabled).toBe(false)

    await clickButton(wrapper, '生成提示词套件', true)
    await flushPromises()
    expect(jobPayload('prompts')?.reviewEnabled).toBe(false)
  })

  it('快捷流程：未填需求时不能提交', async () => {
    const wrapper = await mountEntry()
    await clickButton(wrapper, '快捷流程')

    const generate = wrapper.findAll('button').find((item) => item.text().trim() === '生成 PRD')
    expect(generate).toBeDefined()
    expect((generate?.element as HTMLButtonElement).disabled).toBe(true)

    // 缺失提示必须提前常驻在字段下方：按钮禁用后点不动，靠点击报错是收不到反馈的
    expect(wrapper.text()).toContain('请至少写一句需求描述')
  })

  it('快捷流程：点进度条第 1 步就回到录入页，底栏不再有重复的「返回描述」', async () => {
    const wrapper = await mountEntry()
    await submitQuickPrd(wrapper)

    // 录入页就是进度条上的「描述产品」，底栏再放一个同名按钮是纯重复
    expect(wrapper.text()).not.toContain('返回描述')
    expect(wrapper.text()).not.toContain('返回对话')

    await clickStep(wrapper, '描述产品')
    await flushPromises()

    expect(storeOf(wrapper).viewState).toBe('prd-quick')
    expect(wrapper.text()).toContain('直接生成 PRD')
    // 录入内容由 store 持有，来回切换不丢
    expect((byId(wrapper, 'field-quick-requirements').element as HTMLTextAreaElement).value).toBe(
      '做一个记账 App，支持多人共享账本。',
    )
  })

  it('快捷流程：交付条件与完整流程一致，只生成 PRD 时「交付」格点不动', async () => {
    const wrapper = await mountEntry()
    await submitQuickPrd(wrapper)

    // 两条流程都要求三份齐备才算可交付，快捷流程不降低门槛。
    // 交付入口是进度条那一格（「完成」按钮已撤），未齐备时点了不该有反应。
    await clickStep(wrapper, '交付')
    await flushPromises()

    expect(storeOf(wrapper).viewState).toBe('review-prd')
    expect(wrapper.text()).not.toContain('文档已生成完毕')
  })

  it('我有 PRD：贴入 PRD 直接生成提示词套件，且不出现「查看 PRD」', async () => {
    const wrapper = await mountEntry()
    await clickButton(wrapper, '我有 PRD')

    expect(wrapper.text()).toContain('从现有 PRD 生成提示词套件')
    // 该模式的产物只有提示词，进度条现在是「提示词 → 交付」两步
    expect(wrapper.find('nav[aria-label="生成进度"]').exists()).toBe(true)
    expect(wrapper.findAll('nav[aria-label="生成进度"] li')).toHaveLength(2)

    await byId(wrapper, 'field-debug-prd').setValue(
      '# 小账本 产品需求文档（PRD）\n\n## 1. 产品概述\n家庭记账工具',
    )
    await clickButton(wrapper, '生成提示词套件', true)
    await flushPromises()

    const payload = jobPayload('prompts')
    // 任务只带「做什么」；贴入的 PRD 先落进方案状态，由后端按方案取
    expect(payload).not.toHaveProperty('prdContent')
    expect(savedStateContains('draft_prd_text', '家庭记账工具')).toBe(true)
    expect(wrapper.text()).toContain('提示词套件')
    expect(wrapper.text()).toContain('角色设定')

    // 没生成过 PRD，就不能出现会点进空文档的入口；返回目标也是录入页
    expect(wrapper.text()).not.toContain('查看 PRD')
    expect(wrapper.text()).toContain('返回 PRD')

    // 只有提示词也算齐备，点进度条「交付」即进交付页；交付页只留这一张卡片
    await clickStep(wrapper, '交付')
    await flushPromises()
    expect(wrapper.text()).toContain('文档已生成完毕')
    const doneCards = wrapper.findAll('article')
    expect(doneCards).toHaveLength(1)
    expect(doneCards[0]?.text()).toContain('提示词套件')
    expect(doneCards[0]?.text()).not.toContain('接口文档')
  })

  it('我有 PRD：未贴入 PRD 时不能提交', async () => {
    const wrapper = await mountEntry()
    await clickButton(wrapper, '我有 PRD')

    const generate = wrapper.findAll('button').find((item) => item.text().trim() === '生成提示词套件')
    expect((generate?.element as HTMLButtonElement).disabled).toBe(true)

    expect(jobCount('prompts')).toBe(0)
    expect(wrapper.text()).toContain('请先贴入 PRD 正文')
  })

  it('我有 PRD + 接口文档：两份材料都随任务送出去，产出仍是提示词套件', async () => {
    const wrapper = await mountEntry()
    await clickButton(wrapper, '我有 PRD + 接口文档')

    expect(wrapper.text()).toContain('从现有 PRD 与接口文档生成提示词套件')
    // 与「我有 PRD」同属导入组：产物只有提示词，进度条是「提示词 → 交付」两步
    expect(wrapper.find('nav[aria-label="生成进度"]').exists()).toBe(true)
    expect(wrapper.findAll('nav[aria-label="生成进度"] li')).toHaveLength(2)

    await byId(wrapper, 'field-pair-prd').setValue(
      '# 小账本 产品需求文档（PRD）\n\n## 1. 产品概述\n家庭记账工具',
    )
    await byId(wrapper, 'field-pair-api-docs').setValue(
      '# 小账本 接口文档\n\n## 记账\nPOST /api/ledgers',
    )
    await clickButton(wrapper, '生成提示词套件', true)
    await flushPromises()

    const payload = jobPayload('prompts')
    // 这一条入口的全部意义：接口文档也要真的到后端——但它走的是方案状态，不是请求体
    expect(payload).not.toHaveProperty('prdContent')
    expect(payload).not.toHaveProperty('apiDocsContent')
    expect(savedStateContains('paste_prd_text', '家庭记账工具')).toBe(true)
    expect(savedStateContains('paste_api_docs_text', 'POST /api/ledgers')).toBe(true)
    expect(wrapper.text()).toContain('提示词套件')

    // 自带的两份材料**不算**本工具的产物：返回目标也是录入页，且不出现「查看 PRD」
    expect(wrapper.text()).not.toContain('查看 PRD')
    expect(wrapper.text()).toContain('返回素材')
  })

  it('我有 PRD + 接口文档：接口文档选填，只贴 PRD 也能提交', async () => {
    const wrapper = await mountEntry()
    await clickButton(wrapper, '我有 PRD + 接口文档')

    // 只有 PRD 时主操作就该可点——接口文档是「有更好」，不是必填
    await byId(wrapper, 'field-pair-prd').setValue('# 小账本 PRD\n\n正文')
    await clickButton(wrapper, '生成提示词套件', true)
    await flushPromises()

    expect(jobCount('prompts')).toBe(1)
    // 接口文档是选填：方案状态里没有这一份，请求体里也不该凭空多出正文
    expect(jobPayload('prompts')).not.toHaveProperty('apiDocsContent')
    expect(savedSolutionStates().some((state) => String(state.paste_api_docs_text ?? ''))).toBe(
      false,
    )
  })

  it('我有 PRD + 接口文档：未贴 PRD 时不能提交，缺失提示常驻', async () => {
    const wrapper = await mountEntry()
    await clickButton(wrapper, '我有 PRD + 接口文档')

    const generate = wrapper
      .findAll('button')
      .find((item) => item.text().trim() === '生成提示词套件')
    expect((generate?.element as HTMLButtonElement).disabled).toBe(true)

    expect(jobCount('prompts')).toBe(0)
    expect(wrapper.text()).toContain('请先贴入 PRD 正文')
  })

  it('两个录入页都能「返回入口」，重新选另一条入口', async () => {
    const wrapper = await mountEntry()
    await clickButton(wrapper, '快捷流程')
    expect(wrapper.text()).toContain('直接生成 PRD')

    await clickButton(wrapper, '返回入口', true)
    expect(wrapper.text()).toContain('新建 PRD 方案')

    await clickButton(wrapper, '我有 PRD')
    expect(wrapper.text()).toContain('从现有 PRD 生成提示词套件')

    await clickButton(wrapper, '返回入口', true)
    // 三个录入页都要能退回来，否则用户换一条路只能刷新页面
    await clickButton(wrapper, '我有 PRD + 接口文档')
    expect(wrapper.text()).toContain('从现有 PRD 与接口文档生成提示词套件')

    await clickButton(wrapper, '返回入口', true)
    await clickButton(wrapper, '完整流程')
    expect(wrapper.text()).toContain('告诉我们你的产品想法')
    // 换了入口，进度条口径也跟着换回六步
    expect(wrapper.findAll('nav[aria-label="生成进度"] li')).toHaveLength(6)
  })

  it('完整流程的表单也有返回按钮，默认回到入口页', async () => {
    const wrapper = await mountApp()
    expect(wrapper.text()).toContain('告诉我们你的产品想法')

    await clickButton(wrapper, '返回入口', true)
    expect(wrapper.text()).toContain('新建 PRD 方案')
  })

  it('从对话页「改需求」回到表单时，返回按钮改为回到对话', async () => {
    vi.mocked(api.startConversationStream).mockImplementation(async (_payload, handlers) => {
      handlers?.onChunk?.({ text: '已理解需求。' })
      return { conversationId: 'conv-1', reply: '已理解需求。', asks: [] }
    })

    const wrapper = await mountApp()
    await submitSampleForm(wrapper)
    expect(wrapper.text()).toContain('需求澄清对话')

    await clickButton(wrapper, '改需求')
    expect(wrapper.text()).toContain('告诉我们你的产品想法')
    // 已经进过对话：落点与文案都要跟着变，不能把用户送回入口页丢掉对话位置
    expect(wrapper.text()).not.toContain('返回入口')
    expect(wrapper.text()).toContain('返回对话')

    await clickButton(wrapper, '返回对话', true)
    expect(wrapper.text()).toContain('需求澄清对话')
  })
})

/**
 * 交付页入口：齐备即可进入。
 *
 * 交付页不再由「完成」按钮把关——进度条的「交付」格是**唯一**入口，可点条件是
 * 「本入口所需文档齐备」（完整流程与快捷流程三份、两个导入入口只要提示词）。
 * 齐备那一刻给一次全应用提示，但**不把用户从当前页面带走**：他可能正在改稿。
 */
describe('AppV2 交付页入口', () => {
  /** 三份产物各一份正文（完整流程 / 快捷流程的齐备条件） */
  function allDocuments(): DocumentRecord[] {
    return [
      makeDocumentRecord('prd', '# 产品需求文档\n\n## 1. 项目背景\n内容'),
      makeDocumentRecord('api-docs', '# 接口文档\n\n## 一、概述\n内容'),
      makeDocumentRecord('prompts', '# 提示词套件\n\n## 一、角色设定\n内容'),
    ]
  }

  /** 侧栏方案列表里的一行（列表用得到的字段） */
  function sidebarItem(id: number, title: string) {
    return {
      id,
      title,
      mode: 'structured',
      stage: 'done',
      created_at: '2026-10-13T00:00:00',
      updated_at: '2026-10-13T00:00:00',
      content_updated_at: '2026-10-13T00:00:00',
    }
  }

  /** 点侧栏方案列表里的某一项（换方案走这条路，与用户操作一致） */
  async function clickSolutionInSidebar(wrapper: VueWrapper, title: string): Promise<void> {
    const sidebar = wrapper.find('aside[aria-label="方案列表"]')
    const item = sidebar.findAll('button').find((button) => button.text().includes(title))
    if (!item) throw new Error(`侧栏未找到方案：${title}`)
    await item.trigger('click')
    await flushPromises()
    await flushPromises()
  }

  it('审阅页没有「完成」按钮：齐备后点进度条「交付」格进入交付页', async () => {
    const wrapper = await mountExistingSolution(reviewSnapshot('review-prd'), 1, allDocuments())

    // 「完成」已经撤掉：交付页的唯一入口是进度条那一格
    expect(wrapper.findAll('button').some((item) => item.text().trim() === '完成')).toBe(false)
    expect(storeOf(wrapper).viewState).toBe('review-prd')

    await clickStep(wrapper, '交付')
    await flushPromises()

    expect(storeOf(wrapper).viewState).toBe('done')
    expect(wrapper.text()).toContain('3 份文档已全部完成')
  })

  it('所需文档未齐备时「交付」格点不动，页面留在原地', async () => {
    const wrapper = await mountExistingSolution(reviewSnapshot('review-prd'), 1, [
      makeDocumentRecord('prd', '# 产品需求文档\n\n## 1. 项目背景\n内容'),
    ])

    await clickStep(wrapper, '交付')
    await flushPromises()

    // 只有 PRD：齐备条件没满足，点它不该把用户送进一个空交付页
    expect(storeOf(wrapper).viewState).toBe('review-prd')
    expect(wrapper.text()).not.toContain('文档已生成完毕')
  })

  it('导入入口的齐备判据只有提示词：提示词生成完即可进交付页', async () => {
    const wrapper = await mountExistingSolution(
      { ...reviewSnapshot('review-prompts'), entryMode: 'prompt-debug' },
      1,
      [makeDocumentRecord('prompts', '# 提示词套件\n\n## 一、角色设定\n内容')],
    )

    // 这条入口的进度条只有「提示词 → 交付」两格
    expect(wrapper.findAll('nav[aria-label="生成进度"] li')).toHaveLength(2)

    await clickStep(wrapper, '交付')
    await flushPromises()

    expect(storeOf(wrapper).viewState).toBe('done')
    // 交付页只摆这一份：另外两份本来就不是这条入口的产物
    expect(wrapper.text()).toContain('文档已生成完毕')
    expect(wrapper.findAll('article')).toHaveLength(1)
  })

  it('齐备那一刻只提示一次，不把用户从当前页面带走', async () => {
    const wrapper = await mountExistingSolution(reviewSnapshot('review-prd'), 1, allDocuments())

    expect(wrapper.text()).toContain('三份文档已齐备，可在进度条进入交付页')
    // 停在原页：正文还在，交付页没有被自动打开
    expect(storeOf(wrapper).viewState).toBe('review-prd')
    expect(wrapper.text()).toContain('生成接口文档')
  })

  it('导入入口齐备时不弹「三份文档已齐备」：那句话只说三份', async () => {
    // 规格只要求「三份齐备时全应用提示一次」。两个导入入口只要一份提示词，
    // 对它们说「已齐备」是不可见的谎话；入口照旧是进度条那一格（下一条用例在点它）。
    const wrapper = await mountExistingSolution(
      { ...reviewSnapshot('review-prompts'), entryMode: 'prompt-debug' },
      1,
      [makeDocumentRecord('prompts', '# 提示词套件\n\n## 一、角色设定\n内容')],
    )

    expect(wrapper.text()).not.toContain('可在进度条进入交付页')
    // 交付格照旧可点：不弹提示不等于把入口锁上
    await clickStep(wrapper, '交付')
    await flushPromises()
    expect(storeOf(wrapper).viewState).toBe('done')
  })

  it('提示按方案各给一次：换到三份齐备的方案会提示，回到导入入口不再提示', async () => {
    vi.mocked(solutionApi.fetchSolutions).mockResolvedValue([
      sidebarItem(1, '方案一'),
      sidebarItem(2, '方案二'),
    ])
    vi.mocked(solutionApi.fetchSolution).mockImplementation(async (id: number) =>
      id === 2
        ? makeSolutionDetail(
            2,
            { ...reviewSnapshot('review-prompts'), entryMode: 'prompt-debug' },
            { mode: 'prompt-debug' },
          )
        : makeSolutionDetail(1, reviewSnapshot('review-prd')),
    )
    vi.mocked(documentApi.fetchDocuments).mockImplementation(async (id: number) =>
      id === 2
        ? [makeDocumentRecord('prompts', '# 提示词套件\n\n## 一、角色设定\n内容')]
        : allDocuments(),
    )

    // 从导入入口开始：只有一份提示词，不提示
    const wrapper = await mountEntry('/v2/2')
    expect(wrapper.text()).not.toContain('可在进度条进入交付页')

    // 换到三份齐备的方案：这时才弹那条提示
    await clickSolutionInSidebar(wrapper, '方案一')
    expect(wrapper.text()).toContain('三份文档已齐备，可在进度条进入交付页')
    expect(storeOf(wrapper).viewState).toBe('review-prd')
  })
})

/**
 * PRD 与另两份共用同一套阶段展示（票 #92）。
 *
 * 多章看板已退役：PRD 生成期间显示的仍是 `DocPhaseProgress` 那五格（大纲 →
 * 生成 → 审核 → 改写 → 完成），大纲内容走阶段条下方的输出块。
 */
describe('AppV2 PRD 与另两份共用阶段展示', () => {
  it('PRD 生成显示共用阶段条与大纲内容，不再按章渲染看板', async () => {
    const state: {
      handlers?: api.StreamHandlers
      finish: (reply: string) => Promise<void>
    } = { finish: async () => {} }
    mockJobStream({
      prd: (handlers) => {
        state.handlers = handlers
        return new Promise<api.StreamResult>((resolve) => {
          state.finish = async (reply: string) => {
            resolve({ conversationId: '', reply, asks: [] })
            await flushPromises()
          }
        })
      },
    })
    const wrapper = await mountApp()
    await submitSampleForm(wrapper)
    await clickButton(wrapper, '生成 PRD', true)
    await flushPromises()
    await confirmSummaryPanel(wrapper)
    await nextTick()

    state.handlers?.onStage?.({ stage: 'outlining', note: '', issues: [], continuation: 0 })
    await nextTick()
    state.handlers?.onStageChunk?.({
      stage: 'outlining',
      text: '- 1. 产品概述\n    · "product_name": "演示产品",',
    })
    await nextTick()

    // 阶段条是共用组件：五格都在，且大纲内容真的展示出来了（不是一片空白）
    expect(wrapper.text()).toContain('大纲')
    expect(wrapper.text()).toContain('生成')
    expect(wrapper.text()).toContain('审核')
    expect(wrapper.text()).toContain('改写')
    expect(wrapper.text()).toContain('产品概述')
  })
})
