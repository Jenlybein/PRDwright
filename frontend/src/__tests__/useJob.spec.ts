import { flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useJob } from '@/composables/useJob'
import * as api from '@/services/api'
import type { StreamHandlers } from '@/services/api'
import { ApiError } from '@/services/errors'
import * as solutionApi from '@/services/solutionService'
import { useWorkflowStore } from '@/stores/workflow'
import type { JobSnapshot, RunSummary } from '@/types'

/**
 * 生成任务生命周期编排测试。
 *
 * 这一层把「一次生成」从「一次浏览器请求」升级为「一个后端任务」，四件事必须钉死：
 * 1. `start` **创建任务前后各 flush 一次**——前一次让任务带上 `solution_id`，
 *    后一次把任务指针写进后端快照（刷新后能重连，全押在这一步）；
 * 2. 事件回调（含 `snapshot` / `run_summary`）要原样透传给调用方，并顺手记录运行态；
 * 3. `detach` **只退订、不取消服务端任务**——这正是「生成与连接解耦」的落点；
 * 4. 已用时由运行态统一维护：**每次快照都用服务端的值重新锚定**（刷新重连后不归零），
 *    收尾时停在权威总耗时上。
 */

vi.mock('@/services/api', () => ({
  createJob: vi.fn(),
  readJobStream: vi.fn(),
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
  createSolution: vi.fn(async () => ({
    id: 1,
    title: '测试方案',
    mode: 'structured',
    stage: 'form',
    created_at: '',
    updated_at: '',
  })),
  updateSolution: vi.fn(async (id: number, payload: Record<string, unknown>) => ({
    id,
    title: String(payload.title ?? ''),
    mode: String(payload.mode ?? ''),
    stage: String(payload.stage ?? ''),
    created_at: '',
    updated_at: '',
    data: payload.data,
  })),
  deleteSolution: vi.fn(async () => undefined),
}))

/** 造一份任务快照 */
function snapshot(overrides: Partial<JobSnapshot> = {}): JobSnapshot {
  return {
    jobId: 12,
    kind: 'prd',
    status: 'running',
    phase: 'writing',
    finalizedText: '',
    liveBuffer: '',
    completedSections: [],
    solutionId: 1,
    errorCode: '',
    errorMessage: '',
    elapsedMs: 0,
    pendingItems: [],
    ...overrides,
  }
}

/** 造一份用量汇总（`run_summary` 事件的 data） */
function runSummary(overrides: Partial<RunSummary> = {}): RunSummary {
  return {
    kind: 'prd',
    status: 'completed',
    durationMs: 250,
    totalCalls: 3,
    failedCalls: 0,
    promptTokens: 100,
    completionTokens: 50,
    steps: [],
    injectedSkills: [],
    warnings: [],
    provider: 'ark',
    model: 'doubao-seed',
    requestId: 'req-1',
    ...overrides,
  }
}

/** 造一个 `AbortError`（jsdom 的 DOMException 不一定是 Error 子类，自己造更稳） */
function abortError(): Error {
  const error = new Error('已中止订阅')
  error.name = 'AbortError'
  return error
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  vi.mocked(api.createJob).mockResolvedValue({ jobId: 12, status: 'running' })
  vi.mocked(api.readJobStream).mockResolvedValue({ conversationId: '', reply: '', asks: [] })
  vi.mocked(solutionApi.createSolution).mockResolvedValue({
    id: 1,
    title: '测试方案',
    mode: 'structured',
    stage: 'form',
    created_at: '',
    updated_at: '',
    content_updated_at: '',
  })
})

