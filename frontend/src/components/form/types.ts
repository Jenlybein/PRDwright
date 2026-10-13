/**
 * 需求表单相关的共享类型。
 *
 * 原定义在 RequirementsForm.vue 内部，拆分出 FormFieldRenderer / MvpFeatureEditor /
 * UiPageEditor 后，这些类型被多个组件共用，故集中到本文件。
 */

/** MVP 功能的一行录入数据（GUI 行编辑器用，提交前转换成 MvpFeature） */
export interface MvpFeatureItem {
  /** 功能名称 */
  name: string
  /** 功能描述 */
  description: string
  /** 用户价值 */
  value: string
}

/** 页面结构的一行录入数据（GUI 卡片编辑器用，提交前转换成 UiPage） */
export interface UiPageItem {
  /** 页面名称 */
  name: string
  /** 已确认的模块列表（标签形式展示） */
  modules: string[]
  /** 备注 */
  notes: string
  /** 模块输入框里尚未确认的草稿值；仅界面使用，提交前合并进 modules 后丢弃 */
  moduleDraft: string
}

/**
 * 字符串列表字段的 key。
 * 这些字段全部由「列表编辑器」逐条录入，用户不需要手写任何分隔或换行格式。
 */
export type ListKey = 'target_users' | 'v2_features' | 'key_interactions' | 'scope_in' | 'scope_out'

/**
 * 表单原始输入。
 * 列表类字段一律用数组承载（逐条录入），结构化字段用行对象数组承载，
 * 界面层不出现「按换行 / 按竖线手写」的格式要求。
 */
export interface FormState {
  product_name: string
  platform: string
  product_goal: string
  /** 目标用户：逐条录入 */
  target_users: string[]
  /** MVP 功能：行编辑器 */
  mvp_features: MvpFeatureItem[]
  /** 未来规划：逐条录入 */
  v2_features: string[]
  /** 页面结构：卡片编辑器 */
  ui_pages: UiPageItem[]
  /** 关键交互：逐条录入 */
  key_interactions: string[]
  tech_auth: string
  tech_storage: string
  tech_frontend: string
  tech_backend: string
  tech_deployment: string
  llm_usage: string
  nf_security: string
  nf_performance: string
  nf_availability: string
  /** 在范围内：逐条录入 */
  scope_in: string[]
  /** 不在范围内：逐条录入 */
  scope_out: string[]
}

/** 表单控件的渲染类型 */
export type FieldType = 'input' | 'textarea' | 'select' | 'list' | 'mvp' | 'pages'

/** 单个表单字段的渲染配置 */
export interface FieldConfig {
  /** 字段 key：同时是 form 的属性名与 errors 的键 */
  key: keyof FormState
  label: string
  type: FieldType
  /** 占位提示：格式说明 + 示例；select 场景下作为空选项文案 */
  placeholder?: string
  /** 下拉选项（type 为 select 时使用） */
  options?: string[]
  /** 输入建议（type 为 input 时以 datalist 形式提供，可自由覆写） */
  suggestions?: string[]
  required?: boolean
  /** textarea 行数 */
  rows?: number
  /** 字段标题右侧的极简说明（结构化编辑器用） */
  hint?: string
  /** list 编辑器：单条输入框的占位提示 */
  itemPlaceholder?: string
  /** list 编辑器：新增按钮文案 */
  addLabel?: string
  /** list 编辑器：空状态文案 */
  emptyText?: string
}

/** 一个折叠区块的配置 */
export interface SectionConfig {
  key: string
  /** 标题徽章编号 */
  no: number
  title: string
  hint: string
  /** 区块内是否包含必填字段 */
  required?: boolean
  fields: FieldConfig[]
}
