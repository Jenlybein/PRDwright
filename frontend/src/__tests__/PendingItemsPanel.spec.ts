import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import PendingItemsPanel from '../components/ui/PendingItemsPanel.vue'
import type { PendingImportance, PendingItem } from '../types'

/**
 * 待确认清单是后端在剥离正文标记块时算出的产物，前端**只读**。
 * 这里守的是展示口径：问题逐条列出、序号按位置、空字段不留空标签。
 */
const FULL: PendingItem = {
  id: 'p-1',
  question: '分页上限取多少？',
  sections: ['2.1', '6.1'],
  decider: '产品负责人',
  importance: 'P1',
}
const NO_SECTIONS: PendingItem = {
  id: 'overview-2',
  question: '指标口径谁拍板？',
  sections: [],
  decider: '数据团队',
  importance: 'P0',
}
const NO_DECIDER: PendingItem = {
  id: 'p-3',
  question: '这个功能要不要上？',
  sections: [],
  decider: '',
  importance: 'P2',
}

const ITEMS: PendingItem[] = [FULL, NO_SECTIONS, NO_DECIDER]

describe('PendingItemsPanel', () => {
  it('逐条展示问题，标题带上总条数', () => {
    const wrapper = mount(PendingItemsPanel, { props: { items: ITEMS } })
    const text = wrapper.text()

    expect(text).toContain('待确认问题（3）')
    expect(text).toContain('分页上限取多少？')
    expect(text).toContain('指标口径谁拍板？')
    expect(text).toContain('这个功能要不要上？')
  })

  it('章节与拍板人都给出来时一并展示', () => {
    const wrapper = mount(PendingItemsPanel, { props: { items: [FULL] } })
    const text = wrapper.text()

    expect(text).toContain('涉及章节：2.1、6.1')
    expect(text).toContain('拍板人：产品负责人')
  })

  it('章节为空时不渲染「涉及章节」标签（不留空标签）', () => {
    const wrapper = mount(PendingItemsPanel, { props: { items: [NO_SECTIONS] } })
    const text = wrapper.text()

    expect(text).toContain('拍板人：数据团队')
    expect(text).not.toContain('涉及章节')
  })

  it('拍板人为空时不渲染「拍板人」标签', () => {
    const wrapper = mount(PendingItemsPanel, { props: { items: [NO_DECIDER] } })
    const text = wrapper.text()

    expect(text).toContain('这个功能要不要上？')
    expect(text).not.toContain('拍板人')
  })

  it('序号用位置下标，不把后端 id 暴露给用户', () => {
    const wrapper = mount(PendingItemsPanel, { props: { items: ITEMS } })
    // 只取直接挂在 li 下的徽标：章节与拍板人之间那个「·」分隔符也有 aria-hidden，需排除
    const badges = wrapper.findAll('li > span[aria-hidden="true"]')

    // 每项的序号徽标按 1、2、3 递增
    expect(badges.map((badge) => badge.text())).toEqual(['1', '2', '3'])
    // 后端 id（p-1 / overview-2）只作渲染 key，不进可见文案
    expect(wrapper.text()).not.toContain('p-1')
    expect(wrapper.text()).not.toContain('overview-2')
  })

  it('按重要性从高到低展示：P0 在最前、P2 在最后', () => {
    const wrapper = mount(PendingItemsPanel, { props: { items: ITEMS } })
    const questions = wrapper.findAll('li').map((li) => li.find('p').text())

    // ITEMS 的声明顺序是 P1、P0、P2，重排后应当变成 P0、P1、P2
    expect(questions[0]).toBe('指标口径谁拍板？')
    expect(questions[1]).toBe('分页上限取多少？')
    expect(questions[2]).toBe('这个功能要不要上？')
  })

  it('每条都标出重要性等级徽标', () => {
    const wrapper = mount(PendingItemsPanel, { props: { items: ITEMS } })
    const text = wrapper.text()

    expect(text).toContain('P0')
    expect(text).toContain('P1')
    expect(text).toContain('P2')
  })
})

/**
 * 折叠口径。
 *
 * 清单可能长到十几条，而它就贴在正文之上——全铺开会把正文挤出屏幕。所以默认只铺开前 3 条，
 * 其余折起来由用户主动展开。折叠只影响「铺开多少」：总数、排序、等级一概不变。
 */
describe('PendingItemsPanel 的折叠', () => {
  /** 造 n 条问题，等级按 P0 / P1 / P2 轮转（用来验证展开取的仍是排序后的前缀） */
  function makeItems(n: number): PendingItem[] {
    const levels: PendingImportance[] = ['P0', 'P1', 'P2']
    return Array.from({ length: n }, (_unused, index) => ({
      id: `p-${index + 1}`,
      question: `第 ${index + 1} 个待确认问题`,
      sections: [],
      decider: '',
      importance: levels[index % levels.length]!,
    }))
  }

  it('超过 3 条时默认只铺开前 3 条，展开后全部铺开，可再收起', async () => {
    const wrapper = mount(PendingItemsPanel, { props: { items: makeItems(5) } })

    expect(wrapper.findAll('li')).toHaveLength(3)
    // 折叠不改总数：标题里始终是这份清单的全部条数
    expect(wrapper.text()).toContain('待确认问题（5）')
    expect(wrapper.text()).toContain('展开全部 5 条')

    await wrapper.find('button').trigger('click')
    expect(wrapper.findAll('li')).toHaveLength(5)
    expect(wrapper.text()).toContain('收起')

    await wrapper.find('button').trigger('click')
    expect(wrapper.findAll('li')).toHaveLength(3)
  })

  it('恰好 3 条时不出现展开控件（点了也不会有变化）', () => {
    const wrapper = mount(PendingItemsPanel, { props: { items: makeItems(3) } })

    expect(wrapper.findAll('li')).toHaveLength(3)
    expect(wrapper.find('button').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('展开全部')
  })

  it('展开后仍是「最要紧的在前」：折叠取的是排序后的前缀，不是原始顺序', async () => {
    const wrapper = mount(PendingItemsPanel, { props: { items: makeItems(5) } })
    await wrapper.find('button').trigger('click')

    // 等级 P0、P1、P2 轮转 + 同等级稳定排序 → P0 的第 1、4 条排在最前
    const questions = wrapper.findAll('li').map((li) => li.find('p').text())
    expect(questions[0]).toBe('第 1 个待确认问题')
    expect(questions[1]).toBe('第 4 个待确认问题')
    expect(questions[4]).toBe('第 3 个待确认问题')
  })
})
