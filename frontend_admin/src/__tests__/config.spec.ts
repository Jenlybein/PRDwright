/**
 * 配置页的用例。
 *
 * 这个页面最怕的不是报错，而是**「显示已保存、实际没生效」**。所以这里的重心
 * 全在「保存之后怎么确认」上：
 *
 * 1. 回读值不一致 → 必须说「未生效」，而不是给一句让人安心的「已保存」；
 * 2. `revision` 未变 → 同样视为未生效（说明库里其实没变）；
 * 3. 危险项（关审核、改改写门槛）必须先确认，不能点一下就直接提交。
 *
 * 另一条是「密钥只以变量名出现」：界面上若把密钥值渲染出来，任何截图都会泄露它。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'

vi.mock('@/services/adminService', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/services/adminService')>()
  return {
    ...actual,
    fetchConfig: vi.fn(),
    updateConfig: vi.fn(),
    resetConfig: vi.fn(),
    fetchProviders: vi.fn(),
    createProvider: vi.fn(),
    updateProvider: vi.fn(),
    deleteProvider: vi.fn(),
    activateProvider: vi.fn(),
    testProvider: vi.fn(),
  }
})

import {
  activateProvider,
  createProvider,
  deleteProvider,
  fetchConfig,
  fetchProviders,
  testProvider,
  updateConfig,
  updateProvider,
  type ConfigGroup,
  type Provider,
  type SystemConfig,
} from '@/services/adminService'
import { ApiError } from '@/services/errors'
import ConfigView from '@/views/ConfigView.vue'

const configMock = vi.mocked(fetchConfig)
const updateMock = vi.mocked(updateConfig)
const providersMock = vi.mocked(fetchProviders)
const activateMock = vi.mocked(activateProvider)
const deleteMock = vi.mocked(deleteProvider)
const testMock = vi.mocked(testProvider)
const createMock = vi.mocked(createProvider)
const updateProvMock = vi.mocked(updateProvider)

function item(overrides: Record<string, unknown> = {}) {
  return {
    key: 'prd_pipeline_concurrency',
    value: 3,
    default: 3,
    source: 'env',
    type: 'number',
    label: '并发章节数',
    help: '1 表示退回串行',
    danger: false,
    options: null,
    min: 1,
    max: 8,
    min_exclusive: null,
    ...overrides,
  }
}

function groups(): ConfigGroup[] {
  return [
    {
      key: 'prd',
      label: '分章节生成',
      help: '控制逐章撰写的节奏',
      items: [
        item(),
        item({
          key: 'prd_section_review_enabled',
          value: true,
          default: true,
          type: 'bool',
          label: '逐章审核',
          danger: true,
          min: null,
          max: null,
        }),
      ],
    },
  ]
}

function config(overrides: Partial<SystemConfig> = {}): SystemConfig {
  return {
    groups: groups(),
    active_provider_id: 'ark',
    active_provider: {
      id: 'ark',
      name: '火山方舟',
      protocol: 'ark',
      protocol_label: '火山方舟',
      base_url: '',
      default_model: 'doubao-seed',
      max_tokens: 8192,
      context_window: 0,
      max_output: 0,
      thinking_enabled: false,
    },
    config_source: 'env',
    revision: 'rev-1',
    missing_key: '',
    ...overrides,
  }
}

function provider(overrides: Partial<Provider> = {}): Provider {
  return {
    id: 'cfg_ark0001',
    name: '火山方舟',
    protocol: 'ark',
    protocol_label: '火山方舟',
    base_url: '',
    key_present: true,
    default_model: 'doubao-seed',
    max_tokens: 8192,
    context_window: 0,
    max_output: 0,
    thinking_enabled: false,
    enabled: true,
    sort_order: 0,
    active: true,
    extra: {},
    ...overrides,
  }
}

function providerList(list: Provider[] = [provider()]) {
  return {
    providers: list,
    active_provider_id: list.find((p) => p.active)?.id ?? '',
    presets: [
      {
        key: 'deepseek',
        name: 'DeepSeek',
        protocol: 'openai_compatible',
        base_url: 'https://api.deepseek.com/v1',
        default_model: 'deepseek-chat',
      },
      {
        key: 'ark',
        name: '火山方舟',
        protocol: 'ark',
        base_url: 'https://ark.cn-beijing.volces.com/api/v3',
        default_model: '',
      },
      {
        // 本地部署：**变量名故意为空**，代表这条不需要密钥
        key: 'vllm',
        name: '本地 vLLM / Ollama',
        protocol: 'openai_compatible',
        base_url: 'http://127.0.0.1:8000/v1',
        default_model: '',
      },
    ],
    protocols: [
      { value: 'openai_compatible', label: 'OpenAI 兼容' },
      { value: 'ark', label: '火山方舟' },
    ],
    warning: '',
  }
}

/**
 * 挂载配置页。
 *
 * ⚠️ 必须带**真路由**：分区写在 `?sec=` 上，页面是靠 `useRoute()` 读它的。
 * 用假路由（或不带路由）mount，读到的是「没有分区」的默认态——
 * 那些分区用例会一路绿着，却什么都没验证。
 */
