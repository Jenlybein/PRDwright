import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as solutionApi from '../services/solutionService'
import { SAVE_DEBOUNCE_MS, useSolutionsStore } from '../stores/solutions'
import { useWorkflowStore } from '../stores/workflow'
import type { SolutionSummary } from '../types'

/**
 * 方案 store 测试。
 *
 * 这一层是持久化的**策略中心**，几处约定一旦写错都是「不报错但数据不对」：
 * 1. 「有内容了才建」——只点进入口看一眼不能落库；
 * 2. 首次 `POST` 拿到 id 后走 `PUT`（否则每次都新建，列表里堆一串同名方案）；
 * 3. 生成期间不保存（半成品 + 高频），进入 `review-*` 才落；
 * 4. 保存失败不阻断使用，且要把错误呈现给用户；
 * 5. 标题取产品名，取不到才退默认值。
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

/** 造一份摘要 */
function summary(id: number, overrides: Partial<SolutionSummary> = {}): SolutionSummary {
  return {
    id,
    title: `方案 ${id}`,
    mode: 'structured',
    stage: 'form',
    created_at: '2026-10-13T00:00:00',
    updated_at: '2026-10-13T00:00:00',
    ...overrides,
  }
}

/** 推进防抖计时器并等异步保存落地 */
async function advanceDebounce(ms = SAVE_DEBOUNCE_MS): Promise<void> {
  await vi.advanceTimersByTimeAsync(ms)
}

let workflow: ReturnType<typeof useWorkflowStore>
let solutions: ReturnType<typeof useSolutionsStore>

beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()

  setActivePinia(createPinia())
  workflow = useWorkflowStore()
  solutions = useSolutionsStore()

  vi.mocked(solutionApi.fetchSolutions).mockResolvedValue([])
  vi.mocked(solutionApi.createSolution).mockResolvedValue(summary(1))
  vi.mocked(solutionApi.updateSolution).mockImplementation(async (id, payload) => ({
    ...summary(id, { title: payload.title, mode: payload.mode, stage: payload.stage }),
    data: payload.data,
  }))
  vi.mocked(solutionApi.deleteSolution).mockResolvedValue(undefined)
})

afterEach(() => {
  vi.useRealTimers()
})

// ============================================================================
// 「有内容了才建」
// ============================================================================

describe('有内容了才建', () => {
  it('只是切了阶段、没有内容时不创建', async () => {
    // 走到表单页，但一个字都没填
    workflow.chooseEntry('structured')
    await advanceDebounce()

    expect(solutionApi.createSolution).not.toHaveBeenCalled()
  })

  it('填了产品名之后才创建，并把 id 登记下来', async () => {
    workflow.chooseEntry('structured')
    workflow.summary = {
      product_name: '订单系统',
      product_goal: '收单',
      target_users: [],
      platform: '网页应用',
      mvp_features: [],
    }
    await advanceDebounce()

    expect(solutionApi.createSolution).toHaveBeenCalledTimes(1)
    expect(solutions.currentId).toBe(1)
    expect(solutions.isPersisted).toBe(true)
  })

  it('已在新建态且无内容时，即使有变更也不创建', async () => {
    workflow.goTo('entry')
    await advanceDebounce()

    expect(solutionApi.createSolution).not.toHaveBeenCalled()
  })
})

// ============================================================================
// 创建 / 更新两条路径
// ============================================================================

describe('创建与更新两条路径', () => {
  it('首次保存 POST，之后一律 PUT（不重复新建）', async () => {
    workflow.chooseEntry('structured')
    workflow.summary = {
      product_name: '订单系统',
      product_goal: '收单',
      target_users: [],
      platform: '网页应用',
      mvp_features: [],
    }
    await advanceDebounce()

    workflow.quickInput.requirements = '再改一句'
    await advanceDebounce()

    expect(solutionApi.createSolution).toHaveBeenCalledTimes(1)
    expect(solutionApi.updateSolution).toHaveBeenCalledTimes(1)
    expect(vi.mocked(solutionApi.updateSolution).mock.calls[0]?.[0]).toBe(1)
  })

  it('创建成功后回调通知（用于把地址换成 /v2/{id}）', async () => {
    const onCreated = vi.fn()
    solutions.setCreatedHandler(onCreated)

    workflow.chooseEntry('structured')
    workflow.summary = {
      product_name: '订单系统',
      product_goal: '收单',
      target_users: [],
      platform: '网页应用',
      mvp_features: [],
    }
    await advanceDebounce()

    expect(onCreated).toHaveBeenCalledWith(1)
  })

  it('方案在别处被删（PUT 返回 null）：清掉 id、移出列表并给出提示', async () => {
    vi.mocked(solutionApi.updateSolution).mockResolvedValue(null)

    workflow.chooseEntry('structured')
    workflow.summary = {
      product_name: '订单系统',
      product_goal: '收单',
      target_users: [],
      platform: '网页应用',
      mvp_features: [],
    }
    await advanceDebounce()
    expect(solutions.currentId).toBe(1)

    workflow.quickInput.requirements = '改一句'
    await advanceDebounce()

    expect(solutions.currentId).toBeNull()
    expect(solutions.saveError).toContain('已被删除')
  })
})

