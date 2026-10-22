/**
 * 回执归一化的用例。
 *
 * 这个文件钉住一次真实故障：回执有**两个来源、两种字段风格**——
 * SSE `run_summary` 事件与 `documents.last_run_summary` 都是后端 dataclass 直出的
 * `snake_case`，而前端两处各写了一套解析，落库那侧直接把对象断言成 `RunSummary`。
 * 结果是**刷新页面后回执整块消失**，且不报错。
 *
 * 所以这里的重点不是「字段映射对不对」，而是「两种键都得认」。
 */

import { describe, expect, it } from 'vitest'

import { normalizeRunSummary } from '@/utils/runSummary'

/** 后端真实发出的形状（snake_case） */
function backendSummary(overrides: Record<string, unknown> = {}) {
  return {
    kind: 'prd',
    status: 'completed',
    duration_ms: 61_000,
    total_calls: 12,
    failed_calls: 1,
    prompt_tokens: 90_000,
    completion_tokens: 8_000,
    steps: [
      {
        step: 'prd:writing',
        calls: 10,
        failed_calls: 1,
        prompt_tokens: 85_000,
        completion_tokens: 7_500,
        duration_ms: 55_000,
      },
    ],
    injected_skills: [{ name: 'prd-generator', version: '0.1.0', fingerprint: 'e4f928df7be9' }],
    warnings: ['prd:writing 的输入已达预算上限的 88%'],
    partial: true,
    failed_sections: [{ section_id: 'tech', title: '技术方案', reason: '超时' }],
    protocol: 'ark',
    provider_config_id: 'cfg_ark0001',
    provider_name: '火山方舟',
    model: 'doubao-seed-2-1-turbo',
    request_id: 'req-abc',
    ...overrides,
  }
}

describe('normalizeRunSummary', () => {
  it('认得后端那份 snake_case（事件与落库同形）', () => {
    const summary = normalizeRunSummary(backendSummary())

    expect(summary).not.toBeNull()
    expect(summary!.kind).toBe('prd')
    expect(summary!.durationMs).toBe(61_000)
    expect(summary!.totalCalls).toBe(12)
    expect(summary!.failedCalls).toBe(1)
    expect(summary!.promptTokens).toBe(90_000)
    expect(summary!.completionTokens).toBe(8_000)
    expect(summary!.partial).toBe(true)
    expect(summary!.requestId).toBe('req-abc')
    expect(summary!.steps).toEqual([
      {
        step: 'prd:writing',
        calls: 10,
        failedCalls: 1,
        promptTokens: 85_000,
        completionTokens: 7_500,
        durationMs: 55_000,
      },
    ])
    expect(summary!.injectedSkills).toHaveLength(1)
    expect(summary!.warnings).toEqual(['prd:writing 的输入已达预算上限的 88%'])
    expect(summary!.failedSections).toEqual([
      { sectionId: 'tech', title: '技术方案', reason: '超时' },
    ])
  })

  it('也认 camelCase（历史快照与迁移数据里出现过）', () => {
    const summary = normalizeRunSummary({
      kind: 'prd',
      durationMs: 1_000,
      totalCalls: 2,
      failedCalls: 0,
      promptTokens: 10,
      completionTokens: 20,
      steps: [{ step: 'prd', calls: 2, failedCalls: 0, promptTokens: 10, completionTokens: 20, durationMs: 900 }],
      injectedSkills: [{ name: 'a', version: '1', fingerprint: 'f' }],
      providerConfigId: 'cfg_x',
      providerName: 'X',
      requestId: 'req-old',
    })

    expect(summary!.durationMs).toBe(1_000)
    expect(summary!.totalCalls).toBe(2)
    expect(summary!.steps[0]!.durationMs).toBe(900)
    expect(summary!.injectedSkills[0]!.name).toBe('a')
    expect(summary!.providerConfigId).toBe('cfg_x')
    expect(summary!.requestId).toBe('req-old')
  })

  it('供应商身份拆成三个字段，老回执里的 provider 不会被猜成协议', () => {
    const summary = normalizeRunSummary(backendSummary())

    expect(summary!.protocol).toBe('ark')
    expect(summary!.providerConfigId).toBe('cfg_ark0001')
    expect(summary!.providerName).toBe('火山方舟')
    expect(summary!.model).toBe('doubao-seed-2-1-turbo')

    // v19 之前的回执只有 `provider`（存的是协议名），且没有配置 id/名字。
    // 这里刻意**不**把 `provider` 映射到 `protocol`：那是猜，猜错会让账单行看起来
    // 像是「同一条配置」，而它其实只是同一个协议。
    const legacyRaw = backendSummary({ provider: 'ark' })
    delete (legacyRaw as Record<string, unknown>).provider_config_id
    delete (legacyRaw as Record<string, unknown>).provider_name
    delete (legacyRaw as Record<string, unknown>).protocol
    const legacy = normalizeRunSummary(legacyRaw)
    expect(legacy!.protocol).toBe('')
    expect(legacy!.providerConfigId).toBe('')
    expect(legacy!.providerName).toBe('')
  })

  it('不像回执的东西一律 null（宁可不显示，也不显示空壳）', () => {
    expect(normalizeRunSummary(null)).toBeNull()
    expect(normalizeRunSummary(undefined)).toBeNull()
    expect(normalizeRunSummary('一段文本')).toBeNull()
    expect(normalizeRunSummary([])).toBeNull()
    // 缺 kind：分不清是哪种生成，回执上的「技能包」等判断都会误判
    expect(normalizeRunSummary({ duration_ms: 100 })).toBeNull()
    // 缺耗时：整行回执的核心信息就是它
    expect(normalizeRunSummary({ kind: 'prd' })).toBeNull()
  })

  it('坏项只退化那一项，不让整份回执失效', () => {
    const summary = normalizeRunSummary(
      backendSummary({
        steps: ['坏项', { step: 'prd:writing', calls: 3 }, 42],
        injected_skills: ['坏项', { name: 'prd-generator' }],
        warnings: ['合法告警', 7, null],
        failed_sections: ['坏项'],
      }),
    )

    expect(summary).not.toBeNull()
    expect(summary!.steps).toEqual([
      {
        step: 'prd:writing',
        calls: 3,
        failedCalls: 0,
        promptTokens: 0,
        completionTokens: 0,
        durationMs: 0,
      },
    ])
    expect(summary!.injectedSkills).toEqual([{ name: 'prd-generator', version: '', fingerprint: '' }])
    expect(summary!.warnings).toEqual(['合法告警'])
    expect(summary!.failedSections).toEqual([])
  })
})
