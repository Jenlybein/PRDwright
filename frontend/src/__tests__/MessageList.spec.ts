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
 * 2. **点选项卡选的**（带 `answers`）——**不是气泡**，而是一张轻回执卡。
 *
 * 第 2 条是本次需求的核心：选项卡提交时同步发出了 `content`（那段拼出来的
 * 「1. 题干 / 答：A. xxx」），但它是给**后端**的展示文本与降级兜底，不该原样念回
 * 给用户——既啰嗦，又像模型在说话。回执只显示**简化后的上下文**：题干压到两行
 * + 选中的「标号 + 标题」。
 *
 * 为什么不能只剩标号：选项卡一作答就整块消失（不可交互即不渲染），题干与选项
 * 文本再没有第二个地方可看。用户回看历史时看到「你选了 A」，根本不记得 A 是什么
 * ——这正是第一版回执被退回来的原因。
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
  it('带 answers 的消息渲染成轻回执：题干 + 「你选了 A」+ 选中项标题', () => {
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
    // 上下文三件套：题干、标号、选中项标题，回看时缺一样就读不懂
    expect(wrapper.text()).toContain('第一版要不要做离线同步？')
    expect(wrapper.text()).toContain('第一版就做离线同步')
  })

  it('回执只带选中项，不把未选中的选项也搬上来', () => {
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

    // 「B. 第一版只做在线」是卡片上的另一个选项，回执里不该有
    expect(wrapper.text()).not.toContain('第一版只做在线')
  })

  it('多选一次选中多项时，逐项给出标号与标题', () => {
    const answers: ClarificationAnswerItem[] = [
      { ask_id: 'ask-2', labels: ['A', 'C'], question: '首发要支持哪些端？', texts: ['网页版', '桌面客户端'] },
    ]
    const wrapper = mountList([{ role: 'user', content: 'x', stage: 'requirement', answers }])

    expect(wrapper.text()).toContain('A')
    expect(wrapper.text()).toContain('C')
    // 选中项标题要一起出现，且仍按选项展示顺序
    expect(wrapper.text()).toContain('网页版')
    expect(wrapper.text()).toContain('桌面客户端')
    expect(wrapper.text().indexOf('网页版')).toBeLessThan(wrapper.text().indexOf('桌面客户端'))
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

    // 仍是回执，而不是把上面那段冗长文本（带「1. / 答：」前缀）念回给用户
    expect(wrapper.find('[aria-label*="你选了 A 第一版就做离线同步"]').exists()).toBe(true)
    expect(wrapper.find('.bg-primary-500').exists()).toBe(false)
    // 题干与选中项都出现，但不再带「答：」这种给后端看的拼装格式
    expect(wrapper.text()).toContain('第一版就做离线同步')
    expect(wrapper.text()).not.toContain('答：')
  })

  it('历史回执缺 texts 时只显示标号，不渲染空行也不报错', () => {
    // 早期数据的 `texts` 可能为空；题干也可能缺（模型没写）。两种残缺都要能渲染。
    const wrapper = mountList([
      {
        role: 'user',
        content: 'x',
        stage: 'requirement',
        answers: [{ ask_id: 'ask-old', labels: ['B'], question: '', texts: [] }],
      },
    ])

    expect(wrapper.text()).toContain('你选了')
    expect(wrapper.text()).toContain('B')
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
