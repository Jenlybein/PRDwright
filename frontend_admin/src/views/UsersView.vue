<script setup lang="ts">
/**
 * 用户管理：列表 + 新建 + 启停 / 重置密码 / 删除。
 *
 * 三条交互纪律：
 * 1. **规则由后端说了算**。页面上的禁用按钮只是提示，真正的判断在服务端
 *    （`require_admin` 与 `admin_service`）。前端硬编码「不能删自己」，
 *    将来规则一变就会和后端不一致，而用户看到的是前端那套错的。
 *    所以这里照常发请求，把后端回绝的原因原样显示出来。
 * 2. **删除要二次确认**。删号不可逆，方案与文档一并清掉；
 *    停用是可逆的，所以不需要确认。
 * 3. **改完就重新拉列表**。不本地改一行假装成功——用量是聚合出来的，
 *    本地改出来的数字会和后端不一致。
 */
import { computed, onMounted, ref } from 'vue'

import {
  createUser,
  deleteUser,
  fetchUsers,
  messageOf,
  resetUserPassword,
  setUserActive,
  setUserAdmin,
  toResult,
  type AdminUser,
} from '@/services/adminService'
import { useSessionStore } from '@/stores/session'

const session = useSessionStore()

const users = ref<AdminUser[]>([])
const loading = ref(true)
const error = ref('')
/** 操作反馈。成功也要说一句，否则点了没反应会被当成没生效 */
const notice = ref('')

// 新建账号表单
const form = ref({ username: '', password: '', is_admin: false })
const creating = ref(false)

// 删除确认：记住待删的那一行，避免用 confirm() 弹窗（在部分环境被禁用）
const pendingDelete = ref<AdminUser | null>(null)
// 重置密码：按行展开一个输入框，密码不落在列表状态里
const resettingId = ref<number | null>(null)
const newPassword = ref('')

/** 当前登录者自己：列表里给他一个「本人」标记，避免误操作 */
const currentUserId = computed(() => session.userId)

async function load() {
  loading.value = true
  error.value = ''
  try {
    users.value = await fetchUsers()
  } catch (err) {
    error.value = messageOf(err)
  } finally {
    loading.value = false
  }
}

onMounted(load)

function sayOk(text: string) {
  notice.value = text
  error.value = ''
}

/**
 * 显示一条**已经取好**的错误文案。
 *
 * 参数是字符串而不是异常对象：这里的文案来自 `toResult` 从后端响应里读出的
 * `detail`。若再交给 `messageOf` 处理一遍，它认不出这不是 `ApiError`，
 * 会把后端给的理由覆盖成一句泛泛的「操作失败」——管理员于是看不到
 * 「不能停用自己的账号」这类真正有用的原因。
 */
function sayFail(message: string) {
  error.value = message
  notice.value = ''
}

async function onCreate() {
  creating.value = true
  const result = await toResult(() =>
    createUser({
      username: form.value.username.trim(),
      password: form.value.password,
      is_admin: form.value.is_admin,
    }),
  )
  creating.value = false

  if (!result.ok) {
    sayFail(result.message)
    return
  }
  form.value = { username: '', password: '', is_admin: false }
  sayOk('账号已创建')
  await load()
}

async function onToggleActive(user: AdminUser) {
  const result = await toResult(() => setUserActive(user.id, !user.is_active))
  if (!result.ok) {
    // 后端回绝的原因（如「不能停用自己的账号」）原样显示，
    // 比前端自己猜一句「操作失败」有用得多
    sayFail(result.message)
    return
  }
  sayOk(user.is_active ? `已停用 ${user.username}` : `已启用 ${user.username}`)
  await load()
}

async function onToggleAdmin(user: AdminUser) {
  const result = await toResult(() => setUserAdmin(user.id, !user.is_admin))
  if (!result.ok) {
    // 典型回绝：「这是最后一位管理员，不能撤销——至少保留一位」
    sayFail(result.message)
    return
  }
  sayOk(user.is_admin ? `已撤销 ${user.username} 的管理员身份` : `已将 ${user.username} 设为管理员`)
  await load()
}

async function onResetPassword(user: AdminUser) {
  const result = await toResult(() => resetUserPassword(user.id, newPassword.value))
  if (!result.ok) {
    sayFail(result.message)
    return
  }
  resettingId.value = null
  newPassword.value = ''
  sayOk(`${user.username} 的密码已重置`)
}

async function onDelete(user: AdminUser) {
  const result = await toResult(() => deleteUser(user.id))
  pendingDelete.value = null
  if (!result.ok) {
    sayFail(result.message)
    return
  }
  sayOk(`已删除 ${user.username}`)
  await load()
}

/** 时间只显示到日：后台关心的是「什么时候注册的」，不是精确到毫秒 */
function formatDate(value: string): string {
  return value ? value.slice(0, 10) : '—'
}

function totalTokens(user: AdminUser): number {
  return user.prompt_tokens + user.completion_tokens
}
</script>

