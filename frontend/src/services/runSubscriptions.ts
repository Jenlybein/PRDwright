/**
 * 任务流订阅的登记表（纯模块，零 store 依赖）。
 *
 * 为什么需要它：`useJob` 是 app 级单例，从前用一个 `controller` 单槽 + 一个
 * `reconnectingJobId` 数字来「防止重复订阅」。单槽只能记住最近一次订阅，切方案、
 * 一支收尾另一支还在跑、并发重连两种任务——这些情形下前者会把后者的控制器覆盖掉，
 * 于是 `detach()` 只能中止最近的那一路，另一路继续把事件写进预览。
 *
 * 改成按 `jobId` 登记之后：
 *
 * - `detachAll()` 能真正退订**所有**在途订阅（切方案的语义就是这个）；
 * - `isSubscribed(jobId)` 天然支持多任务，同时充当「重连幂等」的判据；
 * - `solutions` 可以直接 import 本模块，而不必 import `useDocumentPipeline`（避免成环）。
 *
 * 退订 = `abort()` 本地 fetch，**不取消后端任务**：后端照跑，回到原方案时凭快照指针重连。
 */

const controllers = new Map<number, AbortController>()

/** 登记一次订阅；同一 jobId 若已有旧订阅，先中止旧的（同一任务不允许两路并行） */
export function register(jobId: number, controller: AbortController): void {
  const previous = controllers.get(jobId)
  if (previous && previous !== controller) previous.abort()
  controllers.set(jobId, controller)
}

/** 释放登记；只释放**同一个** controller（避免后一次订阅被前一次的 finally 摘掉） */
export function release(jobId: number, controller: AbortController): void {
  if (controllers.get(jobId) === controller) controllers.delete(jobId)
}

/** 该任务是否已有在途订阅 */
export function isSubscribed(jobId: number): boolean {
  return controllers.has(jobId)
}

/** 中止并移除某一支订阅（若存在） */
export function detach(jobId: number): void {
  const controller = controllers.get(jobId)
  if (!controller) return
  controllers.delete(jobId)
  controller.abort()
}

/** 中止并移除全部订阅 */
export function detachAll(): void {
  const pending = [...controllers.values()]
  controllers.clear()
  for (const controller of pending) controller.abort()
}

/** 仅供测试：清空登记表（不触发 abort） */
export function resetForTests(): void {
  controllers.clear()
}
