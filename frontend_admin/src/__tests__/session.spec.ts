/**
 * 管理端会话 store：**登录成功就等于拿到管理员身份**（ADR-049）。
 *
 * 这里钉住的核心是一条「往返次数」约定：登录成功后不再补问一次
 * `/api/admin/me`。旧的复用式登录（`/api/auth/login`）必须补问，因为那条接口
 * 谁都能进；现在身份判定在登录接口里做完了，补问只是白多一次请求——
 * 而它很容易被「看起来更保险」的理由加回来，所以用用例钉住。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { ApiError } from '@/services/errors'

vi.mock('@/services/adminService', () => ({
  probeAdminSession: vi.fn(),
  login: vi.fn(),
  logout: vi.fn(),
}))

import { login as loginRequest, logout as logoutRequest, probeAdminSession } from '@/services/adminService'
import { useSessionStore } from '@/stores/session'

const loginMock = vi.mocked(loginRequest)
const logoutMock = vi.mocked(logoutRequest)
const probe = vi.mocked(probeAdminSession)

beforeEach(() => {
  setActivePinia(createPinia())
  loginMock.mockReset()
  logoutMock.mockReset()
  probe.mockReset()
})

describe('管理端会话 store', () => {
  it('登录成功后直接进入 admin，不再补问一次身份', async () => {
    loginMock.mockResolvedValue({ id: 3, username: 'root' })

    const session = useSessionStore()
    await session.login({ username: 'root', password: 'password123' })

    expect(session.phase).toBe('admin')
    expect(session.username).toBe('root')
    expect(session.userId).toBe(3)
    expect(session.isAdmin).toBe(true)
    // 身份已由登录接口保证，再问一次等于白多一次往返
    expect(probe).not.toHaveBeenCalled()
  })

  it('非管理员登录被后端挡下时，不进入 admin、也不留下账号名', async () => {
    loginMock.mockRejectedValue(new ApiError('账号不存在', 401, 'AUTH_FAILED'))

    const session = useSessionStore()
    await expect(
      session.login({ username: 'alice', password: 'password123' }),
    ).rejects.toThrow('账号不存在')

    expect(session.isAdmin).toBe(false)
    expect(session.username).toBe('')
    expect(session.userId).toBe(0)
  })

  it('探测到非管理员时把后端文案挂到 notice 上（登录页负责显示）', async () => {
    probe.mockResolvedValue({ kind: 'not_admin', message: '账号不存在' })

    const session = useSessionStore()
    await session.refresh()

    expect(session.phase).toBe('anonymous')
    expect(session.notice).toBe('账号不存在')
  })

  it('登出后清掉身份与提示', async () => {
    logoutMock.mockResolvedValue()
    loginMock.mockResolvedValue({ id: 1, username: 'root' })

    const session = useSessionStore()
    await session.login({ username: 'root', password: 'password123' })
    await session.logout()

    expect(session.phase).toBe('anonymous')
    expect(session.username).toBe('')
    expect(session.notice).toBe('')
  })
})
