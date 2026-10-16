import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useClarificationChat, type ChatHooks } from '@/composables/useClarificationChat'
import { continueConversationStream, startConversationStream } from '@/services/api'
import { ApiError } from '@/services/errors'
import { useWorkflowStore } from '@/stores/workflow'
import type { ChatStage } from '@/types'

/**
 * 澄清对话：**线程隔离**与降级重试。
 *
 * 四条澄清线程（需求 / PRD / 接口 / 提示词）共用 store 里那**一个** `messages` 数组，
 * 靠每条消息上的 `stage` 分线；每条线程一个 composable 实例，各自持有流式缓冲与错误提示。
 * 这套设计的失效方式几乎全是**静默**的——清错线程会「聊过的内容没了」，用错缓冲会
 * 「切过去看见一条假的思考中气泡」，两者都不报错。因此这里重点守三件事：
 *
 * 1. **只动本线程**：开新会话只清本线程的消息，发消息只往本线程追加；
 * 2. **只读本线程**：`displayMessages` 过滤到本线程，且流式气泡用**本实例**的标志判断
 *    （不是 store 里那个全局 `chatStreaming`——用它会让别的线程凭空多出一条空气泡）；
 * 3. **会话 id 各记各的**：写 `conversationIds[stage]` 只动自己那一个键。
 *
 * 另有一组用例守**降级重试的判据**：服务端会话存在内存里，重启后 ID 失效，后端在
 * HTTP 200 的 SSE 流里下发 `SESSION_NOT_FOUND`，前端据此清掉 ID 退化为无状态重试。
 * 改造前这里匹配的是「会话不存在」这句中文，后端改一个字降级就静默失效——因此第一条
 * 用例的 `message` 是随手写的一句话，里面根本没有「会话不存在」。
 *
 * `api` 模块整体 mock：本文件测的是编排（要不要重试、重试时带不带 ID、清哪几条消息），
 * 不是 SSE 解析——那有专门的 `api.spec.ts`。
 */
vi.mock('@/services/api', () => ({
  continueConversationStream: vi.fn(),
  startConversationStream: vi.fn(),
}))

/** 四条线程共用的外部能力；本文件不关心提示语与阶段切换 */
const hooks: ChatHooks = { onNotice: () => undefined, onEnterChat: () => undefined }

/** 接续对话的请求载荷，只留这里要断言的两个字段 */
interface ContinueCall {
  content: string
  conversationId?: string
}

/** 建一条线程的对话实例（不传记录参数——记录与 id 都在 store 里按 `stage` 取） */
function makeChat(stage: ChatStage) {
  return useClarificationChat(hooks, { stage })
}

/** 文档澄清线程的实例：共用后端 `doc-clarification` 档案、且不清历史 */
function makeDocChat(stage: ChatStage) {
  return useClarificationChat(hooks, { profileKey: 'doc-clarification', stage, clearHistory: false })
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
  it('任意中文文案 + SESSION_NOT_FOUND 都会触发无状态重试并清掉该线程的会话 ID', async () => {
    const store = useWorkflowStore()
    store.messages = []
    store.conversationIds = { ...store.conversationIds, requirement: 'conv-expired' }
    const chat = makeChat('requirement')

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
    expect(store.conversationIds.requirement).toBe('')
    // 重试成功，不该留下错误提示
    expect(chat.chatError.value).toBe('')
    // 落进消息列表的 assistant 消息带着本线程标记
    expect(store.messages[store.messages.length - 1]).toEqual({
      role: 'assistant',
      content: '重试后的回复',
      stage: 'requirement',
    })
  })

  it('code 不是 SESSION_NOT_FOUND 时不重试，直接把错误交给上层', async () => {
    const store = useWorkflowStore()
    store.messages = []
    store.conversationIds = { ...store.conversationIds, requirement: 'conv-alive' }
    const chat = makeChat('requirement')
    vi.mocked(continueConversationStream).mockRejectedValue(
      new ApiError('上游模型调用失败', 'LLM_UPSTREAM_ERROR'),
    )

    await chat.sendMessage('继续聊')

    expect(continueCalls()).toHaveLength(1)
    // 会话 ID 没被清掉：这次失败不是「会话没了」
    expect(store.conversationIds.requirement).toBe('conv-alive')
    expect(chat.chatError.value).toBe('上游模型调用失败')
    expect(startConversationStream).not.toHaveBeenCalled()
  })

  it('降级重试仍带着档案标识，文档澄清不会悄悄换回默认人设', async () => {
    const store = useWorkflowStore()
    store.messages = []
    store.conversationIds = { ...store.conversationIds, 'prd-clarify': 'conv-expired' }
    const chat = makeDocChat('prd-clarify')

    vi.mocked(continueConversationStream)
      .mockRejectedValueOnce(new ApiError('随便写点什么', 'SESSION_NOT_FOUND'))
      .mockResolvedValueOnce({ conversationId: '', reply: '好的' })

    await chat.sendMessage('继续聊')

    const calls = vi.mocked(continueConversationStream).mock.calls
    expect(calls).toHaveLength(2)
    expect(calls[0]?.[0].profileKey).toBe('doc-clarification')
    // 第二次（无状态重试）同样要点名档案，否则人设在这里就丢了
    expect(calls[1]?.[0].profileKey).toBe('doc-clarification')
  })
})

