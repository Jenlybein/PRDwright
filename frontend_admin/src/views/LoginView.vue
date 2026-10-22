<script setup lang="ts">
/**
 * 登录页。
 *
 * 文案只留「这是什么面板」与两个必填项：
 * 用不着在这里介绍后台有哪些功能、谁能登录、登录态存在哪——
 * 管理员已经知道自己在找什么；其余每一句都在削弱输入框的存在感。
 *
 * 登录成功后**还要再判一次是不是管理员**：普通账号登录成功是正常结果，
 * 只是随后会被送到无权限页。这个判断由 `stores/session.ts` 里的 `login()` 完成。
 */
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import ThemeSwitch from '@/components/ThemeSwitch.vue'
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
  <div class="flex min-h-screen items-center justify-center p-4 pt-16 sm:p-6 sm:pt-20">
    <!-- 还没登录也要能切主题：暗色用户第一眼看到的就是这一页 -->
    <div class="fixed top-3 right-3 z-30">
      <ThemeSwitch />
    </div>

    <div class="w-full max-w-4xl">
      <!-- 窄屏没有左侧海报，品牌名挪到表单上方，否则这一页没有任何落款。
           `normal-case`：kicker 默认会把字母转大写，品牌名得保持原样 -->
      <p class="mb-4 flex items-center justify-center gap-2 lg:hidden">
        <span class="h-2 w-2 bg-sky"></span>
        <span class="kicker normal-case">PRDwright</span>
      </p>

      <div class="grid gap-4 lg:grid-cols-[1fr_1.02fr] lg:gap-6">
        <!-- 左：整块天蓝海报。只留品牌，别的一概不放 -->
        <div
          class="brut-slab-sky relative hidden flex-col justify-between overflow-hidden p-8 lg:flex"
        >
          <p class="kicker relative text-on-sky/70">Admin Console</p>

          <div class="relative">
            <!-- 0.95 行高是这套视觉的下限；再紧就会让字的重心压到下一行 -->
            <h1 class="text-[4.2rem] leading-[0.95] font-black tracking-tight">
              PRD<br />wright
            </h1>
            <!-- 收边：一条实线加一个实心角标，比再写一句说明更安静 -->
            <div class="mt-7 flex items-center gap-2.5" aria-hidden="true">
              <span class="h-[3px] flex-1 bg-on-sky/25"></span>
              <span class="h-2.5 w-2.5 bg-on-sky/70"></span>
            </div>
          </div>
        </div>

        <!-- 右：表单。标题之后直接是输入框 -->
        <div class="brut-card-lg flex flex-col justify-center p-7 sm:p-9">
          <p class="kicker">Sign in</p>
          <h1 class="mt-2 text-2xl font-black tracking-tight text-ink">管理员登录</h1>

          <p v-if="visibleError" class="brut-note brut-note-fail mt-4" role="alert">
            {{ visibleError }}
          </p>

          <form class="mt-6 space-y-4" @submit.prevent="submit">
            <div>
              <label class="brut-label" for="admin-username">用户名</label>
              <input
                id="admin-username"
                v-model="username"
                class="brut-field"
                autocomplete="username"
                required
              />
            </div>
            <div>
              <label class="brut-label" for="admin-password">密码</label>
              <input
                id="admin-password"
                v-model="password"
                type="password"
                class="brut-field"
                autocomplete="current-password"
                required
              />
            </div>

            <button
              type="submit"
              class="brut-btn brut-btn-primary w-full py-2.5"
              :disabled="submitting"
            >
              {{ submitting ? '登录中…' : '进入后台' }}
            </button>
          </form>
        </div>
      </div>
    </div>
  </div>
</template>
