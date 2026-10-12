import { flushPromises, mount, type DOMWrapper, type VueWrapper } from '@vue/test-utils'
import { nextTick } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import AppV2 from '../AppV2.vue'
import * as api from '../services/api'
import type { RequirementsSummary } from '../types'

/**
 * 编排层测试：把流式接口整体 mock 掉，专注验证「第一步换成结构化表单」后
 * 阶段流转、载荷映射与本地快照恢复是否仍然正确。
 */
vi.mock('../services/api', () => ({
  startConversationStream: vi.fn(),
  continueConversationStream: vi.fn(),
  generatePrdStream: vi.fn(),
  generateApiDocsStream: vi.fn(),
  generatePromptsStream: vi.fn(),
  optimizeDocumentStream: vi.fn(),
}))

/** 快照存储键（与 AppV2 内部保持一致，便于构造/断言） */
const SESSION_KEY = 'harnessprd:v2:session'

/** 按 id 查找元素（结构化行级 id 含点号，须用属性选择器） */
function byId(wrapper: VueWrapper | DOMWrapper<Element>, id: string): DOMWrapper<Element> {
  return wrapper.find(`[id="${id}"]`)
}

/** 点击包含指定文字的按钮 */
async function clickButton(wrapper: VueWrapper | DOMWrapper<Element>, text: string): Promise<void> {
  const button = wrapper.findAll('button').find((item) => item.text().includes(text))
  if (!button) throw new Error(`未找到按钮：${text}`)
  await button.trigger('click')
}

/** 点击进度条上的某个步骤（步骤是带 @click 的 div，不是 button） */
async function clickStep(wrapper: VueWrapper, label: string): Promise<void> {
  const item = wrapper.findAll('nav li').find((li) => li.text().includes(label))
  if (!item) throw new Error(`未找到进度步骤：${label}`)
  await item.find('div').trigger('click')
}

/** 构造一份可用于提交的完整摘要 */
const SAMPLE_SUMMARY: RequirementsSummary = {
  product_name: 'HarnessPRD',
  product_goal: '把模糊想法整理成可开工的 PRD',
  target_users: ['独立开发者'],
  platform: '网页应用',
  mvp_features: [{ name: '问卷填写', description: '收集产品信息' }],
}

/**
 * 逐项填出与 `SAMPLE_SUMMARY` 完全一致的表单。
 * 不用「填充示例」按钮，是为了让字段映射断言有确定的期望值。
 */
async function fillSampleForm(wrapper: VueWrapper): Promise<void> {
  await byId(wrapper, 'field-product_name').setValue(SAMPLE_SUMMARY.product_name)
  await byId(wrapper, 'field-product_goal').setValue(SAMPLE_SUMMARY.product_goal)
  await byId(wrapper, 'field-platform').setValue(SAMPLE_SUMMARY.platform)

  await clickButton(wrapper, '添加用户角色')
  await byId(wrapper, 'field-target_users.0').setValue('独立开发者')

  await clickButton(wrapper, '添加功能')
  await byId(wrapper, 'field-mvp_features.0.name').setValue('问卷填写')
  await byId(wrapper, 'field-mvp_features.0.description').setValue('收集产品信息')
}

/** 让「提交需求 → 对话」这条链路走通：填样例表单并提交 */
async function submitSampleForm(wrapper: VueWrapper): Promise<void> {
  await fillSampleForm(wrapper)
  await clickButton(wrapper, '提交需求')
  await flushPromises()
}

/** 每个用例前清掉本地快照，避免互相污染 */
beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
})