describe('useJob.start', () => {
  it('先 flush 拿到方案 id、再创建任务，随后记下指针、切生成态并订阅', async () => {
    const workflow = useWorkflowStore()
    // 有内容，方案才会在第一次 flush 时被建出来、拿到 id
    workflow.summary = {
      product_name: '订单系统',
      product_goal: '收单',
      target_users: [],
      platform: '网页应用',
      mvp_features: [],
    }

    const job = useJob()
    const result = await job.start('prd', { requirements: '一段需求' }, {})

    // 任务带上了方案 id（第一次 flush 已经把它建出来了）
    expect(api.createJob).toHaveBeenCalledWith({
      kind: 'prd',
      solutionId: 1,
      requirements: '一段需求',
    })
    // 前一次 flush 保证有 solution_id，后一次把任务指针写进快照
    expect(solutionApi.createSolution).toHaveBeenCalledTimes(1)
    expect(solutionApi.updateSolution).toHaveBeenCalledTimes(1)
    // 第二次 update 的载荷里带着任务指针
    const savedData = String(vi.mocked(solutionApi.updateSolution).mock.calls[0]?.[1].data ?? '')
    expect(savedData).toContain('"activeJobId":12')

    expect(api.readJobStream).toHaveBeenCalledWith(12, expect.anything(), expect.anything())
    expect(workflow.activeJobId).toBe(12)
    expect(workflow.activeJobKind).toBe('prd')
    expect(workflow.viewState).toBe('generating-prd')
    // 订阅结束后不再处于「生成中」
    expect(workflow.docGenerating).toBe(false)
    expect(result).toEqual({ reply: '', ok: true })
  })

  it('把 snapshot / run_summary 记录到运行态，并原样透传给调用方的 handlers', async () => {
    const snap = snapshot({ phase: 'reviewing', finalizedText: '# 正文\n' })
    vi.mocked(api.readJobStream).mockImplementation(async (_jobId, handlers) => {
      handlers?.onSnapshot?.(snap)
      handlers?.onChunk?.('增量')
      handlers?.onRunSummary?.(runSummary())
      return { conversationId: '', reply: '# 正文\n增量', asks: [] }
    })

    const seen: string[] = []
    const handlers: StreamHandlers = {
      onChunk: (text) => seen.push(`chunk:${text}`),
      onSnapshot: (value) => seen.push(`snapshot:${value.finalizedText}`),
    }

    const job = useJob()
    const result = await job.start('prd', { requirements: 'x' }, handlers)

    // 透传：调用方拿得到所有事件
    expect(seen).toEqual(['snapshot:# 正文\n', 'chunk:增量'])
    // 记录：快照、阶段、收尾摘要
    expect(job.snapshot.value).toEqual(snap)
    expect(job.phase.value).toBe('reviewing')
    expect(job.runSummary.value).toEqual(runSummary())
    expect(result).toEqual({ reply: '# 正文\n增量', ok: true })
  })

  it('已用时：快照用服务端的值锚定，收尾停在权威总耗时上', async () => {
    const job = useJob()
    const seen: number[] = []
    vi.mocked(api.readJobStream).mockImplementation(async (_jobId, handlers) => {
      // 订阅一开始后端就发快照：此时本地计时器才刚起（0），必须被服务端值锚定
      handlers?.onSnapshot?.(snapshot({ elapsedMs: 30_000 }))
      seen.push(job.elapsedMs.value)
      handlers?.onRunSummary?.(runSummary({ durationMs: 45_000 }))
      seen.push(job.elapsedMs.value)
      return { conversationId: '', reply: '', asks: [] }
    })

    await job.start('prd', { requirements: 'x' }, {})

    // 快照把已用时从 0 锚到 30 秒，收尾摘要把总耗时定在 45 秒
    expect(seen).toEqual([30_000, 45_000])
    expect(job.elapsedMs.value).toBe(45_000)
  })

  it('重新开始一轮生成时，已用时归零重算（不延续上一轮）', async () => {
    const job = useJob()
    vi.mocked(api.readJobStream).mockImplementation(async (_jobId, handlers) => {
      handlers?.onRunSummary?.(runSummary({ durationMs: 9_000 }))
      return { conversationId: '', reply: '', asks: [] }
    })

    await job.start('prd', { requirements: 'x' }, {})
    expect(job.elapsedMs.value).toBe(9_000)

    const duringSecond: number[] = []
    vi.mocked(api.readJobStream).mockImplementation(async (_jobId, handlers) => {
      duringSecond.push(job.elapsedMs.value)
      handlers?.onSnapshot?.(snapshot({ elapsedMs: 1_000 }))
      return { conversationId: '', reply: '', asks: [] }
    })

    await job.start('prd', { requirements: 'x' }, {})

    // 第二轮起步时是 0（不是上一轮的 9 秒），随后被这一轮的快照锚定
    expect(duringSecond).toEqual([0])
    expect(job.elapsedMs.value).toBe(1_000)
  })

  it('重连会清掉上一轮的运行摘要（避免把旧用量当成本次生成）', async () => {
    const job = useJob()
    vi.mocked(api.readJobStream).mockImplementation(async (_jobId, handlers) => {
      handlers?.onRunSummary?.(runSummary({ durationMs: 1_000 }))
      return { conversationId: '', reply: '', asks: [] }
    })
    await job.start('prd', { requirements: 'x' }, {})
    expect(job.runSummary.value).not.toBeNull()

    const duringReconnect: unknown[] = []
    vi.mocked(api.readJobStream).mockImplementation(async (_jobId, handlers) => {
      duringReconnect.push(job.runSummary.value)
      handlers?.onRunSummary?.(runSummary({ durationMs: 30_000, requestId: 'req-second' }))
      return { conversationId: '', reply: '', asks: [] }
    })

    await job.reconnect(12, {})

    expect(duringReconnect).toEqual([null])
    expect(job.runSummary.value?.requestId).toBe('req-second')
  })

  it('optimize 就地流式：不改变当前阶段', async () => {
    const workflow = useWorkflowStore()
    workflow.viewState = 'review-prd'

    const job = useJob()
    await job.start('optimize', { document: '# 原文', instruction: '更简洁' }, {})

    expect(workflow.viewState).toBe('review-prd')
    expect(workflow.activeJobKind).toBe('optimize')
    // optimize 没有对应的「生成中」阶段，进度条不呼吸
    expect(workflow.generating).toBe(false)
  })

  it('订阅失败时把错误与错误码记进运行态，并返回 ok:false', async () => {
    vi.mocked(api.readJobStream).mockRejectedValue(
      new ApiError('上游连接中断', 'LLM_UPSTREAM_ERROR'),
    )

    const job = useJob()
    const result = await job.start('prd', { requirements: 'x' }, {})

    expect(result.ok).toBe(false)
    expect(result.errorCode).toBe('LLM_UPSTREAM_ERROR')
    expect(job.error.value).toBe('上游连接中断')
  })
})

