import { mount, type VueWrapper } from '@vue/test-utils'
import { nextTick } from 'vue'
import { describe, expect, it } from 'vitest'

import DocumentReview from '../components/DocumentReview.vue'
import GeneratingStage from '../components/stages/GeneratingStage.vue'
import ReviewStage from '../components/stages/ReviewStage.vue'
import type { PendingItem, RunSummary } from '../types'

/**
 * 「预览 / 编辑」视图切换的落点与行为。
 *
 * 这两个按钮最初挂在文档卡片头部（卡片顶端），而 AI 优化 / 复制 / 保存挂在卡片底部的工具条，
 * 同一份文档的控件被整篇正文隔在上下两处。现已全部收进页面级底栏（`AppActionBar`）的内容行。
 * 本文件锁定「在底栏、不在卡片里」这一事实——否则后续重构很容易又把它挪回卡片头部。
 */

const CONTENT = '# 产品需求文档\n\n这是正文。'

function mountReview(): VueWrapper {
  return mount(ReviewStage, {
    props: {
      title: '产品需求文档',
      content: CONTENT,
      viewState: 'review-prd',
      canFinish: false,
      hasPrd: true,
      hasApiDocs: false,
      hasPrompts: false,
    },
  })
}

/** 按文案找按钮（模板里文案都是纯文字，精确匹配即可） */
function buttonByText(wrapper: VueWrapper, text: string) {
  const button = wrapper.findAll('button').find((item) => item.text() === text)
  if (!button) throw new Error(`未找到按钮：${text}`)
  return button
}

/** 造一份用量汇总（`run_summary` 事件的 data） */
function runSummary(overrides: Partial<RunSummary> = {}): RunSummary {
  return {
    kind: 'prd',
    status: 'completed',
    durationMs: 12_300,
    totalCalls: 8,
    failedCalls: 0,
    promptTokens: 900,
    completionTokens: 300,
    steps: [],
    // PRD 生成本就带技能包身份，放在默认里才贴近真实回执
    injectedSkills: [{ name: 'prd-generator', version: '0.1.0', fingerprint: 'e4f928df7be9' }],
    warnings: [],
    provider: 'ark',
    model: 'doubao-seed',
    requestId: 'req-abc123',
    ...overrides,
  }
}

describe('ReviewStage 的「预览 / 编辑」切换', () => {
  it('切换按钮渲染在页面级底栏里，且与「保存」同属内容行', () => {
    const wrapper = mountReview()

    // 底栏（sticky 态带 .shell-action-bar）里能找到两个切换按钮
    const bar = wrapper.find('.shell-action-bar')
    expect(bar.exists()).toBe(true)
    const barTexts = bar.findAll('button').map((item) => item.text())
    expect(barTexts).toContain('预览')
    expect(barTexts).toContain('编辑')

    // 与「保存」在同一个右端分组：切换按钮 → 切换容器 → 分组 === 保存的直接父节点
    const preview = buttonByText(wrapper, '预览')
    const save = buttonByText(wrapper, '保存')
    expect(preview.element.parentElement?.parentElement).toBe(save.element.parentElement)
  })

  it('文档卡片内部不再有任何按钮（头部只剩标题与徽章）', () => {
    const wrapper = mountReview()
    const card = wrapper.findComponent(DocumentReview)
    expect(card.findAll('button')).toHaveLength(0)
  })

  it('默认预览态渲染 Markdown 而非文本框', () => {
    const wrapper = mountReview()
    expect(wrapper.find('textarea').exists()).toBe(false)
    // 预览态把 Markdown 渲染成了 HTML 标题
    expect(wrapper.html()).toContain('<h1>产品需求文档</h1>')
  })

  it('切到「编辑」出现可编辑原文，再切回「预览」恢复渲染', async () => {
    const wrapper = mountReview()

    await buttonByText(wrapper, '编辑').trigger('click')
    const textarea = wrapper.find('textarea')
    expect(textarea.exists()).toBe(true)
    // 初值是文档正文（Markdown 源码）
    expect((textarea.element as HTMLTextAreaElement).value).toBe(CONTENT)

    await buttonByText(wrapper, '预览').trigger('click')
    expect(wrapper.find('textarea').exists()).toBe(false)
    expect(wrapper.html()).toContain('<h1>产品需求文档</h1>')
  })

  it('生成中不出现视图切换（该阶段没有可切换的最终正文）', () => {
    const wrapper = mount(GeneratingStage, {
      props: { title: '产品需求文档', streamingContent: '# 正在生成', generating: true },
    })
    const texts = wrapper.findAll('button').map((item) => item.text())
    expect(texts).not.toContain('预览')
    expect(texts).not.toContain('编辑')
  })
})

