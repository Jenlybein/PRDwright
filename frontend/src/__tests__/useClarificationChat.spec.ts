import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useClarificationChat } from '../composables/useClarificationChat'
import * as api from '../services/api'
import { useRunsStore } from '../stores/runs'
import { useSolutionsStore } from '../stores/solutions'
import { useWorkflowStore } from '../stores/workflow'
import * as solutionApi from '../services/solutionService'
import type { ChatMessage, ChatStage, RequirementsSummary } from '../types'

/**
 * 对话流程（`useClarificationChat`）的用例。
 *
 * 这一版与上一版的**根本差别**：对话是一个后端任务。因此这里盯的不再是
 * 「增量怎么拼成一条消息」，而是三件属于前端的事：
 *
 * 1. 提交的形状对不对（`stage` / `chat_mode` / `content` / `tool_result`）；
 * 2. 渲染归属对不对（只看本线程；别的线程在跑不会冒气泡）；
 * 3. 收尾之后**从服务端重拉**，而不是自己拼一条消息。
 *
 * 消息的持久化、工具链配对、幂等——那些已经搬到后端（`tests/test_chat_job.py`），
 * 在前端重复断言只会让两处口径慢慢分叉。
 */

vi.mock('../services/api', () => ({
  createJob: vi.fn(),
  readJobStream: vi.fn(),
  cancelJob: vi.fn(),
}))

// 方案的保存链路也要 mock：`useJob.start` 在创建任务前后各 flush 一次，
// 少了这两个接口，`saveNow` 会在 `updateSolution` 上抛 TypeError，
// 注意力会被引到完全无关的地方。
vi.mock('../services/solutionService', () => ({
  fetchTurns: vi.fn(async () => []),
  clearTurns: vi.fn(async () => {}),
  createSolution: vi.fn(async () => ({
    id: 7,
    title: '',
    mode: '',
    stage: '',
    created_at: '',
    updated_at: '',
    content_updated_at: '',
  })),
  // 返回 `null` 是「方案已被删除」的信号，会触发停自动保存 + 清运行态。
  // 这里要返回一份正常详情，否则测的就不是对话了。
  updateSolution: vi.fn(async (id: number) => ({
    id,
    title: '',
    mode: '',
    stage: '',
    created_at: '',
    updated_at: '',
    content_updated_at: '',
    state: {
      view_state: '',
      entry_mode: '',
      chat_tab: '',
      summary: null,
      quick_product_name: '',
      quick_requirements: '',
      draft_prd_text: '',
      paste_prd_text: '',
      paste_api_docs_text: '',
      active_job_id: null,
      active_job_kind: '',
      active_job_doc_kind: '',
    },
  })),
  fetchSolution: vi.fn(async () => null),
  fetchSolutions: vi.fn(async () => []),
}))

const solutionId = 7

/** 一份填得完整的摘要：`buildAnswers` 会读 `target_users.length` 这类字段 */
const sampleSummary = {
  product_name: '周报',
  product_goal: '把周报自动化',
  target_users: ['运营'],
  platform: '网页应用',
  mvp_features: [{ name: '问卷填写', description: '收集需求' }],
} as unknown as RequirementsSummary

function makeChat(stage: ChatStage = 'requirement') {
  const notices: string[] = []
  let entered = 0
  const chat = useClarificationChat(
    { onNotice: (text) => notices.push(text), onEnterChat: () => (entered += 1) },
    { stage },
  )
  return { chat, notices, entered: () => entered }
}

function seedTurns(messages: ChatMessage[]): void {
  useWorkflowStore().setTurns(messages)
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  vi.mocked(api.createJob).mockResolvedValue({ jobId: 42, status: 'running' })
  vi.mocked(api.readJobStream).mockResolvedValue({
    conversationId: '',
    reply: '好，我明白了。',
    asks: [],
  })
  vi.mocked(solutionApi.fetchTurns).mockResolvedValue([])
  useSolutionsStore().currentId = solutionId
})

