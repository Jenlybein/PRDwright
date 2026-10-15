import axios from 'axios'

import { ApiError, BACKEND_ERROR_CODES, toErrorCode } from '@/services/errors'
import type { BackendErrorCode } from '@/services/errors'
import type {
  ChatMessage,
  CompletedSection,
  DocKind,
  DocumentKind,
  JobSnapshot,
  PendingItem,
  QuestionsConfig,
  RequirementAnswers,
  RequirementsSummary,
  RunSummary,
  SkillRef,
  StepUsage,
  StreamDocumentPart,
  StreamStageEvent,
} from '@/types'

/**
 * 全局 axios 实例。
 * 请求走 Vite 的 /api 代理转发到后端，所以这里保持同源、不写死后端地址。
 *
 * `withCredentials` 是登录态的前提：后端把会话放在 HttpOnly Cookie 里，
 * 同源请求默认就会带上，但一旦将来接口跨域（或后端换到别的端口直连），
 * 少了这一项 Cookie 就不会被发送，表现为「明明登录了却一直 401」。
 */
const http = axios.create({
  baseURL: '/',
  timeout: 30_000,
  withCredentials: true,
  headers: {
    'Content-Type': 'application/json',
  },
})

// ============================================================================
// 未登录的统一处理
// ============================================================================

/**
 * 未登录（HTTP 401）错误。
 *
 * 单独建一个类型，是为了让调用方能区分「功能出故障了」与「你还没登录」——
 * 后者不该报错横幅，而应该弹登录窗。
 */
export class UnauthorizedError extends Error {
  constructor(message = '请先登录后再使用该功能') {
    super(message)
    this.name = 'UnauthorizedError'
  }
}

/** 认证接口自身的前缀。它们返回 401 是**正常业务结果**，不能当成「需要弹登录窗」 */
const AUTH_PREFIX = '/api/auth'

/** 收到 401 时的回调；由 AppV2 注册 */
let unauthorizedListener: ((message: string) => void) | null = null

/**
 * 注册「接口返回 401」的处理函数。
 *
 * 触发登录弹窗的是这个回调，而不是每个调用点各自判断——否则只要新增一个接口
 * 忘了处理，用户就只会看到一个莫名其妙的「请求失败」。
 */
export function onUnauthorized(listener: (message: string) => void): void {
  unauthorizedListener = listener
}

/**
 * 认证接口自己返回的 401 不通知上层。
 *
 * `/api/auth/login` 密码错误时返回 401（那是给用户的表单错误提示），
 * `/api/auth/me` 未登录时也返回 401（那是探测登录态的正常结果）——
 * 这两种都不该反过来再弹一次登录窗。
 */
function isAuthEndpoint(url: string | undefined): boolean {
  return typeof url === 'string' && url.startsWith(AUTH_PREFIX)
}

/** 从后端响应体里取出可直接展示给用户的错误说明 */
function readDetail(data: unknown): string {
  if (data && typeof data === 'object' && 'detail' in data) {
    const detail = (data as { detail: unknown }).detail
    if (typeof detail === 'string') return detail
  }
  return '请先登录后再使用该功能'
}

http.interceptors.response.use(
  (response) => response,
  (error) => {
    if (!axios.isAxiosError(error) || error.response?.status !== 401) {
      return Promise.reject(error)
    }

    const message = readDetail(error.response.data)
    if (!isAuthEndpoint(error.config?.url)) {
      unauthorizedListener?.(message)
      return Promise.reject(new UnauthorizedError(message))
    }
    return Promise.reject(error)
  },
)

/**
 * 获取需求澄清问卷配置。
 * 对应后端 `GET /api/conversation/questions`。
 */
export async function getQuestions(): Promise<QuestionsConfig> {
  const { data } = await http.get<QuestionsConfig>('/api/conversation/questions')
  return data
}

// ============================================================================
// 流式对话（SSE）
// ============================================================================

