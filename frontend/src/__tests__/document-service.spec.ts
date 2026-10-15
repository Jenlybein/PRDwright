import { AxiosError, AxiosHeaders } from 'axios'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import http from '../services/api'
import {
  DocumentsNotFoundError,
  fetchDocuments,
  fromBackendKind,
  saveDocument,
} from '../services/documentService'

/**
 * 文档接口测试。
 *
 * 这一层是「文档所有权在后端」落地后前端仅有的两条通道（读 / 提交用户变更）。
 * 值得单独钉住的不是「发没发请求」，而是几处**命名与语义约定**：
 * 1. `api_docs`（后端下划线）↔ `api-docs`（前端连字符）的转换只在这一个文件里发生，
 *    而它的路径参数与响应解析分别走两条不同的分支，两边都得对；
 * 2. 404 一律收敛成 `DocumentsNotFoundError`——后端刻意让「不存在」与「越权」不可区分，
 *    前端也不该擅自还原这个信息；
 * 3. 回执（`last_run_summary`）**按不透明对象透传**，结构对不上只让这一份「不显示回执」，
 *    不能把整条文档丢掉；同理未知 `kind` 只丢那一条，不影响其它文档。
 */

vi.mock('../services/api', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}))

/** 造一个带后端错误响应体的 AxiosError（`isAxiosError` 为真） */
function axiosError(status: number, detail?: unknown): AxiosError {
  const error = new AxiosError('请求失败', 'ERR_BAD_RESPONSE')
  error.response = {
    status,
    statusText: '',
    data: detail === undefined ? {} : { detail },
    headers: {},
    config: { headers: new AxiosHeaders() },
  }
  return error
}

