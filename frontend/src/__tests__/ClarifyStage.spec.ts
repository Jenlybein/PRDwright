import { mount, type VueWrapper } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ClarifyStage from '../components/stages/ClarifyStage.vue'
import type { ChatMessage } from '../types'

/**
 * 文档澄清页的展示口径。
 *
 * 它和「需求澄清」长得像，但终点不同：澄清本身不产出文档，**必须由用户显式触发改写**。
 * 因此这里守两件事——主操作的可用条件（没有对话 / 没有待确认项时不该可点），
 * 以及「只动涉及章节」这句承诺要写在页面上（用户最担心的就是稿子被覆盖）。
 */
const MESSAGES: ChatMessage[] = [
  { role: 'assistant', content: '分页上限取多少？' },
  { role: 'user', content: '就定 50 条。' },
]

function mountStage(props: Record<string, unknown> = {}): VueWrapper {
  return mount(ClarifyStage, {
    props: {
      messages: MESSAGES,
      targetLabel: '接口文档',
      pendingCount: 2,
      ...props,
    },
  })
}

/** 按文案找按钮 */
function buttonByText(wrapper: VueWrapper, text: string) {
  const button = wrapper.findAll('button').find((item) => item.text().includes(text))
  if (!button) throw new Error(`未找到按钮：${text}`)
  return button
}

describe('ClarifyStage', () => {
  it('标题带上这次澄清针对的是哪一份文档', () => {
    const wrapper = mountStage()
    expect(wrapper.text()).toContain('澄清 接口文档 里的待确认项')
  })

  it('把「只动涉及章节」这句承诺写在页面上', () => {
    // 用户最怕的就是「聊完把我改好的稿子整篇覆盖掉」，提前说明比事后解释便宜
    expect(mountStage().text()).toContain('逐字保留')
  })

  it('点「按澄清结果改写」把事件交给上层——本组件不发请求', async () => {
    const wrapper = mountStage()
    await buttonByText(wrapper, '按澄清结果改写').trigger('click')

    expect(wrapper.emitted('rewrite')).toHaveLength(1)
  })

  it('还没有对话时禁用改写，并提示先聊清楚', () => {
    const wrapper = mountStage({ messages: [], pendingCount: 2 })
    expect(buttonByText(wrapper, '按澄清结果改写').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('先聊清楚')
  })

  it('没有待确认项时禁用改写，并说明原因', () => {
    const wrapper = mountStage({ pendingCount: 0 })
    expect(buttonByText(wrapper, '按澄清结果改写').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('已经没有待确认项')
  })

  it('改写进行中时按钮仍显示但不可点，文案变成「正在改写…」', () => {
    // 藏起来只会让用户以为点错了，再去找一遍入口
    const wrapper = mountStage({ rewriting: true })
    const button = buttonByText(wrapper, '正在改写')
    expect(button.attributes('disabled')).toBeDefined()
  })

  it('返回审阅：把事件交给上层（回到哪一份的审阅页由它决定）', async () => {
    const wrapper = mountStage()
    await buttonByText(wrapper, '返回审阅').trigger('click')

    expect(wrapper.emitted('back')).toHaveLength(1)
  })

  it('错误贴在会话区顶部，不打断已有消息', () => {
    const wrapper = mountStage({ error: '按澄清结果改写失败' })
    const alert = wrapper.find('[role="alert"]')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toContain('按澄清结果改写失败')
    // 消息本身还在
    expect(wrapper.text()).toContain('分页上限取多少？')
  })
})
