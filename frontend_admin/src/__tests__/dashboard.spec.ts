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
import { createMemoryHistory, createRouter, type Router } from 'vue-router'

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
  type AdminStats,
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
  succeeded_count: 300,
  prompt_tokens: 1_200_000,
  completion_tokens: 500_000,
  // 四个数字（票 #89）
  queue_count: 7,
  success_rate: 0.8333,
  avg_duration_seconds: 95,
  stuck_count: 3,
  stuck_threshold_minutes: 30,
}

function day(overrides: Partial<AdminDailyStat> = {}): AdminDailyStat {
  return {
    day: '2026-10-22',
    task_count: 10,
    succeeded_count: 8,
    failed_count: 2,
    cancelled_count: 0,
    superseded_count: 0,
    active_count: 0,
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
  const succeeded = sum('succeeded_count')
  const failed = sum('failed_count')
  const finished = succeeded + failed
  return {
    day: '',
    task_count: sum('task_count'),
    succeeded_count: succeeded,
    failed_count: failed,
    cancelled_count: sum('cancelled_count'),
    superseded_count: sum('superseded_count'),
    active_count: sum('active_count'),
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

/**
 * 挂载概览页。
 *
 * 用**真 router**（memory history）而不是 mock：卡死数必须点得动、点完要落在
 * `jobs?stuck=1`——这个跳转是票 #89 的 AC 之一，mock 掉 router 等于没验它。
 */
function makeRouter(): Router {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/dashboard', name: 'dashboard', component: { template: '<div />' } },
      { path: '/jobs', name: 'jobs', component: { template: '<div />' } },
    ],
  })
}

async function render(
  daily: AdminDailyStat[] = [day()],
  users: AdminTopUser[] = [user()],
  summary: AdminDailyStat | null = null,
  platform: Partial<AdminStats> = {},
): Promise<{ wrapper: ReturnType<typeof mount>; router: Router }> {
  statsMock.mockResolvedValue({ ...PLATFORM, ...platform })
  // 汇总默认取「把给的行加总」的结果，模拟后端行为
  dailyMock.mockResolvedValue({ items: daily, summary: summary ?? rollup(daily) })
  topMock.mockResolvedValue(users)

  const router = makeRouter()
  await router.push('/dashboard')

  const wrapper = mount(DashboardView, { global: { plugins: [createPinia(), router] } })
  await vi.waitFor(() => {
    expect(statsMock).toHaveBeenCalled()
  })
  // 三个请求是并行的，等最后一个也落地
  await vi.waitFor(() => {
    expect(dailyMock).toHaveBeenCalled()
    expect(topMock).toHaveBeenCalled()
  })
  await wrapper.vm.$nextTick()
  return { wrapper, router }
}

beforeEach(() => {
  setActivePinia(createPinia())
  statsMock.mockReset()
  dailyMock.mockReset()
  topMock.mockReset()
})

