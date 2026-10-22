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

/** 按天的一行用量（`GET /api/admin/stats/daily`） */
export interface AdminDailyStat {
  day: string
  task_count: number
  completed_count: number
  /** 真失败（已排除主动取消与被顶替） */
  failed_count: number
  cancelled_count: number
  running_count: number
  prompt_tokens: number
  completion_tokens: number
  /** 分母为 0 时为 null：「那天没任务」与「那天 0% 失败」是两回事 */
  failure_rate: number | null
}

/** 用量排行的一行（`GET /api/admin/stats/users`） */
export interface AdminTopUser {
  id: number
  username: string
  is_active: boolean
  task_count: number
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
  // --- 每日额度（v4.5）---
  /** 生效的每日生成次数上限；0 表示不限 */
  daily_task_limit: number
  /** 生效的每日 Token 上限；0 表示不限 */
  daily_token_limit: number
  /** 生效的同时运行任务上限；0 表示不限 */
  max_running: number
  /** 次数上限来自哪里：global 继承全局 / user 单独设置 / off 显式不限 */
  daily_task_source: QuotaSource
  daily_token_source: QuotaSource
  max_running_source: QuotaSource
  /** 今天已占用的生成次数（不含失败与取消） */
  usage_tasks_today: number
  /** 今天已消耗的 Token（含失败任务的实际消耗） */
  usage_tokens_today: number
}

/**
 * 额度来源。
 *
 * 它是前端**编辑回填**的依据：`global` 回填 -1、`off` 回填 0、`user` 回填生效值。
 * 只看生效数字无法区分「继承全局的 10」和「单独设成 10」——两者数字一样，
 * 但管理员下一次改全局时，前者会跟着变、后者不会。
 */
export type QuotaSource = 'global' | 'user' | 'off'