async function mountAt(query = '', cfg = config(), list = providerList()) {
  configMock.mockResolvedValue(cfg)
  providersMock.mockResolvedValue(list)
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/', name: 'config', component: ConfigView }],
  })
  if (query) await router.push(`/${query}`)
  await router.isReady()
  const wrapper = mount(ConfigView, { global: { plugins: [createPinia(), router] } })
  await vi.waitFor(() => {
    expect(configMock).toHaveBeenCalled()
    expect(providersMock).toHaveBeenCalled()
  })
  await wrapper.vm.$nextTick()
  return { wrapper, router }
}

/**
 * 默认落在「模型生成配置」这一分区：
 * 下面多数用例动的是参数项（并发、审核、保存校验），它们都在这个分区里。
 */
async function render(cfg = config(), list = providerList()) {
  return (await mountAt('?sec=generation', cfg, list)).wrapper
}

/** 模型配置分区：供应商列表那一段（新增/编辑/测试连接）的用例用它 */
async function renderProviders(cfg = config(), list = providerList()) {
  return (await mountAt('?sec=model', cfg, list)).wrapper
}

/** 五个后端分组各放一项，用来验「这个分区里该有谁、不该有谁」 */
function allGroups(): ConfigGroup[] {
  return [
    {
      key: 'prd',
      label: '分章节生成',
      help: '',
      items: [item({ key: 'prd_pipeline_concurrency', label: '并发章节数' })],
    },
    {
      key: 'review',
      label: '审核与改写',
      help: '',
      items: [item({ key: 'review_enabled', label: '审核开关', type: 'bool', value: true, min: null, max: null })],
    },
    {
      key: 'llm',
      label: '调用与预算',
      help: '',
      items: [item({ key: 'llm_timeout', label: '调用超时秒数', min: null, max: null })],
    },
    {
      key: 'quota',
      label: '每日额度',
      help: '',
      items: [item({ key: 'daily_quota', label: '每日生成上限', min: null, max: null })],
    },
    {
      key: 'auth',
      label: '安全与登录',
      help: '',
      items: [item({ key: 'login_limit', label: '登录失败限流', min: null, max: null })],
    },
  ]
}

beforeEach(() => {
  setActivePinia(createPinia())
  configMock.mockReset()
  updateMock.mockReset()
  providersMock.mockReset()
  activateMock.mockReset()
  deleteMock.mockReset()
  testMock.mockReset()
  createMock.mockReset()
  updateProvMock.mockReset()
})

