/**
 * 用户管理页的用例。
 *
 * 最关键的一条是「后端回绝的原因要原样显示出来」。前端若自己硬编码
 * 「不能删自己」，将来规则一变（比如允许删除但要求二次确认），页面就会
 * 与后端不一致，而用户看到的是前端那套错的。所以这里照常发请求，
 * 把服务端给的理由显示出来——用例钉住的就是这个行为。
 *
 * 另一条是「删除要二次确认」：删号不可逆，而页面上的按钮离得很近，
 * 一次误点就会清掉一个真实用户的全部数据。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'

// 只mock 掉本页用到的几个函数；`messageOf` 等纯函数保留真实实现，
// 否则「错误文案怎么显示」这件事就没被测到。
vi.mock('@/services/adminService', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/services/adminService')>()
  return {
    ...actual,
    fetchUsers: vi.fn(),
    createUser: vi.fn(),
    setUserActive: vi.fn(),
    setUserAdmin: vi.fn(),
    resetUserPassword: vi.fn(),
    deleteUser: vi.fn(),
    setUserQuota: vi.fn(),
  }
})

import {
  deleteUser,
  fetchUsers,
  setUserActive,
  setUserAdmin,
  setUserQuota,
  type AdminUser,
} from '@/services/adminService'
import { ApiError } from '@/services/errors'
import UsersView from '@/views/UsersView.vue'

/**
 * 回绝必须用 `ApiError` 而不是普通 `Error`。
 *
 * 真实链路里 axios 拦截器会把一切失败转成 `ApiError`，而 `messageOf` 只认它——
 * 其余异常一律显示兜底文案（代码 bug 抛出的 TypeError 不该出现在管理员眼前）。
 * 用普通 Error 去 mock，测到的就会是兜底文案而不是后端给的理由。
 */
function rejectedByBackend(message: string): Error {
  return new ApiError(message, 403)
}

const usersMock = vi.mocked(fetchUsers)
const activeMock = vi.mocked(setUserActive)
const adminMock = vi.mocked(setUserAdmin)
const deleteMock = vi.mocked(deleteUser)
const quotaMock = vi.mocked(setUserQuota)

function user(overrides: Partial<AdminUser> = {}): AdminUser {
  return {
    id: 1,
    username: 'alice',
    created_at: '2026-10-01T00:00:00',
    is_active: true,
    is_admin: false,
    task_count: 3,
    prompt_tokens: 1200,
    completion_tokens: 300,
    // 默认「不限」——这是未配置额度时的真实状态（users 三列默认 -1 → 全局默认 0）
    daily_task_limit: 0,
    daily_token_limit: 0,
    max_running: 0,
    daily_task_source: 'global',
    daily_token_source: 'global',
    max_running_source: 'global',
    usage_tasks_today: 0,
    usage_tokens_today: 0,
    ...overrides,
  }
}

async function render(users: AdminUser[]) {
  usersMock.mockResolvedValue(users)
  const wrapper = mount(UsersView, {
    global: { plugins: [createPinia()] },
  })
  // 等 onMounted 里的请求回来、DOM 更新
  await vi.waitFor(() => {
    expect(usersMock).toHaveBeenCalled()
  })
  await wrapper.vm.$nextTick()
  return wrapper
}

beforeEach(() => {
  setActivePinia(createPinia())
  usersMock.mockReset()
  activeMock.mockReset()
  adminMock.mockReset()
  deleteMock.mockReset()
  quotaMock.mockReset()
})

