import { describe, expect, it } from 'vitest'

import type { PendingItem } from '../types'
import { formatPendingItemsForChat } from '../utils/pendingItems'

/**
 * 待确认清单 → 对话文本的摊平规则。
 *
 * 这是前端在澄清链路上唯一参与「给模型看什么」的地方，而且只做一件事：把后端算好的结构化
 * 清单摊成可读的行。问题本身、涉及章节、拍板人全部来自后端，前端不增不减。
 */
const ITEMS: PendingItem[] = [
  { id: 'p-1', question: '分页上限取多少？', sections: ['4.3'], decider: '产品负责人' },
  { id: 'p-2', question: '鉴权用哪种方式？', sections: [], decider: '' },
]

describe('formatPendingItemsForChat', () => {
  it('逐条编号，带上章节与拍板人', () => {
    const text = formatPendingItemsForChat(ITEMS)

    expect(text).toBe(
      '1. 分页上限取多少？（涉及章节：4.3；拍板人：产品负责人）\n' + '2. 鉴权用哪种方式？',
    )
  })

  it('章节或拍板人为空时不留下空括号', () => {
    const text = formatPendingItemsForChat([ITEMS[1] as PendingItem])
    expect(text).toBe('1. 鉴权用哪种方式？')
    expect(text).not.toContain('（）')
  })

  it('多个章节用顿号连起来（与界面上那一行同口径）', () => {
    const text = formatPendingItemsForChat([
      { id: 'p-1', question: '范围到哪？', sections: ['2.1', '6.1'], decider: '' },
    ])
    expect(text).toContain('涉及章节：2.1、6.1')
  })

  it('清单为空时给一句明确的说明，而不是空字符串', () => {
    // 提示词里留空会让模型以为「没有清单要参考」，而实际是「本来就没有待确认项」
    expect(formatPendingItemsForChat([])).toBe('（本文档没有待确认项）')
  })
})