/** 设置用户额度的入参：-1 跟随全局，0 不限，正数为具体上限 */
export interface UserQuotaPatch {
  daily_task_limit: number
  daily_token_limit: number
  max_running: number
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

/**
 * 一次「成功/失败」的结果：失败原因直接来自后端 `detail`，可原样展示。
 *
 * 成功时带上 `value`（被调函数的返回值）：有些操作需要拿它做**回读复核**
 * ——「保存成功」与「真的生效」是两件事，只判 `ok` 会把后者漏掉。
 * 不需要返回值的调用点照常只判 `ok`，泛型默认 `void` 不影响它们。
 */
export type ActionResult<T = void> =
  | { ok: true; value: T }
  | { ok: false; message: string }

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

/**
 * 最近 `days` 天的每日用量。对应 `GET /api/admin/stats/daily`。
 *
 * ⚠️ 必须带 `tz_offset_minutes`：后端按这个偏移把 UTC 分组成**本地日期**。
 * 不传就是 UTC，东八区用户会看到「昨晚用的」出现在「今天」那一格。
 * `getTimezoneOffset()` 返回的是「本地落后 UTC 多少分钟」（东八区为 -480），
 * 所以这里取负号才是「本地相对 UTC 的偏移」。
 */
export async function fetchDailyStats(
  days = 7,
): Promise<{ items: AdminDailyStat[]; summary: AdminDailyStat | null }> {
  const { data } = await http.get<{
    items: AdminDailyStat[]
    summary: AdminDailyStat | null
    days: number
  }>('/api/admin/stats/daily', {
    params: { days, tz_offset_minutes: -new Date().getTimezoneOffset() },
  })
  return { items: data.items ?? [], summary: data.summary ?? null }
}

/** 用量排行。对应 `GET /api/admin/stats/users`。 */
export async function fetchTopUsers(limit = 10): Promise<AdminTopUser[]> {
  const { data } = await http.get<{ users: AdminTopUser[] }>('/api/admin/stats/users', {
    params: { limit },
  })
  return data.users ?? []
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

/**
 * 设置某个用户的每日额度。对应 `PATCH /api/admin/users/{id}/quota`。
 *
 * 三个值同语义：`-1` 跟随全局、`0` 不限、正数为具体上限。
 * 返回更新后的**完整用户行**，调用方可以直接替换列表里那一行，
 * 并据此复核「我提交的」与「实际生效的」是否一致。
 */
export async function setUserQuota(
  userId: number,
  patch: UserQuotaPatch,
): Promise<AdminUser> {
  const { data } = await http.patch<AdminUser>(`/api/admin/users/${userId}/quota`, patch)
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

/**
 * 强制下线：吊销该用户的全部会话。对应 `POST /api/admin/users/{id}/sessions/revoke`。
 *
 * 返回实际吊销条数而不是空——单点登录下每人至多一条，`revoked=0` 表示「当时并未登录」，
 * 界面要能区分这两种情况，否则管理员会以为点了没反应。
 */
export async function revokeUserSessions(userId: number): Promise<number> {
  const { data } = await http.post<{ revoked: number }>(
    `/api/admin/users/${userId}/sessions/revoke`,
  )
  return data.revoked
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

// ---------------------------------------------------------------------------
// 日志检索
// ---------------------------------------------------------------------------

/** 一条应用日志（`GET /api/admin/logs` 的一行） */
export interface AdminLog {
  id: number
  ts: string
  level: string
  logger: string
  msg: string
  request_id: string | null
  user_id: number | null
  event: string | null
  /** 业务字段（job_id / 耗时 / token 数…）已由后端解析成对象 */
  extra: Record<string, unknown>
  /** 异常堆栈，仅错误类日志有 */
  exc: string | null
}

/** 日志检索结果（一页） */
export interface AdminLogPage {
  items: AdminLog[]
  /** 命中总数（不是本页条数）：分页器靠它算页数 */
  total: number
  page: number
  page_size: number
}

/** 日志检索条件。空串一律视为「不筛」，交给后端判断。 */
export interface LogQuery {
  level?: string
  since?: string
  until?: string
  logger_name?: string
  event?: string
  request_id?: string
  user_id?: number
  keyword?: string
  /** 排除某个事件名。默认排掉访问日志 `request.completed`——它每个请求一条，
   *  量级会压过业务事件，把「刚才那次生成怎么了」埋掉。 */
  exclude_event?: string
  page?: number
  page_size?: number
}

/**
 * 检索日志。对应 `GET /api/admin/logs`。
 *
 * 过滤条件全部走**查询参数**而不是请求体：这是只读接口，且最常见的用法是
 * 把当前筛选的链接发给同事——进 URL 才能分享。空值一律不发，
 * 免得后端收到 `level=` 这种「筛了个空串」的语义。
 */
export async function fetchLogs(query: LogQuery = {}): Promise<AdminLogPage> {
  const params: Record<string, string | number> = {}
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === '') continue
    params[key] = value as string | number
  }
  const { data } = await http.get<AdminLogPage>('/api/admin/logs', { params })
  return {
    items: data.items ?? [],
    total: data.total ?? 0,
    page: data.page ?? 1,
    page_size: data.page_size ?? 100,
  }
}

// ---------------------------------------------------------------------------
// 任务监控（v4.5 T5-1）
// ---------------------------------------------------------------------------

/** 任务监控列表的一行（`GET /api/admin/jobs` 的一行） */
export interface AdminJob {
  id: number
  owner_id: number
  owner_name: string
  kind: string
  status: 'running' | 'completed' | 'failed' | string
  phase: string
  created_at: string
  updated_at: string
  /** 已运行秒数；running 为「至今」，其余为收尾耗时 */
  duration_seconds: number
  /** 是否判定为卡死（running 且超过阈值未更新） */
  is_stuck: boolean
  /** 已停更分钟数；仅 is_stuck 时有意义 */
  stuck_minutes: number
  prompt_tokens: number
  completion_tokens: number
  error_code: string
  error_message: string
}

/** 任务监控列表的一页 */
export interface AdminJobPage {
  items: AdminJob[]
  /** 同口径命中总数，不只是本页条数 */
  total: number
}

/** 任务列表的筛选条件。空串一律视为「不筛」。 */
export interface JobQuery {
  /** running / failed；空串 = 全部 */
  status?: string
  /** 用户名模糊匹配 */
  username?: string
  /** 任务类型 */
  kind?: string
  limit?: number
  offset?: number
}

/** 任务列表。对应 `GET /api/admin/jobs`。 */
export async function fetchJobs(query: JobQuery = {}): Promise<AdminJobPage> {
  const params: Record<string, string | number> = {}
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === '') continue
    params[key] = value as string | number
  }
  const { data } = await http.get<AdminJobPage>('/api/admin/jobs', { params })
  return { items: data.items ?? [], total: data.total ?? 0 }
}

