import { describe, expect, it } from 'vitest'

import type { ChatMessage, EntryMode, RequirementsSummary, SessionDataV2 } from '@/types'
import type { EntrySpec } from '@/utils/workflow'
import {
  ENTRY_REGISTRY,
  EntrySpecMissingError,
  announcesDeliveryReady,
  assertEntrySpecComplete,
  canGeneratePrdFrom,
  chatStageForDocument,
  docKindOfGeneratingState,
  docKindOfState,
  documentKindOfChatStage,
  emptyConversationIds,
  entryModeForDisplay,
  entrySpecFor,
  entrySpecForDisplay,
  fromBackendKind,
  generatingStateFor,
  hasDocumentReview,
  hasRequiredDocuments,
  hasRequirementChat,
  inputPageOnBar,
  isChatStage,
  fromBackendJobKind,
  isDocKind,
  isDocumentKind,
  isEntryMode,
  isViewState,
  jobDocumentKind,
  normalizeChatStage,
  parseRunSummary,
  normalizeSessionSnapshot,
  reachedStepIndex,
  resolveRestoredPhase,
  reviewStateFor,
  stepIndexOf,
  stepsForMode,
  VIEW_STATES,
} from '@/utils/workflow'

/**
 * 流程阶段机纯函数测试。
 *
 * 这些判断原先散在 AppV2 的 computed 里，无法脱离组件验证；抽出来后
 * 可以逐个把边界钉住——尤其是「刷新后降级」和「快照脏数据」两类。
 */
/**
 * 测试内适配：这些用例原本针对「快照是一段 JSON 文本」的旧签名。
 * 现在状态由后端结构化下发，解析器直接吃对象；这里只加一层 JSON 解析，
 * 让「降级 / 任务指针」这些仍然有效的断言不必逐条重写。
 */
function parseSessionSnapshot(raw: string | null) {
  if (!raw) return null
  try {
    return normalizeSessionSnapshot(JSON.parse(raw) as unknown)
  } catch {
    return null
  }
}

describe('阶段判定', () => {
  it('只承认已定义的阶段', () => {
    expect(isViewState('review-prd')).toBe(true)
    expect(isViewState('nope')).toBe(false)
    expect(isViewState(null)).toBe(false)
    expect(VIEW_STATES).toContain('summary-diff')
  })

  it('生成中与确认闸门态刷新后降级到可操作状态', () => {
    expect(resolveRestoredPhase('generating-prd')).toEqual({
      viewState: 'review-prd',
      downgrade: 'generating',
    })
    expect(resolveRestoredPhase('summary-diff')).toEqual({
      viewState: 'chatting',
      downgrade: 'pending',
    })
  })

  it('可直接恢复的阶段不降级，非法值回到入口页', () => {
    expect(resolveRestoredPhase('review-prompts')).toEqual({
      viewState: 'review-prompts',
      downgrade: null,
    })
    expect(resolveRestoredPhase('whatever')).toEqual({ viewState: 'entry', downgrade: null })
  })

  it('两个录入页不降级：内容在快照里，刷新后继续填', () => {
    expect(resolveRestoredPhase('prd-quick')).toEqual({
      viewState: 'prd-quick',
      downgrade: null,
    })
    expect(resolveRestoredPhase('prompt-debug')).toEqual({
      viewState: 'prompt-debug',
      downgrade: null,
    })
  })
})

describe('入口模式对进度与完成条件的影响', () => {
  it('只有完整流程有需求澄清对话', () => {
    expect(hasRequirementChat('structured')).toBe(true)
    // 快捷流程一段话直接进生成，不问问题；两个导入入口自带的 PRD 就是答案
    expect(hasRequirementChat('prd-quick')).toBe(false)
    expect(hasRequirementChat('prompt-debug')).toBe(false)
    expect(hasRequirementChat('prd-and-api-paste')).toBe(false)
    // 还没选入口（或旧快照）时按完整流程处理：六步与完整流程一致
    expect(hasRequirementChat(null)).toBe(true)
  })

  it('只有快捷流程不带审核/改写', () => {
    expect(hasDocumentReview('structured')).toBe(true)
    // 快捷流程的定义就是「不审不改」：调用要跳，界面上那两格也不能画
    expect(hasDocumentReview('prd-quick')).toBe(false)
    expect(hasDocumentReview('prompt-debug')).toBe(true)
    expect(hasDocumentReview('prd-and-api-paste')).toBe(true)
    // 还没选入口（或旧快照）时按完整流程处理：默认带上复核，漏配只是多等一会儿
    expect(hasDocumentReview(null)).toBe(true)
  })

  it('只承认已定义的入口模式', () => {
    expect(isEntryMode('structured')).toBe(true)
    expect(isEntryMode('prd-quick')).toBe(true)
    expect(isEntryMode('prompt-debug')).toBe(true)
    expect(isEntryMode('bogus')).toBe(false)
    expect(isEntryMode(null)).toBe(false)
  })

  it('进度条展示的步骤随入口模式裁剪', () => {
    expect(stepsForMode('structured').map((step) => step.id)).toEqual([
      'form',
      'chatting',
      'review-prd',
      'review-api-docs',
      'review-prompts',
      'done',
    ])
    // 快捷流程与完整流程同一条流水线，步数都一样：只差第一步的形态（极简输入）。
    // 「AI 对话」这一格保留——快捷流程没有需求澄清，但它承载**文档澄清**那三条线程；
    // 少这一格会让停在对话页时下标归零，进度条去高亮「描述产品」。
    expect(stepsForMode('prd-quick').map((step) => step.id)).toEqual([
      'form',
      'chatting',
      'review-prd',
      'review-api-docs',
      'review-prompts',
      'done',
    ])
    expect(stepsForMode('prompt-debug').map((step) => step.id)).toEqual([
      'review-prompts',
      'done',
    ])
    // 双粘贴入口与它同为导入组：产物也只有提示词，进度条口径就该一样
    expect(stepsForMode('prd-and-api-paste').map((step) => step.id)).toEqual([
      'review-prompts',
      'done',
    ])
    // 还没选入口（或旧快照）时按完整流程展示
    expect(stepsForMode(null).map((step) => step.id)).toEqual(
      stepsForMode('structured').map((step) => step.id),
    )
  })

  it('步骤下标按模式口径计算', () => {
    // 快捷流程的录入页落在第 1 步（「描述产品」），与完整流程的表单同位
    expect(stepIndexOf('prd-quick', 'prd-quick')).toBe(0)
    // 「AI 对话」在快捷流程里照样占一格（文档澄清走的就是这个对话页）
    expect(stepIndexOf('chatting', 'prd-quick')).toBe(1)
    expect(stepIndexOf('review-prd', 'prd-quick')).toBe(2)
    expect(stepIndexOf('review-api-docs', 'prd-quick')).toBe(3)
    expect(stepIndexOf('review-prompts', 'prd-quick')).toBe(4)
    expect(stepIndexOf('review-prompts', 'prompt-debug')).toBe(0)
    expect(stepIndexOf('entry', null)).toBe(0)
    // 完整流程口径与全局一致
    expect(stepIndexOf('review-prd', 'structured')).toBe(2)
    // 该模式下不存在的步骤归零而不是抛错，进度条永远有可渲染的位置
    expect(stepIndexOf('form', 'prompt-debug')).toBe(0)
  })

  it('已到达的最远步骤不会超出该模式的步骤范围', () => {
    const base = { hasMessages: false, hasPrd: false, hasApiDocs: false, hasPrompts: false }
    // 调试入口只有一步：哪怕三份文档都在，下标也不能越界
    expect(
      reachedStepIndex({
        ...base,
        mode: 'prompt-debug',
        hasPrd: true,
        hasApiDocs: true,
        hasPrompts: true,
        currentStepIndex: 0,
      }),
    ).toBe(0)
    // 快捷流程与完整流程步数一致：有 PRD 就停在「PRD」那一步（下标 2）
    expect(
      reachedStepIndex({ ...base, mode: 'prd-quick', hasPrd: true, currentStepIndex: 0 }),
    ).toBe(2)
    expect(
      reachedStepIndex({
        ...base,
        mode: 'prd-quick',
        hasPrd: true,
        hasApiDocs: true,
        currentStepIndex: 0,
      }),
    ).toBe(3)
    // 文档澄清聊过之后，「AI 对话」那一格就解锁了
    expect(
      reachedStepIndex({ ...base, mode: 'prd-quick', hasMessages: true, currentStepIndex: 0 }),
    ).toBe(1)
  })

  it('完成条件按各入口模式的交付物判断', () => {
    const onlyPrd = { prd: 'a', apiDocs: '', prompts: '' }
    expect(hasRequiredDocuments('structured', onlyPrd)).toBe(false)
    // 快捷流程复用同一条进度条，交付判据也一并对齐：只有 PRD 不算完成
    expect(hasRequiredDocuments('prd-quick', onlyPrd)).toBe(false)
    expect(hasRequiredDocuments('prompt-debug', onlyPrd)).toBe(false)
    expect(
      hasRequiredDocuments('prompt-debug', { prd: '', apiDocs: '', prompts: 'c' }),
    ).toBe(true)
    // 未选入口（或旧快照）时按完整流程判断
    expect(hasRequiredDocuments(null, onlyPrd)).toBe(false)
  })
})

