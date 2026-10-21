<script setup lang="ts">
/**
 * 管理端外壳：顶栏 + 路由出口。
 *
 * 顶栏只在已登录时显示「当前账号 / 退出」：未登录的人看到的只有登录框，
 * 不必先渲染一套马上就会被守卫换掉的导航。
 */
import { RouterView, useRoute, useRouter } from 'vue-router'

import { useSessionStore } from '@/stores/session'

const session = useSessionStore()
const route = useRoute()
const router = useRouter()

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
