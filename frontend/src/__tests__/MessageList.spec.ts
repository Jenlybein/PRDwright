import { mount, type VueWrapper } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import MessageList from '../components/MessageList.vue'
import type { ChatMessage, ClarificationAnswerItem } from '../types'

/**
 * 聊天消息列表里**用户消息的两种形态**。
 *
 * 同一条「我说了什么」有两种来源，它们要有明显不同的样子：
 *
 * 1. **自己敲的**（不带 `answers`）——普通气泡，原样显示；
 * 2. **点选项卡选的**（带 `answers`）——**不是气泡**，而是一行轻回执。
 *
 * 第 2 条是本次需求的核心：选项卡提交时同步发出了 `content`（那段拼出来的
 * 「1. 题干 / 答：A. xxx」），但它是给**后端**的展示文本与降级兜底，不该原样念回
 * 给用户——既啰嗦，又像模型在说话。回执只列**选中的标号**，题干与选项文本一概不显示。
 *
 * 顺带钉住：选项卡**只在可交互时出现**（不可交互整块不渲染），因此作答后不会残留
 * 一张点不动的卡片。
 */

const ASK = {
  id: 'ask-1',
  question: '第一版要不要做离线同步？',
  background: '你提到用户常在弱网环境使用。',
  multiple: false,
  options: [
    { label: 'A', text: '第一版就做离线同步' },
    { label: 'B', text: '第一版只做在线' },
  ],
}

function mountList(messages: ChatMessage[]): VueWrapper {
  return mount(MessageList, { props: { messages }, attachTo: document.body })
}

describe('MessageList 的用户消息形态', () => {
  it('带 answers 的消息渲染成紧凑回执：显示「你选了 A」', () => {
    const wrapper = mountList([
      {
        role: 'user',
        content: '1. 第一版要不要做离线同步？\n   答：A. 第一版就做离线同步',
        stage: 'requirement',
        answers: [
          {
            ask_id: 'ask-1',
            labels: ['A'],
            question: '第一版要不要做离线同步？',
            texts: ['第一版就做离线同步'],
          },
        ],
      },
    ])

    expect(wrapper.text()).toContain('你选了')
    expect(wrapper.text()).toContain('A')
  })

  it('回执不显示题干与选项文本（只给标号）', () => {
    const wrapper = mountList([
      {
        role: 'user',
        content: '1. 第一版要不要做离线同步？\n   答：A. 第一版就做离线同步',
        stage: 'requirement',
        answers: [
          {
            ask_id: 'ask-1',
            labels: ['A'],
            question: '第一版要不要做离线同步？',
            texts: ['第一版就做离线同步'],
          },
        ],
      },
    ])

    // 题干与选项文本都只属于卡片与正文，不该在回执里再念一遍
    expect(wrapper.text()).not.toContain('第一版要不要做离线同步？')
    expect(wrapper.text()).not.toContain('第一版就做离线同步')
    expect(wrapper.text()).not.toContain('第一版只做在线')
  })

  it('多选一次选中多项时也只列标号', () => {
    const answers: ClarificationAnswerItem[] = [
      { ask_id: 'ask-2', labels: ['A', 'C'], question: '首发要支持哪些端？', texts: ['网页版', '桌面客户端'] },
    ]
    const wrapper = mountList([{ role: 'user', content: 'x', stage: 'requirement', answers }])

    expect(wrapper.text()).toContain('A')
    expect(wrapper.text()).toContain('C')
    expect(wrapper.text()).not.toContain('网页版')
    expect(wrapper.text()).not.toContain('桌面客户端')
  })

  it('一轮多道题时逐行显示，并带上题号前缀', () => {
    const answers: ClarificationAnswerItem[] = [
      { ask_id: 'ask-1', labels: ['A'], question: '第一版要不要做离线同步？', texts: ['第一版就做离线同步'] },
      { ask_id: 'ask-2', labels: ['B'], question: '首发要支持哪些端？', texts: ['小程序'] },
    ]
    const wrapper = mountList([{ role: 'user', content: 'x', stage: 'requirement', answers }])

    expect(wrapper.text()).toContain('第 1 题')
    expect(wrapper.text()).toContain('第 2 题')
  })

  it('单题时不加题号前缀', () => {
    const answers: ClarificationAnswerItem[] = [
      { ask_id: 'ask-1', labels: ['A'], question: '第一版要不要做离线同步？', texts: ['第一版就做离线同步'] },
    ]
    const wrapper = mountList([{ role: 'user', content: 'x', stage: 'requirement', answers }])

    expect(wrapper.text()).not.toContain('第 1 题')
  })

  it('普通用户消息（无 answers）仍是气泡', () => {
    const wrapper = mountList([{ role: 'user', content: '我自己的一段话', stage: 'requirement' }])

    // 气泡的样式特征：`bg-primary-500` + 圆角；回执不带它
    const bubble = wrapper.find('.bg-primary-500')
    expect(bubble.exists()).toBe(true)
    expect(bubble.text()).toContain('我自己的一段话')
  })

  it('降级（题目缺 tool_call_id）时界面不退成气泡：只要带 answers 就渲染回执', () => {
    // 缺 `tool_call_id` 只让 `toolResult` 发不出去（模型那一路转纯文本），
    // `answers` 仍挂在消息上——回执形式与能否结构化回灌无关。
    const answers: ClarificationAnswerItem[] = [
      { ask_id: 'ask-1', labels: ['A'], question: '第一版要不要做离线同步？', texts: ['第一版就做离线同步'] },
    ]
    const wrapper = mountList([
      { role: 'user', content: '1. 第一版要不要做离线同步？\n   答：A. 第一版就做离线同步', stage: 'requirement', answers },
    ])

    // 仍是回执，而不是把上面那段冗长文本念回给用户
    expect(wrapper.find('[aria-label="你选了 A"]').exists()).toBe(true)
    expect(wrapper.find('.bg-primary-500').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('第一版就做离线同步')
  })

  it('answers 为空数组时退回气泡（只在真有值时才挂键）', () => {
    const wrapper = mountList([
      { role: 'user', content: '我自己的一段话', stage: 'requirement', answers: [] },
    ])

    expect(wrapper.find('.bg-primary-500').exists()).toBe(true)
  })

  it('助手消息里的选项卡只在可交互时出现，作答后不再渲染', () => {
    const wrapper = mountList([
      { role: 'assistant', content: '你更倾向哪种做法？', stage: 'requirement', asks: [ASK] },
      { role: 'user', content: '答：A', stage: 'requirement' },
    ])

    // 提问那条已不是最后一条：卡片整块消失，正文照常显示
    expect(wrapper.text()).toContain('你更倾向哪种做法？')
    expect(wrapper.findAll('[role="radiogroup"]')).toHaveLength(0)
    expect(wrapper.text()).not.toContain('提交回答')
  })
})
