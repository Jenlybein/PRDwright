import axios, { type AxiosResponse, type InternalAxiosRequestConfig } from 'axios'
import { afterEach, describe, expect, it, vi } from 'vitest'

import http, {
  continueConversationStream,
  createJob,
  fetchActiveJobs,
  generateApiDocsStream,
  generatePrdFromSummaryStream,
  onUnauthorized,
  readJobStream,
  UnauthorizedError,
} from '../services/api'
import { ApiError } from '../services/errors'
import type {
  ChatMessage,
  JobSnapshot,
  RunSummary,
  StreamDocumentPart,
  StreamStageChunk,
  StreamStageEvent,
  RequirementsSummary,
  ToolResultPayload,
} from '../types'

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

const SUMMARY = { product_name: 'PRDwright' } as RequirementsSummary

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
      replace: false,
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

  it('把 stage_chunk 映射成阶段输出增量：带上归属阶段，且不污染正文 chunk', async () => {
    // 审核 / 改写这两步是整篇量级的静默调用，靠 stage_chunk 让用户看见进展。
    // 它必须与正文 chunk 走两条通道——混在一起审核意见就会跑进文档预览里。
    const fetchMock = vi.fn(async () =>
      streamResponse([
        sseFrame('stage', { type: 'stage', content: '正在生成', data: { stage: 'writing' } }),
        sseFrame('chunk', { type: 'chunk', content: '# 接口文档\n', data: {} }),
        sseFrame('stage', { type: 'stage', content: '正在审核', data: { stage: 'reviewing' } }),
        sseFrame('stage_chunk', { type: 'stage_chunk', content: '{"verdict":', data: { stage: 'reviewing' } }),
        sseFrame('stage_chunk', { type: 'stage_chunk', content: '"pass"}', data: { stage: 'reviewing' } }),
        sseFrame('done', { type: 'done', content: '', data: { reply: '# 接口文档' } }),
        'data: [DONE]\n\n',
      ]),
    )
    vi.stubGlobal('fetch', fetchMock)

    const body: string[] = []
    const stageChunks: StreamStageChunk[] = []
    const phases: string[] = []
    const result = await generateApiDocsStream(
      { prdContent: '# PRD' },
      {
        onChunk: (text) => body.push(text),
        onStage: (event) => phases.push(event.stage),
        onStageChunk: (chunk) => stageChunks.push(chunk),
      },
    )

    // 阶段序列以 writing 开头：进度条要能立刻亮起
    expect(phases).toEqual(['writing', 'reviewing'])
    expect(stageChunks).toEqual([
      { stage: 'reviewing', text: '{"verdict":' },
      { stage: 'reviewing', text: '"pass"}' },
    ])
    // 正文通道只有文档本身，审核 JSON 没有混进去
    expect(body.join('')).toBe('# 接口文档\n')
    expect(result.reply).toBe('# 接口文档')
  })
})

// ============================================================================
// error 事件的错误码
//
// 这条通道上 HTTP 状态码恒为 200，调用方只能靠 `code` 分支。因此除了「码要传过来」，
// 更要保证「码传不过来时不会崩、也不会漏判」——未知与缺失都降级为 INTERNAL_ERROR。
// ============================================================================

describe('SSE error 事件的错误码', () => {
  /** 跑一次流式请求并把抛出的异常交回来（而不是让断言卡在 rejects 的匹配上） */
  async function catchStreamError(blocks: string[]): Promise<unknown> {
    vi.stubGlobal('fetch', vi.fn(async () => streamResponse(blocks)))
    return generatePrdFromSummaryStream({ summary: SUMMARY }).catch((error: unknown) => error)
  }

  it('error 事件带 code 时抛 ApiError，错误码原样保留', async () => {
    const error = await catchStreamError([
      sseFrame('error', {
        type: 'error',
        content: '会话不存在或已过期：conv-1',
        code: 'SESSION_NOT_FOUND',
        data: {},
      }),
    ])

    expect(error).toBeInstanceOf(ApiError)
    // 分支判断只看 code；message 只是给人看的
    expect((error as ApiError).code).toBe('SESSION_NOT_FOUND')
  })

  it('error 事件缺失 code 时降级为 INTERNAL_ERROR', async () => {
    const error = await catchStreamError([
      sseFrame('error', { type: 'error', content: '生成失败：未知原因', data: {} }),
    ])

    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).code).toBe('INTERNAL_ERROR')
  })

  it('error 事件的 code 取值未知时降级为 INTERNAL_ERROR', async () => {
    const error = await catchStreamError([
      sseFrame('error', {
        type: 'error',
        content: '生成失败：未知原因',
        // 模拟后端先上线了新码、前端还没同步
        code: 'SOMETHING_NEW',
        data: {},
      }),
    ])

    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).code).toBe('INTERNAL_ERROR')
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

  it('认证接口自身的 401 不触发登录弹窗（密码错误不是「未登录」）', async () => {
    const messages: string[] = []
    onUnauthorized((message) => messages.push(message))
    http.defaults.adapter = rejectWith401('用户名或密码错误')

    await expect(http.post('/api/auth/login', {})).rejects.toThrow()
    expect(messages).toEqual([])
  })
})

