<script setup lang="ts">
/**
 * 登录页。
 *
 * 与普通登录页唯一的差别是：登录成功后**还要再判一次是不是管理员**——
 * 普通账号在这里登录成功是正常结果，只是随后会被送到无权限页，
 * 而不是直接进后台。这个判断由 `stores/session.ts` 里的 `login()` 完成。
 */
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useSessionStore } from '@/stores/session'
import { isApiError } from '@/services/errors'

const router = useRouter()
const route = useRoute()
const session = useSessionStore()

const username = ref('')
const password = ref('')
const submitting = ref(false)
/** 表单级错误：登录失败时显示，重新提交时清空 */
const formError = ref('')

/** 登录后要去哪。来自路由守卫记下的 `redirect`，缺省是概览页 */
const redirectTo = computed(() => {
  const raw = route.query.redirect
  return typeof raw === 'string' && raw.startsWith('/') ? raw : '/dashboard'
})

/**
 * 优先显示探测阶段的错误（例如后端连不上），其次才是本次登录失败。
 * 「后端不可用」比「密码错了」更需要被看见。
 */
const visibleError = computed(() => session.error || formError.value)

async function submit() {
  if (submitting.value) return
  formError.value = ''
  submitting.value = true
  try {
    await session.login({ username: username.value.trim(), password: password.value })
    if (session.isAdmin) {
      await router.replace(redirectTo.value)
    } else {
      // 登录成功了，但不是管理员：停在无权限页，而不是把人放进一个处处 403 的后台
      await router.replace({ name: 'forbidden' })
    }
  } catch (caught) {
    formError.value = isApiError(caught) ? caught.message : '登录失败，请稍后重试'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="flex min-h-[70vh] items-center justify-center px-4">
    <div class="w-full max-w-sm rounded-card border border-line bg-surface p-6 shadow-card">
      <h1 class="text-lg font-semibold text-fg">管理后台登录</h1>
      <p class="mt-1 text-sm text-muted">请使用管理员账号登录</p>

      <p
        v-if="visibleError"
        class="mt-4 rounded-control border border-danger-line bg-danger-surface px-3 py-2 text-sm text-danger-fg"
        role="alert"
      >
        {{ visibleError }}
      </p>

      <form class="mt-4 space-y-3" @submit.prevent="submit">
        <div>
          <label class="mb-1 block text-sm text-muted" for="admin-username">用户名</label>
          <input
            id="admin-username"
            v-model="username"
            class="w-full rounded-control border border-line bg-field px-3 py-2 text-sm text-fg"
            autocomplete="username"
            required
          />
        </div>
        <div>
          <label class="mb-1 block text-sm text-muted" for="admin-password">密码</label>
          <input
            id="admin-password"
            v-model="password"
            type="password"
            class="w-full rounded-control border border-line bg-field px-3 py-2 text-sm text-fg"
            autocomplete="current-password"
            required
          />
        </div>

        <button
          type="submit"
          class="w-full rounded-control bg-accent px-3 py-2 text-sm font-medium text-on-accent disabled:opacity-60"
          :disabled="submitting"
        >
          {{ submitting ? '登录中…' : '登录' }}
        </button>
      </form>
    </div>
  </div>
</template>
