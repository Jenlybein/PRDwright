/**
 * 生成用量（耗时 / token）的展示格式化。
 *
 * 抽成纯函数而不是写在组件里，是为了让「12 秒」「1.2k tokens」这类口径只有一个定义——
 * 生成中（`GeneratingStage`）与生成后（`ReviewStage`）展示的是同一次任务的同一组数，
 * 两处各写一份迟早会出现「生成中说 12 秒、生成后说 13 秒」这种自相矛盾。
 *
 * 注意这里**只做格式化，不做单位换算以外的事**：耗时与 token 都以毫秒 / 个数传入。
 */

/**
 * 把毫秒格式化成中文可读的耗时。
 *
 * 1 分钟以内用「N 秒」；超过则用「M 分 S 秒」，整分钟时省略秒。
 */
export function formatDuration(ms: number): string {
  const totalSeconds = Math.max(0, Math.round(ms / 1000))
  if (totalSeconds < 60) return `${totalSeconds} 秒`

  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return seconds === 0 ? `${minutes} 分钟` : `${minutes} 分 ${seconds} 秒`
}

/**
 * 把 token 数格式化成紧凑展示。
 *
 * 千位以下原样给出（数字不大时精度比紧凑更重要）；千位以上折算成 `k` 并保留一位小数。
 */
export function formatTokens(count: number): string {
  const value = Math.max(0, Math.round(count))
  if (value < 1000) return String(value)
  return `${(value / 1000).toFixed(1)}k`
}