// ============================================================================
// 生成期间不保存
// ============================================================================

describe('生成期间不保存', () => {
  it('generating-* 阶段的变化不落库', async () => {
    workflow.chooseEntry('structured')
    workflow.summary = {
      product_name: '订单系统',
      product_goal: '收单',
      target_users: [],
      platform: '网页应用',
      mvp_features: [],
    }
    await advanceDebounce()
    vi.mocked(solutionApi.updateSolution).mockClear()

    workflow.viewState = 'generating-prd'
    workflow.prdContent = '# 半成品'
    await advanceDebounce()

    expect(solutionApi.updateSolution).not.toHaveBeenCalled()
  })

  it('生成结束进入 review 态时补上这次保存', async () => {
    workflow.chooseEntry('structured')
    workflow.summary = {
      product_name: '订单系统',
      product_goal: '收单',
      target_users: [],
      platform: '网页应用',
      mvp_features: [],
    }
    await advanceDebounce()

    workflow.viewState = 'generating-prd'
    workflow.prdContent = '# 半成品'
    await advanceDebounce()
    vi.mocked(solutionApi.updateSolution).mockClear()

    workflow.viewState = 'review-prd'
    workflow.prdContent = '# 完整 PRD'
    await advanceDebounce()

    expect(solutionApi.updateSolution).toHaveBeenCalledTimes(1)
    const payload = vi.mocked(solutionApi.updateSolution).mock.calls[0]?.[1]
    expect(payload?.stage).toBe('review-prd')
    expect(payload?.data).toContain('完整 PRD')
  })
})

// ============================================================================
// 标题
// ============================================================================

describe('标题规则', () => {
  it('优先取结构化摘要里的产品名', async () => {
    workflow.chooseEntry('structured')
    workflow.summary = {
      product_name: '订单系统',
      product_goal: '收单',
      target_users: [],
      platform: '网页应用',
      mvp_features: [],
    }
    await advanceDebounce()

    expect(vi.mocked(solutionApi.createSolution).mock.calls[0]?.[0].title).toBe('订单系统')
  })

  it('没有摘要时退回快捷入口的产品名', async () => {
    workflow.chooseEntry('prd-quick')
    workflow.quickInput.productName = '小账本'
    workflow.quickInput.requirements = '记账'
    await advanceDebounce()

    expect(vi.mocked(solutionApi.createSolution).mock.calls[0]?.[0].title).toBe('小账本')
  })

  it('只有粘贴的 PRD 时取首个一级标题', async () => {
    workflow.chooseEntry('prompt-debug')
    workflow.debugPrdText = '# 库存中台需求\n\n正文'
    await advanceDebounce()

    expect(vi.mocked(solutionApi.createSolution).mock.calls[0]?.[0].title).toBe('库存中台需求')
  })

  it('什么都取不到时用带时间的默认标题', async () => {
    workflow.chooseEntry('prompt-debug')
    workflow.debugPrdText = '没有标题的一段话'
    await advanceDebounce()

    expect(vi.mocked(solutionApi.createSolution).mock.calls[0]?.[0].title).toContain('未命名方案')
  })
})

// ============================================================================
// 保存失败与并发
// ============================================================================