describe('配置页', () => {
  it('渲染分区标题、配置指纹与配置项', async () => {
    const wrapper = await render()

    const text = wrapper.text()
    expect(wrapper.find('h1').text()).toBe('模型生成配置')
    // 配置指纹：排查「改了没生效」时，第一眼要看的就是它有没有变
    expect(text).toContain('REV rev-1')
    expect(text).toContain('并发章节数')
    // 变量名不上页面：管理员只关心「并发章节数」，不关心它在代码里叫什么
    expect(text).not.toContain('prd_pipeline_concurrency')
  })

  it('不再跨分区挂一块「当前供应商」卡（那条信息只属于模型配置那一屏）', async () => {
    const wrapper = await render()
    const text = wrapper.text()
    expect(text).not.toContain('当前供应商')
    expect(text).not.toContain('Active Provider')
    expect(text).not.toContain('火山方舟')
  })

  it('启动类配置没有可编辑入口', async () => {
    // 说明文案里**会**提到「日志级别」——那是在告诉用户「它不在此页」，
    // 所以断言不能查文案，只能查「有没有对应的配置项 / 输入控件」。
    // 真正的拦截在后端白名单，前端只是不提供入口。
    const wrapper = await render()
    expect(wrapper.text()).not.toContain('db_path')
    expect(wrapper.text()).not.toContain('log_db_enabled')
  })

  it('页面上不出现任何配置项的原始变量名', async () => {
    // 管理员看到的是「并发章节数」，不是 `prd_pipeline_concurrency`。
    // 这条把整页的 key 都扫一遍：以后新增配置项时，模板里一旦又把 key 印出来就会红。
    const wrapper = await render()
    const text = wrapper.text()
    for (const key of ['prd_pipeline_concurrency', 'prd_section_review_enabled']) {
      expect(text).not.toContain(key)
    }
  })

  it('密钥只显示「已配置/未配置」，绝不渲染密钥值', async () => {
    const wrapper = await renderProviders()
    const text = wrapper.text()
    expect(text).toContain('已配置')
    // 页面里不该出现任何像密钥的东西
    expect(text).not.toMatch(/sk-[A-Za-z0-9]/)
  })

  it('保存后回读不一致时必须说「未生效」，而不是「已保存」', async () => {
    const wrapper = await render()
    const before = wrapper.text()

    // 让 PATCH 返回一个「值没变」的响应（模拟后端没真写进去）
    updateMock.mockResolvedValue(config({ revision: 'rev-2' }))

    const numberInput = wrapper.find('input[type="number"][max="8"]')
    await numberInput.setValue('5')
    await numberInput.trigger('change')
    const save = wrapper.findAll('button').find((b) => b.text() === '保存')
    expect(save).toBeTruthy()
    await save!.trigger('click')

    // 保存失败后页面会**先刷新数据、再把错误设回去**（见 saveGroup 里的顺序说明），
    // 所以这里要等错误文本真的出现，而不是只等 `updateMock` 被调用。
    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('回读不一致')
    })
    expect(wrapper.text()).not.toContain('已生效')
    expect(before).not.toContain('回读不一致')
  })

  it('revision 没变时也判为未生效', async () => {
    const wrapper = await render()
    // revision 与初始相同 → 库里其实没变
    updateMock.mockResolvedValue(
      config({
        revision: 'rev-1',
        groups: [
          {
            key: 'prd',
            label: '分章节生成',
            help: '',
            items: [item({ value: 5, source: 'db' })],
          },
        ],
      }),
    )

    const numberInput = wrapper.find('input[type="number"][max="8"]')
    await numberInput.setValue('5')
    await numberInput.trigger('change')
    await wrapper.findAll('button').find((b) => b.text() === '保存')!.trigger('click')

    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('指纹没有变化')
    })
  })

  it('危险项点击后先弹确认，未确认不发请求', async () => {
    const wrapper = await render()

    const checkbox = wrapper.find('input[type="checkbox"]')
    await checkbox.setValue(false)
    await wrapper.vm.$nextTick()

    // 弹了确认面板，且没有提交
    expect(wrapper.text()).toContain('确定把')
    expect(wrapper.text()).toContain('确认修改')
    expect(updateMock).not.toHaveBeenCalled()

    // 确认之后才形成草稿（仍未提交，要点保存）
    const confirm = wrapper.findAll('button').find((b) => b.text() === '确认修改')
    await confirm!.trigger('click')
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).not.toContain('确定把')
  })

  it('后端拒绝时原样显示原因（400 不是泛泛的「操作失败」）', async () => {
    const wrapper = await render()
    updateMock.mockRejectedValue(
      new ApiError('配置项 prd_pipeline_concurrency 的取值不合法：Input should be less than or equal to 8', 400),
    )

    const numberInput = wrapper.find('input[type="number"][max="8"]')
    await numberInput.setValue('5')
    await numberInput.trigger('change')
    await wrapper.findAll('button').find((b) => b.text() === '保存')!.trigger('click')

    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('取值不合法')
    })
  })

  it('激活供应商要二次确认，确认后才发请求', async () => {
    const wrapper = await renderProviders(
      config(),
      providerList([
        provider(),
        provider({ id: 'cfg_nvidia1', name: 'NVIDIA NIM', active: false }),
      ]),
    )
    activateMock.mockResolvedValue(
      providerList([
        provider({ active: false }),
        provider({ id: 'cfg_nvidia1', name: 'NVIDIA NIM', active: true }),
      ]),
    )

    // 文案是「设为使用中」而不是「激活」：列表里看出的是「换哪一条在生效」，
    // 而不是一个需要先理解「激活」是什么意思的动作。
    const activateButton = wrapper.findAll('button').find((b) => b.text() === '设为使用中')
    expect(activateButton).toBeTruthy()
    await activateButton!.trigger('click')
    await wrapper.vm.$nextTick()

    expect(wrapper.text()).toContain('切换')
    expect(activateMock).not.toHaveBeenCalled()

    await wrapper.findAll('button').find((b) => b.text() === '确认切换')!.trigger('click')
    await vi.waitFor(() => {
      expect(activateMock).toHaveBeenCalledWith('cfg_nvidia1')
    })
  })

  it('列表里只有使用中的那一条带「使用中」，其余是「待命」', async () => {
    const wrapper = await renderProviders(
      config(),
      providerList([
        provider(),
        provider({ id: 'cfg_ds0001', name: 'DeepSeek', default_model: 'deepseek-chat', active: false }),
      ]),
    )

    const tags = wrapper.findAll('.brut-tag').map((t) => t.text())
    expect(tags.filter((t) => t === '使用中')).toHaveLength(1)
    expect(tags).toContain('待命')
    // 每行都要能一眼看到「协议 / 密钥状态 / 输出上限」，否则排查缺密钥只能去翻库。
    // 写的是中文「输出上限」而不是 `max_tokens`：界面上不该出现字段名。
    const text = wrapper.text()
    expect(text).toContain('密钥')
    expect(text).toContain('已配置')
    expect(text).toContain('输出上限 8192')
    expect(text).not.toContain('max_tokens')
  })

  it('使用中的配置也给出删除按钮，被拒绝时原样显示后端原因', async () => {
    // ⚠️ 前端**不**自己判断「使用中不能删」：藏起按钮会让人以为这行坏了，
    // 而且规则一改两边就不一致。按钮照给，让后端用 403 + 原话回答。
    const wrapper = await renderProviders()
    deleteMock.mockRejectedValue(
      new ApiError('这是使用中的配置，请先切换到另一条再删除', 403),
    )

    await wrapper.findAll('button').find((b) => b.text() === '删除')!.trigger('click')
    await wrapper.vm.$nextTick()
    await wrapper.findAll('button').find((b) => b.text() === '确认删除')!.trigger('click')

    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('请先切换到另一条')
    })
  })

  it('测试连接成功走悬浮提示，不再在列表行里占一行字', async () => {
    const wrapper = await renderProviders()
    testMock.mockResolvedValue({ ok: true, detail: '连通正常（120 ms）', latency_ms: 120 })

    await wrapper.findAll('button').find((b) => b.text() === '测试连接')!.trigger('click')
    await vi.waitFor(() => {
      expect(testMock).toHaveBeenCalledWith('cfg_ark0001')
    })
    await wrapper.vm.$nextTick()

    // 悬浮提示：role=status 的固定浮层，带耗时
    const toast = wrapper.find('[role="status"]')
    expect(toast.exists()).toBe(true)
    expect(toast.text()).toContain('连通正常')
    expect(toast.text()).toContain('120 ms')

    // 成功不再写进列表行（行内只保留失败原因）
    expect(wrapper.findAll('li')[0]!.text()).not.toContain('连通正常')
  })

  it('测试连接失败时把后端给的原因显示出来', async () => {
    const wrapper = await renderProviders()
    testMock.mockResolvedValue({
      ok: false,
      detail: '调用失败：ConnectError: 域名解析失败',
      latency_ms: 30,
    })

    await wrapper.findAll('button').find((b) => b.text() === '测试连接')!.trigger('click')
    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('连接失败')
    })
    expect(wrapper.text()).toContain('域名解析失败')
  })

  it('新增与编辑都以弹窗呈现，点「关闭」可收起', async () => {
    const wrapper = await renderProviders()
    // 初始不弹：列表才是主要内容
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)

    await wrapper.findAll('button').find((b) => b.text() === '+ 新增配置')!.trigger('click')
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[role="dialog"]').exists()).toBe(true)

    await wrapper.findAll('button').find((b) => b.text() === '关闭')!.trigger('click')
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)

    await wrapper.findAll('button').find((b) => b.text() === '编辑')!.trigger('click')
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[role="dialog"]').exists()).toBe(true)

    await wrapper.findAll('button').find((b) => b.text() === '关闭')!.trigger('click')
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
  })

  it('新增配置时从「供应商」填充地址与配置名，且没有 id 输入框', async () => {
    const wrapper = await renderProviders()

    // 表单默认收起：列表才是主要内容，供应商下拉只在展开后出现
    await wrapper.findAll('button').find((b) => b.text() === '+ 新增配置')!.trigger('click')
    await wrapper.vm.$nextTick()

    // 页面里不该再有「手填 id」的位置——id 由后端生成（cfg_xxx）
    expect(wrapper.text()).not.toContain('id（身份，创建后不可改）')

    const presetSelect = wrapper.findAll('select')[0]
    expect(presetSelect).toBeTruthy()
    await presetSelect!.setValue('deepseek')
    await wrapper.vm.$nextTick()

    const values = wrapper.findAll('input').map((i) => (i.element as HTMLInputElement).value)
    expect(values).toContain('https://api.deepseek.com/v1')
    // 配置名由模板带出来（可以改名，也可以重名）
    expect(values).toContain('DeepSeek')
    // 模板的下拉值是 key，不是配置 id：它一旦看起来像 id，就会被当成 id 发出去
    expect(values).not.toContain('cfg_deepseek')
  })

  it('表单里只有能改的东西：没有协议、也没有密钥变量名', async () => {
    const wrapper = await renderProviders()
    await wrapper.findAll('button').find((b) => b.text() === '+ 新增配置')!.trigger('click')
    await wrapper.vm.$nextTick()

    // 还没选供应商时就不该有这两项——它们都由供应商决定
    expect(wrapper.findAll('label').find((l) => l.text().startsWith('协议'))).toBeUndefined()
    expect(wrapper.findAll('label').find((l) => l.text().includes('密钥变量名'))).toBeUndefined()

    await wrapper.findAll('select')[0]!.setValue('ark')
    await wrapper.vm.$nextTick()

    // 选了供应商之后仍然没有：改不动的东西不摆出来，也不以「灰掉」的形式暗示这里能改
    expect(wrapper.findAll('label').find((l) => l.text().startsWith('协议'))).toBeUndefined()
    expect(wrapper.findAll('label').find((l) => l.text().includes('密钥变量名'))).toBeUndefined()

    // 地址与模型是可改的，照常出现并由供应商带出来
    const values = wrapper.findAll('input').map((i) => (i.element as HTMLInputElement).value)
    expect(values).toContain('https://ark.cn-beijing.volces.com/api/v3')
  })

  it('列表为空时只有一个新增入口（不再重复摆一个「新增第一条配置」）', async () => {
    const wrapper = await renderProviders(config(), providerList([]))

    expect(wrapper.text()).toContain('还没有任何模型配置')
    // 右上角那个就在同一屏，再摆一个大按钮做同一件事只会让人先愣一下
    // 「这两个是不是不一样」。所以新增入口有且只有一个。
    const adds = wrapper.findAll('button').filter((b) => b.text().includes('新增'))
    expect(adds).toHaveLength(1)
    expect(adds[0]!.text()).toContain('+ 新增配置')
  })

  it('新增成功后显示后端给的提示（首条会自动设为使用中）', async () => {
    const wrapper = await renderProviders(config(), providerList([]))
    await wrapper.findAll('button').find((b) => b.text() === '+ 新增配置')!.trigger('click')
    await wrapper.vm.$nextTick()

    await wrapper.findAll('select')[0]!.setValue('ark')
    await wrapper.vm.$nextTick()

    // 判断在服务端（第一条自动激活、缺密钥要提醒），前端只负责原样显示
    createMock.mockResolvedValue({
      ...providerList(),
      warning: '已新增并设为使用中：火山方舟。但还没有配置密钥：下一次生成会直接报错。',
    })
    await wrapper.findAll('button').find((b) => b.text() === '添加配置')!.trigger('click')

    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('已新增并设为使用中')
    })
    expect(wrapper.text()).toContain('还没有配置密钥')
  })

  it('提交时带上填写的密钥（表单里的明文，后端加密入库）', async () => {
    const wrapper = await renderProviders()
    await wrapper.findAll('button').find((b) => b.text() === '+ 新增配置')!.trigger('click')
    await wrapper.vm.$nextTick()

    await wrapper.findAll('select')[0]!.setValue('ark')
    await wrapper.vm.$nextTick()

    const keyInput = wrapper
      .findAll('input')
      .find((i) => (i.element as HTMLInputElement).placeholder.includes('留空=稍后再配'))!
    await keyInput.setValue('sk-test-123')
    await wrapper.vm.$nextTick()

    createMock.mockResolvedValue(providerList())
    await wrapper.findAll('button').find((b) => b.text() === '添加配置')!.trigger('click')

    await vi.waitFor(() => {
      expect(createMock).toHaveBeenCalled()
    })
    const [payload] = createMock.mock.calls[0]!
    expect(payload).toMatchObject({ name: '火山方舟', protocol: 'ark', api_key: 'sk-test-123' })
  })

  it('不填密钥时请求体不带 api_key（= 稍后再配）', async () => {
    const wrapper = await renderProviders()
    await wrapper.findAll('button').find((b) => b.text() === '+ 新增配置')!.trigger('click')
    await wrapper.vm.$nextTick()

    await wrapper.findAll('select')[0]!.setValue('vllm')
    await wrapper.vm.$nextTick()

    createMock.mockResolvedValue(providerList())
    await wrapper.findAll('button').find((b) => b.text() === '添加配置')!.trigger('click')

    await vi.waitFor(() => {
      expect(createMock).toHaveBeenCalled()
    })
    const [payload] = createMock.mock.calls[0]!
    expect(payload).not.toHaveProperty('api_key')
    expect(payload).toMatchObject({ base_url: 'http://127.0.0.1:8000/v1' })
  })

  it('没选供应商就提交：直接提示，不发请求', async () => {
    const wrapper = await renderProviders()
    await wrapper.findAll('button').find((b) => b.text() === '+ 新增配置')!.trigger('click')
    await wrapper.vm.$nextTick()

    await wrapper.findAll('button').find((b) => b.text() === '添加配置')!.trigger('click')

    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('请先选供应商')
    })
    expect(createMock).not.toHaveBeenCalled()
  })

  it('选了供应商但清空配置名：提示配置名必填，不发请求', async () => {
    const wrapper = await renderProviders()
    await wrapper.findAll('button').find((b) => b.text() === '+ 新增配置')!.trigger('click')
    await wrapper.vm.$nextTick()

    await wrapper.findAll('select')[0]!.setValue('ark')
    await wrapper.vm.$nextTick()

    // 配置名由模板带出来，这里清空它
    const nameInput = wrapper
      .findAll('input')
      .find((i) => (i.element as HTMLInputElement).placeholder.includes('方舟-生产'))!
    await nameInput.setValue('')
    await wrapper.vm.$nextTick()

    await wrapper.findAll('button').find((b) => b.text() === '添加配置')!.trigger('click')

    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('请先填配置名')
    })
    expect(createMock).not.toHaveBeenCalled()
  })

  it('编辑面板不冒充可改控件，密钥只通过 api_key 字段提交', async () => {
    const wrapper = await renderProviders()

    await wrapper.findAll('button').find((b) => b.text() === '编辑')!.trigger('click')
    await wrapper.vm.$nextTick()

    // 整块表单里不该有任何只读输入框——只读输入框会让人以为「差点就能改」
    const readonlyInputs = wrapper
      .findAll('input')
      .filter((i) => (i.element as HTMLInputElement).readOnly)
    expect(readonlyInputs).toHaveLength(0)

    // 旧的「密钥变量名」概念已不存在（密钥现在是录入的明文，不是环境变量名）
    expect(wrapper.findAll('label').find((l) => l.text().includes('密钥变量名'))).toBeUndefined()

    // 但「这条走的是哪套协议」仍答得出来：标题行有一个协议标签
    expect(wrapper.findAll('.brut-tag').some((t) => t.text() === '火山方舟')).toBe(true)

    updateProvMock.mockResolvedValue(providerList([provider({ name: '改过的名字' })]))
    await wrapper.findAll('button').find((b) => b.text() === '保存修改')!.trigger('click')

    await vi.waitFor(() => {
      expect(updateProvMock).toHaveBeenCalled()
    })
    const [, payload] = updateProvMock.mock.calls[0]!
    expect(payload).not.toHaveProperty('protocol')
    expect(payload).not.toHaveProperty('api_key_env_var')
    // 留空表示「保持不变」，所以也不该发 api_key
    expect(payload).not.toHaveProperty('api_key')
  })
})

