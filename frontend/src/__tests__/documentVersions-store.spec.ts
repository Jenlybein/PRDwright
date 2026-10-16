import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import * as documentApi from '../services/documentService'
import { useDocumentsStore } from '../stores/documents'
import { useDocumentVersionsStore } from '../stores/documentVersions'
import type { DocumentKind, DocumentRecord, DocumentVersionSummary } from '../types'

/**
 * 文档版本 store 测试。
 *
 * 这一层把「版本链」与「当前正文的工作副本」分开：`documents` 管正文与手改落库，
 * 本 store 管版本列表与两个写动作。几处约定一旦写错都是「不报错但结果不对」：
 *
 * 1. **写动作之后必须重新拉文档**——不拉的话编辑器里还是旧正文，用户以为「恢复没生效」；
 * 2. **写动作之前先把在途的手改冲掉**——否则 `loadAll` 会把未落库的旧草稿盖在恢复结果上；
 * 3. 加载带序号守卫：快速切换文档时先发的响应后到，不能覆盖后发的；
 * 4. 归属不另存一份，直接读 `documents.solutionId`。
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
  fetchVersions: vi.fn(),
  fetchVersion: vi.fn(),
  checkpointDocument: vi.fn(),
  restoreVersion: vi.fn(),
}))

/** 造一条版本摘要 */
function summary(
  versionId: number,
  versionNo: number,
  isCurrent = false,
): DocumentVersionSummary {
  return {
    versionId,
    versionNo,
    source: 'prd',
    note: '',
    contentSha: 'sha',
    contentLength: 10,
    createdAt: '2026-10-16T00:00:00',
    isCurrent,
  }
}

/** 造一条文档记录 */
function makeRecord(kind: DocumentKind, content: string): DocumentRecord {
  return { kind, content, runSummary: null, pendingItems: [], updatedAt: '2026-10-16T00:00:00' }
}

