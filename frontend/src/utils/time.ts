/**
 * 时间展示工具。
 *
 * 抽成公用模块而不是各组件各写一份：同一个「几小时前」在方案列表与版本历史里
 * 必须算出同一个结果，两处各写一份迟早会在时区补正或阈值上漂移。
 */

/**
 * 把后端返回的 UTC 时间戳转成相对时间（「刚刚 / 5 分钟前 / 3 小时前 / 10-16 09:30」）。
 *
 * ⚠️ 后端存的是**不带时区后缀**的 UTC 字符串，直接 `new Date` 会被当成本地时间，
 * 在东八区会显示成「8 小时前」这样的错值，因此这里先补上 `Z` 再解析。
 * 解析不出来（空串、脏数据）时返回空串，交给调用方决定是否渲染这一段。
 */
export function relativeTime(iso: string): string {
  if (!iso) return ''
  const parsed = new Date(iso.endsWith('Z') ? iso : `${iso}Z`)
  if (Number.isNaN(parsed.getTime())) return ''

  const diffMs = Date.now() - parsed.getTime()
  const minutes = Math.floor(diffMs / 60_000)
  if (minutes < 1) return '刚刚'
  if (minutes < 60) return `${minutes} 分钟前`

  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`

  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())} ${pad(parsed.getHours())}:${pad(parsed.getMinutes())}`
}
