/**
 * 失败退避的界面换算（票 #88）。
 *
 * 后端把「这条任务最早什么时候能被下一个执行者领走」随快照下发：
 * `next_run_at`（ISO 8601 文本，**秒级、UTC、不带时区后缀**，与库里所有时间列同格式）。
 * 等待态的任务**没有运行时**，所以除了这一列，界面没有任何别的「还要等多久」的来源。
 *
 * ## 为什么单独一个纯函数模块
 *
 * 三件容易写错、又都只该写一遍的事：
 *
 * 1. **时区**：后端存的是不带 `Z` 的 UTC 文本。`new Date('2026-01-01T00:00:00')` 在
 *    浏览器里按**本地时区**解析，东八区会整体偏 8 小时——表现是倒计时一进页面就是
 *    「即将重试」（或者多等 8 小时）。所以这里显式补 `Z` 再解析。
 * 2. **不跳成负数**：面板每秒跳一格，服务器时间与本地时钟总有毫秒级抖动；不做 clamp
 *    就会出现「约 -1 秒后」这种负值。到点之后统一显示「即将重试」，不再倒数。
 * 3. **不闪烁**：秒数用 `ceil` 而不是 `floor`——从 30 开始不会先秒跳到 29、
 *    也不会在最后一秒反复横跳（`ceil(0.4)=1` 稳定停在 1，`0` 才换成「即将重试」）。
 *
 * 这些是**纯函数**（时间以参数传入，不读 `Date.now()`），所以用例不需要假时钟也能
 * 钉住边界。
 */

/** 后端 `next_run_at` 的 ISO 文本里有没有时区标记（`Z` 或 `+hh:mm`） */
const HAS_TIMEZONE = /(?:Z|[+-]\d{2}:?\d{2})$/

/**
 * 解析 `next_run_at`（必要时按 UTC 解释），返回毫秒时间戳。
 *
 * 空串 / 缺失 / 坏格式一律返回 `null` = 「这条任务没有等待信息」——与解析其它后端
 * 字段同一口径：一个字段坏掉只让那一个字段退化，不让整个等待态判断失效。
 */
export function parseRetryAt(value: unknown): number | null {
  if (typeof value !== 'string') return null
  const text = value.trim()
  if (text === '') return null
  const iso = HAS_TIMEZONE.test(text) ? text : `${text}Z`
  const parsed = Date.parse(iso)
  return Number.isNaN(parsed) ? null : parsed
}

/**
 * 距离「可以再重试」还剩多少毫秒（`0` = 已到点 / 已过期）。
 *
 * 返回 `null` 表示**不在等待态**（没有 `next_run_at` 或解析不了）：调用方据此决定
 * 显示普通生成视图还是等待视图。
 */
export function retryRemainingMs(retryAt: unknown, nowMs: number): number | null {
  const at = parseRetryAt(retryAt)
  if (at === null) return null
  return Math.max(0, at - nowMs)
}

/** 倒计时文案：等待态只有这两种说法（到点之后不再倒数，所以不会出现负数） */
export function retryNoticeText(remainingMs: number): string {
  if (remainingMs <= 0) return '等待重试，即将重试'
  const seconds = Math.ceil(remainingMs / 1000)
  return `等待重试，约 ${seconds} 秒后`
}

/**
 * 面板是否需要渲染「等待重试」：有等待信息、且**仍在等待**就是等待态。
 *
 * 已到点（`remainingMs === 0`）也算等待态（显示「即将重试」）：那一刻任务还没被领走，
 * 下结论说「已结束」或「失败了」都是错的——宁可多说一句「即将重试」。
 */
export function isWaitingRetry(retryAt: unknown, nowMs: number): boolean {
  return retryRemainingMs(retryAt, nowMs) !== null
}
