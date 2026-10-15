import type { PendingItem } from '@/types'

/**
 * 把待确认清单渲染成给模型看的一段文本。
 *
 * 只有一件事需要在前端做：**把结构化数据摊成可读的行**。清单本身（有哪些问题、涉及哪几章、
 * 谁拍板）全部来自后端，前端不增不减；提示词、改写规则、提问上限仍然全在后端
 * （见 ADR-010：前端只能“点名”场景，不能“递剧本”）。
 *
 * 为什么不做成 JSON 直接塞进去：模型要在对话里逐条讨论这些问题，编号 + 一行一条比一段
 * JSON 更容易被引用（「第 2 条」这种指代也才成立）。
 */
export function formatPendingItemsForChat(items: PendingItem[]): string {
  if (items.length === 0) return '（本文档没有待确认项）'

  return items
    .map((item, index) => {
      const details: string[] = []
      if (item.sections.length) details.push(`涉及章节：${item.sections.join('、')}`)
      if (item.decider) details.push(`拍板人：${item.decider}`)
      const suffix = details.length ? `（${details.join('；')}）` : ''
      return `${index + 1}. ${item.question}${suffix}`
    })
    .join('\n')
}
