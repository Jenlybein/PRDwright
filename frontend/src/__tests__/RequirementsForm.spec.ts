import { mount, type DOMWrapper, type VueWrapper } from '@vue/test-utils'
import { nextTick } from 'vue'
import { describe, expect, it } from 'vitest'

import RequirementsForm from '../components/RequirementsForm.vue'
import type { RequirementsSummary } from '../types'

/** 挂载结构化表单 */
function mountForm(): VueWrapper {
  return mount(RequirementsForm)
}

/**
 * 按 id 查找元素。
 * 结构化行级元素的 id 里带点（如 field-ui_pages.0.name），
 * 用属性选择器而不是 `#id`，避免点号被解析成 class。
 */
function byId(wrapper: VueWrapper | DOMWrapper<Element>, id: string): DOMWrapper<Element> {
  return wrapper.find(`[id="${id}"]`)
}

/** 读取某个输入框的当前值（元素类型收窄到 HTMLInputElement） */
function inputValue(wrapper: VueWrapper, id: string): string {
  return (byId(wrapper, id).element as HTMLInputElement).value
}

/** 填某个普通字段（input / textarea / select 通用） */
async function fill(wrapper: VueWrapper, key: string, value: string): Promise<void> {
  await byId(wrapper, `field-${key}`).setValue(value)
}

/** 点击包含指定文字的按钮（全局第一个匹配） */
async function clickButton(wrapper: VueWrapper | DOMWrapper<Element>, text: string): Promise<void> {
  const button = wrapper.findAll('button').find((item) => item.text().includes(text))
  if (!button) throw new Error(`未找到按钮：${text}`)
  await button.trigger('click')
}

/** 在某个容器内按精确文字点击按钮 */
async function clickExact(container: DOMWrapper<Element>, text: string): Promise<void> {
  const button = container.findAll('button').find((item) => item.text().trim() === text)
  if (!button) throw new Error(`容器内未找到按钮：${text}`)
  await button.trigger('click')
}

/** 读取 JSON 预览内容（预览未打开时先打开） */
async function readPreview(wrapper: VueWrapper): Promise<Record<string, unknown>> {
  if (!wrapper.find('pre').exists()) {
    await clickButton(wrapper, '查看 JSON 预览')
  }
  return JSON.parse(wrapper.find('pre').text()) as Record<string, unknown>
}

/** 派发一次带剪贴板内容的 paste 事件（jsdom 下手工构造 clipboardData） */
async function paste(element: Element, text: string): Promise<void> {
  const event = new Event('paste', { bubbles: true, cancelable: true })
  Object.defineProperty(event, 'clipboardData', { value: { getData: () => text } })
  element.dispatchEvent(event)
  await nextTick()
}

/** 添加一行 MVP 功能并填写三个字段（value 为空字符串表示不填） */
async function addFeatureRow(
  wrapper: VueWrapper,
  name: string,
  description: string,
  value = '',
): Promise<number> {
  const container = byId(wrapper, 'field-mvp_features')
  await clickButton(container, '添加功能')
  const nameInputs = wrapper.findAll('input[id^="field-mvp_features."][id$=".name"]')
  const index = nameInputs.length - 1
  await byId(wrapper, `field-mvp_features.${index}.name`).setValue(name)
  await byId(wrapper, `field-mvp_features.${index}.description`).setValue(description)
  if (value) {
    await byId(wrapper, `field-mvp_features.${index}.value`).setValue(value)
  }
  return index
}

/** 添加一张页面卡片并填写名称 */
async function addPageRow(wrapper: VueWrapper, name: string): Promise<number> {
  const container = byId(wrapper, 'field-ui_pages')
  await clickButton(container, '添加页面')
  const nameInputs = wrapper.findAll('input[id^="field-ui_pages."][id$=".name"]')
  const index = nameInputs.length - 1
  await byId(wrapper, `field-ui_pages.${index}.name`).setValue(name)
  return index
}

/** 填满所有必填项：产品名称 / 平台 / 核心目标 / 一个目标用户 / 一个 MVP 功能 */
async function fillRequired(wrapper: VueWrapper): Promise<void> {
  await fill(wrapper, 'product_name', '测试产品')
  await fill(wrapper, 'platform', 'App')
  await fill(wrapper, 'product_goal', '解决一个问题')
  await clickButton(wrapper, '添加用户角色')
  await byId(wrapper, 'field-target_users.0').setValue('开发者')
  await addFeatureRow(wrapper, '问卷填写', '收集产品信息')
}