describe('提交形状', () => {
  it('发消息时把线程、模式与正文一起交给任务', async () => {
    const { chat } = makeChat('prd-clarify')

    await chat.sendMessage('这段能不能再具体点')

    const payload = vi.mocked(api.createJob).mock.calls[0]?.[0]
    expect(payload?.kind).toBe('chat')
    expect(payload?.chatStage).toBe('prd-clarify')
    expect(payload?.chatMode).toBe('continue')
    expect(payload?.content).toBe('这段能不能再具体点')
    expect(payload?.solutionId).toBe(solutionId)
  })

  it('作答轮把结构化 tool_result 原样带上（它是配对的唯一依据）', async () => {
    const { chat } = makeChat()

    await chat.sendMessage(
      '1. 目标用户？\n   答：A. 运营',
      {
        tool_call_id: 'call-1',
        answers: [{ ask_id: 'a1', question: '目标用户', labels: ['A'], texts: ['运营'] }] as never,
      },
      [{ ask_id: 'a1', question: '目标用户', labels: ['A'], texts: ['运营'] }] as never,
    )

    const payload = vi.mocked(api.createJob).mock.calls[0]?.[0]
    // 形状由后端定义，前端只搬运：这里守「有没有带上」，不逐字段相等
    expect(payload?.toolResult?.tool_call_id).toBe('call-1')
    expect((payload?.toolResult as { answers?: unknown[] })?.answers).toHaveLength(1)
  })

  it('提交需求走 start 模式，并带上映射好的表单答案', async () => {
    const { chat, notices, entered } = makeChat()

    await chat.startConversation(sampleSummary)

    const payload = vi.mocked(api.createJob).mock.calls[0]?.[0]
    expect(payload?.chatMode).toBe('start')
    expect(payload?.formData).toBeTruthy()
    expect(entered()).toBe(1)
    expect(notices[0]).toContain('需求已提交')
  })

  it('开新会话时清的是**服务端**那条线程，而不是只清本地', async () => {
    const { chat } = makeChat()

    await chat.startConversation(sampleSummary)

    expect(solutionApi.clearTurns).toHaveBeenCalledWith(solutionId, 'requirement')
  })

  it('文档澄清不在开新会话时清历史（那是在同一条线程上继续）', async () => {
    const chat = useClarificationChat(
      { onNotice: () => {}, onEnterChat: () => {} },
      { stage: 'prd-clarify', clearHistory: false },
    )

    await chat.startConversationWithAnswers({ document: '# PRD' })

    expect(solutionApi.clearTurns).not.toHaveBeenCalled()
  })
})

describe('渲染归属', () => {
  it('只显示本线程的消息；别的线程一条不混', () => {
    seedTurns([
      { role: 'user', content: '需求线程的话', stage: 'requirement' },
      { role: 'assistant', content: 'PRD 线程的话', stage: 'prd-clarify' },
    ])
    const { chat } = makeChat('prd-clarify')

    expect(chat.displayMessages.value.map((m) => m.content)).toEqual(['PRD 线程的话'])
  })

  it('本线程在跑时，末尾挂一条流式气泡，内容来自预览', () => {
    const runs = useRunsStore()
    runs.begin({ jobId: 42, solutionId, jobKind: 'chat', docKind: null, chatStage: 'requirement' })
    runs.preview.content = '正在说的话'
    const { chat } = makeChat('requirement')

    const shown = chat.displayMessages.value
    expect(shown).toHaveLength(1)
    expect(shown[0]).toMatchObject({ role: 'assistant', content: '正在说的话', streaming: true })
  })

  it('别的线程在跑时，本线程不冒假气泡', () => {
    const runs = useRunsStore()
    runs.begin({ jobId: 42, solutionId, jobKind: 'chat', docKind: null, chatStage: 'requirement' })
    runs.preview.content = '需求线程的内容'
    const { chat } = makeChat('prd-clarify')

    expect(chat.streaming.value).toBe(false)
    expect(chat.displayMessages.value).toEqual([])
  })

  it('别的线程在跑时，本线程仍然可以发消息', async () => {
    const runs = useRunsStore()
    runs.begin({ jobId: 42, solutionId, jobKind: 'chat', docKind: null, chatStage: 'requirement' })
    const { chat } = makeChat('prd-clarify')

    await chat.sendMessage('我也想问一句')

    expect(api.createJob).toHaveBeenCalledTimes(1)
  })
})

