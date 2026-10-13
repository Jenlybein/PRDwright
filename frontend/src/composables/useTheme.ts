import { computed, ref } from 'vue'

/**
 * 主题（亮 / 暗 / 跟随系统）的唯一权威来源。
 *
 * 为什么要抽出来：此前这段逻辑写在 `SolutionSidebar` 里，登录前后、抽屉/桌面两种布局
 * 各自持有 `isDark` 与 `applyTheme`，容易出现「按钮显示的状态」与「<html> 上的类」不一致。
 * 现在状态收敛到模块级单例，UI 只读不算，CSS 只看 `<html class="dark">`。
 *
 * 与 `index.html` 里的内联脚本共用 `theme` 这个 localStorage 键：内联脚本负责首帧不闪，
 * 本模块负责后续切换；两边的「解析规则」必须一起改（见 readStoredMode）。
 */

export type ThemeMode = 'light' | 'dark' | 'system'

const STORAGE_KEY = 'theme'

/** 用户选择的模式（不是最终生效的明暗）。默认跟随系统 */
const mode = ref<ThemeMode>(readStoredMode())

/** 系统当前是否偏好暗色 */
const systemDark = ref(readSystemDark())

/** 最终生效的明暗：显式选择优先，'system' 时看系统偏好 */
const isDark = computed(() => (mode.value === 'system' ? systemDark.value : mode.value === 'dark'))

/** 读取持久化选择；非法值或读不到时回落到「跟随系统」 */
function readStoredMode(): ThemeMode {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved === 'light' || saved === 'dark' || saved === 'system') return saved
  } catch {
    // localStorage 不可用（隐私模式）时按「跟随系统」处理，不影响本次会话切换
  }
  return 'system'
}

/** 系统偏好；jsdom / 老浏览器没有 matchMedia，按亮色处理 */
function readSystemDark(): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    ? window.matchMedia('(prefers-color-scheme: dark)').matches
    : false
}

/** 把解析结果写到 <html> 上——这是 CSS 端唯一的开关 */
function syncDocument(): void {
  document.documentElement.classList.toggle('dark', isDark.value)
}

/** 切换模式并持久化 */
function setMode(next: ThemeMode): void {
  mode.value = next
  try {
    localStorage.setItem(STORAGE_KEY, next)
  } catch {
    // 存不下也不影响当前会话
  }
  syncDocument()
}

/** 在亮 / 暗之间来回切（菜单里「跟随系统」之外的快捷路径） */
function toggle(): void {
  setMode(isDark.value ? 'light' : 'dark')
}

// 跟随系统时实时响应系统切换（macOS/Windows 的定时日落切换）
if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
  const query = window.matchMedia('(prefers-color-scheme: dark)')
  // 老 Safari 只有 addListener；两种都试一下，失败不致命
  if (typeof query.addEventListener === 'function') {
    query.addEventListener('change', (event) => {
      systemDark.value = event.matches
      if (mode.value === 'system') syncDocument()
    })
  } else if (typeof query.addListener === 'function') {
    query.addListener((event) => {
      systemDark.value = event.matches
      if (mode.value === 'system') syncDocument()
    })
  }
}

// 模块加载即对齐一次：内联脚本已经设过 <html>，这里保证「状态与 DOM 一致」
syncDocument()

export function useTheme() {
  return { mode, isDark, setMode, toggle }
}
