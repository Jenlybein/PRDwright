import { flushPromises, mount, type DOMWrapper, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia, type Pinia } from 'pinia'
import { nextTick } from 'vue'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import AppV2 from '../AppV2.vue'
import AppBadge from '../components/ui/AppBadge.vue'
import ChatStage from '../components/stages/ChatStage.vue'
import EntryStage from '../components/stages/EntryStage.vue'
import ReviewStage from '../components/stages/ReviewStage.vue'
import * as api from '../services/api'
import * as authService from '../services/authService'
import * as documentApi from '../services/documentService'
import * as solutionApi from '../services/solutionService'
import { useAuthStore } from '../stores/auth'
import { DOCUMENT_SAVE_DEBOUNCE_MS, useDocumentsStore } from '../stores/documents'
import { useSolutionsStore } from '../stores/solutions'
import { useWorkflowStore } from '../stores/workflow'
import { downloadFile } from '../utils/download'
import type {
  ChatMessage,
  DocumentRecord,
  DocumentVersionDetail,
  DocumentVersionSummary,
  JobSnapshot,
  RequirementsSummary,
  RunSummary,
  StreamDocumentPart,
  StreamStageEvent,
} from '../types'

/**
 * 生成任务撮合器。
 *
 * 流水线已从「一次请求」升级为「一个后端任务」：`useJob` 先 `createJob`、再
 * `readJobStream`。测试要按 **kind** 决定这次任务该产出哪一类事件，而
 * `readJobStream` 只拿得到 `jobId`——因此这里用一张 `jobId → kind` 的表把两者接上，
 * 让每个用例只需配置「prd 这条流怎么跑」，不必关心它拿到的是哪个 id。
 */
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
    handlers.onChunk?.('# 提示词套件\n')
    return { conversationId: '', reply: '# 提示词套件\n\n## 一、角色设定\n内容', asks: [] }
  }
  if (kind === 'api-docs') {
    handlers.onChunk?.('# 接口文档\n')
    return { conversationId: '', reply: '# 接口文档\n\n## 一、概述\n统一响应结构', asks: [] }
  }
  if (kind === 'optimize') {
    return { conversationId: '', reply: '优化后的内容', asks: [] }
  }
  handlers.onChunk?.('# 产品需求文档\n')
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

