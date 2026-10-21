/**
 * 与后端的错误契约。
 *
 * **单列一个模块而不是塞进 `api.ts`**：将来测试里 `vi.mock('@/services/api')`
 * 整体替换请求模块时，`instanceof ApiError` 的右值会变成 `undefined` 而直接抛
 * TypeError——测试失败的原因和被测行为毫无关系，极难定位。契约与传输分开，
 * mock 谁都不会打断 `instanceof`。
 *
 * 判断分支一律看 `status`（以及可选的 `code`），**不要匹配中文文案**：
 * 文案是给人看的，改一个字判断就静默失效，且没有任何报错。
 */

export class ApiError extends Error {
  /** HTTP 状态码；网络不通 / 超时等「没拿到响应」的情况为 0 */
  readonly status: number
  /** 后端给出的机器可读错误码；取不到时为空串 */
  readonly code: string

  constructor(message: string, status: number, code = '') {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

/** 401：未登录或登录态过期 */
export const HTTP_UNAUTHORIZED = 401
/** 403：已登录但权限不足（非管理员访问管理员接口） */
export const HTTP_FORBIDDEN = 403

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError
}

/** 判断是否为「具有某个 HTTP 状态码」的接口错误 */
export function isStatus(error: unknown, status: number): boolean {
  return isApiError(error) && error.status === status
}

/** 兜底文案：拿不到后端 `detail` 时用，避免把 `Request failed` 原样抛给用户 */
export function fallbackMessage(status: number): string {
  if (status === HTTP_UNAUTHORIZED) return '登录状态已失效，请重新登录'
  if (status === HTTP_FORBIDDEN) return '当前账号没有执行该操作的权限'
  if (status === 0) return '无法连接到服务，请检查网络或后端是否已启动'
  return '请求失败，请稍后重试'
}