describe('四条线程互不串线', () => {
  it('开新会话只清本线程的历史，别的线程一条不动', async () => {
    const store = useWorkflowStore()
    store.messages = [
      { role: 'user', content: '需求 A' },
      { role: 'assistant', content: '答 A' },
      { role: 'user', content: 'PRD 那边的问题', stage: 'prd-clarify' },
    ]
    const chat = makeChat('requirement')
    vi.mocked(startConversationStream).mockResolvedValue({
      conversationId: 'conv-new',
      reply: '第一轮追问',
    })

    await chat.startConversationWithAnswers({ product_name: 'X' }, '正在生成回复')

    // 需求线程被清空后重开；PRD 线程那条原样留着
    expect(store.messages).toEqual([
      { role: 'user', content: 'PRD 那边的问题', stage: 'prd-clarify' },
      { role: 'assistant', content: '第一轮追问', stage: 'requirement' },
    ])
    // 会话 id 只写自己那一个键
    expect(store.conversationIds.requirement).toBe('conv-new')
    expect(store.conversationIds['prd-clarify']).toBe('')
  })

  it('clearHistory: false 时保留本线程已有历史，接着往下聊', async () => {
    const store = useWorkflowStore()
    store.messages = [
      { role: 'user', content: '需求 A' },
      { role: 'user', content: '这个字段到底怎么定', stage: 'api-docs-clarify' },
    ]
    const chat = makeDocChat('api-docs-clarify')
    vi.mocked(startConversationStream).mockResolvedValue({
      conversationId: 'conv-doc',
      reply: '我们逐条来看',
    })

    await chat.startConversationWithAnswers({ document: '正文' }, '正在梳理')

    // 三条消息都在：需求线程那条没被误伤，本线程那条也留着，后面接上开场白
    expect(store.messages.map((message) => message.content)).toEqual([
      '需求 A',
      '这个字段到底怎么定',
      '我们逐条来看',
    ])
  })

  it('传给后端的历史只含本线程（别线程的对话不能当成本轮上下文）', async () => {
    const store = useWorkflowStore()
    store.messages = [
      { role: 'user', content: '需求 A' },
      { role: 'assistant', content: '答 A' },
      { role: 'user', content: 'PRD 那边的问题', stage: 'prd-clarify' },
      { role: 'assistant', content: 'PRD 那边的回答', stage: 'prd-clarify' },
    ]
    const chat = makeChat('requirement')
    vi.mocked(continueConversationStream).mockResolvedValue({
      conversationId: '',
      reply: '好',
    })

    await chat.sendMessage('继续聊需求')

    const payload = vi.mocked(continueConversationStream).mock.calls[0]?.[0]
    expect(payload?.history).toEqual([
      { role: 'user', content: '需求 A' },
      { role: 'assistant', content: '答 A' },
    ])
  })

  it('displayMessages 只含本线程；别的线程在跑流式时这边不会冒出假气泡', async () => {
    const store = useWorkflowStore()
    store.messages = [
      { role: 'user', content: '需求 A' },
      { role: 'assistant', content: '答 A' },
      { role: 'user', content: 'PRD 那边的问题', stage: 'prd-clarify' },
    ]
    const requirement = makeChat('requirement')
    const prd = makeDocChat('prd-clarify')

    expect(requirement.displayMessages.value.map((message) => message.content)).toEqual([
      '需求 A',
      '答 A',
    ])
    expect(prd.displayMessages.value.map((message) => message.content)).toEqual(['PRD 那边的问题'])

    // 让请求永远挂着：流式状态停在 true，缓冲里一个字都没有
    vi.mocked(continueConversationStream).mockImplementation(() => new Promise(() => undefined))
    void requirement.sendMessage('在吗')

    expect(requirement.streaming.value).toBe(true)
    // 本线程末尾多出一条流式气泡（内容还空着，列表自己渲染「思考中」）
    const last = requirement.displayMessages.value[requirement.displayMessages.value.length - 1]
    expect(last).toMatchObject({
      role: 'assistant',
      content: '',
      streaming: true,
    })
    // 关键：另一条线程完全不受影响——用全局 `chatStreaming` 判据的话，这里会多出一条空气泡
    expect(prd.streaming.value).toBe(false)
    expect(prd.displayMessages.value.map((message) => message.content)).toEqual(['PRD 那边的问题'])
  })
})
