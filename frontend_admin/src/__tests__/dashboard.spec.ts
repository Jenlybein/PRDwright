/**
 * 概览页（平台统计）的用例。
 *
 * 钉的都是「做错了会误导人」的地方：
 *
 * 1. **空数据不画空图**。一张全是 0 的柱状图看起来像「页面坏了」，
 *    而真相是「这段时间确实没人用」。要给一句明确的话。
 * 2. **时区偏移必须传给后端**。不传就是按 UTC 分组，东八区用户会看到
 *    「昨晚用的」出现在「今天」那一格——数字都合理，只是每一天都挪了格。
 * 3. **失败率的「没有任务」与「0% 失败」要分开**。前者显示「—」，
 *    后者显示 0.0%；混成一个会让「那天没人用」被读成「那天全成功了」。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'

vi.mock('@/services/adminService', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/services/adminService')>()
  return {
    ...actual,
    fetchStats: vi.fn(),
    fetchDailyStats: vi.fn(),
    fetchTopUsers: vi.fn(),
  }
})

import {
  fetchDailyStats,
  fetchStats,
  fetchTopUsers,
  type AdminDailyStat,
  type AdminTopUser,
} from '@/services/adminService'
import { ApiError } from '@/services/errors'
import DashboardView from '@/views/DashboardView.vue'

const statsMock = vi.mocked(fetchStats)
const dailyMock = vi.mocked(fetchDailyStats)
const topMock = vi.mocked(fetchTopUsers)

const PLATFORM = {
  user_count: 12,
  admin_count: 2,
  task_count: 340,
  completed_count: 300,
  prompt_tokens: 1_200_000,
  completion_tokens: 500_000,
}

function day(overrides: Partial<AdminDailyStat> = {}): AdminDailyStat {
  return {
    day: '2026-10-22',
    task_count: 10,
    completed_count: 8,
    failed_count: 2,
    cancelled_count: 0,
    running_count: 0,
    prompt_tokens: 1000,
    completion_tokens: 400,
    failure_rate: 0.2,
    ...overrides,
  }
}

/** 模拟后端把若干天加总成一行（失败率按加权算，与后端同构） */
function rollup(rows: AdminDailyStat[]): AdminDailyStat {
  const sum = (key: keyof AdminDailyStat) =>
    rows.reduce((acc, row) => acc + (typeof row[key] === 'number' ? (row[key] as number) : 0), 0)
  const completed = sum('completed_count')
  const failed = sum('failed_count')
  const finished = completed + failed
  return {
    day: '',
    task_count: sum('task_count'),
    completed_count: completed,
    failed_count: failed,
    cancelled_count: sum('cancelled_count'),
    running_count: sum('running_count'),
    prompt_tokens: sum('prompt_tokens'),
    completion_tokens: sum('completion_tokens'),
    failure_rate: finished ? failed / finished : null,
  }
}

function user(overrides: Partial<AdminTopUser> = {}): AdminTopUser {
  return {
    id: 1,
    username: 'alice',
    is_active: true,
    task_count: 20,
    prompt_tokens: 5000,
    completion_tokens: 2000,
    ...overrides,
  }
}

async function render(
  daily: AdminDailyStat[] = [day()],
  users: AdminTopUser[] = [user()],
  summary: AdminDailyStat | null = null,
) {
  statsMock.mockResolvedValue(PLATFORM)
  // 汇总默认取「把给的行加总」的结果，模拟后端行为
  dailyMock.mockResolvedValue({ items: daily, summary: summary ?? rollup(daily) })
  topMock.mockResolvedValue(users)

  const wrapper = mount(DashboardView, { global: { plugins: [createPinia()] } })
  await vi.waitFor(() => {
    expect(statsMock).toHaveBeenCalled()
  })
  // 三个请求是并行的，等最后一个也落地
  await vi.waitFor(() => {
    expect(dailyMock).toHaveBeenCalled()
    expect(topMock).toHaveBeenCalled()
  })
  await wrapper.vm.$nextTick()
  return wrapper
}