describe('进度条位置', () => {
  it('按阶段映射到步骤下标，确认闸门不单独占步骤', () => {
    expect(stepIndexOf('form')).toBe(0)
    expect(stepIndexOf('chatting')).toBe(1)
    expect(stepIndexOf('summary-diff')).toBe(1)
    expect(stepIndexOf('done')).toBe(5)
  })

  it('已到达的最远步骤取「已生成内容」与「当前阶段」的较大者', () => {
    const base = {
      mode: null,
      hasMessages: false,
      hasPrd: false,
      hasApiDocs: false,
      hasPrompts: false,
    }
    expect(reachedStepIndex({ ...base, currentStepIndex: 0 })).toBe(0)
    // 生成过 PRD 后再回看第一步，仍应能点回 PRD 步骤
    expect(reachedStepIndex({ ...base, hasMessages: true, hasPrd: true, currentStepIndex: 0 })).toBe(2)
    // 当前阶段更靠后时以它为准
    expect(reachedStepIndex({ ...base, currentStepIndex: 3 })).toBe(3)
  })
})

describe('守卫', () => {
  const assistant: ChatMessage = { role: 'assistant', content: '已理解需求' }

  it('至少要有一轮非空 AI 回复才能生成 PRD', () => {
    expect(canGeneratePrdFrom([], false)).toBe(false)
    expect(canGeneratePrdFrom([{ role: 'assistant', content: '   ' }], false)).toBe(false)
    expect(canGeneratePrdFrom([assistant], false)).toBe(true)
  })

  it('请求进行中一律禁止生成 PRD', () => {
    expect(canGeneratePrdFrom([assistant], true)).toBe(false)
  })

  it('完整流程下三份文档都非空才算齐备', () => {
    expect(hasRequiredDocuments('structured', { prd: 'a', apiDocs: 'b', prompts: '' })).toBe(false)
    expect(hasRequiredDocuments('structured', { prd: 'a', apiDocs: 'b', prompts: 'c' })).toBe(true)
    expect(hasRequiredDocuments('structured', { prd: 'a', apiDocs: '  ', prompts: 'c' })).toBe(false)
  })

  it('只有三份齐备的入口才弹「已齐备」提示：导入入口只要一份提示词', () => {
    // 规格只说「三份齐备时全应用提示一次」。对只要一份提示词的人说「三份文档已齐备」
    // 是假话，所以这条提示的**触发条件**与交付格的**可点条件**不是同一个判据。
    expect(announcesDeliveryReady('structured')).toBe(true)
    expect(announcesDeliveryReady('prd-quick')).toBe(true)
    expect(announcesDeliveryReady('prompt-debug')).toBe(false)
    expect(announcesDeliveryReady('prd-and-api-paste')).toBe(false)
    // 「未选择」按完整流程展示（与 hasRequiredDocuments 同口径）：三份，仍然提示
    expect(announcesDeliveryReady(null)).toBe(true)
  })
})

