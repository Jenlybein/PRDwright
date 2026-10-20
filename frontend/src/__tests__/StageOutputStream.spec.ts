import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import StageOutputStream from '../components/ui/StageOutputStream.vue'

/**
 * 阶段输出面板：审核 / 改写两步的静默期很长，面板存在的意义就是「让等待看得见」。
 *
 * 守三条：只在审核 / 改写出现（生成阶段有正文预览、不需要它）、有文本就显示、
 * 一个字的还没有时给等待提示而不是空白。
 */
describe('StageOutputStream：阶段输出面板', () => {
  it('生成阶段不出现：那时正文预览自己在流，再来一块输出面板只会重复', () => {
    const wrapper = mount(StageOutputStream, { props: { phase: 'writing', text: '正文增量' } })

    expect(wrapper.text()).not.toContain('正文增量')
    expect(wrapper.find('[aria-label="阶段输出"]').exists()).toBe(false)
  })

  it('审核阶段显示审核意见原文', () => {
    const wrapper = mount(StageOutputStream, {
      props: { phase: 'reviewing', text: '{"verdict":"pass"}' },
    })

    expect(wrapper.text()).toContain('审核意见')
    expect(wrapper.text()).toContain('{"verdict":"pass"}')
  })

  it('改写阶段换成改写稿标题，并说明会整段替换预览', () => {
    const wrapper = mount(StageOutputStream, {
      props: { phase: 'rewriting', text: '# 改写后的正文' },
    })

    expect(wrapper.text()).toContain('改写稿')
    expect(wrapper.text()).toContain('整段替换')
    expect(wrapper.text()).toContain('# 改写后的正文')
  })

  it('还没有第一个字时给等待提示，不留一片空白', () => {
    const wrapper = mount(StageOutputStream, { props: { phase: 'reviewing', text: '' } })

    expect(wrapper.text()).toContain('等待模型首个输出')
  })
})
