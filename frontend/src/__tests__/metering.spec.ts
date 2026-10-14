import { describe, expect, it } from 'vitest'

import { formatDuration, formatTokens } from '@/utils/metering'

/**
 * 用量展示的口径。
 *
 * 生成中（`GeneratingStage`）与生成后（`ReviewStage`）展示的是同一次任务的同一组数，
 * 口径必须只有一处定义——两处各写一份，迟早出现「生成中说 12 秒、生成后说 13 秒」。
 */
describe('formatDuration', () => {
  it('不足 1 分钟按秒展示（四舍五入到秒）', () => {
    expect(formatDuration(0)).toBe('0 秒')
    expect(formatDuration(4_400)).toBe('4 秒')
    expect(formatDuration(12_300)).toBe('12 秒')
    expect(formatDuration(59_400)).toBe('59 秒')
  })

  it('超过 1 分钟用「M 分 S 秒」，整分钟时省掉秒', () => {
    expect(formatDuration(60_000)).toBe('1 分钟')
    expect(formatDuration(65_000)).toBe('1 分 5 秒')
    expect(formatDuration(125_000)).toBe('2 分 5 秒')
  })

  it('负数与非法输入按 0 处理，不出现「-1 秒」这种文案', () => {
    expect(formatDuration(-5_000)).toBe('0 秒')
  })
})

describe('formatTokens', () => {
  it('千位以下原样给出（数字不大时精度更重要）', () => {
    expect(formatTokens(0)).toBe('0')
    expect(formatTokens(999)).toBe('999')
  })

  it('千位以上折算成 k 并保留一位小数', () => {
    expect(formatTokens(1_000)).toBe('1.0k')
    expect(formatTokens(1_200)).toBe('1.2k')
    expect(formatTokens(12_500)).toBe('12.5k')
  })

  it('负数按 0 处理', () => {
    expect(formatTokens(-10)).toBe('0')
  })
})