// ============================================================================
// 生成任务（jobs）
//
// 有了任务，生成与浏览器连接解耦：刷新 / 断线只断订阅；重连时先收 `snapshot`
// 再收 live。这里钉住三件事——请求方法 / 凭据、`snapshot` 的字段映射、以及
// `run_summary` 的完整契约。任何一处映射错位都不会报错，只会静默丢进度或丢用量。
// ============================================================================

describe('生成任务接口（jobs）', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('createJob 以 POST /api/jobs 下发，kind 转成后端下划线口径', async () => {
    const post = vi
      .spyOn(http, 'post')
      .mockResolvedValue({ data: { job_id: 11, status: 'running' } } as never)

    const created = await createJob({
      kind: 'api-docs',
      solutionId: 3,
      prdContent: '# PRD',
    })

    expect(post).toHaveBeenCalledTimes(1)
    const [url, body] = post.mock.calls[0] ?? []
    expect(url).toBe('/api/jobs')
    const payload = body as Record<string, unknown>
    // 前端用连字符、后端用下划线：转换只在 api.ts 一处发生
    expect(payload.kind).toBe('api_docs')
    expect(payload.solution_id).toBe(3)
    expect(payload.prd_content).toBe('# PRD')
    expect(created).toEqual({ jobId: 11, status: 'running' })
  })

  it('readJobStream 以 GET 订阅任务流，带凭据、带中止信号且不带 Content-Type', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      streamResponse([
        sseFrame('snapshot', {
          type: 'snapshot',
          content: '',
          data: {
            job_id: 5,
            kind: 'prd',
            status: 'running',
            phase: 'writing',
            finalized_text: '# 产品需求文档\n\n',
            live_buffer: '半句',
            completed_sections: [
              { section_id: 'overview', heading: '## 1. 产品概述', text: '## 1. 产品概述\n\n正文\n\n' },
            ],
            solution_id: 3,
            error_code: '',
            error_message: '',
            elapsed_ms: 4500,
          },
        }),
        sseFrame('run_summary', {
          type: 'run_summary',
          content: '',
          data: {
            kind: 'prd',
            status: 'completed',
            duration_ms: 1234,
            total_calls: 3,
            // 中途有过一次失败并被自愈回来
            failed_calls: 1,
            prompt_tokens: 900,
            completion_tokens: 300,
            steps: [
              {
                step: 'prd:writing',
                calls: 2,
                failed_calls: 1,
                prompt_tokens: 800,
                completion_tokens: 250,
                duration_ms: 900,
              },
              {
                step: 'prd:reviewing',
                calls: 1,
                failed_calls: 0,
                prompt_tokens: 100,
                completion_tokens: 50,
                duration_ms: 300,
              },
            ],
            injected_skills: [
              { name: 'prd-generator', version: '0.1.0', fingerprint: 'e4f928df7be9' },
            ],
            warnings: ['prd:writing 的输入约 96,000 tokens，已达预算上限 120,000 的 80%'],
            provider: 'ark',
            model: 'doubao-seed-2-1-turbo-260628',
            request_id: 'req-abc',
          },
        }),
        sseFrame('done', { type: 'done', content: '', data: { reply: '# 产品需求文档\n\n半句' } }),
        'data: [DONE]\n\n',
      ]),
    )
    vi.stubGlobal('fetch', fetchMock)

    const controller = new AbortController()
    const snapshots: JobSnapshot[] = []
    const summaries: RunSummary[] = []
    const result = await readJobStream(
      5,
      { onSnapshot: (snap) => snapshots.push(snap), onRunSummary: (item) => summaries.push(item) },
      controller.signal,
    )

    const [path, init] = fetchMock.mock.calls[0] ?? []
    expect(path).toBe('/api/jobs/5/stream')
    expect(init).toMatchObject({ method: 'GET', credentials: 'same-origin' })
    // 订阅是 GET，不带 body，因此也不该带 Content-Type
    expect(init?.headers).not.toHaveProperty('Content-Type')
    // 退订靠这个信号：abort 只断本地读取，服务端任务照跑
    expect(init?.signal).toBe(controller.signal)

    expect(snapshots).toEqual([
      {
        jobId: 5,
        kind: 'prd',
        status: 'running',
        phase: 'writing',
        finalizedText: '# 产品需求文档\n\n',
        liveBuffer: '半句',
        // 阶段输出（审核意见 / 改写稿）：PRD 快照不带，退化为空串
        stageText: '',
        completedSections: [
          { sectionId: 'overview', heading: '## 1. 产品概述', text: '## 1. 产品概述\n\n正文\n\n' },
        ],
        solutionId: 3,
        errorCode: '',
        errorMessage: '',
        // 刷新重连时把「上次生成留下的待确认清单」一并带回；这份快照没生成过，故为空
        pendingItems: [],
        partial: false,
        failedSections: [],
        // 服务端算的已用时：刷新重连后用它给本地计时器打底，显示的仍是任务真实耗时
        elapsedMs: 4500,
      },
    ])
    // run_summary 的完整契约：任务状态 + 用量汇总（含按步骤拆分）
    expect(summaries).toEqual([
      {
        kind: 'prd',
        status: 'completed',
        durationMs: 1234,
        totalCalls: 3,
        failedCalls: 1,
        promptTokens: 900,
        completionTokens: 300,
        steps: [
          {
            step: 'prd:writing',
            calls: 2,
            failedCalls: 1,
            promptTokens: 800,
            completionTokens: 250,
            durationMs: 900,
          },
          {
            step: 'prd:reviewing',
            calls: 1,
            failedCalls: 0,
            promptTokens: 100,
            completionTokens: 50,
            durationMs: 300,
          },
        ],
        injectedSkills: [{ name: 'prd-generator', version: '0.1.0', fingerprint: 'e4f928df7be9' }],
        warnings: ['prd:writing 的输入约 96,000 tokens，已达预算上限 120,000 的 80%'],
        provider: 'ark',
        model: 'doubao-seed-2-1-turbo-260628',
        requestId: 'req-abc',
        partial: false,
        failedSections: [],
      },
    ])
    expect(result.reply).toBe('# 产品需求文档\n\n半句')
  })

  it('run_summary 的 steps、注入技能与预算告警逐项校验，坏项被丢弃而不让整份摘要失效', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        streamResponse([
          sseFrame('run_summary', {
            type: 'run_summary',
            content: '',
            data: {
              kind: 'api_docs',
              status: 'completed',
              duration_ms: 800,
              // 第一项不是对象、末一项字段类型不对：都不该让整份摘要失败
              steps: ['坏项', { step: 'api_docs', calls: 1 }, 42],
              injected_skills: [
                '坏项',
                { name: 'prd-generator', version: '0.1.0', fingerprint: 'e4f928df7be9' },
                42,
              ],
              // 告警同理：非字符串项丢弃，合法项保留
              warnings: ['api_docs 的输入已达预算上限的 88%', 7],
              provider: 'ark',
              model: 'm',
              request_id: '',
            },
          }),
          'data: [DONE]\n\n',
        ]),
      ),
    )

    const summaries: RunSummary[] = []
    await readJobStream(7, { onRunSummary: (item) => summaries.push(item) })

    expect(summaries).toHaveLength(1)
    const summary = summaries[0]!
    expect(summary.kind).toBe('api_docs')
    expect(summary.totalCalls).toBe(0)
    // 非对象项被丢弃，字段缺失的项按零补
    expect(summary.steps).toEqual([
      {
        step: 'api_docs',
        calls: 1,
        failedCalls: 0,
        promptTokens: 0,
        completionTokens: 0,
        durationMs: 0,
      },
    ])
    // 非对象项被丢弃，合法项保留且缺失字段按空串补
    expect(summary.injectedSkills).toEqual([
      { name: 'prd-generator', version: '0.1.0', fingerprint: 'e4f928df7be9' },
    ])
    expect(summary.warnings).toEqual(['api_docs 的输入已达预算上限的 88%'])
    expect(summary.requestId).toBe('')
  })

  it('snapshot 的 completed_sections 逐项校验，坏项被丢弃而不打断整条流', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        streamResponse([
          sseFrame('snapshot', {
            type: 'snapshot',
            content: '',
            data: {
              job_id: 6,
              // 第一项不是对象：应被丢弃，而不是让整段快照失效
              completed_sections: ['坏项', { section_id: 'overview', heading: '## 1', text: '正文' }],
            },
          }),
          sseFrame('done', { type: 'done', content: '', data: { reply: '正文' } }),
        ]),
      ),
    )

    const snapshots: JobSnapshot[] = []
    await readJobStream(6, { onSnapshot: (snap) => snapshots.push(snap) })

    expect(snapshots[0]?.completedSections).toEqual([
      { sectionId: 'overview', heading: '## 1', text: '正文' },
    ])
    // 缺字段各退化为自己的默认值
    expect(snapshots[0]?.status).toBe('')
    expect(snapshots[0]?.solutionId).toBeNull()
  })

  it('只有 snapshot 没有 done 时，用快照的定稿正文 + 实时缓冲兜底作为 reply', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        streamResponse([
          sseFrame('snapshot', {
            type: 'snapshot',
            content: '',
            data: { job_id: 8, status: 'completed', finalized_text: '# PRD\n\n', live_buffer: '半句' },
          }),
        ]),
      ),
    )

    const result = await readJobStream(8)

    // 任务在断线期间跑完了：没有 done 也要能拿到完整正文
    expect(result.reply).toBe('# PRD\n\n半句')
  })

  it('snapshot 带上当前阶段输出，刷新后审核面板能接着展示', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        streamResponse([
          sseFrame('snapshot', {
            type: 'snapshot',
            content: '',
            data: {
              job_id: 9,
              kind: 'api_docs',
              status: 'running',
              phase: 'reviewing',
              finalized_text: '',
              live_buffer: '# 接口文档\n\n',
              stage_text: '{"verdict":"revise"}',
            },
          }),
        ]),
      ),
    )

    const snapshots: JobSnapshot[] = []
    await readJobStream(9, { onSnapshot: (snap) => snapshots.push(snap) })

    expect(snapshots[0]?.phase).toBe('reviewing')
    expect(snapshots[0]?.stageText).toBe('{"verdict":"revise"}')
  })

  it('fetchActiveJobs 按过滤条件拼查询参数，kind 用下划线口径', async () => {
    const get = vi
      .spyOn(http, 'get')
      .mockResolvedValue({ data: { jobs: [{ id: 7, kind: 'api_docs', status: 'running' }] } } as never)

    const jobs = await fetchActiveJobs({ solutionId: 3, kind: 'api-docs', status: 'running' })

    expect(get).toHaveBeenCalledTimes(1)
    const [url, config] = get.mock.calls[0] ?? []
    expect(url).toBe('/api/jobs')
    expect((config as { params: Record<string, unknown> }).params).toEqual({
      solution_id: 3,
      kind: 'api_docs',
      status: 'running',
    })
    expect(jobs[0]).toMatchObject({ id: 7, kind: 'api_docs', status: 'running' })
  })
})

