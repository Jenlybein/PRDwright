import axios from 'axios'

import type { ChatMessage, QuestionsConfig } from '@/types'

/**
 * 全局 axios 实例。
 * 请求走 Vite 的 /api 代理转发到后端，所以这里保持同源、不写死后端地址。
 */
const http = axios.create({
  baseURL: '/',
  timeout: 30_000,
  headers: {
    'Content-Type': 'application/json',
  },
})

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
  /** 事件类型：状态提示 / 增量文本 / 结束 / 错误，后两种为后续扩展预留 */
  type: 'status' | 'chunk' | 'done' | 'error' | 'tool_call' | 'document'
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
 * 执行一次流式对话请求。
 *
 * axios 不支持读取流式响应体，所以这里直接用 `fetch` + `ReadableStream` 消费
 * SSE：按空行切分事件块，逐块解析后把增量文本回调给调用方。
 *
 * @param path 接口路径，如 `/api/conversation/start-stream`
 * @param body 请求体
 * @param handlers 状态提示与增量文本的回调
 * @returns 会话 ID 与完整回复
 * @throws 网络异常、HTTP 非 2xx，以及后端下发的 `error` 事件
 */
async function streamChat(
  path: string,
  body: unknown,
  handlers: StreamHandlers = {},
): Promise<StreamResult> {
  const response = await fetch(path, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
    },
    body: JSON.stringify(body),
  })

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
 */
export async function startConversationStream(
  payload: { answers: Record<string, string>; title?: string },
  handlers?: StreamHandlers,
): Promise<StreamResult> {
  return streamChat(
    '/api/conversation/start-stream',
    { title: payload.title ?? '', answers: payload.answers },
    handlers,
  )
}

/**
 * 在已有会话上接续对话。
 * 对应后端 `POST /api/conversation/continue-stream`。
 *
 * 不传 `conversationId` 时后端走无状态模式，需要由 `history` 回传完整历史；
 * 两者都传时以后端收到的 `history` 为准。
 */
export async function continueConversationStream(
  payload: { content: string; conversationId?: string; history?: ChatMessage[] },
  handlers?: StreamHandlers,
): Promise<StreamResult> {
  return streamChat(
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

export default http