/**
 * 取消一条任务。对应 `POST /api/admin/jobs/{id}/cancel`。
 *
 * `cancelled=false` 不是失败：它表示任务在点击前已经自然结束。
 * 页面据此提示「它刚好结束了」，而不是弹一个红色错误。
 */
export async function cancelJob(jobId: number): Promise<{ cancelled: boolean }> {
  const { data } = await http.post<{ cancelled: boolean }>(`/api/admin/jobs/${jobId}/cancel`)
  return { cancelled: data.cancelled ?? false }
}

/** 批量取消所有卡死任务。对应 `POST /api/admin/jobs/cancel-stuck`。 */
export async function cancelStuckJobs(): Promise<{ cancelled: number }> {
  const { data } = await http.post<{ cancelled: number }>('/api/admin/jobs/cancel-stuck')
  return { cancelled: data.cancelled ?? 0 }
}

/**
 * 把一个可能失败的操作收敛成 `ActionResult`。
 *
 * 页面拿它决定「显示后端给的原因」还是「刷新列表」——不需要每个调用点都写一遍
 * try/catch，也避免把「规则不允许」误显示成「网络故障」。
 */
export async function toResult<T>(task: () => Promise<T>): Promise<ActionResult<T>> {
  try {
    return { ok: true, value: await task() }
  } catch (error) {
    return { ok: false, message: messageOf(error) }
  }
}

/** 取一条可以直接展示给用户的错误说明 */
export function messageOf(error: unknown): string {
  if (error instanceof ApiError) return error.message
  return '操作失败，请稍后重试'
}

// ---------------------------------------------------------------------------
// 系统配置与模型供应商（v4 T3）
// ---------------------------------------------------------------------------

/** 一个可调配置项 */
export interface ConfigItem {
  key: string
  /** 当前生效值 */
  value: unknown
  /** env / 代码默认值 */
  default: unknown
  /** `db` = 后台覆盖过；`env` = 用默认值 */
  source: string
  /** 控件类型：bool / number / enum / text */
  type: string
  label: string
  help: string
  /** 「改错了不明显但影响大」的项，前端要二次确认 */
  danger: boolean
  options: string[] | null
  min: number | null
  max: number | null
  min_exclusive: number | null
}

export interface ConfigGroup {
  key: string
  label: string
  help: string
  items: ConfigItem[]
}

/** 当前激活供应商的只读视图（不含密钥） */
export interface ActiveProviderView {
  id: string
  name: string
  protocol: string
  protocol_label: string
  base_url: string
  default_model: string
  max_tokens: number
  context_window: number
  max_output: number
  thinking_enabled: boolean
}

/** 配置页的全量数据 */
export interface SystemConfig {
  groups: ConfigGroup[]
  active_provider_id: string
  active_provider: ActiveProviderView | null
  /** db / env / degraded */
  config_source: string
  /** 配置指纹：保存后应变化，未变说明没生效 */
  revision: string
  /** 激活供应商缺密钥时的提示 */
  missing_key: string
}

/** 一家供应商（⚠️ 不含任何密钥值） */
export interface Provider {
  id: string
  name: string
  protocol: string
  protocol_label: string
  base_url: string
  api_key_env_var: string
  /** 该变量当前是否有值（只回布尔） */
  key_present: boolean
  default_model: string
  max_tokens: number
  context_window: number
  max_output: number
  thinking_enabled: boolean
  builtin: boolean
  enabled: boolean
  sort_order: number
  active: boolean
  extra: Record<string, unknown>
}

