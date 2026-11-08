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
      {
        severity: 'high',
        category: 'fabrication',
        detail: '出现了输入中没有的性能指标',
        sectionTitle: '产品概述',
      },
      { severity: 'medium', category: 'structure', detail: '缺少异常流程' },
    ],
    rewritten: false,
    failedSections: [],
    reviewedAt: '2026-10-13T00:00:00',
    ...overrides,
  }
}

/**
 * 旧口径的结论（ADR 0003 之前落库的那些）：条目只有 `where` / `severity` / `detail`，
 * **没有类别**。面板归「未分类」，不推断、不重分类。
 */
function legacyReview(overrides: Partial<ReviewConclusion> = {}): ReviewConclusion {
  return review({
    issues: [
      { severity: 'high', detail: '出现了输入中没有的性能指标', sectionTitle: '产品概述' },
      { severity: 'medium', detail: '缺少异常流程' },
    ],
    ...overrides,
  })
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

/**
 * 纯渲染的嗅觉级用例。
 *
 * 只回答「给这份面板一组 props，它画出来对不对」：徽标、问题条数、折叠、追溯信息。
 * **状态判定与按钮可用性不在这里**——那是行为，断言在应用外壳（`AppV2.spec.ts`：
 * 真 router + 点按钮 + 屏幕结果）。组件直挂 props 的用例看不出「点下去到底发生了什么」，
 * 也测不到「状态从哪来」。
 */
describe('ReviewConclusionPanel', () => {
  it('默认收起：结论可见，明细不占地方', () => {
    const wrapper = mount(ReviewConclusionPanel, { props: { review: review() } })

    // 标题与结论徽标常驻：收起不等于「把问题藏了」
    expect(wrapper.text()).toContain('文档缺陷')
    expect(wrapper.text()).toContain('有问题')
    // 明细压根没渲染（`v-if`）：既不占视觉空间，也不会被 `text()` 读到
    expect(isCollapsed(wrapper)).toBe(true)
    expect(wrapper.text()).not.toContain('出现了输入中没有的性能指标')
  })

  it('标题是「文档缺陷」：这块只报缺陷，不报未决', () => {
    // 「缺什么、该怎么定」归待确认清单；标题照实说，用户才不会把它读成第二份待确认清单
    const wrapper = mount(ReviewConclusionPanel, { props: { review: review() } })

    expect(wrapper.text()).toContain('文档缺陷')
    expect(wrapper.text()).not.toContain('审核结论')
  })

  it('收起时给出分类计数：用户知道「有几条、哪一类等着处理」', () => {
    const wrapper = mount(ReviewConclusionPanel, { props: { review: review() } })

    expect(wrapper.text()).toContain('编造 1 · 结构不符 1')
  })

  it('展开按类分组、类内 high 在前；没有类别的归「未分类」并排在最后', async () => {
    const wrapper = mount(ReviewConclusionPanel, {
      props: {
        review: review({
          issues: [
            { severity: 'medium', category: 'structure', detail: '条目编号与模板不一致' },
            { severity: 'medium', category: 'fabrication', detail: '编造了并发量' },
            { severity: 'high', category: 'fabrication', detail: '编造了鉴权方式' },
            { severity: 'high', detail: '旧口径条目：性能指标' },
          ],
        }),
      },
    })
    expect(wrapper.text()).toContain('编造 2 · 结构不符 1 · 未分类 1')

    await wrapper.find('button').trigger('click')
    const text = body(wrapper).text()
    // 分组顺序固定（编造 → 结构 → 未分类）：未分类是历史，不该挡在新口径的缺陷前面
    expect(text.indexOf('编造')).toBeLessThan(text.indexOf('结构'))
    expect(text.indexOf('结构')).toBeLessThan(text.indexOf('未分类'))
    // 类内 high 在前：先修编造，再修同类里的 medium
    expect(text.indexOf('编造了鉴权方式')).toBeLessThan(text.indexOf('编造了并发量'))
  })

  it('旧结论没有类别：归「未分类」，不推断它属于哪一类', async () => {
    // 不追溯是 ADR 0003 决策 7：照旧标签等于假装历史是按新口径判的
    const wrapper = mount(ReviewConclusionPanel, { props: { review: legacyReview() } })

    expect(wrapper.text()).toContain('未分类 2')
    await wrapper.find('button').trigger('click')
    const text = body(wrapper).text()
    expect(text).toContain('未分类')
    // detail 读起来像「编造」，但旧结论没有类别——不替它猜
    expect(text).not.toContain('编造')
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
    expect(wrapper.text()).not.toContain('未分类')
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

  it('审核失败时不渲染上一次结论的内容：失败下面是「这次没有结论」', async () => {
    // 失败不写结论（C-R1），所以面板上留着的是**上一次**那条。把它逐条列在
    // 「审核失败」下面，读起来就是「本次审出来的结果」——最误导人的一种排法。
    const wrapper = mount(ReviewConclusionPanel, {
      props: {
        review: review(),
        reviewFailedAt: new Date(Date.now() - 3 * 60_000).toISOString(),
      },
    })

    expect(wrapper.text()).toContain('审核失败')
    // 失败时间来自落库的 `reviewFailedAt`（US3）：刷新后仍然有
    expect(wrapper.text()).toContain('失败于')
    // 收起态：连分类计数都不该来自上一次那条结论
    expect(wrapper.text()).not.toContain('编造 1')

    await wrapper.find('button').trigger('click')
    expect(wrapper.text()).not.toContain('出现了输入中没有的性能指标')
    expect(wrapper.text()).not.toContain('缺少异常流程')
    expect(wrapper.text()).not.toContain('2 章完成，2 个待处理问题')
    // 该说的是「这次没跑成」，而且不提旧结论
    expect(body(wrapper).text()).toContain('这次审核没有跑成')
  })

  it('显示「第几版、什么时候审的」：结论能追溯到被审的那一版', () => {
    const reviewedAt = new Date(Date.now() - 5 * 60_000).toISOString()
    const wrapper = mount(ReviewConclusionPanel, {
      props: { review: review({ reviewedAt }), versionNo: 3 },
    })

    expect(wrapper.text()).toContain('第 3 版')
    expect(wrapper.text()).toContain('5 分钟前')
  })

  it('没有审核时间就不编一个：未审核 / 审核失败时不显示「审于」', async () => {
    const wrapper = mount(ReviewConclusionPanel, {
      props: { review: null, reviewFailedAt: '2026-10-13T00:00:00', versionNo: 2 },
    })

    expect(wrapper.text()).toContain('第 2 版')
    expect(wrapper.text()).not.toContain('审于')
  })
})
