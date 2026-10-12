import { describe, expect, it } from 'vitest'

import type { ChatMessage, RequirementsSummary, SessionDataV2 } from '@/types'
import {
  canGeneratePrdFrom,
  hasRequiredDocuments,
  isEntryMode,
  isViewState,
  parseSessionSnapshot,
  reachedStepIndex,
  resolveRestoredPhase,
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
    expect(resolveRestoredPhase('rag-preview')).toEqual({
      viewState: 'review-prd',
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
    expect(stepIndexOf('rag-preview', 'prd-quick')).toBe(0)
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
    expect(stepIndexOf('rag-preview')).toBe(2)
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
    prdContent: '# PRD',
    apiDocsContent: '',
    promptsContent: '',
  }

  it('空值与非法 JSON 一律返回 null', () => {
    expect(parseSessionSnapshot(null)).toBeNull()
    expect(parseSessionSnapshot('')).toBeNull()
    expect(parseSessionSnapshot('{oops')).toBeNull()
    expect(parseSessionSnapshot('"只是个字符串"')).toBeNull()
  })

  it('正常快照原样还原', () => {
    const parsed = parseSessionSnapshot(JSON.stringify(full))
    expect(parsed?.session.prdContent).toBe('# PRD')
    expect(parsed?.session.viewState).toBe('review-prd')
    expect(parsed?.session.entryMode).toBe('structured')
    expect(parsed?.downgrade).toBeNull()
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
    expect(parsed?.session.prdContent).toBe('# PRD')
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