describe('方案状态解析', () => {
  const full = {
    viewState: 'review-prd',
    entryMode: 'structured',
    summary: { product_name: 'X' },
    chatTab: 'requirement',
    quickInput: { productName: '', requirements: '' },
    debugPrdText: '',
    pastePairInput: { prd: '', apiDocs: '' },
    activeJobId: null,
    activeJobKind: null,
    activeJobDocKind: null,
  }

  it('非对象一律返回 null', () => {
    expect(normalizeSessionSnapshot(null)).toBeNull()
    expect(normalizeSessionSnapshot('"只是个字符串"')).toBeNull()
    expect(normalizeSessionSnapshot(42)).toBeNull()
  })

  it('正常状态原样还原', () => {
    const parsed = normalizeSessionSnapshot(full)
    expect(parsed?.session.viewState).toBe('review-prd')
    expect(parsed?.session.entryMode).toBe('structured')
    expect(parsed?.session.chatTab).toBe('requirement')
    expect(parsed?.downgrade).toBeNull()
  })

  it('状态里**没有**消息与会话 id：它们归服务端', () => {
    // 这一条守的是这次改造的核心：聊天记录住在 `chat_turns`、会话 id 住在
    // `chat_sessions`，状态对象里再出现它们就等于又造了一个真源。
    const parsed = normalizeSessionSnapshot({ ...full, messages: [{ role: 'user' }] })
    expect(parsed?.session).not.toHaveProperty('messages')
    expect(parsed?.session).not.toHaveProperty('conversationIds')
    expect(Object.keys(parsed?.session ?? {})).toEqual([
      'viewState',
      'entryMode',
      'summary',
      'chatTab',
      'quickInput',
      'debugPrdText',
      'pastePairInput',
      'activeJobId',
      'activeJobKind',
      'activeJobDocKind',
      'activeChatStage',
    ])
  })

  it('旧阶段名 `clarifying` 按对话页处理', () => {
    const parsed = normalizeSessionSnapshot({ ...full, viewState: 'clarifying' })
    expect(parsed?.session.viewState).toBe('chatting')
  })

  it('生成中的状态会降级并记录原因', () => {
    const parsed = normalizeSessionSnapshot({ ...full, viewState: 'generating-prd' })
    expect(parsed?.session.viewState).toBe('review-prd')
    expect(parsed?.downgrade).toBe('generating')
  })

  it('脏字段只丢弃自己，不让整份状态失效', () => {
    const parsed = normalizeSessionSnapshot({
      ...full,
      entryMode: 'bogus',
      quickInput: 'not-an-object',
      debugPrdText: 42,
      pastePairInput: 'not-an-object',
    })
    expect(parsed?.session.entryMode).toBeNull()
    expect(parsed?.session.quickInput).toEqual({ productName: '', requirements: '' })
    expect(parsed?.session.debugPrdText).toBe('')
    expect(parsed?.session.pastePairInput).toEqual({ prd: '', apiDocs: '' })
    expect(parsed?.session.viewState).toBe('review-prd')
  })

  it('双粘贴入口的两份正文逐字段退化', () => {
    const parsed = normalizeSessionSnapshot({
      ...full,
      viewState: 'prd-and-api-paste',
      pastePairInput: { prd: '# 我的 PRD', apiDocs: 42 },
    })
    expect(parsed?.session.pastePairInput).toEqual({ prd: '# 我的 PRD', apiDocs: '' })
  })

  it('对话任务的线程由 activeJobKind 与 chatTab 共同推出', () => {
    const parsed = normalizeSessionSnapshot({
      ...full,
      chatTab: 'prd-clarify',
      activeJobId: 7,
      activeJobKind: 'chat',
    })
    expect(parsed?.session.activeChatStage).toBe('prd-clarify')
    // 非对话任务没有线程
    const doc = normalizeSessionSnapshot({ ...full, activeJobId: 7, activeJobKind: 'prd' })
    expect(doc?.session.activeChatStage).toBeNull()
  })
})

// ============================================================================
// 带任务指针的快照恢复（刷新静默重连的判定依据）
//
// 生成与浏览器连接解耦后，`generating-*` 是否降级取决于快照里有没有一个还在跑的
// 任务：有 -> 重连（不降级）；没有 -> 按原规则降级并给说明。这里是那条分叉的落点。
// ============================================================================

