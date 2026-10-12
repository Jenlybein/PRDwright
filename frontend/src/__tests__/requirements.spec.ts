import { describe, expect, it } from 'vitest'

import type { RequirementsSummary } from '../types'
import {
  buildAnswers,
  buildRequirementsText,
  diffSummaries,
  fieldLabel,
  formatSummaryValue,
} from '../utils/requirements'

/** 一份用于比对的基础摘要 */
const BASE: RequirementsSummary = {
  product_name: 'HarnessPRD',
  product_goal: '把模糊想法整理成可开工的 PRD',
  target_users: ['独立开发者'],
  platform: '网页应用',
  mvp_features: [{ name: '问卷填写', description: '收集产品信息' }],
  technical_constraints: { auth: '需要登录', storage: '轻量存储' },
}

describe('diffSummaries', () => {
  it('两份完全相同的摘要没有差异', () => {
    expect(diffSummaries(BASE, structuredClone(BASE))).toEqual([])
  })

  it('任一为空时直接返回空列表（避免把「还没填」报成整片删除）', () => {
    expect(diffSummaries(null, BASE)).toEqual([])
    expect(diffSummaries(BASE, null)).toEqual([])
    expect(diffSummaries(undefined, undefined)).toEqual([])
  })

  it('标量字段被修改时报 modified，并带上中文路径', () => {
    const after: RequirementsSummary = { ...BASE, platform: '网页应用 + 微信小程序' }
    const changes = diffSummaries(BASE, after)

    expect(changes).toHaveLength(1)
    const change = changes[0]
    expect(change?.changeType).toBe('modified')
    expect(change?.path).toBe('platform')
    expect(change?.label).toBe('运行平台')
    expect(change?.before).toBe('网页应用')
    expect(change?.after).toBe('网页应用 + 微信小程序')
  })

  it('原摘要没有的可选字段出现时报 added', () => {
    const after: RequirementsSummary = { ...BASE, v2_features: ['团队协作'] }
    const changes = diffSummaries(BASE, after)

    expect(changes).toHaveLength(1)
    expect(changes[0]?.changeType).toBe('added')
    expect(changes[0]?.path).toBe('v2_features')
    expect(changes[0]?.label).toBe('未来规划')
    expect(changes[0]?.before).toBeNull()
    expect(changes[0]?.after).toEqual(['团队协作'])
  })

  it('字段被清空时报 removed，before 保留原值', () => {
    const after: RequirementsSummary = { ...BASE, product_goal: '' }
    const changes = diffSummaries(BASE, after)

    expect(changes).toHaveLength(1)
    expect(changes[0]?.changeType).toBe('removed')
    expect(changes[0]?.path).toBe('product_goal')
    expect(changes[0]?.before).toBe('把模糊想法整理成可开工的 PRD')
    expect(changes[0]?.after).toBeNull()
  })

  it('下钻到嵌套对象的叶子字段，路径用点号连起来', () => {
    const after: RequirementsSummary = {
      ...BASE,
      technical_constraints: { auth: '需要登录（支持微信扫码）', storage: '轻量存储' },
    }
    const changes = diffSummaries(BASE, after)

    expect(changes).toHaveLength(1)
    expect(changes[0]?.path).toBe('technical_constraints.auth')
    expect(changes[0]?.label).toBe('技术约束 · 认证方式')
    expect(changes[0]?.changeType).toBe('modified')
  })

  it('字符串数组按下标逐个比对：新增元素报 added', () => {
    const after: RequirementsSummary = { ...BASE, target_users: ['独立开发者', '小团队产品经理'] }
    const changes = diffSummaries(BASE, after)

    expect(changes).toHaveLength(1)
    expect(changes[0]?.path).toBe('target_users[1]')
    expect(changes[0]?.label).toBe('目标用户 · 第 2 项')
    expect(changes[0]?.changeType).toBe('added')
    expect(changes[0]?.after).toBe('小团队产品经理')
  })

  it('数组元素减少时报 removed', () => {
    const before: RequirementsSummary = { ...BASE, target_users: ['独立开发者', '小团队产品经理'] }
    const changes = diffSummaries(before, BASE)

    expect(changes).toHaveLength(1)
    expect(changes[0]?.path).toBe('target_users[1]')
    expect(changes[0]?.changeType).toBe('removed')
    expect(changes[0]?.before).toBe('小团队产品经理')
  })

  it('对象数组能定位到具体一项的具体子字段（如 mvp 的价值）', () => {
    const after: RequirementsSummary = {
      ...BASE,
      mvp_features: [{ name: '问卷填写', description: '收集产品信息', user_value: '省时间' }],
    }
    const changes = diffSummaries(BASE, after)

    expect(changes).toHaveLength(1)
    expect(changes[0]?.path).toBe('mvp_features[0].user_value')
    expect(changes[0]?.label).toBe('MVP 功能 · 第 1 项 · 对用户的价值')
    expect(changes[0]?.changeType).toBe('added')
    expect(changes[0]?.after).toBe('省时间')
  })

  it('整项新增的功能只报一行（把该项当成一个整体，而不是拆成 name/description 两行）', () => {
    const after: RequirementsSummary = {
      ...BASE,
      mvp_features: [
        { name: '问卷填写', description: '收集产品信息' },
        { name: '一键导出', description: '导出成 Markdown' },
      ],
    }
    const changes = diffSummaries(BASE, after)

    expect(changes).toHaveLength(1)
    expect(changes[0]?.path).toBe('mvp_features[1]')
    expect(changes[0]?.label).toBe('MVP 功能 · 第 2 项')
    expect(changes[0]?.changeType).toBe('added')
    expect(changes[0]?.after).toEqual({ name: '一键导出', description: '导出成 Markdown' })
  })

  it('内容相同但引用不同的对象不应被误报', () => {
    const after = structuredClone(BASE)
    after.mvp_features = [{ name: '问卷填写', description: '收集产品信息' }]
    expect(diffSummaries(BASE, after)).toEqual([])
  })
})

