/**
 * 页头（PageHeader）的用例。
 *
 * 组件本身没有逻辑，所以钉的是**结构契约**——这几条一旦破了，
 * 问题不会报错，只会静默地让某个页面退化：
 *
 * 1. 标题必须是 `h1`：整页只有一个一级标题，屏幕阅读器的「按标题跳转」
 *    靠它定位；退成 div 之后页面仍然好看，但无障碍这条线就断了。
 * 2. 说明文字走 `desc` 槽：它要能整段不传（有的页面一句话就够）。
 * 3. 动作走 `actions` 槽，且不与标题共用一个点击区域。
 */

import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import PageHeader from '@/components/PageHeader.vue'

describe('PageHeader', () => {
  it('标题渲染成唯一的 h1，栏目名与标题各就各位', () => {
    const wrapper = mount(PageHeader, {
      props: { kicker: 'Jobs / 现场', title: '任务监控' },
    })

    const h1 = wrapper.findAll('h1')
    expect(h1).toHaveLength(1)
    expect(h1[0]!.text()).toBe('任务监控')
    expect(wrapper.text()).toContain('Jobs / 现场')
    expect(wrapper.find('.page-head').exists()).toBe(true)
  })

  it('不传说明与动作时也能渲染，且不留下空容器', () => {
    const wrapper = mount(PageHeader, {
      props: { kicker: 'K', title: 'T' },
    })

    expect(wrapper.find('.page-head-desc').exists()).toBe(false)
  })

  it('动作与说明分别落在各自的槽里，不混进标题区', () => {
    const wrapper = mount(PageHeader, {
      props: { kicker: 'K', title: 'T' },
      slots: {
        actions: '<button class="probe-action">刷新</button>',
        desc: '<p class="probe-desc">口径说明</p>',
      },
    })

    expect(wrapper.find('.probe-action').exists()).toBe(true)
    expect(wrapper.find('.page-head-desc .probe-desc').exists()).toBe(true)
    // 动作不在 h1 的祖先链里：点按钮不该触发「点标题」这类语义
    expect(wrapper.find('h1').find('.probe-action').exists()).toBe(false)
  })
})
