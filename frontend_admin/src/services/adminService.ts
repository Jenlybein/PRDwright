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
import { ApiError, HTTP_FORBIDDEN, HTTP_UNAUTHORIZED, isStatus } from '@/services/errors'

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

// ---------------------------------------------------------------------------
// 后台管理对象
// ---------------------------------------------------------------------------

/** 平台总量（`GET /api/admin/stats`） */
export interface AdminStats {
  user_count: number
  admin_count: number
  task_count: number
  completed_count: number
  prompt_tokens: number
  completion_tokens: number
}

/** 用户列表的一行（`GET /api/admin/users`）：账号信息 + 用量聚合 */
export interface AdminUser {
  id: number
  username: string
  created_at: string
  is_active: boolean
  is_admin: boolean
  task_count: number
  prompt_tokens: number
  completion_tokens: number
}

/** 邀请码列表的一行（`GET /api/admin/invites`） */
export interface AdminInvite {
  code: string
  max_uses: number
  used_count: number
  expires_at: string | null
  created_at: string
  note: string | null
}

/** 一次「成功/失败」的结果：失败原因直接来自后端 `detail`，可原样展示 */
export type ActionResult = { ok: true } | { ok: false; message: string }

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

// ---------------------------------------------------------------------------
// 平台统计
// ---------------------------------------------------------------------------

/** 平台总量。对应 `GET /api/admin/stats`。 */
export async function fetchStats(): Promise<AdminStats> {
  const { data } = await http.get<AdminStats>('/api/admin/stats')
  return data
}

// ---------------------------------------------------------------------------
// 审计记录
// ---------------------------------------------------------------------------

/** 一条管理员操作记录（`GET /api/admin/audit`） */
export interface AdminAudit {
  id: number
  actor_id: number | null
  actor_name: string
  action: string
  target: string
  detail: string
  request_id: string | null
  ts: string
}

/**
 * 最近的管理员操作记录，倒序。对应 `GET /api/admin/audit`。
 *
 * `actor_name` 是**冗余保存**的：账号被删之后，这条记录仍然显示当时的操作者名字
 * ——「谁删的」往往正是最需要追的那条。
 */
export async function fetchAudit(limit = 100): Promise<AdminAudit[]> {
  const { data } = await http.get<{ items: AdminAudit[] }>('/api/admin/audit', {
    params: { limit },
  })
  return data.items ?? []
}

// ---------------------------------------------------------------------------
// 用户管理
// ---------------------------------------------------------------------------

/** 用户列表（含每个账号的任务数与 Token 合计）。对应 `GET /api/admin/users`。 */
export async function fetchUsers(): Promise<AdminUser[]> {
  const { data } = await http.get<{ users: AdminUser[] }>('/api/admin/users')
  return data.users ?? []
}

/** 新建账号。对应 `POST /api/admin/users`。 */
export async function createUser(payload: {
  username: string
  password: string
  is_admin: boolean
}): Promise<AdminUser> {
  const { data } = await http.post<AdminUser>('/api/admin/users', payload)
  return data
}

/** 启停账号。对应 `PATCH /api/admin/users/{id}/active`。 */
export async function setUserActive(userId: number, isActive: boolean): Promise<AdminUser> {
  const { data } = await http.patch<AdminUser>(`/api/admin/users/${userId}/active`, {
    is_active: isActive,
  })
  return data
}

/**
 * 授予 / 撤销管理员身份。对应 `PATCH /api/admin/users/{id}/admin`。
 *
 * 规则（不能撤销最后一位管理员）由后端把关，前端照常发请求、
 * 原样显示后端给的理由——前端自己写一套判断，规则一变就会和后端不一致。
 */
export async function setUserAdmin(userId: number, isAdmin: boolean): Promise<AdminUser> {
  const { data } = await http.patch<AdminUser>(`/api/admin/users/${userId}/admin`, {
    is_admin: isAdmin,
  })
  return data
}

/** 重置密码。对应 `POST /api/admin/users/{id}/password`。 */
export async function resetUserPassword(userId: number, password: string): Promise<void> {
  await http.post(`/api/admin/users/${userId}/password`, { password })
}

/** 删除账号。对应 `DELETE /api/admin/users/{id}`。 */
export async function deleteUser(userId: number): Promise<void> {
  await http.delete(`/api/admin/users/${userId}`)
}

// ---------------------------------------------------------------------------
// 邀请码管理
// ---------------------------------------------------------------------------

/** 邀请码列表。对应 `GET /api/admin/invites`。 */
export async function fetchInvites(): Promise<AdminInvite[]> {
  const { data } = await http.get<{ invites: AdminInvite[] }>('/api/admin/invites')
  return data.invites ?? []
}

/** 新建邀请码。对应 `POST /api/admin/invites`。 */
export async function createInvite(payload: {
  code: string
  max_uses: number
  expires_at: string | null
  note: string
}): Promise<void> {
  await http.post('/api/admin/invites', payload)
}

/** 删除邀请码。对应 `DELETE /api/admin/invites/{code}`。 */
export async function deleteInvite(code: string): Promise<void> {
  await http.delete(`/api/admin/invites/${encodeURIComponent(code)}`)
}

/**
 * 把一个可能失败的操作收敛成 `ActionResult`。
 *
 * 页面拿它决定「显示后端给的原因」还是「刷新列表」——不需要每个调用点都写一遍
 * try/catch，也避免把「规则不允许」误显示成「网络故障」。
 */
export async function toResult(task: () => Promise<unknown>): Promise<ActionResult> {
  try {
    await task()
    return { ok: true }
  } catch (error) {
    return { ok: false, message: messageOf(error) }
  }
}

/** 取一条可以直接展示给用户的错误说明 */
export function messageOf(error: unknown): string {
  if (error instanceof ApiError) return error.message
  return '操作失败，请稍后重试'
}
