import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import SolutionSidebar from '../components/SolutionSidebar.vue'
import * as solutionApi from '../services/solutionService'
import { useAuthStore } from '../stores/auth'
import { useSolutionsStore } from '../stores/solutions'
import type { SolutionSummary } from '../types'

/**
 * 方案侧边栏测试。
 *
 * 盯住四件容易做错、做错了用户会直接踩坑的事：
 * 1. 删除有**行内二次确认**——不可逆动作不该点一下就没了；
 * 2. 相对时间的时区处理（后端存的是不带后缀的 UTC，补错 Z 会整体差 8 小时）；
 * 3. 空态 / 加载态 / 列表态的区分；
 * 4. 点当前项不重复请求。
 */

vi.mock('../services/solutionService', () => ({
  SolutionNotFoundError: class SolutionNotFoundError extends Error {
    constructor() {
      super('方案不存在或已被删除')
      this.name = 'SolutionNotFoundError'
    }
  },
  fetchSolutions: vi.fn(),
  fetchSolution: vi.fn(),
  createSolution: vi.fn(),
  updateSolution: vi.fn(),
  deleteSolution: vi.fn(),
}))

function summary(id: number, overrides: Partial<SolutionSummary> = {}): SolutionSummary {
  return {
    id,
    title: `方案 ${id}`,
    mode: 'structured',
    stage: 'form',
    created_at: '2026-10-13T00:00:00',
    updated_at: '2026-10-13T00:00:00',
    ...overrides,
  }
}

/**
 * 造一个真实的 router（内存 history）。
 *
 * 侧边栏通过**改地址**来切换方案（而不是直接加载），因为地址是「当前打开哪一份」
 * 的权威来源；因此测试必须能观察到地址真的变了。
 */
function makeRouter(): Router {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/v2', component: { template: '<div />' } },
      { path: '/v2/:id', component: { template: '<div />' } },
    ],
  })
}

/** 挂载侧边栏并等首次列表请求落地 */
async function mountSidebar(router: Router = makeRouter()): Promise<VueWrapper> {
  await router.push('/v2')
  // 侧边栏常驻，但「新建任务」与方案列表只在登录后展示，因此这里预置登录态
  const auth = useAuthStore()
  auth.user = { id: 1, username: 'tester' }
  // 组件与用例共用同一个 pinia（`beforeEach` 里已激活），
  // 否则用例改 `solutions.currentId` 影响不到已挂载的组件
  const wrapper = mount(SolutionSidebar, { global: { plugins: [router] } })
  await flushPromises()
  return wrapper
}

/** 找文案包含指定文字的按钮 */
function findButton(wrapper: VueWrapper, text: string) {
  const button = wrapper.findAll('button').find((item) => item.text().includes(text))
  if (!button) throw new Error(`未找到按钮：${text}`)
  return button
}

let solutions: ReturnType<typeof useSolutionsStore>

beforeEach(() => {
  vi.clearAllMocks()
  setActivePinia(createPinia())
  solutions = useSolutionsStore()
  vi.mocked(solutionApi.fetchSolutions).mockResolvedValue([])
  vi.mocked(solutionApi.deleteSolution).mockResolvedValue(undefined)
})

afterEach(() => {
  vi.useRealTimers()
})

describe('列表展示', () => {
  it('挂载时拉取列表', async () => {
    vi.mocked(solutionApi.fetchSolutions).mockResolvedValue([summary(1)])

    const wrapper = await mountSidebar()

    expect(solutionApi.fetchSolutions).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).toContain('方案 1')
  })

  it('没有方案时显示空态引导', async () => {
    const wrapper = await mountSidebar()

    expect(wrapper.text()).toContain('还没有方案')
  })

  it('标题与阶段名都展示出来', async () => {
    vi.mocked(solutionApi.fetchSolutions).mockResolvedValue([
      summary(1, { title: '订单系统', stage: 'review-prd' }),
    ])

    const wrapper = await mountSidebar()

    expect(wrapper.text()).toContain('订单系统')
    expect(wrapper.text()).toContain('已出 PRD')
  })

  it('已加载过就不再重复请求（切来切去不该每次都打后端）', async () => {
    vi.mocked(solutionApi.fetchSolutions).mockResolvedValue([summary(1)])
    await mountSidebar()

    // 卸载后重新挂载同一个组件（store 仍在），不该再拉一次
    await mountSidebar()

    expect(solutionApi.fetchSolutions).toHaveBeenCalledTimes(1)
  })
})

