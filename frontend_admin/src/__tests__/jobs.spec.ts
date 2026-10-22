/**
 * 任务监控页的用例。
 *
 * 三条最关键的行为：
 *
 * 1. **卡死标记来自后端**，页面只负责显示成红色。前端自己算会因时钟差
 *    与服务器对不上，出现「列表说卡死、点了却还在跑」。
 * 2. **取消必须二次确认**：它是不可逆的，文档任务不会保留半截正文。
 * 3. **`cancelled=false` 是正常结果**：任务在点击前刚好结束，页面要说
 *    「已经结束了」，而不是弹一个失败。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'

vi.mock('@/services/adminService', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/services/adminService')>()
  return {
    ...actual,
    fetchJobs: vi.fn(),
    cancelJob: vi.fn(),
    cancelStuckJobs: vi.fn(),
  }
})

import {
  cancelJob,
  cancelStuckJobs,
  fetchJobs,
  type AdminJob,
} from '@/services/adminService'
import { ApiError } from '@/services/errors'
import JobsView from '@/views/JobsView.vue'

const jobsMock = vi.mocked(fetchJobs)
const cancelMock = vi.mocked(cancelJob)
const stuckMock = vi.mocked(cancelStuckJobs)

function job(overrides: Partial<AdminJob> = {}): AdminJob {
  return {
    id: 1,
    owner_id: 1,
    owner_name: 'alice',
    kind: 'prd',
    status: 'running',
    phase: 'writing',
    created_at: '2026-10-22T00:00:00',
    updated_at: '2026-10-22T00:10:00',
    duration_seconds: 600,
    is_stuck: false,
    stuck_minutes: 0,
    prompt_tokens: 100,
    completion_tokens: 50,
    error_code: '',
    error_message: '',
    ...overrides,
  }
}

async function render(items: AdminJob[] = [], total = items.length) {
  jobsMock.mockResolvedValue({ items, total })
  const wrapper = mount(JobsView, { global: { plugins: [createPinia()] } })
  await vi.waitFor(() => expect(jobsMock).toHaveBeenCalled())
  await wrapper.vm.$nextTick()
  return wrapper
}

function button(wrapper: ReturnType<typeof mount>, label: string) {
  const found = wrapper.findAll('button').find((b) => b.text() === label)
  expect(found, `找不到按钮「${label}」`).toBeTruthy()
  return found!
}

beforeEach(() => {
  setActivePinia(createPinia())
  jobsMock.mockReset()
  cancelMock.mockReset()
  stuckMock.mockReset()
})

describe('任务监控页', () => {
  it('列出任务，并把卡死任务标红', async () => {
    const wrapper = await render([
      job(),
      job({ id: 2, owner_name: 'bob', is_stuck: true, stuck_minutes: 95 }),
    ])

    expect(wrapper.text()).toContain('alice')
    expect(wrapper.text()).toContain('bob')
    expect(wrapper.text()).toContain('已卡死 95 分钟')
    // 卡死那一行要有可识别的红底（只标文字不够显眼）
    expect(wrapper.find('.bg-danger-surface').exists()).toBe(true)
  })

  it('取消先确认；未确认前不发请求', async () => {
    const wrapper = await render([job()])

    await button(wrapper, '取消').trigger('click')
    await wrapper.vm.$nextTick()

    expect(cancelMock).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('确定取消任务 #1')
  })

  it('确认后才真正取消，并提示已取消', async () => {
    const wrapper = await render([job()])
    cancelMock.mockResolvedValue({ cancelled: true })

    await button(wrapper, '取消').trigger('click')
    await button(wrapper, '确认取消').trigger('click')

    await vi.waitFor(() => expect(cancelMock).toHaveBeenCalledWith(1))
    expect(wrapper.text()).toContain('已取消任务 #1')
  })

  it('任务已结束时提示「无需取消」，不当成失败', async () => {
    const wrapper = await render([job()])
    cancelMock.mockResolvedValue({ cancelled: false })

    await button(wrapper, '取消').trigger('click')
    await button(wrapper, '确认取消').trigger('click')

    await vi.waitFor(() => expect(cancelMock).toHaveBeenCalled())
    expect(wrapper.text()).toContain('已经结束了')
  })

  it('后端回绝时把原因原样显示', async () => {
    const wrapper = await render([job()])
    cancelMock.mockRejectedValue(new ApiError('该任务不存在', 404))

    await button(wrapper, '取消').trigger('click')
    await button(wrapper, '确认取消').trigger('click')

    await vi.waitFor(() => expect(wrapper.text()).toContain('该任务不存在'))
  })

  it('批量取消卡死任务', async () => {
    const wrapper = await render([job({ is_stuck: true, stuck_minutes: 60 })])
    stuckMock.mockResolvedValue({ cancelled: 1 })

    const bulk = wrapper.findAll('button').find((b) => b.text().startsWith('取消全部卡死'))
    expect(bulk).toBeTruthy()
    await bulk!.trigger('click')

    await vi.waitFor(() => expect(stuckMock).toHaveBeenCalled())
    expect(wrapper.text()).toContain('已取消 1 个卡死任务')
  })

  it('筛选条件原样传给后端', async () => {
    const wrapper = await render([])

    await wrapper.find('input[type="text"]').setValue('ali')
    await wrapper.find('select').setValue('prompts')
    // 触发 form 的 submit（点 submit 按钮在 jsdom 里不一定会触发表单提交）
    await wrapper.find('form').trigger('submit')

    await vi.waitFor(() => expect(jobsMock.mock.calls.length).toBeGreaterThanOrEqual(2))
    const calls = jobsMock.mock.calls
    expect(calls[calls.length - 1]![0]).toMatchObject({
      status: 'running',
      username: 'ali',
      kind: 'prompts',
    })
  })

  it('开启自动刷新后按 10 秒重新拉取', async () => {
    vi.useFakeTimers()
    try {
      jobsMock.mockResolvedValue({ items: [], total: 0 })
      const wrapper = mount(JobsView, { global: { plugins: [createPinia()] } })
      await vi.advanceTimersByTimeAsync(0)
      expect(jobsMock).toHaveBeenCalledTimes(1)

      await wrapper.find('input[type="checkbox"]').setValue(true)
      await vi.advanceTimersByTimeAsync(10_000)
      expect(jobsMock.mock.calls.length).toBeGreaterThanOrEqual(2)

      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })
})
