<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'

import { useAuthStore } from '@/stores/auth'

/**
 * 顶栏右端的用户容器。
 *
 * 未登录显示「登录」，点击弹登录窗；已登录显示用户昵称，点击展开「退出登录」。
 * 刻意不做成独立登录页——登录是创建任务前的一道门，不是一次页面跳转，
 * 用户点完还要回到刚才的入口页继续操作。
 *
 * 登录态来自 `stores/auth`，本组件只负责交互与展示。
 */

const auth = useAuthStore()

/** 已登录时的下拉菜单是否展开 */
const menuOpen = ref(false)

/** 容器根元素，用于判断「点击是否发生在容器外」 */
const root = ref<HTMLElement | null>(null)

function handleClick(): void {
  if (!auth.isLoggedIn) {
    auth.openDialog('login')
    return
  }
  menuOpen.value = !menuOpen.value
}

function handleLogout(): void {
  menuOpen.value = false
  void auth.logout()
}

/** 点击容器外时收起下拉菜单 */
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
    <button
      type="button"
      class="flex items-center gap-1.5 rounded-full border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-600 shadow-sm transition hover:border-slate-300 hover:text-slate-900 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300 dark:hover:border-slate-600 dark:hover:text-slate-100"
      :aria-expanded="menuOpen"
      :aria-label="auth.isLoggedIn ? `已登录：${auth.displayName}` : '登录'"
      @click="handleClick"
    >
      <svg class="h-4 w-4 text-slate-400" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
        <path
          d="M10 8a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM3.465 14.493a1.23 1.23 0 0 0 .41 1.412A9.957 9.957 0 0 0 10 18c2.31 0 4.438-.784 6.131-2.1.43-.333.604-.903.408-1.41a7.002 7.002 0 0 0-13.074.003Z"
        />
      </svg>

      <span class="max-w-32 truncate">{{ auth.isLoggedIn ? auth.displayName : '登录' }}</span>

      <svg
        v-if="auth.isLoggedIn"
        class="h-3 w-3 text-slate-400 transition"
        :class="menuOpen ? 'rotate-180' : ''"
        viewBox="0 0 20 20"
        fill="currentColor"
        aria-hidden="true"
      >
        <path
          fill-rule="evenodd"
          d="M5.22 8.22a.75.75 0 0 1 1.06 0L10 11.94l3.72-3.72a.75.75 0 1 1 1.06 1.06l-4.25 4.25a.75.75 0 0 1-1.06 0L5.22 9.28a.75.75 0 0 1 0-1.06Z"
          clip-rule="evenodd"
        />
      </svg>
    </button>

    <Transition
      enter-active-class="transition duration-100"
      enter-from-class="opacity-0 -translate-y-1"
      leave-active-class="transition duration-100"
      leave-to-class="opacity-0"
    >
      <div
        v-if="menuOpen"
        class="absolute bottom-full right-0 mb-2 w-40 overflow-hidden rounded-xl border border-slate-200 bg-white py-1 shadow-lg dark:border-slate-700 dark:bg-slate-800"
      >
        <div class="border-b border-slate-100 px-3 py-2 dark:border-slate-700">
          <div class="text-xs font-semibold text-slate-900 dark:text-slate-100">{{ auth.displayName }}</div>
          <div class="text-[11px] text-slate-400 dark:text-slate-500">已登录</div>
        </div>
        <button
          type="button"
          class="w-full px-3 py-2 text-left text-xs text-slate-600 transition hover:bg-slate-50 hover:text-rose-600 dark:text-slate-300 dark:hover:bg-slate-700"
          @click="handleLogout"
        >
          退出登录
        </button>
      </div>
    </Transition>
  </div>
</template>
