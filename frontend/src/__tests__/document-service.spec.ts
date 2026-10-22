import { AxiosError, AxiosHeaders } from 'axios'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import http from '../services/api'
import {
  DocumentsNotFoundError,
  checkpointDocument,
  deleteVersion,
  fetchDocuments,
  fetchVersion,
  fetchVersions,
  fromBackendKind,
  restoreVersion,
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
      protocol: 'ark',
      providerConfigId: 'cfg_ark0001',
      providerName: '火山方舟',
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
      {
        id: 'overview-1',
        question: '指标口径谁拍板？',
        sections: ['2.1'],
        decider: '数据团队',
        importance: 'P0',
      },
      {
        id: 'p-2',
        question: '分页上限取多少？',
        sections: [],
        decider: '',
        importance: 'P1',
      },
    ]
    vi.mocked(http.get).mockResolvedValue({
      data: { documents: [backendDocument({ pending_items: pendingItems })] },
    })

    const records = await fetchDocuments(7)

    expect(records[0]?.pendingItems).toEqual(pendingItems)
  })

  it('重要性等级缺失或非法时丢弃这一条，而不是替它挑一档', async () => {
    vi.mocked(http.get).mockResolvedValue({
      data: {
        documents: [
          backendDocument({
            pending_items: [
              { id: 'p-1', question: '没标等级？', sections: [], decider: '' },
              { id: 'p-2', question: '乱标等级？', sections: [], decider: '', importance: 'X9' },
              { id: 'p-3', question: '超范围？', sections: [], decider: '', importance: 'P7' },
              { id: 'p-4', question: '正常？', sections: [], decider: '', importance: 'P3' },
            ],
          }),
        ],
      },
    })

    const records = await fetchDocuments(7)

    // 与后端口径一致：等级不合法就是不合法，不兜底——兜底会产出一份「等级是猜的」清单
    expect(records[0]?.pendingItems).toEqual([
      { id: 'p-4', question: '正常？', sections: [], decider: '', importance: 'P3' },
    ])
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
            pending_items: [
              { id: 'p-1', question: '上限？', sections: ['2', 3], decider: 7, importance: 'p3' },
            ],
          }),
        ],
      },
    })

    const records = await fetchDocuments(7)

    expect(records[0]?.pendingItems).toEqual([
      // 等级顺带被规整成大写（`p3` → `P3`）
      { id: 'p-1', question: '上限？', sections: ['2'], decider: '', importance: 'P3' },
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

// ---------------------------------------------------------------------------
// 版本链
// ---------------------------------------------------------------------------

/** 后端形状的一条版本摘要 */
function backendVersion(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    version_id: 11,
    version_no: 2,
    source: 'checkpoint',
    note: '定稿',
    content_sha: 'abcdef123456',
    content_length: 128,
    created_at: '2026-10-16T02:00:00',
    is_current: true,
    ...overrides,
  }
}

describe('fetchVersions', () => {
  it('把路径拼成带下划线的产物段，并把摘要折成 camelCase', async () => {
    vi.mocked(http.get).mockResolvedValue({ data: { versions: [backendVersion()] } })

    const list = await fetchVersions(7, 'api-docs')

    expect(http.get).toHaveBeenCalledWith('/api/solutions/7/documents/api_docs/versions')
    expect(list).toHaveLength(1)
    expect(list[0]).toEqual({
      versionId: 11,
      versionNo: 2,
      source: 'checkpoint',
      note: '定稿',
      contentSha: 'abcdef123456',
      contentLength: 128,
      createdAt: '2026-10-16T02:00:00',
      isCurrent: true,
    })
  })

  it('缺 version_id 的那一条被丢弃（它是恢复动作的引用键），其余照常保留', async () => {
    vi.mocked(http.get).mockResolvedValue({
      data: { versions: [backendVersion(), { version_no: 1 }] },
    })

    const list = await fetchVersions(7, 'prd')

    expect(list.map((v) => v.versionId)).toEqual([11])
  })

  it('没有版本时返回空列表（200 + 空，不是错误）', async () => {
    vi.mocked(http.get).mockResolvedValue({ data: {} })

    await expect(fetchVersions(7, 'prd')).resolves.toEqual([])
  })

  it('404 抛 DocumentsNotFoundError', async () => {
    vi.mocked(http.get).mockRejectedValue(axiosError(404, '方案不存在'))

    await expect(fetchVersions(7, 'prd')).rejects.toBeInstanceOf(DocumentsNotFoundError)
  })

  it('其它错误透传后端 detail，取不到时用兜底文案', async () => {
    vi.mocked(http.get).mockRejectedValue(axiosError(500, '数据库连接失败'))
    await expect(fetchVersions(7, 'prd')).rejects.toThrow('数据库连接失败')

    vi.mocked(http.get).mockRejectedValue(new Error('网络断了'))
    await expect(fetchVersions(7, 'prd')).rejects.toThrow('无法读取版本历史')
  })
})

describe('fetchVersion', () => {
  it('读单个版本，含正文、清单与回执', async () => {
    vi.mocked(http.get).mockResolvedValue({
      data: {
        ...backendVersion(),
        content: '# 定稿正文',
        pending_items: [{ id: 'p-1', question: '确认登录方式', importance: 'P0' }],
        last_run_summary: { kind: 'prd', durationMs: 1, requestId: 'req-1' },
        updated_at: '2026-10-16T02:30:00',
      },
    })

    const detail = await fetchVersion(7, 'prd', 11)

    expect(http.get).toHaveBeenCalledWith('/api/solutions/7/documents/prd/versions/11')
    expect(detail.content).toBe('# 定稿正文')
    expect(detail.pendingItems).toHaveLength(1)
    expect(detail.runSummary?.requestId).toBe('req-1')
    expect(detail.updatedAt).toBe('2026-10-16T02:30:00')
  })

  it('库里那份回执是 snake_case——刷新后必须还能读出来（旧实现这里整块消失）', async () => {
    // 后端把 `run_summary` 事件的**同一个 dict** 存进 `documents.last_run_summary`，
    // 所以真实数据的键一定是 `duration_ms` / `request_id`。旧实现在这里直接断言成
    // `RunSummary` 并要求有 `durationMs`，于是刷新后回执永远为 null——
    // 界面上表现为「生成当下有、刷新就没了」，而且不报错。
    vi.mocked(http.get).mockResolvedValue({
      data: {
        ...backendVersion(),
        content: '# 定稿正文',
        last_run_summary: {
          kind: 'prd',
          status: 'completed',
          duration_ms: 61_000,
          total_calls: 12,
          prompt_tokens: 90_000,
          completion_tokens: 8_000,
          protocol: 'ark',
          provider_config_id: 'cfg_ark0001',
          provider_name: '火山方舟',
          model: 'doubao-seed-2-1-turbo',
          request_id: 'req-refresh',
        },
        updated_at: '2026-10-16T02:30:00',
      },
    })

    const detail = await fetchVersion(7, 'prd', 11)

    expect(detail.runSummary).not.toBeNull()
    expect(detail.runSummary?.durationMs).toBe(61_000)
    expect(detail.runSummary?.totalCalls).toBe(12)
    expect(detail.runSummary?.promptTokens).toBe(90_000)
    expect(detail.runSummary?.requestId).toBe('req-refresh')
    // 回执上要能说清「走的是哪条配置」：协议 + 配置名 + 配置 id
    expect(detail.runSummary?.protocol).toBe('ark')
    expect(detail.runSummary?.providerName).toBe('火山方舟')
    expect(detail.runSummary?.providerConfigId).toBe('cfg_ark0001')
  })

  it('摘要字段不合法（缺 version_id）时抛错，不静默返回空版本', async () => {
    vi.mocked(http.get).mockResolvedValue({ data: { content: '# x' } })

    await expect(fetchVersion(7, 'prd', 11)).rejects.toThrow('无法读取该版本')
  })

  it('404 时透传后端说明（版本级 404 有两种含义，不该拿「方案不存在」去套）', async () => {
    vi.mocked(http.get).mockRejectedValue(axiosError(404, '版本不存在'))

    await expect(fetchVersion(7, 'prd', 99)).rejects.toThrow('版本不存在')
  })
})

describe('checkpointDocument', () => {
  it('POST 到 checkpoint，带上正文与备注', async () => {
    vi.mocked(http.post).mockResolvedValue({ data: backendDocument({ content: '# 存档版' }) })

    const record = await checkpointDocument(7, 'api-docs', '# 存档版', '定稿')

    expect(http.post).toHaveBeenCalledWith('/api/solutions/7/documents/api_docs/checkpoint', {
      content: '# 存档版',
      note: '定稿',
    })
    expect(record.content).toBe('# 存档版')
  })

  it('备注缺省时提交空串（后端字段是必填的）', async () => {
    vi.mocked(http.post).mockResolvedValue({ data: backendDocument() })

    await checkpointDocument(7, 'prd', '# PRD')

    expect(http.post).toHaveBeenCalledWith('/api/solutions/7/documents/prd/checkpoint', {
      content: '# PRD',
      note: '',
    })
  })

  it('404 抛 DocumentsNotFoundError', async () => {
    vi.mocked(http.post).mockRejectedValue(axiosError(404, '方案不存在'))

    await expect(checkpointDocument(7, 'prd', '# PRD')).rejects.toBeInstanceOf(
      DocumentsNotFoundError,
    )
  })

  it('响应不是一份合法文档时抛错，不静默成功', async () => {
    vi.mocked(http.post).mockResolvedValue({ data: { kind: '不认识的产物' } })

    await expect(checkpointDocument(7, 'prd', '# PRD')).rejects.toThrow('保存为新版本失败')
  })
})

describe('restoreVersion', () => {
  it('POST 到 restore，把 version_id 用下划线传给后端', async () => {
    vi.mocked(http.post).mockResolvedValue({ data: backendDocument({ content: '# 恢复后的正文' }) })

    const record = await restoreVersion(7, 'prd', 11)

    expect(http.post).toHaveBeenCalledWith('/api/solutions/7/documents/prd/restore', {
      version_id: 11,
    })
    expect(record.content).toBe('# 恢复后的正文')
  })

  it('404 抛 DocumentsNotFoundError', async () => {
    vi.mocked(http.post).mockRejectedValue(axiosError(404, '版本不存在'))

    await expect(restoreVersion(7, 'prd', 99)).rejects.toBeInstanceOf(DocumentsNotFoundError)
  })
})

describe('deleteVersion', () => {
  it('DELETE 到带下划线的产物段 + 版本号', async () => {
    vi.mocked(http.delete).mockResolvedValue({ data: { ok: true, version_id: 11 } })

    await deleteVersion(7, 'api-docs', 11)

    expect(http.delete).toHaveBeenCalledWith('/api/solutions/7/documents/api_docs/versions/11')
  })

  it('当前版本被拒（400）时透传后端说明，不套兜底文案', async () => {
    vi.mocked(http.delete).mockRejectedValue(
      axiosError(400, '当前版本不能删除。想换用别的版本，请点「恢复此版本」'),
    )

    await expect(deleteVersion(7, 'prd', 11)).rejects.toThrow('当前版本不能删除')
  })

  it('404 同样透传后端说明', async () => {
    vi.mocked(http.delete).mockRejectedValue(axiosError(404, '版本不存在'))

    await expect(deleteVersion(7, 'prd', 99)).rejects.toThrow('版本不存在')
  })

  it('取不到 detail 时用兜底文案', async () => {
    vi.mocked(http.delete).mockRejectedValue(new Error('网络断了'))

    await expect(deleteVersion(7, 'prd', 11)).rejects.toThrow('删除版本失败')
  })
})
