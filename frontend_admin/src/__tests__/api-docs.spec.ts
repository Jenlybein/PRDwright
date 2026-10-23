/**
 * 接口文档页的用例。
 *
 * 钉两件「写错了不会报错，只会骗人」的事：
 *
 * 1. **两个按钮打开的是管理员专属地址**。写成 `/docs` 的表现是：页面能打开、
 *    点开却 401/404——而用户会以为「这个功能坏了」，不会想到是地址过时了。
 * 2. **页面上写出了「仅管理员」**。它不是装饰：同事那边打不开时，这行字是唯一
 *    能解释「为什么」的东西（后端对非管理员一律回「账号不存在」，不会说原因）。
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'

import ApiDocsView from '@/views/ApiDocsView.vue'

type Wrapper = ReturnType<typeof mount>

function openButtons(wrapper: Wrapper) {
  return wrapper.findAll('button')
}

describe('接口文档页', () => {
  afterEach(() => {
    // `mockRestore` 比 `clearAllMocks` 可靠：后者只清调用记录，恢复不了被替换的实现
    vi.restoreAllMocks()
  })

  it('两个入口分别打开 Swagger UI 与 ReDoc', async () => {
    const open = vi.spyOn(window, 'open').mockImplementation(() => null)
    const wrapper = mount(ApiDocsView)

    const buttons = openButtons(wrapper)
    expect(buttons).toHaveLength(2)

    await buttons[0]!.trigger('click')
    expect(open).toHaveBeenNthCalledWith(1, '/api/admin/docs', '_blank', 'noopener')

    await buttons[1]!.trigger('click')
    expect(open).toHaveBeenNthCalledWith(2, '/api/admin/redoc', '_blank', 'noopener')
  })

  it('三个地址都写在页面上，且都带 /api/admin 前缀', () => {
    const wrapper = mount(ApiDocsView)
    const text = wrapper.text()

    expect(text).toContain('/api/admin/docs')
    expect(text).toContain('/api/admin/redoc')
    expect(text).toContain('/api/admin/openapi.json')
  })

  it('明确说明只有管理员能打开', () => {
    const wrapper = mount(ApiDocsView)
    expect(wrapper.text()).toContain('仅管理员')
  })
})
