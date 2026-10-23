import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as documentApi from '../services/documentService'
import * as solutionApi from '../services/solutionService'
import { useDocumentsStore } from '../stores/documents'
import { useRunsStore } from '../stores/runs'
import { SAVE_DEBOUNCE_MS, useSolutionsStore } from '../stores/solutions'
import { useWorkflowStore } from '../stores/workflow'
import type { SolutionSummary } from '../types'
import { toState } from './support/solutionState'

/**
 * 方案 store 测试。
 *
 * 这一层是持久化的**策略中心**，几处约定一旦写错都是「不报错但数据不对」：
 * 1. 「有内容了才建」——只点进入口看一眼不能落库；
 * 2. 首次 `POST` 拿到 id 后走 `PUT`（否则每次都新建，列表里堆一串同名方案）；
 * 3. 生成期间**也保存**——快照里只留任务指针（`activeJobId` / `activeJobKind`），
 *    半截正文不进快照；正因如此，刷新后才有东西可用来静默重连；
 * 4. 保存失败不阻断使用，且要把错误呈现给用户；
 * 5. 标题取产品名，取不到才退默认值。
 *
 * **打开方案会连带拉一次产物正文**（它不在快照里，归后端 `documents` 表），
 * 因此这里也要把文档接口挡掉，否则用例会去打真实网络。
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
  // 对话轮次的读 / 写：`openSolution` 与对话收尾都会走它们（消息的真源是服务端 `chat_turns`），
  // 漏列会让 `openSolution` 在 `fetchTurns` 上抛错、被 catch 吞成「打不开」
  fetchTurns: vi.fn(async () => []),
  clearTurns: vi.fn(async () => {}),
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

/** 造一份摘要 */
function summary(id: number, overrides: Partial<SolutionSummary> = {}): SolutionSummary {
  return {
    id,
    title: `方案 ${id}`,
    mode: 'structured',
    stage: 'form',
    created_at: '2026-10-13T00:00:00',
    updated_at: '2026-10-13T00:00:00',
    // 默认与 `updated_at` 同值：绝大多数用例关心的是别的字段，不必为两根时间轴费心。
    // 「两者不同」这件事由专门的用例构造。
    content_updated_at: '2026-10-13T00:00:00',
    ...overrides,
  }
}

/** 推进防抖计时器并等异步保存落地 */
async function advanceDebounce(ms = SAVE_DEBOUNCE_MS): Promise<void> {
  await vi.advanceTimersByTimeAsync(ms)
}

/**
 * 让本方案有一支在途任务。
 *
 * 任务指针（`activeJobId` / `activeJobKind`）已改成 `stores/runs` 的派生值：从前那种
 * 直接赋值的写法现在会编译不过，也不该再存在——两个字段各存一份就一定会漂移。
 */
function beginRun(jobId: number): void {
  useRunsStore().begin({ jobId, solutionId: 1, jobKind: 'prd', docKind: 'prd' })
}

let workflow: ReturnType<typeof useWorkflowStore>
let solutions: ReturnType<typeof useSolutionsStore>
let documents: ReturnType<typeof useDocumentsStore>

beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()

  setActivePinia(createPinia())
  workflow = useWorkflowStore()
  solutions = useSolutionsStore()
  documents = useDocumentsStore()

  vi.mocked(solutionApi.fetchSolutions).mockResolvedValue([])
  vi.mocked(solutionApi.createSolution).mockResolvedValue(summary(1))
  vi.mocked(solutionApi.updateSolution).mockImplementation(async (id, payload) => ({
    ...summary(id, { title: payload.title, mode: payload.mode, stage: payload.stage }),
    state: payload.state,
  }))
  vi.mocked(solutionApi.deleteSolution).mockResolvedValue(undefined)
  // 默认「这份方案还没有任何文档」
  vi.mocked(documentApi.fetchDocuments).mockResolvedValue([])
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
// 生成期间也保存（持久化的核心目的：留住任务指针）
// ============================================================================