/** 后端推送的 SSE 事件，字段与 `backend/api/schemas.py` 的 `StreamEvent` 一一对应 */
export interface StreamEventPayload {
  /** 事件类型：状态提示 / 增量文本 / 定稿正文 / 阶段进度 / 结束 / 错误 */
  type:
    | 'status'
    | 'chunk'
    | 'part'
    | 'stage'
    | 'done'
    | 'error'
    | 'tool_call'
    | 'document'
    | 'snapshot'
    | 'run_summary'
  /** 文本内容（增量文本或提示语） */
  content: string
  /** 事件附加数据，如 conversation_id 与完整 reply */
  data: Record<string, unknown>
  /**
   * 机器可读的错误码，取值见 `services/errors.ts` 的 `BACKEND_ERROR_CODES`。
   *
   * 只有 `error` 事件会带。它在 SSE 帧里是**顶层**字段（不是 `data` 里的），
   * 因为这条通道上 HTTP 状态码恒为 200，判断「该不该降级重试」只能靠它。
   */
  code?: BackendErrorCode
}

/** 流式回复过程中可注册的回调 */
export interface StreamHandlers {
  /** 收到 status 提示语时触发 */
  onStatus?: (text: string) => void
  /** 收到增量文本时触发 */
  onChunk?: (text: string) => void
  /**
   * 收到 `part` 定稿正文事件时触发。
   * 目前只有「分章节生成 PRD」会下发：文档头一次、每一章一次。
   */
  onPart?: (part: StreamDocumentPart) => void
  /**
   * 收到 `stage` 阶段事件时触发。
   * 目前只有「分章节生成 PRD」会下发该事件，用于驱动前端的阶段指示，并在换章 /
   * 改写时重置本章的实时缓冲。
   */
  onStage?: (event: StreamStageEvent) => void
  /**
   * 收到任务流首发的 `snapshot` 事件时触发。
   * 目前只有生成任务（`/api/jobs/{id}/stream`）会下发：刷新重连时先用它补全
   * 断线前已产出的正文，再接 live 流。
   */
  onSnapshot?: (snapshot: JobSnapshot) => void
  /**
   * 收到任务收尾的 `run_summary` 事件时触发，携带本次生成的完整用量汇总。
   *
   * 终态任务重连时后端会**补发**一份，因此刷新页面后也拿得到，无需再查一次接口。
   */
  onRunSummary?: (summary: RunSummary) => void
}

/** 一次流式对话的结果 */
export interface StreamResult {
  /** 会话 ID；后端未下发时为空串 */
  conversationId: string
  /** 模型这一轮的完整回复 */
  reply: string
}

/** SSE 流的结束标记，收到后不再解析 */
const SSE_DONE = '[DONE]'

/** 从事件附加数据里安全地取字符串字段 */
function readString(data: Record<string, unknown>, key: string): string {
  const value = data[key]
  return typeof value === 'string' ? value : ''
}

