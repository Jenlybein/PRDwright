import { mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, describe, expect, it } from 'vitest'

import AppDialog from '../components/ui/AppDialog.vue'

/**
 * 统一弹窗底座的用例。
 *
 * 这里只钉「外部可观察」的部分：DOM 是否存在、`aria` 是否正确、事件是否抛出、焦点落在哪。
 * 曲线与时长无法在 jsdom 里验证，留给人工慢放清单。
 */

function mountDialog(props: Record<string, unknown> = {}): VueWrapper {
  return mount(AppDialog, {
    props: { modelValue: true, title: '登录', ...props },
    slots: { default: '<p class="probe">正文</p>' },
    global: { stubs: { teleport: true } },
    attachTo: document.body,
  })
}

afterEach(() => {
  document.body.innerHTML = ''
})

describe('AppDialog 的关闭路径', () => {
  it('打开时渲染 dialog、标题与遮罩', () => {
    const wrapper = mountDialog()
    const dialog = wrapper.find('[role="dialog"]')
    expect(dialog.exists()).toBe(true)
    expect(dialog.attributes('aria-modal')).toBe('true')
    expect(wrapper.text()).toContain('登录')
    expect(wrapper.find('.dialog-backdrop').exists()).toBe(true)
  })

  it('点面板内部不关闭，点遮罩关闭', async () => {
    const wrapper = mountDialog()

    await wrapper.find('.probe').trigger('click')
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()

    await wrapper.find('.dialog-backdrop').trigger('click')
    expect(wrapper.emitted('update:modelValue')?.[0]).toEqual([false])
  })

  it('Esc 关闭', async () => {
    const wrapper = mountDialog()

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await wrapper.vm.$nextTick()

    expect(wrapper.emitted('update:modelValue')?.[0]).toEqual([false])
  })

  it('关闭状态下按 Esc 不抛事件', async () => {
    const wrapper = mountDialog()

    await wrapper.setProps({ modelValue: false })
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))

    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
  })
})

describe('AppDialog 的无障碍', () => {
  it('标题通过 aria-labelledby 关联', () => {
    const wrapper = mountDialog()
    const dialog = wrapper.find('[role="dialog"]')
    const id = dialog.attributes('aria-labelledby')

    expect(id).toBeTruthy()
    expect(wrapper.find(`#${id}`).text()).toBe('登录')
  })

  it('打开时焦点进入面板，关闭后归还给打开前的元素', async () => {
    const trigger = document.createElement('button')
    trigger.textContent = '打开'
    document.body.appendChild(trigger)
    trigger.focus()
    expect(document.activeElement).toBe(trigger)

    const wrapper = mountDialog()
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()

    expect(document.activeElement?.closest('[role="dialog"]')).toBeTruthy()

    await wrapper.setProps({ modelValue: false })
    await wrapper.vm.$nextTick()

    expect(document.activeElement).toBe(trigger)
  })

  it('遮罩抑制滚动穿透', () => {
    const wrapper = mountDialog()
    expect(wrapper.find('.dialog-backdrop').classes()).toContain('overscroll-contain')
  })
})
