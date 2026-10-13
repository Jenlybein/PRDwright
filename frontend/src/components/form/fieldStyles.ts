/**
 * 表单控件共用的样式与错误键工具。
 *
 * 此前 controlClass / rowControlClass 散落在 RequirementsForm 内部，拆分出多个字段
 * 编辑组件后统一收敛到此，保证 input / textarea / select / list / 结构化行编辑器
 * 的视觉与配色完全一致（含暗色模式）。
 */

/**
 * 普通控件（input / textarea / select）的样式。
 * @param hasError 该字段是否存在校验错误，决定边框与聚焦环用红还是品牌色。
 */
export function controlClass(hasError: boolean): string {
  const base =
    'w-full rounded-xl border bg-slate-50/50 px-3.5 py-2.5 text-sm text-slate-900 outline-none transition placeholder:text-slate-400 hover:border-slate-300 focus:bg-white focus:ring-4 dark:bg-slate-800/50 dark:text-slate-100 dark:placeholder:text-slate-500 dark:hover:border-slate-600 dark:focus:bg-slate-800 dark:focus:ring-primary-900/50'
  const state = hasError
    ? 'border-red-400 focus:border-red-500 focus:ring-red-100 dark:border-red-500 dark:focus:border-red-500 dark:focus:ring-red-900/40'
    : 'border-slate-200 focus:border-primary-500 focus:ring-primary-100 dark:border-slate-600 dark:focus:border-primary-500 dark:focus:ring-primary-900/50'
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
