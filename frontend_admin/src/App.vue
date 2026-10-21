<script setup lang="ts">
/**
 * 管理端外壳：顶栏 + 路由出口。
 *
 * 顶栏只在已登录时显示「当前账号 / 退出」：未登录的人看到的只有登录框，
 * 不必先渲染一套马上就会被守卫换掉的导航。
 */
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'

import { useSessionStore } from '@/stores/session'

const session = useSessionStore()
const route = useRoute()
const router = useRouter()

/** 后台的顶栏导航。新增页面时只改这一处，不用去每个页面里放返回链接 */
const navItems = [
  { name: 'dashboard', label: '概览' },
  { name: 'users', label: '用户' },
  { name: 'invites', label: '邀请码' },
  { name: 'audit', label: '操作记录' },
]

async function onLogout() {
  await session.logout()
  await router.replace({ name: 'login' })
}
</script>

<template>
  <div class="min-h-screen bg-bg">
    <header
      v-if="route.name !== 'login'"
      class="border-b border-line bg-surface"
    >
      <div class="mx-auto flex max-w-5xl items-center justify-between px-4 py-3">
        <div class="flex items-baseline gap-2">
          <span class="text-sm font-semibold text-fg">PRDwright</span>
          <span class="text-xs text-subtle">管理后台</span>
        </div>
        <div v-if="session.isLoggedIn" class="flex items-center gap-3">
          <nav class="flex items-center gap-1">
            <RouterLink
              v-for="item in navItems"
              :key="item.name"
              :to="{ name: item.name }"
              class="rounded-control px-2.5 py-1 text-sm text-body hover:bg-surface-2"
              active-class="bg-surface-2 font-medium text-fg"
            >
              {{ item.label }}
            </RouterLink>
          </nav>
          <span class="text-sm text-muted">{{ session.username }}</span>
          <button
            type="button"
            class="rounded-control border border-line px-2.5 py-1 text-sm text-body"
            @click="onLogout"
          >
            退出
          </button>
        </div>
      </div>
    </header>

    <main class="mx-auto max-w-5xl px-4 py-6">
      <RouterView />
    </main>
  </div>
</template>
