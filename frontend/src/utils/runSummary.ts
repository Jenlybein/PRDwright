/**
 * 「生成用量回执」的唯一解析入口。
 *
 * ## 为什么要有这个文件
 *
 * 同一份回执有**两个来源、两种字段风格**：
 *
 * 1. SSE 的 `run_summary` 事件 —— 后端 dataclass 直出，字段是 `snake_case`
 *    （`duration_ms` / `request_id` / `provider_config_id`…）；
 * 2. `documents.last_run_summary` —— 存的就是上面那个 dict 的 JSON，**也是 snake_case**。
 *
 * 而前端的历史实现在两处各写了一套解析：事件那侧做了映射、落库那侧直接把对象
 * 断言成 `RunSummary`。结果是**刷新页面之后回执整块消失**——因为 `durationMs`
 * 在 snake_case 里根本不存在，那个「可信吗」的判断直接把它判成了脏数据。
 * 而且在界面之外完全看不出来：生成当下有回执，刷新后没有，看起来像「本来就只显示一次」。
 *
 * 所以解析只留这一份，两种键都认（历史夹具与迁移数据里两种都出现过）。
 * 缺字段只退化那一个字段：回执是**锦上添花**的诊断信息，不能因为它取不到就
 * 打断整条流或让整页报错。
 */

import type { FailedSection, RunSummary, SkillRef, StepUsage } from '@/types'

/** 取对象；不是对象（含数组）时给 null */
function asRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null
  return value as Record<string, unknown>
}

/**
 * 按候选键依次取字符串。键的顺序有意义：先 snake_case、再 camelCase，
 * 与「新数据的真源在后端」一致。
 */
function pickString(data: Record<string, unknown>, ...keys: string[]): string {
  for (const key of keys) {
    const value = data[key]
    if (typeof value === 'string') return value
  }
  return ''
}

/** 同上，取数字；缺失或非有限数给 `null`（交给调用方决定默认值，避免把「没这字段」写成 0） */
function pickNumber(data: Record<string, unknown>, ...keys: string[]): number | null {
  for (const key of keys) {
    const value = data[key]
    if (typeof value === 'number' && Number.isFinite(value)) return value
  }
  return null
}

function pickArray(data: Record<string, unknown>, ...keys: string[]): unknown[] {
  for (const key of keys) {
    const value = data[key]
    if (Array.isArray(value)) return value
  }
  return []
}

function parseSteps(data: Record<string, unknown>): StepUsage[] {
  return pickArray(data, 'steps')
    .map(asRecord)
    .filter((row): row is Record<string, unknown> => row !== null)
    .map((row) => ({
      step: pickString(row, 'step'),
      calls: pickNumber(row, 'calls') ?? 0,
      failedCalls: pickNumber(row, 'failed_calls', 'failedCalls') ?? 0,
      promptTokens: pickNumber(row, 'prompt_tokens', 'promptTokens') ?? 0,
      completionTokens: pickNumber(row, 'completion_tokens', 'completionTokens') ?? 0,
      durationMs: pickNumber(row, 'duration_ms', 'durationMs') ?? 0,
    }))
}

function parseSkills(data: Record<string, unknown>): SkillRef[] {
  return pickArray(data, 'injected_skills', 'injectedSkills')
    .map(asRecord)
    .filter((row): row is Record<string, unknown> => row !== null)
    .map((row) => ({
      name: pickString(row, 'name'),
      version: pickString(row, 'version'),
      fingerprint: pickString(row, 'fingerprint'),
    }))
}

function parseFailedSections(data: Record<string, unknown>): FailedSection[] {
  return pickArray(data, 'failed_sections', 'failedSections')
    .map(asRecord)
    .filter((row): row is Record<string, unknown> => row !== null)
    .map((row) => ({
      sectionId: pickString(row, 'section_id', 'sectionId'),
      title: pickString(row, 'title'),
      reason: pickString(row, 'reason'),
    }))
}

/**
 * 把一份回执归一成 `RunSummary`；不像回执的东西返回 `null`。
 *
 * 「像不像」的判据只有两条，且都挑**不可能由脏数据伪造**的：
 * `kind` 是非空字符串、`duration_ms`/`durationMs` 是数字。旧实现要求 `requestId`
 * 必须是字符串——可它是可选信息，一次没有请求上下文的生成（脚本、直连服务）
 * 本来就没有它，那样会把一份完好的回执判成垃圾。
 */
export function normalizeRunSummary(value: unknown): RunSummary | null {
  const data = asRecord(value)
  if (!data) return null

  const kind = pickString(data, 'kind')
  const durationMs = pickNumber(data, 'duration_ms', 'durationMs')
  if (!kind || durationMs === null) return null

  return {
    kind,
    status: pickString(data, 'status'),
    durationMs,
    totalCalls: pickNumber(data, 'total_calls', 'totalCalls') ?? 0,
    failedCalls: pickNumber(data, 'failed_calls', 'failedCalls') ?? 0,
    promptTokens: pickNumber(data, 'prompt_tokens', 'promptTokens') ?? 0,
    completionTokens: pickNumber(data, 'completion_tokens', 'completionTokens') ?? 0,
    steps: parseSteps(data),
    injectedSkills: parseSkills(data),
    warnings: pickArray(data, 'warnings').filter(
      (item): item is string => typeof item === 'string',
    ),
    partial: data.partial === true,
    failedSections: parseFailedSections(data),
    // 供应商身份拆成三个字段（v19）：协议用来解释「走的是哪类接口」，
    // 配置 id 用来定位「具体哪一条」，名字是给人看的那一行。
    protocol: pickString(data, 'protocol'),
    providerConfigId: pickString(data, 'provider_config_id', 'providerConfigId'),
    providerName: pickString(data, 'provider_name', 'providerName'),
    model: pickString(data, 'model'),
    requestId: pickString(data, 'request_id', 'requestId'),
  }
}
