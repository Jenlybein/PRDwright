/**
 * 表单各录入器的文本解析函数。
 *
 * 主录入路径是 GUI 编辑器，这些函数服务于「批量粘贴导入」等兜底录入方式，
 * 把旧格式的多行文本解析成与后端 Schema 对齐的结构化数据。
 */

import type { MvpFeature, UiPage } from '@/types'

/** 结构化字段分隔符：全角竖线。仅用于「批量粘贴导入」解析历史文本 */
const FIELD_SEPARATOR = '｜'

/** 模块列表分隔符：半角 / 全角逗号、顿号均可 */
const MODULE_SEPARATOR = /[,，、]/

/** 多行文本 → 字符串数组：按换行分割，逐行去首尾空白并丢弃空行 */
export function parseList(text: string): string[] {
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
}

/** 模块文本 → 模块数组：按逗号 / 顿号切分，去空白与空项 */
export function parseModules(text: string): string[] {
  return text
    .split(MODULE_SEPARATOR)
    .map((item) => item.trim())
    .filter(Boolean)
}

/** 按全角竖线拆分一行，并逐段去首尾空白 */
export function splitFields(line: string): string[] {
  return line.split(FIELD_SEPARATOR).map((part) => part.trim())
}

/**
 * 字符串数组 → 规范化数组：逐项去空白、丢弃空项、去掉完全重复项（保留首次出现）。
 * 输出直接进入 JSON，保证不会把用户误敲的空行或重复项带给后端。
 */
export function normalizeList(items: readonly string[]): string[] {
  const result: string[] = []
  for (const raw of items) {
    const text = String(raw ?? '').trim()
    if (!text || result.includes(text)) continue
    result.push(text)
  }
  return result
}

/**
 * MVP 功能文本 → 结构化功能数组。
 * 每行格式「功能名｜描述｜价值」，描述与价值可省略；
 * 输出与后端 schema 的 mvp_features 数组项对齐（name 必有，其余有值才带）。
 */
export function parseMvpFeatures(text: string): MvpFeature[] {
  return parseList(text).map((line) => {
    const [name, description, userValue] = splitFields(line)
    const feature: MvpFeature = { name: name ?? '' }
    if (description) feature.description = description
    if (userValue) feature.user_value = userValue
    return feature
  })
}

/**
 * 页面结构文本 → 结构化页面数组。
 * 每行格式「页面名｜模块1,模块2｜备注」，模块列表按逗号拆分，备注可省略；
 * 输出与后端 schema 的 ui_pages 数组项对齐。
 */
export function parsePages(text: string): UiPage[] {
  return parseList(text).map((line) => {
    const [name, modules, notes] = splitFields(line)
    const page: UiPage = {
      name: name ?? '',
      modules: parseModules(modules ?? ''),
    }
    if (notes) page.notes = notes
    return page
  })
}
