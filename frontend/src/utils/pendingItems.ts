import type { PendingImportance, PendingItem } from '@/types'

/**
 * 把待确认清单渲染成给模型看的一段文本。
 *
 * 只有一件事需要在前端做：**把结构化数据摊成可读的行**。清单本身（有哪些问题、涉及哪几章、
 * 谁拍板、重要性等级）全部来自后端，前端不增不减；提示词、改写规则、提问上限仍然全在后端
 * （见 ADR-010：前端只能“点名”场景，不能“递剧本”）。
 *
 * 为什么不做成 JSON 直接塞进去：模型要在对话里逐条讨论这些问题，编号 + 一行一条比一段
 * JSON 更容易被引用（「第 2 条」这种指代也才成立）。
 */

/**
 * 重要性等级的排序权重，数字越小越重要。
 *
 * 后端已按 P0 → P6 排好序再返回，这里保留一份同口径的常量，是为了让前端在
 * 渲染前再兜一次底——万一将来后端改了顺序，展示顺序不会跟着乱掉。
 */
const IMPORTANCE_ORDER: Record<PendingImportance, number> = {
  P0: 0,
  P1: 1,
  P2: 2,
  P3: 3,
  P4: 4,
  P5: 5,
  P6: 6,
}

/**
 * 把任意外部输入收敛成重要性等级。
 *
 * 这是纯防御：后端在收尾时**逐项校验**过等级（缺失或超出 P0~P6 的项已被丢弃），
 * 但前端两个入口（`api.parsePendingItems` 读快照、`documentService.toPendingItems`
 * 读接口）都要处理外部数据，各自写一遍判定就容易走偏，所以收口在这里。
 *
 * ⚠️ **不兜底**：认不出来就返回 `null`，由调用方决定怎么处置（排序时排到最后、
 * 展示时原样呈现）。替它猜一个等级，等于把一条没标级的问题伪装成已分级。
 *
 * @param raw 外部数据里的重要性字段，可能是任何类型
 * @returns `P0` ~ `P6` 之一；认不出来时为 `null`
 */
export function normalizeImportance(raw: unknown): PendingImportance | null {
  if (typeof raw === 'string') {
    const value = raw.trim().toUpperCase()
    if (value in IMPORTANCE_ORDER) {
      return value as PendingImportance
    }
  }
  return null
}

/** 某个等级的排序权重；认不出来的等级排在所有合法等级之后（而不是插到中间）。 */
function rankOf(item: PendingItem): number {
  const level = normalizeImportance(item.importance)
  return level === null ? Object.keys(IMPORTANCE_ORDER).length : IMPORTANCE_ORDER[level]
}

/**
 * 按重要性从高到低排序（P0 → P6），同等级内保持原有顺序。
 *
 * 后端提取时已经排过一次，这里做的是防御性重排；返回新数组，不改原数据。
 * 认不出来的等级不会退化成 NaN 排序，而是统一排到最后。
 *
 * @param items 后端返回的待确认清单
 * @returns 排序后的新数组
 */
export function sortPendingItems(items: PendingItem[]): PendingItem[] {
  return [...items].sort((a, b) => rankOf(a) - rankOf(b))
}

/**
 * 把待确认清单摊成给模型读的行，**每行带上重要性等级**。
 *
 * 带等级是有用途的：模型要靠它判断「先问哪条」，以及在切换等级时播报
 * 「准备开始 P0 级问题的确认」。没有等级，分级澄清就断了数据源。
 *
 * @param items 后端返回的待确认清单
 * @returns 编号 + 等级 + 问题的多行文本
 */
export function formatPendingItemsForChat(items: PendingItem[]): string {
  if (items.length === 0) return '（本文档没有待确认项）'

  return sortPendingItems(items)
    .map((item, index) => {
      const details: string[] = []
      if (item.sections.length) details.push(`涉及章节：${item.sections.join('、')}`)
      if (item.decider) details.push(`拍板人：${item.decider}`)
      const suffix = details.length ? `（${details.join('；')}）` : ''
      // 后端保证等级合法（非法的项已在收尾时丢弃），这里直接原样输出，不兜底
      return `${index + 1}. [${item.importance}] ${item.question}${suffix}`
    })
    .join('\n')
}
