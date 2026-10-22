/**
 * 通用弹窗的用例。
 *
 * 这几个行为是「所有弹窗共用一套实现」的全部理由——写错任何一个，
 * 受害的是每一个用到它的页面，而且都不会报错：
 *
 * 1. **Esc 能关**：键盘用户不该被迫去点那个「关闭」。
 * 2. **点遮罩能关，点面板内部不能关**：后者会让「在输入框里点一下」变成取消。
 * 3. **打开时锁 body 滚动，关闭时解锁**：不锁的话滚轮会带着底下的页面走；
 *    不解锁的话整个后台再也滚不动（组件被路由切走时也必须解锁）。
 */

import { afterEach, describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import AdminModal from '@/components/AdminModal.vue'

function mountModal(open = true) {
  return mount(AdminModal, {
    props: { open, title: '删除账号', kicker: 'Delete' },
    slots: { default: '<p class="probe">正文</p>' },
  })
}

afterEach(() => {
  document.body.style.overflow = ''
})

describe('AdminModal', () => {
  it('打开时渲染 dialog 与标题，关闭时不渲染', async () => {
    const wrapper = mountModal(false)
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)

    await wrapper.setProps({ open: true })
    const dialog = wrapper.find('[role="dialog"]')
    expect(dialog.exists()).toBe(true)
    expect(dialog.attributes('aria-modal')).toBe('true')
    expect(wrapper.text()).toContain('删除账号')
    expect(wrapper.text()).toContain('Delete')
  })

  it('按 Esc 关闭', async () => {
    const wrapper = mountModal()
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    expect(wrapper.emitted('close')).toHaveLength(1)
  })

  it('点遮罩关闭，点面板内部不关闭', async () => {
    const wrapper = mountModal()

    await wrapper.find('.admin-modal-body').trigger('click')
    expect(wrapper.emitted('close')).toBeUndefined()

    await wrapper.find('[role="dialog"]').trigger('click')
    expect(wrapper.emitted('close')).toHaveLength(1)
  })

  it('打开时锁住页面滚动，关闭后解锁', async () => {
    const wrapper = mountModal()
    expect(document.body.style.overflow).toBe('hidden')

    await wrapper.setProps({ open: false })
    expect(document.body.style.overflow).toBe('')
  })

  it('弹窗还在打开状态时被卸载，也要解锁（否则整页再也滚不动）', () => {
    const wrapper = mountModal()
    expect(document.body.style.overflow).toBe('hidden')

    wrapper.unmount()
    expect(document.body.style.overflow).toBe('')
  })
})