describe('AppV2 完整流程编排', () => {
  it('默认停在第一步：显示进度条与结构化表单，不显示对话区', () => {
    const wrapper = mount(AppV2)
    expect(wrapper.text()).toContain('描述产品')
    expect(wrapper.text()).toContain('AI 对话')
    expect(wrapper.text()).toContain('告诉我们你的产品想法')
    expect(wrapper.text()).not.toContain('需求澄清对话')
    expect(wrapper.find('pre').exists()).toBe(false)
    expect(api.startConversationStream).not.toHaveBeenCalled()
  })

  it('提交需求后进入对话阶段，并把结构化摘要映射成后端题目字段', async () => {
    vi.mocked(api.startConversationStream).mockImplementation(async (_payload, handlers) => {
      handlers?.onChunk?.('我先复述一下理解：这是一个给独立开发者用的 PRD 生成器。')
      return { conversationId: 'conv-1', reply: '我先复述一下理解：这是一个给独立开发者用的 PRD 生成器。' }
    })

    const wrapper = mount(AppV2)
    await submitSampleForm(wrapper)

    expect(wrapper.text()).toContain('需求澄清对话')
    expect(wrapper.text()).toContain('我先复述一下理解')
    expect(api.startConversationStream).toHaveBeenCalledTimes(1)

    // 载荷：结构化字段映射到后端已有题目的字段名
    const payload = vi.mocked(api.startConversationStream).mock.calls[0]?.[0]
    expect(payload?.answers.product_name).toBe('HarnessPRD')
    expect(payload?.answers.problem_to_solve).toBe('把模糊想法整理成可开工的 PRD')
    expect(payload?.answers.platform_type).toBe('网页应用')
    expect(payload?.answers.core_features).toEqual(['问卷填写 —— 收集产品信息'])
  })

  it('对话完成后可生成 PRD，并在生成完成时进入待确认阶段', async () => {
    vi.mocked(api.startConversationStream).mockImplementation(async (_payload, handlers) => {
      handlers?.onChunk?.('已理解需求。')
      return { conversationId: 'conv-1', reply: '已理解需求。' }
    })
    vi.mocked(api.generatePrdStream).mockImplementation(async (_payload, handlers) => {
      handlers?.onChunk?.('# 产品需求文档\\n')
      return { conversationId: '', reply: '# 产品需求文档\n\n## 1. 项目背景\n内容' }
    })

    const wrapper = mount(AppV2)
    await submitSampleForm(wrapper)
    await clickButton(wrapper, '生成 PRD')
    await flushPromises()

    expect(wrapper.text()).toContain('PRD 文档')
    expect(wrapper.text()).toContain('项目背景')

    // PRD 生成拿到的是与 field-schema 对齐的结构化 JSON
    const payload = vi.mocked(api.generatePrdStream).mock.calls[0]?.[0]
    expect(payload?.requirements).toContain('```json')
    expect(payload?.requirements).toContain('"product_name": "HarnessPRD"')
    // 对话历史一并带上：首轮只有 AI 那条回复（需求本身走 requirements 传入）
    expect(payload?.history?.length).toBe(1)
    expect(payload?.history?.[0]?.role).toBe('assistant')
  })

  it('「改需求」回到第一步，表单内容与对话记录都不丢', async () => {
    vi.mocked(api.startConversationStream).mockImplementation(async () => ({
      conversationId: 'conv-1',
      reply: '已理解需求。',
    }))

    const wrapper = mount(AppV2)
    await submitSampleForm(wrapper)
    await clickButton(wrapper, '改需求')

    expect(wrapper.text()).toContain('告诉我们你的产品想法')
    // 表单是常驻组件，切换阶段不会丢内容
    expect((byId(wrapper, 'field-product_name').element as HTMLInputElement).value).toBe('HarnessPRD')

    // 从进度条点回「AI 对话」：上一轮对话记录仍在，不需要重新提交需求
    await clickStep(wrapper, 'AI 对话')
    expect(wrapper.text()).toContain('需求澄清对话')
    expect(wrapper.text()).toContain('已理解需求。')
  })

  it('刷新后按本地快照恢复：回到原阶段并回填表单', async () => {
    localStorage.setItem(
      SESSION_KEY,
      JSON.stringify({
        viewState: 'review-prd',
        summary: SAMPLE_SUMMARY,
        messages: [
          { role: 'user', content: '需求如下' },
          { role: 'assistant', content: '已理解需求。' },
        ],
        conversationId: 'conv-1',
        prdContent: '# 产品需求文档\n历史内容',
        apiDocsContent: '',
        promptsContent: '',
      }),
    )

    const wrapper = mount(AppV2)
    expect(wrapper.text()).toContain('PRD 文档')
    expect(wrapper.text()).toContain('历史内容')
    expect((byId(wrapper, 'field-product_name').element as HTMLInputElement).value).toBe('HarnessPRD')
    expect(api.startConversationStream).not.toHaveBeenCalled()
  })

  it('刷新时若正处于生成中，降级为待确认并给出说明', async () => {
    localStorage.setItem(
      SESSION_KEY,
      JSON.stringify({
        viewState: 'generating-prd',
        summary: SAMPLE_SUMMARY,
        messages: [],
        conversationId: '',
        prdContent: '',
        apiDocsContent: '',
        promptsContent: '',
      }),
    )

    const wrapper = mount(AppV2)
    expect(wrapper.text()).toContain('上次的生成未完成')
    expect(wrapper.text()).toContain('PRD 文档')
  })

  it('重新开始：回到第一步并清空表单与本地快照', async () => {
    vi.mocked(api.startConversationStream).mockImplementation(async () => ({
      conversationId: 'conv-1',
      reply: '已理解需求。',
    }))

    const wrapper = mount(AppV2)
    await submitSampleForm(wrapper)
    await clickButton(wrapper, '重新开始')
    await nextTick()

    expect(wrapper.text()).toContain('告诉我们你的产品想法')
    expect((byId(wrapper, 'field-product_name').element as HTMLInputElement).value).toBe('')
    expect(wrapper.text()).toContain('还没有功能项')
  })

  it('提交失败时保留错误提示，不进入空白对话页', async () => {
    vi.mocked(api.startConversationStream).mockRejectedValue(new Error('后端不可用'))

    const wrapper = mount(AppV2)
    await submitSampleForm(wrapper)

    expect(wrapper.text()).toContain('需求澄清对话')
    expect(wrapper.text()).toContain('后端不可用')
  })
})
