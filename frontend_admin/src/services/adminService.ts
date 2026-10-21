/**
 * 管理端用到的后端接口。
 *
 * 登录态完全由后端的 HttpOnly Cookie 承载，前端**不保存任何 token**——
 * 也因此拿不到「用户是谁、是不是管理员」这类信息，只能去问后端。
 *
 * ⚠️ 关于「怎么知道自己是管理员」：这里刻意**没有**一个「返回 is_admin 布尔值」
 * 的公开接口。想知道答案，就去敲管理员接口，让服务端用**状态码**回答
 * （200 是、403 不是、401 没登录）。这样「我是不是管理员」不会变成任何人都能
 * 探测的问题；而若改成返回一个布尔字段，等于把权限信息挂在了公开读接口上。
 */

import { http } from '@/services/api'
import { HTTP_FORBIDDEN, HTTP_UNAUTHORIZED, isStatus } from '@/services/errors'

/** 登录用户（`/api/auth/me` 的形状） */
export interface AuthUser {
  id: number
  username: string
}

/** 管理员身份自检（`/api/admin/me` 的形状） */
export interface AdminMe {
  id: number
  username: string
  is_admin: boolean
}

export interface LoginPayload {
  username: string
  password: string
}

/** 把后端响应收敛成已知形状；字段缺失时给安全默认值而不是崩溃 */
function toUser(data: unknown): AuthUser {
  const raw = (data ?? {}) as Partial<AuthUser>
  return {
    id: typeof raw.id === 'number' ? raw.id : 0,
    username: typeof raw.username === 'string' ? raw.username : '',
  }
}

/**
 * 登录。成功时后端下发会话 Cookie，浏览器自动保存。
 * 对应 `POST /api/auth/login`。
 */
export async function login(payload: LoginPayload): Promise<AuthUser> {
  const { data } = await http.post<AuthUser>('/api/auth/login', {
    username: payload.username,
    password: payload.password,
  })
  return toUser(data)
}

/** 登出：后端吊销服务端会话并清除 Cookie。对应 `POST /api/auth/logout`。 */
export async function logout(): Promise<void> {
  await http.post('/api/auth/logout')
}

/**
 * 查询当前登录用户。对应 `GET /api/auth/me`。
 *
 * 401 是**正常结果**（表示未登录），这里返回 `null` 而不是抛异常；
 * 其它错误（服务 500、网络不通）才抛出，让调用方能区分「没登录」与「服务挂了」。
 */
export async function fetchCurrentUser(): Promise<AuthUser | null> {
  try {
    const { data } = await http.get<AuthUser>('/api/auth/me')
    return toUser(data)
  } catch (error) {
    if (isStatus(error, HTTP_UNAUTHORIZED)) return null
    throw error
  }
}

/**
 * 管理员身份自检。对应 `GET /api/admin/me`。
 *
 * - 200 → 是管理员，返回身份；
 * - 403 → **已登录但不是管理员**，返回 `null`（不是异常，是正常业务结果）；
 * - 401 → 未登录，同样返回 `null`。
 *
 * 只把 401/403 收敛成 `null`：其余错误照抛，避免把「服务不可用」
 * 误判成「你不是管理员」——那会让用户在后端故障时看到一个完全错误的原因。
 */
export async function fetchAdminMe(): Promise<AdminMe | null> {
  try {
    const { data } = await http.get<AdminMe>('/api/admin/me')
    return data
  } catch (error) {
    if (isStatus(error, HTTP_UNAUTHORIZED) || isStatus(error, HTTP_FORBIDDEN)) return null
    throw error
  }
}