describe('带任务指针的快照恢复', () => {
  const withJob = {
    viewState: 'generating-prd',
    entryMode: 'structured',
    summary: null,
    messages: [],
    conversationId: '',
    quickInput: { productName: '', requirements: '' },
    debugPrdText: '',
    pastePairInput: { prd: '', apiDocs: '' },
    activeJobId: 7,
    activeJobKind: 'prd',
  }

  it('有进行中的任务时 generating-* 不降级：任务在后端继续跑，重连即可续上', () => {
    expect(resolveRestoredPhase('generating-prd', true)).toEqual({
      viewState: 'generating-prd',
      downgrade: null,
    })
    // 没有任务时才回到原规则
    expect(resolveRestoredPhase('generating-prd', false)).toEqual({
      viewState: 'review-prd',
      downgrade: 'generating',
    })
  })

  it('快照带 activeJobId 时保留生成态与任务种类', () => {
    const parsed = parseSessionSnapshot(JSON.stringify(withJob))
    expect(parsed?.session.viewState).toBe('generating-prd')
    expect(parsed?.downgrade).toBeNull()
    expect(parsed?.session.activeJobId).toBe(7)
    expect(parsed?.session.activeJobKind).toBe('prd')
  })

  it('任务指针非法 / 缺失时退化为 null，且仍然降级', () => {
    const parsed = parseSessionSnapshot(
      JSON.stringify({ ...withJob, activeJobId: 'abc', activeJobKind: 'prd' }),
    )
    expect(parsed?.session.activeJobId).toBeNull()
    expect(parsed?.session.viewState).toBe('review-prd')
    expect(parsed?.downgrade).toBe('generating')
    // 指针没了，种类也不该留着——两者成对出现
    expect(parsed?.session.activeJobKind).toBeNull()
  })

  it('任务种类非法时退化为 null，但任务指针仍保留', () => {
    const parsed = parseSessionSnapshot(JSON.stringify({ ...withJob, activeJobKind: 'bogus' }))
    expect(parsed?.session.activeJobId).toBe(7)
    expect(parsed?.session.activeJobKind).toBeNull()
  })

  it('isDocKind 承认全部七种任务种类——包括 chat', () => {
    expect(isDocKind('prd')).toBe(true)
    expect(isDocKind('api-docs')).toBe(true)
    expect(isDocKind('prompts')).toBe(true)
    expect(isDocKind('optimize')).toBe(true)
    expect(isDocKind('clarify')).toBe(true)
    expect(isDocKind('auto-clarify')).toBe(true)
    // 漏掉 chat 的后果极隐蔽：快照恢复时 `activeJobKind` 被判非法、丢成 null，
    // 于是 applySnapshot 退化成 'prd'，刷新后的对话被当成 PRD 生成塞进文档流水线
    expect(isDocKind('chat')).toBe(true)
    expect(isDocKind('bogus')).toBe(false)
    expect(isDocKind(null)).toBe(false)
  })

  it('对话任务的种类能原样往返快照（刷新后不被降级成 prd）', () => {
    const parsed = parseSessionSnapshot(
      JSON.stringify({ ...withJob, activeJobKind: 'chat', chatTab: 'prd-clarify' }),
    )
    expect(parsed?.session.activeJobKind).toBe('chat')
    // 线程也跟着保住：任务发起时用户必然停在那条线程上
    expect(parsed?.session.activeChatStage).toBe('prd-clarify')
  })

  it('fromBackendJobKind 认对话，但 fromBackendKind 不认（对话是动作，不是产物）', () => {
    expect(fromBackendJobKind('chat')).toBe('chat')
    // 「这是哪一份产物」不能答 chat：答了前端就会去 documents 里找一份叫 chat 的文档
    expect(fromBackendKind('chat')).toBeNull()
    expect(fromBackendJobKind('auto_clarify')).toBe('auto-clarify')
  })

  it('isDocumentKind 排除两个「动作」，只管「东西」', () => {
    // 「澄清针对哪一份」必须用这个校验：快照里一个 `clarify` 若被当成产物，
    // 前端会去 documents store 里找一份叫 clarify 的文档
    expect(isDocumentKind('prd')).toBe(true)
    expect(isDocumentKind('api-docs')).toBe(true)
    expect(isDocumentKind('prompts')).toBe(true)
    expect(isDocumentKind('optimize')).toBe(false)
    expect(isDocumentKind('clarify')).toBe(false)
  })

  it('fromBackendKind 把后端产物名转成前端口径，未知取值给 null 不猜', () => {
    // 下划线与连字符两种写法都认：快照里的 `doc_kind` 来自后端，`api_docs` 是常态，
    // 但历史上也有直接存前端口径的旧快照，两种都不能漏。
    expect(fromBackendKind('prd')).toBe('prd')
    expect(fromBackendKind('api_docs')).toBe('api-docs')
    expect(fromBackendKind('api-docs')).toBe('api-docs')
    expect(fromBackendKind('prompts')).toBe('prompts')
    // 动作名、空串、undefined 都**不得**被猜成 prd：猜错等于拿 A 的结果覆盖 B。
    expect(fromBackendKind('optimize')).toBeNull()
    expect(fromBackendKind('auto_clarify')).toBeNull()
    expect(fromBackendKind('')).toBeNull()
    expect(fromBackendKind(undefined)).toBeNull()
  })

  it('generatingStateFor / docKindOfState 互为逆映射（两个动作都不占生成阶段）', () => {    expect(generatingStateFor('prd')).toBe('generating-prd')
    expect(generatingStateFor('api-docs')).toBe('generating-api-docs')
    expect(generatingStateFor('prompts')).toBe('generating-prompts')
    // 就地动某一篇：优化留在审阅页，澄清**先切到对应的生成态**（改写期间要展示流式正文），
    // 两个动作本身都不自带阶段
    expect(generatingStateFor('optimize')).toBeNull()
    expect(generatingStateFor('clarify')).toBeNull()
    expect(docKindOfState('review-prd')).toBe('prd')
    expect(docKindOfState('review-api-docs')).toBe('api-docs')
    expect(docKindOfState('review-prompts')).toBe('prompts')
    // 非审阅态反推不出产物：是哪一份要靠**标签**（`documentKindOfChatStage`）而不是阶段
    expect(docKindOfState('chatting')).toBeNull()
    expect(docKindOfState('generating-prd')).toBeNull()
    expect(docKindOfState('entry')).toBeNull()
  })

  it('jobDocumentKind 只认三种「整篇生成」；optimize / clarify 的目标要另行反推', () => {
    expect(jobDocumentKind('prd')).toBe('prd')
    expect(jobDocumentKind('api-docs')).toBe('api-docs')
    expect(jobDocumentKind('prompts')).toBe('prompts')
    // 两个动作只说「做了一件什么事」，写哪一份由阶段 / 标签回答
    expect(jobDocumentKind('optimize')).toBeNull()
    expect(jobDocumentKind('clarify')).toBeNull()
  })

  it('docKindOfGeneratingState 是 generatingStateFor 的逆，非生成态返回 null', () => {
    expect(docKindOfGeneratingState('generating-prd')).toBe('prd')
    expect(docKindOfGeneratingState('generating-api-docs')).toBe('api-docs')
    expect(docKindOfGeneratingState('generating-prompts')).toBe('prompts')
    expect(docKindOfGeneratingState('review-prd')).toBeNull()
    expect(docKindOfGeneratingState('chatting')).toBeNull()
  })

  it('reviewStateFor 是生成态映射的逆：产物 → 它的审阅页', () => {
    expect(reviewStateFor('prd')).toBe('review-prd')
    expect(reviewStateFor('api-docs')).toBe('review-api-docs')
    expect(reviewStateFor('prompts')).toBe('review-prompts')
  })
})

/**
 * 澄清线程的**四条线程合流**。
 *
 * 合流前有两套并存的形态：PRD 澄清复用主对话栏（消息在 `messages`、会话 id 在
 * `prdClarifyConversationId`、阶段仍是 `chatting`），接口 / 提示词澄清走独立页
 * （消息在 `clarifyMessages`、会话 id 在 `clarifyConversationId`、阶段是 `clarifying`）。
 * 合流后只剩一张消息表 + 一个 `conversationIds` 对象 + 一个 `chatTab`。
 *
 * 旧快照要被**就地换算**成新形态，而不是让全仓代码去认识旧值。换算规则错了不会报错，
 * 只会表现为「打开旧方案时对话少了一半」或「接口文档的澄清结论被送进 PRD」——
 * 后者更糟：改写会写进错的文档，而且看起来很合理。因此下面逐条钉住。
 */
