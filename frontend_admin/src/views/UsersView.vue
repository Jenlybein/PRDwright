<script setup lang="ts">
/**
 * 用户管理：列表 + 新建 + 启停 / 重置密码 / 强制下线 / 删除。
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
  revokeUserSessions,
  setUserActive,
  setUserAdmin,
  setUserQuota,
  toResult,
  type AdminUser,
  type QuotaSource,
  type UserQuotaPatch,
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
/**
 * 强制下线确认。与删除同样走面板而非 `confirm()`：
 * 这一步会把对方当前的登录立刻踢掉，说清「只踢下线、不禁登录」比一个系统弹窗有用。
 */
const pendingRevoke = ref<AdminUser | null>(null)
const revoking = ref(false)
// 重置密码：按行展开一个输入框，密码不落在列表状态里
const resettingId = ref<number | null>(null)
const newPassword = ref('')

// 额度编辑：同样按行展开。记住的是「被编辑那一行的原始覆盖值」，
// 而不是生效值——两者不同（继承全局的 10 与单独设的 10 数字一样，含义不同）。
const quotaEditingId = ref<number | null>(null)
const quotaForm = ref<UserQuotaPatch>({
  daily_task_limit: -1,
  daily_token_limit: -1,
  max_running: -1,
})
const savingQuota = ref(false)

/**
 * 生效值 → 提交值。
 *
 * 编辑框里必须显示「-1 / 0 / 具体值」这套三态，而不是生效数字：管理员看到
 * 继承来的「10」时，得能选择「继续保持继承」还是「给他单独定 10」——
 * 只看生效值，这两件事在界面上长得一模一样。
 */
function rawOf(value: number, source: QuotaSource): number {
  if (source === 'global') return -1
  if (source === 'off') return 0
  return value
}

function openQuota(user: AdminUser) {
  quotaEditingId.value = user.id
  quotaForm.value = {
    daily_task_limit: rawOf(user.daily_task_limit, user.daily_task_source),
    daily_token_limit: rawOf(user.daily_token_limit, user.daily_token_source),
    max_running: rawOf(user.max_running, user.max_running_source),
  }
}

function closeQuota() {
  quotaEditingId.value = null
}

async function onSaveQuota(user: AdminUser) {
  if (savingQuota.value) return
  savingQuota.value = true
  const submitted = { ...quotaForm.value }
  // 用 toResult 保留后端给的具体原因（取值非法时它会说清是哪一项）
  const result = await toResult(() => setUserQuota(user.id, submitted))
  savingQuota.value = false
  if (!result.ok) {
    sayFail(result.message)
    return
  }
  quotaEditingId.value = null
  await load()
  // 复核「提交的」与「生效的」是否一致。
  // `-1`（跟随全局）跳过——它到底等于几由全局默认决定，前端不知道那个值，
  // 硬比会把正常的继承误报成失败。其余两种（0 / 具体值）都能精确验证。
  const mismatched = (
    [
      [submitted.daily_task_limit, result.value.daily_task_limit],
      [submitted.daily_token_limit, result.value.daily_token_limit],
      [submitted.max_running, result.value.max_running],
    ] as const
  ).some(([sent, got]) => sent !== -1 && sent !== got)
  sayOk(
    mismatched
      ? `${user.username} 的额度已提交，但回读值与提交值不一致，请刷新后确认`
      : `${user.username} 的额度已生效（下一次生成起）`,
  )
}

/** 额度文案：不限 / 已用/上限 */
function quotaLabel(user: AdminUser): string {
  if (user.daily_task_limit <= 0) return '不限'
  return `${user.usage_tasks_today}/${user.daily_task_limit}`
}

/** 今日次数是否已用满（列表里标红，让管理员一眼看出谁被卡住了） */
function quotaExhausted(user: AdminUser): boolean {
  return user.daily_task_limit > 0 && user.usage_tasks_today >= user.daily_task_limit
}

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

/**
 * 强制下线。
 *
 * `revoked === 0` 要单独说一句「他本来就没登录」——后端返回的就是 0，不是失败。
 * 若统一提示「已强制下线」，管理员会以为踢掉了，实际上对方根本没在线，
 * 下次排查「为什么他还能用」时就会被这句话误导。
 */
