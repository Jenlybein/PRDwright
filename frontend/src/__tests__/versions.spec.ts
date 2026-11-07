import { describe, expect, it } from 'vitest'

import { versionSourceLabel } from '../utils/versions'

/**
 * 版本来源的展示映射。
 *
 * 守的是**后端写得出的每一个来源取值，抽屉里都得说人话**：漏一个，用户在版本历史里
 * 看到的就是裸标识符（`review_fix` / `auto_clarify`），而那几个正是最近才加的动作。
 * 认不出的取值仍原样返回——那是「后端新增了来源」时的降级，不是把标签猜一个近似的。
 */
describe('versionSourceLabel', () => {
  it('三份产物与已知动作都有中文说法', () => {
    expect(versionSourceLabel('prd')).toBe('生成 PRD')
    expect(versionSourceLabel('api_docs')).toBe('生成接口文档')
    expect(versionSourceLabel('prompts')).toBe('生成提示词')
    expect(versionSourceLabel('optimize')).toBe('AI 优化')
    expect(versionSourceLabel('clarify')).toBe('按澄清改写')
    expect(versionSourceLabel('manual')).toBe('手动编辑')
    expect(versionSourceLabel('checkpoint')).toBe('存为新版本')
    expect(versionSourceLabel('restore')).toBe('恢复历史版本')
  })

  it('审核相关的两个动作不显示裸标识符', () => {
    // 修复会追加版本（`commit_generated(source=runtime.kind)`），来源就是 `review_fix`
    expect(versionSourceLabel('review_fix')).toBe('修复')
    // AI 自行澄清同样是就地改写，来源是 `auto_clarify`
    expect(versionSourceLabel('auto_clarify')).toBe('AI 自行澄清')
  })

  it('认不出的来源原样返回（不吞信息、也不猜）', () => {
    expect(versionSourceLabel('some_future_source')).toBe('some_future_source')
  })
})
