import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { beforeEach, describe, expect, it } from 'vitest'

import { useDocumentsStore } from '@/stores/documents'
import { useRunsStore } from '@/stores/runs'
import { useWorkflowStore } from '@/stores/workflow'
import type { DocKind, DocumentKind } from '@/types'

/**
 * 会话流程 store 测试。
 *
 * 重点验证三件事：导航守卫是否真的拦得住、快照出入口是否完整、
 * 以及「重新开始」是否把数据清干净。
 *
 * 持久化不再由本 store 负责（权威来源是后端，见 `stores/solutions.ts`），
 * 因此这里不再涉及 localStorage。
 *
 * **产物正文不在这里**：它们归 `stores/documents.ts`（后端 `documents` 表的工作副本）。
 * 需要「有内容」的用例统一走 `setDocuments()`，而不是给 workflow store 赋正文。
 */
function setDocuments(values: Partial<Record<DocumentKind, string>>): void {
  const documents = useDocumentsStore()
  for (const [kind, content] of Object.entries(values)) {
    documents.setLocal(kind as DocumentKind, content ?? '')
  }
}

/**
 * 让本方案有一支在途任务。
 *
 * 运行态住在 `stores/runs`，`activeJobId` / `hasActiveRun` 都是从它**派生**出来的——
 * 从前可以直接给 `activeJobId` / `docGenerating` 赋值，现在必须走 `begin`，
 * 否则测的就不是真实链路了（两个字段各存一份正是这一版要消灭的东西）。
 */
function beginRun(jobId = 7, jobKind: DocKind = 'prd', docKind: DocumentKind | null = 'prd'): void {
  useRunsStore().begin({ jobId, solutionId: 1, jobKind, docKind })
}