/**
 * 「保存」的可用性。
 *
 * 曾经按「原文被改过」（dirty）决定禁用态，导致**刚生成完、还没编辑的那一刻按钮是灰的**：
 * 用户刚把文档生成出来，却点不动保存，观感上像功能坏了。自动落库是后台行为，用户看不到，
 * 因此判据改为「有正文即可保存」——只要文档有内容就该能按下去。
 *
 * ⚠️ 断言禁用态前必须 `await nextTick()`：底栏读的是子组件句柄（模板 ref 在首次渲染**之后**
 * 才被赋值），挂载那一刻读到的是 `null`，要等重渲染落地才拿到真实状态。
 */
describe('ReviewStage 的「保存」可用性', () => {
  it('文档生成结束、尚未编辑时即可点（不是灰色假按钮）', async () => {
    const wrapper = mountReview()
    await nextTick()
    const save = buttonByText(wrapper, '保存')
    expect((save.element as HTMLButtonElement).disabled).toBe(false)
  })

  it('点「保存」把当前原文以 save 事件抛出', async () => {
    const wrapper = mountReview()
    await nextTick()
    await buttonByText(wrapper, '保存').trigger('click')
    expect(wrapper.emitted('save')).toEqual([[CONTENT]])
  })

  it('编辑后点「保存」抛的是编辑后的原文', async () => {
    const wrapper = mountReview()
    await nextTick()
    await buttonByText(wrapper, '编辑').trigger('click')
    await wrapper.find('textarea').setValue(`${CONTENT}\n\n新增一段`)
    await buttonByText(wrapper, '保存').trigger('click')
    expect(wrapper.emitted('save')).toEqual([[`${CONTENT}\n\n新增一段`]])
  })

  it('文档没有正文时「保存」禁用（没有东西可存）', async () => {
    const wrapper = mount(ReviewStage, {
      props: {
        title: '文档',
        content: '',
        viewState: 'review-prd',
        canFinish: false,
        hasPrd: false,
        hasApiDocs: false,
        hasPrompts: false,
      },
    })
    await nextTick()
    const save = buttonByText(wrapper, '保存')
    expect((save.element as HTMLButtonElement).disabled).toBe(true)
  })
})

/**
 * 生成用量回执（`runSummary`）。
 *
 * 这一行是「花了多久 / 用了多少 / 出问题报哪个号」的回执，属**只读展示**：
 * 必须留在文档区而不是底栏，也不得借机新增按钮——底栏是全站唯一操作落点，
 * 往那儿塞只读信息会把真正的操作挤走（ADR-015）。
 */