export interface ProviderPreset {
  id: string
  name: string
  protocol: string
  base_url: string
  api_key_env_var: string
  default_model: string
}

export interface ProviderList {
  providers: Provider[]
  active_provider_id: string
  presets: ProviderPreset[]
  protocols: { value: string; label: string }[]
  /** 激活成功但有风险时的提示（如缺密钥） */
  warning: string
}

export interface ProviderTestResult {
  ok: boolean
  detail: string
  latency_ms: number
}

/** 读取可调配置。对应 `GET /api/admin/config`。 */
export async function fetchConfig(): Promise<SystemConfig> {
  const { data } = await http.get<SystemConfig>('/api/admin/config')
  return data
}

/**
 * 修改配置。对应 `PATCH /api/admin/config`。
 *
 * 后端**先全量校验再写**：有一项非法则整批不落库，返回 400 带具体原因。
 */
export async function updateConfig(updates: Record<string, unknown>): Promise<SystemConfig> {
  const { data } = await http.patch<SystemConfig>('/api/admin/config', { updates })
  return data
}

/** 恢复默认（删掉覆盖项）。对应 `POST /api/admin/config/reset`。 */
export async function resetConfig(keys?: string[]): Promise<SystemConfig> {
  const { data } = await http.post<SystemConfig>('/api/admin/config/reset', {
    keys: keys ?? null,
  })
  return data
}

/** 供应商列表 + 预设 + 协议。对应 `GET /api/admin/providers`。 */
export async function fetchProviders(): Promise<ProviderList> {
  const { data } = await http.get<ProviderList>('/api/admin/providers')
  return {
    providers: data.providers ?? [],
    active_provider_id: data.active_provider_id ?? '',
    presets: data.presets ?? [],
    protocols: data.protocols ?? [],
    warning: data.warning ?? '',
  }
}

/** 新增供应商。对应 `POST /api/admin/providers`。 */
export async function createProvider(payload: {
  id: string
  name?: string
  protocol: string
  base_url?: string
  api_key_env_var?: string
  default_model?: string
  max_tokens?: number
  context_window?: number
  max_output?: number
}): Promise<ProviderList> {
  const { data } = await http.post<ProviderList>('/api/admin/providers', payload)
  return data
}

/** 编辑供应商。对应 `PATCH /api/admin/providers/{id}`。 */
export async function updateProvider(
  providerId: string,
  payload: Record<string, unknown>,
): Promise<ProviderList> {
  const { data } = await http.patch<ProviderList>(
    `/api/admin/providers/${encodeURIComponent(providerId)}`,
    payload,
  )
  return data
}

/** 删除供应商。对应 `DELETE /api/admin/providers/{id}`。 */
export async function deleteProvider(providerId: string): Promise<ProviderList> {
  const { data } = await http.delete<ProviderList>(
    `/api/admin/providers/${encodeURIComponent(providerId)}`,
  )
  return data
}

/** 激活供应商。对应 `POST /api/admin/providers/{id}/activate`。 */
export async function activateProvider(providerId: string): Promise<ProviderList> {
  const { data } = await http.post<ProviderList>(
    `/api/admin/providers/${encodeURIComponent(providerId)}/activate`,
  )
  return data
}

/**
 * 测试供应商连通性。对应 `POST /api/admin/providers/{id}/test`。
 *
 * 它会**真的发一次最小调用**（后端限制 max_tokens=16、15 秒超时），
 * 所以可能比其它接口慢，也可能因为网络问题失败——这正是它存在的意义。
 */
export async function testProvider(providerId: string): Promise<ProviderTestResult> {
  const { data } = await http.post<ProviderTestResult>(
    `/api/admin/providers/${encodeURIComponent(providerId)}/test`,
  )
  return data
}
