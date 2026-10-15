import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia, type Pinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import AppV2 from '../AppV2.vue'
import AuthDialog from '../components/AuthDialog.vue'
import UserMenu from '../components/UserMenu.vue'
import * as authService from '../services/authService'
import { useAuthStore } from '../stores/auth'

/**
 * 登录相关测试。
 *
 * 重点不是「按钮点得动」，而是三件容易出错、且出错后用户完全不知道发生了什么的事：
 * 1. 未登录时点入口必须被拦下，且**停在入口页**；
 * 2. 登录成功后**不自动进入**之前选的入口（用户多半已经忘了要选哪个）；
 * 3. 后端返回的错误文案要原样展示，不能被吞成一句「请求失败」。
 */

vi.mock('../services/api', () => ({
  onUnauthorized: vi.fn(),
  startConversationStream: vi.fn(),
  continueConversationStream: vi.fn(),
  generatePrdFromSummaryStream: vi.fn(),
  generateApiDocsStream: vi.fn(),
  generatePromptsStream: vi.fn(),
  optimizeDocumentStream: vi.fn(),
  syncSummaryFromConversation: vi.fn(),
  // 生成任务接口：登录门禁用例不会触发，但整体 mock 一个模块时应列全，
  // 否则新增用例一旦走到任务链路，拿到的会是 `undefined` 而不是一个可断言的桩。
  createJob: vi.fn(),
  readJobStream: vi.fn(),
  fetchJob: vi.fn(),
  fetchActiveJobs: vi.fn(async () => []),
}))

vi.mock('../services/authService', () => ({
  fetchCurrentUser: vi.fn(),
  login: vi.fn(),
  register: vi.fn(),
  logout: vi.fn(),
}))

/**
 * 方案持久化 mock。
 *
 * 本文件测的是登录门禁；挂载 AppV2 会顺带触发方案列表请求，给一组无害实现即可，
 * 免得每个用例都真的发一次 HTTP。
 */
vi.mock('../services/solutionService', () => ({
  SolutionNotFoundError: class SolutionNotFoundError extends Error {},
  fetchSolutions: vi.fn(async () => []),
  fetchSolution: vi.fn(async () => {
    throw new Error('本测试不加载已有方案')
  }),
  createSolution: vi.fn(async () => ({
    id: 1,
    title: '测试方案',
    mode: 'structured',
    stage: 'form',
    created_at: '',
    updated_at: '',
  })),
  updateSolution: vi.fn(async () => null),
  deleteSolution: vi.fn(async () => undefined),
}))

/**
 * 造一个真实的 router（内存 history）。
 *
 * AppV2 从路由参数读方案 id，缺了 router 会在 setup 阶段就抛
 * 「Cannot read properties of undefined (reading 'params')」。
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

/** 造一个空的 Pinia 实例并激活它 */
function freshPinia(): Pinia {
  const pinia = createPinia()
  setActivePinia(pinia)
  return pinia
}

/** 预置为已登录的 Pinia，用于「已登录」相关的用例 */
function loggedInPinia(): Pinia {
  const pinia = freshPinia()
  const auth = useAuthStore(pinia)
  auth.user = { id: 1, username: 'alice' }
  auth.ready = true
  return pinia
}

/** 查询 document.body（弹窗 Teleport 到 body，wrapper.find 搜不到） */
function findInBody<T extends Element>(selector: string): T | null {
  return document.body.querySelector<T>(selector)
}

/** 在 body 里找文案包含指定文字的按钮（用于页签这类非提交按钮） */
function buttonInBody(text: string): HTMLButtonElement {
  const button = Array.from(document.body.querySelectorAll('button')).find((item) =>
    item.textContent?.includes(text),
  )
  if (!button) throw new Error(`未找到按钮：${text}`)
  return button
}

/** 取弹窗里的所有密码框（登录 1 个，注册 2 个） */
function passwordInputs(): HTMLInputElement[] {
  return Array.from(document.body.querySelectorAll<HTMLInputElement>('input[type="password"]'))
}

/**
 * 提交弹窗表单。
 *
 * 直接派发 submit 事件而不是点提交按钮，有两个原因：
 * 一是 jsdom 对表单提交的支持不完整，二是「登录」这个文案同时出现在页签、
 * 提交按钮和顶栏上，按文案找按钮会点错。
 */
function submitDialogForm(): void {
  const form = findInBody<HTMLFormElement>('form')
  if (!form) throw new Error('未找到登录表单')
  form.dispatchEvent(new Event('submit', { cancelable: true }))
}

/** 给弹窗里的输入框赋值并触发 Vue 的响应式更新 */
function setInput(selector: string, value: string): void {
  const input = findInBody<HTMLInputElement>(selector)
  if (!input) throw new Error(`未找到输入框：${selector}`)
  input.value = value
  input.dispatchEvent(new Event('input'))
}

/** 按占位符定位邀请码输入框 */
const INVITE_INPUT = 'input[placeholder="注册需要邀请码"]'

beforeEach(() => {
  vi.clearAllMocks()
  document.body.innerHTML = ''
})