describe('useWorkflowStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('生成中也能跳转：导航不再被「有任务在跑」卡死', () => {
    const store = useWorkflowStore()

    store.goTo('chatting')
    expect(store.viewState).toBe('chatting')

    // 对话流式进行中照样能跳。从前这条会返回 false 并静默吞掉——
    // 进度条、底栏「返回」因此全是死按钮（见 `stores/runs.ts` 顶部的复盘）。
    // 「对话在跑」现在是一支带身份的运行态（`jobKind: 'chat'` + 线程），不再是一个全局布尔
    useRunsStore().begin({
      jobId: 5,
      solutionId: 1,
      jobKind: 'chat',
      docKind: null,
      chatStage: 'requirement',
    })
    store.goTo('form')
    expect(store.viewState).toBe('form')
    // 「忙」仍是事实（界面要据此表现），只是不再兼任闸门
    expect(store.busy).toBe(true)

    // 文档生成在跑时同理
    useRunsStore().clear()
    beginRun()
    store.goTo('review-prd')
    expect(store.viewState).toBe('review-prd')
  })

  it('快照变化时自增 changeTick，供上层决定何时保存', async () => {
    const store = useWorkflowStore()
    const before = store.changeTick

    store.debugPrdText = '# 贴进来的 PRD'
    await nextTick()

    expect(store.changeTick).toBeGreaterThan(before)
  })

  it('产物正文变化不触发 changeTick（它已不属于会话快照）', async () => {
    const store = useWorkflowStore()
    const before = store.changeTick

    setDocuments({ prd: '# PRD', 'api-docs': '# API', prompts: '# Prompts' })
    await nextTick()

    expect(store.changeTick).toBe(before)
  })

  it('contentTick 只跟内容走：切阶段 / 切标签 / 任务指针都不算内容变更', async () => {
    const store = useWorkflowStore()
    const contentBefore = store.contentTick
    const changeBefore = store.changeTick

    // 这些都是「我翻到哪一页」，作品一个字都没变
    store.viewState = 'review-prd'
    store.chatTab = 'prd-clarify'
    beginRun(7)
    await nextTick()

    // 它们照样要落库（相位也是要存的状态），但不该推进内容时间——
    // 否则列表里那行时间永远是一片「刚刚」
    expect(store.changeTick).toBeGreaterThan(changeBefore)
    expect(store.contentTick).toBe(contentBefore)
  })

  it('contentTick 只跟内容字段走：对话轮次归服务端，不算一次内容变更', async () => {
    const store = useWorkflowStore()

    const beforePaste = store.contentTick
    store.debugPrdText = '# 贴进来的 PRD'
    await nextTick()
    expect(store.contentTick).toBeGreaterThan(beforePaste)

    // 对话轮次**不**推进内容时间：真源在服务端 `chat_turns`，它是被 `setTurns()` 整体灌进来的
    // 一次读，不是用户本地改出来的「作品」。若把它也算上，每收一轮回复都会刷新列表时间。
    const beforeMessage = store.contentTick
    store.messages.push({ role: 'user', content: '再加一个登录', stage: 'requirement' })
    await nextTick()
    expect(store.contentTick).toBe(beforeMessage)
  })

  it('运行态变化不触发 changeTick（不该被保存的状态）', () => {
    const store = useWorkflowStore()
    const before = store.changeTick

    store.downgrade = 'generating'

    expect(store.changeTick).toBe(before)
  })

  it('clearDowngrade 关掉降级说明，且不触发保存', () => {
    const store = useWorkflowStore()
    store.downgrade = 'generating'
    expect(store.downgradeNotice).not.toBe('')

    const before = store.changeTick
    store.clearDowngrade()

    expect(store.downgrade).toBeNull()
    expect(store.downgradeNotice).toBe('')
    // 它是纯运行态：关掉说明不该被当成一次「内容变更」而落一次库
    expect(store.changeTick).toBe(before)
  })

  it('applySnapshot 自增 restoreTick，供视图重建那些只读一次初始值的组件', () => {
    const store = useWorkflowStore()
    const before = store.restoreTick

    store.applySnapshot({
      viewState: 'form',
      entryMode: 'structured',
      summary: null,
      chatTab: 'requirement',
      quickInput: { productName: '', requirements: '' },
      debugPrdText: '',
      pastePairInput: { prd: '', apiDocs: '' },
      activeJobId: null,
      activeJobKind: null,
      // 目标产物：`optimize` / `clarify` 凭 kind 定不了它，重连时又要靠它知道改的是哪一份
      activeJobDocKind: null,
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
      chatTab: 'requirement',
      quickInput: { productName: '', requirements: '' },
      debugPrdText: '# 恢复的 PRD',
      pastePairInput: { prd: '', apiDocs: '' },
      activeJobId: null,
      activeJobKind: null,
      // 目标产物：`optimize` / `clarify` 凭 kind 定不了它，重连时又要靠它知道改的是哪一份
      activeJobDocKind: null,
    })
    await nextTick()

    // 两者都动：changeTick 用于「有东西变了」，restoreTick 用于「这是整份换上来的」
    expect(store.restoreTick).toBe(restoreBefore + 1)
    expect(store.changeTick).toBeGreaterThan(changeBefore)
  })

  it('reset 也重置 restoreTick 之外的状态，避免旧方案内容回流', () => {
    const store = useWorkflowStore()
    store.debugPrdText = '# 旧内容'

    store.reset()

    expect(store.debugPrdText).toBe('')
  })

  it('snapshot 只导出可持久化字段，applySnapshot 能原样还原', () => {
    const store = useWorkflowStore()
    store.viewState = 'review-prd'
    store.entryMode = 'structured'
    store.messages.push({ role: 'user', content: 'hi' })
    store.quickInput = { productName: '小账本', requirements: '记账' }
    store.debugPrdText = '# 贴进来的 PRD'
    store.chatTab = 'prd-clarify'
    // 产物正文改在文档 store 里：它不该出现在会话快照中
    setDocuments({ prd: '# PRD', 'api-docs': '# API', prompts: '# Prompts' })

    const snapshot = store.snapshot()
    expect(snapshot).toEqual({
      viewState: 'review-prd',
      entryMode: 'structured',
      summary: null,
      chatTab: 'prd-clarify',
      quickInput: { productName: '小账本', requirements: '记账' },
      debugPrdText: '# 贴进来的 PRD',
      pastePairInput: { prd: '', apiDocs: '' },
      activeJobId: null,
      activeJobKind: null,
      // 目标产物：`optimize` / `clarify` 凭 kind 定不了它，重连时又要靠它知道改的是哪一份
      activeJobDocKind: null,
      // 对话任务停在的线程：重连时靠它知道这轮回复该记到哪条线程（文档任务为 null）
      activeChatStage: null,
    })
    // 运行态不在快照里
    expect('downgrade' in snapshot).toBe(false)
    // 产物正文与回执也不在：它们属于后端 `documents`
    expect('prdContent' in snapshot).toBe(false)
    expect('prdRunSummary' in snapshot).toBe(false)

    const fresh = useWorkflowStore()
    fresh.reset()
    fresh.applySnapshot(snapshot, null)
    expect(fresh.snapshot()).toEqual(snapshot)
    expect(fresh.downgrade).toBeNull()
  })

  /**
   * 四条澄清线程共用**一张**消息表（靠消息上的 `stage` 分线），各线程的后端会话 id 收在
   * `conversationIds` 里，当前停在哪条线程记在 `chatTab`。这三样都必须能随快照原样进出，
   * 否则刷新一次「我在跟 AI 澄清接口文档」这件事就丢了——用户会发现底栏忽然又能生成 PRD、
   * 或者自己的对话跑到了别的标签下。
   */
  it('标签随快照往返；消息改由 setTurns 单独灌入（真源在服务端）', () => {
    const store = useWorkflowStore()
    store.messages.push({ role: 'user', content: '我想做一个记账工具', stage: 'requirement' })
    store.messages.push({ role: 'assistant', content: '好的，先确认几点', stage: 'requirement' })
    store.messages.push({ role: 'assistant', content: '准备开始 P0 级确认', stage: 'api-docs-clarify' })
    store.chatTab = 'api-docs-clarify'

    const snapshot = store.snapshot()
    expect(snapshot.chatTab).toBe('api-docs-clarify')
    // 消息不再随状态导出：它归服务端 `chat_turns`，由 `setTurns` 单独灌入
    expect(snapshot).not.toHaveProperty('messages')

    const fresh = useWorkflowStore()
    fresh.reset()
    fresh.applySnapshot(snapshot, null)

    expect(fresh.chatTab).toBe('api-docs-clarify')
    // 快照落地时镜像先清空——上一份方案的对话不该在新方案里短暂出现（视觉上是「串稿」）
    expect(fresh.messages).toEqual([])
    // 灌入由调用方随后做（`solutions.openSolution` → `fetchTurns`）
    fresh.setTurns([
      { role: 'user', content: '我想做一个记账工具', stage: 'requirement' },
      { role: 'assistant', content: '好的，先确认几点', stage: 'requirement' },
      { role: 'assistant', content: '准备开始 P0 级确认', stage: 'api-docs-clarify' },
    ])
    // 四条线程的消息都在同一条时间线里，顺序不变，按 `stage` 分线
    expect(fresh.messages.map((message) => message.stage)).toEqual([
      'requirement',
      'requirement',
      'api-docs-clarify',
    ])
    expect(
      fresh.messages.filter((message) => message.stage === 'api-docs-clarify'),
    ).toHaveLength(1)
  })

  it('旧快照缺 chatTab / conversationIds 时逐键兜底，而不是让整份快照作废', () => {
    const store = useWorkflowStore()
    store.applySnapshot(
      {
        viewState: 'review-prd',
        entryMode: 'structured',
        summary: null,
        // 上一个版本存下来的快照确实没有这两个字段
        chatTab: undefined as never,
        quickInput: { productName: '', requirements: '' },
        debugPrdText: '',
        pastePairInput: { prd: '', apiDocs: '' },
        activeJobId: null,
        activeJobKind: null,
      },
      null,
    )

    // 落在需求澄清、四条线程的会话 id 都是空串（接续时走无状态模式）
    expect(store.chatTab).toBe('requirement')
    // 消息照常恢复（stage 的归一在 `normalizeSessionSnapshot` 里做，这里给的是原样）
  })

  it('reset 把标签与各线程会话 id 一并清掉——「重新开始」不留任何痕迹', () => {
    const store = useWorkflowStore()
    store.messages.push({ role: 'assistant', content: '准备开始 P0 级确认', stage: 'prd-clarify' })
    store.chatTab = 'prd-clarify'

    store.reset()

    expect(store.chatTab).toBe('requirement')
  })

  it('reset 清空数据并回到入口页', async () => {
    const store = useWorkflowStore()
    store.messages.push({ role: 'user', content: 'hi' })
    store.summary = { product_name: 'X' } as never
    store.entryMode = 'prd-quick'
    store.quickInput = { productName: '小账本', requirements: '记账' }
    store.debugPrdText = '# 别人的 PRD'
    store.viewState = 'review-prd'
    setDocuments({ prd: '# PRD', 'api-docs': '# API', prompts: '# Prompts' })
    await nextTick()

    store.reset()

    expect(store.summary).toBeNull()
    // 入口模式与两处录入内容也要清掉，否则下一次进来会看到上一轮的残留
    expect(store.entryMode).toBeNull()
    expect(store.quickInput).toEqual({ productName: '', requirements: '' })
    expect(store.debugPrdText).toBe('')
    expect(store.viewState).toBe('entry')
    // 产物正文一并清掉：它虽然不属于本 store，但同属「当前会话」
    expect(useDocumentsStore().hasAny).toBe(false)
  })

  it('hasContent 只认内容字段，光选入口不算有内容', () => {
    const store = useWorkflowStore()
    expect(store.hasContent).toBe(false)

    // 走到表单页但什么都没填：不该被当成「有内容」
    store.chooseEntry('structured')
    expect(store.viewState).toBe('form')
    expect(store.hasContent).toBe(false)

    // 对话有了一条消息也**不算**「有内容」：对话轮次归服务端 `chat_turns`，
    // 由 `setTurns()` 灌入镜像，它的真值判断是 `hasChat`（进度条第二格的依据），
    // 而 `hasContent` 只回答「要不要为这份草稿建一份方案」——那件事发生在对话之前。
    store.messages.push({ role: 'user', content: 'hi' })
    expect(store.hasContent).toBe(false)

    // 真正算内容的仍是录入字段
    store.quickInput.requirements = '记账'
    expect(store.hasContent).toBe(true)
  })

  it('hasContent 覆盖四处录入字段（产物正文不再算「有内容」）', () => {
    type Store = ReturnType<typeof useWorkflowStore>
    const cases: Array<[string, (s: Store) => void]> = [
      ['快捷入口产品名', (s) => (s.quickInput.productName = '小账本')],
      ['快捷入口需求描述', (s) => (s.quickInput.requirements = '记账')],
      ['调试入口贴入的 PRD', (s) => (s.debugPrdText = '# PRD')],
      ['双粘贴入口的 PRD', (s) => (s.pastePairInput.prd = '# PRD')],
      ['双粘贴入口的接口文档', (s) => (s.pastePairInput.apiDocs = '# 接口文档')],
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

  it('产物正文不再让 hasContent 变真：正文产生前方案必然已经存在', async () => {
    const store = useWorkflowStore()
    setDocuments({ prd: '# PRD' })
    await nextTick()

    expect(store.hasContent).toBe(false)
  })

  it('派生状态跟随产物正文变化（解锁进度条的是文档，不是会话快照）', async () => {
    const store = useWorkflowStore()
    expect(store.requiredDocsReady).toBe(false)
    expect(store.canFinish).toBe(false)

    setDocuments({ prd: '# PRD', 'api-docs': '# API', prompts: '# Prompts' })
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
    expect(store.steps.map((step) => step.id)).toHaveLength(6)

    expect(store.chooseEntry('prd-quick')).toBe(true)
    expect(store.viewState).toBe('prd-quick')
    // 快捷流程与完整流程同一条流水线，步数都一样：它没有**需求澄清**那条线程，
    // 但「AI 对话」这一格留给文档澄清，省掉会让对话页里的进度条高亮错位
    expect(store.steps.map((step) => step.id)).toEqual([
      'form',
      'chatting',
      'review-prd',
      'review-api-docs',
      'review-prompts',
      'done',
    ])
    // 录入页落在「描述产品」那一步
    expect(store.currentStepIndex).toBe(0)

    expect(store.chooseEntry('prompt-debug')).toBe(true)
    expect(store.viewState).toBe('prompt-debug')
    expect(store.steps.map((step) => step.id)).toEqual(['review-prompts', 'done'])

    // 第四条入口：落在自己的录入页，进度条口径与「我有 PRD」一致
    expect(store.chooseEntry('prd-and-api-paste')).toBe(true)
    expect(store.viewState).toBe('prd-and-api-paste')
    expect(store.steps.map((step) => step.id)).toEqual(['review-prompts', 'done'])
  })

  it('双粘贴入口只需提示词就能完成（自带的两份材料不算产物）', () => {
    const store = useWorkflowStore()
    store.chooseEntry('prd-and-api-paste')

    // 两份材料还躺在录入页里，不算「已产出」
    setDocuments({})
    expect(store.requiredDocsReady).toBe(false)

    setDocuments({ prompts: '# 提示词套件' })
    expect(store.requiredDocsReady).toBe(true)
  })

  it('请求进行中不允许切换入口', () => {
    const store = useWorkflowStore()
    beginRun()
    expect(store.chooseEntry('prd-quick')).toBe(false)
    expect(store.entryMode).toBeNull()
    expect(store.viewState).toBe('entry')
  })

  it('完成条件按入口模式的交付物判断', async () => {
    const store = useWorkflowStore()
    store.entryMode = 'prd-quick'
    setDocuments({ prd: '# PRD' })
    await nextTick()
    // 快捷流程与完整流程口径一致：三份齐备才算完成
    expect(store.canFinish).toBe(false)

    store.entryMode = 'structured'
    await nextTick()
    expect(store.canFinish).toBe(false)

    store.entryMode = 'prd-quick'
    setDocuments({ prd: '# PRD', 'api-docs': '# API', prompts: '# Prompts' })
    await nextTick()
    expect(store.canFinish).toBe(true)

    store.entryMode = 'prompt-debug'
    // 换成调试入口后，只有 PRD 反而不能完成
    setDocuments({ prd: '', prompts: '# Prompts' })
    await nextTick()
    expect(store.canFinish).toBe(true)
  })

  it('任务指针是可持久化字段：变化触发保存信号，并随快照原样往返', async () => {
    const store = useWorkflowStore()
    const before = store.changeTick

    beginRun(7)
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
    beginRun(9)

    store.reset()

    expect(store.activeJobId).toBeNull()
    expect(store.activeJobKind).toBeNull()
  })
})

/**
 * 对话页子标签栏的**可点判定**与线程作废。
 *
 * 判据是「这条线程要澄清的东西准备好了没有」。它错了不会报错，只会表现为
 * 「明明还没生成 PRD，却能点进 PRD 澄清」或者「文档生成好了，标签却还是灰的」。
 *
 * 另一半是**回退**：整篇重新生成会清掉下游产物，正在看的那条线程可能就此失去依据；
 * 不回退的话用户会停在一条空线程上，底栏的「按澄清结果改写」也点不动（无正文即早退），
 * 看起来就像坏了。
 */
describe('对话页的线程可用性与作废', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('需求线程只在完整流程里可用；三条文档线程各看自己那份正文', async () => {
    const store = useWorkflowStore()
    // 还没选入口（或旧快照）按完整流程算：四条线程里只有需求澄清就绪
    expect(store.availableChatTabs).toEqual(['requirement'])

    setDocuments({ prd: '# PRD' })
    await nextTick()
    expect(store.availableChatTabs).toEqual(['requirement', 'prd-clarify'])

    setDocuments({ 'api-docs': '# API', prompts: '# Prompts' })
    await nextTick()
    expect(store.availableChatTabs).toEqual([
      'requirement',
      'prd-clarify',
      'api-docs-clarify',
      'prompts-clarify',
    ])
  })

  it('只有完整流程有需求对话，其余三条入口都没有', async () => {
    const store = useWorkflowStore()
    store.chooseEntry('structured')
    setDocuments({ prd: '# PRD' })
    await nextTick()
    expect(store.availableChatTabs).toEqual(['requirement', 'prd-clarify'])

    // 快捷流程：一段话直接进生成，没有需求可聊；文档澄清照旧
    store.chooseEntry('prd-quick')
    await nextTick()
    expect(store.availableChatTabs).toEqual(['prd-clarify'])

    store.chooseEntry('prompt-debug')
    setDocuments({ prd: '', 'api-docs': '', prompts: '# Prompts' })
    await nextTick()
    expect(store.availableChatTabs).toEqual(['prompts-clarify'])
  })

  it('正在看的线程失去依据时回退到第一个可用线程，但**不改写**用户选中的那条', async () => {
    const store = useWorkflowStore()
    store.chatTab = 'prompts-clarify'
    setDocuments({ prd: '# PRD', 'api-docs': '# API', prompts: '# Prompts' })
    await nextTick()
    expect(store.activeChatTab).toBe('prompts-clarify')

    // 整篇重生成 PRD 会清掉下游（接口文档 / 提示词）——那条线程随之失去依据
    setDocuments({ 'api-docs': '', prompts: '' })
    await nextTick()
    expect(store.availableChatTabs).toEqual(['requirement', 'prd-clarify'])
    // 显示上回退到第一个可用的线程
    expect(store.activeChatTab).toBe('requirement')

    // ⚠️ 关键：`chatTab` 里的**意图**必须留着。若当场改写它，就会引入一个时序依赖——
    // 打开一份停在「接口澄清」的方案时，快照先落地、`documents` 后到达，中间那一小段时间里
    // 「没有正文」是事实，标签于是被改写成需求澄清，等正文到了也回不去，用户上次停在哪条
    // 线程上就静默丢了（AppV2 的「打开一份停在接口澄清的方案」用例钉的是同一件事）
    expect(store.chatTab).toBe('prompts-clarify')

    // 正文回来了，视图自动回到用户选中过的那条线程
    setDocuments({ 'api-docs': '# API', prompts: '# Prompts' })
    await nextTick()
    expect(store.activeChatTab).toBe('prompts-clarify')
  })

  it('dropChatThread 只清那一条线程的消息与会话 id，别的线程一条不动', () => {
    const store = useWorkflowStore()
    store.messages.push({ role: 'user', content: '需求 A' })
    store.messages.push({ role: 'user', content: 'PRD 那边的问题', stage: 'prd-clarify' })
    store.messages.push({ role: 'user', content: '接口那边的问题', stage: 'api-docs-clarify' })

    store.dropChatThread('prd-clarify')
    expect(store.messages.map((message) => message.content)).toEqual([
      '需求 A',
      '接口那边的问题',
    ])
  })

  it('canGeneratePrd 只认需求那一条线程：文档澄清的回复点不亮它', () => {
    const store = useWorkflowStore()
    expect(store.canGeneratePrd).toBe(false)

    // 文档线程里 AI 已经答了一轮，但那不是需求
    store.messages.push({ role: 'assistant', content: '好的', stage: 'api-docs-clarify' })
    expect(store.canGeneratePrd).toBe(false)

    store.messages.push({ role: 'assistant', content: '已理解需求', stage: 'requirement' })
    expect(store.canGeneratePrd).toBe(true)
  })

  it('标签切换本身是可持久化变化，会触发一次保存', async () => {
    const store = useWorkflowStore()
    const before = store.changeTick

    store.chatTab = 'prd-clarify'
    await nextTick()

    // 不变的话，刷新后用户会被丢回需求澄清标签——他刚才明明在别处
    expect(store.changeTick).toBeGreaterThan(before)
  })
})