describe('ReviewStage 的生成用量回执', () => {
  function mountWithSummary(summary: RunSummary | null): VueWrapper {
    return mount(ReviewStage, {
      props: {
        title: '产品需求文档',
        content: CONTENT,
        viewState: 'review-prd',
        canFinish: false,
        hasPrd: true,
        hasApiDocs: false,
        hasPrompts: false,
        runSummary: summary,
      },
    })
  }

  it('没有运行摘要时不渲染回执（如直接打开一份已完成方案）', () => {
    const wrapper = mountWithSummary(null)
    expect(wrapper.text()).not.toContain('本次生成耗时')
  })

  it('展示耗时、token 总量与可选中复制的请求号', () => {
    const wrapper = mountWithSummary(runSummary())
    const text = wrapper.text()

    // 12300ms → 12 秒（不足 1 分钟按秒，四舍五入）
    expect(text).toContain('本次生成耗时 12 秒')
    // 900 + 300 = 1200 → 1.2k
    expect(text).toContain('约 1.2k tokens')
    expect(text).toContain('请求号')
    expect(text).toContain('req-abc123')

    // 请求号用 select-all 让用户能选中复制，而不是给一个复制按钮
    const code = wrapper.find('code')
    expect(code.exists()).toBe(true)
    expect(code.classes()).toContain('select-all')
  })

  it('没有失败调用时不提这一茬（正常情况下回执不该多一行）', () => {
    const wrapper = mountWithSummary(runSummary())
    expect(wrapper.text()).toContain('8 次调用')
    expect(wrapper.text()).not.toContain('次调用失败后重试')
  })

  it('失败过的调用会说出来——它占用了时间却不在 token 数里', () => {
    const wrapper = mountWithSummary(runSummary({ failedCalls: 2 }))
    expect(wrapper.text()).toContain('其中 2 次调用失败后重试')
  })

  it('token 为 0 时不展示 token 段（取不到用量时不留一个「0 tokens」）', () => {
    const wrapper = mountWithSummary(runSummary({ promptTokens: 0, completionTokens: 0 }))
    expect(wrapper.text()).not.toContain('tokens')
    expect(wrapper.text()).toContain('本次生成耗时')
  })

  it('没有请求号时不展示请求号段', () => {
    const wrapper = mountWithSummary(runSummary({ requestId: '', injectedSkills: [] }))
    expect(wrapper.text()).not.toContain('请求号')
    expect(wrapper.find('code').exists()).toBe(false)
  })

  it('展示技能包版本与指纹（这次是按哪一套模板写的，回执上讲得清）', () => {
    const wrapper = mountWithSummary(runSummary())

    expect(wrapper.text()).toContain('技能包 prd-generator v0.1.0')
    expect(wrapper.text()).toContain('e4f928df7be9')
  })

  it('PRD 没用上技能包时显式告警——不再静默退回内置提示词', () => {
    const wrapper = mountWithSummary(runSummary({ injectedSkills: [] }))

    expect(wrapper.text()).toContain('未加载技能包')
    // 只是一句提示：不进底栏，页面上也不因此多出按钮
    expect(wrapper.find('.shell-action-bar').text()).not.toContain('未加载技能包')
  })

  it('接口文档没有技能包不算异常（只有 PRD 依赖它，不该挂莫须有的告警）', () => {
    const wrapper = mountWithSummary(runSummary({ kind: 'api_docs', injectedSkills: [] }))

    expect(wrapper.text()).not.toContain('未加载技能包')
  })

  it('回执自身不带任何按钮，且不进入底栏', () => {
    const wrapper = mountWithSummary(runSummary())
    const bar = wrapper.find('.shell-action-bar')
    expect(bar.text()).not.toContain('本次生成耗时')
    // 页面上的按钮仍是底栏那几个，没有因为回执多出来
    const texts = wrapper.findAll('button').map((item) => item.text())
    expect(texts).not.toContain('复制请求号')
  })

  it('事前预算的告警逐条展示（只读提示，不产生任何按钮）', () => {
    const wrapper = mountWithSummary(
      runSummary({
        warnings: [
          'prd:writing 的输入约 96,000 tokens，已达预算上限 120,000 的 80%',
          'prd:reviewing 的输入约 110,000 tokens，已达预算上限 120,000 的 91%',
        ],
      }),
    )

    const items = wrapper.findAll('li')
    expect(items.map((item) => item.text())).toEqual([
      'prd:writing 的输入约 96,000 tokens，已达预算上限 120,000 的 80%',
      'prd:reviewing 的输入约 110,000 tokens，已达预算上限 120,000 的 91%',
    ])
    // 只是提示：不进底栏，页面上也没有因此多出按钮
    expect(wrapper.find('.shell-action-bar').text()).not.toContain('预算上限')
  })

  it('没有告警时不渲染告警块（大多数生成都逼近不了上限）', () => {
    const wrapper = mountWithSummary(runSummary())
    expect(wrapper.findAll('li')).toHaveLength(0)
  })
})

/**
 * 待确认清单的落点。
 *
 * 清单由后端在剥离正文标记块时算出、随正文落库，前端只读展示。放在正文下方、底栏之上
 * ——它描述的是「这份文档还差哪些问题没敲定」，属于内容本身，不是「对文档做的事」，
 * 因此不进底栏（ADR-015：主操作位只有一个）。
 */
