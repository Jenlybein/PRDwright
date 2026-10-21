import { describe, expect, it } from 'vitest'

import type { ChatMessage, RequirementsSummary, SessionDataV2 } from '@/types'
import {
  canGeneratePrdFrom,
  chatStageForDocument,
  docKindOfGeneratingState,
  docKindOfState,
  documentKindOfChatStage,
  emptyConversationIds,
  fromBackendKind,
  generatingStateFor,
  hasDocumentReview,
  hasRequiredDocuments,
  hasRequirementChat,
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
    // 还没选入口（或旧快照）时按完整流程处理，与 `MODE_STEP_IDS` 的兜底口径一致
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
 * 澄清线程的**四条线程合流**（ADR-028）。
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