describe('生成期间也保存', () => {
  it('generating-* 阶段的变化照样落库，且把任务指针写进快照', async () => {
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

    // 进入生成态并记下任务指针：这一步必须落库，否则刷新后无从重连
    workflow.viewState = 'generating-prd'
    beginRun(7)
    await advanceDebounce()

    expect(solutionApi.updateSolution).toHaveBeenCalledTimes(1)
    const payload = vi.mocked(solutionApi.updateSolution).mock.calls[0]?.[1]
    // 列表里的进度只认**产物**：此刻三份产物都还没生成出来。
    // 但需求摘要已经提交过，所以停在「AI 对话」那一格，而不是退回「填写需求」——
    // 判据是 `hasChat`（有轮次**或**已提交摘要），消息现在由服务端异步取回，
    // 刚提交完表单时 `messages` 还是空的，只看消息会让进度莫名倒退一格。
    // 若这里变回 `generating-prd`，说明退回成「跟着当前页走」了（回看旧文档就会倒退）。
    expect(payload?.state.view_state).toBe('generating-prd')
    expect(payload?.stage).toBe('chatting')
    // 任务指针随快照一起持久化——这正是「刷新后能静默重连」的落点
    expect(payload?.state.active_job_id).toBe(7)
    expect(payload?.state.active_job_kind).toBe('prd')
  })

  it('生成态 flushPendingSave 立即把任务指针落库而不等防抖窗口', async () => {
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

    // 创建任务后紧接着 flush，把 activeJobId 落库（`useJob.start` 的第二次 flush）
    workflow.viewState = 'generating-prd'
    beginRun(9)
    await solutions.flushPendingSave()

    expect(solutionApi.updateSolution).toHaveBeenCalledTimes(1)
    const payload = vi.mocked(solutionApi.updateSolution).mock.calls[0]?.[1]
    // 指针落在结构化状态列的 `active_job_id` 上（不再是整段 JSON 里的 camelCase）
    expect(payload?.state.active_job_id).toBe(9)
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
    await advanceDebounce()
    vi.mocked(solutionApi.updateSolution).mockClear()

    // 生成结束：清掉任务指针并切到 review 态。产物正文此时已经拿到了，
    // 因此列表里的进度应当前进到「已出 PRD」——它算的是**产物**，不是当前页。
    documents.setLocal('prd', '# 订单系统 PRD')
    // 生成结束：任务收尾（指针随之失效），再切到 review 态
    useRunsStore().clear()
    workflow.viewState = 'review-prd'
    await advanceDebounce()

    expect(solutionApi.updateSolution).toHaveBeenCalledTimes(1)
    const payload = vi.mocked(solutionApi.updateSolution).mock.calls[0]?.[1]
    expect(payload?.stage).toBe('review-prd')
    // 任务已终态，指针不再留在快照里
    expect(payload?.state.active_job_id).toBeNull()
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

  it('防抖保存还在途时又 flush：不会并发创建出第二份方案', async () => {
    // 让首次创建「卡住」——这正是 `POST` 返回前 `currentId` 仍为 null 的窗口
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

    // 防抖到点：创建请求已经发出，但还没回来
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
    expect(solutionApi.createSolution).toHaveBeenCalledTimes(1)

    // 此刻调用方（点跳转 / 开新方案）要求立即落库，flush 撞上在途请求
    const flushed = solutions.flushPendingSave()
    await vi.advanceTimersByTimeAsync(0)

    // 关键：在途期间绝不能并发再发一个 POST，否则库里会出现两份同样的方案
    expect(solutionApi.createSolution).toHaveBeenCalledTimes(1)

    release?.()
    await flushed

    expect(solutionApi.createSolution).toHaveBeenCalledTimes(1)
    expect(solutions.currentId).toBe(1)
    expect(solutions.isPersisted).toBe(true)
    expect(solutions.items).toHaveLength(1)
  })

  it('保存请求在途时删掉方案：回来的响应不会把方案插回列表', async () => {
    // 先把方案建出来（id=1）
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

    // 让这次 PUT「卡住」——用户在请求在途时点了删除
    let release: (() => void) | undefined
    vi.mocked(solutionApi.updateSolution).mockImplementation(
      (id, payload) =>
        new Promise((resolve) => {
          release = () => resolve({ ...summary(id), state: payload.state })
        }),
    )

    workflow.quickInput.requirements = '改一句'
    await advanceDebounce()
    expect(solutionApi.updateSolution).toHaveBeenCalledTimes(1)

    await solutions.remove(1)
    expect(solutions.currentId).toBeNull()

    release?.()
    await vi.runAllTimersAsync()

    // 关键：删掉的方案不会被凭空插回列表（否则点开就是 404 的幽灵条目）
    expect(solutions.items.some((item) => item.id === 1)).toBe(false)
    expect(solutions.currentId).toBeNull()
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
  it('拉详情、校验快照并回填 workflow，同时按方案 id 拉回产物正文', async () => {
    vi.mocked(solutionApi.fetchSolution).mockResolvedValue({
      ...summary(8),
      state: toState({
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
      }),
    })
    // 正文不在快照里，要单独从后端取
    vi.mocked(documentApi.fetchDocuments).mockResolvedValue([
      {
        kind: 'prd',
        content: '# PRD',
        runSummary: null,
        pendingItems: [],
        updatedAt: '',
        stale: false,
        derivedFrom: null,
      },
    ])

    const opened = await solutions.openSolution(8)

    expect(opened).toBe(true)
    expect(solutions.currentId).toBe(8)
    expect(workflow.viewState).toBe('review-prd')
    expect(documentApi.fetchDocuments).toHaveBeenCalledWith(8)
    expect(documents.contents.prd).toBe('# PRD')
  })

  it('历史快照没有 entryMode 时：照常恢复，编辑后仍走 PUT（不被空壳闸拦住）', async () => {
    // 老版本快照没有 `entryMode` 字段，恢复出来就是 null。这是**合法状态**：
    // 用户手上明明有一份已经存在的方案（`currentId` 非空），不能被「空壳方案」那道闸误伤。
    // 闸门只装在创建分支上，本用例就是它的反证。
    vi.mocked(solutionApi.fetchSolution).mockResolvedValue({
      ...summary(9),
      state: toState({
        viewState: 'review-prd',
        summary: {
          product_name: '订单系统',
          product_goal: '收单',
          target_users: [],
          platform: '网页应用',
          mvp_features: [],
        },
        messages: [],
        conversationId: '',
      }),
    })

    const opened = await solutions.openSolution(9)

    expect(opened).toBe(true)
    expect(workflow.entryMode).toBeNull()

    // 编辑一下，让自动保存跑起来
    workflow.quickInput.requirements = '改一句'
    await advanceDebounce()

    expect(solutionApi.createSolution).not.toHaveBeenCalled()
    // 顺带钉住「进度」这一列在 `entryMode === null` 时的回落口径：直通 `viewState`。
    // （原先由「还没选入口就落库」那条用例顺带覆盖，创建分支被闸住后只剩这条路径。）
    expect(solutionApi.updateSolution).toHaveBeenCalledWith(
      9,
      expect.objectContaining({ stage: 'review-prd' }),
    )
    expect(solutions.currentId).toBe(9)
  })

  it('快照落在生成中时降级到待确认状态', async () => {
    vi.mocked(solutionApi.fetchSolution).mockResolvedValue({
      ...summary(8),
      state: toState({
        viewState: 'generating-prd',
        entryMode: 'structured',
        summary: null,
        messages: [],
        conversationId: '',
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

  it('快照字段脏时逐项退回默认值，整份状态照样恢复（不再整份作废）', async () => {
    // ⚠️ 语义已变：状态从「一整段 JSON」变成结构化列之后，**整份快照损坏已经不可能发生**——
    // `fromStatePayload` 永远产出对象（缺的字段按默认值补齐），`normalizeSessionSnapshot`
    // 只在入参不是对象时才返回 null（见两处 docstring：「脏数据只会丢弃对应字段，
    // 不会让整份状态失效」）。因此 `openSolution` 里那个『记录已损坏』的分支走不到了，
    // 这里钉住取而代之的口径：脏字段逐个丢弃并回落到安全默认值，方案仍可打开。
    vi.mocked(solutionApi.fetchSolution).mockResolvedValue({
      ...summary(8),
      state: {
        ...toState({}),
        view_state: '不是阶段',
        chat_tab: '不是线程',
        entry_mode: '不是入口',
        active_job_id: 'x' as never,
        summary: 'not-an-object' as never,
      },
    })

    const opened = await solutions.openSolution(8)

    expect(opened).toBe(true)
    expect(solutions.currentId).toBe(8)
    // 每个脏字段都退到默认值，而不是把整份方案判死
    expect(workflow.viewState).toBe('entry')
    expect(workflow.chatTab).toBe('requirement')
    expect(workflow.entryMode).toBeNull()
    expect(workflow.activeJobId).toBeNull()
    expect(workflow.summary).toBeNull()
    expect(solutions.saveError).toBe('')
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
      state: toState({
        viewState: 'form',
        entryMode: 'structured',
        summary: null,
        messages: [],
        conversationId: '',
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
      state: toState({
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

// ============================================================================
// 列表里的「进度」（stage）：只认产物，不跟随当前页
//
// 列表回答的是「这份**做到哪了**」，不是「你上次翻到哪一页」。判据分开之后有两个好处：
// 回看旧文档不会让进度倒退；切一下阶段也不会重写一次列表（用户看不到进度变化，列表却在动）。
// ============================================================================

describe('列表里的进度（stage）', () => {
  /** 让这份方案三份产物齐全（进度推到最远），并保证它已经存在（走 PUT 而不是 POST） */
  async function setupWithAllDocs(): Promise<void> {
    workflow.chooseEntry('structured')
    workflow.summary = {
      product_name: '订单系统',
      product_goal: '收单',
      target_users: [],
      platform: '网页应用',
      mvp_features: [],
    }
    await advanceDebounce()

    documents.setLocal('prd', '# PRD')
    documents.setLocal('api-docs', '# 接口文档')
    documents.setLocal('prompts', '# 提示词')
    workflow.viewState = 'review-prompts'
    await advanceDebounce()
  }

  /** 最近一次 PUT 的载荷 */
  function lastPayload() {
    const calls = vi.mocked(solutionApi.updateSolution).mock.calls
    return calls[calls.length - 1]?.[1]
  }

  it('回看旧文档不会让进度倒退', async () => {
    await setupWithAllDocs()
    expect(lastPayload()?.stage).toBe('review-prompts')

    // 点回 PRD 回看：当前页确实回到了 review-prd，但产物一件没少
    workflow.viewState = 'review-prd'
    await advanceDebounce()

    expect(lastPayload()?.stage).toBe('review-prompts')
  })

  it('「已完成」不会被「已出提示词」顶掉', async () => {
    await setupWithAllDocs()

    workflow.viewState = 'done'
    await advanceDebounce()

    expect(lastPayload()?.stage).toBe('done')
  })

  it('还没选入口时**不再**落库：那是空壳方案的形状，不该建出「未命名方案」', async () => {
    // 未选入口却有内容，真实流程里只可能来自「reset() 之后又被迟到的回调填回内容」。
    // 从前这里会建一份带默认标题的方案；现在它是「删除方案后冒出幽灵方案」的那个入口，
    // 由 `saveNow` 创建分支的 `entryMode === null` 守卫挡掉（回归用例见
    // `orphan-solution.spec.ts`）。因此 `progressStage` 的「未选入口直通 viewState」
    // 那条分支也不再可能被保存动作走到了。
    workflow.debugPrdText = '# 别人给的 PRD'
    expect(workflow.entryMode).toBeNull()
    expect(workflow.viewState).toBe('entry')

    await advanceDebounce()

    expect(solutionApi.createSolution).not.toHaveBeenCalled()
  })

  it('更新已有方案时原位刷新，不会跳到列表最前', async () => {
    vi.mocked(solutionApi.fetchSolutions).mockResolvedValue([summary(5), summary(1)])
    await solutions.loadList()
    vi.mocked(solutionApi.fetchSolution).mockResolvedValue({
      ...summary(1),
      state: toState({
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
      }),
    })

    // 打开列表里排在第二位的方案 1
    expect(await solutions.openSolution(1)).toBe(true)
    expect(solutions.items.map((item) => item.id)).toEqual([5, 1])

    // 改点东西触发一次自动保存（走 PUT）
    workflow.viewState = 'review-prd'
    await advanceDebounce()

    expect(solutionApi.updateSolution).toHaveBeenCalled()
    // 原位刷新：它还在第二位，没有被提到最前（那正是「侧栏自己跳一下」的来源）
    expect(solutions.items.map((item) => item.id)).toEqual([5, 1])
  })
})

// ============================================================================
// 内容变更判定：`content_changed`
//
// 后端不解析快照（见 `solution_repo` 的约定），分不出「翻了一页」与「改了内容」，
// 所以这个标志由前端算、随载荷提交。算错的两个方向都有代价：
// 多报 → 列表时间永远「刚刚」；少报 → 真改了内容时间却不动。
// ============================================================================

describe('内容变更判定（content_changed）', () => {
  /** 造一份已落库的方案，并清掉建方案那次的调用记录 */
  async function persistOnce(): Promise<void> {
    workflow.chooseEntry('structured')
    workflow.debugPrdText = '# 贴进来的 PRD'
    await advanceDebounce()

    expect(solutionApi.createSolution).toHaveBeenCalledTimes(1)
    vi.mocked(solutionApi.updateSolution).mockClear()
  }

  /** 最近一次 PUT 的载荷 */
  function sentPayload() {
    const calls = vi.mocked(solutionApi.updateSolution).mock.calls
    return calls[calls.length - 1]?.[1]
  }

  it('只切阶段：报「不是内容变更」（内容时间不该被翻页推动）', async () => {
    await persistOnce()

    workflow.viewState = 'review-prd'
    await advanceDebounce()

    expect(sentPayload()?.content_changed).toBe(false)
  })

  it('改了内容：报「是内容变更」', async () => {
    await persistOnce()

    workflow.debugPrdText = '# 又改了一版'
    await advanceDebounce()

    expect(sentPayload()?.content_changed).toBe(true)
  })

  it('写入产物正文：即使快照一字未动，也会触发保存并报内容变更', async () => {
    await persistOnce()
    vi.mocked(solutionApi.updateSolution).mockClear()

    // 生成 / 手改 / 版本操作都走这条信号：正文住在后端表里，快照那条计数看不到它们。
    // 没有这条路径，「生成完 PRD，列表时间还停在填表单那一刻」就会是常态。
    documents.markContentWritten()
    await advanceDebounce()

    expect(solutionApi.updateSolution).toHaveBeenCalledTimes(1)
    expect(sentPayload()?.content_changed).toBe(true)
  })

  it('连续两次都是纯导航：第二次也报「不是内容变更」（基准已跟着走）', async () => {
    await persistOnce()

    workflow.viewState = 'review-prd'
    await advanceDebounce()
    vi.mocked(solutionApi.updateSolution).mockClear()

    workflow.viewState = 'review-api-docs'
    await advanceDebounce()

    expect(sentPayload()?.content_changed).toBe(false)
  })

  it('保存失败不推进基准：下一次保存仍然报「内容变了」', async () => {
    await persistOnce()

    workflow.debugPrdText = '# 关键的一版'
    vi.mocked(solutionApi.updateSolution).mockRejectedValueOnce(new Error('网络不可用'))
    await advanceDebounce()

    expect(solutionApi.updateSolution).toHaveBeenCalledTimes(1)
    vi.mocked(solutionApi.updateSolution).mockClear()

    // 再触发一次保存（模拟下一次变更 / flush）。上一次没写进库，这一次必须**仍然**报
    // 内容变更——否则 `content_updated_at` 就永久丢掉了那次改动，而且是静默的。
    documents.markContentWritten()
    await advanceDebounce()

    expect(sentPayload()?.content_changed).toBe(true)
  })

  it('打开方案不报内容变更（读回来的内容不是「刚刚发生」的改动）', async () => {
    await persistOnce()
    vi.mocked(solutionApi.fetchSolution).mockResolvedValue({
      ...summary(1),
      state: toState({
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
      }),
    })

    expect(await solutions.openSolution(1)).toBe(true)
    vi.mocked(solutionApi.updateSolution).mockClear()

    // 打开本身不该保存（`restoreTick` 那条守卫）；再切一次阶段触发保存，
    // 这一次必须报「不是内容变更」——否则读完一份旧方案，它的内容时间会被顶到现在，
    // 正好是「打开方案不该改内容时间」的反面。
    workflow.viewState = 'chatting'
    await advanceDebounce()

    expect(sentPayload()?.content_changed).toBe(false)
  })
})
