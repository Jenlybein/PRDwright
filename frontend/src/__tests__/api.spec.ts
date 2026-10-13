import axios, { type AxiosResponse, type InternalAxiosRequestConfig } from 'axios'
import { afterEach, describe, expect, it, vi } from 'vitest'

import http, {
  generatePrdFromSummaryStream,
  onUnauthorized,
  retrieveApiDocsRag,
  UnauthorizedError,
} from '../services/api'
import type { StreamDocumentPart, StreamStageEvent, RequirementsSummary } from '../types'

/**
 * SSE 解析测试。
 *
 * 重点覆盖 `stage` 与 `part` 两类事件：后端发的是 snake_case（`section_id` /
 * `section_title`），前端类型是 camelCase，映射错了不会报错、只会静默显示错章节
 * ——所以值得单独测。
 */

/** 用给定的 SSE 文本块构造一个流式响应（每块是完整的 `event:` + `data:` 帧） */
function streamResponse(blocks: string[]): Response {
  const encoder = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const block of blocks) controller.enqueue(encoder.encode(block))
      controller.close()
    },
  })
  return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
}

/** 把一个事件对象格式化成后端风格的 SSE 帧 */
function sseFrame(type: string, payload: Record<string, unknown>): string {
  return `event: ${type}\ndata: ${JSON.stringify(payload)}\n\n`
}

const SUMMARY = { product_name: 'HarnessPRD' } as RequirementsSummary

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('SSE 事件解析', () => {
  it('把 stage 事件从 snake_case 映射成前端字段，并把 chunk 交给增量回调', async () => {
    const fetchMock = vi.fn(async () =>
      streamResponse([
        sseFrame('status', { type: 'status', content: '正在分 7 章生成 PRD…', data: { total_sections: 7 } }),
        sseFrame('chunk', { type: 'chunk', content: '# 标题\n\n' }),
        sseFrame('stage', {
          type: 'stage',
          content: '正在撰写 ## 2. 功能需求（2/7）…',
          data: {
            stage: 'writing',
            section_id: 'features',
            section_title: '功能需求',
            section_heading: '## 2. 功能需求',
            index: 1,
            total: 7,
          },
        }),
        sseFrame('chunk', { type: 'chunk', content: '## 2. 功能需求\n\n正文' }),
        // 半帧 + 半帧：验证跨读取边界的缓冲区拼接
        'event: stage\ndata: {"type":"stage","data":{"stage":"rewrit',
        'ing","section_id":"features","section_title":"功能需求","index":1,"total":7,"note":"编造了指标","issues":[{"severity":"high","detail":"出现输入中没有的数据"}]}}\n\n',
        sseFrame('done', { type: 'done', content: '', data: { doc_type: 'prd', reply: '完整正文' } }),
        'data: [DONE]\n\n',
      ]),
    )
    vi.stubGlobal('fetch', fetchMock)

    const chunks: string[] = []
    const stages: StreamStageEvent[] = []
    const result = await generatePrdFromSummaryStream(
      { summary: SUMMARY, history: [] },
      { onChunk: (text) => chunks.push(text), onStage: (event) => stages.push(event) },
    )

    expect(chunks.join('')).toBe('# 标题\n\n## 2. 功能需求\n\n正文')
    expect(result.reply).toBe('完整正文')

    expect(stages).toHaveLength(2)
    expect(stages[0]).toEqual({
      stage: 'writing',
      sectionId: 'features',
      sectionTitle: '功能需求',
      sectionHeading: '## 2. 功能需求',
      index: 1,
      total: 7,
      note: '',
      issues: [],
    })
    // 跨块拼接后的改写事件：原因与问题清单都要完整解析出来
    expect(stages[1]?.stage).toBe('rewriting')
    expect(stages[1]?.note).toBe('编造了指标')
    expect(stages[1]?.issues).toEqual([{ severity: 'high', detail: '出现输入中没有的数据' }])
  })

  it('把 part 事件映射成前端字段：定稿文本可直接按序拼回最终正文', async () => {
    const fetchMock = vi.fn(async () =>
      streamResponse([
        sseFrame('part', { type: 'part', content: '', data: { text: '# 标题\n\n' } }),
        sseFrame('part', {
          type: 'part',
          content: '',
          data: {
            text: '## 2. 功能需求\n\n正文\n\n',
            section_id: 'features',
            section_title: '功能需求',
            section_heading: '## 2. 功能需求',
            index: 1,
            total: 7,
          },
        }),
        sseFrame('done', {
          type: 'done',
          content: '',
          data: { reply: '# 标题\n\n## 2. 功能需求\n\n正文' },
        }),
        'data: [DONE]\n\n',
      ]),
    )
    vi.stubGlobal('fetch', fetchMock)

    const parts: StreamDocumentPart[] = []
    const result = await generatePrdFromSummaryStream(
      { summary: SUMMARY },
      { onPart: (part) => parts.push(part) },
    )

    expect(parts).toHaveLength(2)
    // 文档头不属于任何章节：章节字段全部退化为默认值
    expect(parts[0]).toEqual({
      text: '# 标题\n\n',
      sectionId: '',
      sectionTitle: '',
      sectionHeading: '',
      index: 0,
      total: 0,
    })
    expect(parts[1]?.sectionId).toBe('features')
    expect(parts[1]?.sectionTitle).toBe('功能需求')
    expect(parts[1]?.index).toBe(1)
    // 契约：part 文本按序拼接后即最终正文（前端预览与落库正文同源）
    expect(parts.map((part) => part.text).join('').trim()).toBe(result.reply)
  })

  it('part 事件缺 text 时退化为空串，不影响后续事件', async () => {
    const fetchMock = vi.fn(async () =>
      streamResponse([
        sseFrame('part', { type: 'part', content: '', data: { section_id: 'features' } }),
        sseFrame('done', { type: 'done', content: '', data: { reply: '正文' } }),
      ]),
    )
    vi.stubGlobal('fetch', fetchMock)

    const parts: StreamDocumentPart[] = []
    const result = await generatePrdFromSummaryStream(
      { summary: SUMMARY },
      { onPart: (part) => parts.push(part) },
    )

    expect(parts).toHaveLength(1)
    expect(parts[0]?.text).toBe('')
    expect(result.reply).toBe('正文')
  })

  it('stage 事件字段缺失或类型不对时退化为默认值，不打断整条流', async () => {
    const fetchMock = vi.fn(async () =>
      streamResponse([
        sseFrame('stage', { type: 'stage', content: '', data: { stage: 'reviewing', index: '2' } }),
        sseFrame('chunk', { type: 'chunk', content: '仍然后续内容' }),
        sseFrame('done', { type: 'done', content: '', data: { reply: '仍然后续内容' } }),
      ]),
    )
    vi.stubGlobal('fetch', fetchMock)

    const stages: StreamStageEvent[] = []
    const result = await generatePrdFromSummaryStream(
      { summary: SUMMARY },
      { onStage: (event) => stages.push(event) },
    )

    expect(stages).toHaveLength(1)
    expect(stages[0]).toEqual({
      stage: 'reviewing',
      sectionId: '',
      sectionTitle: '',
      sectionHeading: '',
      index: 0, // 字符串型的 index 视为缺失
      total: 0,
      note: '',
      issues: [],
    })
    expect(result.reply).toBe('仍然后续内容')
  })

  it('后端下发 error 事件时以异常形式抛出（保持既有约定）', async () => {
    const fetchMock = vi.fn(async () =>
      streamResponse([sseFrame('error', { type: 'error', content: '生成失败：被截断', data: {} })]),
    )
    vi.stubGlobal('fetch', fetchMock)

    await expect(generatePrdFromSummaryStream({ summary: SUMMARY })).rejects.toThrow('生成失败：被截断')
  })
})

