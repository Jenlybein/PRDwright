/**
 * 管理端会话：我是不是管理员。
 *
 * **三个阶段而不是一个布尔值**，是因为「还没问过后端」与「问过了、不是管理员」
 * 必须分开：首屏若把「未知」当成「不是管理员」，用户会在刷新时被误踢到登录页，
 * 而这只是因为探测请求还没回来。
 *
 * - `unknown`：还没问过后端（首屏状态，**不能猜**）
 * - `anonymous`：问过了，后端说这张 Cookie 在这里不算数（未登录 / 不是管理员）
 * - `admin`：已登录且是管理员
 *
 * ⚠️ 没有「已登录但不是管理员」这一档（ADR-049）。后台**只承认管理员账号存在**：
 * 非管理员走到哪都表现为「账号不存在」，因此对他而言与「没登录」是同一页
 * （登录页），只是上面多一句后端给的话。原先那页「你登录了，但你不是管理员」
 * 既确认了账号存在，又给不出任何可行动的办法。
 *
 * ⚠️ 阶段只能由**后端**决定。前端这里做的判断只是「让人少点一次」，不是安全边界——
 * 任何人都能跳过页面直接敲接口，真正的拦截在服务端 `require_admin` 与
 * `POST /api/admin/login`。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import {
  login as loginRequest,
  logout as logoutRequest,
  probeAdminSession,
} from '@/services/adminService'
import type { LoginPayload } from '@/services/adminService'
import { isApiError } from '@/services/errors'

export type SessionPhase = 'unknown' | 'anonymous' | 'admin'

export const useSessionStore = defineStore('admin-session', () => {
  const phase = ref<SessionPhase>('unknown')
  const username = ref('')
  /** 当前登录者的 id。用户列表靠它标出「本人」，避免管理员误操作自己的账号 */
  const userId = ref(0)
  /**
   * 登录页上要显示的一句话：探测失败的原因，或「账号不存在」。
   * 为空表示一切正常（正常的未登录不算错误，不该在登录页上喊话）。
   */
  const notice = ref('')

  // 单飞句柄：路由守卫在首屏可能被并发触发多次，不合并就会连发好几次探测请求，
  // 而它们的答案完全一样。
  let inflight: Promise<void> | null = null

  const isAdmin = computed(() => phase.value === 'admin')

  /** 把失败归成一句能给用户看的话 */
  function describeFailure(caught: unknown): string {
    return isApiError(caught) ? caught.message : '无法确定登录状态，请稍后重试'
  }

  /** 退到「在这里没有身份」的状态，并清掉上一次的身份残留 */
  function reset(nextNotice = ''): void {
    phase.value = 'anonymous'
    username.value = ''
    userId.value = 0
    notice.value = nextNotice
  }

  /**
   * 问后端一件事：我是不是管理员。
   *
   * 只问 `/api/admin/me` 就够了——它一个接口同时回答「登录了没有」（401）与
   * 「是不是管理员」（403）。从前先问 `/api/auth/me` 再问它，等于为了让界面知道
   * 用户名，先把「这个账号确实存在」这句话写进了后台自己的状态里。
   */
  async function refresh(): Promise<void> {
    try {
      const probe = await probeAdminSession()
      if (probe.kind === 'admin') {
        phase.value = 'admin'
        username.value = probe.me.username
        userId.value = probe.me.id
        notice.value = ''
        return
      }
      // 非管理员用后端给的那句话（「账号不存在」），前端不另编一句
      reset(probe.kind === 'not_admin' ? probe.message : '')
    } catch (caught) {
      // 探测失败（服务不可用等）时**不假装成未登录**：那会让用户看到「请登录」，
      // 而真正的原因是后端连不上。降级为 anonymous 并保留错误说明，
      // 登录页会把这句话显示出来。
      reset(describeFailure(caught))
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
   * **登录成功就等于拿到了管理员身份**——身份判定已经在服务端做完了：
   * 非管理员会在 `/api/admin/login` 被 401 挡下（文案「账号不存在」），
   * 根本走不到这里。前端不再补一次「登录后再问一遍我是不是管理员」，
   * 那一步在旧的复用式登录下是必须的，现在只会多一次往返。
   */
  async function login(payload: LoginPayload): Promise<void> {
    const user = await loginRequest(payload)
    phase.value = 'admin'
    username.value = user.username
    userId.value = user.id
    notice.value = ''
  }

  /** 登出：清服务端会话，并把阶段退回未登录 */
  async function logout(): Promise<void> {
    await logoutRequest()
    reset()
  }

  return {
    phase,
    username,
    userId,
    notice,
    isAdmin,
    ensureLoaded,
    refresh,
    login,
    logout,
  }
})
