<script setup lang="ts">
/**
 * 主题切换按钮：点一下按「自动 → 亮色 → 暗色 → 自动」循环。
 *
 * 按钮上显示的是**当前**档位，不是「点一下会变成什么」：
 * 按完立刻能看到页面变化，再显示一个「下一站」只会让人多一层推理。
 *
 * 档位只有一处定义（`THEME_MODES`），图标与文案按档位查表，
 * 顶栏与登录页各放一个实例，状态全部来自 `useTheme()`。
 */
import { computed } from 'vue'

import { useTheme } from '@/composables/useTheme'

const { mode, cycle } = useTheme()

const META = {
  system: { label: '跟随系统', short: '自动' },
  light: { label: '亮色', short: '亮色' },
  dark: { label: '暗色', short: '暗色' },
} as const

const current = computed(() => META[mode.value])
const hint = computed(() => `主题：${current.value.label}，点击切换`)
</script>

<template>
  <button
    type="button"
    class="brut-btn brut-btn-xs theme-switch"
    :title="hint"
    :aria-label="hint"
    @click="cycle()"
  >
    <!-- 三个图标都描边不填充，和粗野风的硬线一致；只有「自动」用半实心表达明暗两半 -->
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <template v-if="mode === 'system'">
        <circle cx="12" cy="12" r="8.2" />
        <!-- 内联 style 而非属性：CSS 里的 fill/stroke 会盖掉表现属性，盖不掉 inline style -->
        <path d="M12 3.8a8.2 8.2 0 0 1 0 16.4Z" style="fill: currentColor; stroke: none" />
      </template>
      <template v-else-if="mode === 'light'">
        <circle cx="12" cy="12" r="4.3" />
        <path
          d="M12 2.6v2.4M12 19v2.4M2.6 12H5M19 12h2.4M5.3 5.3l1.7 1.7M17 17l1.7 1.7M18.7 5.3 17 7M7 17l-1.7 1.7"
        />
      </template>
      <template v-else>
        <path d="M20.2 14.6A8.6 8.6 0 0 1 9.4 3.8a8.6 8.6 0 1 0 10.8 10.8Z" />
      </template>
    </svg>
    <span class="theme-switch-label">{{ current.short }}</span>
  </button>
</template>