describe('澄清线程的取值与互逆映射', () => {
  it('isChatStage / normalizeChatStage：认识旧值，但不认识的值一律归需求澄清', () => {
    expect(isChatStage('prd-clarify')).toBe(true)
    expect(isChatStage('document')).toBe(false)
    expect(isChatStage(null)).toBe(false)
    // 旧值改名
    expect(normalizeChatStage('document')).toBe('prd-clarify')
    // 缺失或无法识别 → 需求澄清（消息没有这个字段只可能是旧快照的需求轮次）
    expect(normalizeChatStage(undefined)).toBe('requirement')
    expect(normalizeChatStage('bogus')).toBe('requirement')
    expect(normalizeChatStage('prompts-clarify')).toBe('prompts-clarify')
  })

  it('线程 ↔ 产物是一对互逆映射，需求澄清不对应任何产物', () => {
    expect(documentKindOfChatStage('requirement')).toBeNull()
    expect(chatStageForDocument('prd')).toBe('prd-clarify')
    for (const kind of ['prd', 'api-docs', 'prompts'] as const) {
      expect(documentKindOfChatStage(chatStageForDocument(kind))).toBe(kind)
    }
    expect(documentKindOfChatStage('api-docs-clarify')).toBe('api-docs')
    expect(documentKindOfChatStage('prompts-clarify')).toBe('prompts')
  })

  it('emptyConversationIds 四条线程齐全且都是空串', () => {
    expect(emptyConversationIds()).toEqual({
      requirement: '',
      'prd-clarify': '',
      'api-docs-clarify': '',
      'prompts-clarify': '',
    })
  })

  it('退役的阶段不再存在：clarifying 既不合法也没有进度条归属', () => {
    // 独立澄清页已整条移除。它若还留在 `VIEW_STATES` 里，快照里一个脏值就能让用户
    // 落在一个没有任何视图认领的阶段上
    expect(VIEW_STATES).not.toContain('clarifying')
    expect(isViewState('clarifying')).toBe(false)
  })
})

/**
 * 「未选择」的唯一出口。
 *
 * `null`（用户还没点入口 / 老快照没记入口名）是**合法状态**，不等于「未知入口键」：
 * 它必须照常按完整流程的展示口径给出答案。从前这条口径散在六处各写一次
 * `?? 'structured'`，改默认口径时漏改一处也不会报错，只表现为「入口页进度条少一格」
 * 或「老方案列表显示成别的入口」这类错位。这里把「所有按模式的判断在未选择时
 * 都等于完整流程」钉住，并钉住那个唯一出口本身。
 */
describe('未选择入口（null）按完整流程的展示口径', () => {
  it('唯一出口 entryModeForDisplay 把 null 归到完整流程', () => {
    expect(entryModeForDisplay(null)).toBe('structured')
    // 已选入口时原样返回，不做任何映射
    expect(entryModeForDisplay('prd-quick')).toBe('prd-quick')
  })

  it('进度条步骤与下标在未选择时与完整流程一致（六步）', () => {
    const unselected = stepsForMode(null).map((step) => step.id)
    expect(unselected).toEqual(stepsForMode('structured').map((step) => step.id))
    expect(unselected).toHaveLength(6)
    for (const state of [
      'form',
      'chatting',
      'review-prd',
      'review-api-docs',
      'review-prompts',
      'done',
    ] as const) {
      expect(stepIndexOf(state, null)).toBe(stepIndexOf(state, 'structured'))
    }
  })

  it('三个按模式的判断在未选择时与完整流程一致', () => {
    expect(hasRequirementChat(null)).toBe(hasRequirementChat('structured'))
    expect(hasDocumentReview(null)).toBe(hasDocumentReview('structured'))
    expect(inputPageOnBar(null)).toBe(inputPageOnBar('structured'))
    // 再用显式期望钉一次：避免「两边一起错」也被判成一致
    expect(hasRequirementChat(null)).toBe(true)
    expect(hasDocumentReview(null)).toBe(true)
    expect(inputPageOnBar(null)).toBe(true)
  })

  it('完成条件在未选择时与完整流程一致', () => {
    const threeDocs = { prd: '# prd', apiDocs: '# api', prompts: '# prompts' }
    expect(hasRequiredDocuments(null, threeDocs)).toBe(
      hasRequiredDocuments('structured', threeDocs),
    )
    expect(hasRequiredDocuments(null, threeDocs)).toBe(true)
    // 只齐一份时，两条口径都判「未完成」
    const oneDoc = { prd: '# prd', apiDocs: '', prompts: '' }
    expect(hasRequiredDocuments(null, oneDoc)).toBe(false)
  })
})

/**
 * 入口模式登记表的第一行。
 *
 * 登记表**一行一个入口、七类资料为列**。这里的期望值取自票面与架构文档的口径，
 * 不是把实现再算一遍：六步、三份文档、要澄清、要审核、落结构化表单、列表名「完整流程」。
 */
describe('入口模式登记表：完整流程一行', () => {
  it('七类资料为列，每一列都填了「完整流程」的答案', () => {
    const spec = ENTRY_REGISTRY.structured
    if (!spec) throw new Error('登记表缺「完整流程」一行')

    // ① 键、组别与卡片文案：与入口页上那张卡一致
    expect(spec.mode).toBe('structured')
    expect(spec.card.groupId).toBe('from-scratch')
    expect(spec.card.groupTitle).toBe('从零创建 PRD')
    expect(spec.card.groupDescription).toBe('还没有任何材料，从这里把需求说清楚。')
    expect(spec.card.title).toBe('完整流程')
    expect(spec.card.description).toBe('逐项填表 + 和 AI 对话把细节问清楚，再依次生成三份文档。')

    // ② 落页：选中落在结构化表单；「改需求」回表单；审阅页「返回」回对话页
    expect(spec.landing.onSelect).toBe('form')
    expect(spec.landing.editRequirements).toBe('form')
    expect(spec.landing.backToInput).toBe('chatting')

    // ③ 进度条步骤：六步齐全
    expect(spec.steps).toEqual([
      'form',
      'chatting',
      'review-prd',
      'review-api-docs',
      'review-prompts',
      'done',
    ])

    // ④ 四个布尔：录入页在进度条上、要需求澄清、要审核改写、澄清入口可顶替「返回」
    expect(spec.inputPageOnBar).toBe(true)
    expect(spec.needsRequirementChat).toBe(true)
    expect(spec.needsDocumentReview).toBe(true)
    expect(spec.keepBackButton).toBe(false)

    // ⑤ 完成所需文档：三份齐备
    expect(spec.requiredDocs).toEqual(['prd', 'api-docs', 'prompts'])

    // ⑥ 文案：列表模式名、返回按钮、改需求按钮
    expect(spec.copy.modeLabel).toBe('完整流程')
    expect(spec.copy.backLabel).toBe('返回对话')
    expect(spec.copy.editRequirementsLabel).toBe('改需求')

    // ⑦ 录入字段：结构化表单落在快照的 `summary` 这一列，字段非空才算「有内容」
    expect(spec.input.stateField).toBe('summary')
    expect(spec.input.stateColumn).toBe('summary')
    expect(spec.input.fields.map((field) => field.key)).toEqual([
      'product_name',
      'product_goal',
      'target_users',
      'platform',
      'mvp_features',
      'v2_features',
      'ui_pages',
      'key_interactions',
      'tech_auth',
      'tech_storage',
      'tech_frontend',
      'tech_backend',
      'tech_deployment',
      'llm_usage',
      'nf_security',
      'nf_performance',
      'nf_availability',
      'scope_in',
      'scope_out',
    ])
    expect(spec.input.fields.every((field) => field.countsAsContent)).toBe(true)
    expect(spec.input.fields.find((field) => field.key === 'product_name')?.label).toBe('产品名称')
  })

  it('结构化入口的问询都从这一行取答案（与迁移前的行为一致）', () => {
    const spec = ENTRY_REGISTRY.structured
    if (!spec) throw new Error('登记表缺「完整流程」一行')
    // 这一条是接缝：登记表真的被问询读到，而不是写了一份没人用的数据
    expect(stepsForMode('structured').map((step) => step.id)).toEqual([...spec.steps])
    expect(hasRequirementChat('structured')).toBe(spec.needsRequirementChat)
    expect(hasDocumentReview('structured')).toBe(spec.needsDocumentReview)
    expect(inputPageOnBar('structured')).toBe(spec.inputPageOnBar)
    expect(
      hasRequiredDocuments('structured', { prd: '# prd', apiDocs: '# api', prompts: '# prompts' }),
    ).toBe(true)
    expect(hasRequiredDocuments('structured', { prd: '# prd', apiDocs: '', prompts: '' })).toBe(
      false,
    )
  })

})