describe('保存失败与并发', () => {
  it('保存失败不丢内容：只记错误，内存里的内容还在', async () => {
    vi.mocked(solutionApi.createSolution).mockRejectedValue(new Error('后端不可用'))

    workflow.chooseEntry('structured')
    workflow.summary = {
      product_name: '订单系统',
      product_goal: '收单',
      target_users: [],
      platform: '网页应用',
      mvp_features: [],
    }
    await advanceDebounce()

    expect(solutions.saveError).toBe('后端不可用')
    expect(workflow.summary?.product_name).toBe('订单系统')
    expect(workflow.hasContent).toBe(true)
  })

  it('保存成功会清掉上一次的错误提示', async () => {
    vi.mocked(solutionApi.createSolution).mockRejectedValueOnce(new Error('抖了一下'))

    workflow.chooseEntry('structured')
    workflow.summary = {
      product_name: '订单系统',
      product_goal: '收单',
      target_users: [],
      platform: '网页应用',
      mvp_features: [],
    }
    await advanceDebounce()
    expect(solutions.saveError).toBe('抖了一下')

    workflow.quickInput.requirements = '再改'
    await advanceDebounce()
    expect(solutions.saveError).toBe('')
  })

  it('在途期间的变更会在请求回来后补存一次，避免交错写入', async () => {
    // 让第一次保存「卡住」，期间再改一次
    let release: (() => void) | undefined
    vi.mocked(solutionApi.createSolution).mockImplementation(
      () =>
        new Promise((resolve) => {
          release = () => resolve(summary(1))
        }),
    )

    workflow.chooseEntry('structured')
    workflow.summary = {
      product_name: '订单系统',
      product_goal: '收单',
      target_users: [],
      platform: '网页应用',
      mvp_features: [],
    }
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
    expect(solutionApi.createSolution).toHaveBeenCalledTimes(1)

    // 第一次还没回来，期间又改了一次
    workflow.quickInput.requirements = '改一句'
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
    // 在途期间不该并发再发一个请求
    expect(solutionApi.createSolution).toHaveBeenCalledTimes(1)

    release?.()
    await vi.runAllTimersAsync()

    // 第一次回来后补存了一次（此时已有 id，走 PUT）
    expect(solutionApi.updateSolution).toHaveBeenCalledTimes(1)
  })
})

// ============================================================================
// 列表
// ============================================================================

describe('列表维护', () => {
  it('loadList 拉取列表并标记已加载', async () => {
    vi.mocked(solutionApi.fetchSolutions).mockResolvedValue([summary(2), summary(1)])

    await solutions.loadList()

    expect(solutions.items.map((item) => item.id)).toEqual([2, 1])
    expect(solutions.loaded).toBe(true)
  })

  it('最新保存的方案会被提到列表最前', async () => {
    vi.mocked(solutionApi.fetchSolutions).mockResolvedValue([summary(5), summary(1)])
    await solutions.loadList()

    workflow.chooseEntry('structured')
    workflow.summary = {
      product_name: '订单系统',
      product_goal: '收单',
      target_users: [],
      platform: '网页应用',
      mvp_features: [],
    }
    await advanceDebounce()

    expect(solutions.items[0]?.id).toBe(1)
  })

  it('删除当前打开的方案后回到新建态并清空内容', async () => {
    workflow.chooseEntry('structured')
    workflow.summary = {
      product_name: '订单系统',
      product_goal: '收单',
      target_users: [],
      platform: '网页应用',
      mvp_features: [],
    }
    await advanceDebounce()

    await solutions.remove(1)

    expect(solutionApi.deleteSolution).toHaveBeenCalledWith(1)
    expect(solutions.currentId).toBeNull()
    expect(workflow.hasContent).toBe(false)
  })
})

// ============================================================================
// 打开已有方案
// ============================================================================

