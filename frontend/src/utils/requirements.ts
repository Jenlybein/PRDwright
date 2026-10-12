import type {
  MvpFeature,
  RequirementAnswers,
  RequirementsSummary,
  SummaryChange,
} from '@/types'

/**
 * 结构化需求 → 后端接口载荷的转换工具。
 *
 * V2 的录入表单产出的是与 `field-schema.json` 对齐的结构化摘要，而后端有两个
 * 消费入口，各自需要的形态不同：
 *
 * 1. `POST /api/conversation/start-stream` 收 `answers` 字典，后端用
 *    `format_form_data` 把 key 还原成题目文案再交给模型（见
 *    `core/question_config.json`）。因此这里把字段映射到**已有题目的字段名**上，
 *    让模型看到的是自然的中文题面而不是原始 key。
 * 2. `POST /api/conversation/generate-prd-stream` 收 `requirements` 文本，
 *    会拼进技能包提示词作为「需求输入」。字段契约本身也在提示词里，所以这里
 *    直接给与 schema 完全一致的结构化 JSON。
 */

/** 单个 MVP 功能 → 一行可读文本 */
function formatFeature(feature: MvpFeature): string {
  const parts = [feature.name]
  if (feature.description) parts.push(feature.description)
  if (feature.user_value) parts.push(`价值：${feature.user_value}`)
  return parts.join(' —— ')
}

/**
 * 把结构化摘要里「后端已有题目」之外的字段拼成一段补充说明。
 *
 * 这些字段在后端问卷里没有单独题目，统一并入「还有哪些约束或背景信息」这一题，
 * 避免模型看到 `ui_pages` 之类的原始 key。全部为空时返回空串。
 */
function buildExtraText(summary: RequirementsSummary): string {
  const parts: string[] = []

  if (summary.v2_features?.length) {
    parts.push(`未来规划：${summary.v2_features.join('、')}`)
  }
  if (summary.ui_pages?.length) {
    const pages = summary.ui_pages.map((page) => {
      const modules = page.modules.length ? `模块：${page.modules.join('、')}` : '模块：待确认'
      const notes = page.notes ? `；备注：${page.notes}` : ''
      return `${page.name}（${modules}${notes}）`
    })
    parts.push(`页面结构：${pages.join('；')}`)
  }
  if (summary.key_interactions?.length) {
    parts.push(`关键交互：${summary.key_interactions.join('；')}`)
  }

  const tech = summary.technical_constraints
  if (tech) {
    const items: string[] = []
    if (tech.frontend) items.push(`前端 ${tech.frontend}`)
    if (tech.backend) items.push(`后端 ${tech.backend}`)
    if (tech.deployment) items.push(`部署 ${tech.deployment}`)
    if (items.length) parts.push(`技术选型约束：${items.join('；')}`)
  }

  if (summary.llm_usage) parts.push(`LLM 使用：${summary.llm_usage}`)

  const nonFunctional = summary.non_functional
  if (nonFunctional) {
    const items: string[] = []
    if (nonFunctional.security) items.push(`安全 ${nonFunctional.security}`)
    if (nonFunctional.performance) items.push(`性能 ${nonFunctional.performance}`)
    if (nonFunctional.availability) items.push(`可用性 ${nonFunctional.availability}`)
    if (items.length) parts.push(`非功能要求：${items.join('；')}`)
  }

  if (summary.scope) {
    const { in_scope: inScope, out_of_scope: outOfScope } = summary.scope
    if (inScope.length) parts.push(`范围内：${inScope.join('、')}`)
    if (outOfScope.length) parts.push(`不在范围内：${outOfScope.join('、')}`)
  }

  return parts.join('\n')
}

/**
 * 结构化摘要 → `start-stream` 的 `answers`。
 *
 * 值可以是字符串或字符串数组（后端会按需拼接）。只有填了内容的字段才会出现，
 * 保证首轮上下文里没有空条目。
 */
export function buildAnswers(summary: RequirementsSummary): RequirementAnswers {
  const answers: RequirementAnswers = {}

  if (summary.product_name) answers.product_name = summary.product_name
  if (summary.product_goal) answers.problem_to_solve = summary.product_goal
  if (summary.target_users.length) answers.target_users = summary.target_users
  if (summary.mvp_features.length) answers.core_features = summary.mvp_features.map(formatFeature)
  if (summary.platform) answers.platform_type = summary.platform

  if (summary.technical_constraints?.auth) {
    answers.auth_required = summary.technical_constraints.auth
  }
  if (summary.technical_constraints?.storage) {
    answers.data_storage = summary.technical_constraints.storage
  }

  const extra = buildExtraText(summary)
  if (extra) answers.constraints_or_extra = extra

  return answers
}

/**
 * 结构化摘要 → `generate-prd-stream` 的 `requirements`。
 *
 * 输出为与 `field-schema.json` 一致的 fenced JSON，交给技能包做归一化与渲染。
 */
export function buildRequirementsText(summary: RequirementsSummary): string {
  return [
    '以下为结构化需求（字段与「输入字段契约」一致，数组为逐项录入结果）：',
    '',
    '```json',
    JSON.stringify(summary, null, 2),
    '```',
  ].join('\n')
}

