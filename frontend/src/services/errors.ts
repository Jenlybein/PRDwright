/**
 * 前后端错误契约：机器可读的错误码。
 *
 * **为什么单独成一个模块，而不是放在 `services/api.ts` 里**：
 * `AppV2.spec.ts` 用 `vi.mock('../services/api', ...)` **整体替换** api 模块，
 * 而 `useClarificationChat` 又是经 `@/services/api` 间接依赖它的。若 `ApiError`
 * 定义在 api.ts 里，mock 之后模块里根本没有这个导出，`error instanceof ApiError`
 * 会因为右值是 `undefined` 而直接抛 `TypeError`——测试失败的原因和被测行为
 * 毫无关系，极难定位。契约与传输层分开，mock 谁都不会打断 `instanceof`。
 *
 * 取值必须与 `backend/src/backend/core/errors.py` 的 `ErrorCode` **严格一致**：
 * 这份常量表是两侧唯一的耦合点，后端改名要同步改这里。
 */

/** 后端承诺的错误码 */
export const BACKEND_ERROR_CODES = {
  /** 会话在服务内存里已不存在（SSE error 事件，HTTP 200 流内） */
  SESSION_NOT_FOUND: 'SESSION_NOT_FOUND',
  /** 未登录或登录态过期（HTTP 401） */
  UNAUTHORIZED: 'UNAUTHORIZED',
  /** 注册 / 登录失败的全部分支（HTTP 400 / 401） */
  AUTH_FAILED: 'AUTH_FAILED',
  /** 方案不存在或不属于当前用户（HTTP 404） */
  SOLUTION_NOT_FOUND: 'SOLUTION_NOT_FOUND',
  /** 请求语义不合法，但字段格式没问题（HTTP 400） */
  INVALID_REQUEST: 'INVALID_REQUEST',
  /** 字段校验失败（HTTP 422，`detail` 是列表） */
  VALIDATION_ERROR: 'VALIDATION_ERROR',
  /** 上游大模型调用失败（HTTP 502 / SSE error） */
  LLM_UPSTREAM_ERROR: 'LLM_UPSTREAM_ERROR',
  /** 其余服务端故障；同时是遇到未知码时的降级值（HTTP 500 / SSE error） */
  INTERNAL_ERROR: 'INTERNAL_ERROR',
} as const

/** 后端错误码的联合类型 */
export type BackendErrorCode = (typeof BACKEND_ERROR_CODES)[keyof typeof BACKEND_ERROR_CODES]

/** 已知错误码集合，用于把流里的任意字符串收敛成合法取值 */
const KNOWN_CODES: ReadonlySet<string> = new Set(Object.values(BACKEND_ERROR_CODES))

/**
 * 携带后端错误码的异常。
 *
 * 目前只由 SSE 流内的 `error` 事件构造：那条路径上 HTTP 状态码是 200，
 * 调用方只能靠 `code` 区分「该不该换个姿势重试」与「真的失败了」。
 *
 * `status` 恒为 0 只是占位——SSE 错误没有状态码可言，保留字段是为了让将来
 * 非流式的接口复用同一个类型时不必再引入第二个错误类。
 */
export class ApiError extends Error {
  /** 机器可读的错误码 */
  readonly code: BackendErrorCode
  /** HTTP 状态码；SSE 流内错误为 0（该通道上没有状态码） */
  readonly status: number

  constructor(message: string, code: BackendErrorCode, status = 0) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
  }
}

/**
 * 把后端给出的任意取值收敛成合法错误码。
 *
 * 缺失或未知（例如后端新增了码而前端还没同步）一律降级为 `INTERNAL_ERROR`：
 * 宁可让调用方走到「一般性失败」分支，也不要因为不认识的码而崩溃或漏判。
 */
export function toErrorCode(value: unknown): BackendErrorCode {
  if (typeof value === 'string' && KNOWN_CODES.has(value)) {
    return value as BackendErrorCode
  }
  return BACKEND_ERROR_CODES.INTERNAL_ERROR
}

/**
 * 判断一个异常是否为 `ApiError`，可选地同时校验错误码。
 *
 * 前端的分支判断应该只看 `code`，**不要匹配 `message` 里的中文文案**——
 * 文案是给人看的，改一个字判断就静默失效，而这类失效没有任何报错。
 */
export function isApiError(error: unknown): error is ApiError
export function isApiError(error: unknown, code: BackendErrorCode): boolean
export function isApiError(error: unknown, code?: BackendErrorCode): boolean {
  if (!(error instanceof ApiError)) return false
  return code === undefined || error.code === code
}
