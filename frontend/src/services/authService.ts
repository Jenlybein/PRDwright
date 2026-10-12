/**
 * 认证接口：登录 / 注册 / 登出 / 查询当前用户。
 *
 * 登录态完全由后端的 HttpOnly Cookie 承载，前端**不保存任何 token**——
 * 也因此拿不到「用户已登录」这件事，只能问后端 `/api/auth/me`。
 *
 * 这里只做请求与错误文案的提取，登录态的记忆与弹窗控制都在 `stores/auth.ts`。
 */

import axios from 'axios'

import http from '@/services/api'
import type { AuthUser, LoginPayload, RegisterPayload } from '@/types'

/**
 * 从后端错误响应里取出可以直接展示给用户的说明。
 *
 * 后端统一用 `detail` 字段返回中文提示（400 / 401 是字符串，422 是校验数组），
 * 取不到时回退到调用方给的兜底文案，避免把 `Error: Request failed` 抛给用户。
 */
function backendMessage(error: unknown, fallback: string): string {
  if (!axios.isAxiosError(error)) return fallback

  const data: unknown = error.response?.data
  if (!data || typeof data !== 'object' || !('detail' in data)) return fallback

  const detail = (data as { detail: unknown }).detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) return '填写的内容不符合要求，请检查后重试'
  return fallback
}

/** 把后端响应收敛成 `AuthUser`，缺字段时按「未知用户」处理而不是崩溃 */
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
  try {
    const { data } = await http.post<AuthUser>('/api/auth/login', payload)
    return toUser(data)
  } catch (error) {
    throw new Error(backendMessage(error, '登录失败，请稍后重试'))
  }
}

/**
 * 注册（需邀请码）。成功后后端直接签发会话，等同于自动登录。
 * 对应 `POST /api/auth/register`。
 */
export async function register(payload: RegisterPayload): Promise<AuthUser> {
  try {
    const { data } = await http.post<AuthUser>('/api/auth/register', {
      username: payload.username,
      password: payload.password,
      invite_code: payload.inviteCode,
    })
    return toUser(data)
  } catch (error) {
    throw new Error(backendMessage(error, '注册失败，请稍后重试'))
  }
}

/**
 * 登出：后端吊销服务端会话并清除 Cookie。
 * 对应 `POST /api/auth/logout`。
 */
export async function logout(): Promise<void> {
  await http.post('/api/auth/logout')
}

/**
 * 查询当前登录用户。
 * 对应 `GET /api/auth/me`。
 *
 * 401 是**正常结果**（表示未登录），这里返回 `null` 而不是抛异常；
 * 其它错误（网络不通、服务 500）才抛出，让调用方能区分「没登录」与「服务挂了」。
 */
export async function fetchCurrentUser(): Promise<AuthUser | null> {
  try {
    const { data } = await http.get<AuthUser>('/api/auth/me')
    return toUser(data)
  } catch (error) {
    if (axios.isAxiosError(error) && error.response?.status === 401) return null
    throw new Error(backendMessage(error, '无法获取登录状态'))
  }
}
