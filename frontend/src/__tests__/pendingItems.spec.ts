import { describe, expect, it } from 'vitest'

import type { PendingItem } from '../types'
import { formatPendingItemsForChat, sortPendingItems } from '../utils/pendingItems'

/**
 * 待确认清单 → 对话文本的摊平规则。
 *
 * 这是前端在澄清链路上唯一参与「给模型看什么」的地方，而且只做一件事：把后端算好的结构化
 * 清单摊成可读的行。问题本身、涉及章节、拍板人、重要性等级全部来自后端，前端不增不减。
 */
const ITEMS: PendingItem[] = [
  {
    id: 'p-1',
    question: '分页上限取多少？',
    sections: ['4.3'],
    decider: '产品负责人',
    importance: 'P1',
  },
  { id: 'p-2', question: '鉴权用哪种方式？', sections: [], decider: '', importance: 'P1' },
]

describe('formatPendingItemsForChat', () => {
  it('逐条编号，带上章节与拍板人', () => {
    const text = formatPendingItemsForChat(ITEMS)

    expect(text).toBe(
      '1. [P1] 分页上限取多少？（涉及章节：4.3；拍板人：产品负责人）\n' +
        '2. [P1] 鉴权用哪种方式？',
    )
  })

  it('章节或拍板人为空时不留下空括号', () => {
    const text = formatPendingItemsForChat([ITEMS[1] as PendingItem])
    expect(text).toBe('1. [P1] 鉴权用哪种方式？')
    expect(text).not.toContain('（）')
  })

  it('多个章节用顿号连起来（与界面上那一行同口径）', () => {
    const text = formatPendingItemsForChat([
      { id: 'p-1', question: '范围到哪？', sections: ['2.1', '6.1'], decider: '', importance: 'P1' },
    ])
    expect(text).toContain('涉及章节：2.1、6.1')
  })

  it('每行带上重要性等级：模型靠它决定先问哪一条', () => {
    const text = formatPendingItemsForChat([
      { id: 'p-1', question: '登录方式？', sections: [], decider: '', importance: 'P0' },
    ])
    expect(text).toBe('1. [P0] 登录方式？')
  })

  it('按 P0 → P6 重排后再编号，越重要的排得越靠前', () => {
    const text = formatPendingItemsForChat([
      { id: 'a', question: '最后再问的？', sections: [], decider: '', importance: 'P6' },
      { id: 'b', question: '最要紧的？', sections: [], decider: '', importance: 'P0' },
      { id: 'c', question: '中间某档？', sections: [], decider: '', importance: 'P3' },
    ])
    const lines = text.split('\n')
    expect(lines[0]).toBe('1. [P0] 最要紧的？')
    expect(lines[1]).toBe('2. [P3] 中间某档？')
    expect(lines[2]).toBe('3. [P6] 最后再问的？')
  })

  it('sortPendingItems 返回新数组，不改动传入的原数据', () => {
    const source: PendingItem[] = [
      { id: 'a', question: '次要？', sections: [], decider: '', importance: 'P2' },
      { id: 'b', question: '要紧？', sections: [], decider: '', importance: 'P0' },
    ]
    const sorted = sortPendingItems(source)

    expect(sorted.map((item) => item.importance)).toEqual(['P0', 'P2'])
    // 原数据没被就地打乱
    expect(source.map((item) => item.importance)).toEqual(['P2', 'P0'])
  })

  it('sortPendingItems 对认不出的等级不抛错，并把它排到合法等级之后', () => {
    const sorted = sortPendingItems([
      { id: 'a', question: '乱标？', sections: [], decider: '', importance: 'X' as never },
      { id: 'b', question: '正常？', sections: [], decider: '', importance: 'P6' },
    ])
    expect(sorted.map((item) => item.id)).toEqual(['b', 'a'])
  })

  it('清单为空时给一句明确的说明，而不是空字符串', () => {
    // 提示词里留空会让模型以为「没有清单要参考」，而实际是「本来就没有待确认项」
    expect(formatPendingItemsForChat([])).toBe('（本文档没有待确认项）')
  })
})
