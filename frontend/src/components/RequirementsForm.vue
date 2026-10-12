<script setup lang="ts">
import { computed, nextTick, reactive, ref, watch } from 'vue'

import type { MvpFeature, RequirementsSummary, UiPage } from '@/types'

/**
 * 结构化需求录入表单（V2 流程的第一步）。
 *
 * 设计原则：**不让用户填格式**。所有「格式类」字段都是 GUI 编辑器，用户逐项
 * 填写，前端负责组装成与 `field-schema.json` 对齐的结构化 JSON 后抛给父组件。
 */

// ============================================================================
// 组件契约
// ============================================================================

const props = withDefaults(
  defineProps<{
    /** 初始需求摘要：用于从本地快照恢复上次填写的内容；只在挂载时生效 */
    initial?: RequirementsSummary | null
  }>(),
  { initial: null },
)

const emit = defineEmits<{
  /** 校验通过并生成结构化摘要后触发，载荷为提交给后端 Skill 的标准 JSON */
  submit: [summary: RequirementsSummary]
  /** 编辑器内容变化时触发，供父组件实时保存草稿 */
  'update:summary': [summary: RequirementsSummary]
}>()

// ============================================================================
// 表单内部类型
// ============================================================================

/** MVP 功能的一行录入数据（GUI 行编辑器用，提交前转换成 MvpFeature） */
interface MvpFeatureItem {
  /** 功能名称 */
  name: string
  /** 功能描述 */
  description: string
  /** 用户价值 */
  value: string
}

