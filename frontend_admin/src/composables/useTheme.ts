import { computed, ref } from 'vue'

/**
 * 主题（亮 / 暗 / 跟随系统）的唯一权威来源。
 *
 * 三档而不是开关
 * --------------
 * 只有一个开关时，「关掉」到底是「跟随系统」还是「永远亮色」说不清，
 * 于是系统在日落时切到暗色、界面却毫无反应，用户只会觉得这个按钮坏了。
 * 把「跟随系统」显式做成第三档，配合『记住你选的那一档』，行为才可预期。
 *
 * 两份状态，一个真源
 * ------------------
 * `mode` 是用户选的档位，`isDark` 才是最终生效的明暗。CSS 只看
 * `<html class="dark">`，所以「按钮上的高亮」与「页面实际的明暗」不会各说各话。
 *
 * ⚠️ 与 `index.html` 里的内联脚本共用 `theme` 这个 localStorage 键：
 * 内联脚本负责首帧不闪（等 Vue 起来再切会晚一帧），本模块负责后续切换与持久化。
 * **两边的解析规则必须一起改**——内联脚本把「非 light 的值」一律按跟随系统处理，
 * 所以这里的 'system' 恰好能被它正确理解。
 */

export type ThemeMode = 'light' | 'dark' | 'system'

const STORAGE_KEY = 'theme'

/** 档位顺序即按钮顺序：先「跟随系统」，再两个显式选择 */
export const THEME_MODES: readonly ThemeMode[] = ['system', 'light', 'dark']

/** 用户选择的档位（不是最终生效的明暗）。默认跟随系统 */
const mode = ref<ThemeMode>(readStoredMode())

/** 系统当前是否偏好暗色 */
const systemDark = ref(readSystemDark())

/** 最终生效的明暗：显式选择优先，'system' 时才看系统偏好 */
const isDark = computed(() => (mode.value === 'system' ? systemDark.value : mode.value === 'dark'))

/** 读取持久化档位；非法值、读不到、隐私模式一律回落到「跟随系统」 */
function readStoredMode(): ThemeMode {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved === 'light' || saved === 'dark' || saved === 'system') return saved
  } catch {
    // localStorage 不可用时按「跟随系统」处理，不影响本次会话内的切换
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

/** 切换档位并持久化 */
function setMode(next: ThemeMode): void {
  mode.value = next
  try {
    localStorage.setItem(STORAGE_KEY, next)
  } catch {
    // 存不下也不影响当前会话
  }
  syncDocument()
}

/**
 * 按「跟随系统 → 亮色 → 暗色 → 跟随系统」循环一档。
 *
 * 循环而不是三档并排：顶栏还要放日期、在线状态和退出，三档并排会吃掉一整段宽度。
 * 切主题是低频操作，多按一下比常驻占位划算。
 */
function cycle(): void {
  const next = THEME_MODES[(THEME_MODES.indexOf(mode.value) + 1) % THEME_MODES.length]
  setMode(next ?? 'system')
}

// 跟随系统时实时响应系统切换（macOS / Windows 的定时日落切换）
if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
  const query = window.matchMedia('(prefers-color-scheme: dark)')
  const onChange = (event: MediaQueryListEvent) => {
    systemDark.value = event.matches
    // 注：isDark 已按档位计算，这里重复同步也是幂等的（结果不会错），
    // 加判断只是省掉一次无意义的 DOM 写入。真正保证「显式选过亮/暗就不跟系统变」
    // 的是 isDark 里那条 mode 分支，改它之前先看 theme.spec 的「不跟」用例。
    if (mode.value === 'system') syncDocument()
  }
  // 老 Safari 只有 addListener；两种都试一下，失败不致命
  if (typeof query.addEventListener === 'function') {
    query.addEventListener('change', onChange)
  } else if (typeof query.addListener === 'function') {
    query.addListener(onChange)
  }
}

// 模块加载即对齐一次：内联脚本已经设过 <html>，这里保证「状态与 DOM 一致」
syncDocument()

export function useTheme() {
  return { mode, isDark, setMode, cycle }
}
