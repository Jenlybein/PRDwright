<script setup lang="ts">
/**
 * 无权限页：已登录，但不是管理员。
 *
 * 单独一页而不是弹个提示，是为了把话说清楚：**账号没问题，是这个账号没权限**。
 * 若直接把人踢回登录页，用户会以为密码错了，反复登录却永远进不去——
 * 那正是把 403 当成 401 处理的典型后果。
 */
import { useRouter } from 'vue-router'

import ThemeSwitch from '@/components/ThemeSwitch.vue'
import { useSessionStore } from '@/stores/session'

const router = useRouter()
const session = useSessionStore()

async function switchAccount() {
  await session.logout()
  await router.replace({ name: 'login' })
}
</script>

<template>
  <div class="flex min-h-screen items-center justify-center p-4 pt-16 sm:p-6 sm:pt-20">
    <!-- 无权限页同样不套后台外壳，主题切换只能自带一份 -->
    <div class="fixed top-3 right-3 z-30">
      <ThemeSwitch />
    </div>

    <div class="w-full max-w-lg">
      <!-- 一个巨大的 403 牌：不靠文案先把性质说清 -->
      <div class="brut-slab-ink flex items-end justify-between px-5 py-4">
        <span class="font-mono text-5xl font-black leading-none tracking-tighter">403</span>
        <span class="kicker text-card/70">Forbidden</span>
      </div>

      <div class="brut-card-lg mt-4 p-6">
        <h1 class="text-2xl font-black tracking-tight text-ink">当前账号没有访问权限</h1>
        <p class="mt-3 text-sm text-body">
          <template v-if="session.username">
            已登录为
            <span class="font-mono font-bold text-ink">{{ session.username }}</span>
            ，但该账号不是平台管理员。
          </template>
          <template v-else> 该账号不是平台管理员。 </template>
        </p>
        <p class="mt-2 text-sm text-muted">
          管理员身份需要由运维在服务端任命。若你认为这是误会，请联系系统管理员。
        </p>

        <div class="mt-5 flex flex-wrap items-center gap-2">
          <button type="button" class="brut-btn brut-btn-primary" @click="switchAccount">
            换个账号登录
          </button>
        </div>
      </div>

      <p class="kicker mt-4 text-center">PRDwright · Admin Console</p>
    </div>
  </div>
</template>