/**
 * 登记表的第二行：快捷流程。
 *
 * 它与完整流程**共用同一条流水线**，差别全在这一行里：起点是一段话而不是表单、
 * 没有需求澄清、不审不改、落页是自己的极简录入页。期望值逐列写死，不从实现里取。
 */
describe('入口模式登记表：快捷流程一行', () => {
  it('七类资料为列，每一列都填了「快捷流程」的答案', () => {
    const spec = entrySpecFor('prd-quick')
    if (!spec) throw new Error('登记表缺「快捷流程」一行')

    // ① 键、组别与卡片文案：与入口页上那张卡一致（文案照抄，不改写）
    expect(spec.mode).toBe('prd-quick')
    expect(spec.card.groupId).toBe('from-scratch')
    expect(spec.card.groupTitle).toBe('从零创建 PRD')
    expect(spec.card.groupDescription).toBe('还没有任何材料，从这里把需求说清楚。')
    expect(spec.card.title).toBe('快捷流程')
    expect(spec.card.description).toBe('一段话说清想法，跳过表单与对话，直接逐章生成 PRD。')

    // ② 落页：选中落在极简描述页；「改描述」回同一页；返回落点也是它
    expect(spec.landing.onSelect).toBe('prd-quick')
    expect(spec.landing.editRequirements).toBe('prd-quick')
    expect(spec.landing.backToInput).toBe('prd-quick')

    // ③ 进度条步骤：与完整流程同一套六步——「AI 对话」留给文档澄清
    expect(spec.steps).toEqual([
      'form',
      'chatting',
      'review-prd',
      'review-api-docs',
      'review-prompts',
      'done',
    ])

    // ④ 四个布尔：录入页在进度条上、**不要**需求澄清、**不审不改**、保留「返回」
    expect(spec.inputPageOnBar).toBe(true)
    expect(spec.needsRequirementChat).toBe(false)
    expect(spec.needsDocumentReview).toBe(false)
    expect(spec.keepBackButton).toBe(true)

    // ⑤ 完成所需文档：与完整流程一致，三份齐备
    expect(spec.requiredDocs).toEqual(['prd', 'api-docs', 'prompts'])

    // ⑥ 文案：列表模式名、返回按钮、改描述按钮
    expect(spec.copy.modeLabel).toBe('快捷流程')
    expect(spec.copy.backLabel).toBe('返回对话')
    expect(spec.copy.editRequirementsLabel).toBe('改描述')

    // ⑦ 录入字段：落在快照的 `quickInput`（产品名与需求描述），非空即算有内容
    expect(spec.input.stateField).toBe('quickInput')
    expect(spec.input.fields.map((field) => field.key)).toEqual(['product_name', 'requirements'])
    expect(spec.input.fields.map((field) => field.label)).toEqual(['产品名称', '需求描述'])
    expect(spec.input.fields.map((field) => field.countsAsContent)).toEqual([true, true])
  })

  it('快捷流程不被追问需求：问询给出「不澄清」，也没有摘要回填的余地', () => {
    expect(hasRequirementChat('prd-quick')).toBe(false)
    // 同一行驱动的其它两列也得跟着对，避免「答了半个入口」
    expect(hasDocumentReview('prd-quick')).toBe(false)
    expect(inputPageOnBar('prd-quick')).toBe(true)
  })
})

/**
 * 登记表的第三行：我有 PRD。
 *
 * 两个**导入**入口的共同点是「自带材料、只补做提示词套件」：录入页不在进度条上（所以审阅页
 * 必须留「返回」），完成只要提示词。它与下一个入口的差别只在贴几份材料。
 */