describe('结构化选择题的解析（done 事件的 clarification_asks）', () => {
  /**
   * 用一帧 `done` 跑完解析并返回结果。
   *
   * 解析器是各流式入口共用的，这里借用现成的 job 流入口覆盖它，不必再搭一套会话流。
   * ⚠️ SSE 帧的形状是 `{type, content, data}`——载荷在**内层** `data` 里，
   * 放外层会被静默忽略（表现为「什么都没解析出来」）。
   */
  async function readDone(payload: Record<string, unknown>) {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => streamResponse([sseFrame('done', { type: 'done', content: '', data: payload })])),
    )
    return readJobStream(1)
  }

  it('把结构化块解析成带标号的题目', async () => {
    const result = await readDone({
      job_id: 1,
      status: 'completed',
      reply: '你更倾向哪种？',
      clarification_asks: [
        {
          id: 'ask-1',
          question: '第一版要不要做离线同步？',
          background: '你提到用户常在弱网环境使用。',
          multiple: false,
          options: [
            { label: 'A', text: '第一版就做离线同步' },
            { label: 'B', text: '第一版只做在线' },
          ],
        },
      ],
    })

    expect(result.reply).toBe('你更倾向哪种？')
    expect(result.asks).toEqual([
      {
        id: 'ask-1',
        question: '第一版要不要做离线同步？',
        background: '你提到用户常在弱网环境使用。',
        multiple: false,
        options: [
          { label: 'A', text: '第一版就做离线同步' },
          { label: 'B', text: '第一版只做在线' },
        ],
      },
    ])
  })

  it('没有这个键时给空数组（生成线本来就不带它，不该因此报错）', async () => {
    const result = await readDone({ job_id: 1, status: 'completed', reply: '正文' })

    expect(result.asks).toEqual([])
    expect(result.reply).toBe('正文')
  })

  it('逐项丢弃坏数据：缺题目、选项不足、选项没有文字', async () => {
    const result = await readDone({
      job_id: 1,
      status: 'completed',
      reply: '正文',
      clarification_asks: [
        { question: '   ', options: [{ text: '甲' }, { text: '乙' }] },
        { question: '只有一个选项', options: [{ text: '甲' }] },
        { question: '选项没有文字', options: [{ text: '  ' }, { text: 42 }] },
        '不是对象',
        { question: '这道是好的', options: [{ text: '甲' }, { text: '乙' }] },
      ],
    })

    expect(result.asks.map((item) => item.question)).toEqual(['这道是好的'])
  })

  it('后端没给标号时按顺序补 A/B，且 multiple 只认真正的布尔真', async () => {
    const result = await readDone({
      job_id: 1,
      status: 'completed',
      reply: '正文',
      clarification_asks: [
        {
          question: '缺标号',
          multiple: 'true',
          options: [{ text: '甲' }, { text: '乙' }],
        },
      ],
    })

    expect(result.asks[0]?.options.map((option) => option.label)).toEqual(['A', 'B'])
    // 字符串形式的 "true" 不能把单选题渲染成多选
    expect(result.asks[0]?.multiple).toBe(false)
  })

  it('done 带 tool_call_id / tool_calls 时被原样读出来', async () => {
    const result = await readDone({
      reply: '正文',
      tool_call_id: 'call-1',
      tool_calls: [
        { id: 'call-1', name: 'ask_clarification_questions', args: { questions: [] } },
      ],
    })

    expect(result.toolCallId).toBe('call-1')
    expect(result.toolCalls).toEqual([
      { id: 'call-1', name: 'ask_clarification_questions', args: { questions: [] } },
    ])
  })

  it('题目上的 tool_call_id 只在非空字符串时才挂（空串 / 缺失一律不带这个键）', async () => {
    const result = await readDone({
      reply: '正文',
      clarification_asks: [
        { question: '有 id', options: [{ text: '甲' }, { text: '乙' }], tool_call_id: 'call-9' },
        { question: '空串 id', options: [{ text: '甲' }, { text: '乙' }], tool_call_id: '' },
        { question: '缺 id', options: [{ text: '甲' }, { text: '乙' }] },
      ],
    })

    expect(result.asks[0]).toHaveProperty('tool_call_id', 'call-9')
    expect(result.asks[1]).not.toHaveProperty('tool_call_id')
    expect(result.asks[2]).not.toHaveProperty('tool_call_id')
  })

  it('没有工具链时结果上**不挂**这两个键（区分「没有」与「空」）', async () => {
    const result = await readDone({ reply: '正文' })

    expect(result).not.toHaveProperty('toolCallId')
    expect(result).not.toHaveProperty('toolCalls')
  })

  it('tool_calls 逐项校验：缺 id / 缺 name 的丢弃，args 非法退化为空对象', async () => {
    const result = await readDone({
      reply: '正文',
      tool_calls: [
        '不是对象',
        { name: '缺 id' },
        { id: '', name: '空 id' },
        { id: 'call-1' },
        { id: 'call-2', name: 'args 是字符串', args: '{"a":1}' },
        { id: 'call-3', name: '好', args: { questions: [1] } },
      ],
    })

    expect(result.toolCalls).toEqual([
      { id: 'call-2', name: 'args 是字符串', args: {} },
      { id: 'call-3', name: '好', args: { questions: [1] } },
    ])
  })
})

