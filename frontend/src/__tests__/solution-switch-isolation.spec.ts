import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as documentApi from '../services/documentService'
import * as solutionApi from '../services/solutionService'
import { isSubscribed, register, resetForTests } from '../services/runSubscriptions'
import { useRunsStore } from '../stores/runs'
import { useSolutionsStore } from '../stores/solutions'
import { useWorkflowStore } from '../stores/workflow'
import type { JobSnapshot, SolutionSummary } from '../types'
import { toState } from './support/solutionState'

/**
 * 切方案时的运行态隔离。
 *
 * 这是「运行态去全局化」（ADR-032）里最容易写歪的一环：任务与连接解耦之后，
 * 切走**不能取消**服务端任务（那会让用户在别处点一下侧栏就白跑一次生成），
 * 但**必须**退订本地的流并清掉单槽运行态。少做任何一半都有具体症状：
 *
 * - 只退订不清运行态 → 新方案一打开就显示「生成中」，还带着上一份方案的正文；
 * - 只清运行态不退订 → 旧任务的 SSE 事件继续写进新方案的预览（串稿）；
 * - 两个都不做 → 「在 A 里点生成，切到 B 看到 A 的章节往里长」。
 *
 * 另一半是「回来还能接上」：切回原方案时凭快照里的指针重建运行态并重连**同一个** jobId，
 * 而且「写哪一份」要以服务端给的 `doc_kind` 为准，不能靠阶段反推。
 *
 * 两份约定都不难写对，难在**不会报错**——写错了界面只是「看起来没动静」或者「文案不对」，
 * 因此这里逐条钉住：订阅被 abort、运行态为空、旧事件写不进来、回来时身份还原。
 */

vi.mock('../services/solutionService', () => ({
  SolutionNotFoundError: class SolutionNotFoundError extends Error {
    constructor() {
      super('方案不存在或已被删除')
      this.name = 'SolutionNotFoundError'
    }
  },
  fetchSolutions: vi.fn(),
  fetchSolution: vi.fn(),
  createSolution: vi.fn(),
  updateSolution: vi.fn(),
  deleteSolution: vi.fn(),
}))

vi.mock('../services/documentService', () => ({
  DocumentsNotFoundError: class DocumentsNotFoundError extends Error {
    constructor() {
      super('方案不存在或已被删除')
      this.name = 'DocumentsNotFoundError'
    }
  },
  fetchDocuments: vi.fn(),
  saveDocument: vi.fn(),
}))

/** 造一份方案摘要 */
function summary(id: number): SolutionSummary {
  return {
    id,
    title: `方案 ${id}`,
    mode: 'structured',
    stage: 'form',
    created_at: '2026-10-13T00:00:00',
    updated_at: '2026-10-13T00:00:00',
    content_updated_at: '2026-10-13T00:00:00',
  }
}

/**
 * 造一份会话快照 JSON。
 *
 * 只写解析器认得的字段：`normalizeSessionSnapshot` 对缺字段一律兜底，多写反而容易
 * 在字段改名后悄悄失效（用例还绿着，语义已经不对了）。
 */
function snapshot(overrides: Record<string, unknown> = {}): string {
  return JSON.stringify({
    viewState: 'review-prd',
    entryMode: 'structured',
    summary: null,
    messages: [],
    conversationId: '',
    ...overrides,
  })
}

/** 造一份服务端任务快照（重连时后端先发这一帧，`docKind` 是它的权威结论） */
function jobSnapshot(overrides: Partial<JobSnapshot> = {}): JobSnapshot {
  return {
    jobId: 42,
    kind: 'prd',
    docKind: 'prd',
    status: 'running',
    phase: 'writing',
    finalizedText: '',
    liveBuffer: '',
    stageText: '',
    completedSections: [],
    solutionId: 1,
    errorCode: '',
    errorMessage: '',
    pendingItems: [],
    partial: false,
    failedSections: [],
    elapsedMs: 0,
    ...overrides,
  }
}

let workflow: ReturnType<typeof useWorkflowStore>
let solutions: ReturnType<typeof useSolutionsStore>
let runs: ReturnType<typeof useRunsStore>

beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()
  // 订阅登记表是模块级单例：跨用例残留会让「是否已退订」这类断言失去意义
  resetForTests()

  setActivePinia(createPinia())
  workflow = useWorkflowStore()
  solutions = useSolutionsStore()
  runs = useRunsStore()

  vi.mocked(solutionApi.fetchSolutions).mockResolvedValue([])
  vi.mocked(solutionApi.deleteSolution).mockResolvedValue(undefined)
  vi.mocked(documentApi.fetchDocuments).mockResolvedValue([])
})

afterEach(() => {
  vi.useRealTimers()
})

/** 让当前方案处于「有一支任务在跑」的状态，并挂一路真实的订阅登记 */
function startRun(jobId = 42): AbortController {
  runs.begin({ jobId, solutionId: 1, jobKind: 'prd', docKind: 'prd' })
  runs.preview.content = '# 订单系统 PRD\n\n## 一、背景'
  const controller = new AbortController()
  register(jobId, controller)
  return controller
}

// ============================================================================
// 切走：退订 + 清运行态
// ============================================================================

