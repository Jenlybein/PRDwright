import { flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useDocumentPipeline } from '@/composables/useDocumentPipeline'
import * as api from '@/services/api'
import * as documentApi from '@/services/documentService'
import * as solutionApi from '@/services/solutionService'
import { useDocumentsStore } from '@/stores/documents'
import { useSolutionsStore } from '@/stores/solutions'
import { useWorkflowStore } from '@/stores/workflow'
import type { SolutionSummary } from '@/types'
import { toState } from './support/solutionState'

/**
 * 导航与生成的交互边界。
 *
 * 这一版拆掉了「忙则不许跳转」那道闸门（见 `stores/runs.ts` 顶部的复盘）。闸门一拆，
 * 就必须逐条把**原来被它顺带挡住**的东西补到它该在的地方，否则会从「点不动」变成「乱写」：
 *
 * 1. 重入守卫——连点两次不能起两个任务（闸门从前顺手挡住了）；
 * 2. 收尾落点——用户已经走了就别再把他拽回去（D1），但正文该写照写；
 * 3. 预览缓冲——跳出去看一眼再回来，还得看得到刚才流到哪了。
 *
 * 这些规则错了都不会报错：要么静默丢一次点击，要么把内容写进别人正在看的页面。
 */

vi.mock('@/services/api', () => ({
  createJob: vi.fn(),
  readJobStream: vi.fn(),
  syncSummaryFromConversation: vi.fn(),
  continueConversationStream: vi.fn(),
  startConversationStream: vi.fn(),
}))

vi.mock('@/services/solutionService', () => ({
  SolutionNotFoundError: class SolutionNotFoundError extends Error {
    constructor() {
      super('方案不存在或已被删除')
      this.name = 'SolutionNotFoundError'
    }
  },
  fetchSolutions: vi.fn(async () => []),
  fetchSolution: vi.fn(),
  createSolution: vi.fn(),
  updateSolution: vi.fn(),
  deleteSolution: vi.fn(),
}))

vi.mock('@/services/documentService', () => ({
  DocumentsNotFoundError: class DocumentsNotFoundError extends Error {
    constructor() {
      super('方案不存在或已被删除')
      this.name = 'DocumentsNotFoundError'
    }
  },
  fetchDocuments: vi.fn(async () => []),
  saveDocument: vi.fn(async () => undefined),
  fetchDeliveryZip: vi.fn(),
}))

/** 造一份方案摘要 */
function summary(id: number): SolutionSummary {
  return {
    id,
    title: '订单系统',
    mode: 'structured',
    stage: 'form',
    created_at: '2026-10-21T00:00:00',
    updated_at: '2026-10-21T00:00:00',
    content_updated_at: '2026-10-21T00:00:00',
  }
}

/** 一份能让「生成 PRD」通过前置校验的结构化摘要 */
const FULL_SUMMARY = {
  product_name: '订单系统',
  product_goal: '收单',
  target_users: [],
  platform: '网页应用',
  mvp_features: [],
}

let workflow: ReturnType<typeof useWorkflowStore>
let solutions: ReturnType<typeof useSolutionsStore>
let documents: ReturnType<typeof useDocumentsStore>
let notices: string[]

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  notices = []
  workflow = useWorkflowStore()
  solutions = useSolutionsStore()
  documents = useDocumentsStore()

  vi.mocked(api.createJob).mockResolvedValue({ jobId: 12, status: 'running' })
  vi.mocked(api.readJobStream).mockResolvedValue({ conversationId: '', reply: '', asks: [] })
  vi.mocked(solutionApi.createSolution).mockResolvedValue(summary(1))
  vi.mocked(solutionApi.updateSolution).mockImplementation(async (id, payload) => ({
    ...summary(id),
    title: payload.title,
    stage: payload.stage,
    // 详情比摘要多一个 `data`（会话快照）；这里只关心 id，给个空对象即可
    state: toState({}),
  }))
})

/** 把方案建出来并停在某个审阅页 */
async function reachReview(state: 'review-prd' | 'review-api-docs'): Promise<void> {
  workflow.chooseEntry('structured')
  workflow.summary = FULL_SUMMARY
  await solutions.flushPendingSave()
  workflow.goTo(state)
}