describe('openSolution', () => {
  it('拉详情、校验快照并回填 workflow', async () => {
    vi.mocked(solutionApi.fetchSolution).mockResolvedValue({
      ...summary(8),
      data: JSON.stringify({
        viewState: 'review-prd',
        entryMode: 'structured',
        summary: {
          product_name: '订单系统',
          product_goal: '收单',
          target_users: [],
          platform: '网页应用',
          mvp_features: [],
        },
        messages: [],
        conversationId: '',
        prdContent: '# PRD',
        apiDocsContent: '',
        promptsContent: '',
      }),
    })

    const opened = await solutions.openSolution(8)

    expect(opened).toBe(true)
    expect(solutions.currentId).toBe(8)
    expect(workflow.viewState).toBe('review-prd')
    expect(workflow.prdContent).toBe('# PRD')
  })

  it('快照落在生成中时降级到待确认状态', async () => {
    vi.mocked(solutionApi.fetchSolution).mockResolvedValue({
      ...summary(8),
      data: JSON.stringify({
        viewState: 'generating-prd',
        entryMode: 'structured',
        summary: null,
        messages: [],
        conversationId: '',
        prdContent: '',
        apiDocsContent: '',
        promptsContent: '',
      }),
    })

    await solutions.openSolution(8)

    expect(workflow.viewState).toBe('review-prd')
    expect(workflow.downgrade).toBe('generating')
  })

  it('方案不存在（404）时清 id 并通知路由退回新建页', async () => {
    const onOpened = vi.fn()
    solutions.setOpenedHandler(onOpened)
    vi.mocked(solutionApi.fetchSolution).mockRejectedValue(new solutionApi.SolutionNotFoundError())

    const opened = await solutions.openSolution(8)

    expect(opened).toBe(false)
    expect(solutions.currentId).toBeNull()
    expect(onOpened).toHaveBeenCalledWith(null)
  })

  it('快照内容损坏时不清空当前状态，只标记错误', async () => {
    vi.mocked(solutionApi.fetchSolution).mockResolvedValue({ ...summary(8), data: '不是 JSON' })

    const opened = await solutions.openSolution(8)

    expect(opened).toBe(false)
    expect(solutions.saveError).toContain('损坏')
  })

  it('打开另一份方案前，先把当前压着的改动落库', async () => {
    // 先在当前新建态里改一笔（防抖窗口还没到）
    workflow.chooseEntry('structured')
    workflow.summary = {
      product_name: '订单系统',
      product_goal: '收单',
      target_users: [],
      platform: '网页应用',
      mvp_features: [],
    }

    // 紧接着打开另一份方案——不先 flush 的话这笔改动会被 applySnapshot 抹掉
    vi.mocked(solutionApi.fetchSolution).mockResolvedValue({
      ...summary(8),
      data: JSON.stringify({
        viewState: 'form',
        entryMode: 'structured',
        summary: null,
        messages: [],
        conversationId: '',
        prdContent: '',
        apiDocsContent: '',
        promptsContent: '',
      }),
    })
    await solutions.openSolution(8)

    expect(solutionApi.createSolution).toHaveBeenCalledTimes(1)
    expect(solutions.hasPendingSave()).toBe(false)
    expect(solutions.currentId).toBe(8)
  })

  it('打开方案本身不触发保存——刚读回来的内容原样存回去纯属白跑', async () => {
    vi.mocked(solutionApi.fetchSolution).mockResolvedValue({
      ...summary(8),
      data: JSON.stringify({
        viewState: 'form',
        entryMode: 'structured',
        summary: {
          product_name: '订单系统',
          product_goal: '收单',
          target_users: [],
          platform: '网页应用',
          mvp_features: [],
        },
        messages: [],
        conversationId: '',
        prdContent: '',
        apiDocsContent: '',
        promptsContent: '',
      }),
    })

    await solutions.openSolution(8)
    await advanceDebounce()

    expect(solutionApi.createSolution).not.toHaveBeenCalled()
    expect(solutionApi.updateSolution).not.toHaveBeenCalled()
    expect(solutions.hasPendingSave()).toBe(false)
  })
})

// ============================================================================
// 新建 / 刷新
// ============================================================================

describe('startNew 与 flushPendingSave', () => {
  it('startNew 清空内容并把 id 归零，但不删旧方案', async () => {
    vi.mocked(solutionApi.fetchSolutions).mockResolvedValue([summary(1)])
    await solutions.loadList()

    workflow.chooseEntry('structured')
    workflow.summary = {
      product_name: '订单系统',
      product_goal: '收单',
      target_users: [],
      platform: '网页应用',
      mvp_features: [],
    }
    await advanceDebounce()

    await solutions.startNew()

    expect(solutions.currentId).toBeNull()
    expect(workflow.hasContent).toBe(false)
    // 旧方案仍在列表里——用户的意图是「再写一份」，不是「扔掉上一份」
    expect(solutions.items.map((item) => item.id)).toContain(1)
    expect(solutionApi.deleteSolution).not.toHaveBeenCalled()
  })

  it('flushPendingSave 立即落库而不等防抖窗口', async () => {
    workflow.chooseEntry('structured')
    workflow.summary = {
      product_name: '订单系统',
      product_goal: '收单',
      target_users: [],
      platform: '网页应用',
      mvp_features: [],
    }

    // 还没到防抖时间就 flush
    await solutions.flushPendingSave()

    expect(solutionApi.createSolution).toHaveBeenCalledTimes(1)
    expect(solutions.hasPendingSave()).toBe(false)
  })
})
