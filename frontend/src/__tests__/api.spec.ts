import { describe, expect, it, vi, afterEach } from 'vitest'

import { generatePrdFromSummaryStream } from '../services/api'
import type { StreamStageEvent, RequirementsSummary } from '../types'

/**
 * SSE 解析测试。
 *
 * 重点覆盖新加的 `stage` 事件：后端发的是 snake_case（`section_id` / `section_title`），
 * 前端类型是 camelCase，映射错了不会报错、只会静默显示错章节——所以值得单独测。
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