describe('useJob.reconnect', () => {
  it('只订阅既有任务、不创建新任务，并把快照交给上层', async () => {
    const snap = snapshot({ jobId: 5, status: 'running', liveBuffer: '半句' })
    vi.mocked(api.readJobStream).mockImplementation(async (_jobId, handlers) => {
      handlers?.onSnapshot?.(snap)
      return { conversationId: '', reply: '半句续写', asks: [] }
    })

    const job = useJob()
    const result = await job.reconnect(5, {})

    expect(api.createJob).not.toHaveBeenCalled()
    expect(api.readJobStream).toHaveBeenCalledWith(5, expect.anything(), expect.anything())
    expect(job.snapshot.value).toEqual(snap)
    expect(result).toEqual({ reply: '半句续写', ok: true })
  })
})

describe('useJob.detach', () => {
  it('只退订本地读取，不取消服务端任务', async () => {
    let receivedSignal: AbortSignal | undefined
    vi.mocked(api.readJobStream).mockImplementation((_jobId, _handlers, signal) => {
      receivedSignal = signal
      return new Promise((_resolve, reject) => {
        signal?.addEventListener('abort', () => reject(abortError()))
      })
    })

    const workflow = useWorkflowStore()
    const job = useJob()
    const pending = job.start('prd', { requirements: 'x' }, {})
    // 让 createJob / flush 走完，订阅进入在途状态
    await flushPromises()
    expect(api.readJobStream).toHaveBeenCalledTimes(1)
    expect(receivedSignal?.aborted).toBe(false)

    job.detach()

    const result = await pending
    // 中止只影响这次订阅：返回 ok:false，但不算错误
    expect(result.ok).toBe(false)
    expect(job.error.value).toBe('')
    expect(job.running.value).toBe(false)
    expect(workflow.docGenerating).toBe(false)
    // 服务端任务没有被取消——本期没有 cancel 端点，只创建了一次任务
    expect(api.createJob).toHaveBeenCalledTimes(1)
    expect(receivedSignal?.aborted).toBe(true)
  })
})
