import axios from 'axios'

import type {
  ChatMessage,
  QuestionsConfig,
  RagHit,
  RequirementAnswers,
  RequirementsSummary,
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
  /** 文本内容（增量文本或提示语） */
  content: string
  /** 事件附加数据，如 conversation_id 与完整 reply */
  data: Record<string, unknown>
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
    }
  } catch {
    return null
  }
}

/**
 * 执行一次 SSE 流式请求，读取并消费事件流。
 *
 * axios 不支持读取流式响应体，所以这里直接用 `fetch` + `ReadableStream` 消费
 * SSE：按空行切分事件块，逐块解析后把状态提示与增量文本回调给调用方。
 *
 * @param path 接口路径，如 `/api/conversation/start-stream`
 * @param body 请求体
 * @param handlers 状态提示与增量文本的回调
 * @returns 完整回复文本与事件附加数据里出现过的字段（如 conversation_id）
 * @throws 网络异常、HTTP 非 2xx，以及后端下发的 `error` 事件
 */
async function readStream(
  path: string,
  body: unknown,
  handlers: StreamHandlers = {},
): Promise<StreamResult> {
  const response = await fetch(path, {
    method: 'POST',
    // 这里不能省：fetch 默认不发送 Cookie，而登录态就在 Cookie 里。
    // 少了它，流式接口会稳定地 401，且因为「普通接口没事」而极难定位。
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
    },
    body: JSON.stringify(body),
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
  let failure = ''

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
      case 'error':
        failure = event.content || '生成失败'
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

  if (failure) throw new Error(failure)

  return { conversationId, reply }
}

/**
 * 提交问卷、开启会话，并以 SSE 流式接收模型的第一轮回复。
 * 对应后端 `POST /api/conversation/start-stream`。
 *
 * `answers` 的值允许是字符串数组：后端拿到的是 `dict[str, Any]`，
 * 服务层 `_normalize_value` 会把数组拼成「、」连接的文本。
 *
 * `systemPrompt` 用于给本次会话换一套人设（如澄清对话专用提示词），后端会把它
 * 存在会话记录里，后续接续对话自动沿用。
 */
export async function startConversationStream(
  payload: { answers: RequirementAnswers; title?: string; systemPrompt?: string },
  handlers?: StreamHandlers,
): Promise<StreamResult> {
  return readStream(
    '/api/conversation/start-stream',
    {
      title: payload.title ?? '',
      answers: payload.answers,
      system_prompt_override: payload.systemPrompt ?? '',
    },
    handlers,
  )
}

/**
 * 在已有会话上接续对话。
 * 对应后端 `POST /api/conversation/continue-stream`。
 *
 * 不传 `conversationId` 时后端走无状态模式，需要由 `history` 回传完整历史；
 * 两者都传时以后端收到的 `history` 为准。无状态模式下后端拿不到会话记录，
 * 因此系统提示词人设要靠 `systemPrompt` 重新下发。
 */
export async function continueConversationStream(
  payload: { content: string; conversationId?: string; history?: ChatMessage[]; systemPrompt?: string },
  handlers?: StreamHandlers,
): Promise<StreamResult> {
  return readStream(
    '/api/conversation/continue-stream',
    {
      conversation_id: payload.conversationId ?? '',
      content: payload.content,
      // 只保留后端认识的 role / content 两个字段
      history: (payload.history ?? []).map(({ role, content }) => ({ role, content })),
      system_prompt_override: payload.systemPrompt ?? '',
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

/** 接口规范检索响应 */
export interface ApiDocsRagResult {
  hits: RagHit[]
}

/**
 * 检索与 PRD 相关的团队接口规范与历史接口示例。
 * 对应后端 `POST /api/conversation/retrieve-api-docs-rag`。
 */
export async function retrieveApiDocsRag(payload: {
  prdContent: string
  history?: ChatMessage[]
  limit?: number
}): Promise<ApiDocsRagResult> {
  const { data } = await http.post<ApiDocsRagResult>('/api/conversation/retrieve-api-docs-rag', {
    prd_content: payload.prdContent,
    history: (payload.history ?? []).map(({ role, content }) => ({ role, content })),
    limit: payload.limit ?? 5,
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
 * `ragHits` 为用户在前端确认过的检索命中，会作为「参考规范」随请求下发。
 */
export function generateApiDocsStream(
  payload: { prdContent: string; ragHits?: RagHit[] },
  handlers?: StreamHandlers,
): Promise<DocumentStreamResult> {
  return readStream(
    '/api/conversation/generate-api-docs-stream',
    {
      prd_content: payload.prdContent,
      rag_hits: payload.ragHits ?? [],
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

export default http
