<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId, watch } from 'vue'

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

/** 标题 id：让 dialog 通过 aria-labelledby 读到「登录」/「注册账号」 */
const titleId = useId()

/** 面板元素：焦点陷阱的边界，也是无输入可聚焦时的兜底落点 */
const panelEl = ref<HTMLElement | null>(null)

/** 打开前的焦点落点：关闭弹窗后原路还回去 */
let lastFocused: HTMLElement | null = null

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'

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

/** ESC 关闭；Tab 在面板内循环，不跑到背景页面上 */
function handleKeydown(event: KeyboardEvent): void {
  if (!auth.dialogOpen) return
  if (event.key === 'Escape') {
    close()
    return
  }
  if (event.key !== 'Tab' || !panelEl.value) return

  const items = Array.from(panelEl.value.querySelectorAll<HTMLElement>(FOCUSABLE))
  if (items.length === 0) {
    event.preventDefault()
    return
  }
  const first = items[0] as HTMLElement
  const last = items[items.length - 1] as HTMLElement
  const active = document.activeElement

  if (event.shiftKey && active === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && active === last) {
    event.preventDefault()
    first.focus()
  }
}

onMounted(() => window.addEventListener('keydown', handleKeydown))
onBeforeUnmount(() => {
  window.removeEventListener('keydown', handleKeydown)
  lastFocused?.focus()
})

// 打开时记录焦点落点并聚焦用户名输入框；关闭时把焦点还回去，用户可以直接开打
watch(
  () => auth.dialogOpen,
  (open) => {
    if (open) {
      lastFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null
      void nextTick(() => usernameInput.value?.focus())
      return
    }
    lastFocused?.focus()
    lastFocused = null
  },
)
</script>

<template>
  <Teleport to="body">
    <Transition name="auth-dialog">
      <div
        v-if="auth.dialogOpen"
        class="auth-backdrop fixed inset-0 z-50 flex items-start justify-center overflow-y-auto overscroll-contain bg-stone-950/50 p-4 pt-24 backdrop-blur-sm"
        role="dialog"
        aria-modal="true"
        :aria-labelledby="titleId"
        tabindex="-1"
        @click.self="close"
      >
        <div
          ref="panelEl"
          class="auth-panel w-full max-w-sm rounded-2xl border border-line bg-surface p-6 shadow-pop outline-none"
          tabindex="-1"
        >
          <!-- 顶部：标题 + 关闭 -->
          <div class="flex items-start justify-between">
            <div>
              <h2 :id="titleId" class="text-base font-bold text-fg">
                {{ isRegister ? '注册账号' : '登录' }}
              </h2>
              <!-- 登录态不再叠一句「登录后才能创建任务并调用模型」：被动弹出时
                   下面那条黄条已经说了为什么，这里是重复的。注册态保留邀请码说明（真信息）。 -->
              <p v-if="isRegister" class="mt-1 text-xs text-subtle">
                注册需要邀请码，成功后会自动登录
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

<style scoped>
/* 遮罩只动透明度，面板做 0.96→1 的缩放，两者同时开始、一起收束 */
.auth-dialog-enter-active,
.auth-dialog-leave-active {
  transition: opacity 180ms var(--ease-smooth);
}
.auth-dialog-enter-active .auth-panel,
.auth-dialog-leave-active .auth-panel {
  transition: transform 220ms var(--ease-smooth);
}
.auth-dialog-enter-from,
.auth-dialog-leave-to {
  opacity: 0;
}
.auth-dialog-enter-from .auth-panel,
.auth-dialog-leave-to .auth-panel {
  transform: scale(0.96);
}
</style>