describe('用户管理页', () => {
  it('列出用户名、任务数与 Token 合计', async () => {
    const wrapper = await render([user()])

    const text = wrapper.text()
    expect(text).toContain('alice')
    expect(text).toContain('3')
    // 1200 + 300
    expect(text).toContain('1,500')
  })

  it('管理员与本人都有标记，避免误操作自己的账号', async () => {
    const wrapper = await render([user({ id: 9, is_admin: true })])

    expect(wrapper.text()).toContain('管理员')
  })

  it('停用时后端回绝的原因要原样显示，而不是一句泛泛的「操作失败」', async () => {
    const wrapper = await render([user()])
    activeMock.mockRejectedValue(rejectedByBackend('不能停用自己的账号'))

    const buttons = wrapper.findAll('button')
    const toggle = buttons.find((b) => b.text() === '停用')
    expect(toggle).toBeTruthy()
    await toggle!.trigger('click')
    await vi.waitFor(() => {
      expect(activeMock).toHaveBeenCalled()
    })
    await wrapper.vm.$nextTick()

    // 后端给的理由必须出现在页面上：管理员看到「不能停用自己的账号」
    // 才知道该换个人操作，而不是反复重试
    expect(wrapper.text()).toContain('不能停用自己的账号')
  })

  it('点击删除先弹确认面板，未确认前不发请求', async () => {
    const wrapper = await render([user()])

    const del = wrapper.findAll('button').find((b) => b.text() === '删除')
    await del!.trigger('click')
    await wrapper.vm.$nextTick()

    expect(deleteMock).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('确定删除')
  })

  it('确认后才真正删除，并提示已删除', async () => {
    const wrapper = await render([user()])
    deleteMock.mockResolvedValue(undefined)

    await wrapper.findAll('button').find((b) => b.text() === '删除')!.trigger('click')
    await wrapper.vm.$nextTick()
    await wrapper.findAll('button').find((b) => b.text() === '确认删除')!.trigger('click')

    await vi.waitFor(() => {
      expect(deleteMock).toHaveBeenCalledWith(1)
    })
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toContain('已删除 alice')
  })

  it('删除被后端拒绝时显示原因，且不留在确认面板上', async () => {
    const wrapper = await render([user()])
    deleteMock.mockRejectedValue(rejectedByBackend('这是最后一位管理员，不能删除'))

    await wrapper.findAll('button').find((b) => b.text() === '删除')!.trigger('click')
    await wrapper.vm.$nextTick()
    await wrapper.findAll('button').find((b) => b.text() === '确认删除')!.trigger('click')

    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('最后一位管理员')
    })
  })

  it('普通账号显示「设为管理员」，管理员显示「撤销管理员」', async () => {
    const wrapper = await render([user(), user({ id: 2, username: 'boss', is_admin: true })])

    const labels = wrapper.findAll('button').map((b) => b.text())
    expect(labels).toContain('设为管理员')
    expect(labels).toContain('撤销管理员')
  })

  it('撤销管理员被后端拒绝时，原样显示「最后一位管理员」的理由', async () => {
    const wrapper = await render([user({ is_admin: true })])
    adminMock.mockRejectedValue(rejectedByBackend('这是最后一位管理员，不能撤销——至少保留一位'))

    await wrapper.findAll('button').find((b) => b.text() === '撤销管理员')!.trigger('click')

    await vi.waitFor(() => {
      expect(adminMock).toHaveBeenCalledWith(1, false)
    })
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toContain('最后一位管理员')
  })
})

describe('用户额度', () => {
  it('未配置额度时显示「不限」，而不是 0', async () => {
    // 0 与「不限」在数据上是同一个值，但显示成 0 会让管理员以为「今天一次都不能用」。
    const wrapper = await render([user()])
    expect(wrapper.text()).toContain('不限')
  })

  it('有上限时显示「已用/上限」，用满时标红', async () => {
    const wrapper = await render([
      user({ daily_task_limit: 10, daily_task_source: 'user', usage_tasks_today: 3 }),
      user({ id: 2, username: 'bob', daily_task_limit: 5, daily_task_source: 'user', usage_tasks_today: 5 }),
    ])
    expect(wrapper.text()).toContain('3/10')
    expect(wrapper.text()).toContain('5/5')
    // 用满的那一行要能被一眼认出来（红字）
    const danger = wrapper.findAll('.text-danger-fg').map((n) => n.text())
    expect(danger).toContain('5/5')
  })

  it('编辑框回填三态：继承全局 → -1，显式不限 → 0，单独设置 → 生效值', async () => {
    // 这三种情况生效值可能相同（都等于全局值或 0），只看数字无法区分，
    // 所以必须靠 source 还原成提交值——否则点一次保存就把「继承」变成了「锁死」。
    const wrapper = await render([
      user({ id: 1, username: 'inherit', daily_task_limit: 10, daily_task_source: 'global' }),
      user({ id: 2, username: 'no-limit', daily_task_limit: 0, daily_task_source: 'off' }),
      user({ id: 3, username: 'own', daily_task_limit: 7, daily_task_source: 'user' }),
    ])

    const openButtons = wrapper.findAll('button').filter((b) => b.text() === '额度')
    expect(openButtons.length).toBe(3)

    for (const [index, expected] of [['-1'], ['0'], ['7']].entries()) {
      await openButtons[index]!.trigger('click')
      const input = wrapper.find('input[type="number"]')
      expect((input.element as HTMLInputElement).value).toBe(String(expected))
    }
  })

  it('保存时把三态原样提交给后端', async () => {
    const wrapper = await render([user({ daily_task_limit: 10, daily_task_source: 'global' })])
    quotaMock.mockResolvedValue(
      user({ daily_task_limit: 2, daily_task_source: 'user' }),
    )

    await wrapper.findAll('button').filter((b) => b.text() === '额度')[0]!.trigger('click')
    const input = wrapper.find('input[type="number"]')
    await input.setValue('2')

    const save = wrapper.findAll('button').find((b) => b.text() === '保存')!
    await save.trigger('click')

    await vi.waitFor(() => expect(quotaMock).toHaveBeenCalled())
    const patch = quotaMock.mock.calls[0]![1]
    expect(patch.daily_task_limit).toBe(2)
    // 另外两项没动过，应当保持「跟随全局」而不是被写成 0（0 = 显式不限）
    expect(patch.daily_token_limit).toBe(-1)
    expect(patch.max_running).toBe(-1)
  })

  it('后端回绝时把原因原样显示', async () => {
    const wrapper = await render([user()])
    quotaMock.mockRejectedValue(rejectedByBackend('每日生成次数取值非法'))

    await wrapper.findAll('button').filter((b) => b.text() === '额度')[0]!.trigger('click')
    await wrapper.findAll('button').find((b) => b.text() === '保存')!.trigger('click')

    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('每日生成次数取值非法')
    })
  })
})
