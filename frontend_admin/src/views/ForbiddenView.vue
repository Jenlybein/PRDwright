<script setup lang="ts">
/**
 * 无权限页：已登录，但不是管理员。
 *
 * 单独一页而不是弹个提示，是为了把话说清楚：**账号没问题，是这个账号没权限**。
 * 若直接把人踢回登录页，用户会以为密码错了，反复登录却永远进不去——
 * 那正是把 403 当成 401 处理的典型后果。
 */
import { useRouter } from 'vue-router'

import { useSessionStore } from '@/stores/session'

const router = useRouter()
const session = useSessionStore()

async function switchAccount() {
  await session.logout()
  await router.replace({ name: 'login' })
}
</script>

<template>
  <div class="flex min-h-[70vh] items-center justify-center px-4">
    <div class="w-full max-w-md rounded-card border border-line bg-surface p-6 shadow-card">
      <h1 class="text-lg font-semibold text-fg">当前账号没有访问权限</h1>
      <p class="mt-2 text-sm text-body">
        <template v-if="session.username">
          已登录为
          <span class="font-medium text-fg">{{ session.username }}</span>
          ，但该账号不是平台管理员。
        </template>
        <template v-else> 该账号不是平台管理员。 </template>
      </p>
      <p class="mt-2 text-sm text-muted">
        管理员身份需要由运维在服务端任命。若你认为这是误会，请联系系统管理员。
      </p>
      <button
        type="button"
        class="mt-4 rounded-control border border-line-strong px-3 py-2 text-sm text-fg"
        @click="switchAccount"
      >
        换个账号登录
      </button>
    </div>
  </div>
</template>