// ============================================================================
// store
// ============================================================================

describe('登录态 store', () => {
  it('首次探测未登录时保持未登录，且只问后端一次', async () => {
    vi.mocked(authService.fetchCurrentUser).mockResolvedValue(null)
    const auth = useAuthStore(freshPinia())

    await auth.load()
    expect(auth.isLoggedIn).toBe(false)

    await auth.load()
    expect(authService.fetchCurrentUser).toHaveBeenCalledTimes(1)
  })

  it('登录成功后写入用户并关闭弹窗', async () => {
    vi.mocked(authService.login).mockResolvedValue({ id: 7, username: 'alice' })
    const auth = useAuthStore(freshPinia())
    auth.openDialog('login')

    const ok = await auth.login({ username: 'alice', password: 'password123' })

    expect(ok).toBe(true)
    expect(auth.user).toEqual({ id: 7, username: 'alice' })
    expect(auth.dialogOpen).toBe(false)
  })

  it('登录失败时保留后端给出的错误文案', async () => {
    vi.mocked(authService.login).mockRejectedValue(new Error('用户名或密码错误'))
    const auth = useAuthStore(freshPinia())

    const ok = await auth.login({ username: 'alice', password: 'wrong' })

    expect(ok).toBe(false)
    expect(auth.errorMessage).toBe('用户名或密码错误')
  })

  it('注册成功后同样视为已登录（后端会直接签发会话）', async () => {
    vi.mocked(authService.register).mockResolvedValue({ id: 9, username: 'bob' })
    const auth = useAuthStore(freshPinia())

    const ok = await auth.register({ username: 'bob', password: 'password123', inviteCode: 'x' })

    expect(ok).toBe(true)
    expect(auth.isLoggedIn).toBe(true)
  })

  it('登出后即使后端失败也清掉本地登录态', async () => {
    vi.mocked(authService.logout).mockRejectedValue(new Error('网络错误'))
    const auth = useAuthStore(loggedInPinia())

    await auth.logout()

    expect(auth.isLoggedIn).toBe(false)
  })

  it('已登录时 requireLogin 不再弹窗', () => {
    const auth = useAuthStore(loggedInPinia())

    auth.requireLogin('创建任务需要先登录')

    expect(auth.dialogOpen).toBe(false)
  })

  it('未登录时 requireLogin 弹窗并带上原因', () => {
    const auth = useAuthStore(freshPinia())
    auth.ready = true

    auth.requireLogin('登录状态已失效，请重新登录')

    expect(auth.dialogOpen).toBe(true)
    expect(auth.promptReason).toBe('登录状态已失效，请重新登录')
  })
})

// ============================================================================
// 弹窗
// ============================================================================

describe('登录 / 注册弹窗', () => {
  /** 挂载一个已展开的弹窗 */
  function mountDialog(mode: 'login' | 'register' = 'login'): VueWrapper {
    const pinia = freshPinia()
    const auth = useAuthStore(pinia)
    auth.openDialog(mode)
    return mount(AuthDialog, { global: { plugins: [pinia] } })
  }

  it('登录页签只有一个密码框，切到注册后多出确认密码与邀请码', async () => {
    mountDialog()
    await flushPromises()

    expect(passwordInputs()).toHaveLength(1)
    expect(findInBody(INVITE_INPUT)).toBeNull()

    buttonInBody('注册').click()
    await flushPromises()

    expect(passwordInputs()).toHaveLength(2)
    expect(findInBody(INVITE_INPUT)).not.toBeNull()
  })

  it('两次密码不一致时本地拦下，不发请求', async () => {
    mountDialog('register')
    await flushPromises()

    setInput('input[autocomplete="username"]', 'alice')
    passwordInputs().forEach((input, index) => {
      input.value = index === 0 ? 'password123' : 'password456'
      input.dispatchEvent(new Event('input'))
    })
    setInput(INVITE_INPUT, '123456')

    submitDialogForm()
    await flushPromises()

    expect(authService.register).not.toHaveBeenCalled()
    expect(document.body.textContent).toContain('两次输入的密码不一致')
  })

  it('未填邀请码时本地拦下，不发请求', async () => {
    mountDialog('register')
    await flushPromises()

    setInput('input[autocomplete="username"]', 'alice')
    passwordInputs().forEach((input) => {
      input.value = 'password123'
      input.dispatchEvent(new Event('input'))
    })

    submitDialogForm()
    await flushPromises()

    expect(authService.register).not.toHaveBeenCalled()
    expect(document.body.textContent).toContain('请填写邀请码')
  })

  it('填写完整后调用注册接口，并把邀请码映射成后端的 snake_case 字段', async () => {
    vi.mocked(authService.register).mockResolvedValue({ id: 1, username: 'alice' })
    mountDialog('register')
    await flushPromises()

    setInput('input[autocomplete="username"]', 'alice')
    passwordInputs().forEach((input) => {
      input.value = 'password123'
      input.dispatchEvent(new Event('input'))
    })
    setInput(INVITE_INPUT, 'my-invite')

    submitDialogForm()
    await flushPromises()

    expect(authService.register).toHaveBeenCalledWith({
      username: 'alice',
      password: 'password123',
      inviteCode: 'my-invite',
    })
  })

  it('后端拒绝时展示后端文案，而不是「请求失败」', async () => {
    vi.mocked(authService.login).mockRejectedValue(new Error('用户名或密码错误'))
    mountDialog('login')
    await flushPromises()

    setInput('input[autocomplete="username"]', 'alice')
    setInput('input[type="password"]', 'wrong-password')

    submitDialogForm()
    await flushPromises()

    expect(document.body.textContent).toContain('用户名或密码错误')
  })

  it('被动弹出时展示原因，主动打开时不展示', async () => {
    const pinia = freshPinia()
    const auth = useAuthStore(pinia)
    auth.requireLogin('创建任务需要先登录')
    mount(AuthDialog, { global: { plugins: [pinia] } })
    await flushPromises()

    expect(document.body.textContent).toContain('创建任务需要先登录')

    auth.closeDialog()
    auth.openDialog('login')
    await flushPromises()

    expect(document.body.textContent).not.toContain('创建任务需要先登录')
  })
})