describe('ReviewStage 的待确认清单', () => {
  function mountWithItems(pendingItems: PendingItem[] | undefined): VueWrapper {
    return mount(ReviewStage, {
      props: {
        title: '产品需求文档',
        content: CONTENT,
        viewState: 'review-prd',
        canFinish: false,
        hasPrd: true,
        hasApiDocs: false,
        hasPrompts: false,
        pendingItems,
      },
    })
  }

  it('有待确认项时挂在正文下方展示', () => {
    const wrapper = mountWithItems([
      {
        id: 'overview-1',
        question: '指标口径谁拍板？',
        sections: ['2.1'],
        decider: '数据团队',
        importance: 'P0',
      },
    ])

    expect(wrapper.text()).toContain('待确认问题（1）')
    expect(wrapper.text()).toContain('指标口径谁拍板？')
    expect(wrapper.text()).toContain('拍板人：数据团队')
  })

  it('没有待确认项时整块不渲染（不留一个空的「待确认问题（0）」）', () => {
    const wrapper = mountWithItems([])
    expect(wrapper.text()).not.toContain('待确认')
  })

  it('后端没给清单时也不渲染（旧快照 / 手改过的文档）', () => {
    const wrapper = mountWithItems(undefined)
    expect(wrapper.text()).not.toContain('待确认')
  })

  it('清单不进底栏：它是内容的一部分，不是「对文档做的事」', () => {
    const wrapper = mountWithItems([
      { id: 'p-1', question: '分页上限取多少？', sections: [], decider: '', importance: 'P1' },
    ])
    const bar = wrapper.find('.shell-action-bar')

    expect(bar.exists()).toBe(true)
    expect(bar.text()).not.toContain('分页上限取多少？')
    // 也没有因为清单多出任何按钮（P4 的澄清入口会接在清单下面，不占主操作位）
    const texts = wrapper.findAll('button').map((item) => item.text())
    expect(texts).not.toContain('去澄清')
  })

  /**
   * 两个澄清入口互斥：PRD 给「回到对话」（复用主对话栏），其余文档给「逐条澄清」
   * （独立澄清页）。由上层按阶段决定、组件只负责渲染——因此这里直接传两个开关。
   */
  it('PRD 给「回到对话」入口，其余文档给「逐条澄清」入口（两者互斥）', () => {
    const items: PendingItem[] = [
      { id: 'p-1', question: '分页上限取多少？', sections: [], decider: '', importance: 'P0' },
    ]
    const baseProps = {
      title: '文档',
      content: CONTENT,
      canFinish: false,
      hasPrd: true,
      hasApiDocs: false,
      hasPrompts: false,
      pendingItems: items,
    }

    const prd = mount(ReviewStage, {
      props: { ...baseProps, viewState: 'review-prd', canReturnToChat: true, canClarify: false },
    })
    expect(prd.text()).toContain('回到对话，确认剩下的 1 个问题')
    expect(prd.text()).not.toContain('逐条澄清这些待确认项')

    const apiDocs = mount(ReviewStage, {
      props: {
        ...baseProps,
        viewState: 'review-api-docs',
        canReturnToChat: false,
        canClarify: true,
      },
    })
    expect(apiDocs.text()).toContain('逐条澄清这些待确认项')
    expect(apiDocs.text()).not.toContain('回到对话，确认剩下的')
  })

  it('点「回到对话」发出 returnToChat 事件（走哪条路由由上层决定）', async () => {
    const wrapper = mount(ReviewStage, {
      props: {
        title: '产品需求文档',
        content: CONTENT,
        viewState: 'review-prd',
        canFinish: false,
        hasPrd: true,
        hasApiDocs: false,
        hasPrompts: false,
        pendingItems: [
          { id: 'p-1', question: '分页上限取多少？', sections: [], decider: '', importance: 'P0' },
        ],
        canReturnToChat: true,
      },
    })

    const entry = wrapper
      .findAll('button')
      .find((button) => button.text().includes('回到对话，确认剩下的'))
    expect(entry).toBeTruthy()
    await entry?.trigger('click')

    expect(wrapper.emitted('returnToChat')).toHaveLength(1)
    // 不沿用旧的 clarify 事件：两条路是两回事，各行其道
    expect(wrapper.emitted('clarify')).toBeUndefined()
  })
})

/**
 * 生成中显示已用时。
 *
 * 计时归任务运行态管（重连要用服务端值打底、收尾要停在权威总耗时），
 * 展示组件只读 `elapsedMs`；为 0 时不显示，避免刚进入生成态就闪一个「已用时 0 秒」。
 */
describe('GeneratingStage 的已用时', () => {
  function mountGenerating(props: Record<string, unknown> = {}): VueWrapper {
    return mount(GeneratingStage, {
      props: { title: '产品需求文档', streamingContent: '# 正在生成', generating: true, ...props },
    })
  }

  it('生成中且有耗时显示「已用时」', () => {
    const wrapper = mountGenerating({ elapsedMs: 65_000 })
    // 65000ms → 1 分 5 秒
    expect(wrapper.text()).toContain('已用时 1 分 5 秒')
  })

  it('耗时为零时不显示（尚未开始计时）', () => {
    const wrapper = mountGenerating({ elapsedMs: 0 })
    expect(wrapper.text()).not.toContain('已用时')
  })

  it('未在生成中时不显示（失败态只留错误与返回入口）', () => {
    const wrapper = mountGenerating({ elapsedMs: 5000, generating: false, error: '生成失败' })
    expect(wrapper.text()).not.toContain('已用时')
    expect(wrapper.text()).toContain('生成失败')
  })
})