// ============================================================================
// 摘要 Diff（用于「回填结果确认」面板）
// ============================================================================

/** 字段 → 中文名，用于把 `technical_constraints.auth` 这类路径翻成人话 */
const FIELD_LABELS: Record<string, string> = {
  product_name: '产品名称',
  product_goal: '产品目标',
  target_users: '目标用户',
  platform: '运行平台',
  mvp_features: 'MVP 功能',
  v2_features: '未来规划',
  ui_pages: '页面结构',
  key_interactions: '关键交互',
  technical_constraints: '技术约束',
  llm_usage: 'LLM 使用',
  non_functional: '非功能要求',
  scope: '项目范围',
  name: '名称',
  description: '描述',
  user_value: '对用户的价值',
  modules: '包含模块',
  notes: '备注',
  auth: '认证方式',
  storage: '存储需求',
  frontend: '前端',
  backend: '后端',
  deployment: '部署方式',
  security: '安全',
  performance: '性能',
  availability: '可用性',
  in_scope: '范围内',
  out_of_scope: '不在范围内',
}

/** 把字段名转成中文展示名；未登记的字段原样返回 */
export function fieldLabel(key: string): string {
  return FIELD_LABELS[key] ?? key
}

/** 判断值是否「相当于没填」：undefined / null / 空串（空数组、空对象仍参与逐项比对） */
function isAbsent(value: unknown): boolean {
  return value === undefined || value === null || value === ''
}

/** 判断是否为可递归比对的普通对象（排除数组与 null） */
function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** 递归比对两个值，把差异写进 out */
function diffValues(
  before: unknown,
  after: unknown,
  path: string,
  crumbs: string[],
  out: SummaryChange[],
): void {
  const beforeAbsent = isAbsent(before)
  const afterAbsent = isAbsent(after)

  if (beforeAbsent && afterAbsent) return
  if (beforeAbsent) {
    out.push({ path, label: crumbs.join(' · '), changeType: 'added', before: null, after })
    return
  }
  if (afterAbsent) {
    out.push({ path, label: crumbs.join(' · '), changeType: 'removed', before, after: null })
    return
  }

  if (Array.isArray(before) && Array.isArray(after)) {
    const length = Math.max(before.length, after.length)
    for (let index = 0; index < length; index += 1) {
      diffValues(
        before[index],
        after[index],
        `${path}[${index}]`,
        [...crumbs, `第 ${index + 1} 项`],
        out,
      )
    }
    return
  }

  if (isPlainObject(before) && isPlainObject(after)) {
    const keys = new Set([...Object.keys(before), ...Object.keys(after)])
    for (const key of keys) {
      diffValues(before[key], after[key], path ? `${path}.${key}` : key, [...crumbs, fieldLabel(key)], out)
    }
    return
  }

  // 类型不同（如数组被整体替换成字符串）也算修改
  if (JSON.stringify(before) !== JSON.stringify(after)) {
    out.push({ path, label: crumbs.join(' · '), changeType: 'modified', before, after })
  }
}

/**
 * 比对两份结构化摘要，列出逐字段的差异。
 *
 * 递归下钻到标量级别：数组逐个下标比对、对象逐键比对，因此前端能把变更精确定位到
 * 诸如 `mvp_features[0].user_value` 这样的叶子字段。判断一字段是否变更时先做
 * `JSON.stringify` 比较，避免对象/数组引用不同但内容相同被误报。
 *
 * @param before 原始摘要（用户当前填写的）
 * @param after  回填后的摘要（后端返回的）
 * @returns 变更列表，顺序与字段遍历顺序一致；无变更时为空数组。
 */
export function diffSummaries(
  before: RequirementsSummary | null | undefined,
  after: RequirementsSummary | null | undefined,
): SummaryChange[] {
  const changes: SummaryChange[] = []
  if (!before || !after) return changes

  const keys = new Set([...Object.keys(before), ...Object.keys(after)])
  const beforeRecord = before as unknown as Record<string, unknown>
  const afterRecord = after as unknown as Record<string, unknown>
  for (const key of keys) {
    const beforeValue = beforeRecord[key]
    const afterValue = afterRecord[key]
    if (isAbsent(beforeValue) && isAbsent(afterValue)) continue
    diffValues(beforeValue, afterValue, key, [fieldLabel(key)], changes)
  }
  return changes
}

/**
 * 把摘要里的任意取值渲染成一行可读文本，供 Diff 面板展示。
 *
 * @param value 任意字段值
 * @returns 字符串；空值返回「（空）」，数组用「、」连接，对象转 JSON。
 */
export function formatSummaryValue(value: unknown): string {
  if (isAbsent(value)) return '（空）'
  if (Array.isArray(value)) {
    if (!value.length) return '（空）'
    return value.map((item) => formatSummaryValue(item)).join('、')
  }
  if (isPlainObject(value)) {
    const parts = Object.entries(value)
      .filter(([, item]) => !isAbsent(item))
      .map(([key, item]) => `${fieldLabel(key)}：${formatSummaryValue(item)}`)
    return parts.length ? parts.join('；') : '（空）'
  }
  return String(value)
}
