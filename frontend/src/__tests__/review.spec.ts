import { describe, expect, it } from 'vitest'

import { DEFECT_CATEGORIES, parseReview } from '../utils/review'

/**
 * `parseReview` 的**类别归一**（ADR 0003）。
 *
 * 结论有两个数据源——SSE 的 `review` 事件与落库的文档记录——两者共用这一个解析器。
 * 类别在这里归一的意义：面板的分组与计数只认契约里的六类；一条错别字或不认识的
 * 字符串不该在界面上凭空多出一个分组。
 *
 * 形态断言（不是行为断言）在这里是合适的：归一化的产出**就是**这个形状，而它正是
 * 两个数据源「刷新前后长得一样」的前提。
 */
describe('parseReview：缺陷类别归一', () => {
  function raw(issues: unknown[]): Record<string, unknown> {
    return {
      kind: 'api_docs',
      verdict: 'revise',
      summary: '2 个待处理问题',
      issues,
      rewritten: false,
      failed_sections: [],
      reviewed_at: '2026-10-13T00:00:00',
    }
  }

  it('六类原样保留：面板据此分组与计数', () => {
    const parsed = parseReview(
      raw(DEFECT_CATEGORIES.map((category) => ({ category, severity: 'medium', detail: 'x' }))),
    )

    expect(parsed?.issues.map((issue) => issue.category)).toEqual([...DEFECT_CATEGORIES])
  })

  it('旧结论没有 category → null：「未分类」，不推断', () => {
    const parsed = parseReview(raw([{ severity: 'high', detail: '编造了鉴权方式' }]))

    expect(parsed?.issues[0]?.category).toBeNull()
  })

  it('不认识的类别归一成 null：分组取值域是有限的六类', () => {
    const parsed = parseReview(raw([{ category: 'coverage_missing', severity: 'high', detail: 'x' }]))

    expect(parsed?.issues[0]?.category).toBeNull()
  })
})
