<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'

import { useTheme, type ThemeMode } from '@/composables/useTheme'
import AppButton from '@/components/ui/AppButton.vue'
import AppIcon from '@/components/ui/AppIcon.vue'

/**
 * 主题切换（侧栏顶端的图标按钮 + 下拉）。
 *
 * 做成「亮色 / 暗色 / 跟随系统」三选一而不是单个「亮⇄暗」开关：只留开关的话，
 * 用户一旦点过一次就再也回不到「跟随系统」（localStorage 里已经被写死了），
 * 而跟随系统恰恰是最该作为默认值的选项。三个选项写清楚当前生效的是哪个。
 *
 * 状态全在 `composables/useTheme`，本组件只负责交互与展示。
 */

const { mode, setMode } = useTheme()

/** 下拉是否展开 */
const menuOpen = ref(false)

/** 容器根元素，用于判断「点击是否发生在容器外」 */
const root = ref<HTMLElement | null>(null)

const OPTIONS: { value: ThemeMode; label: string; path: string }[] = [
  {
    value: 'light',
    label: '亮色',
    // 太阳：圆圈 + 光芒
    path: 'M10 1.5a8.5 8.5 0 1 0 0 17 8.5 8.5 0 0 0 0-17Zm0 2.5a6 6 0 1 0 0 12 6 6 0 0 0 0-12Zm0-2a.75.75 0 0 1 .75.75v1.5a.75.75 0 0 1-1.5 0v-1.5A.75.75 0 0 1 10 2Zm0 13a.75.75 0 0 1 .75.75v1.5a.75.75 0 0 1-1.5 0v-1.5A.75.75 0 0 1 10 15Zm5.657-8.157a.75.75 0 0 1 0 1.061l-1.06 1.06a.75.75 0 1 1-1.06-1.06l1.06-1.06a.75.75 0 0 1 1.06 0Zm-11.314 0a.75.75 0 0 1 1.06 0l1.06 1.06a.75.75 0 0 1-1.06 1.06l-1.06-1.06a.75.75 0 0 1 0-1.06Zm9.9 4.243a.75.75 0 0 1 1.06 0 .75.75 0 0 1 0 1.061l-1.061 1.06a.75.75 0 1 1-1.06-1.06l1.06-1.06Zm-9.9-4.243a.75.75 0 0 1 0 1.06L5.66 9.22a.75.75 0 1 1-1.06-1.06l1.06-1.06Zm-1.06 7.07a.75.75 0 0 1 1.06 0 .75.75 0 0 1 0 1.06l-1.06 1.06a.75.75 0 1 1-1.06-1.06l1.06-1.06Zm1.06-7.07a.75.75 0 0 1-1.06 0 .75.75 0 0 1 0-1.06l1.06-1.06a.75.75 0 1 1 1.06 1.06l-1.06 1.06Z',
  },
  {
    value: 'dark',
    label: '暗色',
    // 月亮
    path: 'M7.455 2.004a.75.75 0 0 1 .26.77 7 7 0 0 0 9.958 7.967.75.75 0 0 1 1.067.853A8.5 8.5 0 1 1 6.647 1.921a.75.75 0 0 1 .808 1.083Z',
  },
  {
    value: 'system',
    label: '跟随系统',
    // 显示器
    path: 'M3.5 3A1.5 1.5 0 0 0 2 4.5v7A1.5 1.5 0 0 0 3.5 13h4v1.5H6a.75.75 0 0 0 0 1.5h8a.75.75 0 0 0 0-1.5h-1.5V13h4A1.5 1.5 0 0 0 18 11.5v-7A1.5 1.5 0 0 0 16.5 3h-13Zm.5 1.5h12v6H4v-6ZM8.75 13h2.5v1.5h-2.5V13Z',
  },
]

/** 当前模式对应的图标 / 无障碍文案 */
const current = (): { label: string; path: string } => {
  const found = OPTIONS.find((o) => o.value === mode.value) ?? OPTIONS[2]!
  return { label: found.label, path: found.path }
}

function pick(next: ThemeMode): void {
  setMode(next)
  menuOpen.value = false
}

/** 点击容器外时收起下拉 */
function handleOutsideClick(event: MouseEvent): void {
  if (!menuOpen.value) return
  if (root.value && event.target instanceof Node && !root.value.contains(event.target)) {
    menuOpen.value = false
  }
}

onMounted(() => document.addEventListener('click', handleOutsideClick))
onBeforeUnmount(() => document.removeEventListener('click', handleOutsideClick))
</script>

<template>
  <div ref="root" class="relative">
    <AppButton
      variant="ghost"
      size="sm"
      class="!px-2"
      :aria-label="`主题：${current().label}，点击切换`"
      :title="`主题：${current().label}`"
      :aria-expanded="menuOpen"
      aria-haspopup="menu"
      @click="menuOpen = !menuOpen"
    >
      <AppIcon :path="current().path" size="sm" />
    </AppButton>

    <Transition
      enter-active-class="transition duration-100"
      enter-from-class="opacity-0 -translate-y-1"
      leave-active-class="transition duration-100"
      leave-to-class="opacity-0"
    >
      <div
        v-if="menuOpen"
        role="menu"
        class="absolute right-0 top-full z-50 mt-2 w-36 overflow-hidden rounded-xl border border-line bg-surface py-1 shadow-pop"
      >
        <button
          v-for="option in OPTIONS"
          :key="option.value"
          type="button"
          role="menuitemradio"
          :aria-checked="mode === option.value"
          class="flex w-full items-center gap-2 px-3 py-2 text-left text-xs transition hover:bg-surface-2"
          :class="mode === option.value ? 'text-accent' : 'text-body'"
          @click="pick(option.value)"
        >
          <AppIcon :path="option.path" size="sm" />
          <span class="flex-1">{{ option.label }}</span>
          <AppIcon
            v-if="mode === option.value"
            path="M16.704 5.29a1 1 0 0 1 .006 1.414l-7.5 7.5a1 1 0 0 1-1.414 0l-4-4a1 1 0 1 1 1.414-1.414l3.293 3.293 6.793-6.793a1 1 0 0 1 1.408 0Z"
            size="sm"
          />
        </button>
      </div>
    </Transition>
  </div>
</template>