async function onRevokeSessions(user: AdminUser) {
  revoking.value = true
  const result = await toResult(() => revokeUserSessions(user.id))
  revoking.value = false
  pendingRevoke.value = null
  if (!result.ok) {
    sayFail(result.message)
    return
  }
  sayOk(
    result.value > 0
      ? `已让 ${user.username} 下线（吊销 ${result.value} 条会话）`
      : `${user.username} 当前并未登录，无需下线`,
  )
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
            <th class="px-4 py-2 font-medium">今日额度</th>
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
            <td class="align-top px-4 py-3">
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
            <td class="align-top px-4 py-3 text-muted">{{ formatDate(user.created_at) }}</td>
            <td class="align-top px-4 py-3 text-muted">{{ user.task_count }}</td>
            <td class="align-top px-4 py-3 text-muted">{{ totalTokens(user).toLocaleString() }}</td>
            <td class="align-top px-4 py-3">
              <span
                :class="quotaExhausted(user) ? 'text-danger-fg' : 'text-muted'"
                class="text-xs"
                :title="`次数 ${user.daily_task_limit || '不限'} · Token ${user.daily_token_limit || '不限'} · 并发 ${user.max_running || '不限'}`"
              >
                {{ quotaLabel(user) }}
              </span>
            </td>
            <td class="align-top px-4 py-3">
              <span
                :class="user.is_active ? 'text-success-fg' : 'text-warning-fg'"
                class="text-xs"
              >
                {{ user.is_active ? '启用' : '已停用' }}
              </span>
            </td>
            <td class="align-top px-4 py-3">
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
                  class="rounded-control border border-line px-2.5 py-1 text-xs text-body"
                  @click="openQuota(user)"
                >
                  额度
                </button>
                <button
                  type="button"
                  class="rounded-control border border-line px-2.5 py-1 text-xs text-body"
                  :disabled="!user.is_active"
                  :title="user.is_active ? '吊销他的登录会话，下次请求即失效' : '账号已停用，本就无法登录'"
                  @click="pendingRevoke = user"
                >
                  强制下线
                </button>
                <button
                  type="button"
                  class="rounded-control border border-danger-line px-2.5 py-1 text-xs text-danger-fg"
                  @click="pendingDelete = user"
                >
                  删除
                </button>
              </div>

              <!-- 额度编辑：三个输入都是三态（-1 跟全局 / 0 不限 / 正数上限） -->
              <div v-if="quotaEditingId === user.id" class="mt-2 space-y-2">
                <p class="text-xs text-subtle">
                  -1 跟随全局（配置页的「每日额度」）· 0 不限 · 正数为上限
                </p>
                <div class="flex flex-wrap items-center gap-2">
                  <label class="flex items-center gap-1 text-xs text-muted">
                    次数
                    <input
                      v-model.number="quotaForm.daily_task_limit"
                      type="number"
                      min="-1"
                      class="w-20 rounded-control border border-line bg-field px-2 py-1 text-xs text-fg"
                    />
                  </label>
                  <label class="flex items-center gap-1 text-xs text-muted">
                    Token
                    <input
                      v-model.number="quotaForm.daily_token_limit"
                      type="number"
                      min="-1"
                      step="1000"
                      class="w-24 rounded-control border border-line bg-field px-2 py-1 text-xs text-fg"
                    />
                  </label>
                  <label class="flex items-center gap-1 text-xs text-muted">
                    并发
                    <input
                      v-model.number="quotaForm.max_running"
                      type="number"
                      min="-1"
                      class="w-16 rounded-control border border-line bg-field px-2 py-1 text-xs text-fg"
                    />
                  </label>
                </div>
                <div class="flex items-center gap-2">
                  <button
                    type="button"
                    class="rounded-control bg-accent px-2.5 py-1 text-xs text-on-accent"
                    :disabled="savingQuota"
                    @click="onSaveQuota(user)"
                  >
                    保存
                  </button>
                  <button type="button" class="text-xs text-subtle" @click="closeQuota">
                    取消
                  </button>
                  <span class="text-xs text-subtle">
                    今日已用 {{ user.usage_tasks_today }} 次 / {{ user.usage_tokens_today.toLocaleString() }} Token
                  </span>
                </div>
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
            <td colspan="7" class="px-4 py-6 text-center text-sm text-subtle">
              还没有任何用户
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 强制下线确认。说清「只是踢下线、不改密码、不禁登录」——三者的区别是后台最常见的误解 -->
    <div
      v-if="pendingRevoke"
      class="rounded-card border border-warning-line bg-warning-surface p-4"
    >
      <p class="text-sm text-warning-fg">
        让「{{ pendingRevoke.username }}」立刻下线？
      </p>
      <p class="mt-1 text-xs text-warning-fg">
        只吊销他当前的登录会话，<strong>不改密码、不禁登录</strong>——他下次请求会被要求重新登录，
        但用原密码立刻就能登回来。正在跑的任务不受影响（要停任务请去任务监控页）。
      </p>
      <div class="mt-3 flex items-center gap-2">
        <button
          type="button"
          class="rounded-control bg-accent px-3 py-1.5 text-sm font-medium text-on-accent disabled:opacity-50"
          :disabled="revoking"
          @click="onRevokeSessions(pendingRevoke)"
        >
          {{ revoking ? '处理中…' : '确认下线' }}
        </button>
        <button
          type="button"
          class="rounded-control border border-line px-3 py-1.5 text-sm text-body"
          @click="pendingRevoke = null"
        >
          取消
        </button>
      </div>
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
