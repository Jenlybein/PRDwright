import { flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as documentApi from '../services/documentService'
import { DOCUMENT_SAVE_DEBOUNCE_MS, useDocumentsStore } from '../stores/documents'
import type { DocumentKind, DocumentRecord, PendingItem, RunSummary } from '../types'

/**
 * 文档 store 测试。
 *
 * 这一层是产物正文在前端的**工作副本**，几处约定一旦写错都是「不报错但内容不对」：
 * 1. 它只是镜像——`loadAll` 拿回什么就是什么，不会自己拼正文；
 * 2. **未落库的手改优先于拉回来的响应**：用户刚敲的字比后端上一条记录更新，
 *    切阶段触发一次重读不能把它抹掉；
 * 3. 手改防抖 + 单飞：连续输入合并成一次，且任一时刻至多一个 PUT 在途
 *    （同一份文档被交错写入会互相覆盖）；
 * 4. 保存失败不丢内容：改动留在队列里，下一次变更或 flush 会重试；
 * 5. 方案不可见（404）时不继续显示上一份方案的正文。
 */

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

/** 造一条文档记录 */
function makeRecord(
  kind: DocumentKind,
  content: string,
  runSummary: RunSummary | null = null,
  pendingItems: PendingItem[] = [],
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

/** 造一份最小合法回执 */
function makeRunSummary(requestId: string): RunSummary {
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
    provider: 'ark',
    model: 'doubao',
    requestId,
  }
}

let documents: ReturnType<typeof useDocumentsStore>

beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()
  setActivePinia(createPinia())
  documents = useDocumentsStore()
  vi.mocked(documentApi.fetchDocuments).mockResolvedValue([])
  vi.mocked(documentApi.saveDocument).mockImplementation(async (_id, kind, content) =>
    makeRecord(kind, content),
  )
})

afterEach(() => {
  vi.useRealTimers()
})

/** 推进防抖窗口并等异步保存落地 */
async function advanceDebounce(ms = DOCUMENT_SAVE_DEBOUNCE_MS): Promise<void> {
  await vi.advanceTimersByTimeAsync(ms)
  await flushPromises()
}

describe('loadAll', () => {
  it('把三份文档的正文与回执一起装进工作副本', async () => {
    vi.mocked(documentApi.fetchDocuments).mockResolvedValue([
      makeRecord('prd', '# PRD', makeRunSummary('prd-req')),
      makeRecord('api-docs', '# 接口文档'),
    ])

    await documents.loadAll(7)

    expect(documentApi.fetchDocuments).toHaveBeenCalledWith(7)
    expect(documents.contentOf('prd')).toBe('# PRD')
    expect(documents.contentOf('api-docs')).toBe('# 接口文档')
    expect(documents.contentOf('prompts')).toBe('')
    expect(documents.runSummaryOf('prd')?.requestId).toBe('prd-req')
    expect(documents.runSummaryOf('api-docs')).toBeNull()
    expect(documents.solutionId).toBe(7)
  })

  it('把三份文档的待确认清单一起装进工作副本（后端落库的那一份，原样镜像）', async () => {
    const items: PendingItem[] = [
      {
        id: 'overview-1',
        question: '指标口径谁拍板？',
        sections: ['2.1'],
        decider: '数据团队',
        importance: 'P0',
      },
    ]
    vi.mocked(documentApi.fetchDocuments).mockResolvedValue([
      makeRecord('prd', '# PRD', makeRunSummary('prd-req'), items),
      makeRecord('api-docs', '# 接口文档'),
    ])

    await documents.loadAll(7)

    expect(documents.pendingItemsOf('prd')).toEqual(items)
    // 没生成过的文档是空清单，不是「后端没给」
    expect(documents.pendingItemsOf('api-docs')).toEqual([])
    expect(documents.pendingItemsOf('prompts')).toEqual([])
  })

  it('clear 把清单一并清空——切方案时不留下上一份的待确认项', async () => {
    vi.mocked(documentApi.fetchDocuments).mockResolvedValue([
      makeRecord('prd', '# PRD', null, [
        { id: 'p-1', question: '分页上限取多少？', sections: [], decider: '', importance: 'P1' },
      ]),
    ])
    await documents.loadAll(7)
    expect(documents.pendingItemsOf('prd')).toHaveLength(1)

    documents.clear()

    expect(documents.pendingItemsOf('prd')).toEqual([])
  })

  it('404 时清空内容并记下原因——不继续显示上一份方案的正文', async () => {
    documents.setLocal('prd', '# 上一份方案的 PRD')

    vi.mocked(documentApi.fetchDocuments).mockRejectedValue(
      new documentApi.DocumentsNotFoundError(),
    )

    await documents.loadAll(7)

    expect(documents.contentOf('prd')).toBe('')
    expect(documents.hasAny).toBe(false)
    expect(documents.saveError).toBe('方案不存在或已被删除')
  })

  it('其它读取失败保留旧内容，只记下错误', async () => {
    documents.setLocal('prd', '# 上一版')

    vi.mocked(documentApi.fetchDocuments).mockRejectedValue(new Error('网络断了'))

    await documents.loadAll(7)

    expect(documents.contentOf('prd')).toBe('# 上一版')
    expect(documents.saveError).toBe('网络断了')
  })

  it('切换方案时先发的响应后到也不会污染（序号守卫）', async () => {
    let releaseFirst: (records: DocumentRecord[]) => void = () => {}
    vi.mocked(documentApi.fetchDocuments)
      .mockImplementationOnce(
        () =>
          new Promise<DocumentRecord[]>((resolve) => {
            releaseFirst = resolve
          }),
      )
      .mockImplementationOnce(async () => [makeRecord('prd', 'B 方案的 PRD')])

    const first = documents.loadAll(1)
    const second = documents.loadAll(2)
    await second
    expect(documents.contentOf('prd')).toBe('B 方案的 PRD')

    // A 的响应现在才回来：它属于上一份方案，必须自己作废
    releaseFirst([makeRecord('prd', 'A 方案的 PRD')])
    await first
    expect(documents.contentOf('prd')).toBe('B 方案的 PRD')
    expect(documents.solutionId).toBe(2)
  })

  it('尚未落库的手改优先于拉回来的响应', async () => {
    documents.attach(3)
    // 手改还在防抖窗口里，没有发出去
    documents.save('prd', '刚敲的字')

    vi.mocked(documentApi.fetchDocuments).mockResolvedValue([makeRecord('prd', '后端的旧版')])
    await documents.loadAll(3)

    expect(documents.contentOf('prd')).toBe('刚敲的字')
  })
})

