/**
 * 失败退避的界面（票 #88）：等待重试要看得见、看得懂、退得掉。
 *
 * 三条硬要求各对应一组用例：
 *
 * 1. **排队中显示等待原因**（「等待重试，约 xx 秒后」），不是失败、也不无限转圈；
 * 2. **倒计时不跳成负数、不闪烁**（`ceil` + clamp，到点换成「即将重试」）；
 * 3. **退避期间取消仍然可用**（等待面板上有「停止生成」，点它抛 `cancel`）。
 *
 * 时间换算被抽成纯函数（`@/utils/backoff`），所以第一组用例不需要假时钟；
 * 组件的每秒推进用 `vi.useFakeTimers()` + `setSystemTime` 演示，真的等 30 秒是不必要的。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

import GeneratingStage from '@/components/stages/GeneratingStage.vue'
import { isWaitingRetry, parseRetryAt, retryNoticeText, retryRemainingMs } from '@/utils/backoff'

/** 后端 `next_run_at` 的真实格式：秒级 UTC、**不带时区后缀** */
function isoNaiveUtc(msFromNow: number, nowMs: number): string {
  return new Date(nowMs + msFromNow).toISOString().replace('Z', '').replace(/\.\d+$/, '')
}

describe('backoff 工具：把 next_run_at 折成倒计时', () => {
  it('空值 / 坏值读成「没在等待」，不打断界面的其它字段', () => {
    expect(parseRetryAt(null)).toBeNull()
    expect(parseRetryAt(undefined)).toBeNull()
    expect(parseRetryAt('')).toBeNull()
    expect(parseRetryAt('   ')).toBeNull()
    expect(parseRetryAt('not-a-date')).toBeNull()
    expect(isWaitingRetry(null, 1_000)).toBe(false)
  })

  it('不带时区后缀按 UTC 解释（不能按本地时区，否则东八区整体偏 8 小时）', () => {
    // 正是后端存的形状：`2026-11-17T08:00:00`（UTC 八点）
    expect(parseRetryAt('2026-11-17T08:00:00')).toBe(Date.UTC(2026, 10, 17, 8, 0, 0))
    // 带后缀的写法仍然照办（将来后端若改成带 Z，这里不需要改）
    expect(parseRetryAt('2026-11-17T08:00:00Z')).toBe(Date.UTC(2026, 10, 17, 8, 0, 0))
  })

  it('剩余时间不会变成负数：到点之后是 0，而不是 -1', () => {
    const at = Date.UTC(2030, 0, 1, 0, 0, 30)
    expect(retryRemainingMs('2030-01-01T00:00:30', at - 30_000)).toBe(30_000)
    expect(retryRemainingMs('2030-01-01T00:00:30', at)).toBe(0)
    // 服务器与本地时钟有毫秒级抖动，过期一点点也必须落在 0
    expect(retryRemainingMs('2030-01-01T00:00:30', at + 5_000)).toBe(0)
  })

  it('文案向上取整：30 秒不会先闪成 29 秒', () => {
    expect(retryNoticeText(30_000)).toBe('等待重试，约 30 秒后')
    expect(retryNoticeText(29_500)).toBe('等待重试，约 30 秒后')
    expect(retryNoticeText(1_200)).toBe('等待重试，约 2 秒后')
    expect(retryNoticeText(0)).toBe('等待重试，即将重试')
    // 负数（时钟回拨 / 过期）同样只说「即将重试」，永远不显示「约 -1 秒后」
    expect(retryNoticeText(-5_000)).toBe('等待重试，即将重试')
  })
})

describe('GeneratingStage：等待重试的样子', () => {
  const NOW = Date.UTC(2030, 0, 1, 0, 0, 0)

  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW)
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  function mountStage(props: Record<string, unknown> = {}) {
    return mount(GeneratingStage, {
      props: {
        title: '产品需求文档',
        streamingContent: '',
        generating: true,
        statusLabel: '生成中',
        ...props,
      },
    })
  }

  it('显示「等待重试，约 xx 秒后」，并按秒往下走', async () => {
    const wrapper = mountStage({ retryAt: isoNaiveUtc(30_000, NOW) })
    expect(wrapper.text()).toContain('等待重试，约 30 秒后')

    vi.advanceTimersByTime(1_000)
    await nextTick()
    expect(wrapper.text()).toContain('等待重试，约 29 秒后')

    vi.advanceTimersByTime(1_000)
    await nextTick()
    expect(wrapper.text()).toContain('等待重试，约 28 秒后')
  })

  it('到点那一刻只说「即将重试」：不跳成负数、也不再倒数', async () => {
    const wrapper = mountStage({ retryAt: isoNaiveUtc(2_000, NOW) })

    vi.advanceTimersByTime(2_000)
    await nextTick()
    expect(wrapper.text()).toContain('等待重试，即将重试')
    expect(wrapper.text()).not.toContain('-')

    // 再往后走（真实服务器时间可能比本地慢一丁点）：文案必须稳定，不来回跳
    vi.advanceTimersByTime(3_000)
    await nextTick()
    expect(wrapper.text()).toContain('等待重试，即将重试')
  })

  it('等待不是失败：不出错误提示、不显示「已用时」、也不转圈', async () => {
    const wrapper = mountStage({
      retryAt: isoNaiveUtc(30_000, NOW),
      elapsedMs: 65_000,
      // 若等待态误渲染 DocumentReview（它带转圈动画），这个徽标就会出现在文本里
      statusLabel: '生成中',
    })

    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('生成失败')
    // 等待期间没有执行者：不该出现「已用时」，也不该出现生成中的转圈徽标
    expect(wrapper.text()).not.toContain('已用时')
    expect(wrapper.text()).not.toContain('生成中')
    // 说明它为什么在这里等（不是卡死、不是失败）
    expect(wrapper.text()).toContain('按退避等待')
  })

  it('退避期间取消仍然可用：等待面板上有「停止生成」并抛出 cancel', async () => {
    const wrapper = mountStage({ retryAt: isoNaiveUtc(30_000, NOW) })
    const cancel = wrapper.findAll('button').find((button) => button.text() === '停止生成')
    expect(cancel).toBeDefined()

    await cancel?.trigger('click')
    await nextTick()

    expect(wrapper.emitted('cancel')).toBeTruthy()
  })

  it('没有 next_run_at 时行为不变：常规生成视图照旧', () => {
    const wrapper = mountStage({ retryAt: null, elapsedMs: 65_000 })

    expect(wrapper.text()).not.toContain('等待重试')
    expect(wrapper.text()).not.toContain('停止生成')
    expect(wrapper.text()).toContain('已用时 1 分 5 秒')
    expect(wrapper.text()).toContain('生成中')
  })
})
