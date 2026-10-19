import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useClarificationChat, type ChatHooks } from '@/composables/useClarificationChat'
import { continueConversationStream, startConversationStream } from '@/services/api'
import { ApiError } from '@/services/errors'
import { useWorkflowStore } from '@/stores/workflow'
import type { ChatMessage, ChatStage, ToolResultPayload } from '@/types'

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

/** 接续对话的请求载荷，只留这里要断言的字段 */
interface ContinueCall {
  content: string
  conversationId?: string
  history?: unknown[]
  toolResult?: unknown
}

/** 一条「问过选题」的助手消息；`tool_calls` 是后端配对工具应答的依据 */
const ASKED: ChatMessage = {
  role: 'assistant',
  content: '你更倾向哪种做法？',
  stage: 'requirement',
  tool_calls: [{ id: 'call-1', name: 'ask_clarification_questions', args: { questions: [] } }],
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
    history: payload.history,
    toolResult: payload.toolResult,
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
      .mockResolvedValueOnce({ conversationId: '', reply: '重试后的回复', asks: [] })

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
      .mockResolvedValueOnce({ conversationId: '', reply: '好的', asks: [] })

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
      asks: [],
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
      asks: [],
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
      asks: [],
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

describe('结构化作答的回灌', () => {
  const TOOL_RESULT: ToolResultPayload = {
    tool_call_id: 'call-1',
    answers: [
      {
        ask_id: 'ask-1',
        labels: ['A'],
        question: '第一版要不要做离线同步？',
        texts: ['第一版就做离线同步'],
      },
    ],
  }

  it('sendMessage 带上 toolResult 时把它交给接口', async () => {
    const store = useWorkflowStore()
    store.messages = []
    const chat = makeChat('requirement')
    vi.mocked(continueConversationStream).mockResolvedValue({
      conversationId: '',
      reply: '好',
      asks: [],
    })

    await chat.sendMessage('展示文本', TOOL_RESULT)

    const call = continueCalls()[0]
    expect(call?.toolResult).toEqual(TOOL_RESULT)
    expect(call?.content).toBe('展示文本')
  })

  it('不带 toolResult 时接口收不到它（普通续聊，行为与先前一致）', async () => {
    const store = useWorkflowStore()
    store.messages = []
    const chat = makeChat('requirement')
    vi.mocked(continueConversationStream).mockResolvedValue({
      conversationId: '',
      reply: '好',
      asks: [],
    })

    await chat.sendMessage('普通消息')

    expect(continueCalls()[0]?.toolResult).toBeUndefined()
  })

  it('点选项卡提交时推入的消息带 answers（界面据此渲染成回执而不是气泡）', async () => {
    const store = useWorkflowStore()
    store.messages = []
    const chat = makeChat('requirement')
    vi.mocked(continueConversationStream).mockResolvedValue({
      conversationId: '',
      reply: '好',
      asks: [],
    })

    await chat.sendMessage(
      '1. 第一版要不要做离线同步？\n   答：A. 第一版就做离线同步',
      TOOL_RESULT,
      TOOL_RESULT.answers,
    )

    // 推入的第一条是用户消息（第二条是随后固化的助手回复）
    expect(store.messages[0]).toEqual({
      role: 'user',
      content: '1. 第一版要不要做离线同步？\n   答：A. 第一版就做离线同步',
      stage: 'requirement',
      answers: TOOL_RESULT.answers,
    })
  })

  it('自由文本作答的消息**没有** answers 这个键（仍是普通气泡）', async () => {
    const store = useWorkflowStore()
    store.messages = []
    const chat = makeChat('requirement')
    vi.mocked(continueConversationStream).mockResolvedValue({
      conversationId: '',
      reply: '好',
      asks: [],
    })

    await chat.sendMessage('我直接说说我的想法')

    // 推入的第一条是用户消息（第二条是随后固化的助手回复）
    const user = store.messages[0]
    expect(user).not.toHaveProperty('answers')
    // 与既有精确形状断言一致：普通消息就只有这三个键
    expect(user).toEqual({
      role: 'user',
      content: '我直接说说我的想法',
      stage: 'requirement',
    })
  })

  it('降级（题目缺 tool_call_id）时消息**仍然**带 answers：界面不退成气泡', async () => {
    const store = useWorkflowStore()
    store.messages = []
    const chat = makeChat('requirement')
    vi.mocked(continueConversationStream).mockResolvedValue({
      conversationId: '',
      reply: '好',
      asks: [],
    })

    // 拿不到工具调用 id 只影响**模型**那一路（`toolResult` 不發）；
    // 界面那一路照旧，因为用户看不到模型收到了什么。
    await chat.sendMessage(
      '1. 第一版要不要做离线同步？\n   答：A. 第一版就做离线同步',
      undefined,
      [
        {
          ask_id: 'ask-1',
          labels: ['A'],
          question: '第一版要不要做离线同步？',
          texts: ['第一版就做离线同步'],
        },
      ],
    )

    expect(store.messages[0]).toEqual({
      role: 'user',
      content: '1. 第一版要不要做离线同步？\n   答：A. 第一版就做离线同步',
      stage: 'requirement',
      answers: [
        {
          ask_id: 'ask-1',
        labels: ['A'],
          question: '第一版要不要做离线同步？',
        texts: ['第一版就做离线同步'],
        },
      ],
    })
    // 同时：请求体里不该有 tool_result（降级的两面一起钉住）
    expect(continueCalls()[0]?.toolResult).toBeUndefined()
  })

  it('回传的历史保留 assistant 的 tool_calls（裁掉会让后端看到悬空的 tool_call）', async () => {
    const store = useWorkflowStore()
    store.messages = [{ role: 'user', content: '开始吧', stage: 'requirement' }, ASKED]
    const chat = makeChat('requirement')
    vi.mocked(continueConversationStream).mockResolvedValue({
      conversationId: '',
      reply: '好',
      asks: [],
    })

    await chat.sendMessage('答：A')

    expect(continueCalls()[0]?.history).toEqual([
      { role: 'user', content: '开始吧' },
      // stage 本来就不回传（后端不认），但 tool_calls 必须在
      { role: 'assistant', content: '你更倾向哪种做法？', tool_calls: ASKED.tool_calls },
    ])
  })

  it('done 下发的 tool_calls 挂到固化后的助手消息上', async () => {
    const store = useWorkflowStore()
    store.messages = []
    const chat = makeChat('requirement')
    vi.mocked(startConversationStream).mockResolvedValue({
      conversationId: 'conv-1',
      reply: '你更倾向哪种做法？',
      asks: [
        {
          id: 'ask-1',
          question: '第一版要不要做离线同步？',
          background: '',
          multiple: false,
          options: [
            { label: 'A', text: '第一版就做离线同步' },
            { label: 'B', text: '第一版只做在线' },
          ],
          tool_call_id: 'call-1',
        },
      ],
      toolCallId: 'call-1',
      toolCalls: [
        { id: 'call-1', name: 'ask_clarification_questions', args: { questions: [] } },
      ],
    })

    await chat.startConversationWithAnswers({ product_name: 'X' })

    expect(store.messages[store.messages.length - 1]).toEqual({
      role: 'assistant',
      content: '你更倾向哪种做法？',
      stage: 'requirement',
      asks: [
        {
          id: 'ask-1',
          question: '第一版要不要做离线同步？',
          background: '',
          multiple: false,
          options: [
            { label: 'A', text: '第一版就做离线同步' },
            { label: 'B', text: '第一版只做在线' },
          ],
          tool_call_id: 'call-1',
        },
      ],
      tool_calls: [{ id: 'call-1', name: 'ask_clarification_questions', args: { questions: [] } }],
    })
    // 工具应答（tool 角色）由后端自己落：这里不该出现那两个字段
    expect(store.messages[store.messages.length - 1]).not.toHaveProperty('tool_call_id')
    expect(store.messages[store.messages.length - 1]).not.toHaveProperty('tool_result')
  })

  it('固化后的助手消息不带 tool_call_id / tool_result（前端不产生 tool 消息）', async () => {
    const store = useWorkflowStore()
    store.messages = []
    const chat = makeChat('requirement')
    vi.mocked(startConversationStream).mockResolvedValue({
      conversationId: 'conv-1',
      reply: '信息已经足够',
      asks: [],
      toolCallId: 'call-1',
      toolCalls: [
        { id: 'call-1', name: 'ask_clarification_questions', args: { questions: [] } },
      ],
    })

    await chat.startConversationWithAnswers({ product_name: 'X' })

    const last = store.messages[store.messages.length - 1]
    // 工具应答是后端自己落的：前端只回传 assistant 发起的调用，不伪造 tool 角色消息
    expect(last).not.toHaveProperty('tool_call_id')
    expect(last).not.toHaveProperty('tool_result')
    expect(last?.tool_calls).toEqual([
      { id: 'call-1', name: 'ask_clarification_questions', args: { questions: [] } },
    ])
  })

  it('没有工具链时助手消息**不带** tool_calls 这个键（保持既有精确形状断言不红）', async () => {
    const store = useWorkflowStore()
    store.messages = []
    const chat = makeChat('requirement')
    vi.mocked(startConversationStream).mockResolvedValue({
      conversationId: 'conv-1',
      reply: '信息已经足够',
      asks: [],
    })

    await chat.startConversationWithAnswers({ product_name: 'X' })

    expect(store.messages[store.messages.length - 1]).not.toHaveProperty('tool_calls')
  })
})

describe('结构化选择题随助手消息固化', () => {
  const ASKS = [
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
  ]

  it('后端下发的选项挂到那条助手消息上，供消息列表渲染卡片', async () => {
    const store = useWorkflowStore()
    vi.mocked(startConversationStream).mockResolvedValue({
      conversationId: 'conv-1',
      reply: '你更倾向哪种做法？',
      asks: ASKS,
    })

    await makeChat('requirement').startConversationWithAnswers({ product_name: 'X' })

    const last = store.messages[store.messages.length - 1]
    expect(last?.asks).toEqual(ASKS)
    expect(last?.content).toBe('你更倾向哪种做法？')
  })

  it('没有选项时消息**不带**这个字段（既不渲染卡片，也不弄脏消息形状）', async () => {
    const store = useWorkflowStore()
    vi.mocked(startConversationStream).mockResolvedValue({
      conversationId: 'conv-1',
      reply: '信息已经足够，可以点击生成 PRD',
      asks: [],
    })

    await makeChat('requirement').startConversationWithAnswers({ product_name: 'X' })

    const last = store.messages[store.messages.length - 1]
    expect(last).not.toHaveProperty('asks')
  })

  it('选项只跟着本线程：另一条线程的 displayMessages 里不会冒出卡片', async () => {
    vi.mocked(startConversationStream).mockResolvedValue({
      conversationId: 'conv-1',
      reply: '你更倾向哪种做法？',
      asks: ASKS,
    })

    const requirement = makeChat('requirement')
    const prd = makeDocChat('prd-clarify')
    await requirement.startConversationWithAnswers({ product_name: 'X' })

    expect(requirement.displayMessages.value.some((message) => message.asks?.length)).toBe(true)
    expect(prd.displayMessages.value).toEqual([])
  })
})