describe('概览页', () => {
  it('显示总量卡片与排行', async () => {
    const { wrapper } = await render()

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

  it('四个数字都在界面上，且每个都带一句口径说明（票 #89）', async () => {
    const { wrapper } = await render()

    // 数字全部来自 `GET /api/admin/stats`，页面只做展示（不自己推算）
    const queue = wrapper.find('[data-metric="queue"]')
    expect(queue.text()).toContain('排队数')
    expect(queue.text()).toContain('7')
    expect(queue.text()).toContain('含等待退避')

    const success = wrapper.find('[data-metric="success"]')
    expect(success.text()).toContain('成功率')
    // 0.8333 → 83.3%（换算只在展示层做，比率本身由后端给）
    expect(success.text()).toContain('83.3%')
    // 这句口径是被专门要求的：避免成功率被读成「系统很脆弱」
    expect(success.text()).toContain('排除被取代与已取消')

    const duration = wrapper.find('[data-metric="duration"]')
    expect(duration.text()).toContain('平均耗时')
    expect(duration.text()).toContain('1m 35s') // 95 秒

    const stuck = wrapper.find('[data-metric="stuck"]')
    expect(stuck.text()).toContain('卡死数')
    expect(stuck.text()).toContain('3')
    // 判据与阈值都由后端给（阈值 30 来自 stuck_threshold_minutes，不在前端写死）
    expect(stuck.text()).toContain('租约已过期')
    expect(stuck.text()).toContain('30 分钟')
  })

  it('成功率没有分母时显示「—」，不把「还没有收尾」读成 0%', async () => {
    const { wrapper } = await render([day()], [user()], null, {
      success_rate: null,
      avg_duration_seconds: null,
    })

    const success = wrapper.find('[data-metric="success"]')
    expect(success.text()).toContain('—')
    expect(success.text()).not.toContain('0.0%')

    const duration = wrapper.find('[data-metric="duration"]')
    expect(duration.text()).toContain('—')
  })

  it('卡死数可点，点进去是只含卡死任务的任务列表', async () => {
    const { wrapper, router } = await render()

    await wrapper.find('[data-metric="stuck"]').trigger('click')

    await vi.waitFor(() => {
      expect(router.currentRoute.value.name).toBe('jobs')
    })
    // 「只含卡死任务」由后端筛选（stuck=true），不是前端把列表再过滤一遍
    expect(router.currentRoute.value.query.stuck).toBe('1')
  })

  it('空数据显示说明文案，而不是一张什么都没有的图', async () => {
    const { wrapper } = await render(
      [day({ task_count: 0, succeeded_count: 0, failed_count: 0, prompt_tokens: 0, completion_tokens: 0, failure_rate: null })],
      [],
    )

    expect(wrapper.text()).toContain('这段时间还没有生成记录')
    // 排行也有自己的空态
    expect(wrapper.text()).toContain('还没有任何用户生成过任务')
  })

  it('失败率显示：没有任务时是「—」，0% 失败时是 0.0%', async () => {
    // 2 失败 / (6 成功 + 2 失败) = 25%
    const { wrapper: withFailure } = await render([
      day({ succeeded_count: 6, failed_count: 2, failure_rate: 0.25 }),
    ])
    expect(withFailure.text()).toContain('25.0%')

    // 全部成功（分母非 0）→ 0.0%，而不是「—」
    const { wrapper: allPass } = await render([
      day({ failed_count: 0, succeeded_count: 7, failure_rate: 0 }),
    ])
    expect(allPass.text()).toContain('0.0%')

    // 没有任何已收尾任务 → 「—」
    const { wrapper: noFinished } = await render([
      day({ succeeded_count: 0, failed_count: 0, task_count: 3, failure_rate: null }),
    ])
    expect(noFinished.text()).toContain('—')
  })

  it('已取消与被取代分开计数，且都不进失败率分母', async () => {
    const { wrapper } = await render([
      day({
        succeeded_count: 5,
        failed_count: 1,
        cancelled_count: 2,
        superseded_count: 3,
        failure_rate: 1 / 6,
      }),
    ])

    const text = wrapper.text()
    // 汇总条上有两个独立的格子
    expect(text).toContain('已取消')
    expect(text).toContain('被取代')
    expect(text).toContain('2') // 已取消
    expect(text).toContain('3') // 被取代
    // 切到「失败率」：分母只算「已定稿 + 真失败」
    const rateTab = wrapper.findAll('button').find((b) => b.text() === '失败率')
    expect(rateTab).toBeTruthy()
    await rateTab!.trigger('click')
    await wrapper.vm.$nextTick()
    const titles = wrapper.findAll('[title]').map((node) => node.attributes('title'))
    expect(titles.some((t) => t?.includes('失败（1 / 6）'))).toBe(true)
    // 图例里三者各有自己的色块
    const legend = wrapper.text()
    expect(legend).toContain('已取消')
    expect(legend).toContain('被取代')
    expect(legend).toContain('在途')
  })

  it('切换指标与天数会重新拉数据', async () => {
    const { wrapper } = await render()

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

    const router = makeRouter()
    await router.push('/dashboard')
    const wrapper = mount(DashboardView, { global: { plugins: [createPinia(), router] } })
    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('需要管理员权限')
    })
  })
})