describe('入口模式登记表：我有 PRD 一行', () => {
  it('七类资料为列，每一列都填了「我有 PRD」的答案', () => {
    const spec = entrySpecFor('prompt-debug')
    if (!spec) throw new Error('登记表缺「我有 PRD」一行')

    // ① 键、组别与卡片文案：第二个分组的第一个叶子
    expect(spec.mode).toBe('prompt-debug')
    expect(spec.card.groupId).toBe('with-material')
    expect(spec.card.groupTitle).toBe('导入已有素材')
    expect(spec.card.groupDescription).toBe('手头已经有写好的材料，别重复劳动。')
    expect(spec.card.title).toBe('我有 PRD')
    expect(spec.card.description).toBe('贴一份现成 PRD，直接生成提示词套件，之后可用 AI 优化反复调。')

    // ② 落页：选中落在粘贴 PRD 页；「返回」回它
    expect(spec.landing.onSelect).toBe('prompt-debug')
    // 「改需求」与旧版一致地落在结构化表单：语义不理想，但本期是行为不变的裁决
    // （导入入口没有需求澄清线程，这一列只为旧快照存在）。要改另开票。
    expect(spec.landing.editRequirements).toBe('form')
    expect(spec.landing.backToInput).toBe('prompt-debug')

    // ③ 进度条步骤：产物只有提示词，两格
    expect(spec.steps).toEqual(['review-prompts', 'done'])

    // ④ 四个布尔：录入页不在进度条上、不要需求澄清、要审核改写、保留「返回」
    expect(spec.inputPageOnBar).toBe(false)
    expect(spec.needsRequirementChat).toBe(false)
    expect(spec.needsDocumentReview).toBe(true)
    expect(spec.keepBackButton).toBe(true)

    // ⑤ 完成所需文档：只要提示词套件（自带的那份 PRD 不算本工具的产物）
    expect(spec.requiredDocs).toEqual(['prompts'])

    // ⑥ 文案：列表模式名沿用「导入 PRD」、返回写「返回 PRD」
    expect(spec.copy.modeLabel).toBe('导入 PRD')
    expect(spec.copy.backLabel).toBe('返回 PRD')
    expect(spec.copy.editRequirementsLabel).toBe('改需求')

    // ⑦ 录入字段：贴入的 PRD 落在快照的 `debugPrdText`
    expect(spec.input.stateField).toBe('debugPrdText')
    expect(spec.input.fields.map((field) => field.key)).toEqual(['prd'])
    expect(spec.input.fields.map((field) => field.label)).toEqual(['PRD 正文（Markdown）'])
    expect(spec.input.fields.every((field) => field.countsAsContent)).toBe(true)
  })

  it('两个导入入口口径一致：两格进度条、只要提示词即可完成、录入页不在条上', () => {
    for (const mode of ['prompt-debug', 'prd-and-api-paste'] as const) {
      expect(stepsForMode(mode).map((step) => step.id)).toEqual(['review-prompts', 'done'])
      expect(hasRequiredDocuments(mode, { prd: '# p', apiDocs: '# a', prompts: '' })).toBe(false)
      expect(hasRequiredDocuments(mode, { prd: '', apiDocs: '', prompts: '# p' })).toBe(true)
      expect(inputPageOnBar(mode)).toBe(false)
      expect(hasRequirementChat(mode)).toBe(false)
      expect(hasDocumentReview(mode)).toBe(true)
    }
  })
})

/**
 * 登记表的第四行：我有 PRD + 接口文档。
 *
 * 它与「我有 PRD」同属导入组，口径应当逐列一致，只有录入页与两份文案不同。
 */
describe('入口模式登记表：我有 PRD + 接口文档一行', () => {
  it('七类资料为列，每一列都填了「我有 PRD + 接口文档」的答案', () => {
    const spec = entrySpecFor('prd-and-api-paste')
    if (!spec) throw new Error('登记表缺「我有 PRD + 接口文档」一行')

    // ① 键、组别与卡片文案：第二个分组的第二个叶子
    expect(spec.mode).toBe('prd-and-api-paste')
    expect(spec.card.groupId).toBe('with-material')
    expect(spec.card.groupTitle).toBe('导入已有素材')
    expect(spec.card.groupDescription).toBe('手头已经有写好的材料，别重复劳动。')
    expect(spec.card.title).toBe('我有 PRD + 接口文档')
    expect(spec.card.description).toBe('两份都贴进来，提示词因此能对齐真实的接口路径与字段名。')

    // ② 落页：选中落在双粘贴页；「返回」回它
    expect(spec.landing.onSelect).toBe('prd-and-api-paste')
    // 同「我有 PRD」：改需求落结构化表单是旧版行为，本期不变
    expect(spec.landing.editRequirements).toBe('form')
    expect(spec.landing.backToInput).toBe('prd-and-api-paste')

    // ③ 进度条步骤：与另一个导入入口同口径，两格
    expect(spec.steps).toEqual(['review-prompts', 'done'])

    // ④ 四个布尔：录入页不在进度条上、不要需求澄清、要审核改写、保留「返回」
    expect(spec.inputPageOnBar).toBe(false)
    expect(spec.needsRequirementChat).toBe(false)
    expect(spec.needsDocumentReview).toBe(true)
    expect(spec.keepBackButton).toBe(true)

    // ⑤ 完成所需文档：只要提示词套件
    expect(spec.requiredDocs).toEqual(['prompts'])

    // ⑥ 文案：列表模式名沿用「导入 PRD + 接口文档」、返回写「返回素材」
    expect(spec.copy.modeLabel).toBe('导入 PRD + 接口文档')
    expect(spec.copy.backLabel).toBe('返回素材')
    expect(spec.copy.editRequirementsLabel).toBe('改需求')

    // ⑦ 录入字段：两份材料都落在快照的 `pastePairInput`（PRD 为主内容列）
    expect(spec.input.stateField).toBe('pastePairInput')
    expect(spec.input.stateColumn).toBe('paste_prd_text')
    expect(spec.input.fields.map((field) => field.key)).toEqual(['prd', 'api_docs'])
    expect(spec.input.fields.map((field) => field.label)).toEqual([
      'PRD 正文（Markdown）',
      '接口文档正文（Markdown）',
    ])
    expect(spec.input.fields.every((field) => field.countsAsContent)).toBe(true)
  })
})

/**
 * 四行齐全。
 *
 * 这一条是「旧表已退役」的正面证据：四个入口全部在登记表里，问询函数只可能从这些行取答案。
 */
describe('入口模式登记表：四行齐全', () => {
  it('四个入口各占一行，没有缺行', () => {
    const modes = ['structured', 'prd-quick', 'prompt-debug', 'prd-and-api-paste'] as const
    expect(Object.keys(ENTRY_REGISTRY).sort()).toEqual([...modes].sort())
    for (const mode of modes) {
      expect(ENTRY_REGISTRY[mode]).toBeDefined()
      expect(ENTRY_REGISTRY[mode]?.mode).toBe(mode)
      expect(entrySpecFor(mode)?.mode).toBe(mode)
    }
  })

  it('四行的组别与卡片文案合起来就是入口页上的两组四叶', () => {
    const fromScratch = Object.values(ENTRY_REGISTRY).filter(
      (spec) => spec?.card.groupId === 'from-scratch',
    )
    const withMaterial = Object.values(ENTRY_REGISTRY).filter(
      (spec) => spec?.card.groupId === 'with-material',
    )
    expect(fromScratch.map((spec) => spec?.card.title)).toEqual(['完整流程', '快捷流程'])
    expect(withMaterial.map((spec) => spec?.card.title)).toEqual(['我有 PRD', '我有 PRD + 接口文档'])
  })

  it('每个入口的问询都从自己那一行取答案（登记表真的被读到）', () => {
    const modes = ['structured', 'prd-quick', 'prompt-debug', 'prd-and-api-paste'] as const
    for (const mode of modes) {
      const spec = ENTRY_REGISTRY[mode]
      if (!spec) throw new Error(`登记表缺 ${mode} 一行`)
      expect(stepsForMode(mode).map((step) => step.id)).toEqual([...spec.steps])
      expect(hasRequirementChat(mode)).toBe(spec.needsRequirementChat)
      expect(hasDocumentReview(mode)).toBe(spec.needsDocumentReview)
      expect(inputPageOnBar(mode)).toBe(spec.inputPageOnBar)
      // 完成判据按这一行登记的那几份文档算：凑齐它们是 true，少一份是 false
      const all = { prd: '# prd', apiDocs: '# api', prompts: '# prompts' }
      expect(hasRequiredDocuments(mode, all)).toBe(true)
      expect(hasRequiredDocuments(mode, { prd: '', apiDocs: '', prompts: '' })).toBe(false)
    }
  })
})

