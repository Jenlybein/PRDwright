import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useClarificationChat } from '@/composables/useClarificationChat'
import { continueConversationStream, startConversationStream } from '@/services/api'
import { ApiError } from '@/services/errors'
import { useWorkflowStore } from '@/stores/workflow'

/**
 * 澄清对话的降级重试。
 *
 * 这里最要紧的一点是**判据与文案解耦**：服务端会话存在内存里，重启后 ID 失效，
 * 后端会在 HTTP 200 的 SSE 流里下发 `SESSION_NOT_FOUND`，前端据此清掉 ID 退化为
 * 无状态重试。改造前这里匹配的是「会话不存在」这句中文，后端改一个字降级就静默
 * 失效——那类失效没有任何报错，只能靠一条**用任意文案构造**的用例兜住：
 * 下面第一例的 `message` 是随手写的一句话，里面根本没有「会话不存在」，
 * 只要它还能触发重试，就证明判定已经不依赖文案了。
 *
 * `api` 模块整体 mock：本文件测的是编排（要不要重试、重试时带不带 ID），
 * 不是 SSE 解析——那有专门的 `api.spec.ts`。
 */
vi.mock('@/services/api', () => ({
  continueConversationStream: vi.fn(),
  startConversationStream: vi.fn(),
}))

/** 接续对话的请求载荷，只留这里要断言的两个字段 */
interface ContinueCall {
  content: string
  conversationId?: string
}

/** 建一个带指定会话 ID 的对话环境 */
function setupChat(conversationId: string) {
  const store = useWorkflowStore()
  store.messages = []
  store.conversationId = conversationId

  const chat = useClarificationChat({
    onNotice: () => undefined,
    onEnterChat: () => undefined,
  })
  return { store, chat }
}

/** 取出接续请求实际发出的载荷序列（直接读 mock 记录，不依赖实现里是否自增计数） */
function continueCalls(): ContinueCall[] {
  return vi.mocked(continueConversationStream).mock.calls.map(([payload]) => ({
    content: payload.content,
    conversationId: payload.conversationId,
  }))
}

beforeEach(() => {
  setActivePinia(createPinia())
  // resetAllMocks 而不是 clearAllMocks：`mockRejectedValueOnce` 这类一次性实现
  // 若上一个用例没消费完，会残留到下一个用例里，让失败原因变得极难定位。
  vi.resetAllMocks()
})

describe('会话失效后的降级重试', () => {
  it('任意中文文案 + SESSION_NOT_FOUND 都会触发无状态重试并清掉会话 ID', async () => {
    const { store, chat } = setupChat('conv-expired')

    vi.mocked(continueConversationStream)
      .mockRejectedValueOnce(new ApiError('随便写点什么', 'SESSION_NOT_FOUND'))
      // 无状态模式：后端不下发新的会话 ID
      .mockResolvedValueOnce({ conversationId: '', reply: '重试后的回复' })

    await chat.sendMessage('继续聊')

    // 关键：判据是 code，与 message 里的文案无关
    const calls = continueCalls()
    expect(calls).toHaveLength(2)
    // 第一次带上失效 ID，第二次退化为无状态（不带 ID、改由 history 回传）
    expect(calls[0]?.conversationId).toBe('conv-expired')
    expect(calls[1]?.conversationId).toBeUndefined()
    expect(calls[1]?.content).toBe('继续聊')
    // 失效 ID 已被清掉
    expect(store.conversationId).toBe('')
    // 重试成功，不该留下错误提示
    expect(chat.chatError.value).toBe('')
    expect(store.messages[store.messages.length - 1]).toEqual({
      role: 'assistant',
      content: '重试后的回复',
    })
  })

  it('code 不是 SESSION_NOT_FOUND 时不重试，直接把错误交给上层', async () => {
    const { store, chat } = setupChat('conv-alive')
    vi.mocked(continueConversationStream).mockRejectedValue(
      new ApiError('上游模型调用失败', 'LLM_UPSTREAM_ERROR'),
    )

    await chat.sendMessage('继续聊')

    expect(continueCalls()).toHaveLength(1)
    // 会话 ID 没被清掉：这次失败不是「会话没了」
    expect(store.conversationId).toBe('conv-alive')
    expect(chat.chatError.value).toBe('上游模型调用失败')
    expect(startConversationStream).not.toHaveBeenCalled()
  })
})