// ============================================================================
// 登录凭据的传递与 401 处理
//
// 这两件事出错时的症状都是「静默的」：不报错，只是表现为一直 401，
// 或者用户看到一句毫无信息量的「请求失败」。所以值得单独占一组用例。
// ============================================================================

describe('登录凭据与未登录处理', () => {
  const originalAdapter = http.defaults.adapter

  afterEach(() => {
    http.defaults.adapter = originalAdapter
  })

  /**
   * 造一个「返回 401」的 axios 适配器，用来驱动响应拦截器。
   *
   * 请求的 URL 由实际调用决定，这样每个用例测的就是它自己那个接口的真实路径。
   */
  function rejectWith401(detail: string) {
    return (async (config: InternalAxiosRequestConfig) => {
      const response = {
        status: 401,
        statusText: 'Unauthorized',
        headers: {},
        config,
        data: { detail },
      } as AxiosResponse
      throw new axios.AxiosError(detail, 'ERR_BAD_REQUEST', config, undefined, response)
    }) as never
  }

  it('axios 全局开启 withCredentials：将来直连后端时 Cookie 才会被带上', () => {
    expect(http.defaults.withCredentials).toBe(true)
  })

  it('SSE 请求带 credentials: same-origin，否则流式接口会稳定地 401', async () => {
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        streamResponse([sseFrame('done', { type: 'done', content: '', data: { reply: 'ok' } })]),
    )
    vi.stubGlobal('fetch', fetchMock)

    await generatePrdFromSummaryStream({ summary: SUMMARY })

    const [firstCall] = fetchMock.mock.calls
    expect(firstCall?.[1]).toMatchObject({ credentials: 'same-origin' })
  })

  it('流式接口 401 时抛 UnauthorizedError，并通知上层弹登录窗', async () => {
    const messages: string[] = []
    onUnauthorized((message) => messages.push(message))
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 401 })))

    await expect(generatePrdFromSummaryStream({ summary: SUMMARY })).rejects.toBeInstanceOf(
      UnauthorizedError,
    )
    expect(messages).toEqual(['请先登录后再使用该功能'])
  })

  it('业务接口 401 时抛 UnauthorizedError，并把后端文案原样交给上层', async () => {
    const messages: string[] = []
    onUnauthorized((message) => messages.push(message))
    http.defaults.adapter = rejectWith401('请先登录后再使用该功能')

    await expect(retrieveApiDocsRag({ prdContent: 'x' })).rejects.toBeInstanceOf(UnauthorizedError)
    expect(messages).toEqual(['请先登录后再使用该功能'])
  })

  it('认证接口自身的 401 不触发登录弹窗（密码错误不是「未登录」）', async () => {
    const messages: string[] = []
    onUnauthorized((message) => messages.push(message))
    http.defaults.adapter = rejectWith401('用户名或密码错误')

    await expect(http.post('/api/auth/login', {})).rejects.toThrow()
    expect(messages).toEqual([])
  })
})
