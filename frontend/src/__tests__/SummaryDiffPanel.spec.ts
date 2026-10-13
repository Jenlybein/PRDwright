import { mount, type VueWrapper } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import SummaryDiffPanel from '../components/SummaryDiffPanel.vue'
import type { SummaryChange } from '../types'

/** 找包含指定文字的按钮 */
function findButton(wrapper: VueWrapper, text: string) {
  const button = wrapper.findAll('button').find((item) => item.text().includes(text))
  if (!button) throw new Error(`未找到按钮：${text}`)
  return button
}

const CHANGES: SummaryChange[] = [
  {
    path: 'target_users[1]',
    label: '目标用户 · 第 2 项',
    changeType: 'added',
    before: null,
    after: '小团队产品经理',
  },
  {
    path: 'technical_constraints.auth',
    label: '技术约束 · 认证方式',
    changeType: 'modified',
    before: '需要登录',
    after: '需要登录（支持微信扫码）',
  },
  {
    path: 'scope.out_of_scope[0]',
    label: '项目范围 · 不在范围内 · 第 1 项',
    changeType: 'removed',
    before: '登录',
    after: null,
  },
]

describe('SummaryDiffPanel', () => {
  it('逐字段展示变更：类型徽章、路径、变更前与变更后', () => {
    const wrapper = mount(SummaryDiffPanel, { props: { changes: CHANGES } })
    const text = wrapper.text()

    // 三种变更类型都有对应的中文徽章
    expect(text).toContain('新增')
    expect(text).toContain('修改')
    expect(text).toContain('删除')

    // 字段路径同时给出中文面包屑与机器可读路径
    expect(text).toContain('目标用户 · 第 2 项')
    expect(text).toContain('target_users[1]')
    expect(text).toContain('技术约束 · 认证方式')
    expect(text).toContain('technical_constraints.auth')

    // 变更前 / 变更后的值
    expect(text).toContain('需要登录')
    expect(text).toContain('需要登录（支持微信扫码）')
    expect(text).toContain('小团队产品经理')
    expect(text).toContain('共 3 处变更')
    expect(text).toContain('新增 1')
    expect(text).toContain('修改 1')
    expect(text).toContain('删除 1')
  })

  it('新增的行不展示「变更前」，删除的行不展示「变更后」', () => {
    const wrapper = mount(SummaryDiffPanel, { props: { changes: [CHANGES[0] as SummaryChange] } })
    const addedRow = wrapper.findAll('li')[0]
    expect(addedRow?.text()).not.toContain('变更前')
    expect(addedRow?.text()).toContain('变更后')

    const removedWrapper = mount(SummaryDiffPanel, {
      props: { changes: [CHANGES[2] as SummaryChange] },
    })
    const removedRow = removedWrapper.findAll('li')[0]
    expect(removedRow?.text()).toContain('变更前')
    expect(removedRow?.text()).not.toContain('变更后')
  })

  it('三种变更类型用不同颜色标识（新增绿 / 修改蓝 / 删除红）', () => {
    const wrapper = mount(SummaryDiffPanel, { props: { changes: CHANGES } })
    const rows = wrapper.findAll('li')

    expect(rows[0]?.html()).toContain('emerald')
    expect(rows[1]?.html()).toContain('primary')
    expect(rows[2]?.html()).toContain('rose')
  })

  it('回填中：展示进度提示并禁用两个按钮', () => {
    const wrapper = mount(SummaryDiffPanel, { props: { loading: true } })

    expect(wrapper.text()).toContain('正在从对话中整理需求摘要')
    const buttons = wrapper.findAll('button')
    expect(buttons.length).toBe(2)
    expect(buttons.every((button) => button.attributes('disabled') !== undefined)).toBe(true)
  })

  it('回填失败：展示原因，主按钮降级为「跳过更新，直接生成 PRD」', () => {
    const wrapper = mount(SummaryDiffPanel, { props: { error: '后端不可用' } })

    expect(wrapper.text()).toContain('后端不可用')
    expect(wrapper.text()).toContain('跳过更新，直接生成 PRD')
    // 出错时仍可拒绝
    expect(findButton(wrapper, '拒绝更改').exists()).toBe(true)
  })

  it('没有变更：提示摘要保持不变，主按钮为「直接生成 PRD」', () => {
    const wrapper = mount(SummaryDiffPanel, { props: { changes: [] } })

    expect(wrapper.text()).toContain('没有检测到需要回填的补充信息')
    expect(wrapper.text()).toContain('直接生成 PRD')
    expect(wrapper.findAll('li')).toHaveLength(0)
  })

  it('点击确认更新 / 拒绝更改时抛出对应事件', async () => {
    const wrapper = mount(SummaryDiffPanel, { props: { changes: CHANGES } })

    await findButton(wrapper, '确认更新').trigger('click')
    expect(wrapper.emitted('confirm')).toHaveLength(1)
    expect(wrapper.emitted('reject')).toBeUndefined()

    await findButton(wrapper, '拒绝更改').trigger('click')
    expect(wrapper.emitted('reject')).toHaveLength(1)
  })
})