describe('切方案时退订在途任务', () => {
  it('打开另一份方案：订阅被中止、运行态清空、预览不留上一份正文', async () => {
    solutions.currentId = 1
    const controller = startRun(42)
    vi.mocked(solutionApi.fetchSolution).mockResolvedValue({ ...summary(2), state: toState(JSON.parse(snapshot())) })

    await solutions.openSolution(2)

    expect(controller.signal.aborted).toBe(true)
    expect(isSubscribed(42)).toBe(false)
    expect(runs.activeRun).toBeNull()
    expect(runs.hasRunning).toBe(false)
    expect(runs.preview.content).toBe('')
  })

  it('切走之后旧任务的事件写不进新方案（按 jobId 认领，认不到就丢）', async () => {
    solutions.currentId = 1
    startRun(42)
    vi.mocked(solutionApi.fetchSolution).mockResolvedValue({ ...summary(2), state: toState(JSON.parse(snapshot())) })
    await solutions.openSolution(2)

    // 旧任务收尾前又推来一帧快照 / 一次收尾：身份已经不在，必须原样丢弃
    runs.applySnapshot(42, jobSnapshot({ liveBuffer: '## 二、功能清单' }))
    expect(runs.isRunning(42)).toBe(false)
    expect(runs.settle(42)).toBe(false)
    expect(runs.preview.content).toBe('')
  })

  it('开新方案同样退订（旧方案的任务留在后端，回来时再连）', async () => {
    solutions.currentId = 1
    const controller = startRun(42)

    await solutions.startNew()

    expect(controller.signal.aborted).toBe(true)
    expect(runs.activeRun).toBeNull()
    expect(runs.preview.content).toBe('')
    expect(solutions.currentId).toBeNull()
  })

  it('删掉当前方案同样退订', async () => {
    solutions.currentId = 1
    const controller = startRun(42)

    await solutions.remove(1)

    expect(controller.signal.aborted).toBe(true)
    expect(runs.activeRun).toBeNull()
    expect(runs.preview.content).toBe('')
  })

  it('删的是别的方案时不动当前运行态（不能把正在跑的流一起掐掉）', async () => {
    solutions.currentId = 1
    const controller = startRun(42)

    await solutions.remove(9)

    expect(controller.signal.aborted).toBe(false)
    expect(runs.isRunning(42)).toBe(true)
    expect(runs.preview.content).not.toBe('')
  })
})

// ============================================================================
// 回来：凭指针重建运行态
// ============================================================================

describe('回到原方案时重建运行态', () => {
  it('快照里记着指针：同一 jobId、产物种类与生成态都还原', async () => {
    vi.mocked(solutionApi.fetchSolution).mockResolvedValue({
      ...summary(1),
      state: toState(JSON.parse(snapshot({
        viewState: 'generating-api-docs',
        activeJobId: 42,
        activeJobKind: 'api-docs',
        activeJobDocKind: 'api-docs',
      }))),
    })

    await solutions.openSolution(1)

    // 有指针时**不降级**：任务还在后端跑，界面就该停在生成态等它的 snapshot
    expect(workflow.viewState).toBe('generating-api-docs')
    expect(workflow.downgrade).toBeNull()
    expect(runs.activeRun?.jobId).toBe(42)
    expect(runs.pointer).toEqual({ jobId: 42, jobKind: 'api-docs', docKind: 'api-docs' })
    expect(runs.isRunningFor('api-docs')).toBe(true)
  })

  it('旧快照没有产物种类时先留空，首个服务端快照到达后以它为准', async () => {
    // 旧版本前端只在快照里存了 jobId + kind，`optimize` 这类动作凭 kind 推不出写哪一份
    vi.mocked(solutionApi.fetchSolution).mockResolvedValue({
      ...summary(1),
      state: toState(JSON.parse(snapshot({ viewState: 'review-prompts', activeJobId: 42, activeJobKind: 'optimize' }))),
    })

    await solutions.openSolution(1)

    expect(runs.pointer).toEqual({ jobId: 42, jobKind: 'optimize', docKind: null })

    // 后端在 `snapshot` 帧里给出权威结论：拿它校正，而不是按阶段反推一个默认值
    runs.applySnapshot(42, jobSnapshot({ kind: 'optimize', docKind: 'prompts' }))

    expect(runs.pointer?.docKind).toBe('prompts')
    expect(runs.isRunningFor('prompts')).toBe(true)
  })

  it('切回来这件事本身不会清掉刚重建的那一支（退订在恢复之前）', async () => {
    // 本标签页里这支任务原本是订阅着的（A → B → A 的最后一跳）
    const previous = startRun(42)
    vi.mocked(solutionApi.fetchSolution).mockResolvedValue({
      ...summary(1),
      state: toState(JSON.parse(snapshot({
        viewState: 'generating-prd',
        activeJobId: 42,
        activeJobKind: 'prd',
        activeJobDocKind: 'prd',
      }))),
    })

    await solutions.openSolution(1)

    // 旧订阅必须摘掉（否则重新订阅时两路 SSE 会往同一份预览里交错写）
    expect(previous.signal.aborted).toBe(true)
    expect(isSubscribed(42)).toBe(false)
    // 但**刚重建的运行态**必须还在：顺序若反过来（先恢复再退订），这一支会被一起摘掉，
    // 表现是「切回来以后再也不重连」——比切走时不退订更难查
    expect(runs.activeRun?.jobId).toBe(42)
  })
})
