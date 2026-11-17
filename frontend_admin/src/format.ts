/**
 * 管理端共用的展示格式化。
 *
 * 为什么单开一个文件：同一份「秒数」会在两个页面出现——概览页的「平均耗时」与
 * 任务列表的「耗时」。各写一遍格式化，迟早一处显示 `150s`、另一处显示 `2m 30s`，
 * 而管理员会以为它们量的不是同一件事。
 */

/**
 * 秒数 → 人读的时长（`30s` / `2m 30s` / `1h 20m`）。
 *
 * 刻意**不显示秒的小数**：任务耗时是给人判断「慢不慢」的，小数位只会挤占列宽。
 */
export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`
  const hours = Math.floor(minutes / 60)
  return `${hours}h ${minutes % 60}m`
}
