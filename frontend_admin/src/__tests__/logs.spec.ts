/**
 * 日志页的用例。
 *
 * 这里钉的三条都是「不做就会骗人」的行为：
 *
 * 1. **默认只查最近 24 小时**。日志表只增不减，一进来就拉全量会把整张表搬过来；
 *    更糟的是它看起来「能用」，只是慢得没道理。
 * 2. **点请求号 = 按它反查，并清掉其它条件**。排障主路径就是「粘一个号看全链路」。
 *    若同时残留着级别/关键词筛选，这个请求会被悄悄筛掉一部分，
 *    表现为「这次请求只查到一条」，而实际有很多条——最难查的一类假象。
 * 3. **空结果要说明白**，而不是一片空白：分不清「没有日志」和「页面坏了」。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'

// 只 mock 请求函数，`messageOf` 等纯函数保留真实实现
vi.mock('@/services/adminService', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/services/adminService')>()
  return { ...actual, fetchLogs: vi.fn() }
})

import {
  fetchLogs,
  type AdminLog,
  type AdminLogPage,
  type LogQuery,
} from '@/services/adminService'
import { ApiError } from '@/services/errors'
import LogsView from '@/views/LogsView.vue'

const logsMock = vi.mocked(fetchLogs)

function log(overrides: Partial<AdminLog> = {}): AdminLog {
  return {
    id: 1,
    ts: '2026-10-22T02:30:00.123Z',
    level: 'INFO',
    logger: 'backend.services.job_service',
    msg: '生成任务已派发',
    request_id: 'req-abcdef123456',
    user_id: 3,
    event: 'job.started',
    extra: { job_id: 7 },
    exc: null,
    ...overrides,
  }
}

function page(items: AdminLog[], total = items.length): AdminLogPage {
  return { items, total, page: 1, page_size: 100 }
}

/** 取第 n 次调用的查询参数；`calls[n][0]` 在 TS 里是「可能没有」，这里收口一次 */
function queryAt(n = 0): Partial<LogQuery> {
  return (logsMock.mock.calls[n]?.[0] ?? {}) as Partial<LogQuery>
}

async function render(data: AdminLogPage) {
  logsMock.mockResolvedValue(data)
  const wrapper = mount(LogsView, { global: { plugins: [createPinia()] } })
  await vi.waitFor(() => {
    expect(logsMock).toHaveBeenCalled()
  })
  await wrapper.vm.$nextTick()
  return wrapper
}

beforeEach(() => {
  setActivePinia(createPinia())
  logsMock.mockReset()
})