describe('reload / attach', () => {
  it('reload 用已登记的方案 id 重拉', async () => {
    documents.attach(5)

    await documents.reload()

    expect(documentApi.fetchDocuments).toHaveBeenCalledWith(5)
  })

  it('还没有归属（新建中）时 reload 不发请求', async () => {
    await documents.reload()

    expect(documentApi.fetchDocuments).not.toHaveBeenCalled()
  })

  it('attach 只登记归属，不发起请求（方案刚建出来时后端还没有文档）', () => {
    documents.attach(9)

    expect(documents.solutionId).toBe(9)
    expect(documentApi.fetchDocuments).not.toHaveBeenCalled()
  })
})

describe('save（用户手改）', () => {
  it('先更新本地（界面立刻可见），防抖后落库', async () => {
    documents.attach(3)

    documents.save('prd', '# 改过的 PRD')
    expect(documents.contentOf('prd')).toBe('# 改过的 PRD')
    expect(documentApi.saveDocument).not.toHaveBeenCalled()

    await advanceDebounce()
    expect(documentApi.saveDocument).toHaveBeenCalledWith(3, 'prd', '# 改过的 PRD')
  })

  it('连续输入合并成一次请求，只发最后那一版', async () => {
    documents.attach(3)

    documents.save('prd', '第一版')
    await vi.advanceTimersByTimeAsync(DOCUMENT_SAVE_DEBOUNCE_MS / 2)
    documents.save('prd', '第二版')
    await advanceDebounce()

    expect(documentApi.saveDocument).toHaveBeenCalledTimes(1)
    expect(documentApi.saveDocument).toHaveBeenCalledWith(3, 'prd', '第二版')
  })

  it('方案还没有 id 时不排队——没有方案可挂，发出去只会 404', async () => {
    documents.save('prd', '# 还没有方案')

    await advanceDebounce()

    expect(documentApi.saveDocument).not.toHaveBeenCalled()
    // 本地仍然更新了：界面不该因为「还没建方案」而看不到自己敲的字
    expect(documents.contentOf('prd')).toBe('# 还没有方案')
  })

  it('任一时刻至多一个保存在途；在途期间的新改动在收尾后补存', async () => {
    let concurrent = 0
    let maxConcurrent = 0
    const resolvers: Array<() => void> = []
    vi.mocked(documentApi.saveDocument).mockImplementation(async (_id, kind, content) => {
      concurrent += 1
      maxConcurrent = Math.max(maxConcurrent, concurrent)
      await new Promise<void>((resolve) => resolvers.push(resolve))
      concurrent -= 1
      return makeRecord(kind, content)
    })

    documents.attach(3)
    documents.save('prd', '第一版')
    await advanceDebounce()
    expect(documentApi.saveDocument).toHaveBeenCalledTimes(1)

    // 在途期间又改了一次：此刻绝不能并发再发一个请求
    documents.save('prd', '第二版')
    await advanceDebounce()
    expect(documentApi.saveDocument).toHaveBeenCalledTimes(1)

    // 放行第一个：补存自动接手
    resolvers.shift()?.()
    await flushPromises()
    expect(documentApi.saveDocument).toHaveBeenCalledTimes(2)
    expect(vi.mocked(documentApi.saveDocument).mock.calls[1]?.[2]).toBe('第二版')

    resolvers.shift()?.()
    await flushPromises()

    expect(maxConcurrent).toBe(1)
    expect(documents.hasPendingSave()).toBe(false)
  })

  it('保存失败时改动留在队列里，下一次变更会重试', async () => {
    vi.mocked(documentApi.saveDocument).mockRejectedValueOnce(new Error('网络断了'))

    documents.attach(3)
    documents.save('prd', '第一版')
    await advanceDebounce()

    expect(documents.hasPendingSave()).toBe(true)
    expect(documents.saveError).toBe('网络断了')
    // 内容还在本地——一次网络抖动不该让用户丢掉刚敲的字
    expect(documents.contentOf('prd')).toBe('第一版')

    documents.save('prd', '第二版')
    await advanceDebounce()

    expect(documentApi.saveDocument).toHaveBeenLastCalledWith(3, 'prd', '第二版')
    expect(documents.hasPendingSave()).toBe(false)
    expect(documents.saveError).toBe('')
  })

  it('方案不可见（404）时不反复重试，只把原因呈现给用户', async () => {
    vi.mocked(documentApi.saveDocument).mockRejectedValue(new documentApi.DocumentsNotFoundError())

    documents.attach(3)
    documents.save('prd', '第一版')
    await advanceDebounce()

    expect(documents.saveError).toBe('方案不存在或已被删除')
    // 404 再试也没有意义，队列不放回
    expect(documents.hasPendingSave()).toBe(false)
  })
})

