import { flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useClarificationChat } from '@/composables/useClarificationChat'
import { useDocumentPipeline } from '@/composables/useDocumentPipeline'
import * as api from '@/services/api'
import * as documentApi from '@/services/documentService'
import * as solutionApi from '@/services/solutionService'
import { useDocumentsStore } from '@/stores/documents'
import { useRunsStore } from '@/stores/runs'
import { useSolutionsStore } from '@/stores/solutions'
import { useWorkflowStore } from '@/stores/workflow'
import type { SolutionSummary } from '@/types'

/**
 * 回归：**删除当前方案之后，迟到的生成回调不能再造出一份「未命名方案」**。
 *
 * 线上的形状：用户在大模型调用进行中删掉了当前方案 → 调用结束 → 列表里多出一份
 * 「未命名方案 · MM-DD HH:mm」，刷新仍在（库里真的有这一行）。
 *
 * 两个环节各负一半责任，本文件同时钉住：
 * 1. `useDocumentPipeline` 的迟到回调必须被丢弃（会话代次守卫）——否则刚被 `reset()`
 *    清空的 `messages` 又被填满，`hasContent` 重新为真；
 * 2. `solutions.saveNow()` 的创建分支必须有最后一道闸（`entryMode === null` 不创建）——
 *    因为「有内容」这件事可能来自别的迟到路径（对话流把回复 push 回 `messages`），
 *    只靠第 1 点守不全。
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
    created_at: '2026-10-20T00:00:00',
    updated_at: '2026-10-20T00:00:00',
    content_updated_at: '2026-10-20T00:00:00',
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

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()

  workflow = useWorkflowStore()
  solutions = useSolutionsStore()
  documents = useDocumentsStore()

  vi.mocked(api.createJob).mockResolvedValue({ jobId: 12, status: 'running' })
  vi.mocked(api.readJobStream).mockResolvedValue({ conversationId: '', reply: '', asks: [] })
  vi.mocked(solutionApi.createSolution).mockResolvedValue(summary(1))
  vi.mocked(solutionApi.updateSolution).mockImplementation(async (id, payload) => ({
    ...summary(id),
    title: payload.title,
    mode: payload.mode,
    stage: payload.stage,
    data: payload.data,
  }))
  vi.mocked(solutionApi.deleteSolution).mockResolvedValue(undefined)
})

describe('删除方案后迟到的回调', () => {
  it('流回调与落稿全被丢弃，且不会新建出第二份方案', async () => {
    // 1. 先把当前方案建出来（POST 一次，拿到 id=1）
    workflow.chooseEntry('structured')
    workflow.summary = FULL_SUMMARY
    await solutions.flushPendingSave()
    expect(solutionApi.createSolution).toHaveBeenCalledTimes(1)
    expect(solutions.currentId).toBe(1)

    // 2. 发起一次生成，并让订阅「卡住」——模拟调用还在途中
    const pipeline = useDocumentPipeline({ onNotice: () => {} })
    let releaseStream: (() => void) | undefined
    vi.mocked(api.readJobStream).mockImplementation(async (_jobId, handlers) => {
      await new Promise<void>((resolve) => {
        releaseStream = resolve
      })
      // 事件在「删除之后」才到：这些回调必须被丢弃
      handlers?.onChunk?.('迟到的正文')
      return { conversationId: '', reply: '# 迟到的 PRD', asks: [] }
    })

    const generating = pipeline.generatePrd()
    await flushPromises()
    expect(api.readJobStream).toHaveBeenCalledTimes(1)
    // 生成态已置位，且任务指针已落库（真实链路的形状）
    expect(workflow.viewState).toBe('generating-prd')

    // 3. 用户在调用进行中删除了当前方案
    vi.mocked(solutionApi.createSolution).mockClear()
    await solutions.remove(1)

    expect(solutions.currentId).toBeNull()
    expect(workflow.hasContent).toBe(false)

    // 4. 迟到的流回来了
    releaseStream?.()
    await generating
    await flushPromises()

    // 回调没写进本文件的状态：预览、正文、内容计数都还是空的
    expect(pipeline.docStreamingContent.value).toBe('')
    expect(documents.contents.prd).toBe('')
    expect(documents.contentWriteTick).toBe(0)
    expect(workflow.viewState).not.toBe('review-prd')

    // 5. 另一条迟到的写入路径：对话流把 assistant 回复 push 回 messages。
    //    此时 `hasContent` 重新为真——这正是线上那个 Bug 的形状。
    workflow.messages.push({ role: 'assistant', content: '迟到的回复', stage: 'requirement' })
    documents.markContentWritten()
    expect(workflow.hasContent).toBe(true)

    await solutions.flushPendingSave()

    // 关键断言：没有新建出第二份方案（列表里不会冒出「未命名方案 · MM-DD HH:mm」）
    expect(solutionApi.createSolution).not.toHaveBeenCalled()
    expect(solutions.currentId).toBeNull()
    expect(solutions.items.some((item) => item.id !== 1)).toBe(false)
  })

  it('没删方案时：生成完成后正文落进文档并进入审阅页（守卫不误伤正常收尾）', async () => {
    // 反向回归：`finishDoc` / 各条 `await` 之后都加了 `if (jobExpired()) return`，
    // 正常会话里代次不变，这些守卫必须全部放行——否则「生成完了界面还是空的」。
    workflow.chooseEntry('structured')
    workflow.summary = FULL_SUMMARY
    await solutions.flushPendingSave()
    expect(solutions.currentId).toBe(1)

    const pipeline = useDocumentPipeline({ onNotice: () => {} })
    vi.mocked(api.readJobStream).mockResolvedValue({
      conversationId: '',
      reply: '# 正常 PRD',
      asks: [],
    })

    await pipeline.generatePrd()
    await flushPromises()

    expect(documents.contents.prd).toBe('# 正常 PRD')
    expect(documents.contentWriteTick).toBeGreaterThan(0)
    expect(workflow.viewState).toBe('review-prd')
  })

  it('迟到的流事件不再写进预览缓冲（onChunk 链路），正常流不受影响', async () => {
    // 这里刻意走**接口文档**这条链路：prd 的 handlers 只挂 onPart / onStage，
    // 而 `onChunk` 只存在于非 prd 的 handlers 上——它是「迟到的增量文本」最直接的入口，
    // 也是 `guardJobHandlers` 唯一有可观测副作用的回调（见 useDocumentPipeline.ts）。
    workflow.chooseEntry('structured')
    workflow.summary = FULL_SUMMARY
    await solutions.flushPendingSave()
    expect(solutions.currentId).toBe(1)

    // 接口文档以 PRD 正文为输入
    documents.contents.prd = '# 已有 PRD'

    const pipeline = useDocumentPipeline({ onNotice: () => {} })
    let releaseStream: (() => void) | undefined
    let streamingWhileLive = ''
    vi.mocked(api.readJobStream).mockImplementation(async (_jobId, handlers) => {
      // 正常流：事件应当照常落进预览缓冲（守卫不能误伤）
      handlers?.onChunk?.('接口正文')
      streamingWhileLive = pipeline.docStreamingContent.value
      await new Promise<void>((resolve) => {
        releaseStream = resolve
      })
      // 删除之后才到的事件：必须被丢弃
      handlers?.onChunk?.('迟到的接口正文')
      return { conversationId: '', reply: '# 迟到的接口文档', asks: [] }
    })

    const generating = pipeline.handleGenerateApiDocs()
    await flushPromises()
    expect(api.readJobStream).toHaveBeenCalledTimes(1)
    // 反向断言（同一条链路）：没删方案时 chunk 必须真的写进缓冲
    expect(streamingWhileLive).toBe('接口正文')

    await solutions.remove(1)
    releaseStream?.()
    await generating
    await flushPromises()

    // 迟到的 chunk 与整稿都没写进来。
    // 不断言整段为 `''`：删除前那一段合法流入的缓冲留在本 composable 实例里，
    // 由下一次 `goTo` 清空——这里要钉的是「删除之后到达的事件一个字节都没进来」。
    expect(pipeline.docStreamingContent.value).not.toContain('迟到的')
    expect(documents.contents['api-docs']).toBe('')
    expect(documents.contentWriteTick).toBe(0)
    expect(workflow.viewState).not.toBe('review-api-docs')
  })

  it('方案 A 迟到的事件不会串进后启动的方案 B（代次必须按任务各自持有）', async () => {
    // 缺陷形状：代次若用一个**共享的「当前任务代次」**，B 启动时会把它覆盖成新会话的值；
    // 之后 A 的迟到事件拿这个共享值自检恰好相等，于是被误判成「没过期」——
    // A 的增量写进 B 的预览缓冲，A 的正文写进 B 的文档（内容不丢不报错，只是串了方案）。
    workflow.chooseEntry('structured')
    workflow.summary = FULL_SUMMARY
    await solutions.flushPendingSave()
    expect(solutions.currentId).toBe(1)

    const pipeline = useDocumentPipeline({ onNotice: () => {} })

    // 两条流各自「卡住」，按被调用的顺序放行：0 = A，1 = B
    const releases: Array<() => void> = []
    let streamCount = 0
    vi.mocked(api.readJobStream).mockImplementation(async (_jobId, handlers) => {
      const index = streamCount
      streamCount += 1
      await new Promise<void>((resolve) => {
        releases[index] = resolve
      })
      if (index === 0) {
        // A 的迟到事件：它属于已删除的会话 A
        handlers?.onChunk?.('A 的迟到增量')
        return { conversationId: '', reply: '# A 的迟到接口文档', asks: [] }
      }
      // B 正常结束但无正文，避免 B 自己的落稿干扰断言
      return { conversationId: '', reply: '', asks: [] }
    })

    // 1. 方案 A 发起生成（接口文档链路：它的 handlers 挂了 onChunk，串稿最直观）
    //    接口文档以 PRD 正文为输入，没有它入口会直接返回
    documents.contents.prd = '# A 的 PRD'
    const jobA = pipeline.handleGenerateApiDocs()
    await flushPromises()
    expect(streamCount).toBe(1)

    // 2. 删除 A
    await solutions.remove(1)
    expect(solutions.currentId).toBeNull()

    // 3. 新建方案 B 并再发起一次生成——这一步会让 `handlersForKind` 被**再调用一次**，
    //    共享代次被覆盖就发生在这里。
    //    运行态已随 `remove` → `reset()` → `runs.clear()` 一起清空：A 的订阅不再挡住界面
    //    （判据是「本方案有没有在途任务」，而不是一个与方案无关的全局布尔）。
    vi.mocked(solutionApi.createSolution).mockResolvedValue(summary(2))
    workflow.chooseEntry('structured')
    workflow.summary = FULL_SUMMARY
    await solutions.flushPendingSave()
    expect(solutions.currentId).toBe(2)

    documents.contents.prd = '# B 的 PRD'
    const jobB = pipeline.handleGenerateApiDocs()
    await flushPromises()
    expect(streamCount).toBe(2)

    // 4. 放行 A 的迟到事件：此时 B 的订阅还开着，预览缓冲正是 B 的
    releases[0]?.()
    await jobA
    await flushPromises()

    // A 的增量没有进 B 的缓冲，A 的正文没有进 documents（B 的产物仍为空）
    expect(pipeline.docStreamingContent.value).not.toContain('A 的')
    expect(documents.contents['api-docs']).toBe('')
    expect(documents.contentWriteTick).toBe(0)
    expect(workflow.viewState).not.toBe('review-api-docs')
    // B 仍是当前方案，A 的收尾不该把它切走或清掉
    expect(solutions.currentId).toBe(2)

    // 收尾：放行 B
    releases[1]?.()
    await jobB
    await flushPromises()
  })

  /**
   * 场景骨架：A 起任务并卡住 → 删 A → 建 B 再起任务 → 放行 A → 检查 **B 的任务指针还在**。
   *
   * 钉的是 7 条链里那句 `if (isExpired(epochAtStart)) return`（**不是** handlers 那道守卫，
   * 也不是 `finishDoc` 那道）。少了它，A 的收尾会继续往下走执行到 `clearActiveJob()`，
   * 把 **B** 的 `activeJobId` 清空——用户看到的是「B 生成到一半，刷新页面就再也找不回任务」。
   * 因为 `finishDoc` 自己还有一道守卫，正文不会串进文档，所以只看文档的断言全是绿的，
   * 只有任务指针会露馅——这也正是它容易被漏抄一处的原因。
   *
   * `startA` 只决定「A 用哪条链发起」：守卫是逐链复制的一行，一条链上有用例，
   * 不代表另一条链上那行还在。
   */
  async function expectLateFinishKeepsTaskPointer(
    startA: (pipeline: ReturnType<typeof useDocumentPipeline>) => Promise<void>,
  ): Promise<void> {
    workflow.chooseEntry('structured')
    workflow.summary = FULL_SUMMARY
    await solutions.flushPendingSave()
    expect(solutions.currentId).toBe(1)

    // 两次 createJob 拿到不同的任务 id，才看得出指针是被谁清掉的
    let nextJobId = 12
    vi.mocked(api.createJob).mockImplementation(async () => {
      const jobId = nextJobId
      nextJobId += 1
      return { jobId, status: 'running' }
    })

    const pipeline = useDocumentPipeline({ onNotice: () => {} })
    const releases: Array<() => void> = []
    let streamCount = 0
    vi.mocked(api.readJobStream).mockImplementation(async () => {
      const index = streamCount
      streamCount += 1
      await new Promise<void>((resolve) => {
        releases[index] = resolve
      })
      if (index === 0) {
        return { conversationId: '', reply: '# A 的迟到正文', asks: [] }
      }
      return { conversationId: '', reply: '', asks: [] }
    })

    // 方案 A 发起一次生成（链路由调用方决定）
    documents.contents.prd = '# A 的 PRD'
    const jobA = startA(pipeline)
    await flushPromises()
    expect(workflow.activeJobId).toBe(12)

    // 删除 A，再建 B 并让 B 也发起一次生成
    await solutions.remove(1)
    vi.mocked(solutionApi.createSolution).mockResolvedValue(summary(2))
    workflow.chooseEntry('structured')
    workflow.summary = FULL_SUMMARY
    await solutions.flushPendingSave()
    expect(solutions.currentId).toBe(2)

    documents.contents.prd = '# B 的 PRD'
    const jobB = pipeline.handleGenerateApiDocs()
    await flushPromises()
    expect(workflow.activeJobId).toBe(13)

    // 放行 A 的迟到收尾
    releases[0]?.()
    await jobA
    await flushPromises()

    // B 仍然握着**自己**的任务指针（刷新后能靠它重连），文档也没被 A 写进来
    expect(workflow.activeJobId).toBe(13)
    expect(workflow.activeJobKind).toBe('api-docs')
    expect(documents.contents['api-docs']).toBe('')
    expect(pipeline.docError.value).toBe('')

    releases[1]?.()
    await jobB
    await flushPromises()
  }

  it('方案 A 的迟到收尾不能清掉方案 B 的任务指针（接口文档链）', async () => {
    await expectLateFinishKeepsTaskPointer((pipeline) => pipeline.handleGenerateApiDocs())
  })

  it('同上，PRD 链：await 之后的守卫是逐链复制的，漏抄一处只有指针会露馅', async () => {
    await expectLateFinishKeepsTaskPointer((pipeline) => pipeline.generatePrd())
  })

  it('同上，提示词链', async () => {
    await expectLateFinishKeepsTaskPointer(async (pipeline) => {
      // 提示词套件以上游两份正文为输入，缺一份入口会直接返回
      documents.contents['api-docs'] = '# A 的接口文档'
      await pipeline.handleGeneratePrompts()
    })
  })

  it('同上，优化链', async () => {
    await expectLateFinishKeepsTaskPointer(async (pipeline) => {
      // 优化不切阶段：目标产物由当前审阅页反推，因此要先停在审阅页
      workflow.goTo('review-api-docs')
      await pipeline.onAiOptimize({ content: '# A 的接口文档', instruction: '改写一句' })
    })
  })

  it('同上，按澄清改写链', async () => {
    await expectLateFinishKeepsTaskPointer(async (pipeline) => {
      // 改写链要有本线程的对话历史与正文，否则入口直接返回
      workflow.chatTab = 'api-docs-clarify'
      workflow.messages = [{ role: 'user', content: '就按这个来', stage: 'api-docs-clarify' }]
      documents.contents['api-docs'] = '# A 的接口文档'
      await pipeline.applyClarification()
    })
  })

  it('同上，AI 自行澄清链', async () => {
    await expectLateFinishKeepsTaskPointer(async (pipeline) => {
      workflow.goTo('review-api-docs')
      documents.contents['api-docs'] = '# A 的接口文档'
      documents.pendingItems['api-docs'] = [
        {
          id: 'p1',
          question: '第一版要不要做离线同步',
          sections: ['2.1'],
          decider: '',
          importance: 'P1',
        },
      ]
      await pipeline.autoClarify()
    })
  })

  it('同上，重连链：刷新后重连的任务同样要被代次拦下', async () => {
    // 重连**不创建任务**（`activeJobId` 是快照里恢复出来的），所以单独一套骨架
    workflow.chooseEntry('structured')
    workflow.summary = FULL_SUMMARY
    await solutions.flushPendingSave()

    let nextJobId = 13
    vi.mocked(api.createJob).mockImplementation(async () => {
      const jobId = nextJobId
      nextJobId += 1
      return { jobId, status: 'running' }
    })

    const releases: Array<() => void> = []
    let streamCount = 0
    vi.mocked(api.readJobStream).mockImplementation(async () => {
      const index = streamCount
      streamCount += 1
      await new Promise<void>((resolve) => {
        releases[index] = resolve
      })
      return { conversationId: '', reply: '', asks: [] }
    })

    const pipeline = useDocumentPipeline({ onNotice: () => {} })
    // 模拟「刷新后从快照恢复出任务指针，随即重连」：指针已不再是一个独立字段，
    // 而是 `stores/runs` 里那一支运行态的派生值，因此这里先登记运行态
    useRunsStore().restore({ jobId: 12, solutionId: 1, jobKind: 'api-docs', docKind: 'api-docs' })
    const reconnecting = pipeline.reconnectJob(12)
    await flushPromises()
    expect(api.readJobStream).toHaveBeenCalledTimes(1)

    // 删掉方案 A，再在方案 B 里起一个新任务
    await solutions.remove(1)
    vi.mocked(solutionApi.createSolution).mockResolvedValue(summary(2))
    workflow.chooseEntry('structured')
    workflow.summary = FULL_SUMMARY
    await solutions.flushPendingSave()
    documents.contents.prd = '# B 的 PRD'
    const jobB = pipeline.handleGenerateApiDocs()
    await flushPromises()
    expect(workflow.activeJobId).toBe(13)

    // A 的重连迟到收尾
    releases[0]?.()
    await reconnecting
    await flushPromises()

    expect(workflow.activeJobId).toBe(13)

    releases[1]?.()
    await jobB
    await flushPromises()
  })

  it('没有入口模式时（空壳方案）不创建——创建分支的最后一道闸', async () => {
    // 有内容但没选入口：真实流程里只可能来自「reset() 之后又被迟到的回调填回内容」，
    // 因为用户必然先选入口才会产生内容。
    workflow.debugPrdText = '# 迟到的回调写进来的内容'
    expect(workflow.entryMode).toBeNull()
    expect(workflow.hasContent).toBe(true)

    await solutions.flushPendingSave()

    expect(solutionApi.createSolution).not.toHaveBeenCalled()
    expect(solutions.currentId).toBeNull()
  })

  it('删掉方案后重新选了入口，迟到的澄清回复也不落消息、不新建方案', async () => {
    // 这是同一个 Bug 的**第二条路径**：入口模式非空时，`saveNow` 那道
    // 「空壳方案」闸门拦不住，只能靠对话流自己按会话代次丢弃迟到的回复。
    workflow.chooseEntry('structured')
    workflow.summary = FULL_SUMMARY
    await solutions.flushPendingSave()
    expect(solutions.currentId).toBe(1)

    const chat = useClarificationChat(
      { onNotice: () => undefined, onEnterChat: () => undefined },
      { stage: 'requirement' },
    )

    // 让澄清回复「卡住」：模拟模型还在生成，请求已经在途
    let releaseStream: (() => void) | undefined
    vi.mocked(api.continueConversationStream).mockImplementation(async () => {
      await new Promise<void>((resolve) => {
        releaseStream = resolve
      })
      return { conversationId: 'conv-late', reply: '迟到的澄清回复', asks: [] }
    })

    const chatting = chat.sendMessage('继续聊')
    await flushPromises()
    expect(api.continueConversationStream).toHaveBeenCalledTimes(1)
    // 用户消息是删除之前由用户自己发的，此时还在列表里
    expect(workflow.messages.map((message) => message.content)).toContain('继续聊')

    // 1. 用户在回复途中删掉了当前方案
    vi.mocked(solutionApi.createSolution).mockClear()
    await solutions.remove(1)
    expect(workflow.messages).toHaveLength(0)

    // 2. 用户重新选了入口。此刻 `busy` 必须已经回到 false，否则入口点不动；
    //    `chatStreaming` 是四条线程共用的全局标志，别的线程收尾就会把它清掉，
    //    因此「本线程的请求还在飞、而入口已经可点」是真实可达的状态。
    workflow.chatStreaming = false
    expect(workflow.chooseEntry('structured')).toBe(true)
    expect(workflow.entryMode).toBe('structured')

    // 3. 迟到的回复这时才到
    releaseStream?.()
    await chatting
    await flushPromises()

    // 整路丢弃：没有新消息、没写会话 id、没留错误提示
    expect(workflow.messages).toHaveLength(0)
    expect(workflow.conversationIds.requirement).toBe('')
    expect(chat.chatError.value).toBe('')

    // 关上最后一道：即便此刻触发一次保存，也不会冒出「未命名方案」
    await solutions.flushPendingSave()
    expect(solutionApi.createSolution).not.toHaveBeenCalled()
    expect(solutions.currentId).toBeNull()
  })
})