describe('RequirementsForm 结构化录入表单', () => {
  it('默认渲染四个区块，JSON 预览默认关闭', () => {
    const wrapper = mountForm()
    const text = wrapper.text()
    expect(text).toContain('产品与用户')
    expect(text).toContain('功能与页面')
    expect(text).toContain('技术约束')
    expect(text).toContain('非功能与范围')
    expect(wrapper.find('pre').exists()).toBe(false)
  })

  it('列表编辑器：逐条添加目标用户，直接生成字符串数组', async () => {
    const wrapper = mountForm()
    await clickButton(wrapper, '添加用户角色')
    await byId(wrapper, 'field-target_users.0').setValue('独立开发者')
    await clickButton(wrapper, '添加用户角色')
    await byId(wrapper, 'field-target_users.1').setValue('  3-10 人小团队的产品经理  ')
    const preview = await readPreview(wrapper)
    expect(preview.target_users).toEqual(['独立开发者', '3-10 人小团队的产品经理'])
  })

  it('列表编辑器：粘贴多行文本自动拆成多条，用户无需自己整理格式', async () => {
    const wrapper = mountForm()
    await clickButton(wrapper, '添加用户角色')
    await paste(
      byId(wrapper, 'field-target_users.0').element,
      '独立开发者\n\n  3-10 人小团队的产品经理  \n',
    )
    const preview = await readPreview(wrapper)
    expect(preview.target_users).toEqual(['独立开发者', '3-10 人小团队的产品经理'])
  })

  it('列表编辑器：删除一条后 JSON 预览同步更新', async () => {
    const wrapper = mountForm()
    await clickButton(wrapper, '添加范围内项')
    await byId(wrapper, 'field-scope_in.0').setValue('固定格式表单录入')
    await clickButton(wrapper, '添加范围内项')
    await byId(wrapper, 'field-scope_in.1').setValue('JSON 实时预览')

    const container = byId(wrapper, 'field-scope_in')
    const deletes = container.findAll('button').filter((item) => item.text() === '删除')
    const firstDelete = deletes[0]
    if (!firstDelete) throw new Error('未找到删除按钮')
    await firstDelete.trigger('click')

    const preview = await readPreview(wrapper)
    expect(preview.scope).toEqual({ in_scope: ['JSON 实时预览'], out_of_scope: [] })
  })

  it('MVP 功能为结构化行编辑器：添加行并按字段填写，直接生成结构化 JSON', async () => {
    const wrapper = mountForm()
    // 未添加时展示空状态引导，不要求用户手写格式
    expect(wrapper.text()).toContain('还没有功能项')
    await addFeatureRow(wrapper, '问卷填写', '分基础与进阶两组收集产品信息', '降低填写门槛')
    await addFeatureRow(wrapper, 'JSON 预览', '填写过程实时生成结构化 JSON')
    const preview = await readPreview(wrapper)
    expect(preview.mvp_features).toEqual([
      { name: '问卷填写', description: '分基础与进阶两组收集产品信息', user_value: '降低填写门槛' },
      { name: 'JSON 预览', description: '填写过程实时生成结构化 JSON' },
    ])
  })

  it('MVP 功能：删除行后 JSON 预览同步更新', async () => {
    const wrapper = mountForm()
    await addFeatureRow(wrapper, '功能一', '描述一')
    await addFeatureRow(wrapper, '功能二', '描述二')
    let preview = await readPreview(wrapper)
    expect(preview.mvp_features).toHaveLength(2)

    const container = byId(wrapper, 'field-mvp_features')
    const deletes = container.findAll('button').filter((item) => item.text() === '删除')
    const firstDelete = deletes[0]
    if (!firstDelete) throw new Error('未找到删除按钮')
    await firstDelete.trigger('click')

    preview = await readPreview(wrapper)
    expect(preview.mvp_features).toEqual([{ name: '功能二', description: '描述二' }])
  })

  it('MVP 功能：批量粘贴导入把旧格式文本解析为结构化行', async () => {
    const wrapper = mountForm()
    const container = byId(wrapper, 'field-mvp_features')
    await clickButton(container, '批量粘贴导入')
    await wrapper
      .find('textarea[placeholder^="每行一个功能"]')
      .setValue('问卷填写｜收集产品信息｜降低门槛\nJSON 预览｜实时查看结构化结果')
    await clickExact(container, '解析并导入')
    const preview = await readPreview(wrapper)
    expect(preview.mvp_features).toEqual([
      { name: '问卷填写', description: '收集产品信息', user_value: '降低门槛' },
      { name: 'JSON 预览', description: '实时查看结构化结果' },
    ])
    // 导入后粘贴面板自动收起
    expect(wrapper.find('textarea[placeholder^="每行一个功能"]').exists()).toBe(false)
  })

  it('页面编辑器：模块以标签形式添加（按钮 / 回车 / 逗号），生成结构化页面', async () => {
    const wrapper = mountForm()
    await addPageRow(wrapper, '首页')
    const moduleInput = (): DOMWrapper<Element> => byId(wrapper, 'field-ui_pages.0.modules')

    await moduleInput().setValue('产品介绍')
    await clickButton(byId(wrapper, 'field-ui_pages'), '添加模块')
    await moduleInput().setValue('快速开始')
    await moduleInput().trigger('keydown', { key: 'Enter' })
    // 一次输入多个模块，逗号即确认
    await moduleInput().setValue('数据看板,统计图表')
    await moduleInput().trigger('keydown', { key: ',' })
    await byId(wrapper, 'field-ui_pages.0.notes').setValue('面向新用户')

    const preview = await readPreview(wrapper)
    expect(preview.ui_pages).toEqual([
      {
        name: '首页',
        modules: ['产品介绍', '快速开始', '数据看板', '统计图表'],
        notes: '面向新用户',
      },
    ])
  })

  it('页面编辑器：移除模块标签后 JSON 预览同步更新', async () => {
    const wrapper = mountForm()
    const index = await addPageRow(wrapper, '表单页')
    const moduleInput = byId(wrapper, `field-ui_pages.${index}.modules`)
    await moduleInput.setValue('分组表单,JSON 预览')
    await moduleInput.trigger('keydown', { key: 'Enter' })

    const container = byId(wrapper, 'field-ui_pages')
    const chipButtons = container.findAll('button').filter((item) => item.text() === '×')
    const firstChip = chipButtons[0]
    if (!firstChip) throw new Error('未找到模块标签移除按钮')
    await firstChip.trigger('click')

    const preview = await readPreview(wrapper)
    expect(preview.ui_pages).toEqual([{ name: '表单页', modules: ['JSON 预览'] }])
  })

  it('页面编辑器：批量粘贴导入按旧格式解析为页面卡片', async () => {
    const wrapper = mountForm()
    const container = byId(wrapper, 'field-ui_pages')
    await clickButton(container, '批量粘贴导入')
    await wrapper
      .find('textarea[placeholder^="每行一个页面"]')
      .setValue('首页｜产品介绍,快速开始｜面向新用户\n表单页｜分组表单,JSON 预览')
    await clickExact(container, '解析并导入')
    const preview = await readPreview(wrapper)
    expect(preview.ui_pages).toEqual([
      { name: '首页', modules: ['产品介绍', '快速开始'], notes: '面向新用户' },
      { name: '表单页', modules: ['分组表单', 'JSON 预览'] },
    ])
  })

  it('页面编辑器：提交前把未回车的模块草稿自动入库', async () => {
    const wrapper = mountForm()
    await fillRequired(wrapper)
    const index = await addPageRow(wrapper, '首页')
    // 输入模块但不回车、不点添加
    await byId(wrapper, `field-ui_pages.${index}.modules`).setValue('产品介绍')
    await clickButton(wrapper, '提交')
    const preview = JSON.parse(wrapper.find('pre').text()) as Record<string, unknown>
    expect(preview.ui_pages).toEqual([{ name: '首页', modules: ['产品介绍'] }])
  })

  it('buildRequirementsSummary：可选字段为空时不出现在 JSON 中，键顺序与 schema 一致', async () => {
    const wrapper = mountForm()
    await fillRequired(wrapper)
    const preview = await readPreview(wrapper)
    expect(Object.keys(preview)).toEqual([
      'product_name',
      'product_goal',
      'target_users',
      'platform',
      'mvp_features',
    ])
    expect(preview).not.toHaveProperty('v2_features')
    expect(preview).not.toHaveProperty('ui_pages')
    expect(preview).not.toHaveProperty('key_interactions')
    expect(preview).not.toHaveProperty('technical_constraints')
    expect(preview).not.toHaveProperty('scope')
  })

  it('buildRequirementsSummary：完整填写时输出与后端 Schema 对齐的完整结构', async () => {
    const wrapper = mountForm()
    await clickButton(wrapper, '填充示例')
    const preview = await readPreview(wrapper)
    expect(Object.keys(preview)).toEqual([
      'product_name',
      'product_goal',
      'target_users',
      'platform',
      'mvp_features',
      'v2_features',
      'ui_pages',
      'key_interactions',
      'technical_constraints',
      'llm_usage',
      'non_functional',
      'scope',
    ])
    expect(preview.product_name).toBe('HarnessPRD')
    expect(preview.platform).toBe('网页应用')
    expect(preview.target_users).toEqual(['独立开发者', '3-10 人小团队的产品经理'])
    expect(preview.mvp_features).toEqual([
      { name: '问卷填写', description: '分基础与进阶两组收集产品信息', user_value: '降低填写门槛' },
      { name: 'JSON 预览', description: '填写过程实时生成结构化 JSON', user_value: '便于排查格式问题' },
      { name: '格式校验', description: '提交前检查必填项与字段完整性', user_value: '减少后端报错' },
    ])
    expect(preview.ui_pages).toEqual([
      { name: '首页', modules: ['产品介绍', '快速开始'], notes: '面向新用户' },
      { name: '表单页', modules: ['分组表单', 'JSON 预览'], notes: '核心录入页' },
    ])
    expect(preview.scope).toEqual({
      in_scope: ['固定格式表单录入', 'JSON 实时预览', '基础格式校验'],
      out_of_scope: ['动态 JSON Schema 渲染', '多人协作', '登录系统'],
    })
    expect((preview.technical_constraints as Record<string, string>).auth).toBe('免登录')
    expect((preview.technical_constraints as Record<string, string>).storage).toBe('浏览器本地存储')
    expect(preview.non_functional).toEqual({
      security: 'API Key 不落库；用户输入仅存储于本地浏览器',
      performance: '首屏加载 < 2s；表单输入到 JSON 预览更新 < 100ms',
      availability: '核心功能可用性 99.9%；后端不可用时给出明确提示',
    })
  })

  it('必填校验：空表单提交时五个必填项全部报红且不打开预览', async () => {
    const wrapper = mountForm()
    await clickButton(wrapper, '提交')
    const text = wrapper.text()
    expect(text).toContain('产品名称为必填项')
    expect(text).toContain('请选择运行平台')
    expect(text).toContain('核心目标为必填项')
    expect(text).toContain('目标用户为必填项，至少添加一个用户角色')
    expect(text).toContain('至少添加一个 MVP 功能')
    expect(wrapper.find('pre').exists()).toBe(false)
  })

  it('MVP 校验：功能行缺少名称或描述时逐行报红', async () => {
    const wrapper = mountForm()
    await addFeatureRow(wrapper, '问卷填写', '收集产品信息')
    await clickButton(byId(wrapper, 'field-mvp_features'), '添加功能') // 第二行整体留空
    await clickButton(wrapper, '提交')
    const text = wrapper.text()
    expect(text).toContain('第 2 个功能：功能名称为必填项')
    expect(text).toContain('第 2 个功能：功能描述为必填项')
    // 第一行已填写完整，不应出现针对它的错误
    expect(text).not.toContain('第 1 个功能')
    expect(wrapper.find('pre').exists()).toBe(false)
  })

  it('页面校验：页面卡片缺少模块时报出行号', async () => {
    const wrapper = mountForm()
    await fillRequired(wrapper)
    await addPageRow(wrapper, '首页')
    await clickButton(wrapper, '提交')
    expect(wrapper.text()).toContain('第 1 个页面：请至少添加一个模块')
    expect(wrapper.find('pre').exists()).toBe(false)
  })

  it('范围校验：范围内外出现相同内容时双向报红', async () => {
    const wrapper = mountForm()
    await clickButton(wrapper, '添加范围内项')
    await byId(wrapper, 'field-scope_in.0').setValue('登录系统')
    await clickButton(wrapper, '添加范围内项')
    await byId(wrapper, 'field-scope_in.1').setValue('JSON 预览')
    await clickButton(wrapper, '添加范围外项')
    await byId(wrapper, 'field-scope_out.0').setValue('登录系统')
    await clickButton(wrapper, '提交')
    const text = wrapper.text()
    expect(text).toContain('「登录系统」同时出现在范围内与范围外')
    // 在范围内与不在范围内两个字段都标记错误
    const occurrences = text.split('「登录系统」同时出现在范围内与范围外').length - 1
    expect(occurrences).toBe(2)
  })

  it('校验通过：提交后自动展开 JSON 预览并抛出结构化摘要', async () => {
    const wrapper = mountForm()
    await clickButton(wrapper, '填充示例')
    await clickButton(wrapper, '提交')
    expect(wrapper.find('pre').exists()).toBe(true)
    expect(wrapper.text()).toContain('校验通过，已生成结构化 JSON')
    const preview = JSON.parse(wrapper.find('pre').text()) as Record<string, unknown>
    expect(preview.mvp_features).toHaveLength(3)
    // submit 事件带回完整摘要，供上层直接使用
    const emitted = wrapper.emitted('submit')
    expect(emitted).toHaveLength(1)
    expect((emitted?.[0]?.[0] as RequirementsSummary).product_name).toBe('HarnessPRD')
  })

  it('JSON 预览为格式化后的等宽文本', async () => {
    const wrapper = mountForm()
    await fill(wrapper, 'product_name', '缩进检查')
    await clickButton(wrapper, '查看 JSON 预览')
    const pre = wrapper.find('pre')
    expect(pre.classes()).toContain('font-mono')
    // 2 空格缩进的格式化 JSON
    expect(pre.text()).toContain('"product_name": "缩进检查"')
    expect(pre.text()).toContain('\n  "')
  })

  it('重新输入后对应字段的错误被清除', async () => {
    const wrapper = mountForm()
    await clickButton(wrapper, '提交')
    expect(wrapper.text()).toContain('产品名称为必填项')
    await fill(wrapper, 'product_name', 'HarnessPRD')
    expect(wrapper.text()).not.toContain('产品名称为必填项')
  })

  it('清空按钮重置全部输入，结构化编辑器回到空状态', async () => {
    const wrapper = mountForm()
    await fill(wrapper, 'product_name', '临时内容')
    await addFeatureRow(wrapper, '临时功能', '临时描述')
    await addPageRow(wrapper, '临时页面')
    await clickButton(wrapper, '清空')
    expect(wrapper.text()).toContain('还没有功能项')
    expect(wrapper.text()).toContain('还没有页面')
    expect(wrapper.text()).toContain('点击下方按钮添加用户角色')
    const preview = await readPreview(wrapper)
    expect(preview.product_name).toBe('')
    expect(preview.mvp_features).toEqual([])
    expect(preview.target_users).toEqual([])
  })

  it('传入 initial 时回填上次填写的内容（本地草稿恢复）', async () => {
    const initial: RequirementsSummary = {
      product_name: '恢复的产品',
      product_goal: '继续上次的目标',
      target_users: ['独立开发者'],
      platform: 'CLI',
      mvp_features: [{ name: '一键初始化', description: '生成项目骨架' }],
      scope: { in_scope: ['基础录入'], out_of_scope: [] },
    }
    const wrapper = mount(RequirementsForm, { props: { initial } })
    expect(wrapper.text()).toContain('已为你恢复上次填写的内容')
    expect(inputValue(wrapper, 'field-product_name')).toBe('恢复的产品')
    expect(inputValue(wrapper, 'field-target_users.0')).toBe('独立开发者')
    expect(inputValue(wrapper, 'field-mvp_features.0.name')).toBe('一键初始化')
    const preview = await readPreview(wrapper)
    expect(preview.platform).toBe('CLI')
  })

  it('内容变化时抛出 update:summary，供上层保存草稿', async () => {
    const wrapper = mountForm()
    await fill(wrapper, 'product_name', '草稿产品')
    const events = wrapper.emitted('update:summary')
    expect(events && events.length > 0).toBe(true)
    const last = events?.[events.length - 1]?.[0] as RequirementsSummary
    expect(last.product_name).toBe('草稿产品')
  })

  it('返回按钮默认文案为「返回入口」，点击抛出 back', async () => {
    const wrapper = mountForm()
    const back = findByText(wrapper, '返回入口')
    expect(back).toBeDefined()

    await back?.trigger('click')
    expect(wrapper.emitted('back')).toHaveLength(1)
    // 返回不触发提交，也不清空用户填的内容
    expect(wrapper.emitted('submit')).toBeUndefined()
  })

  it('返回按钮文案可由父组件按上下文覆盖', async () => {
    const wrapper = mount(RequirementsForm, { props: { backLabel: '返回对话' } })
    expect(findByText(wrapper, '返回对话')).toBeDefined()
    expect(findByText(wrapper, '返回入口')).toBeUndefined()
  })
})

/** 按按钮文案精确查找（返回类按钮文案短，避免子串误命中） */
function findByText(wrapper: VueWrapper, text: string): DOMWrapper<Element> | undefined {
  return wrapper.findAll('button').find((item) => item.text().trim() === text)
}
