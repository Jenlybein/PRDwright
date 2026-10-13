import { mount, type VueWrapper } from '@vue/test-utils'
import { nextTick } from 'vue'
import { describe, expect, it } from 'vitest'

import DocumentReview from '../components/DocumentReview.vue'
import GeneratingStage from '../components/stages/GeneratingStage.vue'
import ReviewStage from '../components/stages/ReviewStage.vue'

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