/** 后端形状的一份文档 */
function backendDocument(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    kind: 'prd',
    content: '# 产品需求文档',
    pending_items: [],
    last_run_summary: null,
    updated_at: '2026-10-13T00:00:00',
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('fromBackendKind', () => {
  it('认下划线写法，并把它折成前端的连字符写法', () => {
    expect(fromBackendKind('prd')).toBe('prd')
    expect(fromBackendKind('api_docs')).toBe('api-docs')
    expect(fromBackendKind('prompts')).toBe('prompts')
  })

  it('连字符写法也认（迁移期两种都可能出现）', () => {
    expect(fromBackendKind('api-docs')).toBe('api-docs')
  })

  it('未知取值与非法类型一律返回 null，由调用方丢弃这一条', () => {
    expect(fromBackendKind('unknown')).toBeNull()
    expect(fromBackendKind(null)).toBeNull()
    expect(fromBackendKind(42)).toBeNull()
  })
})

describe('fetchDocuments', () => {
  it('读取某方案下的文档，并把 api_docs 折成 api-docs', async () => {
    vi.mocked(http.get).mockResolvedValue({
      data: {
        documents: [
          backendDocument(),
          backendDocument({ kind: 'api_docs', content: '# 接口文档' }),
          backendDocument({ kind: 'prompts', content: '# 提示词套件' }),
        ],
      },
    })

    const records = await fetchDocuments(7)

    expect(http.get).toHaveBeenCalledWith('/api/solutions/7/documents')
    expect(records.map((item) => item.kind)).toEqual(['prd', 'api-docs', 'prompts'])
    expect(records[1]?.content).toBe('# 接口文档')
    expect(records[0]?.updatedAt).toBe('2026-10-13T00:00:00')
  })

  it('回执按不透明对象透传（结构合格时原样带出）', async () => {
    const summary = {
      kind: 'prd',
      status: 'completed',
      durationMs: 1200,
      totalCalls: 1,
      failedCalls: 0,
      promptTokens: 10,
      completionTokens: 20,
      steps: [],
      injectedSkills: [],
      warnings: [],
      provider: 'ark',
      model: 'doubao',
      requestId: 'prd-req',
    }
    vi.mocked(http.get).mockResolvedValue({
      data: { documents: [backendDocument({ last_run_summary: summary })] },
    })

    const records = await fetchDocuments(7)

    expect(records[0]?.runSummary?.requestId).toBe('prd-req')
  })

  it('回执结构对不上时退化为 null，但文档本身保留', async () => {
    vi.mocked(http.get).mockResolvedValue({
      data: { documents: [backendDocument({ last_run_summary: { 某个: '不认识的形状' } })] },
    })

    const records = await fetchDocuments(7)

    expect(records).toHaveLength(1)
    expect(records[0]?.runSummary).toBeNull()
  })

  it('待确认清单原样带出（后端落库的那一份，前端不重算）', async () => {
    const pendingItems = [
      { id: 'overview-1', question: '指标口径谁拍板？', sections: ['2.1'], decider: '数据团队' },
      { id: 'p-2', question: '分页上限取多少？', sections: [], decider: '' },
    ]
    vi.mocked(http.get).mockResolvedValue({
      data: { documents: [backendDocument({ pending_items: pendingItems })] },
    })

    const records = await fetchDocuments(7)

    expect(records[0]?.pendingItems).toEqual(pendingItems)
  })

  it('清单缺字段时退化为空数组——「没有待确认项」，不影响文档本身', async () => {
    vi.mocked(http.get).mockResolvedValue({
      data: { documents: [backendDocument({ pending_items: [{ question: '缺 id' }, 42] })] },
    })

    const records = await fetchDocuments(7)

    expect(records).toHaveLength(1)
    expect(records[0]?.pendingItems).toEqual([])
    expect(records[0]?.content).toBe('# 产品需求文档')
  })

  it('清单字段类型不对时按缺省值补齐（脏数据不该让整条文档读不出来）', async () => {
    vi.mocked(http.get).mockResolvedValue({
      data: {
        documents: [
          backendDocument({
            pending_items: [{ id: 'p-1', question: '上限？', sections: ['2', 3], decider: 7 }],
          }),
        ],
      },
    })

    const records = await fetchDocuments(7)

    expect(records[0]?.pendingItems).toEqual([
      { id: 'p-1', question: '上限？', sections: ['2'], decider: '' },
    ])
  })

  it('未知 kind 的那条被丢弃，其余照常返回', async () => {
    vi.mocked(http.get).mockResolvedValue({
      data: {
        documents: [backendDocument(), backendDocument({ kind: '未来的新产物' })],
      },
    })

    const records = await fetchDocuments(7)

    expect(records.map((item) => item.kind)).toEqual(['prd'])
  })

  it('documents 字段缺失时返回空数组——「没有文档」不是错误', async () => {
    vi.mocked(http.get).mockResolvedValue({ data: {} })

    await expect(fetchDocuments(7)).resolves.toEqual([])
  })

  it('404 抛 DocumentsNotFoundError（方案不可见）', async () => {
    vi.mocked(http.get).mockRejectedValue(axiosError(404, '方案不存在'))

    await expect(fetchDocuments(7)).rejects.toBeInstanceOf(DocumentsNotFoundError)
  })

  it('其他错误抛普通 Error，并透传后端 detail', async () => {
    vi.mocked(http.get).mockRejectedValue(axiosError(500, '数据库连接失败'))

    await expect(fetchDocuments(7)).rejects.toThrow('数据库连接失败')
  })

  it('取不到 detail 时用兜底文案', async () => {
    vi.mocked(http.get).mockRejectedValue(new Error('网络断了'))

    await expect(fetchDocuments(7)).rejects.toThrow('无法读取文档')
  })
})

describe('saveDocument', () => {
  it('PUT 到带下划线的路径，只提交 content', async () => {
    vi.mocked(http.put).mockResolvedValue({
      data: backendDocument({ kind: 'api_docs', content: '# 改过的接口文档' }),
    })

    const record = await saveDocument(7, 'api-docs', '# 改过的接口文档')

    expect(http.put).toHaveBeenCalledWith('/api/solutions/7/documents/api_docs', {
      content: '# 改过的接口文档',
    })
    expect(record.kind).toBe('api-docs')
    expect(record.content).toBe('# 改过的接口文档')
  })

  it('prd / prompts 路径不带上任何前缀写法', async () => {
    vi.mocked(http.put).mockResolvedValue({ data: backendDocument() })

    await saveDocument(7, 'prd', '# PRD')

    expect(http.put).toHaveBeenCalledWith('/api/solutions/7/documents/prd', { content: '# PRD' })
  })

  it('404 抛 DocumentsNotFoundError', async () => {
    vi.mocked(http.put).mockRejectedValue(axiosError(404, '方案不存在'))

    await expect(saveDocument(7, 'prd', '# PRD')).rejects.toBeInstanceOf(DocumentsNotFoundError)
  })

  it('校验失败（422 且 detail 是数组）时给可读文案', async () => {
    vi.mocked(http.put).mockRejectedValue(axiosError(422, [{ msg: 'bad' }]))

    await expect(saveDocument(7, 'prd', '# PRD')).rejects.toThrow(
      '提交的内容不符合要求，请检查后重试',
    )
  })

  it('响应不是一份合法文档时抛「保存文档失败」，不静默成功', async () => {
    vi.mocked(http.put).mockResolvedValue({ data: { kind: '不认识的产物' } })

    await expect(saveDocument(7, 'prd', '# PRD')).rejects.toThrow('保存文档失败')
  })
})