/** 页面结构的一行录入数据（GUI 卡片编辑器用，提交前转换成 UiPage） */
interface UiPageItem {
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
type ListKey = 'target_users' | 'v2_features' | 'key_interactions' | 'scope_in' | 'scope_out'

/**
 * 表单原始输入。
 * 列表类字段一律用数组承载（逐条录入），结构化字段用行对象数组承载，
 * 界面层不出现「按换行 / 按竖线手写」的格式要求。
 */
interface FormState {
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
type FieldType = 'input' | 'textarea' | 'select' | 'list' | 'mvp' | 'pages'

/** 单个表单字段的渲染配置 */
interface FieldConfig {
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
interface SectionConfig {
  key: string
  /** 标题徽章编号 */
  no: number
  title: string
  hint: string
  /** 区块内是否包含必填字段 */
  required?: boolean
  fields: FieldConfig[]
}

// ============================================================================
// 常量配置
// ============================================================================

/** 结构化字段分隔符：全角竖线。仅用于「批量粘贴导入」解析历史文本，日常录入不需要 */
const FIELD_SEPARATOR = '｜'

/** 模块列表分隔符：半角 / 全角逗号、顿号均可 */
const MODULE_SEPARATOR = /[,，、]/

/** 平台选项 */
const PLATFORM_OPTIONS = ['网页应用', '微信小程序', 'App', 'CLI']

/** 用户认证选项 */
const AUTH_OPTIONS = ['免登录', '邮箱密码登录', '第三方登录（微信 / GitHub）', '企业单点登录（SSO）']

/** 数据存储选项 */
const STORAGE_OPTIONS = ['无需存储', '浏览器本地存储', '服务端数据库', '云数据库服务']

/** 前端技术常见组合（输入建议，仍可自由填写） */
const FRONTEND_SUGGESTIONS = [
  'Vue 3 + Vite + TypeScript',
  'React + Next.js',
  '微信小程序原生',
  'Flutter',
]

/** 后端技术常见组合（输入建议，仍可自由填写） */
const BACKEND_SUGGESTIONS = [
  'Python + FastAPI',
  'Node.js + Express',
  'Go + Gin',
  'Supabase / Firebase（免自建后端）',
]

/** 部署方式常见组合（输入建议，仍可自由填写） */
const DEPLOYMENT_SUGGESTIONS = [
  'Docker 部署到云服务器',
  'Vercel / Netlify 静态托管',
  '本地运行',
  '微信小程序云开发',
]

/** 四个区块的字段配置 */
const SECTIONS: SectionConfig[] = [
  {
    key: 'product-users',
    no: 1,
    title: '产品与用户',
    hint: '必填，AI 主要依据这些信息生成文档',
    required: true,
    fields: [
      {
        key: 'product_name',
        label: '产品名称',
        type: 'input',
        placeholder: '例如：HarnessPRD、团队周报助手',
        required: true,
      },
      {
        key: 'platform',
        label: '平台',
        type: 'select',
        placeholder: '请选择运行平台',
        options: PLATFORM_OPTIONS,
        required: true,
      },
      {
        key: 'product_goal',
        label: '核心目标',
        type: 'textarea',
        placeholder:
          '要说清「解决谁的问题」与「解决成什么样」，例如：帮独立开发者在 30 分钟内把模糊想法整理成可开工的 PRD，替代口头对齐与反复返工。',
        required: true,
        rows: 3,
      },
      {
        key: 'target_users',
        label: '目标用户',
        type: 'list',
        required: true,
        hint: '逐条添加，无需换行书写',
        itemPlaceholder: '例如：独立开发者',
        addLabel: '添加用户角色',
        emptyText: '点击下方按钮添加用户角色，一个角色一条',
      },
    ],
  },
  {
    key: 'features-pages',
    no: 2,
    title: '功能与页面',
    hint: '逐项录入即可，前端自动组装成结构化数据，不需要手写任何分隔符',
    fields: [
      {
        key: 'mvp_features',
        label: 'MVP 功能',
        type: 'mvp',
        required: true,
        hint: '按字段填写，无需拼格式',
      },
      {
        key: 'v2_features',
        label: '未来规划',
        type: 'list',
        hint: '逐条添加',
        itemPlaceholder: '例如：历史版本对比',
        addLabel: '添加规划项',
        emptyText: '点击下方按钮添加后续版本打算做的事',
      },
      {
        key: 'ui_pages',
        label: '页面结构',
        type: 'pages',
        hint: '页面名称 + 模块（回车或逗号确认）+ 备注',
      },
      {
        key: 'key_interactions',
        label: '关键交互',
        type: 'list',
        hint: '逐条添加',
        itemPlaceholder: '例如：填写表单时实时生成 JSON 预览',
        addLabel: '添加交互',
        emptyText: '点击下方按钮添加关键交互',
      },
    ],
  },
  {
    key: 'technical',
    no: 3,
    title: '技术约束',
    hint: '选填；未填写的项按缺失信息处理，AI 不会擅自推断',
    fields: [
      {
        key: 'tech_auth',
        label: '用户认证',
        type: 'select',
        placeholder: '请选择认证方式',
        options: AUTH_OPTIONS,
      },
      {
        key: 'tech_storage',
        label: '数据存储',
        type: 'select',
        placeholder: '请选择存储方式',
        options: STORAGE_OPTIONS,
      },
      {
        key: 'tech_frontend',
        label: '前端技术',
        type: 'input',
        placeholder: '例如：Vue 3 + Vite + TypeScript + Tailwind CSS',
        suggestions: FRONTEND_SUGGESTIONS,
      },
      {
        key: 'tech_backend',
        label: '后端技术',
        type: 'input',
        placeholder: '例如：Python + FastAPI，或 Node.js + Express',
        suggestions: BACKEND_SUGGESTIONS,
      },
      {
        key: 'tech_deployment',
        label: '部署方式',
        type: 'input',
        placeholder: '例如：Docker 部署到云服务器 / Vercel 静态托管 / 本地运行',
        suggestions: DEPLOYMENT_SUGGESTIONS,
      },
      {
        key: 'llm_usage',
        label: 'LLM 使用说明',
        type: 'textarea',
        placeholder: '例如：调用豆包大模型生成 PRD；API Key 通过环境变量配置，不写入代码',
        rows: 3,
      },
    ],
  },
  {
    key: 'non-functional',
    no: 4,
    title: '非功能与范围',
    hint: '选填，用于补充质量要求与项目边界',
    fields: [
      {
        key: 'nf_security',
        label: '安全要求',
        type: 'textarea',
        placeholder: '例如：API Key 不落库；用户输入仅存储于本地浏览器',
        rows: 2,
      },
      {
        key: 'nf_performance',
        label: '性能要求',
        type: 'textarea',
        placeholder: '例如：首屏加载 < 2s；表单输入到 JSON 预览更新 < 100ms',
        rows: 2,
      },
      {
        key: 'nf_availability',
        label: '可用性要求',
        type: 'textarea',
        placeholder: '例如：核心功能可用性 99.9%；后端不可用时给出明确提示',
        rows: 2,
      },
      {
        key: 'scope_in',
        label: '在范围内',
        type: 'list',
        hint: '逐条添加',
        itemPlaceholder: '例如：固定格式表单录入',
        addLabel: '添加范围内项',
        emptyText: '点击下方按钮添加本版本要做的事',
      },
      {
        key: 'scope_out',
        label: '不在范围内',
        type: 'list',
        hint: '逐条添加',
        itemPlaceholder: '例如：多人协作',
        addLabel: '添加范围外项',
        emptyText: '点击下方按钮添加本版本明确不做的事',
      },
    ],
  },
]

// ============================================================================
// 表单状态
// ============================================================================

/** 空白表单初始值：初始化与「清空」共用，保证两边口径一致 */
function emptyForm(): FormState {
  return {
    product_name: '',
    platform: '',
    product_goal: '',
    target_users: [],
    mvp_features: [],
    v2_features: [],
    ui_pages: [],
    key_interactions: [],
    tech_auth: '',
    tech_storage: '',
    tech_frontend: '',
    tech_backend: '',
    tech_deployment: '',
    llm_usage: '',
    nf_security: '',
    nf_performance: '',
    nf_availability: '',
    scope_in: [],
    scope_out: [],
  }
}

/** 表单原始输入；key 与 FieldConfig.key 一致 */
const form = reactive<FormState>(emptyForm())

/** 各折叠区块的展开状态；含必填字段的前两个区块默认展开 */
const expanded = reactive<Record<string, boolean>>({
  'product-users': true,
  'features-pages': true,
  technical: false,
  'non-functional': false,
})

/** 校验错误提示；普通字段与列表字段用字段 key，结构化行用 `字段.{行号}.{子字段}` */
const errors = reactive<Record<string, string>>({})

/** JSON 预览是否展开 */
const showJsonPreview = ref(false)

/** MVP 功能的「批量粘贴导入」面板是否展开（可选兜底录入方式） */
const showFeaturePaste = ref(false)

/** MVP 功能批量粘贴的原始文本 */
const featurePasteText = ref('')

/** 页面结构的「批量粘贴导入」面板是否展开（可选兜底录入方式） */
const showPagePaste = ref(false)

/** 页面结构批量粘贴的原始文本 */
const pagePasteText = ref('')

/** 操作成功的短暂提示 */
const successTip = ref('')

/** 是否从本地快照恢复了上次填写的内容（用于顶部说明文案） */
const restored = ref(false)

/** 成功提示的定时器 */
let successTipTimer: number | undefined

// ============================================================================
// 解析 / 规范化工具函数
// ============================================================================

/**
 * 多行文本 → 字符串数组。
 * 按换行分割，逐行去首尾空白并丢弃空行。
 * 主流程已改为列表编辑器，本函数仅服务于「粘贴多行自动拆分」与批量导入。
 */
function parseList(text: string): string[] {
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
}

/** 模块文本 → 模块数组：按逗号 / 顿号切分，去空白与空项 */
function parseModules(text: string): string[] {
  return text
    .split(MODULE_SEPARATOR)
    .map((item) => item.trim())
    .filter(Boolean)
}

/** 按全角竖线拆分一行，并逐段去首尾空白 */
function splitFields(line: string): string[] {
  return line.split(FIELD_SEPARATOR).map((part) => part.trim())
}

/**
 * 字符串数组 → 规范化数组：逐项去空白、丢弃空项、去掉完全重复项（保留首次出现）。
 * 输出直接进入 JSON，保证不会把用户误敲的空行或重复项带给后端。
 */
function normalizeList(items: readonly string[]): string[] {
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
 *
 * 主录入路径是 GUI 行编辑器；本函数服务于可选的「批量粘贴导入」。
 */
function parseMvpFeatures(text: string): MvpFeature[] {
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
 *
 * 主录入路径是 GUI 卡片编辑器；本函数服务于可选的「批量粘贴导入」。
 */
function parsePages(text: string): UiPage[] {
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

/** 结构化行是否填了内容（用于折叠头计数与空行过滤） */
function rowHasContent(row: object): boolean {
  return Object.entries(row).some(([key, value]) => {
    // 模块草稿是纯界面状态，不参与「已填」判断
    if (key === 'moduleDraft') return false
    if (Array.isArray(value)) return value.length > 0
    return String(value ?? '').trim().length > 0
  })
}

/** MVP 行编辑器的一行 → schema 功能项：空白的可选字段不进摘要 */
function toSummaryFeature(item: MvpFeatureItem): MvpFeature {
  const feature: MvpFeature = { name: item.name.trim() }
  if (item.description.trim()) feature.description = item.description.trim()
  if (item.value.trim()) feature.user_value = item.value.trim()
  return feature
}

/** 页面卡片编辑器的一张卡片 → schema 页面项：空备注不进摘要 */
function toSummaryPage(item: UiPageItem): UiPage {
  const page: UiPage = { name: item.name.trim(), modules: normalizeList(item.modules) }
  if (item.notes.trim()) page.notes = item.notes.trim()
  return page
}

/**
 * 主函数：整合全部解析逻辑，输出与后端 Schema 对齐的结构化需求摘要。
 * 五个 required 字段恒定出现；可选字段仅在用户填写了对应内容时才进入摘要，
 * 避免给后端塞入大量空值。字段顺序与 schema properties 声明顺序一致。
 */
function buildRequirementsSummary(): RequirementsSummary {
  const summary: RequirementsSummary = {
    product_name: form.product_name.trim(),
    product_goal: form.product_goal.trim(),
    target_users: normalizeList(form.target_users),
    platform: form.platform,
    mvp_features: form.mvp_features.filter(rowHasContent).map(toSummaryFeature),
  }

  const v2Features = normalizeList(form.v2_features)
  if (v2Features.length > 0) summary.v2_features = v2Features

  const pages = form.ui_pages.filter(rowHasContent).map(toSummaryPage)
  if (pages.length > 0) summary.ui_pages = pages

  const interactions = normalizeList(form.key_interactions)
  if (interactions.length > 0) summary.key_interactions = interactions

  const technical: NonNullable<RequirementsSummary['technical_constraints']> = {}
  if (form.tech_auth) technical.auth = form.tech_auth
  if (form.tech_storage) technical.storage = form.tech_storage
  if (form.tech_frontend.trim()) technical.frontend = form.tech_frontend.trim()
  if (form.tech_backend.trim()) technical.backend = form.tech_backend.trim()
  if (form.tech_deployment.trim()) technical.deployment = form.tech_deployment.trim()
  if (Object.keys(technical).length > 0) summary.technical_constraints = technical

  if (form.llm_usage.trim()) summary.llm_usage = form.llm_usage.trim()

  const nonFunctional: NonNullable<RequirementsSummary['non_functional']> = {}
  if (form.nf_security.trim()) nonFunctional.security = form.nf_security.trim()
  if (form.nf_performance.trim()) nonFunctional.performance = form.nf_performance.trim()
  if (form.nf_availability.trim()) nonFunctional.availability = form.nf_availability.trim()
  if (Object.keys(nonFunctional).length > 0) summary.non_functional = nonFunctional

  const inScope = normalizeList(form.scope_in)
  const outScope = normalizeList(form.scope_out)
  if (inScope.length > 0 || outScope.length > 0) {
    summary.scope = { in_scope: inScope, out_of_scope: outScope }
  }

  return summary
}

/** 当前结构化摘要（响应式） */
const currentSummary = computed<RequirementsSummary>(() => buildRequirementsSummary())

/** 用一份结构化摘要回填表单（本地快照恢复） */
function applySummary(summary: RequirementsSummary): void {
  Object.assign(form, {
    product_name: summary.product_name ?? '',
    platform: summary.platform ?? '',
    product_goal: summary.product_goal ?? '',
    target_users: [...(summary.target_users ?? [])],
    mvp_features: (summary.mvp_features ?? []).map((feature) => ({
      name: feature.name,
      description: feature.description ?? '',
      value: feature.user_value ?? '',
    })),
    v2_features: [...(summary.v2_features ?? [])],
    ui_pages: (summary.ui_pages ?? []).map((page) => ({
      name: page.name,
      modules: [...page.modules],
      notes: page.notes ?? '',
      moduleDraft: '',
    })),
    key_interactions: [...(summary.key_interactions ?? [])],
    tech_auth: summary.technical_constraints?.auth ?? '',
    tech_storage: summary.technical_constraints?.storage ?? '',
    tech_frontend: summary.technical_constraints?.frontend ?? '',
    tech_backend: summary.technical_constraints?.backend ?? '',
    tech_deployment: summary.technical_constraints?.deployment ?? '',
    llm_usage: summary.llm_usage ?? '',
    nf_security: summary.non_functional?.security ?? '',
    nf_performance: summary.non_functional?.performance ?? '',
    nf_availability: summary.non_functional?.availability ?? '',
    scope_in: [...(summary.scope?.in_scope ?? [])],
    scope_out: [...(summary.scope?.out_of_scope ?? [])],
  })
}

/**
 * 初始值回填：`props.initial` 在 setup 阶段就已可读，因此**同步**回填，
 * 让首次渲染就带上内容。若放到 `onMounted` 里改状态，Vue 的更新会被排进微任务，
 * 首帧会先渲染一遍空表单（视觉上会闪一下，同步读取 DOM 时也拿不到值）。
 */
if (props.initial) {
  applySummary(props.initial)
  restored.value = true
}

// 任何输入变化都把最新摘要抛给父组件，供其保存草稿。
// 注册在初始值回填之后，避免挂载时无谓地抛一次 update:summary。
watch(currentSummary, (value) => emit('update:summary', value))

// ============================================================================
// JSON 预览
// ============================================================================

/** 实时 JSON 预览：表单任意变化都会重新生成（computed 自动追踪依赖） */
const jsonPreview = computed(() => JSON.stringify(currentSummary.value, null, 2))

/** 预览字符数，展示在预览面板头部 */
const jsonSize = computed(() => `${jsonPreview.value.length} 字符`)

/** 复制 JSON 到剪贴板；不安全上下文等失败场景降级为提示手动复制 */
async function copyJson(): Promise<void> {
  try {
    await navigator.clipboard.writeText(jsonPreview.value)
    flashSuccessTip('JSON 已复制到剪贴板')
  } catch {
    flashSuccessTip('复制失败，请手动选择文本复制')
  }
}

// ============================================================================
// 结构化编辑器：行级错误键
// ============================================================================

/** 结构化行内子字段的错误键，如 `mvp_features.0.name`；同时用作输入框 id 便于定位 */
function rowErrorKey(prefix: string, index: number, field: string): string {
  return `${prefix}.${index}.${field}`
}

/** 把焦点交给指定错误键对应的输入框 */
async function focusRowInput(key: string): Promise<void> {
  await nextTick()
  document.getElementById(`field-${key}`)?.focus()
}

// ============================================================================
// 字符串列表编辑器（目标用户 / 未来规划 / 关键交互 / 范围）
// ============================================================================

/** 追加一个空列表项，并把焦点交给新行 */
async function addListItem(key: ListKey): Promise<void> {
  form[key].push('')
  await focusRowInput(`${key}.${form[key].length - 1}`)
}

/** 删除一个列表项 */
function removeListItem(key: ListKey, index: number): void {
  form[key].splice(index, 1)
  clearError(key)
}

/** 列表项输入回写（显式写法避免数组下标的联合类型告警） */
function setListItem(key: ListKey, index: number, value: string): void {
  const items = form[key]
  if (index < 0 || index >= items.length) return
  items[index] = value
  clearError(key)
}

/**
 * 列表行内粘贴多行文本：自动拆成多条，用户不必先自己整理格式。
 * 单行粘贴不拦截，交给浏览器默认行为。
 */
function handleListPaste(event: ClipboardEvent, key: ListKey, index: number): void {
  const text = event.clipboardData?.getData('text') ?? ''
  if (!text || !/[\r\n]/.test(text)) return
  event.preventDefault()
  const items = parseList(text)
  if (items.length === 0) return
  form[key].splice(index, 1, ...items)
  clearError(key)
}

// ============================================================================
// MVP 功能行编辑器
// ============================================================================

/** 添加一个空白功能行，并把焦点交给新行的功能名称输入框 */
async function addFeature(): Promise<void> {
  form.mvp_features.push({ name: '', description: '', value: '' })
  await focusRowInput(rowErrorKey('mvp_features', form.mvp_features.length - 1, 'name'))
}

/** 删除指定下标的功能行 */
function removeFeature(index: number): void {
  form.mvp_features.splice(index, 1)
  clearError('mvp_features')
}

/** 批量粘贴导入：把「功能名｜描述｜价值」多行文本解析成结构化行，追加到列表末尾 */
function importFeatures(): void {
  const imported = parseMvpFeatures(featurePasteText.value)
  if (imported.length === 0) return
  for (const feature of imported) {
    form.mvp_features.push({
      name: feature.name,
      description: feature.description ?? '',
      value: feature.user_value ?? '',
    })
  }
  featurePasteText.value = ''
  showFeaturePaste.value = false
  clearError('mvp_features')
  flashSuccessTip(`已导入 ${imported.length} 个功能`)
}

// ============================================================================
// 页面结构卡片编辑器
// ============================================================================

/** 添加一张空白页面卡片，并把焦点交给页面名称输入框 */
async function addPage(): Promise<void> {
  form.ui_pages.push({ name: '', modules: [], notes: '', moduleDraft: '' })
  await focusRowInput(rowErrorKey('ui_pages', form.ui_pages.length - 1, 'name'))
}

/** 删除一张页面卡片 */
function removePage(index: number): void {
  form.ui_pages.splice(index, 1)
  clearError('ui_pages')
}

/** 把某张卡片模块输入框里的草稿确认为标签（支持一次输入多个，逗号分隔） */
function addModule(index: number): void {
  const page = form.ui_pages[index]
  if (!page) return
  const modules = parseModules(page.moduleDraft)
  if (modules.length === 0) return
  for (const module of modules) {
    if (!page.modules.includes(module)) page.modules.push(module)
  }
  page.moduleDraft = ''
  clearError(rowErrorKey('ui_pages', index, 'modules'))
}

/** 删除某个模块标签 */
function removeModule(index: number, moduleIndex: number): void {
  form.ui_pages[index]?.modules.splice(moduleIndex, 1)
  clearError(rowErrorKey('ui_pages', index, 'modules'))
}

/** 模块输入框键盘事件：回车 / 逗号即确认标签（输入法组合中不拦截） */
function onModuleKeydown(event: KeyboardEvent, index: number): void {
  if (event.isComposing) return
  const key = event.key
  if (key === 'Enter' || key === ',' || key === '，' || key === '、') {
    event.preventDefault()
    addModule(index)
  }
}

/** 模块输入框内粘贴：多行或逗号分隔的文本直接拆成多个标签 */
function handleModulePaste(event: ClipboardEvent, index: number): void {
  const text = event.clipboardData?.getData('text') ?? ''
  if (!text) return
  const modules = parseModules(text)
  if (modules.length === 0) return
  if (modules.length === 1 && !/[\r\n]/.test(text)) return
  const page = form.ui_pages[index]
  if (!page) return
  event.preventDefault()
  for (const module of modules) {
    if (!page.modules.includes(module)) page.modules.push(module)
  }
  page.moduleDraft = ''
  clearError(rowErrorKey('ui_pages', index, 'modules'))
}

/**
 * 提交前把所有未回车的模块草稿入库。
 * 避免用户「输入了模块但忘了回车」导致内容被静默丢弃。
 */
function commitModuleDrafts(): void {
  form.ui_pages.forEach((_, index) => addModule(index))
}

/** 批量粘贴导入：把「页面名｜模块1,模块2｜备注」多行文本解析成页面卡片 */
function importPages(): void {
  const imported = parsePages(pagePasteText.value)
  if (imported.length === 0) return
  for (const page of imported) {
    form.ui_pages.push({
      name: page.name,
      modules: normalizeList(page.modules),
      notes: page.notes ?? '',
      moduleDraft: '',
    })
  }
  pagePasteText.value = ''
  showPagePaste.value = false
  clearError('ui_pages')
  flashSuccessTip(`已导入 ${imported.length} 个页面`)
}

// ============================================================================
// 表单校验
// ============================================================================

/** 记录一条错误 */
function setError(key: string, message: string): void {
  errors[key] = message
}

/** 用户重新输入时清除对应字段的错误，避免打字过程中一直标红 */
function clearError(key: string): void {
  if (errors[key]) delete errors[key]
}

/**
 * 全量校验：必填 → 完整性 → 范围。
 * 通过返回 true；不通过时把错误写入 errors 并返回 false。
 */
function validate(): boolean {
  // 重新校验前清空旧错误，保证结果只反映当前输入
  for (const key of Object.keys(errors)) delete errors[key]
  // 未回车的模块草稿先入库，再参与校验
  commitModuleDrafts()

  // 1) 必填校验：产品名称、平台、核心目标、目标用户、MVP 功能
  if (!form.product_name.trim()) setError('product_name', '产品名称为必填项')
  if (!form.platform) setError('platform', '请选择运行平台')
  if (!form.product_goal.trim()) setError('product_goal', '核心目标为必填项')
  if (normalizeList(form.target_users).length === 0) {
    setError('target_users', '目标用户为必填项，至少添加一个用户角色')
  }
  if (form.mvp_features.filter(rowHasContent).length === 0) {
    setError('mvp_features', '至少添加一个 MVP 功能')
  }
  form.mvp_features.forEach((item, index) => {
    if (!item.name.trim()) {
      setError(
        rowErrorKey('mvp_features', index, 'name'),
        `第 ${index + 1} 个功能：功能名称为必填项`,
      )
    }
    if (!item.description.trim()) {
      setError(
        rowErrorKey('mvp_features', index, 'description'),
        `第 ${index + 1} 个功能：功能描述为必填项`,
      )
    }
  })

  // 2) 完整性校验：每张页面卡片都要有页面名称与至少一个模块
  form.ui_pages.forEach((page, index) => {
    if (!page.name.trim()) {
      setError(rowErrorKey('ui_pages', index, 'name'), `第 ${index + 1} 个页面：页面名称为必填项`)
    }
    if (normalizeList(page.modules).length === 0) {
      setError(
        rowErrorKey('ui_pages', index, 'modules'),
        `第 ${index + 1} 个页面：请至少添加一个模块`,
      )
    }
  })

  // 3) 范围校验：在范围内 / 不在范围内不能出现相同内容
  const inScope = normalizeList(form.scope_in)
  const outScope = normalizeList(form.scope_out)
  const overlap = inScope.filter((item) => outScope.includes(item))
  if (overlap.length > 0) {
    const message = `「${overlap.join('、')}」同时出现在范围内与范围外，请只保留在一处`
    setError('scope_in', message)
    setError('scope_out', message)
  }

  return Object.keys(errors).length === 0
}

// ============================================================================
// 交互动作
// ============================================================================

/** 短暂展示成功提示，随后自动淡出 */
function flashSuccessTip(text: string): void {
  successTip.value = text
  window.clearTimeout(successTipTimer)
  successTipTimer = window.setTimeout(() => {
    successTip.value = ''
  }, 2500)
}

/** 提交：校验 → 生成结构化摘要 → 抛给父组件，并自动展开 JSON 预览供核对 */
function handleSubmit(): void {
  if (!validate()) {
    // 展开所有含错误的区块，并滚动到第一个错误字段，保证用户看得到
    for (const section of SECTIONS) {
      if (hasError(section)) {
        expanded[section.key] = true
      }
    }
    const firstErrorKey = Object.keys(errors)[0]
    if (firstErrorKey) {
      const firstErrorEl = document.getElementById(`field-${firstErrorKey}`)
      if (firstErrorEl && typeof firstErrorEl.scrollIntoView === 'function') {
        firstErrorEl.scrollIntoView({ behavior: 'smooth', block: 'center' })
      }
    }
    return
  }

  const summary = buildRequirementsSummary()
  emit('submit', summary)
  // 提交成功后自动展开 JSON 预览，方便核对即将交给后端的数据
  showJsonPreview.value = true
  flashSuccessTip('校验通过，已生成结构化 JSON')
}

/** 填充示例数据：与各字段提示中的示例一致，便于快速体验与排查 */
function fillExample(): void {
  applySummary({
    product_name: 'HarnessPRD',
    platform: '网页应用',
    product_goal:
      '帮独立开发者与小团队在 30 分钟内把模糊的产品想法整理成可开工的 PRD，替代口头对齐与反复返工。',
    target_users: ['独立开发者', '3-10 人小团队的产品经理'],
    mvp_features: [
      { name: '问卷填写', description: '分基础与进阶两组收集产品信息', user_value: '降低填写门槛' },
      { name: 'JSON 预览', description: '填写过程实时生成结构化 JSON', user_value: '便于排查格式问题' },
      { name: '格式校验', description: '提交前检查必填项与字段完整性', user_value: '减少后端报错' },
    ],
    v2_features: ['历史版本对比', '多人协作编辑'],
    ui_pages: [
      { name: '首页', modules: ['产品介绍', '快速开始'], notes: '面向新用户' },
      { name: '表单页', modules: ['分组表单', 'JSON 预览'], notes: '核心录入页' },
    ],
    key_interactions: ['填写表单时实时生成 JSON 预览', '提交时高亮所有校验失败的字段'],
    technical_constraints: {
      auth: '免登录',
      storage: '浏览器本地存储',
      frontend: 'Vue 3 + Vite + TypeScript + Tailwind CSS',
      backend: 'Python + FastAPI',
      deployment: 'Docker 部署到云服务器',
    },
    llm_usage: '调用豆包大模型生成 PRD；API Key 通过环境变量配置，不写入代码',
    non_functional: {
      security: 'API Key 不落库；用户输入仅存储于本地浏览器',
      performance: '首屏加载 < 2s；表单输入到 JSON 预览更新 < 100ms',
      availability: '核心功能可用性 99.9%；后端不可用时给出明确提示',
    },
    scope: {
      in_scope: ['固定格式表单录入', 'JSON 实时预览', '基础格式校验'],
      out_of_scope: ['动态 JSON Schema 渲染', '多人协作', '登录系统'],
    },
  })
  // 同步清除错误，让示例处于可直接提交的状态
  for (const key of Object.keys(errors)) delete errors[key]
}

/** 清空全部输入与错误 */
function resetForm(): void {
  Object.assign(form, emptyForm())
  for (const key of Object.keys(errors)) delete errors[key]
  showFeaturePaste.value = false
  showPagePaste.value = false
  featurePasteText.value = ''
  pagePasteText.value = ''
  restored.value = false
}

/** 普通控件（input / textarea / select）需要 label 的 for 指向控件本身 */
function isPlainField(field: FieldConfig): boolean {
  return field.type === 'input' || field.type === 'textarea' || field.type === 'select'
}

/** 取列表字段的数组值（模板中收窄类型用） */
function listValue(key: keyof FormState): string[] {
  return form[key] as string[]
}

/** 某字段是否已填写（用于折叠头计数） */
function isFieldFilled(field: FieldConfig): boolean {
  const value = form[field.key]
  if (Array.isArray(value)) {
    return value.some((item) =>
      typeof item === 'string' ? item.trim().length > 0 : rowHasContent(item),
    )
  }
  return String(value ?? '').trim().length > 0
}

/** 某区块已填写字段数（折叠时展示，提示面板里有内容） */
function filledCount(section: SectionConfig): number {
  return section.fields.filter(isFieldFilled).length
}

/** 某区块是否存在校验错误（折叠时展示红色标记；含结构化行的子字段错误） */
function hasError(section: SectionConfig): boolean {
  const errorKeys = Object.keys(errors)
  return section.fields.some((field) =>
    errorKeys.some((key) => key === field.key || key.startsWith(`${field.key}.`)),
  )
}

/** 表单控件样式：有错误时切换为红色边框与红色聚焦环 */
function controlClass(key: string): string {
  const base =
    'w-full rounded-xl border bg-slate-50/50 px-3.5 py-2.5 text-sm text-slate-900 outline-none transition placeholder:text-slate-400 hover:border-slate-300 focus:bg-white focus:ring-4'
  const state = errors[key]
    ? 'border-red-400 focus:border-red-500 focus:ring-red-100'
    : 'border-slate-200 focus:border-primary-500 focus:ring-primary-100'
  return `${base} ${state}`
}

/** 结构化编辑器里较小的输入框样式（与 controlClass 同一套配色） */
function rowControlClass(key: string): string {
  return controlClass(key).replace('px-3.5 py-2.5', 'px-3 py-2')
}

/** 暴露给父组件：读取当前摘要（父组件需要时可直接取，无需等 submit） */
defineExpose({ getSummary: () => currentSummary.value })
</script>

<template>
  <div>
    <!-- 页面标题与说明 -->
    <div class="mb-6">
      <h1 class="text-2xl font-bold tracking-tight text-slate-900 sm:text-3xl">
        告诉我们你的产品想法
      </h1>
      <p class="mt-2 max-w-xl text-sm leading-relaxed text-slate-600">
        按字段逐项填写下面的表单，列表类内容逐条添加即可，<span class="font-medium text-slate-700"
          >不需要自己拼格式或写分隔符</span
        >，前端会实时转换成后端 Skill 可直接使用的结构化 JSON。带
        <span class="text-red-500">*</span> 的字段为必填。
        <template v-if="restored">已为你恢复上次填写的内容，可以继续编辑。</template>
      </p>
    </div>

    <!-- 操作栏：JSON 预览开关 / 示例 / 清空 -->
    <div class="mb-6 flex flex-wrap items-center gap-2">
      <button
        type="button"
        class="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-600 shadow-sm transition hover:border-primary-400 hover:text-primary-700"
        @click="showJsonPreview = !showJsonPreview"
      >
        <svg class="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
          <path
            fill-rule="evenodd"
            d="M6.28 3.22a.75.75 0 0 1 0 1.06L2.56 8l3.72 3.72a.75.75 0 0 1-1.06 1.06L.97 8.53a.75.75 0 0 1 0-1.06l4.25-4.25a.75.75 0 0 1 1.06 0Zm7.44 0a.75.75 0 0 1 1.06 0l4.25 4.25a.75.75 0 0 1 0 1.06l-4.25 4.25a.75.75 0 0 1-1.06-1.06L17.44 8l-3.72-3.72a.75.75 0 0 1 0-1.06Z"
            clip-rule="evenodd"
          />
        </svg>
        {{ showJsonPreview ? '关闭 JSON 预览' : '查看 JSON 预览' }}
      </button>
      <button
        type="button"
        class="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-600 shadow-sm transition hover:border-primary-400 hover:text-primary-700"
        @click="fillExample"
      >
        填充示例
      </button>
      <button
        type="button"
        class="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-600 shadow-sm transition hover:border-red-300 hover:text-red-600"
        @click="resetForm"
      >
        清空
      </button>

      <!-- 操作成功的短暂提示，自动淡出 -->
      <Transition
        enter-active-class="transition duration-200"
        enter-from-class="opacity-0 translate-y-1"
        leave-active-class="transition duration-500"
        leave-to-class="opacity-0"
      >
        <span
          v-if="successTip"
          class="ml-auto flex items-center gap-1 rounded-full bg-emerald-50 px-3 py-1 text-xs font-medium text-emerald-600 ring-1 ring-emerald-200"
        >
          <svg class="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
            <path
              fill-rule="evenodd"
              d="M10 18a8 8 0 1 0 0-16 8 8 0 0 0 0 16Zm3.857-9.809a.75.75 0 0 0-1.214-.882l-3.483 4.79-1.88-1.88a.75.75 0 1 1-1.06 1.061l2.5 2.5a.75.75 0 0 0 1.137-.089l4-5.5Z"
              clip-rule="evenodd"
            />
          </svg>
          {{ successTip }}
        </span>
      </Transition>
    </div>

    <!-- JSON 预览面板：实时反映表单内容，等宽字体便于阅读与排查 -->
    <section
      v-if="showJsonPreview"
      class="mb-6 overflow-hidden rounded-2xl border border-slate-200/80 bg-white shadow-sm"
    >
      <div class="flex items-center justify-between gap-3 border-b border-slate-100 px-5 py-3">
        <div class="min-w-0">
          <h2 class="text-sm font-semibold text-slate-900">结构化 JSON 预览</h2>
          <p class="mt-0.5 text-xs text-slate-500">
            实时反映表单内容 · 与后端 Skill 字段结构一致 · {{ jsonSize }}
          </p>
        </div>
        <button
          type="button"
          class="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-600 transition hover:border-primary-400 hover:text-primary-700"
          @click="copyJson"
        >
          <svg class="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
            <path
              d="M7 3.5A1.5 1.5 0 0 1 8.5 2h3.879a1.5 1.5 0 0 1 1.06.44l3.122 3.12A1.5 1.5 0 0 1 17 6.622V12.5A1.5 1.5 0 0 1 15.5 14h-1v-3.379a3 3 0 0 0-.879-2.121L10.5 5.379A3 3 0 0 0 8.379 4.5H7v-1Z"
            />
            <path
              d="M4.5 6A1.5 1.5 0 0 0 3 7.5v9A1.5 1.5 0 0 0 4.5 18h7a1.5 1.5 0 0 0 1.5-1.5v-5.879a1.5 1.5 0 0 0-.44-1.06L9.44 6.439A1.5 1.5 0 0 0 8.378 6H4.5Z"
            />
          </svg>
          复制 JSON
        </button>
      </div>
      <pre
        class="max-h-96 overflow-auto bg-slate-50 px-5 py-4 font-mono text-xs leading-relaxed text-slate-700"
        >{{ jsonPreview }}</pre
      >
    </section>

    <!-- 结构化录入表单 -->
    <form class="space-y-4" novalidate @submit.prevent="handleSubmit">
      <!-- 折叠区块 -->
      <section
        v-for="section in SECTIONS"
        :key="section.key"
        class="rounded-2xl border border-slate-200/80 bg-white shadow-sm shadow-slate-200/50"
      >
        <!-- 区块标题：点击折叠 / 展开 -->
        <button
          type="button"
          class="flex w-full items-center justify-between gap-4 rounded-2xl px-5 py-4 text-left transition hover:bg-slate-50 sm:px-6"
          :aria-expanded="expanded[section.key]"
          @click="expanded[section.key] = !expanded[section.key]"
        >
          <span class="flex items-center gap-3">
            <span
              class="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-primary-100 text-sm font-bold text-primary-700"
            >
              {{ section.no }}
            </span>
            <span class="min-w-0">
              <span class="flex flex-wrap items-center gap-2 text-base font-semibold text-slate-900">
                {{ section.title }}
                <span
                  v-if="section.required"
                  class="rounded-full bg-red-50 px-2 py-0.5 text-[11px] font-medium text-red-500 ring-1 ring-red-200"
                  >必填</span
                >
                <span
                  v-if="!expanded[section.key] && hasError(section)"
                  class="rounded-full bg-red-50 px-2 py-0.5 text-[11px] font-medium text-red-500 ring-1 ring-red-200"
                  >有错误</span
                >
              </span>
              <span class="block text-xs text-slate-500">
                {{ section.hint }}
                <template v-if="!expanded[section.key]">
                  · 已填 {{ filledCount(section) }}/{{ section.fields.length }}
                </template>
              </span>
            </span>
          </span>
          <svg
            class="h-4 w-4 shrink-0 text-slate-400 transition-transform"
            :class="expanded[section.key] ? 'rotate-180' : ''"
            viewBox="0 0 20 20"
            fill="currentColor"
            aria-hidden="true"
          >
            <path
              fill-rule="evenodd"
              d="M5.23 7.21a.75.75 0 0 1 1.06.02L10 11.17l3.71-3.94a.75.75 0 1 1 1.08 1.04l-4.25 4.5a.75.75 0 0 1-1.08 0l-4.25-4.5a.75.75 0 0 1 .02-1.06Z"
              clip-rule="evenodd"
            />
          </svg>
        </button>

        <!-- 区块字段 -->
        <div
          v-show="expanded[section.key]"
          class="space-y-5 border-t border-slate-100 px-5 pb-6 pt-5 sm:px-6"
        >
          <div v-for="field in section.fields" :key="field.key">
            <!-- 普通控件：label 关联到控件 -->
            <label
              v-if="isPlainField(field)"
              :for="`field-${field.key}`"
              class="mb-2 block text-sm font-medium text-slate-800"
            >
              {{ field.label }}
              <span v-if="field.required" class="ml-0.5 text-red-500" aria-hidden="true">*</span>
            </label>

            <!-- 结构化编辑器：整体作为一组，label 不加 for -->
            <div v-else class="mb-2 flex flex-wrap items-baseline gap-x-2">
              <span class="text-sm font-medium text-slate-800">
                {{ field.label }}
                <span v-if="field.required" class="ml-0.5 text-red-500" aria-hidden="true">*</span>
              </span>
              <span v-if="field.hint" class="text-xs text-slate-400">{{ field.hint }}</span>
            </div>

            <!-- 单行文本（含输入建议，仍可自由填写） -->
            <template v-if="field.type === 'input'">
              <input
                :id="`field-${field.key}`"
                v-model="form[field.key] as string"
                type="text"
                :class="controlClass(field.key)"
                :placeholder="field.placeholder"
                :list="field.suggestions ? `${field.key}-suggestions` : undefined"
                @input="clearError(field.key)"
              />
              <!-- 常见选项建议：点击选择或直接输入自定义值 -->
              <datalist v-if="field.suggestions" :id="`${field.key}-suggestions`">
                <option v-for="option in field.suggestions" :key="option" :value="option" />
              </datalist>
            </template>

            <!-- 多行文本 -->
            <textarea
              v-else-if="field.type === 'textarea'"
              :id="`field-${field.key}`"
              v-model="form[field.key] as string"
              :rows="field.rows ?? 4"
              :class="controlClass(field.key)"
              :placeholder="field.placeholder"
              @input="clearError(field.key)"
            ></textarea>

            <!-- 下拉选择（自定义箭头） -->
            <div v-else-if="field.type === 'select'" class="relative">
              <select
                :id="`field-${field.key}`"
                v-model="form[field.key] as string"
                :class="controlClass(field.key) + ' appearance-none pr-10'"
                @change="clearError(field.key)"
              >
                <option value="">{{ field.placeholder }}</option>
                <option v-for="option in field.options ?? []" :key="option" :value="option">
                  {{ option }}
                </option>
              </select>
              <svg
                class="pointer-events-none absolute right-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400"
                viewBox="0 0 20 20"
                fill="currentColor"
                aria-hidden="true"
              >
                <path
                  fill-rule="evenodd"
                  d="M5.23 7.21a.75.75 0 0 1 1.06.02L10 11.17l3.71-3.94a.75.75 0 1 1 1.08 1.04l-4.25 4.5a.75.75 0 0 1-1.08 0l-4.25-4.5a.75.75 0 0 1 .02-1.06Z"
                  clip-rule="evenodd"
                />
              </svg>
            </div>

            <!--
              字符串列表编辑器（目标用户 / 未来规划 / 关键交互 / 范围）。
              一条一行、逐条添加，用户不需要关心换行或分隔符；粘贴多行会自动拆分。
            -->
            <div
              v-else-if="field.type === 'list'"
              :id="`field-${field.key}`"
              class="space-y-2"
              role="group"
              :aria-label="field.label"
            >
              <p
                v-if="listValue(field.key).length === 0"
                class="rounded-xl border border-dashed border-slate-300 bg-slate-50/50 px-4 py-5 text-center text-xs text-slate-400"
              >
                {{ field.emptyText }}
              </p>

              <div
                v-for="(item, index) in listValue(field.key)"
                :key="index"
                class="flex items-center gap-2"
              >
                <span class="w-5 shrink-0 text-center text-xs font-medium text-slate-400">
                  {{ index + 1 }}
                </span>
                <input
                  :id="`field-${field.key}.${index}`"
                  :value="item"
                  type="text"
                  :class="rowControlClass(field.key)"
                  :placeholder="field.itemPlaceholder"
                  @input="
                    setListItem(
                      field.key as ListKey,
                      index,
                      ($event.target as HTMLInputElement).value,
                    )
                  "
                  @paste="handleListPaste($event, field.key as ListKey, index)"
                />
                <button
                  type="button"
                  title="删除该条"
                  class="shrink-0 rounded-md px-2 py-1 text-xs font-medium text-slate-400 transition hover:bg-red-50 hover:text-red-600"
                  @click="removeListItem(field.key as ListKey, index)"
                >
                  删除
                </button>
              </div>

              <button
                type="button"
                class="w-full rounded-xl border border-dashed border-slate-300 px-4 py-2.5 text-xs font-medium text-slate-500 transition hover:border-primary-400 hover:text-primary-600"
                @click="addListItem(field.key as ListKey)"
              >
                ＋ {{ field.addLabel }}
              </button>
            </div>

            <!--
              MVP 功能：结构化行编辑器。
              用户按字段逐项填写，无需手写分隔符格式，前端直接组装结构化数据。
            -->
            <div
              v-else-if="field.type === 'mvp'"
              :id="`field-${field.key}`"
              class="space-y-2.5"
              role="group"
              :aria-label="field.label"
            >
              <!-- 空状态引导 -->
              <div
                v-if="form.mvp_features.length === 0"
                class="rounded-xl border border-dashed border-slate-300 bg-slate-50/50 px-4 py-6 text-center text-xs text-slate-400"
              >
                还没有功能项，点击下方「添加功能」逐项填写
              </div>

              <!-- 功能行卡片 -->
              <div
                v-for="(item, index) in form.mvp_features"
                :key="index"
                class="rounded-xl border border-slate-200 bg-slate-50/40 p-3.5"
              >
                <div class="mb-2.5 flex items-center justify-between">
                  <span class="text-xs font-semibold text-slate-500">功能 {{ index + 1 }}</span>
                  <button
                    type="button"
                    title="删除该功能"
                    class="rounded-md px-2 py-1 text-xs font-medium text-slate-400 transition hover:bg-red-50 hover:text-red-600"
                    @click="removeFeature(index)"
                  >
                    删除
                  </button>
                </div>

                <div class="space-y-2.5">
                  <div>
                    <label
                      :for="`field-mvp_features.${index}.name`"
                      class="mb-1.5 block text-xs font-medium text-slate-700"
                    >
                      功能名称
                      <span class="text-red-500" aria-hidden="true">*</span>
                    </label>
                    <input
                      :id="`field-mvp_features.${index}.name`"
                      v-model="item.name"
                      type="text"
                      :class="rowControlClass(rowErrorKey('mvp_features', index, 'name'))"
                      placeholder="例如：问卷填写"
                      @input="clearError(rowErrorKey('mvp_features', index, 'name'))"
                    />
                    <p
                      v-if="errors[rowErrorKey('mvp_features', index, 'name')]"
                      class="mt-1.5 flex items-start gap-1.5 text-xs font-medium text-red-600"
                    >
                      <svg
                        class="mt-0.5 h-3.5 w-3.5 shrink-0"
                        viewBox="0 0 20 20"
                        fill="currentColor"
                        aria-hidden="true"
                      >
                        <path
                          fill-rule="evenodd"
                          d="M18 10A8 8 0 1 1 2 10a8 8 0 0 1 16 0Zm-8-4a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5A.75.75 0 0 1 10 6Zm0 8a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z"
                          clip-rule="evenodd"
                        />
                      </svg>
                      <span>{{ errors[rowErrorKey('mvp_features', index, 'name')] }}</span>
                    </p>
                  </div>

                  <div>
                    <label
                      :for="`field-mvp_features.${index}.description`"
                      class="mb-1.5 block text-xs font-medium text-slate-700"
                    >
                      功能描述
                      <span class="text-red-500" aria-hidden="true">*</span>
                    </label>
                    <input
                      :id="`field-mvp_features.${index}.description`"
                      v-model="item.description"
                      type="text"
                      :class="rowControlClass(rowErrorKey('mvp_features', index, 'description'))"
                      placeholder="例如：分基础与进阶两组收集产品信息"
                      @input="clearError(rowErrorKey('mvp_features', index, 'description'))"
                    />
                    <p
                      v-if="errors[rowErrorKey('mvp_features', index, 'description')]"
                      class="mt-1.5 flex items-start gap-1.5 text-xs font-medium text-red-600"
                    >
                      <svg
                        class="mt-0.5 h-3.5 w-3.5 shrink-0"
                        viewBox="0 0 20 20"
                        fill="currentColor"
                        aria-hidden="true"
                      >
                        <path
                          fill-rule="evenodd"
                          d="M18 10A8 8 0 1 1 2 10a8 8 0 0 1 16 0Zm-8-4a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5A.75.75 0 0 1 10 6Zm0 8a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z"
                          clip-rule="evenodd"
                        />
                      </svg>
                      <span>{{ errors[rowErrorKey('mvp_features', index, 'description')] }}</span>
                    </p>
                  </div>

                  <div>
                    <label
                      :for="`field-mvp_features.${index}.value`"
                      class="mb-1.5 block text-xs font-medium text-slate-700"
                    >
                      价值（选填）
                    </label>
                    <input
                      :id="`field-mvp_features.${index}.value`"
                      v-model="item.value"
                      type="text"
                      :class="rowControlClass(`mvp_features.${index}.value`)"
                      placeholder="例如：降低填写门槛"
                    />
                  </div>
                </div>
              </div>

              <!-- 添加功能行 -->
              <button
                type="button"
                class="w-full rounded-xl border border-dashed border-slate-300 px-4 py-2.5 text-xs font-medium text-slate-500 transition hover:border-primary-400 hover:text-primary-600"
                @click="addFeature"
              >
                ＋ 添加功能
              </button>

              <!-- 批量粘贴导入（可选）：兼容「功能名｜描述｜价值」旧格式文本 -->
              <div class="pt-0.5">
                <button
                  type="button"
                  class="text-xs text-slate-400 underline-offset-2 transition hover:text-primary-600 hover:underline"
                  @click="showFeaturePaste = !showFeaturePaste"
                >
                  {{ showFeaturePaste ? '收起批量粘贴' : '批量粘贴导入（可选）' }}
                </button>
                <div v-if="showFeaturePaste" class="mt-2 space-y-2">
                  <textarea
                    v-model="featurePasteText"
                    rows="3"
                    class="w-full resize-y rounded-xl border border-slate-200 bg-slate-50/50 px-3.5 py-2.5 text-sm leading-relaxed text-slate-900 outline-none transition placeholder:text-slate-400 hover:border-slate-300 focus:border-primary-500 focus:bg-white focus:ring-4 focus:ring-primary-100"
                    placeholder="每行一个功能：功能名｜描述｜价值，例如：&#10;问卷填写｜分基础与进阶两组收集产品信息｜降低填写门槛"
                  ></textarea>
                  <button
                    type="button"
                    class="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-600 shadow-sm transition hover:border-primary-400 hover:text-primary-700 disabled:cursor-not-allowed disabled:opacity-50"
                    :disabled="!featurePasteText.trim()"
                    @click="importFeatures"
                  >
                    解析并导入
                  </button>
                </div>
              </div>
            </div>

            <!--
              页面结构：卡片 + 模块标签编辑器。
              页面名称、模块、备注各自独立输入；模块回车/逗号即成标签，无需手写格式。
            -->
            <div
              v-else-if="field.type === 'pages'"
              :id="`field-${field.key}`"
              class="space-y-3"
              role="group"
              :aria-label="field.label"
            >
              <!-- 空状态引导 -->
              <div
                v-if="form.ui_pages.length === 0"
                class="rounded-xl border border-dashed border-slate-300 bg-slate-50/50 px-4 py-6 text-center text-xs text-slate-400"
              >
                还没有页面，点击下方「添加页面」逐张填写
              </div>

              <!-- 页面卡片 -->
              <div
                v-for="(page, index) in form.ui_pages"
                :key="index"
                class="rounded-xl border border-slate-200 bg-slate-50/40 p-3.5"
              >
                <div class="mb-2.5 flex items-center justify-between">
                  <span class="text-xs font-semibold text-slate-500">页面 {{ index + 1 }}</span>
                  <button
                    type="button"
                    title="删除该页面"
                    class="rounded-md px-2 py-1 text-xs font-medium text-slate-400 transition hover:bg-red-50 hover:text-red-600"
                    @click="removePage(index)"
                  >
                    删除
                  </button>
                </div>

                <div class="space-y-2.5">
                  <!-- 页面名称 -->
                  <div>
                    <label
                      :for="`field-ui_pages.${index}.name`"
                      class="mb-1.5 block text-xs font-medium text-slate-700"
                    >
                      页面名称
                      <span class="text-red-500" aria-hidden="true">*</span>
                    </label>
                    <input
                      :id="`field-ui_pages.${index}.name`"
                      v-model="page.name"
                      type="text"
                      :class="rowControlClass(rowErrorKey('ui_pages', index, 'name'))"
                      placeholder="例如：首页"
                      @input="clearError(rowErrorKey('ui_pages', index, 'name'))"
                    />
                    <p
                      v-if="errors[rowErrorKey('ui_pages', index, 'name')]"
                      class="mt-1.5 flex items-start gap-1.5 text-xs font-medium text-red-600"
                    >
                      <svg
                        class="mt-0.5 h-3.5 w-3.5 shrink-0"
                        viewBox="0 0 20 20"
                        fill="currentColor"
                        aria-hidden="true"
                      >
                        <path
                          fill-rule="evenodd"
                          d="M18 10A8 8 0 1 1 2 10a8 8 0 0 1 16 0Zm-8-4a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5A.75.75 0 0 1 10 6Zm0 8a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z"
                          clip-rule="evenodd"
                        />
                      </svg>
                      <span>{{ errors[rowErrorKey('ui_pages', index, 'name')] }}</span>
                    </p>
                  </div>

                  <!-- 模块标签 -->
                  <div>
                    <label
                      :for="`field-ui_pages.${index}.modules`"
                      class="mb-1.5 block text-xs font-medium text-slate-700"
                    >
                      模块 / 区块
                      <span class="text-red-500" aria-hidden="true">*</span>
                      <span class="ml-1 font-normal text-slate-400">回车或逗号确认一个模块</span>
                    </label>

                    <!-- 已确认的模块标签 -->
                    <div v-if="page.modules.length > 0" class="mb-2 flex flex-wrap gap-1.5">
                      <span
                        v-for="(module, moduleIndex) in page.modules"
                        :key="`${module}-${moduleIndex}`"
                        class="inline-flex items-center gap-1 rounded-lg bg-primary-50 px-2.5 py-1 text-xs font-medium text-primary-700 ring-1 ring-primary-100"
                      >
                        {{ module }}
                        <button
                          type="button"
                          :title="`移除模块：${module}`"
                          class="text-primary-400 transition hover:text-red-600"
                          @click="removeModule(index, moduleIndex)"
                        >
                          ×
                        </button>
                      </span>
                    </div>

                    <div class="flex items-center gap-2">
                      <input
                        :id="`field-ui_pages.${index}.modules`"
                        v-model="page.moduleDraft"
                        type="text"
                        :class="rowControlClass(rowErrorKey('ui_pages', index, 'modules'))"
                        placeholder="例如：产品介绍（可连续输入，用逗号分隔）"
                        @keydown="onModuleKeydown($event, index)"
                        @paste="handleModulePaste($event, index)"
                        @input="clearError(rowErrorKey('ui_pages', index, 'modules'))"
                      />
                      <button
                        type="button"
                        class="shrink-0 rounded-lg border border-slate-300 bg-white px-3 py-2 text-xs font-medium text-slate-600 transition hover:border-primary-400 hover:text-primary-700"
                        @click="addModule(index)"
                      >
                        添加模块
                      </button>
                    </div>
                    <p
                      v-if="errors[rowErrorKey('ui_pages', index, 'modules')]"
                      class="mt-1.5 flex items-start gap-1.5 text-xs font-medium text-red-600"
                    >
                      <svg
                        class="mt-0.5 h-3.5 w-3.5 shrink-0"
                        viewBox="0 0 20 20"
                        fill="currentColor"
                        aria-hidden="true"
                      >
                        <path
                          fill-rule="evenodd"
                          d="M18 10A8 8 0 1 1 2 10a8 8 0 0 1 16 0Zm-8-4a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5A.75.75 0 0 1 10 6Zm0 8a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z"
                          clip-rule="evenodd"
                        />
                      </svg>
                      <span>{{ errors[rowErrorKey('ui_pages', index, 'modules')] }}</span>
                    </p>
                  </div>

                  <!-- 备注 -->
                  <div>
                    <label
                      :for="`field-ui_pages.${index}.notes`"
                      class="mb-1.5 block text-xs font-medium text-slate-700"
                    >
                      备注（选填）
                    </label>
                    <input
                      :id="`field-ui_pages.${index}.notes`"
                      v-model="page.notes"
                      type="text"
                      :class="rowControlClass(`ui_pages.${index}.notes`)"
                      placeholder="例如：面向新用户"
                    />
                  </div>
                </div>
              </div>

              <!-- 添加页面卡片 -->
              <button
                type="button"
                class="w-full rounded-xl border border-dashed border-slate-300 px-4 py-2.5 text-xs font-medium text-slate-500 transition hover:border-primary-400 hover:text-primary-600"
                @click="addPage"
              >
                ＋ 添加页面
              </button>

              <!-- 批量粘贴导入（可选）：兼容「页面名｜模块1,模块2｜备注」旧格式文本 -->
              <div class="pt-0.5">
                <button
                  type="button"
                  class="text-xs text-slate-400 underline-offset-2 transition hover:text-primary-600 hover:underline"
                  @click="showPagePaste = !showPagePaste"
                >
                  {{ showPagePaste ? '收起批量粘贴' : '批量粘贴导入（可选）' }}
                </button>
                <div v-if="showPagePaste" class="mt-2 space-y-2">
                  <textarea
                    v-model="pagePasteText"
                    rows="3"
                    class="w-full resize-y rounded-xl border border-slate-200 bg-slate-50/50 px-3.5 py-2.5 text-sm leading-relaxed text-slate-900 outline-none transition placeholder:text-slate-400 hover:border-slate-300 focus:border-primary-500 focus:bg-white focus:ring-4 focus:ring-primary-100"
                    placeholder="每行一个页面：页面名｜模块1,模块2｜备注，例如：&#10;首页｜产品介绍,快速开始｜面向新用户"
                  ></textarea>
                  <button
                    type="button"
                    class="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-600 shadow-sm transition hover:border-primary-400 hover:text-primary-700 disabled:cursor-not-allowed disabled:opacity-50"
                    :disabled="!pagePasteText.trim()"
                    @click="importPages"
                  >
                    解析并导入
                  </button>
                </div>
              </div>
            </div>

            <!-- 红色错误提示（字段级，如「至少添加一个目标用户角色」） -->
            <p
              v-if="errors[field.key]"
              class="mt-2 flex items-start gap-1.5 text-xs font-medium text-red-600"
            >
              <svg class="mt-0.5 h-3.5 w-3.5 shrink-0" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                <path
                  fill-rule="evenodd"
                  d="M18 10A8 8 0 1 1 2 10a8 8 0 0 1 16 0Zm-8-4a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5A.75.75 0 0 1 10 6Zm0 8a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z"
                  clip-rule="evenodd"
                />
              </svg>
              <span>{{ errors[field.key] }}</span>
            </p>
          </div>
        </div>
      </section>

      <!-- 提交区 -->
      <div class="pt-2">
        <button
          type="button"
          class="w-full rounded-xl bg-gradient-to-r from-primary-500 to-sky-600 px-4 py-3 text-sm font-semibold text-white shadow-lg shadow-sky-500/25 transition hover:from-primary-600 hover:to-sky-700 hover:shadow-sky-500/35 focus:outline-none focus:ring-4 focus:ring-primary-200 active:scale-[0.99]"
          @click="handleSubmit"
        >
          提交需求，开始 AI 澄清
        </button>
        <p class="mt-3 text-center text-xs text-slate-400">
          提交前自动校验必填项、字段完整性与范围冲突 · 可随时打开 JSON 预览核对
        </p>
      </div>
    </form>
  </div>
</template>
