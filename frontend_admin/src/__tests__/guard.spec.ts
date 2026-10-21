/**
 * 管理端门禁用例。
 *
 * 这里验的是**三种身份对应三个去处**：未登录→登录页、已登录非管理员→无权限页、
 * 管理员→放行。只测其中一条是不够的——「普通用户被放进后台」与
 * 「普通用户被反复踢去登录」是两种同样糟糕、且成因只差一个判断的失败。
 *
 * 注意：这些用例验的是**前端体验**，不是安全边界。真正的拦截在后端
 * `require_admin`，后端已有对应用例（`tests/test_admin_access.py`）。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { ApiError } from '@/services/errors'

vi.mock('@/services/adminService', () => ({
  fetchCurrentUser: vi.fn(),
  fetchAdminMe: vi.fn(),
  login: vi.fn(),
  logout: vi.fn(),
}))

import { fetchAdminMe, fetchCurrentUser } from '@/services/adminService'
import { router } from '@/router'
import { useSessionStore } from '@/stores/session'

const currentUser = vi.mocked(fetchCurrentUser)
const adminMe = vi.mocked(fetchAdminMe)

beforeEach(async () => {
  // 每个用例一个全新 store：会话阶段存在 store 里，不重置就会「上一个用例的
  // 登录态」渗进下一个用例，而那正是这类用例最容易自欺欺人的地方。
  setActivePinia(createPinia())
  currentUser.mockReset()
  adminMe.mockReset()
  // 先落到不需要守卫的登录页，避免「上一个用例已停在 /dashboard」导致
  // 下一次 push 相同路径被去重、守卫根本不跑。
  await router.replace('/login')
})

describe('管理端路由守卫', () => {
  it('未登录访问后台页面时跳登录页，并记下原本要去哪', async () => {
    currentUser.mockResolvedValue(null)

    await router.push('/dashboard')

    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBe('/dashboard')
  })

  it('已登录但不是管理员时停在无权限页，而不是被踢回登录页', async () => {
    currentUser.mockResolvedValue({ id: 7, username: 'bob' })
    // 后端的 403 已在 service 层收敛成 null
    adminMe.mockResolvedValue(null)

    await router.push('/dashboard')

    expect(router.currentRoute.value.name).toBe('forbidden')
  })

  it('管理员放行进入后台页面', async () => {
    currentUser.mockResolvedValue({ id: 1, username: 'root' })
    adminMe.mockResolvedValue({ id: 1, username: 'root', is_admin: true })

    await router.push('/dashboard')

    expect(router.currentRoute.value.name).toBe('dashboard')
  })

  it('探测失败时降级为未登录，并留下可读的原因（不假装成「你不是管理员」）', async () => {
    currentUser.mockRejectedValue(new ApiError('无法连接到服务，请检查网络或后端是否已启动', 0))

    await router.push('/dashboard')

    expect(router.currentRoute.value.name).toBe('login')
    const session = useSessionStore()
    expect(session.phase).toBe('anonymous')
    expect(session.error).toContain('无法连接到服务')
  })

  it('登录页不需要管理员身份，未登录也能进', async () => {
    currentUser.mockResolvedValue(null)

    await router.push('/login')

    expect(router.currentRoute.value.name).toBe('login')
    // 不该因为「未登录」就去问管理员接口
    expect(adminMe).not.toHaveBeenCalled()
  })
})
