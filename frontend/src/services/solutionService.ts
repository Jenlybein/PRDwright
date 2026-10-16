/**
 * 方案接口：列表 / 创建 / 读取 / 更新 / 删除。
 *
 * 方案就是一份完整会话的**全量快照**，因此更新是整份覆盖（`PUT`）而不是字段级补丁。
 * 唯一需要调用方自己判断的是：**这份方案是否已经存在**——首次出现内容时调 `createSolution`，
 * 之后一律 `updateSolution`。这个判断在 `stores/solutions.ts` 里，依据是 URL 上有没有 id。
 *
 * 越权与不存在在后端都表现为 404，这里统一转成「方案不存在」，
 * 不去猜测到底是哪一种——后端刻意让两者不可区分，前端也不该擅自还原这个信息。
 */

import axios from 'axios'

import http from '@/services/api'
import type { SolutionDetail, SolutionSavePayload, SolutionSummary } from '@/types'

/** 从后端错误响应里取可直接展示的说明；取不到时用兜底文案 */
function backendMessage(error: unknown, fallback: string): string {
  if (!axios.isAxiosError(error)) return fallback

  const data: unknown = error.response?.data
  if (!data || typeof data !== 'object' || !('detail' in data)) return fallback

  const detail = (data as { detail: unknown }).detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) return '提交的内容不符合要求，请检查后重试'
  return fallback
}

/** 把后端响应收敛成 `SolutionSummary`，字段缺失时给安全默认值而不是崩溃 */
function toSummary(data: unknown): SolutionSummary {
  const raw = (data ?? {}) as Partial<SolutionSummary>
  return {
    id: typeof raw.id === 'number' ? raw.id : 0,
    title: typeof raw.title === 'string' ? raw.title : '',
    mode: typeof raw.mode === 'string' ? raw.mode : '',
    stage: typeof raw.stage === 'string' ? raw.stage : '',
    created_at: typeof raw.created_at === 'string' ? raw.created_at : '',
    updated_at: typeof raw.updated_at === 'string' ? raw.updated_at : '',
    // 缺省为空串而不是回落到 `updated_at`：这里只做「形状收敛」，把两种时间混起来会
    // 让「旧后端没这列」这件事静默消失。展示层的回落写在用它的那个组件里，一眼可见。
    content_updated_at:
      typeof raw.content_updated_at === 'string' ? raw.content_updated_at : '',
  }
}

/**
 * 列出当前用户的方案摘要，最近更新在前。
 * 对应 `GET /api/solutions`。
 */
export async function fetchSolutions(): Promise<SolutionSummary[]> {
  try {
    const { data } = await http.get<{ solutions: unknown[] }>('/api/solutions')
    const items = Array.isArray(data?.solutions) ? data.solutions : []
    return items.map(toSummary)
  } catch (error) {
    throw new Error(backendMessage(error, '无法获取方案列表'))
  }
}

/**
 * 读取方案详情（含快照正文）。
 * 对应 `GET /api/solutions/{id}`。
 *
 * 404 单独抛成 `SolutionNotFoundError`，让调用方能区分「这份方案没了」
 * （应该把地址退回 `/v2` 新建）与「网络/服务异常」（应该提示重试）。
 */
export async function fetchSolution(id: number): Promise<SolutionDetail> {
  try {
    const { data } = await http.get<SolutionDetail>(`/api/solutions/${id}`)
    return { ...toSummary(data), data: typeof data?.data === 'string' ? data.data : '' }
  } catch (error) {
    if (axios.isAxiosError(error) && error.response?.status === 404) {
      throw new SolutionNotFoundError()
    }
    throw new Error(backendMessage(error, '无法读取该方案'))
  }
}

/** 方案不存在或不属于当前用户时抛出 */
export class SolutionNotFoundError extends Error {
  constructor() {
    super('方案不存在或已被删除')
    this.name = 'SolutionNotFoundError'
  }
}

/**
 * 创建方案，返回带新 id 的摘要。
 * 对应 `POST /api/solutions`。
 */
export async function createSolution(payload: SolutionSavePayload): Promise<SolutionSummary> {
  try {
    const { data } = await http.post<SolutionSummary>('/api/solutions', payload)
    return toSummary(data)
  } catch (error) {
    throw new Error(backendMessage(error, '创建方案失败'))
  }
}

/**
 * 整份覆盖更新方案。
 * 对应 `PUT /api/solutions/{id}`。
 *
 * 返回 `null` 表示方案已不存在（被删或不属于当前用户）——调用方据此停止自动保存，
 * 避免每 1.5 秒对着一个不存在的 id 反复重试。
 */
export async function updateSolution(
  id: number,
  payload: SolutionSavePayload,
): Promise<SolutionDetail | null> {
  try {
    const { data } = await http.put<SolutionDetail>(`/api/solutions/${id}`, payload)
    return { ...toSummary(data), data: typeof data?.data === 'string' ? data.data : '' }
  } catch (error) {
    if (axios.isAxiosError(error) && error.response?.status === 404) return null
    throw new Error(backendMessage(error, '保存方案失败'))
  }
}

/**
 * 删除方案。
 * 对应 `DELETE /api/solutions/{id}`。
 *
 * 已不存在时视为删除成功——用户的意图是「让它消失」，而它确实不在了。
 */
export async function deleteSolution(id: number): Promise<void> {
  try {
    await http.delete(`/api/solutions/${id}`)
  } catch (error) {
    if (axios.isAxiosError(error) && error.response?.status === 404) return
    throw new Error(backendMessage(error, '删除方案失败'))
  }
}
