import type { MvpFeature, RequirementAnswers, RequirementsSummary } from '@/types'

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
