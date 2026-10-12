import { mount, type VueWrapper } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import RagResultPanel from '../components/RagResultPanel.vue'
import type { RagHit } from '../types'

/** 找包含指定文字的按钮 */
function findButton(wrapper: VueWrapper, text: string) {
  const button = wrapper.findAll('button').find((item) => item.text().includes(text))
  if (!button) throw new Error(`未找到按钮：${text}`)
  return button
}

/** 造一条检索命中 */
function makeHit(overrides: Partial<RagHit> & { title: string }): RagHit {
  return {
    id: overrides.title,
    source: '团队接口规范',
    content: `${overrides.title} 的正文内容`,
    tags: [],
    score: 0.3,
    matched_terms: [],
    ...overrides,
  }
}

const HITS: RagHit[] = [
  makeHit({ title: '统一响应结构与字段命名', matched_terms: ['code', '响应'] }),
  makeHit({ title: '列表与分页规范', matched_terms: ['分页', 'page'] }),
  makeHit({ title: '用户登录与令牌刷新', source: '历史接口示例', matched_terms: ['登录'] }),
]

describe('RagResultPanel', () => {
  it('按来源分组展示命中的规范与历史示例，并给出标题、正文与相关度', () => {
    const wrapper = mount(RagResultPanel, { props: { hits: HITS } })
    const text = wrapper.text()

    expect(text).toContain('接口规范检索结果')
    expect(text).toContain('团队接口规范')
    expect(text).toContain('2 条')
    expect(text).toContain('历史接口示例')
    expect(text).toContain('统一响应结构与字段命名')
    expect(text).toContain('统一响应结构与字段命名 的正文内容')
    // 命中关键词用于解释「为什么检索到这条」
    expect(text).toContain('分页')
    expect(text).toContain('相关度 0.30')
  })

  it('默认全选，确认时抛出全部命中', async () => {
    const wrapper = mount(RagResultPanel, { props: { hits: HITS } })

    const checkboxes = wrapper.findAll('input[type="checkbox"]')
    expect(checkboxes).toHaveLength(3)
    expect(checkboxes.every((box) => (box.element as HTMLInputElement).checked)).toBe(true)

    await findButton(wrapper, '确认并生成接口文档').trigger('click')
    const emitted = wrapper.emitted('confirm')
    expect(emitted).toHaveLength(1)
    expect(emitted?.[0]?.[0]).toHaveLength(3)
  })

  it('取消勾选后确认，只抛出勾选的条目', async () => {
    const wrapper = mount(RagResultPanel, { props: { hits: HITS } })

    await wrapper.findAll('input[type="checkbox"]')[1]?.setValue(false)
    expect(wrapper.text()).toContain('已选 2 条')

    await findButton(wrapper, '确认并生成接口文档').trigger('click')
    const payload = wrapper.emitted('confirm')?.[0]?.[0] as RagHit[] | undefined
    expect(payload?.map((hit) => hit.title)).toEqual([
      '统一响应结构与字段命名',
      '用户登录与令牌刷新',
    ])
  })

  it('「全不选」后确认，抛出空数组（相当于不带参考规范生成）', async () => {
    const wrapper = mount(RagResultPanel, { props: { hits: HITS } })

    await findButton(wrapper, '全不选').trigger('click')
    expect(wrapper.text()).toContain('已选 0 条')

    await findButton(wrapper, '确认并生成接口文档').trigger('click')
    expect(wrapper.emitted('confirm')?.[0]?.[0]).toEqual([])

    // 再切回全选
    await findButton(wrapper, '全选').trigger('click')
    expect(wrapper.text()).toContain('已选 3 条')
  })

  it('点击取消时抛出 cancel，且不抛出 confirm', async () => {
    const wrapper = mount(RagResultPanel, { props: { hits: HITS } })

    await findButton(wrapper, '取消').trigger('click')
    expect(wrapper.emitted('cancel')).toHaveLength(1)
    expect(wrapper.emitted('confirm')).toBeUndefined()
  })

  it('检索中：展示进度提示，按钮禁用', () => {
    const wrapper = mount(RagResultPanel, { props: { loading: true } })

    expect(wrapper.text()).toContain('正在检索团队接口规范')
    const buttons = wrapper.findAll('button')
    expect(buttons.every((button) => button.attributes('disabled') !== undefined)).toBe(true)
  })

  it('检索失败：展示原因并提示可直接生成', () => {
    const wrapper = mount(RagResultPanel, { props: { error: '接口规范检索失败：文件缺失' } })

    expect(wrapper.text()).toContain('接口规范检索失败：文件缺失')
    expect(wrapper.text()).toContain('检索失败不影响生成')
    expect(findButton(wrapper, '直接生成接口文档').exists()).toBe(true)
  })

  it('没有命中：提示可直接生成，主按钮不叫「确认」', () => {
    const wrapper = mount(RagResultPanel, { props: { hits: [] } })

    expect(wrapper.text()).toContain('没有检索到相关规范')
    expect(findButton(wrapper, '直接生成接口文档').exists()).toBe(true)
    expect(wrapper.findAll('input[type="checkbox"]')).toHaveLength(0)
  })
})