/** 造一份任务快照（刷新重连时后端首发的事件） */
function makeJobSnapshot(overrides: Partial<JobSnapshot> = {}): JobSnapshot {
  return {
    jobId: 1,
    kind: 'prd',
    status: 'running',
    phase: '',
    finalizedText: '',
    liveBuffer: '',
    stageText: '',
    completedSections: [],
    solutionId: 1,
    errorCode: '',
    errorMessage: '',
    elapsedMs: 0,
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
    status: 'completed',
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
    provider: 'ark',
    model: 'doubao',
    requestId: 'req-default',
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
    data: payload.data,
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
    data: JSON.stringify(snapshot),
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
): DocumentRecord {
  return {
    kind,
    content,
    runSummary,
    pendingItems,
    updatedAt: '2026-10-13T00:00:00',
    stale: false,
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

  it('提交需求后进入对话阶段，映射后端题目字段且载荷不携带任何提示词', async () => {
    vi.mocked(api.startConversationStream).mockImplementation(async (_payload, handlers) => {
      handlers?.onChunk?.('我先复述一下理解：这是一个给独立开发者用的 PRD 生成器。')
      return { conversationId: 'conv-1', reply: '我先复述一下理解：这是一个给独立开发者用的 PRD 生成器。', asks: [] }
    })

    const wrapper = await mountApp()
    await submitSampleForm(wrapper)

    expect(wrapper.text()).toContain('需求澄清对话')
    expect(wrapper.text()).toContain('我先复述一下理解')
    expect(api.startConversationStream).toHaveBeenCalledTimes(1)

    // 载荷：结构化字段映射到后端已有题目的字段名
    const payload = vi.mocked(api.startConversationStream).mock.calls[0]?.[0]
    expect(payload?.answers.product_name).toBe('PRDwright')
    expect(payload?.answers.problem_to_solve).toBe('把模糊想法整理成可开工的 PRD')
    expect(payload?.answers.platform_type).toBe('网页应用')
    expect(payload?.answers.core_features).toEqual(['问卷填写 —— 收集产品信息'])

    // 提示词归后端独占（后端按对话档案解析人设与开场白），前端只提交问卷内容
    expect(payload).not.toHaveProperty('systemPrompt')
    expect(payload).not.toHaveProperty('system_prompt_override')
    expect(JSON.stringify(payload)).not.toContain('资深产品经理')
  })

  it('澄清提问带结构化选项时渲染成选项卡，提交后作为一条用户消息发出、卡片随之消失', async () => {
    vi.mocked(api.startConversationStream).mockImplementation(async (_payload, handlers) => {
      handlers?.onChunk?.('你更倾向哪种做法？')
      return {
        conversationId: 'conv-1',
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
    })
    // 提交之后 AI 会回一句，于是那条带选项的消息不再是最后一条
    vi.mocked(api.continueConversationStream).mockResolvedValue({
      conversationId: 'conv-1',
      reply: '好，那就先做离线草稿。',
      asks: [],
    })

    const wrapper = await mountApp()
    await submitSampleForm(wrapper)

    const optionA = wrapper.findAll('button').find((item) => item.text().startsWith('A'))
    expect(optionA, '后端下发了选项就应当渲染出可点的卡片').toBeTruthy()
    expect(optionA?.attributes('disabled')).toBeUndefined()

    await optionA?.trigger('click')
    await clickButton(wrapper, '提交回答')
    await flushPromises()

    // 提交走的是**普通发送链路**：载荷里就是一条用户消息（带题干，模型才知道答的是哪题）
    const payload = vi.mocked(api.continueConversationStream).mock.calls[0]?.[0]
    expect(payload?.content).toBe('1. 第一版要不要做离线同步？\n   答：A. 第一版就做离线同步')

    // 作答之后它不再是最后一条消息：卡片整块消失，而不是留下点不动的只读卡片
    const after = wrapper.findAll('button').find((item) => item.text().startsWith('A'))
    expect(after).toBeUndefined()
  })

  it('选项卡的作答沿 emit 链原样透传到接口的结构化 tool_result', async () => {
    vi.mocked(api.startConversationStream).mockImplementation(async (_payload, handlers) => {
      handlers?.onChunk?.('你更倾向哪种做法？')
      return {
        conversationId: 'conv-1',
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
    })
    vi.mocked(api.continueConversationStream).mockResolvedValue({
      conversationId: 'conv-1',
      reply: '好，那就先做离线草稿。',
      asks: [],
    })

    const wrapper = await mountApp()
    await submitSampleForm(wrapper)

    await wrapper.findAll('button').find((item) => item.text().startsWith('A'))?.trigger('click')
    await clickButton(wrapper, '提交回答')
    await flushPromises()

    const payload = vi.mocked(api.continueConversationStream).mock.calls[0]?.[0]
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

    // 但界面上不再念这段话：改用一行轻回执，只列选中的标号
    const receipt = wrapper.find('[aria-label="你选了 A"]')
    expect(receipt.exists()).toBe(true)
    expect(receipt.text()).not.toContain('第一版就做离线同步')
    expect(receipt.text()).not.toContain('第一版要不要做离线同步？')
  })

  it('对话完成后可生成 PRD，并在生成完成时进入待确认阶段', async () => {
    vi.mocked(api.startConversationStream).mockImplementation(async (_payload, handlers) => {
      handlers?.onChunk?.('已理解需求。')
      return { conversationId: 'conv-1', reply: '已理解需求。', asks: [] }
    })

    const wrapper = await mountApp()
    await reachPrdReview(wrapper)

    expect(wrapper.text()).toContain('PRD 文档')
    expect(wrapper.text()).toContain('项目背景')

    // PRD 生成拿到的是与 field-schema 对齐的结构化 JSON（走 createJob 下发）
    const payload = jobPayload('prd')
    expect(payload?.kind).toBe('prd')
    // 直接下发结构化摘要字典，需求文本由后端统一格式化
    expect(payload?.summary?.product_name).toBe('PRDwright')
    expect(payload?.summary?.mvp_features?.[0]?.name).toBe('问卷填写')
    // 对话历史一并带上：首轮只有 AI 那条回复（需求本身走 summary 传入）
    expect(payload?.history?.length).toBe(1)
    expect(payload?.history?.[0]?.role).toBe('assistant')
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
    // 回填结果已写回摘要，并作为生成输入下发
    expect(prdPayload?.summary?.target_users).toContain('小团队产品经理')
    expect(prdPayload?.summary?.mvp_features?.[0]?.user_value).toBe('省时间')
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

    const wrapper = await mountExistingSolution(
      {
        viewState: 'chatting',
        entryMode: 'structured',
        summary: SAMPLE_SUMMARY,
        messages: [
          { role: 'assistant', content: '我先确认几个关键点', stage: 'requirement' },
          { role: 'assistant', content: '准备开始 P0 级问题的确认', stage: 'document' },
        ],
        conversationId: '',
        quickInput: { productName: '', requirements: '' },
        debugPrdText: '',
        pastePairInput: { prd: '', apiDocs: '' },
        activeJobId: null,
        activeJobKind: null,
        clarifyMessages: [],
        clarifyConversationId: '',
        clarifyTarget: null,
        prdClarifyConversationId: '',
        prdClarifying: false,
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
        handlers.onChunk?.('# 接口文档\n')
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
    vi.mocked(api.startConversationStream).mockImplementation(async () => ({
      conversationId: 'conv-1',
      reply: '已理解需求。',
      asks: [],
    }))

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
      handlers?.onChunk?.('续写')
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

  it('恢复路径 1b：刷新时正赶上审核，阶段输出随快照接回来', async () => {
    vi.mocked(api.readJobStream).mockImplementation(async (jobId, handlers) => {
      expect(jobId).toBe(43)
      // 重连**不会补发**历史 `stage_chunk`：审核意见只能靠快照的 `stageText` 带回来
      handlers?.onSnapshot?.(
        makeJobSnapshot({
          jobId: 43,
          kind: 'api_docs',
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

  it('恢复路径 2：无指针但快照落在生成中时兜底查到活跃任务，撤销降级并重连', async () => {
    vi.mocked(api.fetchActiveJobs).mockResolvedValue([
      {
        id: 7,
        kind: 'prd',
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

    // 兜底查询带上了当前方案与文档种类
    expect(api.fetchActiveJobs).toHaveBeenCalledWith({ solutionId: 1, kind: 'prd', status: 'running' })
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
    vi.mocked(api.startConversationStream).mockRejectedValue(new Error('后端不可用'))

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
      expect(documentApi.saveDocument).toHaveBeenCalledWith(1, 'prd', '# 改过的正文')
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
    expect(payload?.requirements).toBe('做一个记账 App，支持多人共享账本。')
    expect(payload?.productName).toBe('小账本')
    // 没有结构化摘要，也没有对话历史（这条路根本不聊）
    expect(payload?.summary ?? null).toBeNull()
    expect(payload?.history ?? []).toEqual([])
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

  it('快捷流程：交付条件与完整流程一致，只生成 PRD 时「完成」仍不可用', async () => {
    const wrapper = await mountEntry()
    await submitQuickPrd(wrapper)

    // 两条流程都要求三份齐备才算交付完成，快捷流程不降低门槛
    const finish = wrapper.findAll('button').find((item) => item.text().trim() === '完成')
    expect(finish).toBeDefined()
    expect((finish?.element as HTMLButtonElement).disabled).toBe(true)
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
    expect(payload?.prdContent).toContain('家庭记账工具')
    expect(wrapper.text()).toContain('提示词套件')
    expect(wrapper.text()).toContain('角色设定')

    // 没生成过 PRD，就不能出现会点进空文档的入口；返回目标也是录入页
    expect(wrapper.text()).not.toContain('查看 PRD')
    expect(wrapper.text()).toContain('返回 PRD')

    // 只有提示词也能完成，且完成页只留这一张卡片
    await clickButton(wrapper, '完成', true)
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
    expect(payload?.prdContent).toContain('家庭记账工具')
    // 这一条入口的全部意义：接口文档也要真的送到后端
    expect(payload?.apiDocsContent).toContain('POST /api/ledgers')
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
    expect(jobPayload('prompts')?.apiDocsContent).toBe('')
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
      handlers?.onChunk?.('已理解需求。')
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
 * 分章节生成（第二阶段）的前端表现。
 *
 * 用一个「手动放行」的流替代默认 mock：先把回调交出来，再逐步推送 stage 事件，
 * 这样才有机会观察到生成过程中的中间态（默认 mock 会立刻跑完并跳到待确认页）。
 */
describe('AppV2 分章节生成的阶段展示', () => {
  /** 手动控制的一次 PRD 流 */
  interface ControlledStream {
    /** 后端回调 */
    handlers: api.StreamHandlers | undefined
    /** 结束这次流 */
    finish: (reply: string) => Promise<void>
  }

  /** 把 PRD 生成换成可手动推进的流，并走到「生成中」阶段 */
  async function startControlledPrdGeneration(wrapper: VueWrapper): Promise<ControlledStream> {
    const state: ControlledStream = { handlers: undefined, finish: async () => {} }
    // 先把对话环节走通（拿到「生成 PRD」入口），再接管 PRD 生成
    vi.mocked(api.startConversationStream).mockResolvedValue({
      conversationId: 'conv-1',
      reply: '已理解需求。',
      asks: [],
    })
    // 用「手动放行」的 PRD 任务流替换默认实现，才能观察到生成过程中的中间态
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

    await submitSampleForm(wrapper)
    await clickButton(wrapper, '生成 PRD', true)
    await flushPromises()
    await confirmSummaryPanel(wrapper)
    await nextTick()
    return state
  }

  /** 推送一条章节级 stage 事件 */
  async function pushStage(
    stream: ControlledStream,
    stage: string,
    extra: Partial<StreamStageEvent> = {},
  ): Promise<void> {
    stream.handlers?.onStage?.({
      stage,
      sectionId: 'overview',
      sectionTitle: '产品概述',
      sectionHeading: '## 1. 产品概述',
      index: 0,
      total: 7,
      note: '',
      issues: [],
      ...extra,
    })
    await nextTick()
  }

  /** 推送一段「已定稿正文」（文档头或某一章） */
  async function pushPart(
    stream: ControlledStream,
    text: string,
    extra: Partial<StreamDocumentPart> = {},
  ): Promise<void> {
    stream.handlers?.onPart?.({
      text,
      sectionId: '',
      sectionTitle: '',
      sectionHeading: '',
      index: 0,
      total: 0,
      ...extra,
    })
    await nextTick()
  }

  it('按 stage 事件展示车道的章节号（并行看板）', async () => {
    const wrapper = await mountApp()
    const stream = await startControlledPrdGeneration(wrapper)

    await pushStage(stream, 'writing', { total: 7 })
    await nextTick()
    // 有章节信息后看板才出现：四个车道 + 第 1 章在「生成」
    expect(wrapper.text()).toContain('生成')
    expect(wrapper.text()).toContain('审核')
    expect(wrapper.text()).toContain('改写')
    expect(wrapper.text()).toContain('定稿')
    expect(wrapper.text()).toContain('①')
    expect(wrapper.text()).toContain('0/7')

    // 第 2 章进入审核：两章分别落在不同车道（并行）
    await pushStage(stream, 'reviewing', {
      sectionId: 'features',
      sectionTitle: '功能需求',
      sectionHeading: '## 2. 功能需求',
      index: 1,
    })
    expect(wrapper.text()).toContain('②')

    await stream.finish('# 产品需求文档\n\n## 1. 产品概述\n\n第一版内容')
    expect(wrapper.text()).toContain('PRD 文档')
  })

  it('part 定稿后章节进入「定稿」车道，并按 index 归位', async () => {
    const wrapper = await mountApp()
    const stream = await startControlledPrdGeneration(wrapper)

    await pushStage(stream, 'writing', { total: 7 })
    await pushPart(stream, '## 1. 产品概述\n\n需求里没写标题\n\n', {
      sectionId: 'overview',
      sectionTitle: '产品概述',
      sectionHeading: '## 1. 产品概述',
      index: 0,
      total: 7,
    })

    // 预览用 part 的权威文本（含后端补上的标题）
    expect(wrapper.text()).toContain('1. 产品概述')
    expect(wrapper.text()).toContain('需求里没写标题')
    // 看板：第 1 章已定稿
    expect(wrapper.text()).toContain('1/7')

    await stream.finish('# 产品需求文档\n\n## 1. 产品概述\n\n需求里没写标题')
    expect(wrapper.text()).toContain('PRD 文档')
  })

  it('同一段定稿正文被重复投递时预览不重复（重复订阅也污染不了预览）', async () => {
    const wrapper = await mountApp()
    const stream = await startControlledPrdGeneration(wrapper)

    // 文档头与第一章各投递两遍——等价于「同一个任务被订阅了两次」时客户端的所见
    await pushPart(stream, '# 产品需求文档\n\n')
    await pushPart(stream, '# 产品需求文档\n\n')

    await pushStage(stream, 'writing')
    stream.handlers?.onChunk?.('## 1. 产品概述\n\n正文内容\n')
    await nextTick()

    const overview: Partial<StreamDocumentPart> = {
      sectionId: 'overview',
      sectionTitle: '产品概述',
      sectionHeading: '## 1. 产品概述',
      total: 7,
    }
    await pushPart(stream, '## 1. 产品概述\n\n正文内容\n\n', overview)
    await pushPart(stream, '## 1. 产品概述\n\n正文内容\n\n', overview)

    const text = wrapper.text()
    // 重复的 part 被守卫丢弃：文档头与本章正文都只出现一次
    expect(text.split('产品需求文档').length - 1).toBe(1)
    expect(text.split('正文内容').length - 1).toBe(1)
  })

  it('改写时旧稿留在预览里（不出现空白期），新稿到达后替换', async () => {
    const wrapper = await mountApp()
    const stream = await startControlledPrdGeneration(wrapper)

    await pushStage(stream, 'writing', { total: 7 })
    await pushPart(stream, '## 1. 产品概述\n\n第一版内容（编造了指标）\n\n', {
      sectionId: 'overview',
      sectionTitle: '产品概述',
      sectionHeading: '## 1. 产品概述',
      index: 0,
      total: 7,
    })
    expect(wrapper.text()).toContain('第一版内容（编造了指标）')

    await pushStage(stream, 'reviewing', { sectionId: 'overview', index: 0 })
    await pushStage(stream, 'rewriting', {
      sectionId: 'overview',
      index: 0,
      note: '出现了输入中没有的性能指标',
      issues: [{ severity: 'high', detail: '出现了输入中没有的性能指标' }],
    })

    // 旧稿不撤下：直接清空会让用户看到「这一章突然没了」，而它只是想表达「正在重写」
    expect(wrapper.text()).toContain('第一版内容（编造了指标）')
    // 看板：第 1 章在「改写」车道
    expect(wrapper.text()).toContain('①')

    // 重写内容一流出就顶替旧稿，不会与新文本并存
    await pushPart(stream, '## 1. 产品概述\n\n重写后的内容\n\n', {
      sectionId: 'overview',
      sectionTitle: '产品概述',
      sectionHeading: '## 1. 产品概述',
      index: 0,
      total: 7,
    })
    expect(wrapper.text()).toContain('重写后的内容')
    expect(wrapper.text()).not.toContain('编造了指标')

    await stream.finish('# 产品需求文档\n\n## 1. 产品概述\n\n重写后的内容')
    expect(wrapper.text()).toContain('PRD 文档')
  })

  it('后端降级为整篇生成（没有 stage 事件）时不显示章节计数，流程照常走完', async () => {
    vi.mocked(api.startConversationStream).mockResolvedValue({
      conversationId: 'conv-1',
      reply: '已理解需求。',
      asks: [],
    })
    mockJobStream({
      prd: async (handlers) => {
        handlers.onChunk?.('# 产品需求文档\n\n## 1. 项目背景\n内容')
        return { conversationId: '', reply: '# 产品需求文档\n\n## 1. 项目背景\n内容', asks: [] }
      },
    })

    const wrapper = await mountApp()
    await reachPrdReview(wrapper)

    expect(wrapper.text()).toContain('PRD 文档')
    expect(wrapper.text()).not.toContain('第 1 /')
    expect(wrapper.text()).not.toContain('逐章撰写')
  })
})

/**
 * 文档澄清：从审阅页进入**对话页的某条线程**、聊清楚、再按结论就地改写。
 *
 * 三条文档线程（PRD / 接口 / 提示词）与需求澄清**共用同一个对话页**，靠顶部子标签栏切换；
 * 消息共用一张表、靠 `stage` 分线（ADR-028）。这条链路的落点必须在测试里钉住：
 *
 * ① 交给后端的**只有**正文与清单（前端不递提示词，只点名 `doc-clarification` 场景）；
 * ② 改写任务必须带 `docKind`——它既是「写回哪一份」也是「注入哪套产出规范」的依据，
 *    少了后端不会猜，而是拒绝落库；写错则是一次**看起来挺合理**的静默错写；
 * ③ 改写完成后回到**那份文档**的审阅页，而不是停在对话页看一份旧正文；
 * ④ 对话区只显示当前标签那条线程——串线同样是静默的，用户只会觉得「我聊的怎么跑别处去了」。
 */
describe('AppV2 文档澄清', () => {
  const PENDING = [
    {
      id: 'p-1',
      question: '分页上限取多少？',
      sections: ['4.3'],
      decider: '产品负责人',
      importance: 'P0' as const,
    },
  ]

  /** 从审阅页点进澄清，等首轮追问落地 */
  async function openClarification(wrapper: VueWrapper): Promise<void> {
    await clickButton(wrapper, CLARIFY_ENTRY_TEXT)
    await flushPromises()
  }

  /** 造一个按「在哪份文档上澄清」回不同话术的开场 mock，便于区分各线程的消息 */
  function mockOpeningByDocument(): void {
    vi.mocked(api.startConversationStream).mockImplementation(async (payload, handlers) => {
      const reply = String(payload.answers.document ?? '').startsWith('# 接口文档')
        ? '接口那边先确认分页'
        : '先问最关键的一个：分页上限取多少？'
      handlers?.onChunk?.(reply)
      return { conversationId: `conv-${reply.slice(0, 2)}`, reply, asks: [] }
    })
  }

  /** 打开一份「PRD 有待确认项」的方案，停在 PRD 审阅页 */
  async function mountReviewWithPending(): Promise<VueWrapper> {
    mockOpeningByDocument()
    const wrapper = await mountExistingSolution(reviewSnapshot('review-prd'), 1, [
      makeDocumentRecord('prd', '# 产品需求文档\n\n分页上限：待确认', null, PENDING),
    ])
    await flushPromises()
    return wrapper
  }

  /** 打开一份「PRD 与接口文档都齐、接口文档有待确认项」的方案，停在接口文档审阅页 */
  async function mountApiDocsReview(): Promise<VueWrapper> {
    mockOpeningByDocument()
    const wrapper = await mountExistingSolution(reviewSnapshot('review-api-docs'), 1, [
      makeDocumentRecord('prd', '# 产品需求文档'),
      makeDocumentRecord('api-docs', '# 接口文档\n\n分页上限：待确认', null, PENDING),
    ])
    await flushPromises()
    return wrapper
  }

  it('审阅 PRD 页给出澄清入口，并把正文与清单交给后端（场景点名，不递提示词）', async () => {
    const wrapper = await mountReviewWithPending()

    expect(wrapper.text()).toContain('前往澄清问题')
    expect(wrapper.text()).toContain('还剩 1 个问题')
    await openClarification(wrapper)

    // 回到的是**对话页**（标题切到 PRD 澄清），不是另开一个澄清页
    expect(wrapper.text()).toContain('PRD 待确认项澄清')

    const payload = vi.mocked(api.startConversationStream).mock.calls[0]?.[0]
    // 前端只能点名场景：档案标识由后端解析成人设与开场白
    expect(payload?.profileKey).toBe('doc-clarification')
    expect(payload?.answers.document).toContain('分页上限：待确认')
    expect(payload?.answers.pending_items).toContain(
      '1. [P0] 分页上限取多少？（涉及章节：4.3；拍板人：产品负责人）',
    )
  })

  it('接口文档的审阅页给的是同一个入口（三种审阅页不再各走一条路）', async () => {
    const wrapper = await mountApiDocsReview()

    expect(wrapper.text()).toContain('前往澄清问题')
    expect(wrapper.text()).toContain('还剩 1 个问题')
    await openClarification(wrapper)

    // 进的是对话页的**接口澄清**那条线程
    expect(wrapper.text()).toContain('接口文档待确认项澄清')
    expect(storeOf(wrapper).chatTab).toBe('api-docs-clarify')
  })

  it('「快捷流程」的审阅页也给同一组入口，且不再重复给「返回描述」', async () => {
    mockOpeningByDocument()
    const wrapper = await mountExistingSolution(
      { ...reviewSnapshot('review-prd'), entryMode: 'prd-quick' },
      1,
      [makeDocumentRecord('prd', '# 产品需求文档\n\n分页上限：待确认', null, PENDING)],
    )
    await flushPromises()

    // 四条入口的澄清入口完全一致
    expect(wrapper.text()).toContain('前往澄清问题')
    expect(wrapper.text()).toContain('AI 自行澄清')
    // 剩余条数放在按钮旁的芯片里，不进按钮文案
    expect(wrapper.text()).toContain('还剩 1 个问题')
    // 录入页在进度条第 1 步上，底栏不再有「返回描述」
    expect(wrapper.text()).not.toContain('返回描述')

    await openClarification(wrapper)
    expect(storeOf(wrapper).chatTab).toBe('prd-clarify')
  })

  it('点「按澄清结果改写」发起 clarify 任务，带 docKind 与这一轮对话，完成后回到审阅页', async () => {
    mockJobStream({
      clarify: async (handlers) => {
        handlers.onChunk?.('# 产品需求文档\n\n分页上限：50 条')
        return { conversationId: '', reply: '# 产品需求文档\n\n分页上限：50 条', asks: [] }
      },
    })

    const wrapper = await mountReviewWithPending()
    await openClarification(wrapper)

    await clickButton(wrapper, '按澄清结果改写')
    await flushPromises()

    const payload = jobPayload('clarify')
    expect(payload?.docKind).toBe('prd')
    // 对话记录随请求一起交给后端——改写依据就是它，后端不可能自己猜到
    expect(payload?.history?.[0]?.content).toContain('分页上限取多少？')
    expect(payload?.document).toContain('分页上限：待确认')

    // 改写完成 → 回到 PRD 的审阅页，并且正文已经是新的
    expect(wrapper.text()).toContain('PRD 文档')
    expect(wrapper.text()).not.toContain('PRD 待确认项澄清')
  })

  it('接口文档的改写带 docKind: api-docs 并回到接口文档的审阅页（别写进 PRD）', async () => {
    mockJobStream({
      clarify: async () => ({
        conversationId: '',
        reply: '# 接口文档\n\n分页上限：50 条',
        asks: [],
      }),
    })

    const wrapper = await mountApiDocsReview()
    await openClarification(wrapper)

    await clickButton(wrapper, '按澄清结果改写')
    await flushPromises()

    // 目标由**当前标签**反推：写错位置不会报错，只会静默地改错文档，因此必须钉住
    expect(jobPayload('clarify')?.docKind).toBe('api-docs')
    expect(storeOf(wrapper).viewState).toBe('review-api-docs')
    // 落回的正文在接口文档那一份上，PRD 没被碰
    expect(documentsOf(wrapper).contentOf('api-docs')).toContain('分页上限：50 条')
    expect(documentsOf(wrapper).contentOf('prd')).toBe('# 产品需求文档')
  })

  it('「AI 自行澄清」不开对话，直接把正文与清单交给后端拍板，完成后回到审阅页', async () => {
    mockJobStream({
      'auto-clarify': async (handlers) => {
        handlers.onChunk?.('# 产品需求文档\n\n分页上限：50 条')
        return { conversationId: '', reply: '# 产品需求文档\n\n分页上限：50 条', asks: [] }
      },
    })

    const wrapper = await mountReviewWithPending()
    // 它和「前往澄清问题」是两条路：这条不需要用户回答任何问题
    expect(api.startConversationStream).not.toHaveBeenCalled()

    await clickButton(wrapper, 'AI 自行澄清')
    // 它会直接改写正文，因此先过一次确认弹窗
    await clickButtonInDialog('开始澄清')

    const payload = jobPayload('auto-clarify')
    // 三样都要带：正文是改写对象，清单是拍板范围，docKind 决定写回哪一份
    expect(payload?.document).toContain('分页上限：待确认')
    expect(payload?.pendingItems).toHaveLength(1)
    expect(payload?.pendingItems?.[0]?.question).toBe('分页上限取多少？')
    expect(payload?.docKind).toBe('prd')

    // 落到 PRD 的审阅页，正文已经是 AI 拍板后的版本
    expect(storeOf(wrapper).viewState).toBe('review-prd')
    expect(documentsOf(wrapper).contentOf('prd')).toContain('分页上限：50 条')
  })

  it('「AI 自行澄清」失败时停在生成视图并给出错误，不悄悄退回审阅页', async () => {
    // 后端拒绝落库（如 doc_kind 对不上）时不会有正文，前端不能让用户以为改好了
    vi.mocked(api.readJobStream).mockImplementation(async (jobId, handlers) => {
      const kind = jobHarness.kinds.get(jobId) ?? 'prd'
      if (kind === 'auto-clarify') throw new Error('模型调用失败')
      return defaultJobStream(kind, handlers ?? {})
    })

    const wrapper = await mountReviewWithPending()
    await clickButton(wrapper, 'AI 自行澄清')
    await clickButtonInDialog('开始澄清')

    expect(storeOf(wrapper).viewState).toBe('generating-prd')
    expect(wrapper.text()).toContain('模型调用失败')
  })

  it('点「按澄清结果改写」立刻进入对应的生成视图展示流式改写，完成后回到审阅页', async () => {
    // 用挂起的 deferred 流制造「可观测的中间态」：改写会一直进行到 release() 被调用
    let release!: () => void
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    mockJobStream({
      clarify: async (handlers) => {
        handlers.onChunk?.('# 产品需求文档\n\n分页上限：50 条')
        await gate
        return { conversationId: '', reply: '# 产品需求文档\n\n分页上限：50 条', asks: [] }
      },
    })

    const wrapper = await mountReviewWithPending()
    await openClarification(wrapper)
    const store = storeOf(wrapper)

    await clickButton(wrapper, '按澄清结果改写')
    await flushPromises()

    // 中间态：已切到 PRD 生成视图、流式正文可见、对话栏标题消失
    expect(store.viewState).toBe('generating-prd')
    expect(wrapper.text()).toContain('PRD 文档')
    expect(wrapper.text()).not.toContain('PRD 待确认项澄清')
    expect(wrapper.text()).toContain('分页上限：50 条')

    // 放行任务 → 落到审阅页
    release()
    await flushPromises()
    expect(store.viewState).toBe('review-prd')
  })

  it('没有待确认项时不给澄清入口（点进去只能闲聊）', async () => {
    const wrapper = await mountExistingSolution(reviewSnapshot('review-prd'), 1, [
      makeDocumentRecord('prd', '# 产品需求文档\n\n正文'),
    ])
    await flushPromises()

    expect(wrapper.text()).toContain('PRD 文档')
    expect(wrapper.text()).not.toContain(CLARIFY_ENTRY_TEXT)
    expect(api.startConversationStream).not.toHaveBeenCalled()
  })

  it('澄清的消息落在那条线程上，需求线程一条不动', async () => {
    const wrapper = await mountReviewWithPending()
    const store = storeOf(wrapper)
    await openClarification(wrapper)

    // 与需求澄清**共用同一个列表**：用户看到的是一条连续的时间线
    expect(store.messages).toHaveLength(1)
    expect(store.messages[0]?.stage).toBe('prd-clarify')
    // 但明细上分得开：需求澄清那一轮一条都没有
    expect(
      store.messages.filter((message) => (message.stage ?? 'requirement') === 'requirement'),
    ).toHaveLength(0)
    expect(store.chatTab).toBe('prd-clarify')
    expect(wrapper.text()).toContain('PRD 待确认项澄清')
  })

  it('再回到对话时不重新开场——同一段澄清继续聊（这是「复用」的要害）', async () => {
    const wrapper = await mountReviewWithPending()
    const store = storeOf(wrapper)
    await openClarification(wrapper)
    expect(vi.mocked(api.startConversationStream)).toHaveBeenCalledTimes(1)

    // 回审阅页（澄清标签下没有「返回审阅」按钮，靠顶部进度条走回去）
    await clickStep(wrapper, 'PRD')
    await flushPromises()
    expect(store.viewState).toBe('review-prd')

    // 再进来：开场请求不应重发，否则每次回来都被问候一遍
    await openClarification(wrapper)

    expect(vi.mocked(api.startConversationStream)).toHaveBeenCalledTimes(1)
    expect(store.chatTab).toBe('prd-clarify')
    // 聊天记录没有被清空
    expect(store.messages).toHaveLength(1)
  })

  it('整篇重新生成某一篇，会作废它自己与下游那条线程的对话', async () => {
    mockOpeningByDocument()
    mockJobStream()

    // 提示词有待确认项，先在那里聊过一轮
    const wrapper = await mountExistingSolution(reviewSnapshot('review-prompts'), 1, [
      makeDocumentRecord('prd', '# 产品需求文档'),
      makeDocumentRecord('api-docs', '# 接口文档'),
      makeDocumentRecord('prompts', '# 提示词套件\n\n分页上限：待确认', null, PENDING),
    ])
    await flushPromises()
    const store = storeOf(wrapper)

    await openClarification(wrapper)
    expect(store.messages.filter((m) => m.stage === 'prompts-clarify')).toHaveLength(1)

    // 换一版提示词：回到接口文档那一步，重新生成提示词套件
    await clickStep(wrapper, '接口文档')
    await flushPromises()
    await clickButton(wrapper, '生成提示词套件')
    await flushPromises()

    // 旧线程聊的是**上一版**的待确认项。留着它，用户点「按澄清结果改写」就会把
    // 针对旧文档的结论写进新文档——一次静默的错写，而且看起来还挺合理
    expect(store.messages.filter((m) => m.stage === 'prompts-clarify')).toHaveLength(0)
  })
})

/**
 * 对话页的子标签栏：四条澄清线程常驻，切换即换对话历史与底栏动作。
 *
 * 这一块最容易出的错是**串线**：消息共用一张表，过滤口径一旦写歪，用户就会看到
 * 别人那条线程的对话，或者自己的对话莫名其妙少了。串线不报错，所以必须有用例。
 */
describe('AppV2 对话页的子标签栏', () => {
  const PENDING = [
    {
      id: 'p-1',
      question: '分页上限取多少？',
      sections: ['4.3'],
      decider: '产品负责人',
      importance: 'P0' as const,
    },
  ]

  /** 当前交给对话区渲染的消息（组件拿到的已经是按当前标签过滤好的那些） */
  function chatMessages(wrapper: VueWrapper): ChatMessage[] {
    return wrapper.findComponent(ChatStage).props('messages') as ChatMessage[]
  }

  /** 底栏里出现过的按钮文案 */
  function barButtons(wrapper: VueWrapper): string[] {
    return wrapper.find('.shell-action-bar').findAll('button').map((item) => item.text())
  }

  /** 造一个按「在哪份文档上澄清」回不同话术的开场 mock，便于区分各线程的消息 */
  function mockOpeningByDocument(): void {
    vi.mocked(api.startConversationStream).mockImplementation(async (payload, handlers) => {
      const reply = String(payload.answers.document ?? '').startsWith('# 接口文档')
        ? '接口那边先确认分页'
        : 'PRD 那边先确认分页'
      handlers?.onChunk?.(reply)
      return { conversationId: `conv-${reply.slice(0, 2)}`, reply, asks: [] }
    })
  }

  async function openPromptsAndPrdClarification(): Promise<VueWrapper> {
    mockOpeningByDocument()
    const wrapper = await mountExistingSolution(reviewSnapshot('review-prd'), 1, [
      makeDocumentRecord('prd', '# 产品需求文档\n\n分页上限：待确认', null, PENDING),
      makeDocumentRecord('api-docs', '# 接口文档\n\n分页上限：待确认', null, PENDING),
    ])
    await flushPromises()

    // 先在 PRD 那条线程上聊一轮
    await clickButton(wrapper, CLARIFY_ENTRY_TEXT)
    await flushPromises()
    // 再去接口文档那条线程聊一轮
    await clickStep(wrapper, '接口文档')
    await flushPromises()
    await clickButton(wrapper, CLARIFY_ENTRY_TEXT)
    await flushPromises()
    return wrapper
  }

  it('四条标签常驻，未就绪的置灰（标签就是地图）', async () => {
    const wrapper = await mountExistingSolution(reviewSnapshot('review-prd'), 1, [
      makeDocumentRecord('prd', '# 产品需求文档\n\n分页上限：待确认', null, PENDING),
    ])
    await flushPromises()
    await clickButton(wrapper, CLARIFY_ENTRY_TEXT)
    await flushPromises()

    const tabs = wrapper.findAll('[role="tab"]')
    expect(tabs.map((item) => item.text())).toEqual([
      '需求澄清',
      'PRD 澄清',
      '接口澄清',
      '提示词澄清',
    ])
    // PRD 有正文且正停在这条线上 → 可点；接口 / 提示词还没有正文 → 置灰
    const byLabel = (label: string) => tabs.find((item) => item.text() === label)
    expect((byLabel('PRD 澄清')?.element as HTMLButtonElement).disabled).toBe(false)
    expect((byLabel('接口澄清')?.element as HTMLButtonElement).disabled).toBe(true)
    expect((byLabel('提示词澄清')?.element as HTMLButtonElement).disabled).toBe(true)
  })

  it('快捷流程不显示「需求澄清」那一格：这条路没有需求可聊', async () => {
    mockOpeningByDocument()
    const wrapper = await mountExistingSolution(
      { ...reviewSnapshot('review-prd'), entryMode: 'prd-quick' },
      1,
      [makeDocumentRecord('prd', '# 产品需求文档\n\n分页上限：待确认', null, PENDING)],
    )
    await flushPromises()
    await clickButton(wrapper, CLARIFY_ENTRY_TEXT)
    await flushPromises()

    // 只剩三条文档线程。给一格永远点不亮的「需求澄清」，比不显示更让人困惑
    const tabs = wrapper.findAll('[role="tab"]')
    expect(tabs.map((item) => item.text())).toEqual(['PRD 澄清', '接口澄清', '提示词澄清'])
    expect(storeOf(wrapper).chatTab).toBe('prd-clarify')
  })

  it('对话区只显示当前标签那条线程，点标签即换对话', async () => {
    const wrapper = await openPromptsAndPrdClarification()

    // 当前停在接口澄清上，只看到接口那条线程的对话
    expect(storeOf(wrapper).chatTab).toBe('api-docs-clarify')
    expect(chatMessages(wrapper).map((message) => message.content)).toEqual([
      '接口那边先确认分页',
    ])

    await clickButton(wrapper, 'PRD 澄清')
    await flushPromises()

    // 切过去只看到 PRD 那条；需求澄清一条都没有（它是空的，但也不该串进来别的）
    expect(storeOf(wrapper).chatTab).toBe('prd-clarify')
    expect(chatMessages(wrapper).map((message) => message.content)).toEqual([
      'PRD 那边先确认分页',
    ])
    // 两条线程的消息都在同一张表里，一条没丢
    expect(storeOf(wrapper).messages).toHaveLength(2)
  })

  it('底栏动作随标签切换：需求线程有「重新开始 / 改需求」，澄清线程只有「按澄清结果改写」', async () => {
    const wrapper = await openPromptsAndPrdClarification()

    // 澄清标签：主操作是改写；「重新开始」会连整份会话一起清掉，点错代价太大，不该出现
    let buttons = barButtons(wrapper)
    expect(buttons).toContain('按澄清结果改写')
    expect(buttons).not.toContain('重新开始')
    expect(buttons).not.toContain('改需求')
    expect(buttons).not.toContain('生成 PRD')
    // 澄清标签下也没有「返回审阅」：回审阅页靠顶部进度条（ADR-028 的取舍）
    expect(buttons).not.toContain('返回审阅')

    await clickButton(wrapper, '需求澄清')
    await flushPromises()

    buttons = barButtons(wrapper)
    expect(buttons).toContain('重新开始')
    expect(buttons).toContain('改需求')
    expect(buttons).not.toContain('按澄清结果改写')
  })

  it('「改需求」只在需求线程可用：回到表单后再返回，落在对话页', async () => {
    const wrapper = await openPromptsAndPrdClarification()
    await clickButton(wrapper, '需求澄清')
    await flushPromises()

    await clickButton(wrapper, '改需求')
    await flushPromises()
    expect(storeOf(wrapper).viewState).toBe('form')

    await clickButton(wrapper, '返回对话')
    await flushPromises()
    expect(storeOf(wrapper).viewState).toBe('chatting')
  })

  it('打开一份停在接口澄清的方案：落在对话页，并停回那条标签上', async () => {
    const snapshot = {
      ...reviewSnapshot('chatting'),
      chatTab: 'api-docs-clarify',
      conversationIds: {
        requirement: '',
        'prd-clarify': '',
        'api-docs-clarify': 'conv-api',
        'prompts-clarify': '',
      },
      messages: [
        { role: 'user', content: '需求那边聊的' },
        { role: 'user', content: '接口这边聊的', stage: 'api-docs-clarify' },
      ],
    }
    const wrapper = await mountExistingSolution(snapshot, 1, [
      makeDocumentRecord('prd', '# 产品需求文档'),
      makeDocumentRecord('api-docs', '# 接口文档'),
    ])
    await flushPromises()

    // 标签与各线程会话 id 都随快照回来了：刷新后接着聊，而不是被丢回需求澄清
    expect(storeOf(wrapper).chatTab).toBe('api-docs-clarify')
    expect(storeOf(wrapper).conversationIds['api-docs-clarify']).toBe('conv-api')
    expect(chatMessages(wrapper).map((message) => message.content)).toEqual(['接口这边聊的'])
  })
})

/**
 * 版本历史抽屉的装配。
 *
 * 抽屉本身由 `VersionDrawer` 的用例单独覆盖（纯展示）；这里验证的是**接线**：
 * 入口在审阅页底栏、打开时按当前文档拉版本、切文档自动收起、两个动作把载荷送到后端。
 * 这几处任一接错，用户看到的是「点了没反应」或「抽屉里是另一份文档的版本」。
 */
describe('AppV2 的版本历史抽屉', () => {
  /** 打开一份停在「PRD 待确认」的方案，并备好两份文档 */
  async function openReviewWithDocs(): Promise<VueWrapper> {
    const wrapper = await mountExistingSolution(reviewSnapshot('review-prd'), 1, [
      makeDocumentRecord('prd', '# 产品需求文档'),
      makeDocumentRecord('api-docs', '# 接口文档'),
    ])
    await flushPromises()
    return wrapper
  }

  it('点底栏「版本」打开抽屉，并按当前文档拉取版本列表', async () => {
    vi.mocked(documentApi.fetchVersions).mockResolvedValue([])
    const wrapper = await openReviewWithDocs()

    await clickButton(wrapper, '版本', true)
    await flushPromises()

    expect(documentApi.fetchVersions).toHaveBeenCalledWith(1, 'prd')
    expect(wrapper.find('.shell-version-drawer').classes()).toContain('translate-x-0')
  })

  it('切到另一份文档时自动收起（否则标题与列表会与内容对不上）', async () => {
    vi.mocked(documentApi.fetchVersions).mockResolvedValue([])
    const wrapper = await openReviewWithDocs()

    await clickButton(wrapper, '版本', true)
    await flushPromises()
    await clickButton(wrapper, '查看接口文档')
    await flushPromises()

    expect(wrapper.find('.shell-version-drawer').classes()).toContain('translate-x-full')
  })

  it('「存为新版本」把当前正文与备注送到后端，成功后给一句提示', async () => {
    vi.mocked(documentApi.fetchVersions).mockResolvedValue([])
    vi.mocked(documentApi.checkpointDocument).mockResolvedValue(
      makeDocumentRecord('prd', '# 产品需求文档'),
    )
    const wrapper = await openReviewWithDocs()

    await clickButton(wrapper, '版本', true)
    await flushPromises()
    await wrapper.find('.shell-version-drawer input').setValue('定稿')
    await clickButton(wrapper, '存为新版本')
    await flushPromises()

    expect(documentApi.checkpointDocument).toHaveBeenCalledWith(1, 'prd', '# 产品需求文档', '定稿')
    expect(wrapper.text()).toContain('已存为新版本')
  })

  it('「恢复此版本」调用恢复接口，成功后给一句提示', async () => {
    vi.mocked(documentApi.fetchVersions).mockResolvedValue([
      {
        versionId: 11,
        versionNo: 2,
        source: 'checkpoint',
        note: '',
        contentSha: 'sha',
        contentLength: 10,
        createdAt: '2026-10-16T00:00:00',
        isCurrent: false,
      },
    ])
    vi.mocked(documentApi.restoreVersion).mockResolvedValue(
      makeDocumentRecord('prd', '# 恢复的正文'),
    )
    const wrapper = await openReviewWithDocs()

    await clickButton(wrapper, '版本', true)
    await flushPromises()
    // 恢复要二次确认：先展开，再确认
    await clickButton(wrapper, '恢复此版本')
    await clickButton(wrapper, '确认恢复')
    await flushPromises()

    expect(documentApi.restoreVersion).toHaveBeenCalledWith(1, 'prd', 11)
    expect(wrapper.text()).toContain('已恢复到所选版本')
  })

  it('版本接口失败时闪一条失败提示', async () => {
    vi.mocked(documentApi.fetchVersions).mockRejectedValue(new Error('无法读取版本历史'))
    const wrapper = await openReviewWithDocs()

    await clickButton(wrapper, '版本', true)
    await flushPromises()

    expect(wrapper.text()).toContain('无法读取版本历史')
  })
})

/**
 * 版本抽屉的**预览与删除**接线。
 *
 * 两处容易接错的地方各有用例盯着：
 * - 点条目要真的去取那一版的正文（只改选中态、不发请求 = 点了没反应）；
 * - 删除要先把载荷送到后端、并把结果回灌到列表（删了却还显示着 = 界面在骗人）。
 */
describe('AppV2 的版本预览与删除', () => {
  /** 造一条版本摘要 */
  function versionSummary(overrides: Partial<DocumentVersionSummary> = {}): DocumentVersionSummary {
    return {
      versionId: 11,
      versionNo: 2,
      source: 'checkpoint',
      note: '',
      contentSha: 'sha',
      contentLength: 10,
      createdAt: '2026-10-16T00:00:00',
      isCurrent: false,
      ...overrides,
    }
  }

  /** 造一份版本详情 */
  function versionDetail(overrides: Partial<DocumentVersionDetail> = {}): DocumentVersionDetail {
    return {
      ...versionSummary(),
      content: '# 第二版正文\n\n内容',
      pendingItems: [],
      runSummary: null,
      updatedAt: '2026-10-16T00:00:00',
      ...overrides,
    }
  }

  /** 打开一份停在「PRD 待确认」的方案，并打开抽屉（列表里有一条历史版本） */
  async function openDrawerWithOneVersion(): Promise<VueWrapper> {
    vi.mocked(documentApi.fetchVersions).mockResolvedValue([versionSummary()])
    const wrapper = await mountExistingSolution(reviewSnapshot('review-prd'), 1, [
      makeDocumentRecord('prd', '# 产品需求文档'),
      makeDocumentRecord('api-docs', '# 接口文档'),
    ])
    await flushPromises()
    await clickButton(wrapper, '版本', true)
    await flushPromises()
    return wrapper
  }

  /** 按文案点抽屉里的按钮（不按整页找：侧栏也有「删除」，整页找会点错对象） */
  async function clickInDrawer(wrapper: VueWrapper, text: string): Promise<void> {
    const drawer = wrapper.find('.shell-version-drawer')
    const button = drawer.findAll('button').find((item) => item.text().trim() === text)
    if (!button) throw new Error(`抽屉里未找到按钮：${text}`)
    await button.trigger('click')
  }

  it('点某一版：取那一版的正文并进预览（抽屉加宽 + 渲染出正文）', async () => {
    const wrapper = await openDrawerWithOneVersion()
    vi.mocked(documentApi.fetchVersion).mockResolvedValue(versionDetail())

    await wrapper.find('.shell-version-drawer button[aria-label="预览 v2"]').trigger('click')
    await flushPromises()

    expect(documentApi.fetchVersion).toHaveBeenCalledWith(1, 'prd', 11)
    const drawer = wrapper.find('.shell-version-drawer')
    expect(drawer.classes()).toContain('w-[640px]')
    expect(drawer.find('.prose').text()).toContain('第二版正文')
  })

  it('预览里「恢复此版本」同样要确认，确认后走恢复接口', async () => {
    const wrapper = await openDrawerWithOneVersion()
    vi.mocked(documentApi.fetchVersion).mockResolvedValue(versionDetail())
    vi.mocked(documentApi.restoreVersion).mockResolvedValue(
      makeDocumentRecord('prd', '# 恢复的正文'),
    )
    await wrapper.find('.shell-version-drawer button[aria-label="预览 v2"]').trigger('click')
    await flushPromises()

    await clickInDrawer(wrapper, '恢复此版本')
    await flushPromises()
    expect(documentApi.restoreVersion).not.toHaveBeenCalled()

    await clickInDrawer(wrapper, '确认恢复')
    await flushPromises()

    expect(documentApi.restoreVersion).toHaveBeenCalledWith(1, 'prd', 11)
  })

  it('当前版本没有预览热区：点它不弹预览（正文区里正在看的就是这一版）', async () => {
    vi.mocked(documentApi.fetchVersions).mockResolvedValue([versionSummary({ isCurrent: true })])
    const wrapper = await mountExistingSolution(reviewSnapshot('review-prd'), 1, [
      makeDocumentRecord('prd', '# 产品需求文档'),
    ])
    await flushPromises()
    await clickButton(wrapper, '版本', true)
    await flushPromises()

    expect(wrapper.find('.shell-version-drawer button[aria-label="预览 v2"]').exists()).toBe(false)
    expect(documentApi.fetchVersion).not.toHaveBeenCalled()
  })

  it('删除要先确认，确认后才真删、并闪一句提示', async () => {
    const wrapper = await openDrawerWithOneVersion()
    vi.mocked(documentApi.deleteVersion).mockResolvedValue(undefined)

    // 第一下只是展开确认，不发请求
    await clickInDrawer(wrapper, '删除')
    expect(documentApi.deleteVersion).not.toHaveBeenCalled()

    await clickInDrawer(wrapper, '确认删除')
    await flushPromises()

    expect(documentApi.deleteVersion).toHaveBeenCalledWith(1, 'prd', 11)
    expect(wrapper.text()).toContain('已删除该版本')
  })

  it('删除当前版本被拒时，把后端的说明闪出来', async () => {
    const wrapper = await openDrawerWithOneVersion()
    vi.mocked(documentApi.deleteVersion).mockRejectedValue(
      new Error('当前版本不能删除。想换用别的版本，请点「恢复此版本」'),
    )

    await clickInDrawer(wrapper, '删除')
    await clickInDrawer(wrapper, '确认删除')
    await flushPromises()

    expect(wrapper.text()).toContain('当前版本不能删除')
  })

  it('关抽屉时退出预览：下次打开回到列表', async () => {
    const wrapper = await openDrawerWithOneVersion()
    vi.mocked(documentApi.fetchVersion).mockResolvedValue(versionDetail())
    await wrapper.find('.shell-version-drawer button[aria-label="预览 v2"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('.shell-version-drawer').classes()).toContain('w-[640px]')

    await wrapper.find('.shell-version-drawer button[aria-label="关闭版本历史"]').trigger('click')
    await flushPromises()
    await clickButton(wrapper, '版本', true)
    await flushPromises()

    // 回到列表：窄栏 + 没有 .prose 正文
    expect(wrapper.find('.shell-version-drawer').classes()).toContain('w-[360px]')
    expect(wrapper.find('.shell-version-drawer .prose').exists()).toBe(false)
  })
})