describe('相对时间', () => {
  it('把不带时区后缀的 UTC 时间按 UTC 解析，而不是当成本地时间', async () => {
    // 后端给的 `2026-10-13T00:00:00` 是 UTC；若不补 Z，东八区会当成 8 小时前
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-13T00:30:00Z'))
    vi.mocked(solutionApi.fetchSolutions).mockResolvedValue([
      summary(1, { updated_at: '2026-10-13T00:00:00' }),
    ])

    const wrapper = await mountSidebar()

    // 差了 30 分钟，而不是 8 小时 30 分
    expect(wrapper.text()).toContain('30 分钟前')
  })

  it('一分钟以内显示「刚刚」', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-13T00:00:30Z'))
    vi.mocked(solutionApi.fetchSolutions).mockResolvedValue([
      summary(1, { updated_at: '2026-10-13T00:00:00' }),
    ])

    const wrapper = await mountSidebar()

    expect(wrapper.text()).toContain('刚刚')
  })

  it('超过一天显示具体日期', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-15T00:00:00Z'))
    vi.mocked(solutionApi.fetchSolutions).mockResolvedValue([
      summary(1, { updated_at: '2026-10-13T00:00:00' }),
    ])

    const wrapper = await mountSidebar()

    expect(wrapper.text()).toMatch(/10-1[34]\s+\d{2}:\d{2}/)
  })
})

describe('删除的行内二次确认', () => {
  it('点删除先出确认条，不直接删', async () => {
    vi.mocked(solutionApi.fetchSolutions).mockResolvedValue([summary(1, { title: '订单系统' })])
    const wrapper = await mountSidebar()

    await wrapper.find('[aria-label="删除方案 订单系统"]').trigger('click')

    expect(wrapper.text()).toContain('删除「订单系统」？')
    expect(solutionApi.deleteSolution).not.toHaveBeenCalled()
  })

  it('确认后才真的删', async () => {
    vi.mocked(solutionApi.fetchSolutions).mockResolvedValue([summary(1, { title: '订单系统' })])
    const wrapper = await mountSidebar()

    await wrapper.find('[aria-label="删除方案 订单系统"]').trigger('click')
    await findButton(wrapper, '删除').trigger('click')
    await flushPromises()

    expect(solutionApi.deleteSolution).toHaveBeenCalledWith(1)
  })

  it('取消则恢复原样，不发删除请求', async () => {
    vi.mocked(solutionApi.fetchSolutions).mockResolvedValue([summary(1, { title: '订单系统' })])
    const wrapper = await mountSidebar()

    await wrapper.find('[aria-label="删除方案 订单系统"]').trigger('click')
    await findButton(wrapper, '取消').trigger('click')

    expect(wrapper.text()).not.toContain('删除「订单系统」？')
    expect(wrapper.find('[aria-label="删除方案 订单系统"]').exists()).toBe(true)
    expect(solutionApi.deleteSolution).not.toHaveBeenCalled()
  })
})

describe('切换与新建', () => {
  /** 列表项的「打开」按钮：文案包含标题 */
  function openButton(wrapper: VueWrapper, title: string) {
    const button = wrapper
      .findAll('button')
      .find((item) => item.text().includes(title) && !item.attributes('aria-label'))
    if (!button) throw new Error(`未找到方案项：${title}`)
    return button
  }

  it('点列表项改地址到该方案（地址是「打开哪一份」的权威来源）', async () => {
    vi.mocked(solutionApi.fetchSolutions).mockResolvedValue([summary(7, { title: '订单系统' })])
    const router = makeRouter()
    const wrapper = await mountSidebar(router)

    await openButton(wrapper, '订单系统').trigger('click')
    await flushPromises()

    expect(router.currentRoute.value.path).toBe('/v2/7')
    // 侧边栏自己不加载内容：改完地址由 AppV2 的 watch 负责，避免两条路径各行其是
    expect(solutionApi.fetchSolution).not.toHaveBeenCalled()
  })

  it('点当前已打开的项不重复导航', async () => {
    vi.mocked(solutionApi.fetchSolutions).mockResolvedValue([summary(7, { title: '订单系统' })])
    const router = makeRouter()
    const wrapper = await mountSidebar(router)
    // 组件与用例共用同一个 store，因此这里的状态会反映到已挂载的界面上
    solutions.currentId = 7
    await flushPromises()

    await openButton(wrapper, '订单系统').trigger('click')
    await flushPromises()

    expect(router.currentRoute.value.path).toBe('/v2')
  })

  it('「新建任务」清空当前会话并把地址回退到 /v2', async () => {
    vi.mocked(solutionApi.fetchSolutions).mockResolvedValue([summary(7)])
    const router = makeRouter()
    const wrapper = await mountSidebar(router)
    await router.push('/v2/7')
    solutions.currentId = 7
    await flushPromises()

    await findButton(wrapper, '新建任务').trigger('click')
    await flushPromises()

    expect(router.currentRoute.value.path).toBe('/v2')
    // 旧方案仍在列表里
    expect(solutions.items.map((item) => item.id)).toContain(7)
  })
})
