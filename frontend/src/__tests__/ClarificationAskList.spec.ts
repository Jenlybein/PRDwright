import { mount, type VueWrapper } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ClarificationAskList, {
  type AnswerPayload,
} from '../components/ClarificationAskList.vue'
import type { ClarificationAsk } from '../types'

/**
 * 澄清提问的选项卡。
 *
 * 它承载两种作答方式（单选 / 多选），且**只该有一个出口**：整轮一个「提交回答」，
 * 把已作答的题组织成一条消息。「想自己写」不在这里办——那是底部聊天输入框的事，
 * 本组件只在提交按钮旁常驻一行提示把人指过去。这里盯住五件事：
 *
 * 1. 选择状态与「谁可交互」对得上（历史消息不可点，但选项文字照常显示）；
 * 2. 单选与多选的行为差异（替换 vs 切换）；
 * 3. 组织出来的消息文本能让人读懂「他答的是哪道题」；
 * 4. 那行指路文案常驻且在位，且组件里不再有任何输入框；
 * 5. 出口发出的是**结构化载荷**：除展示文本外还带逐题的结构化作答，
 *    且拿不到工具调用 id 时**不带**这个键（降级为纯文本）。
 */

const SINGLE: ClarificationAsk = {
  id: 'ask-1',
  question: '第一版要不要做离线同步？',
  background: '你提到用户常在弱网环境使用。',
  multiple: false,
  options: [
    { label: 'A', text: '第一版就做离线同步' },
    { label: 'B', text: '第一版只做在线' },
  ],
}

const MULTI: ClarificationAsk = {
  id: 'ask-2',
  question: '首发要支持哪些端？',
  background: '',
  multiple: true,
  options: [
    { label: 'A', text: '网页版' },
    { label: 'B', text: '小程序' },
    { label: 'C', text: '桌面客户端' },
  ],
}

/** 给一道题挂上工具调用 id；缺省 `call-1` 模拟后端随 `done` 下发的来源调用 */
function withToolCallId(ask: ClarificationAsk, toolCallId = 'call-1'): ClarificationAsk {
  return { ...ask, tool_call_id: toolCallId }
}

function mountAsks(
  asks: ClarificationAsk[] = [SINGLE],
  props: Record<string, unknown> = {},
): VueWrapper {
  return mount(ClarificationAskList, { props: { asks, interactive: true, ...props } })
}

/** 按选项标号取那个可点卡片 */
function optionOf(wrapper: VueWrapper, label: string) {
  const button = wrapper.findAll('button').find((item) => item.text().startsWith(label))
  if (!button) throw new Error(`未找到选项：${label}`)
  return button
}

/** 按文案取按钮 */
function buttonOf(wrapper: VueWrapper, text: string) {
  const button = wrapper.findAll('button').find((item) => item.text() === text)
  if (!button) throw new Error(`未找到按钮：${text}`)
  return button
}

/** 最近一次 emit 出来的回答载荷 */
function lastAnswer(wrapper: VueWrapper): AnswerPayload {
  const events = wrapper.emitted('answer') ?? []
  const last = events[events.length - 1]
  expect(last, '应当 emit 出 answer').toBeTruthy()
  return last?.[0] as AnswerPayload
}

/** 最近一次 emit 出来的展示文本 */
function lastText(wrapper: VueWrapper): string {
  return lastAnswer(wrapper).text
}