/** 从事件附加数据里安全地取数字字段（缺失或类型不对时返回 0） */
function readNumber(data: Record<string, unknown>, key: string): number {
  const value = data[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

/** 从事件附加数据里安全地取字符串数组（缺失或类型不对时返回空数组，非字符串项被丢弃） */
function readStringArray(data: Record<string, unknown>, key: string): string[] {
  const value = data[key]
  if (!Array.isArray(value)) return []
  return value.filter((item): item is string => typeof item === 'string')
}

/** 逐项校验对象数组，只保留对象项（`null` 与原始值一律丢弃） */
function readObjectArray(data: Record<string, unknown>, key: string): Record<string, unknown>[] {
  const value = data[key]
  if (!Array.isArray(value)) return []
  return value.filter(
    (item): item is Record<string, unknown> => typeof item === 'object' && item !== null,
  )
}

/**
 * 把快照里的 `pending_items` 归一成 `PendingItem[]`。
 *
 * 与 `documentService.toPendingItems` 同一口径：只保留「有 id 且有 question」的项，
 * 坏数据（缺字段、非对象）直接丢弃——不该因为一条脏数据让整份快照失效。
 * 这是刷新重连时待确认清单的还原点，与 `documents.pending_items` 形状一致。
 */
function parsePendingItems(data: Record<string, unknown>): PendingItem[] {
  return readObjectArray(data, 'pending_items').flatMap((item) => {
    const id = item.id
    const question = item.question
    if (typeof id !== 'string' || typeof question !== 'string') return []
    return [
      {
        id,
        question,
        sections: readStringArray(item, 'sections'),
        decider: readString(item, 'decider'),
      },
    ]
  })
}

/**
 * 把 `stage` 事件的 `data` 归一成 `StreamStageEvent`。
 *
 * 后端字段是 snake_case，前端类型是 camelCase，转换收口在这里；
 * 任何一个字段缺失都不会让整条流断掉，只把那一个字段退化为默认值。
 */
function parseStageEvent(data: Record<string, unknown>): StreamStageEvent {
  const rawIssues = data.issues
  const issues = Array.isArray(rawIssues)
    ? rawIssues
        .filter((item): item is Record<string, unknown> => typeof item === 'object' && item !== null)
        .map((item) => ({
          severity: readString(item, 'severity'),
          detail: readString(item, 'detail'),
        }))
    : []

  return {
    stage: readString(data, 'stage'),
    sectionId: readString(data, 'section_id'),
    sectionTitle: readString(data, 'section_title'),
    sectionHeading: readString(data, 'section_heading'),
    index: readNumber(data, 'index'),
    total: readNumber(data, 'total'),
    note: readString(data, 'note'),
    issues,
  }
}

/**
 * 把 `part` 事件的 `data` 归一成 `StreamDocumentPart`。
 *
 * 与 `parseStageEvent` 同样的口径：字段缺失或类型不对只把那一个字段退化为默认值，
 * 绝不让一个坏字段打断整条流。`text` 是唯一的必需要素，缺了就退化成空串，
 * 由调用方决定是否忽略。
 */
function parsePartEvent(data: Record<string, unknown>): StreamDocumentPart {
  return {
    text: readString(data, 'text'),
    sectionId: readString(data, 'section_id'),
    sectionTitle: readString(data, 'section_title'),
    sectionHeading: readString(data, 'section_heading'),
    index: readNumber(data, 'index'),
    total: readNumber(data, 'total'),
  }
}

/**
 * 把任务流 `snapshot` 事件的 `data` 归一成 `JobSnapshot`。
 *
 * 与其它解析函数同一口径：字段缺失或类型不对只退化那一个字段，不打断整条流。
 * `completedSections` 会逐项校验，坏项被丢弃而不是让整段快照失效。
 */
function parseSnapshotEvent(data: Record<string, unknown>): JobSnapshot {
  const completedSections: CompletedSection[] = readObjectArray(data, 'completed_sections').map(
    (item) => ({
      sectionId: readString(item, 'section_id'),
      heading: readString(item, 'heading'),
      text: readString(item, 'text'),
    }),
  )

  const rawSolutionId = data.solution_id
  return {
    jobId: readNumber(data, 'job_id'),
    kind: readString(data, 'kind'),
    status: readString(data, 'status'),
    phase: readString(data, 'phase'),
    finalizedText: readString(data, 'finalized_text'),
    liveBuffer: readString(data, 'live_buffer'),
    completedSections,
    solutionId: typeof rawSolutionId === 'number' ? rawSolutionId : null,
    errorCode: readString(data, 'error_code'),
    errorMessage: readString(data, 'error_message'),
    pendingItems: parsePendingItems(data),
    elapsedMs: readNumber(data, 'elapsed_ms'),
  }
}

/** 把 `run_summary` 里的 `steps` 数组归一成 `StepUsage[]`（坏项丢弃，不打断整条流）。 */
function parseSteps(data: Record<string, unknown>): StepUsage[] {
  return readObjectArray(data, 'steps').map((item) => ({
    step: readString(item, 'step'),
    calls: readNumber(item, 'calls'),
    failedCalls: readNumber(item, 'failed_calls'),
    promptTokens: readNumber(item, 'prompt_tokens'),
    completionTokens: readNumber(item, 'completion_tokens'),
    durationMs: readNumber(item, 'duration_ms'),
  }))
}

/** 把 `run_summary` 里的 `injected_skills` 归一成 `SkillRef[]`（坏项丢弃）。 */
function parseSkills(data: Record<string, unknown>): SkillRef[] {
  return readObjectArray(data, 'injected_skills').map((item) => ({
    name: readString(item, 'name'),
    version: readString(item, 'version'),
    fingerprint: readString(item, 'fingerprint'),
  }))
}

/**
 * 把任务流 `run_summary` 事件的 `data` 归一成 `RunSummary`。
 *
 * 与其它解析函数同一口径：字段缺失或类型不对只退化那一个字段。`steps`、
 * `injected_skills`、`warnings` 逐项校验，坏项被丢弃而不是让整份摘要失效——用量是
 * **锦上添花**的信息，绝不能因为它取不到就把整条流打断。
 */
function parseRunSummary(data: Record<string, unknown>): RunSummary {
  return {
    kind: readString(data, 'kind'),
    status: readString(data, 'status'),
    durationMs: readNumber(data, 'duration_ms'),
    totalCalls: readNumber(data, 'total_calls'),
    failedCalls: readNumber(data, 'failed_calls'),
    promptTokens: readNumber(data, 'prompt_tokens'),
    completionTokens: readNumber(data, 'completion_tokens'),
    steps: parseSteps(data),
    injectedSkills: parseSkills(data),
    warnings: readStringArray(data, 'warnings'),
    provider: readString(data, 'provider'),
    model: readString(data, 'model'),
    requestId: readString(data, 'request_id'),
  }
}

/**
 * 解析单个 SSE 事件块。
 *
 * 后端每个事件都同时输出 `event:` 与 `data:` 两行，二者重复，这里以 `event:`
 * 为准、缺失时回退到 `data` 里的 `type` 字段；`data: [DONE]` 之类的结束标记与
 * 非法 JSON 都返回 `null`，避免一个坏帧打断整条流。
 */
function parseSseBlock(block: string): StreamEventPayload | null {
  const dataLines: string[] = []
  let eventName = ''

  for (const rawLine of block.split('\n')) {
    const line = rawLine.trimEnd()
    if (line.startsWith('event:')) {
      eventName = line.slice('event:'.length).trim()
    } else if (line.startsWith('data:')) {
      // 规范允许「data:」后跟一个空格，去掉它但保留内容自身的缩进
      dataLines.push(line.slice('data:'.length).replace(/^ /, ''))
    }
  }

  if (!dataLines.length) return null

  const payload = dataLines.join('\n')
  if (payload === SSE_DONE) return null

  try {
    const parsed = JSON.parse(payload) as Partial<StreamEventPayload>
    const type = parsed.type ?? eventName
    if (!type) return null
    return {
      type: type as StreamEventPayload['type'],
      content: parsed.content ?? '',
      data: parsed.data ?? {},
      // 只有真的带了这个字段才赋值：`code` 缺失与「码未知」是两回事，
      // 都由消费方统一降级成 INTERNAL_ERROR，这里不替它编一个值出来。
      ...(typeof parsed.code === 'string' ? { code: toErrorCode(parsed.code) } : {}),
    }
  } catch {
    return null
  }
}

/** 一次流式请求的描述：POST 带 body，GET 不带 */
interface StreamRequest {
  method: 'POST' | 'GET'
  path: string
  body?: unknown
  /** 可选的中止信号：`detach()` 用它退订（不取消服务端任务） */
  signal?: AbortSignal
}

/**
 * 执行一次 SSE 流式请求，读取并消费事件流（**POST 与 GET 共用同一套解析**）。
 *
 * axios 不支持读取流式响应体，所以这里直接用 `fetch` + `ReadableStream` 消费
 * SSE：按空行切分事件块，逐块解析后把各类事件回调给调用方。
 *
 * POST（文档生成）与 GET（生成任务订阅 `/api/jobs/{id}/stream`）只是请求方法不同，
 * 分帧 / `[DONE]` / 错误处理完全相同，因此收口在这一个函数里，绝不复制解析逻辑。
 *
 * @param request 请求描述（方法 / 路径 / 可选 body）
 * @param handlers 各类事件的回调
 * @returns 完整回复文本与事件附加数据里出现过的字段（如 conversation_id）
 * @throws 网络异常、HTTP 非 2xx，以及后端下发的 `error` 事件——后者抛 `ApiError`，
 *   调用方按 `code` 分支（见 `services/errors.ts`），不要匹配 message 里的中文文案
 */
async function readStreamRequest(
  request: StreamRequest,
  handlers: StreamHandlers = {},
): Promise<StreamResult> {
  const hasBody = request.body !== undefined
  const response = await fetch(request.path, {
    method: request.method,
    // 这里不能省：fetch 默认不发送 Cookie，而登录态就在 Cookie 里。
    // 少了它，流式接口会稳定地 401，且因为「普通接口没事」而极难定位。
    credentials: 'same-origin',
    headers: {
      ...(hasBody ? { 'Content-Type': 'application/json' } : {}),
      Accept: 'text/event-stream',
    },
    ...(hasBody ? { body: JSON.stringify(request.body) } : {}),
    ...(request.signal ? { signal: request.signal } : {}),
  })

  if (response.status === 401) {
    unauthorizedListener?.('请先登录后再使用该功能')
    throw new UnauthorizedError()
  }
  if (!response.ok) {
    throw new Error(`请求失败（HTTP ${response.status}）`)
  }
  if (!response.body) {
    throw new Error('当前浏览器不支持流式响应')
  }

  const reader = response.body.getReader()
  // stream: true 让解码器自行处理被切断的多字节字符（中文很常见）
  const decoder = new TextDecoder()
  let buffer = ''
  let conversationId = ''
  let reply = ''
  // 后端下发的 error 事件：HTTP 状态码是 200，分类信息全在事件的 code 里
  let failure: ApiError | null = null

  function handleBlock(block: string): void {
    const event = parseSseBlock(block)
    if (!event) return

    switch (event.type) {
      case 'status':
        handlers.onStatus?.(event.content)
        conversationId = readString(event.data, 'conversation_id') || conversationId
        break
      case 'chunk':
        handlers.onChunk?.(event.content)
        break
      case 'part':
        handlers.onPart?.(parsePartEvent(event.data))
        break
      case 'stage':
        handlers.onStage?.(parseStageEvent(event.data))
        break
      case 'done':
        conversationId = readString(event.data, 'conversation_id') || conversationId
        reply = readString(event.data, 'reply') || reply
        break
      case 'snapshot': {
        const snapshot = parseSnapshotEvent(event.data)
        // 尚未收到 done 前，用快照的定稿正文 + 实时缓冲先兜一份 reply：
        // 这样「任务已完成、只有快照没有 done」时也能拿到完整正文。
        if (!reply) reply = snapshot.finalizedText + snapshot.liveBuffer
        handlers.onSnapshot?.(snapshot)
        break
      }
      case 'run_summary':
        handlers.onRunSummary?.(parseRunSummary(event.data))
        break
      case 'error':
        // 文案（message）照旧给人和日志看，分支判断走 code：
        // 缺失或取值未知时降级为 INTERNAL_ERROR，绝不因为不认识的码而漏判
        failure = new ApiError(
          event.content || '生成失败',
          event.code ?? BACKEND_ERROR_CODES.INTERNAL_ERROR,
        )
        break
      default:
        break
    }
  }

  while (true) {
    const { done, value } = await reader.read()
    if (done) break

    buffer += decoder.decode(value, { stream: true })

    // SSE 事件块之间以空行分隔；最后一段可能被截断，留在缓冲区等下一次读取
    const blocks = buffer.split('\n\n')
    buffer = blocks.pop() ?? ''
    for (const block of blocks) handleBlock(block)
  }

  // 收尾：处理最后一段没有以空行结尾的内容
  handleBlock(buffer)

  // 直接抛 ApiError 而不是重新包一层 Error：调用方要按 code 分支，
  // 包一层就把错误码弄丢了
  if (failure) throw failure

  return { conversationId, reply }
}

/**
 * 提交问卷、开启会话，并以 SSE 流式接收模型的第一轮回复。
 * 对应后端 `POST /api/conversation/start-stream`。
 *
 * `answers` 的值允许是字符串数组：后端拿到的是 `dict[str, Any]`，
 * 服务层 `_normalize_value` 会把数组拼成「、」连接的文本。
 *
 * 提示词（系统人设与首轮开场白）由后端的对话档案决定，请求里不携带任何提示词字段。
 */
export async function startConversationStream(
  payload: { answers: RequirementAnswers; title?: string },
  handlers?: StreamHandlers,
): Promise<StreamResult> {
  return readStream(
    '/api/conversation/start-stream',
    {
      title: payload.title ?? '',
      answers: payload.answers,
    },
    handlers,
  )
}

/**
 * 在已有会话上接续对话。
 * 对应后端 `POST /api/conversation/continue-stream`。
 *
 * 不传 `conversationId` 时后端走无状态模式，需要由 `history` 回传完整历史；
 * 两者都传时以后端收到的 `history` 为准。人设由后端按档案标识解析，无状态模式下
 * 回退到默认档案，因此前端不需要补发提示词。
 */
export async function continueConversationStream(
  payload: { content: string; conversationId?: string; history?: ChatMessage[] },
  handlers?: StreamHandlers,
): Promise<StreamResult> {
  return readStream(
    '/api/conversation/continue-stream',
    {
      conversation_id: payload.conversationId ?? '',
      content: payload.content,
      // 只保留后端认识的 role / content 两个字段
      history: (payload.history ?? []).map(({ role, content }) => ({ role, content })),
    },
    handlers,
  )
}

// ============================================================================
// 结构化摘要回填与接口规范检索（一次性 JSON 接口）
// ============================================================================

/** 摘要回填响应：后端返回的是**完整**摘要（含未改动字段），Diff 由前端计算 */
export interface SummarySyncResult {
  summary: RequirementsSummary
  /** 后端给出的便利标志；判定是否有变更仍以前端 Diff 结果为准 */
  changed: boolean
}

/**
 * 从对话历史回填结构化需求摘要。
 * 对应后端 `POST /api/conversation/sync-summary-from-conversation`。
 */
export async function syncSummaryFromConversation(payload: {
  summary: RequirementsSummary
  history?: ChatMessage[]
}): Promise<SummarySyncResult> {
  const { data } = await http.post<SummarySyncResult>('/api/conversation/sync-summary-from-conversation', {
    summary: payload.summary,
    // 只保留后端认识的 role / content 两个字段
    history: (payload.history ?? []).map(({ role, content }) => ({ role, content })),
  })
  return data
}

// ============================================================================
// 文档流式生成（SSE）
// ============================================================================

/** 文档流式生成的请求体，字段与后端 schemas 的对应请求模型一致 */
export interface GeneratePrdStreamPayload {
  /** 需求澄清结果（问卷答案整理成的文本） */
  requirements: string
  /** 补充对话记录；为空则按无对话处理 */
  history?: ChatMessage[]
}

/**
 * 生成 PRD 的请求体（需求输入二选一）。
 *
 * `summary` 与 `requirements` 对应两条入口：完整流程传结构化摘要，
 * PRD 快捷入口传用户写下的一段话。后端把它们汇合到同一条生成链路，
 * 因此前端只需要按入口填其中一个字段。
 */
export interface GeneratePrdFromSummaryPayload {
  /** 结构化需求摘要（与 field-schema.json 对齐）；与 requirements 二选一 */
  summary?: RequirementsSummary
  /** 自由文本需求（PRD 快捷入口） */
  requirements?: string
  /** 产品名称，仅用于 PRD 文档头；摘要里已有 product_name 时以摘要为准 */
  productName?: string
  /** 补充对话记录；为空则按无对话处理 */
  history?: ChatMessage[]
}

/** 文档流式生成的结果：仅关心完整正文，会话 ID 恒为空串 */
export type DocumentStreamResult = StreamResult

/**
 * 根据对话生成 PRD，SSE 流式接收正文。
 * 对应后端 `POST /api/conversation/generate-prd-stream`。
 */
export function generatePrdStream(
  payload: GeneratePrdStreamPayload,
  handlers?: StreamHandlers,
): Promise<DocumentStreamResult> {
  return readStream(
    '/api/conversation/generate-prd-stream',
    {
      requirements: payload.requirements,
      // 只保留后端认识的 role / content 两个字段
      history: (payload.history ?? []).map(({ role, content }) => ({ role, content })),
    },
    handlers,
  )
}

/**
 * 由**需求**生成 PRD，SSE 流式接收正文。
 * 对应后端 `POST /api/conversation/generate-prd-from-summary-stream`。
 *
 * 需求可以是结构化摘要（完整流程）或自由文本（PRD 快捷入口），后端按输入形态
 * 选格式化函数后走同一条「分章节撰写 → 审核 → 改写」链路——「需求 → PRD」
 * 只有一个实现，多入口复用同一份契约。
 *
 * 流式过程中下发两类事件：`stage`（`onStage`）展示「写到第几章、是否在改写」，
 * `part`（`onPart`）在每段正文定稿时给出**可直接追加**的权威文本——预览由
 * 「已定稿部分 + 当前章实时缓冲」拼成，因此与最终 `reply` 逐字一致。
 */
export function generatePrdFromSummaryStream(
  payload: GeneratePrdFromSummaryPayload,
  handlers?: StreamHandlers,
): Promise<DocumentStreamResult> {
  return readStream(
    '/api/conversation/generate-prd-from-summary-stream',
    {
      // 二选一只下发有值的那个字段：显式传 null 会让后端的两条分支都要处理空对象
      summary: payload.summary ?? null,
      requirements: payload.requirements ?? '',
      product_name: payload.productName ?? '',
      // 只保留后端认识的 role / content 两个字段
      history: (payload.history ?? []).map(({ role, content }) => ({ role, content })),
    },
    handlers,
  )
}

/**
 * 根据 PRD 正文生成接口文档，SSE 流式接收正文。
 * 对应后端 `POST /api/conversation/generate-api-docs-stream`。
 *
 * 团队接口规范由技能包全量注入，前端不再检索、也不再随请求下发命中。
 */
export function generateApiDocsStream(
  payload: { prdContent: string },
  handlers?: StreamHandlers,
): Promise<DocumentStreamResult> {
  return readStream(
    '/api/conversation/generate-api-docs-stream',
    {
      prd_content: payload.prdContent,
    },
    handlers,
  )
}

/**
 * 根据 PRD 正文生成提示词套件，SSE 流式接收正文。
 * 对应后端 `POST /api/conversation/generate-prompts-stream`。
 */
export function generatePromptsStream(
  payload: { prdContent: string },
  handlers?: StreamHandlers,
): Promise<DocumentStreamResult> {
  return readStream(
    '/api/conversation/generate-prompts-stream',
    { prd_content: payload.prdContent },
    handlers,
  )
}

/**
 * 按用户指令优化文档，SSE 流式接收优化后的正文。
 * 对应后端 `POST /api/conversation/optimize-document-stream`。
 */
export function optimizeDocumentStream(
  payload: { document: string; instruction: string },
  handlers?: StreamHandlers,
): Promise<DocumentStreamResult> {
  return readStream(
    '/api/conversation/optimize-document-stream',
    {
      document: payload.document,
      instruction: payload.instruction,
    },
    handlers,
  )
}

/**
 * 兼容旧调用点：以 `POST` 发起一次流式请求。
 *
 * 保留这个名字是因为对话 / 文档生成的调用点都按「POST + body」使用它，
 * 内部转交给 `readStreamRequest`，解析逻辑只有一份。
 */
async function readStream(
  path: string,
  body: unknown,
  handlers: StreamHandlers = {},
): Promise<StreamResult> {
  return readStreamRequest({ method: 'POST', path, body }, handlers)
}

// ============================================================================
// 生成任务（jobs）：创建 / 查询 / 订阅
//
// 有了任务，生成与浏览器连接解耦：刷新 / 断线只断订阅，不中断任务；重连时先收
// `snapshot`（补全已产出正文）再接 live，实现页面恢复后的自动静默重连。
// ============================================================================

/** 后端 `kind` 用下划线，前端 `DocKind` 用连字符——这里是唯一的转换点 */
function toBackendKind(kind: DocKind): string {
  return kind === 'api-docs' ? 'api_docs' : kind
}

/** 创建任务响应 */
export interface JobCreated {
  /** 任务 id */
  jobId: number
  /** 初始状态，创建时恒为 running */
  status: string
}

/** 任务状态摘要（列表 / 查询共用；**不含正文**） */
export interface JobStatusSummary {
  id: number
  /** 后端原始 kind（下划线口径） */
  kind: string
  status: string
  phase: string
  solutionId: number | null
  errorCode: string
  errorMessage: string
  createdAt: string
  updatedAt: string
  /**
   * 该次生成的用量汇总；任务未收尾时后端为 `null`。
   *
   * 与 `run_summary` 事件同构，因此复用同一个解析函数，两处口径不会分叉。
   */
  usage: RunSummary | null
}

/** 把后端任务状态响应归一成前端结构（字段缺失给安全默认值） */
function toJobStatus(data: unknown): JobStatusSummary {
  const raw = (data ?? {}) as Record<string, unknown>
  const rawUsage = raw.usage
  return {
    id: typeof raw.id === 'number' ? raw.id : 0,
    kind: typeof raw.kind === 'string' ? raw.kind : '',
    status: typeof raw.status === 'string' ? raw.status : '',
    phase: typeof raw.phase === 'string' ? raw.phase : '',
    solutionId: typeof raw.solution_id === 'number' ? raw.solution_id : null,
    errorCode: typeof raw.error_code === 'string' ? raw.error_code : '',
    errorMessage: typeof raw.error_message === 'string' ? raw.error_message : '',
    createdAt: typeof raw.created_at === 'string' ? raw.created_at : '',
    updatedAt: typeof raw.updated_at === 'string' ? raw.updated_at : '',
    usage:
      typeof rawUsage === 'object' && rawUsage !== null
        ? parseRunSummary(rawUsage as Record<string, unknown>)
        : null,
  }
}

/** `createJob` 入参：`kind` + 各类型各自的字段（无关字段留空即可） */
export interface CreateJobPayload {
  kind: DocKind
  solutionId?: number | null
  /**
   * 本次任务**作用在**哪一份产物上；只有 `optimize` 需要。
   *
   * `kind` 回答「做了什么」（生成 / 优化），`docKind` 回答「改的是哪一份」——
   * 后端要落库就得知道后者。漏传时后端**不会**猜一个默认值，而是拒绝落库。
   */
  docKind?: DocumentKind
  summary?: RequirementsSummary | null
  requirements?: string
  productName?: string
  history?: ChatMessage[]
  prdContent?: string
  document?: string
  instruction?: string
}

/**
 * 创建一次生成任务并启动后台生成。
 * 对应后端 `POST /api/jobs`。
 */
export async function createJob(payload: CreateJobPayload): Promise<JobCreated> {
  const { data } = await http.post<{ job_id: number; status: string }>('/api/jobs', {
    kind: toBackendKind(payload.kind),
    solution_id: payload.solutionId ?? null,
    doc_kind: payload.docKind ?? null,
    summary: payload.summary ?? null,
    requirements: payload.requirements ?? '',
    product_name: payload.productName ?? '',
    // 只保留后端认识的 role / content 两个字段
    history: (payload.history ?? []).map(({ role, content }) => ({ role, content })),
    prd_content: payload.prdContent ?? '',
    document: payload.document ?? '',
    instruction: payload.instruction ?? '',
  })
  return {
    jobId: typeof data?.job_id === 'number' ? data.job_id : 0,
    status: typeof data?.status === 'string' ? data.status : 'running',
  }
}

/**
 * 读取单个任务的状态摘要。
 * 对应后端 `GET /api/jobs/{id}`。
 */
export async function fetchJob(jobId: number): Promise<JobStatusSummary> {
  const { data } = await http.get(`/api/jobs/${jobId}`)
  return toJobStatus(data)
}

/**
 * 按 owner + 过滤条件查询任务（兜底找回）。
 * 对应后端 `GET /api/jobs`。
 *
 * 典型用途：刷新后快照里没有 `activeJobId`，用
 * `{solutionId, kind, status:'running'}` 仍能找到那个还在跑的任务并重连。
 */
export async function fetchActiveJobs(filters: {
  solutionId?: number | null
  kind?: DocKind
  status?: string
}): Promise<JobStatusSummary[]> {
  const params: Record<string, string | number> = {}
  if (filters.solutionId != null) params.solution_id = filters.solutionId
  if (filters.kind) params.kind = toBackendKind(filters.kind)
  if (filters.status) params.status = filters.status
  const { data } = await http.get<{ jobs?: unknown[] }>('/api/jobs', { params })
  const jobs = Array.isArray(data?.jobs) ? data.jobs : []
  return jobs.map(toJobStatus)
}

/**
 * 订阅一个生成任务的事件流（SSE，GET）。
 * 对应后端 `GET /api/jobs/{id}/stream`。
 *
 * 订阅开始时后端会**先发一份 `snapshot`**（补全断线前已产出的正文），再接 live 事件；
 * 因此刷新后重连只需调用它，即可「静默」把进度接上。
 */
export function readJobStream(
  jobId: number,
  handlers?: StreamHandlers,
  signal?: AbortSignal,
): Promise<StreamResult> {
  return readStreamRequest(
    { method: 'GET', path: `/api/jobs/${jobId}/stream`, signal },
    handlers,
  )
}

export default http
