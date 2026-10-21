/**
 * 全局 axios 实例与「错误归一化」。
 *
 * 请求走 Vite 的 `/api` 代理转发到后端，所以这里保持同源、不写死后端地址。
 *
 * `withCredentials` 是登录态的前提：会话放在后端下发的 HttpOnly Cookie 里，
 * 同源请求默认就会带上；但一旦将来接口跨域或改成直连后端端口，少了这一项
 * Cookie 就不会被发送，表现为「明明登录了却一直 401」。
 *
 * **拦截器把所有失败统一转成 `ApiError`**：调用方只需要认这一个类，
 * 不必在每个调用点重复判断 `axios.isAxiosError`。
 */

import axios from 'axios'

import { ApiError, fallbackMessage } from '@/services/errors'

export const http = axios.create({
  baseURL: '/',
  timeout: 30_000,
  withCredentials: true,
  headers: {
    'Content-Type': 'application/json',
  },
})

/**
 * 从后端响应体里取出可以直接展示给用户的说明。
 *
 * 后端统一用 `detail` 返回中文提示（多数是字符串，422 校验失败时是数组），
 * 取不到就回退到按状态码给的兜底文案。
 */
function readDetail(data: unknown): string | null {
  if (!data || typeof data !== 'object' || !('detail' in data)) return null
  const detail = (data as { detail: unknown }).detail
  if (typeof detail === 'string' && detail.trim()) return detail
  if (Array.isArray(detail)) return '填写的内容不符合要求，请检查后重试'
  return null
}

/** 从响应体里取出机器可读的错误码（后端可能没给，取不到就留空串） */
function readCode(data: unknown): string {
  if (data && typeof data === 'object' && 'code' in data) {
    const code = (data as { code: unknown }).code
    if (typeof code === 'string') return code
  }
  return ''
}

http.interceptors.response.use(
  (response) => response,
  (error) => {
    if (!axios.isAxiosError(error)) {
      return Promise.reject(new ApiError(String(error), 0))
    }
    // 没有 response 意味着根本没拿到响应（网络不通 / 超时 / 请求被取消）
    if (!error.response) {
      return Promise.reject(new ApiError(fallbackMessage(0), 0))
    }
    const { status, data } = error.response
    return Promise.reject(
      new ApiError(readDetail(data) ?? fallbackMessage(status), status, readCode(data)),
    )
  },
)
