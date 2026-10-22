/**
 * 主题切换用例。
 *
 * 这个控件最容易出的两种错，都不是「点不动」，而是**状态与页面不一致**：
 * 1. 按钮高亮在「暗色」，页面却是亮的（状态没落到 `<html>` 上）；
 * 2. 选了「跟随系统」却被持久化成一个固定值，第二天系统切暗色时页面纹丝不动。
 *
 * 所以这里的断言一律落在两处「外部事实」上：`<html>` 上有没有 `dark`、
 * `localStorage.theme` 存的是什么——而不是只看组件内部变量。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'

import ThemeSwitch from '@/components/ThemeSwitch.vue'
import { useTheme } from '@/composables/useTheme'

const htmlHasDark = () => document.documentElement.classList.contains('dark')

beforeEach(() => {
  localStorage.clear()
  document.documentElement.classList.remove('dark')
  // useTheme 是模块级单例，用例之间必须回到「跟随系统」这个初始档
  const { setMode } = useTheme()
  setMode('system')
  document.documentElement.classList.remove('dark')
})

describe('useTheme', () => {
  it('默认档位是「跟随系统」，且不上固定的 dark 类', () => {
    const { mode } = useTheme()
    expect(mode.value).toBe('system')
    // jsdom 的 matchMedia 恒为 false（系统亮色），所以此时页面应为亮色
    expect(htmlHasDark()).toBe(false)
  })

  it('选暗色：立即落到 <html> 并持久化', () => {
    const { setMode, isDark } = useTheme()
    setMode('dark')
    expect(isDark.value).toBe(true)
    expect(htmlHasDark()).toBe(true)
    // 键名与 index.html 内联脚本共用，写错一个字符首帧就会闪白
    expect(localStorage.getItem('theme')).toBe('dark')
  })

  it('选亮色：移除 dark 类', () => {
    const { setMode } = useTheme()
    setMode('dark')
    setMode('light')
    expect(htmlHasDark()).toBe(false)
    expect(localStorage.getItem('theme')).toBe('light')
  })

  it('回到「跟随系统」：不残留上一档的结果', () => {
    const { setMode } = useTheme()
    setMode('dark')
    setMode('system')
    expect(htmlHasDark()).toBe(false)
    expect(localStorage.getItem('theme')).toBe('system')
  })
})

describe('ThemeSwitch', () => {
  /**
   * 单按钮循环。这里钉住三件事：
   * 1. 只有一个按钮（曾经是三档并排，顶栏放不下）；
   * 2. 循环顺序是 自动 → 亮色 → 暗色 → 自动，且**回到自动**而不是停在暗色；
   * 3. 按钮上写的是当前档位，它必须和页面真实明暗一致。
   */
  it('只有一个按钮，初始显示「自动」', () => {
    const wrapper = mount(ThemeSwitch)
    expect(wrapper.findAll('button')).toHaveLength(1)
    expect(wrapper.text()).toContain('自动')
    expect(wrapper.attributes('aria-label')).toContain('跟随系统')
  })

  it('点一下换成亮色，再点换成暗色，第三下回到自动', async () => {
    const wrapper = mount(ThemeSwitch)

    await wrapper.trigger('click')
    expect(wrapper.text()).toContain('亮色')
    expect(htmlHasDark()).toBe(false)

    await wrapper.trigger('click')
    expect(wrapper.text()).toContain('暗色')
    expect(htmlHasDark()).toBe(true)

    // 第三下必须回到「自动」（jsdom 的系统偏好是亮色 → 页面回到亮色），
    // 停在暗色就说明循环少了一档，用户再也点不回跟随系统
    await wrapper.trigger('click')
    expect(wrapper.text()).toContain('自动')
    expect(htmlHasDark()).toBe(false)
    expect(localStorage.getItem('theme')).toBe('system')
  })

  it('显示的文字与页面明暗始终一致', async () => {
    const wrapper = mount(ThemeSwitch)
    await wrapper.trigger('click') // 亮色
    await wrapper.trigger('click') // 暗色
    expect(wrapper.text()).toContain('暗色')
    expect(htmlHasDark()).toBe(true)
  })

  it('两个实例共享同一份状态（顶栏与登录页各一个）', async () => {
    const a = mount(ThemeSwitch)
    const b = mount(ThemeSwitch)

    await a.trigger('click')
    expect(b.text()).toContain('亮色')

    // 换个实例继续点，仍接着同一个循环往下走
    await b.trigger('click')
    expect(a.text()).toContain('暗色')
    expect(htmlHasDark()).toBe(true)
  })
})

describe('跟随系统', () => {
  /**
   * 系统偏好变化只能靠替身触发。
   * `useTheme` 在**模块加载时**就订阅了 matchMedia，所以必须先装替身、
   * 再重新加载模块，才能确保它订阅的是这个替身。
   */
  function installMatchMedia(initialDark: boolean) {
    const listeners: Array<(event: MediaQueryListEvent) => void> = []
    const query = {
      matches: initialDark,
      media: '(prefers-color-scheme: dark)',
      onchange: null,
      addEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) =>
        listeners.push(listener),
      removeEventListener: () => {},
      addListener: (listener: (event: MediaQueryListEvent) => void) => listeners.push(listener),
      removeListener: () => {},
      dispatchEvent: () => false,
    }
    vi.stubGlobal('matchMedia', () => query)
    return (dark: boolean) => {
      query.matches = dark
      for (const listener of listeners) listener({ matches: dark } as MediaQueryListEvent)
    }
  }

  it('「跟随系统」时系统切暗就跟着暗', async () => {
    const emitChange = installMatchMedia(false)
    vi.resetModules()
    const fresh = await import('@/composables/useTheme')
    document.documentElement.classList.remove('dark')

    expect(fresh.useTheme().mode.value).toBe('system')
    emitChange(true)
    expect(htmlHasDark()).toBe(true)
    emitChange(false)
    expect(htmlHasDark()).toBe(false)

    vi.unstubAllGlobals()
  })

  it('显式选了亮色后，系统切暗不跟', async () => {
    const emitChange = installMatchMedia(false)
    vi.resetModules()
    const fresh = await import('@/composables/useTheme')
    document.documentElement.classList.remove('dark')

    fresh.useTheme().setMode('light')
    emitChange(true)
    // 用户明确选过亮色，系统再怎么变都不该改他的页面
    expect(htmlHasDark()).toBe(false)

    vi.unstubAllGlobals()
  })
})