describe('配置页 · 分区', () => {
  it('每个分区只显示自己那一摊，别的分区的配置不出现', async () => {
    const cases: Array<{ sec: string; wanted: string[]; unwanted: string[] }> = [
      {
        sec: 'generation',
        wanted: ['并发章节数', '审核开关'],
        unwanted: ['调用超时秒数', '每日生成上限', '登录失败限流', '测试连接'],
      },
      {
        sec: 'budget',
        wanted: ['调用超时秒数', '每日生成上限'],
        unwanted: ['并发章节数', '审核开关', '登录失败限流'],
      },
      {
        sec: 'security',
        wanted: ['登录失败限流'],
        unwanted: ['并发章节数', '调用超时秒数', '每日生成上限'],
      },
    ]

    for (const one of cases) {
      const wrapper = await mountAt(`?sec=${one.sec}`, config({ groups: allGroups() })).then(
        (r) => r.wrapper,
      )
      const text = wrapper.text()
      for (const label of one.wanted) expect(text).toContain(label)
      for (const label of one.unwanted) expect(text).not.toContain(label)
    }
  })

  it('「模型配置」这一屏只有供应商列表，没有参数组', async () => {
    const wrapper = await renderProviders(config({ groups: allGroups() }))
    const text = wrapper.text()
    // 供应商列表在
    expect(text).toContain('测试连接')
    expect(text).toContain('输出上限 8192')
    // 参数组不在：它们各自属于「生成 / 预算 / 安全」三屏
    expect(text).not.toContain('并发章节数')
    expect(text).not.toContain('调用超时秒数')
    expect(text).not.toContain('登录失败限流')
  })

  it('标题与内容都由 URL 上的 sec 决定（刷新/分享链接都回到同一屏）', async () => {
    const wrapper = await mountAt('?sec=budget', config({ groups: allGroups() })).then(
      (r) => r.wrapper,
    )
    // 页头标题就是这个分区的名字
    expect(wrapper.find('h1').text()).toBe('预算与额度')
    expect(wrapper.text()).toContain('调用超时秒数')
    expect(wrapper.text()).toContain('每日生成上限')
  })

  it('页面里不再摆一排分区切换按钮（入口只有侧栏子菜单）', async () => {
    const wrapper = await renderProviders(config({ groups: allGroups() }))
    expect(wrapper.find('.brut-seg').exists()).toBe(false)
  })

  it('不认识的 ?sec= 回落到第一个分区，不白屏', async () => {
    const wrapper = await mountAt('?sec=not-a-section', config({ groups: allGroups() })).then(
      (r) => r.wrapper,
    )
    // 兜底分区是「模型配置」：供应商列表在，参数组不在
    expect(wrapper.text()).toContain('测试连接')
    expect(wrapper.text()).not.toContain('登录失败限流')
  })

  it('缺密钥的提醒保留为一行（它会在生成时直接报错，不能跟着卡片一起删）', async () => {
    const wrapper = await mountAt(
      '?sec=security',
      config({ groups: allGroups(), missing_key: '当前供应商还没有配置密钥：生成会直接报错。' }),
    ).then((r) => r.wrapper)
    expect(wrapper.text()).toContain('还没有配置密钥')
    expect(wrapper.text()).toContain('生成会直接报错')
  })
})
