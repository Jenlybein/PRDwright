import { describe, expect, it } from 'vitest'

import { STEP_OF_STATE } from '@/types'
import type { ChatMessage, RequirementsSummary, SessionDataV2 } from '@/types'
import {
  canGeneratePrdFrom,
  docKindOfState,
  generatingStateFor,
  hasRequiredDocuments,
  isDocKind,
  isDocumentKind,
  isEntryMode,
  isViewState,
  parseRunSummary,
  parseSessionSnapshot,
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
    ])
    // 快捷入口跳过表单与对话，不该显示两步点不动的空壳
    expect(stepsForMode('prd-quick').map((step) => step.id)).toEqual([
      'review-prd',
      'review-api-docs',
      'review-prompts',
    ])
    expect(stepsForMode('prompt-debug').map((step) => step.id)).toEqual(['review-prompts'])
    // 还没选入口（或旧快照）时按完整流程展示
    expect(stepsForMode(null).map((step) => step.id)).toEqual(
      stepsForMode('structured').map((step) => step.id),
    )
  })

  it('步骤下标按模式口径计算', () => {
    // 快捷入口的「PRD」是它的第 1 步，而不是全局的第 3 步
    expect(stepIndexOf('prd-quick', 'prd-quick')).toBe(0)
    expect(stepIndexOf('review-api-docs', 'prd-quick')).toBe(1)
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
    // 快捷入口：有 PRD 是第 1 步，再加接口文档才是第 2 步
    expect(
      reachedStepIndex({ ...base, mode: 'prd-quick', hasPrd: true, currentStepIndex: 0 }),
    ).toBe(0)
    expect(
      reachedStepIndex({
        ...base,
        mode: 'prd-quick',
        hasPrd: true,
        hasApiDocs: true,
        currentStepIndex: 0,
      }),
    ).toBe(1)
  })

  it('完成条件按各入口模式的交付物判断', () => {
    const onlyPrd = { prd: 'a', apiDocs: '', prompts: '' }
    expect(hasRequiredDocuments('structured', onlyPrd)).toBe(false)
    expect(hasRequiredDocuments('prd-quick', onlyPrd)).toBe(true)
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
    expect(stepIndexOf('done')).toBe(4)
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

describe('会话快照解析', () => {
  const full: SessionDataV2 = {
    viewState: 'review-prd',
    entryMode: 'structured',
    summary: { product_name: 'X' } as unknown as RequirementsSummary,
    messages: [{ role: 'user', content: 'hi' }],
    conversationId: 'c1',
    quickInput: { productName: '', requirements: '' },
    debugPrdText: '',
    activeJobId: null,
    activeJobKind: null,
    clarifyMessages: [],
    clarifyConversationId: '',
    clarifyTarget: null,
    prdClarifyConversationId: '',
    prdClarifying: false,
  }

  it('空值与非法 JSON 一律返回 null', () => {
    expect(parseSessionSnapshot(null)).toBeNull()
    expect(parseSessionSnapshot('')).toBeNull()
    expect(parseSessionSnapshot('{oops')).toBeNull()
    expect(parseSessionSnapshot('"只是个字符串"')).toBeNull()
  })

  it('正常快照原样还原', () => {
    const parsed = parseSessionSnapshot(JSON.stringify(full))
    expect(parsed?.session.viewState).toBe('review-prd')
    expect(parsed?.session.entryMode).toBe('structured')
    expect(parsed?.session.messages).toEqual([{ role: 'user', content: 'hi' }])
    expect(parsed?.downgrade).toBeNull()
  })

  it('产物正文与用量回执不再是快照的一部分：旧快照里残留的它们被忽略', () => {
    // 这一条守的是「文档所有权在后端」——正文与回执已经搬到 `documents` 表，
    // 快照里不该再出现它们。旧快照（上个版本存下来的）里当然还有，
    // 解析器要做的是**丢掉**而不是还原。
    const parsed = parseSessionSnapshot(
      JSON.stringify({
        ...full,
        prdContent: '# 旧快照里的正文',
        apiDocsContent: '# 旧接口文档',
        promptsContent: '# 旧提示词',
        prdRunSummary: { kind: 'prd', durationMs: 1, requestId: 'p' },
      }),
    )
    expect(parsed?.session).not.toHaveProperty('prdContent')
    expect(parsed?.session).not.toHaveProperty('prdRunSummary')
    expect(Object.keys(parsed?.session ?? {})).toEqual([
      'viewState',
      'entryMode',
      'summary',
      'messages',
      'conversationId',
      'quickInput',
      'debugPrdText',
      'activeJobId',
      'activeJobKind',
      'clarifyMessages',
      'clarifyConversationId',
      'clarifyTarget',
      'prdClarifyConversationId',
      'prdClarifying',
    ])
  })

  it('parseRunSummary 只认带关键字段的对象，脏数据一律退化', () => {
    expect(parseRunSummary(null)).toBeNull()
    expect(parseRunSummary('x')).toBeNull()
    expect(parseRunSummary([])).toBeNull()
    expect(parseRunSummary({ kind: 'prd' })).toBeNull()
    expect(parseRunSummary({ kind: 'prd', durationMs: 1 })).toBeNull()
    expect(parseRunSummary({ kind: 'prd', durationMs: 1, requestId: 'r' })).not.toBeNull()
  })

  it('入口模式与两处录入内容随快照还原', () => {
    const parsed = parseSessionSnapshot(
      JSON.stringify({
        ...full,
        viewState: 'prompt-debug',
        entryMode: 'prompt-debug',
        debugPrdText: '# 别人的 PRD',
        quickInput: { productName: '小账本', requirements: '记账' },
      }),
    )
    expect(parsed?.session.entryMode).toBe('prompt-debug')
    expect(parsed?.session.debugPrdText).toBe('# 别人的 PRD')
    expect(parsed?.session.quickInput).toEqual({ productName: '小账本', requirements: '记账' })
  })

  it('旧快照（没有入口模式字段）按完整流程处理', () => {
    const parsed = parseSessionSnapshot(
      JSON.stringify({
        viewState: 'form',
        summary: null,
        messages: [],
        conversationId: '',
        prdContent: '',
        apiDocsContent: '',
        promptsContent: '',
      }),
    )
    expect(parsed?.session.entryMode).toBeNull()
    expect(parsed?.session.quickInput).toEqual({ productName: '', requirements: '' })
    expect(parsed?.session.debugPrdText).toBe('')
    expect(parsed?.downgrade).toBeNull()
  })

  it('生成中的快照会降级并记录原因', () => {
    const parsed = parseSessionSnapshot(JSON.stringify({ ...full, viewState: 'generating-prd' }))
    expect(parsed?.session.viewState).toBe('review-prd')
    expect(parsed?.downgrade).toBe('generating')
  })

  it('脏字段只丢弃自己，不让整份快照失效', () => {
    const parsed = parseSessionSnapshot(
      JSON.stringify({
        ...full,
        conversationId: 42,
        messages: [{ role: 'bogus', content: 'x' }, { role: 'user', content: 'ok' }],
      }),
    )
    expect(parsed?.session.conversationId).toBe('')
    expect(parsed?.session.messages).toEqual([{ role: 'user', content: 'ok' }])
    // 同一条快照里合法的字段仍然保留
    expect(parsed?.session.viewState).toBe('review-prd')
    expect(parsed?.session.summary).toEqual({ product_name: 'X' })
  })

  it('入口相关的脏值各退化为自己的默认值', () => {
    const parsed = parseSessionSnapshot(
      JSON.stringify({
        ...full,
        entryMode: 'bogus',
        quickInput: 'not-an-object',
        debugPrdText: 42,
      }),
    )
    expect(parsed?.session.entryMode).toBeNull()
    expect(parsed?.session.quickInput).toEqual({ productName: '', requirements: '' })
    expect(parsed?.session.debugPrdText).toBe('')
  })

  it('录入内容字段部分缺失时逐字段退化', () => {
    const parsed = parseSessionSnapshot(
      JSON.stringify({ ...full, quickInput: { productName: '小账本' } }),
    )
    expect(parsed?.session.quickInput).toEqual({ productName: '小账本', requirements: '' })
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

  it('isDocKind 只承认五种文档种类', () => {
    expect(isDocKind('prd')).toBe(true)
    expect(isDocKind('api-docs')).toBe(true)
    expect(isDocKind('prompts')).toBe(true)
    expect(isDocKind('optimize')).toBe(true)
    expect(isDocKind('clarify')).toBe(true)
    expect(isDocKind('bogus')).toBe(false)
    expect(isDocKind(null)).toBe(false)
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

  it('generatingStateFor / docKindOfState 互为逆映射（两个动作都不占生成阶段）', () => {
    expect(generatingStateFor('prd')).toBe('generating-prd')
    expect(generatingStateFor('api-docs')).toBe('generating-api-docs')
    expect(generatingStateFor('prompts')).toBe('generating-prompts')
    // 就地动某一篇：优化留在审阅页、澄清留在澄清页，都不切到 generating-*
    expect(generatingStateFor('optimize')).toBeNull()
    expect(generatingStateFor('clarify')).toBeNull()
    expect(docKindOfState('review-prd')).toBe('prd')
    expect(docKindOfState('review-api-docs')).toBe('api-docs')
    expect(docKindOfState('review-prompts')).toBe('prompts')
    // 澄清阶段反推不出产物：是哪一份要看 `clarifyTarget`，不能靠阶段猜
    expect(docKindOfState('clarifying')).toBeNull()
    expect(docKindOfState('entry')).toBeNull()
  })

  it('reviewStateFor 是生成态映射的逆：产物 → 它的审阅页', () => {
    expect(reviewStateFor('prd')).toBe('review-prd')
    expect(reviewStateFor('api-docs')).toBe('review-api-docs')
    expect(reviewStateFor('prompts')).toBe('review-prompts')
  })
})

/**
 * 澄清阶段（`clarifying`）的四处同步。
 *
 * 加一个 `ViewState` 要同时改四处（`ViewState` / `VIEW_STATES` / `STEP_OF_STATE` /
 * `STATE_DOWNGRADE`），漏一处不会报错、只会表现为「进度条算错」或「刷新后进不去」。
 * 这里把四处钉在一起，顺带把「澄清**不**降级」这条口径也钉住。
 */
describe('澄清阶段的登记与降级口径', () => {
  it('已登记进 VIEW_STATES，且进度条归属挂在审阅步骤上', () => {
    expect(VIEW_STATES).toContain('clarifying')
    expect(isViewState('clarifying')).toBe(true)
    // 静态兜底：精确的那一档由 store 按 `clarifyTarget` 算（见 stores/workflow.ts）
    expect(STEP_OF_STATE_FOR_TEST('clarifying')).toBe('review-prd')
  })

  it('不做刷新降级：对话与目标都随快照持久化，刷新后能接着聊', () => {
    expect(resolveRestoredPhase('clarifying')).toEqual({
      viewState: 'clarifying',
      downgrade: null,
    })
  })
})

/** 直接读 `STEP_OF_STATE`，避免在测试里重复一份映射表 */
function STEP_OF_STATE_FOR_TEST(state: 'clarifying') {
  return STEP_OF_STATE[state]
}
