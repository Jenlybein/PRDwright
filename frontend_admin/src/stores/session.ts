/**
 * 管理端会话：我是谁、我是不是管理员。
 *
 * **四个阶段而不是一个布尔值**，是因为「还没问过后端」与「问过了、不是管理员」
 * 必须分开：首屏若把「未知」当成「不是管理员」，用户会在刷新时被误踢到无权限页，
 * 而这只是因为探测请求还没回来。
 *
 * - `unknown`：还没问过后端（首屏状态，**不能猜**）
 * - `anonymous`：问过了，未登录
 * - `user`：已登录，但不是管理员
 * - `admin`：已登录且是管理员
 *
 * ⚠️ 阶段只能由**后端**决定。前端这里做的判断只是「让人少点一次」，不是安全边界——
 * 任何人都能跳过页面直接敲接口，真正的拦截在服务端 `require_admin`。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import {
  fetchAdminMe,
  fetchCurrentUser,
  login as loginRequest,
  logout as logoutRequest,
} from '@/services/adminService'
import type { LoginPayload } from '@/services/adminService'
import type { ApiError } from '@/services/errors'
import { isApiError } from '@/services/errors'

export type SessionPhase = 'unknown' | 'anonymous' | 'user' | 'admin'

export const useSessionStore = defineStore('admin-session', () => {
  const phase = ref<SessionPhase>('unknown')
  const username = ref('')
  /** 当前登录者的 id。用户列表靠它标出「本人」，避免管理员误操作自己的账号 */
  const userId = ref(0)
  /** 探测失败时的说明；为空表示一切正常（正常失败如「未登录」不算错误） */
  const error = ref('')

  // 单飞句柄：路由守卫在首屏可能被并发触发多次，不合并就会连发好几组探测请求，
  // 而它们的答案完全一样。
  let inflight: Promise<void> | null = null

  const isLoggedIn = computed(() => phase.value === 'user' || phase.value === 'admin')
  const isAdmin = computed(() => phase.value === 'admin')

  /** 把失败归成一句能给用户看的话 */
  function describeFailure(caught: unknown): string {
    return isApiError(caught) ? caught.message : '无法确定登录状态，请稍后重试'
  }

  /**
   * 问后端两件事：登录了没有、是不是管理员。
   *
   * 顺序不能反：未登录时 `/api/admin/me` 会返回 401，与「登录了但不是管理员」的
   * 403 混在一起就分不清该跳登录页还是无权限页。
   */
  async function refresh(): Promise<void> {
    try {
      const user = await fetchCurrentUser()
      if (!user) {
        phase.value = 'anonymous'
        username.value = ''
        userId.value = 0
        error.value = ''
        return
      }
      username.value = user.username
      userId.value = user.id
      const admin = await fetchAdminMe()
      phase.value = admin ? 'admin' : 'user'
      error.value = ''
    } catch (caught) {
      // 探测失败（服务不可用等）时**不假装成未登录**：那会让用户看到「请登录」，
      // 而真正的原因是后端连不上。降级为 anonymous 并保留错误说明，
      // 登录页会把这句话显示出来。
      phase.value = 'anonymous'
      username.value = ''
      userId.value = 0
      error.value = describeFailure(caught)
    }
  }

  /**
   * 保证会话已经探测过；并发调用合并成一次。
   *
   * 失败**不向上抛**：调用方（路由守卫）只需要看 `phase`，再按阶段决定去哪。
   * 抛出去反而会让守卫多出一条与「去哪个页面」无关的分支。
   */
  function ensureLoaded(): Promise<void> {
    if (!inflight) {
      inflight = refresh().finally(() => {
        inflight = null
      })
    }
    return inflight
  }

  /**
   * 登录。
   *
   * 注意：普通账号**也能登录成功**，只是进不去后台——登录成功后要再问一次
   * 「是不是管理员」，由后端决定落到 `user` 还是 `admin`。
   * 若不做这一步，普通用户登录后会直接被放进后台首页，而里面的接口全部 403。
   */
  async function login(payload: LoginPayload): Promise<void> {
    const user = await loginRequest(payload)
    username.value = user.username
    userId.value = user.id
    phase.value = 'user'
    const admin = await fetchAdminMe()
    phase.value = admin ? 'admin' : 'user'
    error.value = ''
  }

  /** 登出：清服务端会话，并把阶段退回未登录 */
  async function logout(): Promise<void> {
    await logoutRequest()
    phase.value = 'anonymous'
    username.value = ''
    userId.value = 0
    error.value = ''
  }

  return {
    phase,
    username,
    userId,
    error,
    isLoggedIn,
    isAdmin,
    ensureLoaded,
    refresh,
    login,
    logout,
  }
})