<template>
  <div class="space-y-4">
    <div class="rounded-card border border-line bg-surface p-5 shadow-card">
      <h1 class="text-base font-semibold text-fg">用户管理</h1>
      <p class="mt-1 text-sm text-muted">
        停用只是禁止登录（可恢复）；删除会连同方案、文档与任务一并清除，不可恢复。
      </p>
    </div>

    <p v-if="notice" class="rounded-card border border-success-line bg-success-surface px-4 py-3 text-sm text-success-fg">
      {{ notice }}
    </p>
    <p v-if="error" class="rounded-card border border-danger-line bg-danger-surface px-4 py-3 text-sm text-danger-fg">
      {{ error }}
    </p>

    <!-- 新建账号 -->
    <form
      class="rounded-card border border-line bg-surface p-4"
      @submit.prevent="onCreate"
    >
      <div class="grid gap-3 sm:grid-cols-[1fr_1fr_auto_auto] sm:items-end">
        <label class="block">
          <span class="text-xs text-subtle">用户名</span>
          <input
            v-model="form.username"
            type="text"
            required
            minlength="3"
            maxlength="32"
            class="mt-1 w-full rounded-control border border-line bg-field px-3 py-2 text-sm text-fg"
            placeholder="3-32 个字符"
          />
        </label>
        <label class="block">
          <span class="text-xs text-subtle">密码</span>
          <input
            v-model="form.password"
            type="password"
            required
            minlength="8"
            class="mt-1 w-full rounded-control border border-line bg-field px-3 py-2 text-sm text-fg"
            placeholder="至少 8 位"
          />
        </label>
        <label class="flex items-center gap-2 pb-2 text-sm text-body">
          <input v-model="form.is_admin" type="checkbox" />
          设为管理员
        </label>
        <button
          type="submit"
          :disabled="creating"
          class="rounded-control bg-accent px-4 py-2 text-sm font-medium text-on-accent disabled:opacity-60"
        >
          {{ creating ? '创建中…' : '新建账号' }}
        </button>
      </div>
    </form>

    <!-- 列表 -->
    <div class="rounded-card border border-line bg-surface">
      <div v-if="loading" class="px-4 py-6 text-sm text-subtle">加载中…</div>

      <table v-else class="w-full text-sm">
        <thead class="border-b border-line text-left text-xs text-subtle">
          <tr>
            <th class="px-4 py-2 font-medium">用户</th>
            <th class="px-4 py-2 font-medium">注册时间</th>
            <th class="px-4 py-2 font-medium">任务数</th>
            <th class="px-4 py-2 font-medium">Token 合计</th>
            <th class="px-4 py-2 font-medium">状态</th>
            <th class="px-4 py-2 font-medium">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="user in users"
            :key="user.id"
            class="border-b border-line last:border-0"
          >
            <td class="px-4 py-3">
              <div class="flex items-center gap-2">
                <span class="font-medium text-fg">{{ user.username }}</span>
                <span
                  v-if="user.is_admin"
                  class="rounded bg-accent-soft px-1.5 py-0.5 text-xs text-accent-soft-line"
                >
                  管理员
                </span>
                <span
                  v-if="user.id === currentUserId"
                  class="rounded bg-surface-2 px-1.5 py-0.5 text-xs text-subtle"
                >
                  本人
                </span>
              </div>
            </td>
            <td class="px-4 py-3 text-muted">{{ formatDate(user.created_at) }}</td>
            <td class="px-4 py-3 text-muted">{{ user.task_count }}</td>
            <td class="px-4 py-3 text-muted">{{ totalTokens(user).toLocaleString() }}</td>
            <td class="px-4 py-3">
              <span
                :class="user.is_active ? 'text-success-fg' : 'text-warning-fg'"
                class="text-xs"
              >
                {{ user.is_active ? '启用' : '已停用' }}
              </span>
            </td>
            <td class="px-4 py-3">
              <div class="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  class="rounded-control border border-line px-2.5 py-1 text-xs text-body"
                  @click="onToggleActive(user)"
                >
                  {{ user.is_active ? '停用' : '启用' }}
                </button>
                <button
                  type="button"
                  class="rounded-control border border-line px-2.5 py-1 text-xs text-body"
                  @click="onToggleAdmin(user)"
                >
                  {{ user.is_admin ? '撤销管理员' : '设为管理员' }}
                </button>
                <button
                  type="button"
                  class="rounded-control border border-line px-2.5 py-1 text-xs text-body"
                  @click="resettingId = user.id"
                >
                  重置密码
                </button>
                <button
                  type="button"
                  class="rounded-control border border-danger-line px-2.5 py-1 text-xs text-danger-fg"
                  @click="pendingDelete = user"
                >
                  删除
                </button>
              </div>

              <!-- 重置密码：就地展开 -->
              <div v-if="resettingId === user.id" class="mt-2 flex items-center gap-2">
                <input
                  v-model="newPassword"
                  type="password"
                  minlength="8"
                  placeholder="新密码，至少 8 位"
                  class="w-48 rounded-control border border-line bg-field px-2 py-1 text-xs text-fg"
                />
                <button
                  type="button"
                  class="rounded-control bg-accent px-2.5 py-1 text-xs text-on-accent"
                  @click="onResetPassword(user)"
                >
                  确定
                </button>
                <button
                  type="button"
                  class="text-xs text-subtle"
                  @click="resettingId = null; newPassword = ''"
                >
                  取消
                </button>
              </div>
            </td>
          </tr>
          <tr v-if="!users.length">
            <td colspan="6" class="px-4 py-6 text-center text-sm text-subtle">
              还没有任何用户
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 删除确认。用面板而不是 confirm()：后者在部分环境被禁用，且没法说明后果 -->
    <div
      v-if="pendingDelete"
      class="rounded-card border border-danger-line bg-danger-surface p-4"
    >
      <p class="text-sm text-danger-fg">
        确定删除「{{ pendingDelete.username }}」？其方案、文档与任务会一并清除，且无法恢复。
      </p>
      <p class="mt-1 text-xs text-danger-fg">
        如果只是想禁止他登录，请改用「停用」。
      </p>
      <div class="mt-3 flex items-center gap-2">
        <button
          type="button"
          class="rounded-control bg-danger px-3 py-1.5 text-sm text-on-accent"
          @click="onDelete(pendingDelete)"
        >
          确认删除
        </button>
        <button
          type="button"
          class="rounded-control border border-line px-3 py-1.5 text-sm text-body"
          @click="pendingDelete = null"
        >
          取消
        </button>
      </div>
    </div>
  </div>
</template>
