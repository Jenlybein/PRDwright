import { flushPromises, mount, type DOMWrapper, type VueWrapper } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { nextTick } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import AppV2 from '../AppV2.vue'
import * as api from '../services/api'
import type { RagHit, RequirementsSummary, StreamStageEvent } from '../types'

/**
 * 编排层测试：把流式接口整体 mock 掉，专注验证「第一步换成结构化表单」后
 * 阶段流转、载荷映射、本地快照恢复，以及两道确认闸门（摘要 Diff / 规范检索）
 * 是否按「先确认、后生成」的约定工作。
 */
vi.mock('../services/api', () => ({
  startConversationStream: vi.fn(),
  continueConversationStream: vi.fn(),
  generatePrdFromSummaryStream: vi.fn(),
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
  vi.mocked(api.generatePrdFromSummaryStream).mockImplementation(async (_payload, handlers) => {
    handlers?.onChunk?.('# 产品需求文档\n')
    return { conversationId: '', reply: '# 产品需求文档\n\n## 1. 项目背景\n内容' }
  })
  // 默认：提示词生成成功（提示词调试入口直接从这一步开始）
  vi.mocked(api.generatePromptsStream).mockImplementation(async (_payload, handlers) => {
    handlers?.onChunk?.('# 提示词套件\n')
    return { conversationId: '', reply: '# 提示词套件\n\n## 一、角色设定\n内容' }
  })
})

/** 挂载应用，停在入口选择页 */
function mountEntry(): VueWrapper {
  return mount(AppV2, { global: { plugins: [createPinia()] } })
}

/**
 * 挂载应用并选择「完整流程」入口。
 * 绝大多数用例验证的是完整流程内部的行为，入口选择对它只是前置动作。
 */
async function mountApp(): Promise<VueWrapper> {
  const wrapper = mountEntry()
  await clickButton(wrapper, '完整流程')
  return wrapper
}

describe('AppV2 完整流程编排', () => {
  it('默认停在入口选择页：三条入口可选，且尚未发起任何请求', () => {
    const wrapper = mountEntry()

    expect(wrapper.text()).toContain('选择一种开始方式')
    expect(wrapper.text()).toContain('完整流程')
    expect(wrapper.text()).toContain('PRD 快捷')
    expect(wrapper.text()).toContain('提示词调试')
    // 入口页还没有进度可言，进度条整体不出现
    expect(wrapper.find('nav[aria-label="生成进度"]').exists()).toBe(false)
    expect(api.startConversationStream).not.toHaveBeenCalled()
  })

  it('选择「完整流程」后显示进度条与结构化表单，不显示对话区', async () => {
    const wrapper = await mountApp()

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

    const wrapper = await mountApp()
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

    const wrapper = await mountApp()
    await reachPrdReview(wrapper)

    expect(wrapper.text()).toContain('PRD 文档')
    expect(wrapper.text()).toContain('项目背景')

    // PRD 生成拿到的是与 field-schema 对齐的结构化 JSON
    const payload = vi.mocked(api.generatePrdFromSummaryStream).mock.calls[0]?.[0]
    // 直接下发结构化摘要字典，需求文本由后端统一格式化
    expect(payload?.summary?.product_name).toBe('HarnessPRD')
    expect(payload?.summary?.mvp_features?.[0]?.name).toBe('问卷填写')
    // 对话历史一并带上：首轮只有 AI 那条回复（需求本身走 summary 传入）
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

    const wrapper = await mountApp()
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
    expect(api.generatePrdFromSummaryStream).not.toHaveBeenCalled()

    // 回填请求带上了当前摘要与完整对话历史
    const syncPayload = vi.mocked(api.syncSummaryFromConversation).mock.calls[0]?.[0]
    expect(syncPayload?.summary.product_name).toBe('HarnessPRD')
    expect(syncPayload?.history?.[0]?.role).toBe('assistant')

    await clickButton(wrapper, '确认更新')
    await flushPromises()

    // 确认后摘要被写回，PRD 需求文本里带上了新增的目标用户
    expect(wrapper.text()).toContain('PRD 文档')
    expect(api.generatePrdFromSummaryStream).toHaveBeenCalledTimes(1)
    const prdPayload = vi.mocked(api.generatePrdFromSummaryStream).mock.calls[0]?.[0]
    // 回填结果已写回摘要，并作为生成输入下发
    expect(prdPayload?.summary?.target_users).toContain('小团队产品经理')
    expect(prdPayload?.summary?.mvp_features?.[0]?.user_value).toBe('省时间')
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

    const wrapper = await mountApp()
    await submitSampleForm(wrapper)
    await clickButton(wrapper, '生成 PRD', true)
    await flushPromises()

    expect(wrapper.text()).toContain('运行平台')

    await clickButton(wrapper, '拒绝更改')
    await flushPromises()

    expect(wrapper.text()).toContain('需求澄清对话')
    expect(api.generatePrdFromSummaryStream).not.toHaveBeenCalled()
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

    const wrapper = await mountApp()
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

    const wrapper = await mountApp()
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

    const wrapper = await mountApp()
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

    const wrapper = await mountApp()
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

    // 快照已经把阶段定在 review-prd，直接挂载即可（不需要经过入口页）
    const wrapper = mountEntry()
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

    const wrapper = mountEntry()
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

    const wrapper = mountEntry()
    expect(wrapper.text()).toContain('上次的确认步骤未完成')
    expect(wrapper.text()).toContain('需求澄清对话')
    expect(wrapper.text()).not.toContain('需求摘要更新确认')
  })

  it('重新开始：回到入口页并清空表单、入口模式与本地快照', async () => {
    vi.mocked(api.startConversationStream).mockImplementation(async () => ({
      conversationId: 'conv-1',
      reply: '已理解需求。',
    }))

    const wrapper = await mountApp()
    await submitSampleForm(wrapper)
    await clickButton(wrapper, '重新开始')
    await nextTick()

    // 重新开始回到入口页，而不是直接落回表单
    expect(wrapper.text()).toContain('选择一种开始方式')

    // 快照被持久化 watch 重新写成了「空态」——所以断言内容而不是「键不存在」：
    // 重开一次页面必须回到入口页，而不是把上一轮的产物又拉回来
    const snapshot = JSON.parse(localStorage.getItem(SESSION_KEY) ?? '{}')
    expect(snapshot.viewState).toBe('entry')
    expect(snapshot.entryMode).toBeNull()
    expect(snapshot.quickInput).toEqual({ productName: '', requirements: '' })
    expect(snapshot.debugPrdText).toBe('')
    expect(snapshot.prdContent).toBe('')

    // 再进完整流程时，表单是重建后的空表单（依赖 formKey 自增强制重建）
    await clickButton(wrapper, '完整流程')
    expect(wrapper.text()).toContain('告诉我们你的产品想法')
    expect((byId(wrapper, 'field-product_name').element as HTMLInputElement).value).toBe('')
    expect(wrapper.text()).toContain('还没有功能项')
  })

  it('提交失败时保留错误提示，不进入空白对话页', async () => {
    vi.mocked(api.startConversationStream).mockRejectedValue(new Error('后端不可用'))

    const wrapper = await mountApp()
    await submitSampleForm(wrapper)

    expect(wrapper.text()).toContain('需求澄清对话')
    expect(wrapper.text()).toContain('后端不可用')
  })
})

/**
 * 三条入口（第三步）的前端表现。
 *
 * 三条入口共用「生成 → 审阅 → 下载」链路，差异只有三处：需求从哪来、进度条显示哪几步、
 * 完成需要哪几份文档。因此这里重点钉住这三处，外加一条硬约束——
 * **快捷入口不能经过摘要确认闸门**（那道闸门是给「对话补充信息回填」用的，
 * 快捷入口根本没有对话）。
 */
describe('AppV2 入口模式', () => {
  /** 进入 PRD 快捷入口并填好一段话 */
  async function fillQuickPrd(wrapper: VueWrapper): Promise<void> {
    await clickButton(wrapper, 'PRD 快捷')
    await byId(wrapper, 'field-quick-product-name').setValue('小账本')
    await byId(wrapper, 'field-quick-requirements').setValue('做一个记账 App，支持多人共享账本。')
  }

  it('PRD 快捷：一段话直接生成 PRD，不经过摘要确认闸门', async () => {
    const wrapper = mountEntry()
    await fillQuickPrd(wrapper)

    // 跳过了表单与对话：进度条只剩 PRD / 接口文档 / 提示词三步
    expect(wrapper.text()).toContain('直接生成 PRD')
    expect(wrapper.text()).not.toContain('需求澄清对话')
    expect(wrapper.findAll('nav[aria-label="生成进度"] li')).toHaveLength(3)
    expect(wrapper.text()).not.toContain('描述产品')

    await clickButton(wrapper, '生成 PRD', true)
    await flushPromises()

    // 关键：一次摘要回填都没发生，也就没有弹过确认面板
    expect(api.syncSummaryFromConversation).not.toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('需求摘要更新确认')
    expect(api.generatePrdFromSummaryStream).toHaveBeenCalledTimes(1)

    const payload = vi.mocked(api.generatePrdFromSummaryStream).mock.calls[0]?.[0]
    expect(payload?.requirements).toBe('做一个记账 App，支持多人共享账本。')
    expect(payload?.productName).toBe('小账本')
    // 没有结构化摘要，也没有对话历史
    expect(payload?.summary ?? null).toBeNull()
    expect(payload?.history).toEqual([])

    expect(api.startConversationStream).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('PRD 文档')
    expect(wrapper.text()).toContain('项目背景')
  })

  it('PRD 快捷：未填需求时不能提交', async () => {
    const wrapper = mountEntry()
    await clickButton(wrapper, 'PRD 快捷')

    const generate = wrapper.findAll('button').find((item) => item.text().trim() === '生成 PRD')
    expect(generate).toBeDefined()
    expect((generate?.element as HTMLButtonElement).disabled).toBe(true)

    // 缺失提示必须提前常驻在字段下方：按钮禁用后点不动，靠点击报错是收不到反馈的
    expect(wrapper.text()).toContain('请至少写一句需求描述')
  })

  it('PRD 快捷：「返回描述」回到录入页且内容还在，返回按钮文案随模式变化', async () => {
    const wrapper = mountEntry()
    await fillQuickPrd(wrapper)
    await clickButton(wrapper, '生成 PRD', true)
    await flushPromises()

    // 返回目标不是对话页，文案也不能写「返回对话」
    expect(wrapper.text()).toContain('返回描述')
    expect(wrapper.text()).not.toContain('返回对话')

    await clickButton(wrapper, '返回描述', true)
    expect(wrapper.text()).toContain('直接生成 PRD')
    // 录入内容由 store 持有，来回切换不丢
    expect((byId(wrapper, 'field-quick-requirements').element as HTMLTextAreaElement).value).toBe(
      '做一个记账 App，支持多人共享账本。',
    )
  })

  it('PRD 快捷：只要 PRD 就能完成，完成页不留空文档卡片', async () => {
    const wrapper = mountEntry()
    await fillQuickPrd(wrapper)
    await clickButton(wrapper, '生成 PRD', true)
    await flushPromises()

    // 只有 PRD 时「完成」应该可用（完整流程下同样条件是不可用的）
    const finish = wrapper.findAll('button').find((item) => item.text().trim() === '完成')
    expect((finish?.element as HTMLButtonElement).disabled).toBe(false)

    await clickButton(wrapper, '完成', true)
    await flushPromises()

    expect(wrapper.text()).toContain('文档已生成完毕')
    // 只有 PRD 一张卡片：没生成过的文档不该留一个点进去是空的卡片
    // （文本断言会被顶部导航的步骤名与常驻表单污染，因此数卡片元素）
    const doneCards = wrapper.findAll('article')
    expect(doneCards).toHaveLength(1)
    expect(doneCards[0]?.text()).toContain('PRD 文档')
    expect(doneCards[0]?.text()).not.toContain('接口文档')
    expect(doneCards[0]?.text()).not.toContain('提示词套件')
  })

  it('提示词调试：贴入 PRD 直接生成提示词套件，且不出现「查看 PRD」', async () => {
    const wrapper = mountEntry()
    await clickButton(wrapper, '提示词调试')

    expect(wrapper.text()).toContain('从现有 PRD 生成提示词套件')
    // 该模式的产物只有提示词，全程只有一步——单步进度条没有信息量，整体不显示
    expect(wrapper.find('nav[aria-label="生成进度"]').exists()).toBe(false)

    await byId(wrapper, 'field-debug-prd').setValue(
      '# 小账本 产品需求文档（PRD）\n\n## 1. 产品概述\n家庭记账工具',
    )
    await clickButton(wrapper, '生成提示词套件', true)
    await flushPromises()

    const payload = vi.mocked(api.generatePromptsStream).mock.calls[0]?.[0]
    expect(payload?.prdContent).toContain('家庭记账工具')
    expect(wrapper.text()).toContain('提示词套件')
    expect(wrapper.text()).toContain('角色设定')

    // 没生成过 PRD，就不能出现会点进空文档的入口；返回目标也是录入页
    expect(wrapper.text()).not.toContain('查看 PRD')
    expect(wrapper.text()).toContain('返回 PRD')

    // 只有提示词也能完成，且完成页只留这一张卡片
    await clickButton(wrapper, '完成', true)
    await flushPromises()
    expect(wrapper.text()).toContain('文档已生成完毕')
    const doneCards = wrapper.findAll('article')
    expect(doneCards).toHaveLength(1)
    expect(doneCards[0]?.text()).toContain('提示词套件')
    expect(doneCards[0]?.text()).not.toContain('接口文档')
  })

  it('提示词调试：未贴入 PRD 时不能提交', async () => {
    const wrapper = mountEntry()
    await clickButton(wrapper, '提示词调试')

    const generate = wrapper.findAll('button').find((item) => item.text().trim() === '生成提示词套件')
    expect((generate?.element as HTMLButtonElement).disabled).toBe(true)

    expect(api.generatePromptsStream).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('请先贴入 PRD 正文')
  })

  it('两个录入页都能「返回入口」，重新选另一条入口', async () => {
    const wrapper = mountEntry()
    await clickButton(wrapper, 'PRD 快捷')
    expect(wrapper.text()).toContain('直接生成 PRD')

    await clickButton(wrapper, '返回入口', true)
    expect(wrapper.text()).toContain('选择一种开始方式')

    await clickButton(wrapper, '提示词调试')
    expect(wrapper.text()).toContain('从现有 PRD 生成提示词套件')

    await clickButton(wrapper, '返回入口', true)
    await clickButton(wrapper, '完整流程')
    expect(wrapper.text()).toContain('告诉我们你的产品想法')
    // 换了入口，进度条口径也跟着换回五步
    expect(wrapper.findAll('nav[aria-label="生成进度"] li')).toHaveLength(5)
  })

  it('完整流程的表单也有返回按钮，默认回到入口页', async () => {
    const wrapper = await mountApp()
    expect(wrapper.text()).toContain('告诉我们你的产品想法')

    await clickButton(wrapper, '返回入口', true)
    expect(wrapper.text()).toContain('选择一种开始方式')
  })

  it('从对话页「改需求」回到表单时，返回按钮改为回到对话', async () => {
    vi.mocked(api.startConversationStream).mockImplementation(async (_payload, handlers) => {
      handlers?.onChunk?.('已理解需求。')
      return { conversationId: 'conv-1', reply: '已理解需求。' }
    })

    const wrapper = await mountApp()
    await submitSampleForm(wrapper)
    expect(wrapper.text()).toContain('需求澄清对话')

    await clickButton(wrapper, '改需求')
    expect(wrapper.text()).toContain('告诉我们你的产品想法')
    // 已经进过对话：落点与文案都要跟着变，不能把用户送回入口页丢掉对话位置
    expect(wrapper.text()).not.toContain('返回入口')
    expect(wrapper.text()).toContain('返回对话')

    await clickButton(wrapper, '返回对话', true)
    expect(wrapper.text()).toContain('需求澄清对话')
  })
})

/**
 * 分章节生成（第二阶段）的前端表现。
 *
 * 用一个「手动放行」的流替代默认 mock：先把回调交出来，再逐步推送 stage 事件，
 * 这样才有机会观察到生成过程中的中间态（默认 mock 会立刻跑完并跳到待确认页）。
 */
describe('AppV2 分章节生成的阶段展示', () => {
  /** 手动控制的一次 PRD 流 */
  interface ControlledStream {
    /** 后端回调 */
    handlers: api.StreamHandlers | undefined
    /** 结束这次流 */
    finish: (reply: string) => Promise<void>
  }

  /** 把 PRD 生成换成可手动推进的流，并走到「生成中」阶段 */
  async function startControlledPrdGeneration(wrapper: VueWrapper): Promise<ControlledStream> {
    const state: ControlledStream = { handlers: undefined, finish: async () => {} }
    // 先把对话环节走通（拿到「生成 PRD」入口），再接管 PRD 生成
    vi.mocked(api.startConversationStream).mockResolvedValue({
      conversationId: 'conv-1',
      reply: '已理解需求。',
    })
    vi.mocked(api.generatePrdFromSummaryStream).mockImplementation((_payload, handlers) => {
      state.handlers = handlers
      return new Promise((resolve) => {
        state.finish = async (reply: string) => {
          resolve({ conversationId: '', reply })
          await flushPromises()
        }
      })
    })

    await submitSampleForm(wrapper)
    await clickButton(wrapper, '生成 PRD', true)
    await flushPromises()
    await confirmSummaryPanel(wrapper)
    await nextTick()
    return state
  }

  /** 推送一条章节级 stage 事件 */
  async function pushStage(
    stream: ControlledStream,
    stage: string,
    extra: Partial<StreamStageEvent> = {},
  ): Promise<void> {
    stream.handlers?.onStage?.({
      stage,
      sectionId: 'overview',
      sectionTitle: '产品概述',
      sectionHeading: '## 1. 产品概述',
      index: 0,
      total: 7,
      note: '',
      issues: [],
      ...extra,
    })
    await nextTick()
  }

  it('按 stage 事件展示阶段与章节计数', async () => {
    const wrapper = await mountApp()
    const stream = await startControlledPrdGeneration(wrapper)

    // 进入生成态：阶段条先给出「撰写中」，但还没有章节信息
    expect(wrapper.text()).toContain('正在撰写章节')
    expect(wrapper.text()).not.toContain('第 1 / 7 章')

    await pushStage(stream, 'writing')
    stream.handlers?.onChunk?.('## 1. 产品概述\n\n第一版内容\n')
    await nextTick()
    expect(wrapper.text()).toContain('正在撰写 产品概述')
    expect(wrapper.text()).toContain('第 1 / 7 章')
    expect(wrapper.text()).toContain('第一版内容')

    // 换到第 2 章：计数跟着走
    await pushStage(stream, 'reviewing', {
      sectionId: 'features',
      sectionTitle: '功能需求',
      sectionHeading: '## 2. 功能需求',
      index: 1,
    })
    expect(wrapper.text()).toContain('正在审核 功能需求')
    expect(wrapper.text()).toContain('第 2 / 7 章')

    await stream.finish('# 产品需求文档\n\n## 1. 产品概述\n\n第一版内容')
    expect(wrapper.text()).toContain('PRD 文档')
  })

  it('改写时丢弃该章已接收的文本，并给出原因', async () => {
    const wrapper = await mountApp()
    const stream = await startControlledPrdGeneration(wrapper)

    // 文档头先到（此时还没有章节开始）
    await pushStage(stream, 'writing')
    stream.handlers?.onChunk?.('## 1. 产品概述\n\n第一版内容（编造了指标）\n')
    await nextTick()
    expect(wrapper.text()).toContain('第一版内容')

    await pushStage(stream, 'reviewing')
    await pushStage(stream, 'rewriting', {
      note: '出现了输入中没有的性能指标',
      issues: [{ severity: 'high', detail: '出现了输入中没有的性能指标' }],
    })

    // 被改写的那一章已流出的文本必须消失，否则用户会看到同一章出现两次
    expect(wrapper.text()).not.toContain('第一版内容')
    expect(wrapper.text()).toContain('本章审核未通过，正在重写')
    expect(wrapper.text()).toContain('出现了输入中没有的性能指标')

    // 重写后的内容重新流进来
    stream.handlers?.onChunk?.('## 1. 产品概述\n\n重写后的内容\n')
    await nextTick()
    expect(wrapper.text()).toContain('重写后的内容')
    expect(wrapper.text()).not.toContain('编造了指标')

    await stream.finish('# 产品需求文档\n\n## 1. 产品概述\n\n重写后的内容')
    expect(wrapper.text()).toContain('PRD 文档')
  })

  it('后端降级为整篇生成（没有 stage 事件）时不显示章节计数，流程照常走完', async () => {
    vi.mocked(api.startConversationStream).mockResolvedValue({
      conversationId: 'conv-1',
      reply: '已理解需求。',
    })
    vi.mocked(api.generatePrdFromSummaryStream).mockImplementation(async (_payload, handlers) => {
      handlers?.onChunk?.('# 产品需求文档\n\n## 1. 项目背景\n内容')
      return { conversationId: '', reply: '# 产品需求文档\n\n## 1. 项目背景\n内容' }
    })

    const wrapper = await mountApp()
    await reachPrdReview(wrapper)

    expect(wrapper.text()).toContain('PRD 文档')
    expect(wrapper.text()).not.toContain('第 1 /')
    expect(wrapper.text()).not.toContain('逐章撰写')
  })
})
