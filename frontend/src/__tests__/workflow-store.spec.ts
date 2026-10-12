import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { beforeEach, describe, expect, it } from 'vitest'

import { SESSION_STORAGE_KEY, useWorkflowStore } from '@/stores/workflow'

/**
 * 会话流程 store 测试。
 *
 * 重点验证三件事：导航守卫是否真的拦得住、会话数据是否自动落盘与恢复、
 * 以及「重新开始」是否连本地快照一起清掉（否则刷新会把旧会话又拉回来）。
 */
describe('useWorkflowStore', () => {
  beforeEach(() => {
    localStorage.clear()
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

  it('会话数据变化时写入本地快照', async () => {
    const store = useWorkflowStore()
    store.prdContent = '# 新 PRD'
    await nextTick()

    const raw = localStorage.getItem(SESSION_STORAGE_KEY)
    expect(raw).toContain('# 新 PRD')
  })

  it('reset 清空数据、回到入口页并删除本地快照', async () => {
    const store = useWorkflowStore()
    store.prdContent = '# PRD'
    store.messages.push({ role: 'user', content: 'hi' })
    store.summary = { product_name: 'X' } as never
    store.entryMode = 'prd-quick'
    store.quickInput = { productName: '小账本', requirements: '记账' }
    store.debugPrdText = '# 别人的 PRD'
    store.viewState = 'review-prd'
    await nextTick()
    expect(localStorage.getItem(SESSION_STORAGE_KEY)).not.toBeNull()

    store.reset()

    expect(store.prdContent).toBe('')
    expect(store.messages).toEqual([])
    expect(store.summary).toBeNull()
    // 入口模式与两处录入内容也要清掉，否则下一次进来会看到上一轮的残留
    expect(store.entryMode).toBeNull()
    expect(store.quickInput).toEqual({ productName: '', requirements: '' })
    expect(store.debugPrdText).toBe('')
    expect(store.viewState).toBe('entry')
    // 立刻删除本地快照：这里断言的是 reset 中的删除动作本身。
    // （持久化 watch 会在下一次 flush 时把「空态」再写回去，那是无害的，
    // 因为空态恢复出来就是入口页——见 AppV2 里对重新开始的断言。）
    expect(localStorage.getItem(SESSION_STORAGE_KEY)).toBeNull()
  })

  it('挂载时从已有快照恢复，并对生成中断做降级', () => {
    localStorage.setItem(
      SESSION_STORAGE_KEY,
      JSON.stringify({
        viewState: 'generating-prd',
        summary: null,
        messages: [],
        conversationId: '',
        prdContent: '# 半截 PRD',
        apiDocsContent: '',
        promptsContent: '',
      }),
    )

    const store = useWorkflowStore()

    expect(store.viewState).toBe('review-prd')
    expect(store.prdContent).toBe('# 半截 PRD')
    expect(store.downgrade).toBe('generating')
    expect(store.downgradeNotice).toContain('上次的生成未完成')
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

  it('入口模式与两处录入内容随快照持久化', async () => {
    const store = useWorkflowStore()
    store.chooseEntry('prompt-debug')
    store.debugPrdText = '# 别人的 PRD'
    await nextTick()

    const raw = JSON.parse(localStorage.getItem(SESSION_STORAGE_KEY) ?? '{}')
    expect(raw.entryMode).toBe('prompt-debug')
    expect(raw.debugPrdText).toBe('# 别人的 PRD')
    expect(raw.viewState).toBe('prompt-debug')
  })

  it('刷新后仍停在录入页（录入内容在快照里，无需降级）', () => {
    localStorage.setItem(
      SESSION_STORAGE_KEY,
      JSON.stringify({
        viewState: 'prd-quick',
        entryMode: 'prd-quick',
        summary: null,
        messages: [],
        conversationId: '',
        quickInput: { productName: '小账本', requirements: '做一个记账 App' },
        debugPrdText: '',
        prdContent: '',
        apiDocsContent: '',
        promptsContent: '',
      }),
    )

    const store = useWorkflowStore()

    expect(store.viewState).toBe('prd-quick')
    expect(store.downgrade).toBeNull()
    expect(store.quickInput).toEqual({ productName: '小账本', requirements: '做一个记账 App' })
  })
})
