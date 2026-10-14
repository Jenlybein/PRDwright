import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { beforeEach, describe, expect, it } from 'vitest'

import { useWorkflowStore } from '@/stores/workflow'

/**
 * 会话流程 store 测试。
 *
 * 重点验证三件事：导航守卫是否真的拦得住、快照出入口是否完整、
 * 以及「重新开始」是否把数据清干净。
 *
 * 持久化不再由本 store 负责（权威来源是后端，见 `stores/solutions.ts`），
 * 因此这里不再涉及 localStorage。
 */
describe('useWorkflowStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('请求进行中禁止跳转', () => {
    const store = useWorkflowStore()

    expect(store.goTo('chatting')).toBe(true)
    expect(store.viewState).toBe('chatting')

    store.chatStreaming = true
    expect(store.goTo('form')).toBe(false)
    expect(store.viewState).toBe('chatting')

    // 文档生成同样算「忙」
    store.chatStreaming = false
    store.docGenerating = true
    expect(store.goTo('form')).toBe(false)
  })

  it('快照变化时自增 changeTick，供上层决定何时保存', async () => {
    const store = useWorkflowStore()
    const before = store.changeTick

    store.prdContent = '# 新 PRD'
    await nextTick()

    expect(store.changeTick).toBeGreaterThan(before)
  })

  it('运行态变化不触发 changeTick（不该被保存的状态）', () => {
    const store = useWorkflowStore()
    const before = store.changeTick

    store.chatStreaming = true
    store.docGenerating = true
    store.downgrade = 'generating'

    expect(store.changeTick).toBe(before)
  })

  it('applySnapshot 自增 restoreTick，供视图重建那些只读一次初始值的组件', () => {
    const store = useWorkflowStore()
    const before = store.restoreTick

    store.applySnapshot({
      viewState: 'form',
      entryMode: 'structured',
      summary: null,
      messages: [],
      conversationId: '',
      quickInput: { productName: '', requirements: '' },
      debugPrdText: '',
      prdContent: '',
      apiDocsContent: '',
      promptsContent: '',
      activeJobId: null,
      activeJobKind: null,
    })

    expect(store.restoreTick).toBe(before + 1)
  })

  it('restoreTick 与 changeTick 是两条独立的信号：加载也会让 changeTick 变，但语义不同', async () => {
    const store = useWorkflowStore()
    const changeBefore = store.changeTick
    const restoreBefore = store.restoreTick

    store.applySnapshot({
      viewState: 'review-prd',
      entryMode: 'structured',
      summary: null,
      messages: [],
      conversationId: '',
      quickInput: { productName: '', requirements: '' },
      debugPrdText: '',
      prdContent: '# 恢复的 PRD',
      apiDocsContent: '',
      promptsContent: '',
      activeJobId: null,
      activeJobKind: null,
    })
    await nextTick()

    // 两者都动：changeTick 用于「有东西变了」，restoreTick 用于「这是整份换上来的」
    expect(store.restoreTick).toBe(restoreBefore + 1)
    expect(store.changeTick).toBeGreaterThan(changeBefore)
  })

  it('reset 也重置 restoreTick 之外的状态，避免旧方案内容回流', () => {
    const store = useWorkflowStore()
    store.prdContent = '# 旧 PRD'

    store.reset()

    expect(store.prdContent).toBe('')
  })

  it('snapshot 只导出可持久化字段，applySnapshot 能原样还原', () => {
    const store = useWorkflowStore()
    store.viewState = 'review-prd'
    store.entryMode = 'structured'
    store.messages.push({ role: 'user', content: 'hi' })
    store.quickInput = { productName: '小账本', requirements: '记账' }
    store.debugPrdText = '# 贴进来的 PRD'
    store.prdContent = '# PRD'
    store.apiDocsContent = '# API'
    store.promptsContent = '# Prompts'
    store.conversationId = 'conv-1'

    const snapshot = store.snapshot()
    expect(snapshot).toEqual({
      viewState: 'review-prd',
      entryMode: 'structured',
      summary: null,
      messages: [{ role: 'user', content: 'hi' }],
      conversationId: 'conv-1',
      quickInput: { productName: '小账本', requirements: '记账' },
      debugPrdText: '# 贴进来的 PRD',
      prdContent: '# PRD',
      apiDocsContent: '# API',
      promptsContent: '# Prompts',
      activeJobId: null,
      activeJobKind: null,
    })
    // 运行态不在快照里
    expect('chatStreaming' in snapshot).toBe(false)
    expect('downgrade' in snapshot).toBe(false)

    const fresh = useWorkflowStore()
    fresh.reset()
    fresh.applySnapshot(snapshot, null)
    expect(fresh.snapshot()).toEqual(snapshot)
    expect(fresh.downgrade).toBeNull()
  })

  it('reset 清空数据并回到入口页', async () => {
    const store = useWorkflowStore()
    store.prdContent = '# PRD'
    store.messages.push({ role: 'user', content: 'hi' })
    store.summary = { product_name: 'X' } as never
    store.entryMode = 'prd-quick'
    store.quickInput = { productName: '小账本', requirements: '记账' }
    store.debugPrdText = '# 别人的 PRD'
    store.viewState = 'review-prd'
    await nextTick()

    store.reset()

    expect(store.prdContent).toBe('')
    expect(store.messages).toEqual([])
    expect(store.summary).toBeNull()
    // 入口模式与两处录入内容也要清掉，否则下一次进来会看到上一轮的残留
    expect(store.entryMode).toBeNull()
    expect(store.quickInput).toEqual({ productName: '', requirements: '' })
    expect(store.debugPrdText).toBe('')
    expect(store.viewState).toBe('entry')
  })

  it('hasContent 只认内容字段，光选入口不算有内容', () => {
    const store = useWorkflowStore()
    expect(store.hasContent).toBe(false)

    // 走到表单页但什么都没填：不该被当成「有内容」
    store.chooseEntry('structured')
    expect(store.viewState).toBe('form')
    expect(store.hasContent).toBe(false)

    // 对话有了一条消息，就算有内容了
    store.messages.push({ role: 'user', content: 'hi' })
    expect(store.hasContent).toBe(true)
  })

  it('hasContent 覆盖三处录入与三份文档', () => {
    type Store = ReturnType<typeof useWorkflowStore>
    const cases: Array<[string, (s: Store) => void]> = [
      ['快捷入口产品名', (s) => (s.quickInput.productName = '小账本')],
      ['快捷入口需求描述', (s) => (s.quickInput.requirements = '记账')],
      ['调试入口贴入的 PRD', (s) => (s.debugPrdText = '# PRD')],
      ['PRD 正文', (s) => (s.prdContent = '# PRD')],
      ['接口文档正文', (s) => (s.apiDocsContent = '# API')],
      ['提示词正文', (s) => (s.promptsContent = '# Prompts')],
      ['结构化摘要', (s) => (s.summary = { product_name: 'X' } as never)],
    ]

    for (const [label, apply] of cases) {
      setActivePinia(createPinia())
      const store = useWorkflowStore()
      expect(store.hasContent, label).toBe(false)
      apply(store)
      expect(store.hasContent, label).toBe(true)
    }
  })

  it('派生状态跟随数据变化', async () => {
    const store = useWorkflowStore()
    expect(store.requiredDocsReady).toBe(false)
    expect(store.canFinish).toBe(false)

    store.prdContent = '# PRD'
    store.apiDocsContent = '# API'
    store.promptsContent = '# Prompts'
    await nextTick()

    expect(store.requiredDocsReady).toBe(true)
    expect(store.canFinish).toBe(true)
    // 文档齐备后进度条应到达最后一步
    expect(store.reachedStepIndex).toBe(4)
  })

  it('选择入口后进入该入口的第一个界面，并按模式裁剪进度条', async () => {
    const store = useWorkflowStore()
    // 初始落在入口选择页
    expect(store.viewState).toBe('entry')
    expect(store.entryMode).toBeNull()

    expect(store.chooseEntry('structured')).toBe(true)
    expect(store.entryMode).toBe('structured')
    expect(store.viewState).toBe('form')
    expect(store.steps.map((step) => step.id)).toHaveLength(5)

    expect(store.chooseEntry('prd-quick')).toBe(true)
    expect(store.viewState).toBe('prd-quick')
    // 快捷入口跳过表单与对话，进度条只剩三步
    expect(store.steps.map((step) => step.id)).toEqual([
      'review-prd',
      'review-api-docs',
      'review-prompts',
    ])
    // 「PRD」是该模式的第 1 步
    expect(store.currentStepIndex).toBe(0)

    expect(store.chooseEntry('prompt-debug')).toBe(true)
    expect(store.viewState).toBe('prompt-debug')
    expect(store.steps.map((step) => step.id)).toEqual(['review-prompts'])
  })

  it('请求进行中不允许切换入口', () => {
    const store = useWorkflowStore()
    store.docGenerating = true
    expect(store.chooseEntry('prd-quick')).toBe(false)
    expect(store.entryMode).toBeNull()
    expect(store.viewState).toBe('entry')
  })

  it('完成条件按入口模式的交付物判断', async () => {
    const store = useWorkflowStore()
    store.entryMode = 'prd-quick'
    store.prdContent = '# PRD'
    await nextTick()
    // 快捷入口只要 PRD 就能完成
    expect(store.canFinish).toBe(true)

    store.entryMode = 'structured'
    await nextTick()
    expect(store.canFinish).toBe(false)

    store.entryMode = 'prompt-debug'
    // 换成调试入口后，只有 PRD 反而不能完成
    store.prdContent = ''
    store.promptsContent = '# Prompts'
    await nextTick()
    expect(store.canFinish).toBe(true)
  })

  it('任务指针是可持久化字段：变化触发保存信号，并随快照原样往返', async () => {
    const store = useWorkflowStore()
    const before = store.changeTick

    store.activeJobId = 7
    store.activeJobKind = 'prd'
    await nextTick()

    // 变化必须触发保存信号，否则「刷新后重连」所需的指针不会落库
    expect(store.changeTick).toBeGreaterThan(before)

    const snapshot = store.snapshot()
    expect(snapshot.activeJobId).toBe(7)
    expect(snapshot.activeJobKind).toBe('prd')

    const fresh = useWorkflowStore()
    fresh.reset()
    fresh.applySnapshot(snapshot)
    expect(fresh.activeJobId).toBe(7)
    expect(fresh.activeJobKind).toBe('prd')
  })

  it('reset 清掉任务指针，避免刷新后重连一个已结束的任务', () => {
    const store = useWorkflowStore()
    store.activeJobId = 9
    store.activeJobKind = 'prd'

    store.reset()

    expect(store.activeJobId).toBeNull()
    expect(store.activeJobKind).toBeNull()
  })
})
