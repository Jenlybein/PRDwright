<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { useAuthStore } from '@/stores/auth'
import AppButton from '@/components/ui/AppButton.vue'

/**
 * 登录 / 注册弹窗。
 *
 * 两个页签共用一个弹窗，而不是做「登录页 + 注册页」互相跳转：
 * 用户在两件事之间切换的成本低（表单只差一个邀请码），来回跳页面反而容易丢输入。
 *
 * 组件不持有登录态，全部读写 `stores/auth`；它只管表单本身与本地校验
 * （两次密码是否一致这类后端无从判断的事），其余交给后端返回的错误文案。
 */

const auth = useAuthStore()

/** 用户名输入框，弹窗打开时自动聚焦 */
const usernameInput = ref<HTMLInputElement | null>(null)

const username = ref('')
const password = ref('')
/** 仅注册时使用；后端无从判断「两次输入是否一致」，只能由前端兜 */
const confirmPassword = ref('')
const inviteCode = ref('')

/** 本地校验的提示；与后端返回的 `auth.errorMessage` 分开，避免互相覆盖 */
const localError = ref('')

/** 当前是否在注册页签 */
const isRegister = computed(() => auth.dialogMode === 'register')

/** 最终展示给用户的错误：本地校验优先，否则用后端的 */
const errorText = computed(() => localError.value || auth.errorMessage)

/** 清空表单（切换页签、关闭弹窗时用） */
function resetForm(): void {
  password.value = ''
  confirmPassword.value = ''
  inviteCode.value = ''
  localError.value = ''
  auth.clearError()
}

/** 切换登录 / 注册页签 */
function switchMode(mode: 'login' | 'register'): void {
  if (auth.dialogMode === mode) return
  auth.dialogMode = mode
  resetForm()
}

/** 关闭弹窗 */
function close(): void {
  resetForm()
  auth.closeDialog()
}

function handleSubmit(): void {
  const name = username.value.trim()
  localError.value = ''
  auth.clearError()

  if (!name || !password.value) {
    localError.value = '请填写用户名和密码'
    return
  }

  if (isRegister.value) {
    // 这几条后端也会校验，但前端先拦一次能省掉一次往返，且提示更即时
    if (name.length < 3 || name.length > 32) {
      localError.value = '用户名长度需在 3-32 个字符之间'
      return
    }
    if (password.value.length < 8) {
      localError.value = '密码至少需要 8 位'
      return
    }
    if (password.value !== confirmPassword.value) {
      localError.value = '两次输入的密码不一致'
      return
    }
    if (!inviteCode.value.trim()) {
      localError.value = '请填写邀请码'
      return
    }

    void auth.register({
      username: name,
      password: password.value,
      inviteCode: inviteCode.value.trim(),
    })
    return
  }

  void auth.login({ username: name, password: password.value })
}

/** ESC 关闭 */
function handleKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape' && auth.dialogOpen) close()
}

onMounted(() => window.addEventListener('keydown', handleKeydown))
onBeforeUnmount(() => window.removeEventListener('keydown', handleKeydown))

// 打开时聚焦用户名输入框，用户可以直接开打
watch(
  () => auth.dialogOpen,
  (open) => {
    if (!open) return
    void nextTick(() => usernameInput.value?.focus())
  },
)
</script>

