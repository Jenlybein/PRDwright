/**
 * 文档接口：读取某方案下的三份产物正文、提交用户的手改。
 *
 * 这是「文档所有权在后端」落地后前端仅有的两条通道——**读**与**提交用户变更**。
 * 生成结果不再由前端写回（那是后端在任务收尾时做的事），前端因此不需要、也不应该
 * 自己拼一份正文存起来：两份真源迟早会对不上。
 *
 * 命名口径与 `services/api.ts` 一致：后端用下划线（`api_docs`），前端用连字符
 * （`api-docs`），转换只在这一个文件里发生。
 *
 * 越权与不存在在后端都是 404，这里统一转成「方案不存在」——
 * 后端刻意让两者不可区分，前端也不该擅自还原这个信息。
 */

import axios from 'axios'

import http from '@/services/api'
import { parseRunSummary } from '@/utils/workflow'
import { normalizeImportance } from '@/utils/pendingItems'
import type { DocumentKind, DocumentRecord, PendingItem, RunSummary } from '@/types'

/** 后端 `kind` 用下划线，前端 `DocumentKind` 用连字符——这里是唯一的转换点 */
function toBackendKind(kind: DocumentKind): string {
  return kind === 'api-docs' ? 'api_docs' : kind
}

/** 后端 `kind` → 前端 `DocumentKind`；未知取值返回 `null`（调用方据此丢弃这一条） */
export function fromBackendKind(kind: unknown): DocumentKind | null {
  if (kind === 'prd') return 'prd'
  if (kind === 'api_docs' || kind === 'api-docs') return 'api-docs'
  if (kind === 'prompts') return 'prompts'
  return null
}

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

/** 方案不可见（不存在 / 不属于当前用户）时抛出 */
export class DocumentsNotFoundError extends Error {
  constructor() {
    super('方案不存在或已被删除')
    this.name = 'DocumentsNotFoundError'
  }
}

/**
 * 把后端返回的待确认清单收敛成前端结构。
 *
 * 后端返回的 `pending_items` 是对象数组；逐条校验「有 id 且有 question」才收，
 * 缺字段的脏数据直接丢弃（不该因为一条坏数据让整份文档读不出来）。这不是「分支判断」——
 * 只是对外部输入的防御性清洗，最终只得到可信的 `PendingItem[]`。
 */
function toPendingItems(data: unknown): PendingItem[] {
  if (!Array.isArray(data)) return []
  const items: PendingItem[] = []
  for (const entry of data) {
    if (typeof entry !== 'object' || entry === null) continue
    const raw = entry as Record<string, unknown>
    const id = raw.id
    const question = raw.question
    if (typeof id !== 'string' || typeof question !== 'string') continue
    const sections = Array.isArray(raw.sections)
      ? raw.sections.filter((s): s is string => typeof s === 'string')
      : []
    // 等级交给同一套收敛逻辑：与快照入口（`api.parsePendingItems`）同口径。
    // 认不出来 → 丢弃这一条（后端也不兜底，两边行为一致）。
    const importance = normalizeImportance(raw.importance)
    if (importance === null) continue
    items.push({
      id,
      question,
      sections,
      decider: typeof raw.decider === 'string' ? raw.decider : '',
      importance,
    })
  }
  return items
}

/**
 * 把后端返回的一条文档收敛成前端结构。
 *
 * 回执用 `parseRunSummary` 校验后透传：迁移进来的旧回执是另一种字段风格，
 * 结构对不上时退化为 `null`，让这一份「不显示回执」，而不是把整条文档丢掉。
 */
function toRecord(data: unknown): DocumentRecord | null {
  if (typeof data !== 'object' || data === null) return null
  const raw = data as Record<string, unknown>
  const kind = fromBackendKind(raw.kind)
  if (kind === null) return null
  const summary: RunSummary | null = parseRunSummary(raw.last_run_summary)
  return {
    kind,
    content: typeof raw.content === 'string' ? raw.content : '',
    runSummary: summary,
    pendingItems: toPendingItems(raw.pending_items),
    updatedAt: typeof raw.updated_at === 'string' ? raw.updated_at : '',
  }
}

/**
 * 读取某方案下的全部文档（至多三份）。
 * 对应 `GET /api/solutions/{id}/documents`。
 *
 * 「方案不存在」与「方案还没有文档」是两件事：前者 404（抛错），后者 200 + 空列表。
 */
export async function fetchDocuments(solutionId: number): Promise<DocumentRecord[]> {
  try {
    const { data } = await http.get<{ documents?: unknown[] }>(
      `/api/solutions/${solutionId}/documents`,
    )
    const items = Array.isArray(data?.documents) ? data.documents : []
    return items.map(toRecord).filter((item): item is DocumentRecord => item !== null)
  } catch (error) {
    if (axios.isAxiosError(error) && error.response?.status === 404) {
      throw new DocumentsNotFoundError()
    }
    throw new Error(backendMessage(error, '无法读取文档'))
  }
}

/**
 * 保存用户编辑后的正文。
 * 对应 `PUT /api/solutions/{id}/documents/{kind}`。
 *
 * 只覆盖正文——待确认清单与用量回执是「上一次生成」的事实，不因用户改几个字而改变。
 */
export async function saveDocument(
  solutionId: number,
  kind: DocumentKind,
  content: string,
): Promise<DocumentRecord> {
  try {
    const { data } = await http.put(`/api/solutions/${solutionId}/documents/${toBackendKind(kind)}`, {
      content,
    })
    const record = toRecord(data)
    if (record === null) throw new Error('保存文档失败')
    return record
  } catch (error) {
    if (axios.isAxiosError(error) && error.response?.status === 404) {
      throw new DocumentsNotFoundError()
    }
    // 兜底文案与上面 `toRecord` 失配时抛的错同源：非 axios 错误一律收敛成它，
    // 因此这里不必再分辨「是不是自己抛的」——那会变成拿中文文案做判断。
    throw new Error(backendMessage(error, '保存文档失败'))
  }
}