describe('日志页', () => {
  it('默认只查最近 24 小时，而不是拉全量', async () => {
    await render(page([log()]))

    const query = queryAt(0)
    expect(query.since).toBeTruthy()

    // 起点应落在 24 小时前附近（给一分钟的容差，避免用例因机器慢而抖动）
    const since = new Date(query.since as string).getTime()
    const expected = Date.now() - 24 * 60 * 60 * 1000
    expect(Math.abs(since - expected)).toBeLessThan(60 * 1000)
  })

  it('默认只看业务事件：请求里排掉访问日志', async () => {
    // 访问日志每个请求一条，量级会压过业务事件。不排掉的话，
    // 管理员打开页面看到的是一屏「请求完成」，而他找的是「刚才那次生成怎么了」。
    await render(page([log()]))

    expect(queryAt(0).exclude_event).toBe('request.completed')
  })

  it('列出消息、级别与事件名', async () => {
    const wrapper = await render(page([log({ level: 'ERROR', msg: '生成任务异常终止' })]))

    const text = wrapper.text()
    expect(text).toContain('生成任务异常终止')
    expect(text).toContain('ERROR')
    expect(text).toContain('job.started')
  })

  it('点请求号按它反查，并清掉其它筛选条件', async () => {
    const wrapper = await render(page([log()]))

    // 先设一个级别筛选，模拟「筛着筛着突然想按请求号查」
    await wrapper.find('select').setValue('ERROR')
    await vi.waitFor(() => expect(logsMock).toHaveBeenCalledTimes(2))

    const requestButton = wrapper
      .findAll('button')
      .find((b) => b.text().includes('req-abcd'))
    expect(requestButton).toBeTruthy()
    await requestButton!.trigger('click')

    await vi.waitFor(() => expect(logsMock).toHaveBeenCalledTimes(3))
    const query = queryAt(2)
    expect(query.request_id).toBe('req-abcdef123456')
    // 关键：级别与时间都被清掉，否则这次请求的一部分日志会被悄悄筛走。
    // 用 `toBeFalsy` 而不是 `toBeUndefined`：空串与 undefined 都是「不筛」，
    // 后端两者都认（见 `LogRepository.search` 的 `if level:`），
    // 断言绑死某一种写法会让「换一种等价的清空方式」无故变红。
    expect(query.level).toBeFalsy()
    expect(query.since).toBeFalsy()
    // 也不能再排访问日志：那一条「请求完成」正是「这个请求到没到过后端」的证据
    expect(query.exclude_event).toBeFalsy()
  })

  it('展开详情能看到业务字段与异常堆栈', async () => {
    const wrapper = await render(
      page([
        log({
          level: 'ERROR',
          extra: { job_id: 7, duration_ms: 5200 },
          exc: 'Traceback (most recent call last):\n  ...\nConnectionError: 上游连接中断',
        }),
      ])
    )

    // 详情默认折叠：先在页面上找不到堆栈
    expect(wrapper.text()).not.toContain('上游连接中断')

    await wrapper.find('tbody tr').trigger('click')
    await wrapper.vm.$nextTick()

    const text = wrapper.text()
    expect(text).toContain('job_id')
    expect(text).toContain('duration_ms')
    expect(text).toContain('上游连接中断')
  })

  it('时间按本地时区显示，而不是原样截 UTC 串', async () => {
    // 后端存的是 UTC。直接截字符串会让东八区用户看到 8 小时前的时刻，
    // 而时间正是排障时最先看的一列。这里用「与 Date 的本地格式化一致」来断言，
    // 不写死某个时区的期望值（CI 机器的时区未必和开发机一样）。
    const utc = '2026-10-22T02:30:00.123Z'
    const wrapper = await render(page([log({ ts: utc })]))

    const local = new Date(utc)
    const pad = (n: number) => String(n).padStart(2, '0')
    const expected =
      `${local.getFullYear()}-${pad(local.getMonth() + 1)}-${pad(local.getDate())} ` +
      `${pad(local.getHours())}:${pad(local.getMinutes())}:${pad(local.getSeconds())}.123`

    expect(wrapper.text()).toContain(expected)
    // 反向断言：绝不能出现「原样截 UTC」的那种字符串（形如 2026-10-22 02:30:00.123）
    const rawTrimmed = utc.replace('T', ' ').replace('Z', '').slice(0, 23)
    if (rawTrimmed !== expected) {
      expect(wrapper.text()).not.toContain(rawTrimmed)
    }
  })

  it('没有匹配结果时给出说明，而不是一片空白', async () => {
    const wrapper = await render(page([]))

    expect(wrapper.text()).toContain('没有匹配的日志')
  })

  it('请求失败时显示后端给的原因', async () => {
    logsMock.mockRejectedValue(new ApiError('无权访问后台', 403))
    const wrapper = mount(LogsView, { global: { plugins: [createPinia()] } })

    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('无权访问后台')
    })
  })

  it('总数超过一页时显示分页，并可翻到下一页', async () => {
    const wrapper = await render(page([log()], 250))

    expect(wrapper.text()).toContain('第 1 / 3 页')

    const next = wrapper.findAll('button').find((b) => b.text() === '下一页')
    expect(next).toBeTruthy()
    await next!.trigger('click')

    await vi.waitFor(() => expect(logsMock).toHaveBeenCalledTimes(2))
    expect(queryAt(1).page).toBe(2)
  })
})
