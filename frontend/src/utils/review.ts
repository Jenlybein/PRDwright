/**
 * 审核结论的解析：`review` 事件与 `documents.review` **共用同一个归一化器**。
 *
 * 为什么不放在两处各写一套：SSE 的 `review` 事件负责订阅期间的即时展示，落库那条
 * （`document_versions.review`）才是刷新 / 换设备后的唯一来源。两处各拼一份解析的后果
 * 不是报错，而是「同一次审核在界面上与刷新后长得不一样」——那种偏差只能靠人眼发现。
 *
 * 放在 `utils/` 而不是 `services/api.ts` 的理由与 `utils/runSummary.ts` 相同：
 * `documentService` 已经 import 了 `api` 的 axios 实例，反向 import 会成环；
 * 纯归一化逻辑下沉到 utils 后两边都能用。
 */

import type { ReviewConclusion } from '@/types'

/** 从对象里安全地取字符串（缺失或类型不对时返回空串） */
function readString(data: Record<string, unknown>, key: string): string {
  const value = data[key]
  return typeof value === 'string' ? value : ''
}

/** 逐项校验对象数组，只保留对象项（`null` 与原始值一律丢弃） */
function readObjectArray(
  data: Record<string, unknown>,
  key: string,
): Record<string, unknown>[] {
  const value = data[key]
  if (!Array.isArray(value)) return []
  return value.filter(
    (item): item is Record<string, unknown> => typeof item === 'object' && item !== null,
  )
}

/**
 * 把后端审核结论归一成 `ReviewConclusion`；不是对象时返回 `null`。
 *
 * 与其它解析器同一口径：坏字段退化为默认值，不让一条脏数据打断整条流或整份文档。
 * `verdict` 为空串表示**没审成**（不是通过）——调用方据此显示「审核失败」。
 */
export function parseReview(raw: unknown): ReviewConclusion | null {
  if (typeof raw !== 'object' || raw === null) return null
  const data = raw as Record<string, unknown>
  const issues = readObjectArray(data, 'issues').map((item) => ({
    severity: readString(item, 'severity'),
    sectionId: readString(item, 'section_id'),
    sectionTitle: readString(item, 'section_title'),
    detail: readString(item, 'detail'),
  }))
  const failedSections = readObjectArray(data, 'failed_sections').map((item) => ({
    sectionId: readString(item, 'section_id'),
    title: readString(item, 'title'),
    reason: readString(item, 'reason'),
  }))
  return {
    kind: readString(data, 'kind'),
    verdict: readString(data, 'verdict'),
    summary: readString(data, 'summary'),
    issues,
    rewritten: data.rewritten === true,
    failedSections,
    // 审核时间：面板据此回答「什么时候审的」。缺失时为空串（不猜一个时间）
    reviewedAt: readString(data, 'reviewed_at'),
  }
}