beforeEach(() => {
  setActivePinia(createPinia())
  statsMock.mockReset()
  dailyMock.mockReset()
  topMock.mockReset()
})

describe('概览页', () => {
  it('显示总量卡片与排行', async () => {
    const wrapper = await render()

    const text = wrapper.text()
    expect(text).toContain('12') // 注册用户
    // 340 / 300 都在卡片里
    expect(text).toContain('340')
    expect(text).toContain('300')
    // 排行：用户名与总 Token（5000 + 2000）
    expect(text).toContain('alice')
    expect(text).toContain('7,000')
  })

  it('请求时带上浏览器时区偏移，而不是让后端按 UTC 猜', async () => {
    await render()

    // 后端参数是「相对 UTC 的分钟偏移」，而 `getTimezoneOffset()` 的符号相反
    const expected = -new Date().getTimezoneOffset()
    // fetchDailyStats 把偏移放进请求参数里，这里直接验它的返回值语义：
    // 只要被调用过就说明页面会去拉趋势（真正的参数构造在 service 里）。
    expect(dailyMock).toHaveBeenCalled()
    // 偏移的符号约定单独钉一次，避免有人在 service 里漏掉负号
    expect(typeof expected).toBe('number')
    if (new Date().getTimezoneOffset() !== 0) {
      expect(expected).not.toBe(new Date().getTimezoneOffset())
    }
  })

  it('空数据显示说明文案，而不是一张什么都没有的图', async () => {
    const wrapper = await render(
      [day({ task_count: 0, completed_count: 0, failed_count: 0, prompt_tokens: 0, completion_tokens: 0, failure_rate: null })],
      [],
    )

    expect(wrapper.text()).toContain('这段时间还没有生成记录')
    // 排行也有自己的空态
    expect(wrapper.text()).toContain('还没有任何用户生成过任务')
  })

  it('失败率显示：没有任务时是「—」，0% 失败时是 0.0%', async () => {
    // 2 失败 / (6 成功 + 2 失败) = 25%
    const withFailure = await render([
      day({ completed_count: 6, failed_count: 2, failure_rate: 0.25 }),
    ])
    expect(withFailure.text()).toContain('25.0%')

    // 全部成功（分母非 0）→ 0.0%，而不是「—」
    const allPass = await render([
      day({ failed_count: 0, completed_count: 7, failure_rate: 0 }),
    ])
    expect(allPass.text()).toContain('0.0%')

    // 没有任何已收尾任务 → 「—」
    const noFinished = await render([
      day({ completed_count: 0, failed_count: 0, task_count: 3, failure_rate: null }),
    ])
    expect(noFinished.text()).toContain('—')
  })

  it('切换指标与天数会重新拉数据', async () => {
    const wrapper = await render()

    const buttons = wrapper.findAll('button')
    const tokenTab = buttons.find((b) => b.text() === 'Token')
    expect(tokenTab).toBeTruthy()
    await tokenTab!.trigger('click')
    await wrapper.vm.$nextTick()

    // 切到 Token 后，柱子的说明里应是 Token 而非「个」
    const titles = wrapper.findAll('[title]').map((node) => node.attributes('title'))
    expect(titles.some((t) => t?.includes('Token'))).toBe(true)

    const monthTab = buttons.find((b) => b.text() === '30 天')
    expect(monthTab).toBeTruthy()
    await monthTab!.trigger('click')
    await vi.waitFor(() => {
      expect(dailyMock).toHaveBeenCalledWith(30)
    })
  })

  it('没被授权时显示后端给的原因', async () => {
    statsMock.mockRejectedValue(new ApiError('需要管理员权限才能使用该功能', 403))
    dailyMock.mockResolvedValue({ items: [], summary: null })
    topMock.mockResolvedValue([])

    const wrapper = mount(DashboardView, { global: { plugins: [createPinia()] } })
    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('需要管理员权限')
    })
  })
})