describe('flushPendingSave', () => {
  it('把防抖窗口里压着的那次保存立刻发出去，并等它结束', async () => {
    documents.attach(3)
    documents.save('prd', '# 最后一笔')

    // 不推进防抖窗口，直接 flush
    await documents.flushPendingSave()

    expect(documentApi.saveDocument).toHaveBeenCalledWith(3, 'prd', '# 最后一笔')
    expect(documents.hasPendingSave()).toBe(false)
  })

  it('没有待保存的改动时什么也不做', async () => {
    documents.attach(3)

    await documents.flushPendingSave()

    expect(documentApi.saveDocument).not.toHaveBeenCalled()
  })
})

describe('派生状态', () => {
  it('三份正文各自决定对应的 has* 与总体的 hasAny', async () => {
    expect(documents.hasAny).toBe(false)

    vi.mocked(documentApi.fetchDocuments).mockResolvedValue([makeRecord('prompts', '# 提示词')])
    await documents.loadAll(1)

    expect(documents.hasPrd).toBe(false)
    expect(documents.hasPrompts).toBe(true)
    expect(documents.hasAny).toBe(true)
  })

  it('只有空白的正文不算「有」', () => {
    documents.setLocal('prd', '   \n  ')

    expect(documents.hasPrd).toBe(false)
    expect(documents.hasAny).toBe(false)
  })
})

describe('clear', () => {
  it('清空正文、回执与归属', async () => {
    vi.mocked(documentApi.fetchDocuments).mockResolvedValue([
      makeRecord('prd', '# PRD', makeRunSummary('prd-req')),
    ])
    await documents.loadAll(1)

    documents.clear()

    expect(documents.hasAny).toBe(false)
    expect(documents.contentOf('prd')).toBe('')
    expect(documents.runSummaryOf('prd')).toBeNull()
    expect(documents.solutionId).toBeNull()
  })

  it('让在途的读取结果作废——它的响应属于上一份方案', async () => {
    let release: (records: DocumentRecord[]) => void = () => {}
    vi.mocked(documentApi.fetchDocuments).mockImplementationOnce(
      () =>
        new Promise<DocumentRecord[]>((resolve) => {
          release = resolve
        }),
    )

    const pending = documents.loadAll(1)
    documents.clear()
    release([makeRecord('prd', 'A 方案的 PRD')])
    await pending

    expect(documents.contentOf('prd')).toBe('')
  })
})