describe('ClarificationAskList', () => {
  it('单选题给出 radiogroup 语义，选中态走 aria-checked', async () => {
    const wrapper = mountAsks()
    const group = wrapper.find('[role="radiogroup"]')

    expect(group.exists()).toBe(true)
    expect(group.attributes('aria-label')).toBe(SINGLE.question)
    expect(optionOf(wrapper, 'A').attributes('aria-checked')).toBe('false')

    await optionOf(wrapper, 'A').trigger('click')

    expect(optionOf(wrapper, 'A').attributes('aria-checked')).toBe('true')
    expect(optionOf(wrapper, 'B').attributes('aria-checked')).toBe('false')
  })

  it('单选题换选是替换而不是累加', async () => {
    const wrapper = mountAsks()

    await optionOf(wrapper, 'A').trigger('click')
    await optionOf(wrapper, 'B').trigger('click')

    expect(optionOf(wrapper, 'A').attributes('aria-checked')).toBe('false')
    expect(optionOf(wrapper, 'B').attributes('aria-checked')).toBe('true')
  })

  it('多选题可以累计选择，提交时把选项按行内顺序拼起来', async () => {
    const wrapper = mountAsks([MULTI])

    expect(wrapper.find('[role="group"]').exists()).toBe(true)

    await optionOf(wrapper, 'A').trigger('click')
    await optionOf(wrapper, 'C').trigger('click')
    await buttonOf(wrapper, '提交回答').trigger('click')

    expect(lastText(wrapper)).toBe(
      '1. 首发要支持哪些端？\n   答：A. 网页版；C. 桌面客户端',
    )
  })

  it('多选题反序点击也按选项显示顺序拼接（提交文本不随点击次序变）', async () => {
    const wrapper = mountAsks([MULTI])

    // 先点 C 再点 A：顺序是操作路径，不该泄漏到发出去的文本里
    await optionOf(wrapper, 'C').trigger('click')
    await optionOf(wrapper, 'A').trigger('click')
    await buttonOf(wrapper, '提交回答').trigger('click')

    expect(lastText(wrapper)).toBe(
      '1. 首发要支持哪些端？\n   答：A. 网页版；C. 桌面客户端',
    )
  })

  it('没有作何作答时「提交回答」是禁用的（提交空消息没有意义）', () => {
    const wrapper = mountAsks()

    expect(buttonOf(wrapper, '提交回答').attributes('disabled')).toBeDefined()
  })

  it('提交按钮旁常驻一行指路文案，逐字为「若不满意选项，可以自行在输入框输入想法」', () => {
    const wrapper = mountAsks()

    // 本次需求的核心：自己写的入口指向底部聊天输入框，这里只给一句常驻指路
    const spanTexts = wrapper.findAll('span').map((item) => item.text())
    expect(spanTexts).toContain('若不满意选项，可以自行在输入框输入想法')

    // 且组件里不再有任何输入框——想自己写去底部的那个，不在这儿另立一个
    expect(wrapper.find('textarea').exists()).toBe(false)
  })

  it('一轮问了多道题：题干才显示，且一次提交把它们合成一条消息', async () => {
    const wrapper = mountAsks([SINGLE, MULTI])

    // 只问一道题时正文里刚写过同一个问题，不重复渲染
    expect(wrapper.text()).toContain(SINGLE.question)
    expect(wrapper.text()).toContain(MULTI.question)

    await optionOf(wrapper, 'A').trigger('click')
    // 两组题里各有一个「B」，第二组的 B 是最后那一个
    const bButtons = wrapper.findAll('button').filter((item) => item.text().startsWith('B'))
    await bButtons[bButtons.length - 1]?.trigger('click')
    await buttonOf(wrapper, '提交回答').trigger('click')

    expect(lastText(wrapper)).toBe(
      '1. 第一版要不要做离线同步？\n   答：A. 第一版就做离线同步\n' +
        '2. 首发要支持哪些端？\n   答：B. 小程序',
    )
  })

  it('只答了一部分也能提交（未答的留给下一轮，不该把人卡住）', async () => {
    const wrapper = mountAsks([SINGLE, MULTI])

    await optionOf(wrapper, 'A').trigger('click')

    expect(wrapper.text()).toContain('还有 1 题未作答')

    await buttonOf(wrapper, '提交回答').trigger('click')

    expect(lastText(wrapper)).toBe('1. 第一版要不要做离线同步？\n   答：A. 第一版就做离线同步')
  })

  it('只问一道题时不显示题干（正文里刚写过，重复一遍是噪声）', () => {
    const wrapper = mountAsks([SINGLE])

    // 选项文字里有「离线同步」，题干整句不该出现
    expect(wrapper.find('p').exists()).toBe(false)
  })

  it('提交时 emit 结构化载荷：展示文本 + 逐题的 labels/question/texts（按选项顺序）', async () => {
    const wrapper = mountAsks([withToolCallId(MULTI)])

    // 故意反序点击：结构化作答的顺序同样该跟随选项，而不是手指
    await optionOf(wrapper, 'C').trigger('click')
    await optionOf(wrapper, 'A').trigger('click')
    await buttonOf(wrapper, '提交回答').trigger('click')

    const payload = lastAnswer(wrapper)
    expect(payload.text).toBe('1. 首发要支持哪些端？\n   答：A. 网页版；C. 桌面客户端')
    expect(payload.toolResult).toEqual({
      tool_call_id: 'call-1',
      answers: [
        {
          ask_id: 'ask-2',
          labels: ['A', 'C'],
          question: '首发要支持哪些端？',
        texts: ['网页版', '桌面客户端'],
        },
      ],
    })
  })

  it('一轮多道题时结构化作答逐题给出，tool_call_id 取本轮共同的那个', async () => {
    const wrapper = mountAsks([withToolCallId(SINGLE), withToolCallId(MULTI)])

    await optionOf(wrapper, 'A').trigger('click')
    const bButtons = wrapper.findAll('button').filter((item) => item.text().startsWith('B'))
    await bButtons[bButtons.length - 1]?.trigger('click')
    await buttonOf(wrapper, '提交回答').trigger('click')

    expect(lastAnswer(wrapper).toolResult).toEqual({
      tool_call_id: 'call-1',
      answers: [
        {
          ask_id: 'ask-1',
          labels: ['A'],
          question: '第一版要不要做离线同步？',
          texts: ['第一版就做离线同步'],
        },
        {
          ask_id: 'ask-2',
          labels: ['B'],
          question: '首发要支持哪些端？',
          texts: ['小程序'],
        },
      ],
    })
  })

  it('只答了一部分时结构化作答也只含答过的那些题', async () => {
    const wrapper = mountAsks([withToolCallId(SINGLE), withToolCallId(MULTI)])

    await optionOf(wrapper, 'A').trigger('click')
    await buttonOf(wrapper, '提交回答').trigger('click')

    expect(lastAnswer(wrapper).toolResult?.answers).toEqual([
      {
        ask_id: 'ask-1',
        labels: ['A'],
        question: '第一版要不要做离线同步？',
        texts: ['第一版就做离线同步'],
      },
    ])
  })

  it('题目缺 tool_call_id 时载荷里**没有** toolResult 这个键（降级为纯文本作答）', async () => {
    const wrapper = mountAsks([SINGLE])

    await optionOf(wrapper, 'A').trigger('click')
    await buttonOf(wrapper, '提交回答').trigger('click')

    const payload = lastAnswer(wrapper)
    expect(payload).not.toHaveProperty('toolResult')
    // ⚠️ 但 `answers` 照旧带上：降级只影响「模型怎么收到答案」，
    // 不影响「用户看到什么」——他看不到模型收到了什么。
    expect(payload.answers).toEqual([
      {
        ask_id: 'ask-1',
        labels: ['A'],
        question: '第一版要不要做离线同步？',
        texts: ['第一版就做离线同步'],
      },
    ])
    // 展示文本照旧发出去：后端按普通用户消息处理，这段完整人话是它唯一能读到的东西
    expect(payload.text).toBe('1. 第一版要不要做离线同步？\n   答：A. 第一版就做离线同步')
  })

  it('一轮里只要有一道题缺 tool_call_id 就不带 toolResult', async () => {
    const wrapper = mountAsks([withToolCallId(SINGLE), MULTI])

    await optionOf(wrapper, 'A').trigger('click')
    const bButtons = wrapper.findAll('button').filter((item) => item.text().startsWith('B'))
    await bButtons[bButtons.length - 1]?.trigger('click')
    await buttonOf(wrapper, '提交回答').trigger('click')

    // 半截结构化答案比没有更糟：模型会看到一次只被应答了一部分的调用
    expect(lastAnswer(wrapper)).not.toHaveProperty('toolResult')
  })

  it('一轮里的 tool_call_id 不一致时不带 toolResult', async () => {
    const wrapper = mountAsks([withToolCallId(SINGLE, 'call-1'), withToolCallId(MULTI, 'call-2')])

    await optionOf(wrapper, 'A').trigger('click')
    const bButtons = wrapper.findAll('button').filter((item) => item.text().startsWith('B'))
    await bButtons[bButtons.length - 1]?.trigger('click')
    await buttonOf(wrapper, '提交回答').trigger('click')

    expect(lastAnswer(wrapper)).not.toHaveProperty('toolResult')
  })

  it('不可交互时整块不渲染：提交之后选项卡就该消失', async () => {
    const wrapper = mountAsks([SINGLE], { interactive: false })

    // 不是「退成只读样式」——卡片整块不出现，连选项文字也不再占地方
    expect(wrapper.findAll('button')).toHaveLength(0)
    expect(wrapper.text()).not.toContain('第一版就做离线同步')
    expect(wrapper.text()).not.toContain('提交回答')

    // 也就无从点出回答
    expect(wrapper.emitted('answer')).toBeUndefined()
  })

  it('交互态才渲染卡片与「提交回答」', async () => {
    const wrapper = mountAsks([SINGLE], { interactive: true })

    // 选项卡片 + 提交按钮
    expect(wrapper.findAll('button')).toHaveLength(SINGLE.options.length + 1)
    expect(buttonOf(wrapper, '提交回答').exists()).toBe(true)
  })
})
