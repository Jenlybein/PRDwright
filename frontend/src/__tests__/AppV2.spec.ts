import { flushPromises, mount, type DOMWrapper, type VueWrapper } from '@vue/test-utils'
import { nextTick } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import AppV2 from '../AppV2.vue'
import * as api from '../services/api'
import type { RagHit, RequirementsSummary } from '../types'

/**
 * 编排层测试：把流式接口整体 mock 掉，专注验证「第一步换成结构化表单」后
 * 阶段流转、载荷映射、本地快照恢复，以及两道确认闸门（摘要 Diff / 规范检索）
 * 是否按「先确认、后生成」的约定工作。
 */
vi.mock('../services/api', () => ({
  startConversationStream: vi.fn(),
  continueConversationStream: vi.fn(),
  generatePrdStream: vi.fn(),
  generateApiDocsStream: vi.fn(),
  generatePromptsStream: vi.fn(),
  optimizeDocumentStream: vi.fn(),
  syncSummaryFromConversation: vi.fn(),
  retrieveApiDocsRag: vi.fn(),
}))

/** 快照存储键（与 AppV2 内部保持一致，便于构造/断言） */
const SESSION_KEY = 'harnessprd:v2:session'

/** 按 id 查找元素（结构化行级 id 含点号，须用属性选择器） */
function byId(wrapper: VueWrapper | DOMWrapper<Element>, id: string): DOMWrapper<Element> {
  return wrapper.find(`[id="${id}"]`)
}

/**
 * 点击按钮。
 * 默认按「包含」匹配（编辑器里的按钮文案常带前后缀），传 `exact` 时要求文案完全相等——
 * 用于消歧，例如摘要面板的「确认更新，生成 PRD」也包含「生成 PRD」。
 */