/**
 * 入口登记表完整性：漏行漏列当场失败。
 *
 * 三种「没有」必须分清，这是本票的核心口径：
 * - `null` = **「未选择」**，合法：照完整流程展示，绝不抛错；
 * - **未知入口键**（从没登记过的键）= 失败，不猜任何默认值；
 * - **缺行 / 缺列**（维护者加了第 5 个入口却漏填一列）= 失败，消息里点名缺的是哪一列。
 *
 * 这里只断言**外部可观察结果**：问询抛不抛错、抛出的消息能不能让维护者一眼定位。
 * 不扫源码、不数落点、不查标识符词频。
 */
describe('入口登记表完整性：漏行漏列当场失败', () => {
  /** 取一次调用的错误消息；没抛错就是空串（于是后面的 `toContain` 会红） */
  function messageOf(run: () => unknown): string {
    try {
      run()
    } catch (error) {
      return error instanceof Error ? error.message : String(error)
    }
    return ''
  }

  /**
   * 一份「填全了」的假第 5 入口。
   *
   * 逐条用例只在它身上挖掉一列，其余保持完整——这样「被拒」只可能是被挖掉的那一列，
   * 不会出现「反正哪儿都不全，随便拒绝一下也算通过」。
   */
  function completeFifthEntry(): EntrySpec {
    return {
      mode: 'fifth-entry' as EntryMode,
      card: {
        groupId: 'from-scratch',
        groupTitle: '从零创建 PRD',
        groupDescription: '还没有任何材料，从这里把需求说清楚。',
        title: '第五入口',
        description: '维护者新加的一行。',
      },
      landing: {
        onSelect: 'prompt-debug',
        editRequirements: 'prompt-debug',
        backToInput: 'prompt-debug',
      },
      steps: ['review-prompts', 'done'],
      inputPageOnBar: false,
      needsRequirementChat: false,
      needsDocumentReview: false,
      keepBackButton: false,
      requiredDocs: ['prompts'],
      copy: {
        modeLabel: '第五入口',
        backLabel: '返回素材',
        editRequirementsLabel: '改需求',
      },
      input: {
        stateField: 'debugPrdText',
        stateColumn: 'draft_prd_text',
        fields: [{ key: 'prd', label: 'PRD 正文（Markdown）', defaultValue: '', countsAsContent: true }],
      },
    }
  }

  it('填全了的一行能通过校验（正例：否则「全都拒绝」也会看起来像通过）', () => {
    const spec = completeFifthEntry()
    expect(assertEntrySpecComplete('fifth-entry' as EntryMode, spec)).toBe(spec)
  })

  it('只填一半的假第 5 入口被当场拒绝，消息指出缺的是哪一列', () => {
    // 维护者最容易漏的一列：完成所需文档。空数组就是典型的「忘了填」
    const halfFilled: EntrySpec = { ...completeFifthEntry(), requiredDocs: [] }

    expect(() => assertEntrySpecComplete('fifth-entry' as EntryMode, halfFilled)).toThrow(
      EntrySpecMissingError,
    )
    const message = messageOf(() => assertEntrySpecComplete('fifth-entry' as EntryMode, halfFilled))
    // 维护者一眼定位：哪个入口、缺哪一列
    expect(message).toContain('fifth-entry')
    expect(message).toContain('requiredDocs')
  })

  it('文案列空了同样当场失败（不是只盯步骤那一列）', () => {
    const halfFilled: EntrySpec = {
      ...completeFifthEntry(),
      copy: { modeLabel: '', backLabel: '返回素材', editRequirementsLabel: '改需求' },
    }
    expect(messageOf(() => assertEntrySpecComplete('fifth-entry' as EntryMode, halfFilled))).toContain(
      'modeLabel',
    )
  })

  it('缺行与未知入口键在问询时失败，不返回任何默认值', () => {
    // 没登记过的键：展示口径当场失败，绝不拿完整流程那一行顶替
    expect(() => entrySpecForDisplay('bogus' as EntryMode)).toThrow(EntrySpecMissingError)
    expect(messageOf(() => entrySpecForDisplay('bogus' as EntryMode))).toContain('bogus')

    // 失败路径覆盖所有按模式的问询，而不是只在某一个函数上生效
    expect(() => stepsForMode('bogus' as EntryMode)).toThrow(EntrySpecMissingError)
    expect(() => hasRequiredDocuments('bogus' as EntryMode, {
      prd: '# prd',
      apiDocs: '# api',
      prompts: '# promises',
    })).toThrow(EntrySpecMissingError)
  })

  it('往登记表里放一行只填一半的假第 5 入口 → 问询当场被拒（最高可注入点）', () => {
    const key = 'fifth-half-filled' as EntryMode
    const halfFilled: EntrySpec = { ...completeFifthEntry(), mode: key, requiredDocs: [] }

    // 登记表是运行时读的，所以这里能真的把它塞进去——这就是维护者「加了一行」的那一步
    ENTRY_REGISTRY[key] = halfFilled
    try {
      expect(() => entrySpecForDisplay(key)).toThrow(EntrySpecMissingError)
      // 同一个残缺行，换一个问询函数也照样被拒：缺的是那一列，不是某个入口的局部问题
      expect(messageOf(() => stepsForMode(key))).toContain('requiredDocs')
    } finally {
      delete ENTRY_REGISTRY[key]
    }
  })

  it('未选择（null）仍走合法口径：不抛错，就是完整流程那一行', () => {
    let spec: EntrySpec | undefined
    expect(() => {
      spec = entrySpecForDisplay(null)
    }).not.toThrow()
    expect(spec).toEqual(ENTRY_REGISTRY.structured)
    // 四个已登记的入口都填齐了：日常路径不会因为这条守卫变红
    for (const mode of ['structured', 'prd-quick', 'prompt-debug', 'prd-and-api-paste'] as const) {
      expect(() => entrySpecForDisplay(mode)).not.toThrow()
    }
  })
})