describe('收尾', () => {
  it('任务结束后从服务端重拉轮次，而不是自己拼一条消息', async () => {
    vi.mocked(solutionApi.fetchTurns).mockResolvedValue([
      { role: 'user', content: '你好', stage: 'requirement', id: 1 },
      { role: 'assistant', content: '好，我明白了。', stage: 'requirement', id: 2 },
    ])
    const { chat } = makeChat()

    await chat.sendMessage('你好')

    expect(solutionApi.fetchTurns).toHaveBeenCalledWith(solutionId)
    const shown = useWorkflowStore().messages
    expect(shown.map((m) => m.content)).toEqual(['你好', '好，我明白了。'])
    // 预览清空：正文已经在轮次里，留着会与列表里那条重复
    expect(useRunsStore().preview.content).toBe('')
  })

  it('失败时保留错误提示，且不把预览正文留在界面上', async () => {
    vi.mocked(api.readJobStream).mockRejectedValue(new Error('上游连接中断'))
    const { chat } = makeChat()

    await chat.sendMessage('你好')

    expect(useRunsStore().preview.error).toContain('上游连接中断')
    expect(useRunsStore().preview.content).toBe('')
  })

  it('发出去的用户消息立刻可见（乐观插入），随后被服务端整体替换', async () => {
    const { chat } = makeChat()

    const pending = chat.sendMessage('我先说一句')
    expect(useWorkflowStore().messages.map((m) => m.content)).toEqual(['我先说一句'])
    await pending
  })

  it('空文本不发起任务', async () => {
    const { chat } = makeChat()

    await chat.sendMessage('   ')

    expect(api.createJob).not.toHaveBeenCalled()
  })
})

describe('流式可见性', () => {
  it('任务还在跑的时候，增量就已经出现在消息列表里', async () => {
    // 控制流：先吐一段，再挂住，最后放行——模拟真实 LLM 逐字返回的节奏
    let release: () => void = () => {}
    let runAtSubscribe: unknown = 'unset'
    vi.mocked(api.readJobStream).mockImplementation(async (_jobId, handlers) => {
      runAtSubscribe = useRunsStore().activeRun
      handlers?.onChunk?.('第一段')
      await new Promise<void>((resolve) => {
        release = resolve
      })
      handlers?.onChunk?.('第二段')
      return { conversationId: '', reply: '第一段第二段', asks: [] }
    })

    const { chat } = makeChat()
    const pending = chat.sendMessage('你好')
    // 让 `useJob.start` 走到订阅那一步
    await new Promise((resolve) => setTimeout(resolve, 20))

    // 定位用：这时候到底有没有订阅、运行态里的线程对不对
    expect(api.readJobStream).toHaveBeenCalledTimes(1)
    expect(runAtSubscribe).toMatchObject({ jobKind: 'chat', chatStage: 'requirement' })
    expect(useRunsStore().activeRun?.chatStage).toBe('requirement')

    const during = chat.displayMessages.value
    // 乐观插入的用户消息 + 流式中的助手气泡
    expect(during).toHaveLength(2)
    expect(during[1]).toMatchObject({ role: 'assistant', content: '第一段', streaming: true })

    release()
    await pending
  })
})

describe('刷新接回', () => {
  it('重连时增量写进对话预览，收尾后从服务端拉齐轮次', async () => {
    let release: () => void = () => {}
    vi.mocked(api.readJobStream).mockImplementation(async (_jobId, handlers) => {
      handlers?.onChunk?.('断线前已经说出的一半')
      await new Promise<void>((resolve) => {
        release = resolve
      })
      handlers?.onChunk?.('，以及后半句')
      return { conversationId: '', reply: '断线前已经说出的一半，以及后半句', asks: [] }
    })
    // 服务端此时已经有完整轮次
    vi.mocked(solutionApi.fetchTurns).mockResolvedValue([
      {
        id: 3,
        role: 'assistant',
        content: '断线前已经说出的一半，以及后半句',
        stage: 'requirement',
      },
    ])

    // 刷新后快照恢复出来的运行态：身份必须包含「哪条线程」
    useRunsStore().restore({
      jobId: 99,
      solutionId,
      jobKind: 'chat',
      docKind: null,
      chatStage: 'requirement',
    })

    const { chat } = makeChat()
    const pending = chat.reconnect(99)
    await new Promise((resolve) => setTimeout(resolve, 20))

    // 关键：重连期间就能看到半截内容（这正是「刷新后流式丢了」的反面）
    const during = chat.displayMessages.value
    expect(during[during.length - 1]).toMatchObject({
      role: 'assistant',
      content: '断线前已经说出的一半',
      streaming: true,
    })

    release()
    await pending

    // 收尾：预览让位给服务端那条权威轮次，界面不出现重复的两条
    expect(solutionApi.fetchTurns).toHaveBeenCalledWith(solutionId)
    expect(useWorkflowStore().messages.map((m) => m.id)).toEqual([3])
    expect(useRunsStore().preview.content).toBe('')
  })
})
