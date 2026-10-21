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
    id: 'ark',
    name: '火山方舟',
    protocol: 'ark',
    protocol_label: '火山方舟',
    base_url: '',
    api_key_env_var: 'ARK_API_KEY',
    key_present: true,
    default_model: 'doubao-seed',
    max_tokens: 8192,
    context_window: 0,
    max_output: 0,
    thinking_enabled: false,
    builtin: true,
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
        id: 'deepseek',
        name: 'DeepSeek',
        protocol: 'openai_compatible',
        base_url: 'https://api.deepseek.com/v1',
        api_key_env_var: 'DEEPSEEK_API_KEY',
        default_model: 'deepseek-chat',
      },
    ],
    protocols: [{ value: 'openai_compatible', label: 'OpenAI 兼容' }],
    warning: '',
  }
}

async function render(cfg = config(), list = providerList()) {
  configMock.mockResolvedValue(cfg)
  providersMock.mockResolvedValue(list)
  const wrapper = mount(ConfigView, { global: { plugins: [createPinia()] } })
  await vi.waitFor(() => {
    expect(configMock).toHaveBeenCalled()
    expect(providersMock).toHaveBeenCalled()
  })
  await wrapper.vm.$nextTick()
  return wrapper
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
})

describe('配置页', () => {
  it('渲染三要素：当前供应商、配置来源、配置项', async () => {
    const wrapper = await render()

    const text = wrapper.text()
    expect(text).toContain('火山方舟')
    expect(text).toContain('环境变量默认值')
    expect(text).toContain('并发章节数')
    expect(text).toContain('prd_pipeline_concurrency')
  })

  it('启动类配置没有可编辑入口', async () => {
    // 说明文案里**会**提到「日志级别」——那是在告诉用户「它不在此页」，
    // 所以断言不能查文案，只能查「有没有对应的配置项 / 输入控件」。
    // 真正的拦截在后端白名单，前端只是不提供入口。
    const wrapper = await render()
    expect(wrapper.text()).not.toContain('db_path')
    expect(wrapper.text()).not.toContain('log_db_enabled')
    // 页面上出现的 number 输入里，不该有给启动类配置留的位置
    const keys = wrapper.findAll('.font-mono').map((n) => n.text())
    expect(keys.some((t) => t.startsWith('log_'))).toBe(false)
  })

  it('密钥只以变量名出现，绝不渲染密钥值', async () => {
    const wrapper = await render()
    const text = wrapper.text()
    expect(text).toContain('ARK_API_KEY')
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
    const wrapper = await render(
      config(),
      providerList([provider(), provider({ id: 'nvidia', name: 'NVIDIA NIM', active: false, builtin: false })]),
    )
    activateMock.mockResolvedValue(providerList([provider({ active: false }), provider({ id: 'nvidia', name: 'NVIDIA NIM', active: true })]))

    const activateButton = wrapper.findAll('button').find((b) => b.text() === '激活')
    expect(activateButton).toBeTruthy()
    await activateButton!.trigger('click')
    await wrapper.vm.$nextTick()

    expect(wrapper.text()).toContain('切换')
    expect(activateMock).not.toHaveBeenCalled()

    await wrapper.findAll('button').find((b) => b.text() === '确认切换')!.trigger('click')
    await vi.waitFor(() => {
      expect(activateMock).toHaveBeenCalledWith('nvidia')
    })
  })

  it('内置供应商不显示删除按钮', async () => {
    const wrapper = await render()
    const labels = wrapper.findAll('button').map((b) => b.text())
    expect(labels).not.toContain('删除')
  })

  it('删除被后端拒绝时显示原因', async () => {
    const wrapper = await render(
      config(),
      providerList([provider(), provider({ id: 'mine', name: '我的', active: false, builtin: false })]),
    )
    deleteMock.mockRejectedValue(new ApiError('这是当前激活的供应商，请先激活另一家', 403))

    await wrapper.findAll('button').find((b) => b.text() === '删除')!.trigger('click')
    await wrapper.vm.$nextTick()
    await wrapper.findAll('button').find((b) => b.text() === '确认删除')!.trigger('click')

    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('请先激活另一家')
    })
  })

  it('测试连接会显示结果（成功与失败分开）', async () => {
    const wrapper = await render()
    testMock.mockResolvedValue({ ok: true, detail: '连通正常（120 ms）', latency_ms: 120 })

    await wrapper.findAll('button').find((b) => b.text() === '测试连接')!.trigger('click')
    await vi.waitFor(() => {
      expect(testMock).toHaveBeenCalledWith('ark')
    })
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toContain('连通正常')
  })

  it('测试连接失败时把后端给的原因显示出来', async () => {
    const wrapper = await render()
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

  it('新增供应商时可从预设填充地址与变量名', async () => {
    const wrapper = await render()
    const presetSelect = wrapper.findAll('select')[0]
    expect(presetSelect).toBeTruthy()
    await presetSelect!.setValue('deepseek')
    await wrapper.vm.$nextTick()

    const inputs = wrapper.findAll('input')
    const values = inputs.map((i) => (i.element as HTMLInputElement).value)
    expect(values).toContain('deepseek')
    expect(values).toContain('https://api.deepseek.com/v1')
    expect(values).toContain('DEEPSEEK_API_KEY')
  })
})