async function clickButton(
  wrapper: VueWrapper | DOMWrapper<Element>,
  text: string,
  exact = false,
): Promise<void> {
  const buttons = wrapper.findAll('button')
  const button = exact
    ? buttons.find((item) => item.text().trim() === text)
    : buttons.find((item) => item.text().includes(text))
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

/** 一条检索命中 */
const SAMPLE_HIT: RagHit = {
  id: 'spec-pagination',
  source: '团队接口规范',
  title: '列表与分页规范',
  content: '列表接口统一用 page / pageSize，响应带 total。',
  tags: ['分页'],
  score: 0.31,
  matched_terms: ['分页', 'page'],
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

/** 在摘要确认面板上点确认（文案随有无变更而变，用正则兜住） */
async function confirmSummaryPanel(wrapper: VueWrapper): Promise<void> {
  const button = wrapper
    .findAll('button')
    .find((item) => /确认更新|直接生成 PRD|跳过更新/.test(item.text()))
  if (!button) throw new Error('未找到摘要确认按钮')
  await button.trigger('click')
  await flushPromises()
}

/** 走完「提交需求 → 确认摘要 → 生成 PRD」，停在 PRD 待确认 */
async function reachPrdReview(wrapper: VueWrapper): Promise<void> {
  await submitSampleForm(wrapper)
  await clickButton(wrapper, '生成 PRD', true)
  await flushPromises()
  await confirmSummaryPanel(wrapper)
}

/** 每个用例前清掉本地快照，并给出各接口的安全默认实现 */
beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()

  // 默认：回填结果与当前摘要一致（无变更）
  vi.mocked(api.syncSummaryFromConversation).mockImplementation(async ({ summary }) => ({
    summary,
    changed: false,
  }))
  // 默认：没有检索到规范
  vi.mocked(api.retrieveApiDocsRag).mockResolvedValue({ hits: [] })
  // 默认：PRD 生成成功
  vi.mocked(api.generatePrdStream).mockImplementation(async (_payload, handlers) => {
    handlers?.onChunk?.('# 产品需求文档\n')
    return { conversationId: '', reply: '# 产品需求文档\n\n## 1. 项目背景\n内容' }
  })
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

  it('提交需求后进入对话阶段，映射后端题目字段并下发澄清专用系统提示词', async () => {
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

    // 澄清对话专用人设：资深产品经理 / 只做澄清 / 信息足够时提示生成 PRD
    expect(payload?.systemPrompt).toContain('资深产品经理')
    expect(payload?.systemPrompt).toContain('绝不生成 PRD')
    expect(payload?.systemPrompt).toContain('可以点击生成 PRD')
  })

  it('对话完成后可生成 PRD，并在生成完成时进入待确认阶段', async () => {
    vi.mocked(api.startConversationStream).mockImplementation(async (_payload, handlers) => {
      handlers?.onChunk?.('已理解需求。')
      return { conversationId: 'conv-1', reply: '已理解需求。' }
    })

    const wrapper = mount(AppV2)
    await reachPrdReview(wrapper)

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

  it('点「生成 PRD」先弹摘要确认面板，确认后把回填结果写回摘要再生成', async () => {
    vi.mocked(api.startConversationStream).mockImplementation(async () => ({
      conversationId: 'conv-1',
      reply: '已理解需求。',
    }))
    // 回填接口声称用户在对话里补了目标用户与一个 MVP 功能的价值
    vi.mocked(api.syncSummaryFromConversation).mockImplementation(async ({ summary }) => ({
      summary: {
        ...summary,
        target_users: [...summary.target_users, '小团队产品经理'],
        mvp_features: summary.mvp_features.map((feature) => ({
          ...feature,
          user_value: '省时间',
        })),
      },
      changed: true,
    }))

    const wrapper = mount(AppV2)
    await submitSampleForm(wrapper)
    await clickButton(wrapper, '生成 PRD', true)
    await flushPromises()

    // 停在第 1 道闸门：展示 Diff，尚未调用生成接口
    expect(wrapper.text()).toContain('需求摘要更新确认')
    expect(wrapper.text()).toContain('目标用户 · 第 2 项')
    expect(wrapper.text()).toContain('MVP 功能 · 第 1 项 · 对用户的价值')
    expect(wrapper.text()).toContain('小团队产品经理')
    expect(wrapper.text()).toContain('省时间')
    expect(wrapper.text()).toContain('共 2 处变更')
    expect(api.generatePrdStream).not.toHaveBeenCalled()

    // 回填请求带上了当前摘要与完整对话历史
    const syncPayload = vi.mocked(api.syncSummaryFromConversation).mock.calls[0]?.[0]
    expect(syncPayload?.summary.product_name).toBe('HarnessPRD')
    expect(syncPayload?.history?.[0]?.role).toBe('assistant')

    await clickButton(wrapper, '确认更新')
    await flushPromises()

    // 确认后摘要被写回，PRD 需求文本里带上了新增的目标用户
    expect(wrapper.text()).toContain('PRD 文档')
    expect(api.generatePrdStream).toHaveBeenCalledTimes(1)
    const prdPayload = vi.mocked(api.generatePrdStream).mock.calls[0]?.[0]
    expect(prdPayload?.requirements).toContain('小团队产品经理')
    expect(prdPayload?.requirements).toContain('省时间')
  })

  it('拒绝更改：摘要保持不变并回到对话页，不进入生成流程', async () => {
    vi.mocked(api.startConversationStream).mockImplementation(async () => ({
      conversationId: 'conv-1',
      reply: '已理解需求。',
    }))
    vi.mocked(api.syncSummaryFromConversation).mockImplementation(async ({ summary }) => ({
      summary: { ...summary, platform: '命令行工具' },
      changed: true,
    }))

    const wrapper = mount(AppV2)
    await submitSampleForm(wrapper)
    await clickButton(wrapper, '生成 PRD', true)
    await flushPromises()

    expect(wrapper.text()).toContain('运行平台')

    await clickButton(wrapper, '拒绝更改')
    await flushPromises()

    expect(wrapper.text()).toContain('需求澄清对话')
    expect(api.generatePrdStream).not.toHaveBeenCalled()
    // 摘要未被改动：回到第一步看到的仍是原平台
    expect((byId(wrapper, 'field-platform').element as HTMLInputElement).value).toBe('网页应用')
  })

  it('满足条件时进入第 2 道闸门：先检索团队规范，确认后才生成接口文档', async () => {
    vi.mocked(api.startConversationStream).mockImplementation(async () => ({
      conversationId: 'conv-1',
      reply: '已理解需求。',
    }))
    vi.mocked(api.retrieveApiDocsRag).mockResolvedValue({ hits: [SAMPLE_HIT] })
    vi.mocked(api.generateApiDocsStream).mockImplementation(async (_payload, handlers) => {
      handlers?.onChunk?.('# 接口文档\n')
      return { conversationId: '', reply: '# 接口文档\n\n## 一、概述\n统一响应结构' }
    })

    const wrapper = mount(AppV2)
    await reachPrdReview(wrapper)
    await clickButton(wrapper, '生成接口文档')
    await flushPromises()

    // 停在检索结果面板：尚未生成
    expect(wrapper.text()).toContain('接口规范检索结果')
    expect(wrapper.text()).toContain('列表与分页规范')
    expect(wrapper.text()).toContain('相关度 0.31')
    expect(api.generateApiDocsStream).not.toHaveBeenCalled()

    // 检索请求带上了 PRD 正文
    const ragPayload = vi.mocked(api.retrieveApiDocsRag).mock.calls[0]?.[0]
    expect(ragPayload?.prdContent).toContain('项目背景')

    await clickButton(wrapper, '确认并生成接口文档')
    await flushPromises()

    expect(wrapper.text()).toContain('接口文档')
    expect(api.generateApiDocsStream).toHaveBeenCalledTimes(1)
    // 勾选的命中随生成请求下发
    const docsPayload = vi.mocked(api.generateApiDocsStream).mock.calls[0]?.[0]
    expect(docsPayload?.ragHits?.[0]?.title).toBe('列表与分页规范')
  })

  it('取消检索结果：不调用生成接口，回到 PRD 待确认', async () => {
    vi.mocked(api.startConversationStream).mockImplementation(async () => ({
      conversationId: 'conv-1',
      reply: '已理解需求。',
    }))
    vi.mocked(api.retrieveApiDocsRag).mockResolvedValue({ hits: [SAMPLE_HIT] })

    const wrapper = mount(AppV2)
    await reachPrdReview(wrapper)
    await clickButton(wrapper, '生成接口文档')
    await flushPromises()

    await clickButton(wrapper, '取消')
    await flushPromises()

    expect(api.generateApiDocsStream).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('PRD 文档')
    expect(wrapper.text()).not.toContain('接口规范检索结果')
  })

  it('检索失败也不卡流程：确认按钮降级为「跳过更新，直接生成接口文档」', async () => {
    vi.mocked(api.startConversationStream).mockImplementation(async () => ({
      conversationId: 'conv-1',
      reply: '已理解需求。',
    }))
    vi.mocked(api.retrieveApiDocsRag).mockRejectedValue(new Error('接口规范检索失败：文件缺失'))
    vi.mocked(api.generateApiDocsStream).mockImplementation(async () => ({
      conversationId: '',
      reply: '# 接口文档\n内容',
    }))

    const wrapper = mount(AppV2)
    await reachPrdReview(wrapper)
    await clickButton(wrapper, '生成接口文档')
    await flushPromises()

    expect(wrapper.text()).toContain('接口规范检索失败：文件缺失')

    await clickButton(wrapper, '直接生成接口文档')
    await flushPromises()

    expect(api.generateApiDocsStream).toHaveBeenCalledTimes(1)
    expect(vi.mocked(api.generateApiDocsStream).mock.calls[0]?.[0]?.ragHits).toEqual([])
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

  it('刷新时若停在确认闸门，回到发起该步骤的阶段并给出说明', async () => {
    localStorage.setItem(
      SESSION_KEY,
      JSON.stringify({
        viewState: 'summary-diff',
        summary: SAMPLE_SUMMARY,
        messages: [{ role: 'assistant', content: '已理解需求。' }],
        conversationId: 'conv-1',
        prdContent: '',
        apiDocsContent: '',
        promptsContent: '',
      }),
    )

    const wrapper = mount(AppV2)
    expect(wrapper.text()).toContain('上次的确认步骤未完成')
    expect(wrapper.text()).toContain('需求澄清对话')
    expect(wrapper.text()).not.toContain('需求摘要更新确认')
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