<template>
  <Teleport to="body">
    <Transition
      enter-active-class="transition duration-150"
      enter-from-class="opacity-0"
      leave-active-class="transition duration-150"
      leave-to-class="opacity-0"
    >
      <div
        v-if="auth.dialogOpen"
        class="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-slate-950/50 p-4 pt-24 backdrop-blur-sm"
        role="dialog"
        aria-modal="true"
        aria-label="登录或注册"
        @click.self="close"
      >
        <div class="w-full max-w-sm rounded-2xl border border-line bg-surface p-6 shadow-pop">
          <!-- 顶部：标题 + 关闭 -->
          <div class="flex items-start justify-between">
            <div>
              <h2 class="text-base font-bold text-fg">
                {{ isRegister ? '注册账号' : '登录' }}
              </h2>
              <p class="mt-1 text-xs text-subtle">
                {{
                  isRegister
                    ? '注册需要邀请码，成功后会自动登录'
                    : '登录后才能创建任务并调用模型'
                }}
              </p>
            </div>
            <AppButton variant="ghost" size="sm" class="-mr-1 -mt-1 !px-1.5 !py-1.5" aria-label="关闭" @click="close">
              <svg class="h-4 w-4" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                <path
                  d="M6.28 5.22a.75.75 0 0 0-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 1 0 1.06 1.06L10 11.06l3.72 3.72a.75.75 0 1 0 1.06-1.06L11.06 10l3.72-3.72a.75.75 0 0 0-1.06-1.06L10 8.94 6.28 5.22Z"
                />
              </svg>
            </AppButton>
          </div>

          <!-- 为什么弹出来：例如「创建任务需要先登录」，被动弹出时才显示 -->
          <p
            v-if="auth.promptReason"
            class="mt-4 rounded-xl border border-warning-line bg-warning-surface px-3 py-2 text-xs text-warning-fg"
          >
            {{ auth.promptReason }}
          </p>

          <!-- 页签 -->
          <div class="mt-4 flex gap-1 rounded-xl bg-surface-2 p-1 text-sm">
            <button
              type="button"
              class="flex-1 rounded-lg py-1.5 font-medium transition"
              :class="
                isRegister
                  ? 'text-muted hover:text-fg'
                  : 'bg-surface text-fg shadow-sm ring-1 ring-line-strong'
              "
              @click="switchMode('login')"
            >
              登录
            </button>
            <button
              type="button"
              class="flex-1 rounded-lg py-1.5 font-medium transition"
              :class="
                isRegister
                  ? 'bg-surface text-fg shadow-sm ring-1 ring-line-strong'
                  : 'text-muted hover:text-fg'
              "
              @click="switchMode('register')"
            >
              注册
            </button>
          </div>

          <form class="mt-4 space-y-3" @submit.prevent="handleSubmit">
            <label class="block">
              <span class="text-xs font-medium text-muted">用户名</span>
              <input
                ref="usernameInput"
                v-model="username"
                type="text"
                autocomplete="username"
                class="mt-1 w-full rounded-xl border border-line-strong bg-field px-3 py-2 text-sm text-fg outline-none transition placeholder:text-subtle focus:border-accent focus:ring-2 focus:ring-accent-soft-line"
                placeholder="3-32 个字符"
              />
            </label>

            <label class="block">
              <span class="text-xs font-medium text-muted">密码</span>
              <input
                v-model="password"
                type="password"
                :autocomplete="isRegister ? 'new-password' : 'current-password'"
                class="mt-1 w-full rounded-xl border border-line-strong bg-field px-3 py-2 text-sm text-fg outline-none transition placeholder:text-subtle focus:border-accent focus:ring-2 focus:ring-accent-soft-line"
                :placeholder="isRegister ? '至少 8 位' : '请输入密码'"
              />
            </label>

            <template v-if="isRegister">
              <label class="block">
                <span class="text-xs font-medium text-muted">确认密码</span>
                <input
                  v-model="confirmPassword"
                  type="password"
                  autocomplete="new-password"
                  class="mt-1 w-full rounded-xl border border-line-strong bg-field px-3 py-2 text-sm text-fg outline-none transition placeholder:text-subtle focus:border-accent focus:ring-2 focus:ring-accent-soft-line"
                  placeholder="再输入一次"
                />
              </label>

              <label class="block">
                <span class="text-xs font-medium text-muted">邀请码</span>
                <input
                  v-model="inviteCode"
                  type="text"
                  class="mt-1 w-full rounded-xl border border-line-strong bg-field px-3 py-2 text-sm text-fg outline-none transition placeholder:text-subtle focus:border-accent focus:ring-2 focus:ring-accent-soft-line"
                  placeholder="注册需要邀请码"
                />
              </label>
            </template>

            <p v-if="errorText" class="rounded-xl bg-danger-surface px-3 py-2 text-xs text-danger-fg">
              {{ errorText }}
            </p>

            <AppButton
              type="submit"
              variant="primary"
              block
              :loading="auth.submitting"
              :disabled="auth.submitting"
            >
              {{ auth.submitting ? '处理中…' : isRegister ? '注册并登录' : '登录' }}
            </AppButton>
          </form>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>
