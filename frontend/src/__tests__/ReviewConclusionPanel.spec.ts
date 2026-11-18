import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ReviewConclusionPanel from '../components/ui/ReviewConclusionPanel.vue'
import type { ReviewConclusion } from '../types'

/** 一份「审出问题」的结论：最常用的夹具（收起时也要能看出「有问题」） */
function review(overrides: Partial<ReviewConclusion> = {}): ReviewConclusion {
  return {
    kind: 'prd',
    verdict: 'revise',
    summary: '整篇审出 2 个待处理问题',
    issues: [
      {
        severity: 'high',
        category: 'fabrication',
        detail: '出现了输入中没有的性能指标',
        where: '产品概述',
      },
      { severity: 'medium', category: 'structure', detail: '缺少异常流程' },
    ],
    rewritten: false,
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
      { severity: 'high', detail: '出现了输入中没有的性能指标', where: '产品概述' },
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

  it('严重程度高/中/低显式可读：收起态给统计，展开后每条带徽标', async () => {
    const wrapper = mount(ReviewConclusionPanel, {
      props: {
        review: review({
          issues: [
            { severity: 'high', category: 'fabrication', detail: '编造了鉴权方式' },
            { severity: 'medium', category: 'structure', detail: '缺少异常流程' },
            { severity: 'low', category: 'vague', detail: '描述稍显空泛' },
          ],
        }),
      },
    })

    // 收起态：按严重程度统计，三档都显示（文字 + 颜色双通道）
    const collapsed = wrapper.text()
    expect(collapsed).toContain('高')
    expect(collapsed).toContain('中')
    expect(collapsed).toContain('低')

    await wrapper.find('button').trigger('click')
    const expanded = body(wrapper).text()
    // 展开后每条都带「严重度 X」文字标签
    expect(expanded).toContain('严重度高')
    expect(expanded).toContain('严重度中')
    expect(expanded).toContain('严重度低')
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
    expect(text).toContain('整篇审出 2 个待处理问题')
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

  it('改写过的会在标题下方元信息行标注「已改写一次」', () => {
    const wrapper = mount(
      ReviewConclusionPanel,
      { props: { review: review({ rewritten: true }) } },
    )

    expect(wrapper.text()).toContain('已改写一次')
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

  /* ---------------------------------------------------------------------------
   * 排版重做后的新增口径（用户反馈「信息杂乱、Markdown 标记露出来」）。
   * 这些用例守的是**层次**：总体结论 → 四类各几条 → 每条在哪、是什么。
   * ------------------------------------------------------------------------- */

  it('展开后每组一个标题行：图标 + 类别名 + 条数，顺序按严重度', async () => {
    const wrapper = mount(ReviewConclusionPanel, {
      props: {
        review: review({
          issues: [
            { severity: 'medium', category: 'vague', detail: '只给了名词', where: '功能范围' },
            { severity: 'medium', category: 'structure', detail: '编号与模板不一致', where: '接口列表' },
            { severity: 'high', category: 'contradiction', detail: '与上游口径冲突', where: '产品概述' },
            { severity: 'high', category: 'fabrication', detail: '编造了鉴权方式', where: '产品概述' },
          ],
        }),
      },
    })
    await wrapper.find('button').trigger('click')

    const headings = wrapper.findAll('h4')
    expect(headings).toHaveLength(4)
    // 顺序照后端的严重度：编造 / 矛盾（high）在前，结构不符 / 空泛（medium）在后
    expect(headings[0]!.text()).toContain('编造')
    expect(headings[1]!.text()).toContain('矛盾')
    expect(headings[2]!.text()).toContain('结构不符')
    expect(headings[3]!.text()).toContain('空泛')
    // 每组标题都带条数，且条数紧跟在类别名后面（组内 1 条）
    for (const heading of headings) {
      expect(heading.text()).toContain('1 条')
      // 图标是标题行的第一个子元素（装饰性，读屏靠 aria-hidden 跳过）
      expect(heading.find('svg').exists()).toBe(true)
    }
    // 图标表达严重度：high 组用 danger 色，medium 组用弱化色（不再挂单字徽标）
    expect(headings[0]!.find('svg').classes()).toContain('text-danger-fg')
    expect(headings[2]!.find('svg').classes()).toContain('text-subtle')
  })

  it('没有内容的类别不渲染：0 条不出标题、也不进计数', async () => {
    const wrapper = mount(ReviewConclusionPanel, {
      props: {
        review: review({
          issues: [
            { severity: 'high', category: 'fabrication', detail: '编造了鉴权方式', where: '产品概述' },
          ],
        }),
      },
    })
    await wrapper.find('button').trigger('click')

    expect(wrapper.findAll('h4')).toHaveLength(1)
    // 计数行与展开体都只说实际存在的这一类
    expect(wrapper.text()).toContain('编造 1')
    for (const absent of ['矛盾', '结构不符', '空泛', '推断未标注', '清单漏登记', '未分类']) {
      expect(wrapper.text()).not.toContain(absent)
    }
  })

  it('单条缺陷：定位行带严重度徽标，描述是独立主体行', async () => {
    const wrapper = mount(ReviewConclusionPanel, { props: { review: review() } })
    await wrapper.find('button').trigger('click')

    const items = body(wrapper).findAll('li')
    expect(items).toHaveLength(2)

    // 第一类（编造，high）：第一段是「严重度徽标 + 定位」，第二段是描述
    const located = items[0]!.findAll('p')
    expect(located).toHaveLength(2)
    expect(located[0]!.text()).toContain('产品概述')
    expect(located[0]!.text()).toContain('严重度高')
    expect(located[0]!.find('.font-mono').exists()).toBe(true)
    expect(located[1]!.text()).toBe('出现了输入中没有的性能指标')
    expect(located[1]!.classes()).toContain('text-sm')

    // 没有定位的条目：第一段只有严重度徽标，描述仍在独立行
    const unlocated = items[1]!.findAll('p')
    expect(unlocated).toHaveLength(2)
    expect(unlocated[0]!.text()).toContain('严重度中')
    expect(unlocated[1]!.text()).toBe('缺少异常流程')
  })

  it('后端已抹平行内 Markdown：面板里不再出现 `##` / `###` 标记', async () => {
    // 归一化在 `review_defects.normalize_issue` 里做（`where` / `detail` 抹成纯文本）。
    // 前端**不**再拿正则洗一遍（否则就是第二个真源），这里守的是「面板照纯文本渲染」。
    const wrapper = mount(ReviewConclusionPanel, {
      props: {
        review: review({
          issues: [
            { severity: 'high', category: 'fabrication', detail: '性能指标没有出处', where: '产品概述' },
            { severity: 'medium', category: 'structure', detail: '标题层级与模板不一致', where: '接口列表' },
          ],
        }),
      },
    })
    await wrapper.find('button').trigger('click')

    expect(wrapper.text()).not.toContain('##')
    expect(wrapper.text()).not.toContain('###')
    // 章节标题只作为定位文本出现，没有被当成 Markdown 标题渲染成新的大纲节点
    expect(body(wrapper).find('h1').exists()).toBe(false)
    expect(body(wrapper).find('h2').exists()).toBe(false)
    expect(body(wrapper).text()).toContain('产品概述')
  })

  it('元信息收成标题下方一行小字：第几版 / 何时审 / 已改写，且不与类别计数同排', () => {
    const reviewedAt = new Date(Date.now() - 5 * 60_000).toISOString()
    const wrapper = mount(ReviewConclusionPanel, {
      props: {
        review: review({ reviewedAt, rewritten: true }),
        versionNo: 2,
      },
    })

    const spans = wrapper.findAll('span')
    const meta = spans.find((span) => span.text().startsWith('第 2 版'))
    expect(meta).toBeDefined()
    expect(meta?.text()).toContain('第 2 版')
    expect(meta?.text()).toContain('审于 5 分钟前')
    expect(meta?.text()).toContain('已改写一次')
    expect(meta?.classes()).toContain('text-subtle')

    // 计数行是另一行：它只讲条数、不带版本/时间/改写标记
    const counts = spans.find((span) => span.text().startsWith('共 '))
    expect(counts).toBeDefined()
    expect(counts?.text()).toContain('编造 1')
    expect(counts?.text()).not.toContain('第 2 版')
    expect(counts?.text()).not.toContain('审于')
  })
})
