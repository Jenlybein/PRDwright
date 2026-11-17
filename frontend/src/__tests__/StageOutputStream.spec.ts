import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import StageOutputStream from '../components/ui/StageOutputStream.vue'

/**
 * 阶段输出面板：大纲 / 审核 / 改写这几步要么是整篇量级的调用、要么是「先定结构」的
 * 关键决定，面板存在的意义就是「让等待与判断都有证据」。
 *
 * 守四条：审核 / 改写只在对应阶段出现（生成阶段有正文预览、不需要它）、有文本就显示、
 * 一个字的还没有时给等待提示而不是空白、**大纲单独一块且正文撰写期间仍在**。
 */
describe('StageOutputStream：阶段输出面板', () => {
  it('生成阶段不出现审核块：那时正文预览自己在流，再来一块输出面板只会重复', () => {
    const wrapper = mount(StageOutputStream, { props: { phase: 'writing', text: '正文增量' } })

    expect(wrapper.find('[aria-label="阶段输出"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('审核意见')
  })

  it('审核阶段显示审核意见原文', () => {
    const wrapper = mount(StageOutputStream, {
      props: { phase: 'reviewing', text: '{"verdict":"pass"}' },
    })

    expect(wrapper.text()).toContain('审核意见')
    expect(wrapper.text()).toContain('{"verdict":"pass"}')
  })

  it('改写阶段换成改写稿标题，并说明会整段刷新预览', () => {
    const wrapper = mount(StageOutputStream, {
      props: { phase: 'rewriting', text: '# 改写后的正文' },
    })

    expect(wrapper.text()).toContain('改写稿')
    // 只重写受影响的章节，但发过来的是拼好的全文，所以预览仍是一次整段刷新
    expect(wrapper.text()).toContain('整段刷新')
    expect(wrapper.text()).toContain('# 改写后的正文')
  })

  it('还没有第一个字时给等待提示，不留一片空白', () => {
    const wrapper = mount(StageOutputStream, { props: { phase: 'reviewing', text: '' } })

    expect(wrapper.text()).toContain('等待模型首个输出')
  })

  it('大纲阶段显示大纲原文：用户能看到「先定了哪些结构」', () => {
    const wrapper = mount(StageOutputStream, {
      props: {
        phase: 'outlining',
        text: '',
        outlineText: '# 接口文档大纲\n- 接口列表\n    · - F-01 用户登录：支持邮箱密码登录',
      },
    })

    expect(wrapper.find('[aria-label="大纲输出"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('大纲')
    expect(wrapper.text()).toContain('F-01 用户登录')
    // 抽取阶段还没到审核，不该冒出审核块
    expect(wrapper.find('[aria-label="阶段输出"]').exists()).toBe(false)
  })

  it('撰写期间大纲仍留在面板上（与「换阶段即清空」的审核块不同）', () => {
    const wrapper = mount(StageOutputStream, {
      props: { phase: 'writing', text: '', outlineText: '- 接口列表\n    · F-01 用户登录' },
    })

    expect(wrapper.find('[aria-label="大纲输出"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('F-01 用户登录')
    expect(wrapper.text()).toContain('正文正在写')
  })

  it('「待确认」项原样展示（与待确认清单同一口径，不另起说法）', () => {
    const wrapper = mount(StageOutputStream, {
      props: {
        phase: 'outlining',
        text: '',
        outlineText: '- 错误码约定：待确认（上游材料未给出与「错误码约定」相关的内容）',
      },
    })

    expect(wrapper.text()).toContain('待确认')
  })
})