// ============================================================================
// 顶栏用户容器
// ============================================================================

describe('顶栏用户容器', () => {
  it('未登录显示「登录」，点击弹出登录窗', async () => {
    const pinia = freshPinia()
    const auth = useAuthStore(pinia)
    auth.ready = true
    const wrapper = mount(UserMenu, { global: { plugins: [pinia] } })

    expect(wrapper.text()).toContain('登录')

    await wrapper.find('button').trigger('click')
    expect(auth.dialogOpen).toBe(true)
  })

  it('已登录显示昵称，点击展开菜单并可退出登录', async () => {
    const pinia = loggedInPinia()
    const auth = useAuthStore(pinia)
    const wrapper = mount(UserMenu, { global: { plugins: [pinia] } })

    expect(wrapper.text()).toContain('alice')

    await wrapper.find('button').trigger('click')
    expect(wrapper.text()).toContain('退出登录')

    const logout = wrapper.findAll('button').find((item) => item.text().includes('退出登录'))
    if (!logout) throw new Error('未找到退出登录按钮')
    await logout.trigger('click')
    await flushPromises()

    expect(authService.logout).toHaveBeenCalled()
    expect(auth.isLoggedIn).toBe(false)
  })
})

// ============================================================================
// 应用入口的登录门禁
// ============================================================================

describe('AppV2 的登录门禁', () => {
  /** 挂载一个未登录的应用 */
  async function mountAnonymous(): Promise<VueWrapper> {
    vi.mocked(authService.fetchCurrentUser).mockResolvedValue(null)
    const router = makeRouter()
    await router.push('/v2')
    const wrapper = mount(AppV2, { global: { plugins: [freshPinia(), router] } })
    await flushPromises()
    return wrapper
  }

  /** 点入口页上的某条入口 */
  async function clickEntry(wrapper: VueWrapper, label: string): Promise<void> {
    const button = wrapper.findAll('button').find((item) => item.text().includes(label))
    if (!button) throw new Error(`未找到入口：${label}`)
    await button.trigger('click')
    await flushPromises()
  }

  /** 在弹窗里填完登录表单并提交 */
  async function loginThroughDialog(): Promise<void> {
    setInput('input[autocomplete="username"]', 'alice')
    setInput('input[type="password"]', 'password123')
    submitDialogForm()
    await flushPromises()
  }

  it('未登录时选择入口会被拦下：弹登录窗，且仍停在入口页', async () => {
    const wrapper = await mountAnonymous()

    await clickEntry(wrapper, '完整流程')

    expect(document.body.textContent).toContain('创建任务需要先登录')
    // 关键：没有被放行到表单页
    expect(wrapper.text()).toContain('选择一种开始方式')
    expect(wrapper.find('nav[aria-label="生成进度"]').exists()).toBe(false)
  })

  it('登录成功后只关弹窗并给提示，不自动进入刚才选的入口', async () => {
    vi.mocked(authService.login).mockResolvedValue({ id: 1, username: 'alice' })
    const wrapper = await mountAnonymous()

    await clickEntry(wrapper, '完整流程')
    await loginThroughDialog()

    // 弹窗关了
    expect(document.body.textContent).not.toContain('创建任务需要先登录')
    // 但仍停在入口页——把选择权交还给用户
    expect(wrapper.text()).toContain('选择一种开始方式')
    // 提示语替代了「自动进入」的引导作用
    expect(wrapper.text()).toContain('已登录，请选择入口开始')
  })

  it('登录之后再选入口就能正常进入', async () => {
    vi.mocked(authService.login).mockResolvedValue({ id: 1, username: 'alice' })
    const wrapper = await mountAnonymous()

    await clickEntry(wrapper, '完整流程')
    await loginThroughDialog()
    await clickEntry(wrapper, '完整流程')

    expect(wrapper.find('nav[aria-label="生成进度"]').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('选择一种开始方式')
  })
})