function setup(solutionId: number | null = 1) {
  setActivePinia(createPinia())
  const documents = useDocumentsStore()
  const versions = useDocumentVersionsStore()
  vi.mocked(documentApi.fetchDocuments).mockResolvedValue([])
  if (solutionId !== null) documents.attach(solutionId)
  return { documents, versions }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('useDocumentVersionsStore 的读取', () => {
  it('load：把版本列表落到对应产物上', async () => {
    const { versions } = setup()
    vi.mocked(documentApi.fetchVersions).mockResolvedValue([summary(11, 2, true), summary(10, 1)])

    await versions.load('prd')

    expect(documentApi.fetchVersions).toHaveBeenCalledWith(1, 'prd')
    expect(versions.versionsByKind.prd.map((v) => v.versionId)).toEqual([11, 10])
    expect(versions.loading).toBe(false)
    expect(versions.error).toBe('')
  })

  it('load：方案还没登记时不做任何请求', async () => {
    const { versions } = setup(null)

    await versions.load('prd')

    expect(documentApi.fetchVersions).not.toHaveBeenCalled()
  })

  it('load：失败时记下原因并清空该产物的列表', async () => {
    const { versions } = setup()
    versions.versionsByKind.prd = [summary(11, 2)]
    vi.mocked(documentApi.fetchVersions).mockRejectedValue(new Error('无法读取版本历史'))

    await versions.load('prd')

    expect(versions.error).toBe('无法读取版本历史')
    expect(versions.versionsByKind.prd).toEqual([])
    expect(versions.loading).toBe(false)
  })

  it('load：先发的响应后到时被丢弃，不覆盖后发的结果', async () => {
    const { versions } = setup()
    let resolveFirst: (value: DocumentVersionSummary[]) => void = () => {}
    vi.mocked(documentApi.fetchVersions)
      .mockImplementationOnce(
        () =>
          new Promise<DocumentVersionSummary[]>((resolve) => {
            resolveFirst = resolve
          }),
      )
      .mockResolvedValueOnce([summary(99, 9)])

    const first = versions.load('prd')
    const second = versions.load('prd')
    await second
    resolveFirst([summary(1, 1)])
    await first

    expect(versions.versionsByKind.prd.map((v) => v.versionId)).toEqual([99])
  })
})

describe('useDocumentVersionsStore 的写入', () => {
  it('checkpoint：写版本后重新拉文档与列表（否则界面还是旧正文）', async () => {
    const { versions } = setup()
    vi.mocked(documentApi.checkpointDocument).mockResolvedValue(makeRecord('prd', '# 存档'))
    vi.mocked(documentApi.fetchVersions).mockResolvedValue([summary(11, 2, true)])

    const ok = await versions.checkpoint('prd', '# 存档', '定稿')

    expect(ok).toBe(true)
    expect(documentApi.checkpointDocument).toHaveBeenCalledWith(1, 'prd', '# 存档', '定稿')
    expect(documentApi.fetchDocuments).toHaveBeenCalledWith(1) // reload
    expect(versions.versionsByKind.prd).toHaveLength(1)
    expect(versions.error).toBe('')
  })

  it('checkpoint：先把在途的手改冲掉，再写版本', async () => {
    const { documents, versions } = setup()
    vi.mocked(documentApi.saveDocument).mockResolvedValue(makeRecord('prd', '# 手改'))
    vi.mocked(documentApi.checkpointDocument).mockResolvedValue(makeRecord('prd', '# 手改'))
    vi.mocked(documentApi.fetchVersions).mockResolvedValue([])

    // 制造一次「尚未落库的手改」（防抖窗口内）
    documents.save('prd', '# 手改')

    await versions.checkpoint('prd', '# 手改')

    const saveOrder = vi.mocked(documentApi.saveDocument).mock.invocationCallOrder[0] ?? 0
    const checkpointOrder = vi.mocked(documentApi.checkpointDocument).mock.invocationCallOrder[0] ?? 0
    expect(saveOrder).toBeGreaterThan(0) // 手改确实发出去了
    expect(saveOrder).toBeLessThan(checkpointOrder)
  })

  it('restore：调用恢复接口并重新拉文档', async () => {
    const { versions } = setup()
    vi.mocked(documentApi.restoreVersion).mockResolvedValue(makeRecord('prd', '# 恢复'))
    vi.mocked(documentApi.fetchVersions).mockResolvedValue([summary(12, 3, true)])

    const ok = await versions.restore('prd', 10)

    expect(ok).toBe(true)
    expect(documentApi.restoreVersion).toHaveBeenCalledWith(1, 'prd', 10)
    expect(documentApi.fetchDocuments).toHaveBeenCalledWith(1)
  })

  it('写入失败时记下原因并返回 false，不刷新文档', async () => {
    const { versions } = setup()
    vi.mocked(documentApi.restoreVersion).mockRejectedValue(new Error('恢复版本失败'))

    const ok = await versions.restore('prd', 10)

    expect(ok).toBe(false)
    expect(versions.error).toBe('恢复版本失败')
    expect(documentApi.fetchDocuments).not.toHaveBeenCalled()
  })

  it('方案还没登记时直接返回 false，不发请求', async () => {
    const { versions } = setup(null)

    await expect(versions.checkpoint('prd', '# x')).resolves.toBe(false)
    await expect(versions.restore('prd', 1)).resolves.toBe(false)

    expect(documentApi.checkpointDocument).not.toHaveBeenCalled()
    expect(documentApi.restoreVersion).not.toHaveBeenCalled()
  })
})

describe('useDocumentVersionsStore 的复位', () => {
  it('clear：清空列表、加载态与错误', async () => {
    const { versions } = setup()
    vi.mocked(documentApi.fetchVersions).mockRejectedValue(new Error('读不到'))
    await versions.load('prd')
    expect(versions.error).toBe('读不到')

    versions.clear()

    expect(versions.versionsByKind).toEqual({ prd: [], 'api-docs': [], prompts: [] })
    expect(versions.error).toBe('')
    expect(versions.loading).toBe(false)
  })
})
