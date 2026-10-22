/**
 * 管理端门禁用例。
 *
 * 这里验的是**两种身份对应两个去处**：不是管理员（未登录 / 账号不存在）→ 登录页、
 * 管理员 → 放行。只测其中一条是不够的——「普通账号被放进后台」与
 * 「管理员被反复踢去登录」是两种同样糟糕、且成因只差一个判断的失败。
 *
 * ⚠️ 后台**没有**「无权限页」这一站（ADR-049）。非管理员看到的就是登录页 +
 * 一句「账号不存在」，所以这里断言的是「停在登录页且拿到了那句话」，
 * 而不是「去了某个 /forbidden」。
 *
 * 注意：这些用例验的是**前端体验**，不是安全边界。真正的拦截在后端
 * `require_admin` 与 `POST /api/admin/login`，后端有对应用例
 * （`tests/test_admin_access.py`、`tests/test_admin_login.py`）。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { ApiError } from '@/services/errors'

vi.mock('@/services/adminService', () => ({
  probeAdminSession: vi.fn(),
  login: vi.fn(),
  logout: vi.fn(),
}))

import { probeAdminSession } from '@/services/adminService'
import { router } from '@/router'
import { useSessionStore } from '@/stores/session'

const probe = vi.mocked(probeAdminSession)

beforeEach(async () => {
  // 每个用例一个全新 store：会话阶段存在 store 里，不重置就会「上一个用例的
  // 登录态」渗进下一个用例，而那正是这类用例最容易自欺欺人的地方。
  setActivePinia(createPinia())
  probe.mockReset()
  // 先落到不需要守卫的登录页，避免「上一个用例已停在 /dashboard」导致
  // 下一次 push 相同路径被去重、守卫根本不跑。
  await router.replace('/login')
})

describe('管理端路由守卫', () => {
  it('未登录访问后台页面时跳登录页，并记下原本要去哪', async () => {
    probe.mockResolvedValue({ kind: 'anonymous' })

    await router.push('/dashboard')

    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBe('/dashboard')
    // 正常的未登录不该在登录页上喊话
    expect(useSessionStore().notice).toBe('')
  })

  it('账号不存在时停在登录页，并把后端那句话交给登录页显示', async () => {
    probe.mockResolvedValue({ kind: 'not_admin', message: '账号不存在' })

    await router.push('/dashboard')

    expect(router.currentRoute.value.name).toBe('login')
    const session = useSessionStore()
    expect(session.phase).toBe('anonymous')
    expect(session.notice).toBe('账号不存在')
  })

  it('管理员放行进入后台页面，并拿到当前账号名', async () => {
    probe.mockResolvedValue({ kind: 'admin', me: { id: 1, username: 'root', is_admin: true } })

    await router.push('/dashboard')

    expect(router.currentRoute.value.name).toBe('dashboard')
    const session = useSessionStore()
    expect(session.phase).toBe('admin')
    expect(session.username).toBe('root')
  })

  it('探测失败时降级为未登录，并留下可读的原因（不伪造成「账号不存在」）', async () => {
    probe.mockRejectedValue(new ApiError('无法连接到服务，请检查网络或后端是否已启动', 0))

    await router.push('/dashboard')

    expect(router.currentRoute.value.name).toBe('login')
    const session = useSessionStore()
    expect(session.phase).toBe('anonymous')
    expect(session.notice).toContain('无法连接到服务')
  })

  it('登录页不需要管理员身份，未登录也能进', async () => {
    await router.push('/login')

    expect(router.currentRoute.value.name).toBe('login')
    // 不该为了看登录页就去问管理员接口
    expect(probe).not.toHaveBeenCalled()
  })
})
