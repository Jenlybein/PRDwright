import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ReviewConclusionPanel from '../components/ui/ReviewConclusionPanel.vue'
import type { ReviewConclusion } from '../types'

/** 一份「审出问题」的结论：最常用的夹具（收起时也要能看出「有问题」） */
function review(overrides: Partial<ReviewConclusion> = {}): ReviewConclusion {
  return {
    kind: 'prd',
    verdict: 'revise',
    summary: '2 章完成，2 个待处理问题',
    issues: [
      { severity: 'high', detail: '出现了输入中没有的性能指标', sectionTitle: '产品概述' },
      { severity: 'medium', detail: '缺少异常流程' },
    ],
    rewritten: false,
    failedSections: [],
    ...overrides,
  }
}

/** 折叠体：`aria-controls` 指向的那个元素（收起时**不存在**，用 `v-if`） */
function body(wrapper: ReturnType<typeof mount>) {
  const id = wrapper.find('button').attributes('aria-controls')
  return wrapper.find(`#${id}`)
}

/** 收起态：折叠体压根没渲染 */
function isCollapsed(wrapper: ReturnType<typeof mount>): boolean {
  return !body(wrapper).exists()
}

describe('ReviewConclusionPanel', () => {
  it('默认收起：结论可见，明细不占地方', () => {
    const wrapper = mount(ReviewConclusionPanel, { props: { review: review() } })

    // 标题与结论徽标常驻：收起不等于「把问题藏了」
    expect(wrapper.text()).toContain('审核结论')
    expect(wrapper.text()).toContain('有问题')
    // 明细压根没渲染（`v-if`）：既不占视觉空间，也不会被 `text()` 读到
    expect(isCollapsed(wrapper)).toBe(true)
    expect(wrapper.text()).not.toContain('出现了输入中没有的性能指标')
  })

  it('收起时给出问题条数：用户知道「有几条等着处理」', () => {
    const wrapper = mount(ReviewConclusionPanel, { props: { review: review() } })

    expect(wrapper.text()).toContain('2 个待处理问题')
  })

  it('点一下展开，问题逐条可见', async () => {
    const wrapper = mount(ReviewConclusionPanel, { props: { review: review() } })

    await wrapper.find('button').trigger('click')

    expect(isCollapsed(wrapper)).toBe(false)
    const text = body(wrapper).text()
    expect(text).toContain('出现了输入中没有的性能指标')
    expect(text).toContain('产品概述')
    expect(text).toContain('缺少异常流程')
    // 摘要也在折叠体里（它比一行标题长，属于「想看细节才展开」的部分）
    expect(text).toContain('2 章完成，2 个待处理问题')
  })

  it('再点一下收起（同一个按钮切两态）', async () => {
    const wrapper = mount(ReviewConclusionPanel, { props: { review: review() } })

    // 每次都重新取按钮：展开会触发重渲染，旧句柄可能指向已被替换的节点
    await wrapper.find('button').trigger('click')
    expect(isCollapsed(wrapper)).toBe(false)

    await wrapper.find('button').trigger('click')
    expect(isCollapsed(wrapper)).toBe(true)
  })

  it('aria-expanded 跟着状态走，且指向自己的折叠体', async () => {
    const wrapper = mount(ReviewConclusionPanel, { props: { review: review() } })
    const toggle = wrapper.find('button')

    // 无障碍：读屏软件靠这两个属性知道「这是个可展开的区域，现在是收起还是展开」
    expect(toggle.attributes('aria-expanded')).toBe('false')
    // 收起时折叠体不渲染，`aria-controls` 指向的 id 只在展开后存在——
    // 两个属性本身仍要在（读屏靠它判断「这里能展开」）
    expect(toggle.attributes('aria-controls')).toBeTruthy()

    await toggle.trigger('click')
    expect(toggle.attributes('aria-expanded')).toBe('true')
  })

  it('审核通过时徽标是「通过」，且没有问题计数', () => {
    const wrapper = mount(
      ReviewConclusionPanel,
      { props: { review: review({ verdict: 'pass', issues: [], summary: '6 章完成' }) } },
    )

    expect(wrapper.text()).toContain('通过')
    expect(wrapper.text()).not.toContain('待处理问题')
    // 没问题时展开只有一个摘要——不该出现空的列表容器
    expect(wrapper.find('ul').exists()).toBe(false)
  })

  it('改写过的会在标题行标注「已改写一次」', () => {
    const wrapper = mount(
      ReviewConclusionPanel,
      { props: { review: review({ rewritten: true }) } },
    )

    expect(wrapper.text()).toContain('已改写一次')
  })

  it('有章节失败时，提示只在展开后出现', async () => {
    const wrapper = mount(ReviewConclusionPanel, {
      props: {
        review: review({
          failedSections: [
            { sectionId: 'pages', title: '页面设计', reason: '正文为空' },
          ],
        }),
      },
    })

    // 收起时连失败章节的名字都不出现：那一行摘要已经用「有问题」讲了结论
    expect(wrapper.text()).not.toContain('页面设计')

    await wrapper.find('button').trigger('click')
    expect(body(wrapper).text()).toContain('本次有 1 章生成失败')
    expect(body(wrapper).text()).toContain('页面设计')
  })
})
