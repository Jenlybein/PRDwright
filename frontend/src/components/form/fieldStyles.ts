/**
 * 表单控件共用的样式与错误键工具。
 *
 * 此前 controlClass / rowControlClass 散落在 RequirementsForm 内部，拆分出多个字段
 * 编辑组件后统一收敛到此，保证 input / textarea / select / list / 结构化行编辑器
 * 的视觉与配色完全一致（含暗色模式）。
 *
 * 配色一律走语义令牌（bg-surface-2 / text-fg / border-line / focus:ring-accent-soft-line …），
 * 与 ui/AppInput 同一套口径；错误态用 danger 令牌，不再手写 dark: 变体。
 */

/**
 * 普通控件（input / textarea / select）的样式。
 * @param hasError 该字段是否存在校验错误，决定边框与聚焦环用红还是品牌色。
 */
export function controlClass(hasError: boolean): string {
  const base =
    'w-full rounded-xl border bg-surface-2 px-3.5 py-2.5 text-sm text-fg outline-none transition placeholder:text-subtle hover:border-line-strong focus:bg-surface focus:ring-4'
  const state = hasError
    ? 'border-danger focus:border-danger focus:ring-danger-line'
    : 'border-line focus:border-accent focus:ring-accent-soft-line'
  return `${base} ${state}`
}

/** 结构化编辑器里较小的输入框样式（与 controlClass 同一套配色，仅内边距更小） */
export function rowControlClass(hasError: boolean): string {
  return controlClass(hasError).replace('px-3.5 py-2.5', 'px-3 py-2')
}

/** 结构化行内子字段的错误键，如 `mvp_features.0.name`；同时用作输入框 id 便于定位 */
export function rowErrorKey(prefix: string, index: number, field: string): string {
  return `${prefix}.${index}.${field}`
}