// ============================================================================
// 结构化作答回灌（continue-stream 的 tool_result）
//
// 后端把答案作为工具结果回给当初提问的那次工具调用，不再重复投喂这段人话。
// 这里钉住三件事：`tool_result` 只在有值时带键；history **不能**裁掉助手消息上的
// `tool_calls`（裁了后端就只剩悬空的 tool_call，上游 API 直接报错）；history 里
// **不出现** `tool_call_id` / `tool_result`——工具应答由后端自己落，前端不产生。
// ============================================================================

describe('结构化作答回灌（continue-stream）', () => {
  const TOOL_RESULT: ToolResultPayload = {
    tool_call_id: 'call-1',
    answers: [
      {
        ask_id: 'ask-1',
        labels: ['A', 'C'],
        question: '首发要支持哪些端？',
        texts: ['网页版', '桌面客户端'],
      },
    ],
  }

  /** 跑一次接续对话并把实际发出的请求体交回来 */
  async function continueWith(
    payload: Parameters<typeof continueConversationStream>[0],
  ): Promise<Record<string, unknown>> {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      void init
      return streamResponse([sseFrame('done', { type: 'done', content: '', data: { reply: 'ok' } })])
    })
    vi.stubGlobal('fetch', fetchMock)
    await continueConversationStream(payload)
    const [, init] = fetchMock.mock.calls[0] ?? []
    return JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>
  }

  it('带 toolResult 时把它放进请求体的 tool_result', async () => {
    const body = await continueWith({ content: '展示文本', toolResult: TOOL_RESULT })

    expect(body.tool_result).toEqual(TOOL_RESULT)
    // content 仍要传：它是气泡里给人看的展示文本
    expect(body.content).toBe('展示文本')
  })

  it('不带 toolResult 时请求体没有 tool_result 这个键（走普通续聊）', async () => {
    const body = await continueWith({ content: '普通消息' })

    expect(body).not.toHaveProperty('tool_result')
    expect(body.content).toBe('普通消息')
  })

  it('history 保留 assistant 消息上的 tool_calls（后端配对工具应答的依据）', async () => {
    const history: ChatMessage[] = [
      { role: 'user', content: '先聊点别的' },
   {
        role: 'assistant',
        content: '你更倾向哪种？',
        tool_calls: [
          { id: 'call-1', name: 'ask_clarification_questions', args: { questions: [] } },
        ],
      },
      { role: 'assistant', content: '普通回复' },
    ]

    const body = await continueWith({ content: '继续', history })

    expect(body.history).toEqual([
      { role: 'user', content: '先聊点别的' },
      {
        role: 'assistant',
        content: '你更倾向哪种？',
        tool_calls: [
          { id: 'call-1', name: 'ask_clarification_questions', args: { questions: [] } },
        ],
      },
      // 普通消息不挂空壳：多一个空数组会让后端以为这里也有工具链
      { role: 'assistant', content: '普通回复' },
    ])
  })

  it('history 里不出现 tool_call_id / tool_result：前端不产生 tool 消息', async () => {
    // `ToolMessage` 由后端拿请求体的 `tool_result` 自己落，前端只负责把 assistant 的
    // `tool_calls` 带回去供它配对。让这两个键出现在 history 里，等于把「悬空应答」
    // 的风险搬回前端——所以显式钉住它们**不会**出现。
    const history: ChatMessage[] = [
      { role: 'assistant', content: '你更倾向哪种？', stage: 'requirement' },
      { role: 'user', content: '答：A', stage: 'requirement' },
    ]

    const body = await continueWith({ content: '继续', history })
    const items = body.history as Record<string, unknown>[]

    expect(items).toEqual([
      { role: 'assistant', content: '你更倾向哪种？' },
      { role: 'user', content: '答：A' },
    ])
    for (const item of items) {
      expect(item).not.toHaveProperty('tool_call_id')
      expect(item).not.toHaveProperty('tool_result')
    }
  })
})