describe('收尾落点：不抢方向盘（D1）', () => {
  it('optimize 期间可以走去澄清页，收尾留在原地但正文已更新', async () => {
    await reachReview('review-api-docs')
    documents.contents['api-docs'] = '# 接口文档'

    let release!: () => void
    vi.mocked(api.readJobStream).mockImplementation(async () => {
      await new Promise<void>((resolve) => {
        release = resolve
      })
      return { conversationId: '', reply: '# 优化后的接口文档', asks: [] }
    })

    const pipeline = useDocumentPipeline({ onNotice: (text) => notices.push(text) })
    const optimizing = pipeline.onAiOptimize({ content: '# 接口文档', instruction: '更简洁' })
    await flushPromises()
    expect(workflow.hasActiveRun).toBe(true)

    // 「前往澄清」→ 对话页。从前这里被 busy 静默吞掉：点了没反应
    const material = pipeline.startDocumentClarification('api-docs')
    expect(material).not.toBeNull()
    expect(workflow.viewState).toBe('chatting')

    release()
    await optimizing
    await flushPromises()

    // 优化本来就不切阶段，用户又已经走了：留在对话页，正文照写
    expect(workflow.viewState).toBe('chatting')
    expect(documents.contents['api-docs']).toBe('# 优化后的接口文档')
    // 优化不收尾跳转，因此也不该冒出「已生成完毕」的浮层
    expect(notices).toEqual([])
  })

  it('生成途中走去看别的文档：收尾不拽回去，只提示，正文照写', async () => {
    await reachReview('review-prd')
    documents.contents.prd = '# 已有 PRD'

    let release!: () => void
    vi.mocked(api.readJobStream).mockImplementation(async (_jobId, handlers) => {
      handlers?.onChunk?.('# 接口文档\n')
      await new Promise<void>((resolve) => {
        release = resolve
      })
      return { conversationId: '', reply: '# 接口文档\n\n## 一、概述', asks: [] }
    })

    const pipeline = useDocumentPipeline({ onNotice: (text) => notices.push(text) })
    const generating = pipeline.handleGenerateApiDocs()
    await flushPromises()
    expect(workflow.viewState).toBe('generating-api-docs')

    // 用户点回 PRD 的审阅页去看老文档
    pipeline.goTo('review-prd')
    expect(workflow.viewState).toBe('review-prd')

    release()
    await generating
    await flushPromises()

    // 不拽人、给提示、正文已写到该写的那一份
    expect(workflow.viewState).toBe('review-prd')
    expect(notices).toEqual(['接口文档已生成完毕，可在进度条上查看'])
    expect(documents.contents['api-docs']).toBe('# 接口文档\n\n## 一、概述')
    // 用户眼前那一份没被顺带改写
    expect(documents.contents.prd).toBe('# 已有 PRD')
  })

  it('停留在生成页时照旧进入审阅页（这不是「抢方向盘」，正是他在等的）', async () => {
    await reachReview('review-prd')
    documents.contents.prd = '# 已有 PRD'
    vi.mocked(api.readJobStream).mockResolvedValue({
      conversationId: '',
      reply: '# 接口文档\n\n## 一、概述',
      asks: [],
    })

    const pipeline = useDocumentPipeline({ onNotice: (text) => notices.push(text) })
    await pipeline.handleGenerateApiDocs()
    await flushPromises()

    expect(workflow.viewState).toBe('review-api-docs')
    expect(notices).toEqual([])
  })
})

describe('重入守卫（闸门拆掉后必须显式补上）', () => {
  it('任务在飞时再点一次生成，不会再起第二个任务', async () => {
    await reachReview('review-prd')
    documents.contents.prd = '# 已有 PRD'

    let release!: () => void
    vi.mocked(api.readJobStream).mockImplementation(async () => {
      await new Promise<void>((resolve) => {
        release = resolve
      })
      return { conversationId: '', reply: '', asks: [] }
    })

    const pipeline = useDocumentPipeline({ onNotice: () => {} })
    const first = pipeline.handleGenerateApiDocs()
    await flushPromises()
    expect(api.createJob).toHaveBeenCalledTimes(1)

    // 第二次点：直接返回，不创建任务
    await pipeline.handleGenerateApiDocs()
    expect(api.createJob).toHaveBeenCalledTimes(1)

    release()
    await first
    await flushPromises()

    // 收尾之后又能正常发起
    vi.mocked(api.readJobStream).mockResolvedValue({ conversationId: '', reply: '', asks: [] })
    await pipeline.handleGenerateApiDocs()
    expect(api.createJob).toHaveBeenCalledTimes(2)
  })
})

describe('跳转不清预览', () => {
  it('生成途中去别的页面再回来，已流出的正文还在', async () => {
    await reachReview('review-prd')
    documents.contents.prd = '# 已有 PRD'

    let release!: () => void
    vi.mocked(api.readJobStream).mockImplementation(async (_jobId, handlers) => {
      handlers?.onChunk?.('# 接口文档\n')
      await new Promise<void>((resolve) => {
        release = resolve
      })
      return { conversationId: '', reply: '# 接口文档\n', asks: [] }
    })

    const pipeline = useDocumentPipeline({ onNotice: () => {} })
    const generating = pipeline.handleGenerateApiDocs()
    await flushPromises()
    expect(pipeline.docStreamingContent.value).toBe('# 接口文档\n')

    // 离开这一页再回来：预览是「这次生成的进度」，不该被导航清掉
    pipeline.goTo('review-prd')
    expect(pipeline.docStreamingContent.value).toBe('# 接口文档\n')
    pipeline.goTo('generating-api-docs')
    expect(pipeline.docStreamingContent.value).toBe('# 接口文档\n')

    release()
    await generating
    await flushPromises()
  })
})
