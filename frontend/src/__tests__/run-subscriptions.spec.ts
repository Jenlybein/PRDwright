import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  detach,
  detachAll,
  isSubscribed,
  register,
  release,
  resetForTests,
} from '@/services/runSubscriptions'

/**
 * 任务流订阅登记表测试。
 *
 * 从前「防止重复订阅」靠 `useJob` 里一个 `controller` 单槽加一个 `reconnectingJobId`
 * 数字。单槽只能记住最近一次订阅：切方案、一支收尾另一支还在跑、两种任务各自重连，
 * 前者会把后者的控制器覆盖掉，于是「退订全部」只能退掉最近那一路，另一路继续把事件
 * 写进预览。改成按 `jobId` 登记之后，下面这几条才成立。
 */

/** 造一个可观察的控制器 */
function controller(): { controller: AbortController; aborted: () => boolean } {
  const c = new AbortController()
  return { controller: c, aborted: () => c.signal.aborted }
}

beforeEach(() => {
  resetForTests()
})

describe('runSubscriptions', () => {
  it('登记后能查到，释放后查不到；重复释放不影响别的登记', () => {
    const first = controller()
    register(12, first.controller)
    expect(isSubscribed(12)).toBe(true)

    // 后一次订阅覆盖同一 jobId 时，前一次必须被中止——同一任务不允许两路并行
    const second = controller()
    register(12, second.controller)
    expect(first.aborted()).toBe(true)
    expect(second.aborted()).toBe(false)

    // 前一次的 finally 迟到释放：不能把后一次的登记摘掉
    release(12, first.controller)
    expect(isSubscribed(12)).toBe(true)

    release(12, second.controller)
    expect(isSubscribed(12)).toBe(false)
  })

  it('detach 只退订点名的那一支', () => {
    const a = controller()
    const b = controller()
    register(12, a.controller)
    register(13, b.controller)

    detach(12)

    expect(a.aborted()).toBe(true)
    expect(b.aborted()).toBe(false)
    expect(isSubscribed(12)).toBe(false)
    expect(isSubscribed(13)).toBe(true)
  })

  it('detachAll 退订全部在途订阅（切方案的语义）', () => {
    const a = controller()
    const b = controller()
    register(12, a.controller)
    register(13, b.controller)

    detachAll()

    expect(a.aborted()).toBe(true)
    expect(b.aborted()).toBe(true)
    expect(isSubscribed(12)).toBe(false)
    expect(isSubscribed(13)).toBe(false)
  })

  it('detachAll 之后的 abort 事件照常派发（退订 = 中止本地读取，不取消服务端任务）', () => {
    const a = controller()
    const onAbort = vi.fn()
    a.controller.signal.addEventListener('abort', onAbort)
    register(12, a.controller)

    detachAll()

    expect(onAbort).toHaveBeenCalledTimes(1)
  })
})