describe('fieldLabel / formatSummaryValue', () => {
  it('未登记的字段名原样返回', () => {
    expect(fieldLabel('product_name')).toBe('产品名称')
    expect(fieldLabel('unknown_key')).toBe('unknown_key')
  })

  it('把各种取值渲染成一行可读文本', () => {
    expect(formatSummaryValue('')).toBe('（空）')
    expect(formatSummaryValue(null)).toBe('（空）')
    expect(formatSummaryValue([])).toBe('（空）')
    expect(formatSummaryValue(['独立开发者', '产品经理'])).toBe('独立开发者、产品经理')
    expect(formatSummaryValue({ auth: '需要登录', storage: '' })).toBe('认证方式：需要登录')
    expect(formatSummaryValue(3)).toBe('3')
  })
})

describe('buildAnswers / buildRequirementsText（既有契约回归）', () => {
  it('结构化摘要映射到后端已有题目的字段名', () => {
    const answers = buildAnswers({
      ...BASE,
      technical_constraints: { auth: '需要登录', storage: '轻量存储', frontend: 'Vue 3' },
      scope: { in_scope: ['表单'], out_of_scope: ['登录'] },
    })

    expect(answers.product_name).toBe('HarnessPRD')
    expect(answers.problem_to_solve).toBe('把模糊想法整理成可开工的 PRD')
    expect(answers.target_users).toEqual(['独立开发者'])
    expect(answers.core_features).toEqual(['问卷填写 —— 收集产品信息'])
    expect(answers.platform_type).toBe('网页应用')
    expect(answers.auth_required).toBe('需要登录')
    expect(answers.data_storage).toBe('轻量存储')
    expect(String(answers.constraints_or_extra)).toContain('技术选型约束：前端 Vue 3')
    expect(String(answers.constraints_or_extra)).toContain('不在范围内：登录')
  })

  it('PRD 需求文本是带围栏的结构化 JSON', () => {
    const text = buildRequirementsText(BASE)
    expect(text).toContain('```json')
    expect(text).toContain('"product_name": "HarnessPRD"')
  })
})
