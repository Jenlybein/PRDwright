<script setup lang="ts">
/**
 * 用户管理：列表 + 新建 + 启停 / 重置密码 / 强制下线 / 导出 / 删除。
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

import AdminModal from '@/components/AdminModal.vue'
import PageHeader from '@/components/PageHeader.vue'

import {
  createUser,
  deleteUser,
  exportUserData,
  fetchDeletionPreview,
  fetchUsers,
  messageOf,
  resetUserPassword,
  revokeUserSessions,
  setUserActive,
  setUserAdmin,
  setUserQuota,
  toResult,
  type AdminDeletionPreview,
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

/**
 * 当前打开的弹窗。
 *
 * 所有「需要额外空间才填得完」的东西都进弹窗，不再在卡片里就地展开：
 * 行内展开会撑高整行（额度编辑尤其明显），把下面几行推到视野之外，滚动位置也跟着漂。
 * `actions` 是这一页的操作清单——七个动作收进一个竖排列表，不在表格里排成一片小按钮。
 */
type Panel = 'create' | 'actions' | 'quota' | 'password' | 'revoke' | 'delete' | null
const panel = ref<Panel>(null)
/** 当前被操作的用户；新建账号时为 null */
const activeUser = ref<AdminUser | null>(null)

// 新建账号表单
const form = ref({ username: '', password: '', is_admin: false })
const creating = ref(false)

// 删除确认：记住待删的那一行，避免用 confirm() 弹窗（在部分环境被禁用）
const pendingDelete = ref<AdminUser | null>(null)
/**
 * 删除预览与「手打用户名」确认。
 *
 * 删除不可逆，原先只有一句「确定删除？」——管理员并不知道自己点掉的是 1 份方案
 * 还是 30 份。现在先拉一次预览把条数摆出来，并要求**手打被删的用户名**，
 * 打错按钮就是灰的（防的是「手滑点两下」这种最常见的误删）。
 */
const preview = ref<AdminDeletionPreview | null>(null)
const previewLoading = ref(false)
const deleteConfirmText = ref('')
const deleting = ref(false)
/** 正在导出哪一行（导出可能有点慢，按钮要给出反应，否则会被连点） */
const exportingId = ref<number | null>(null)
/**
 * 强制下线确认。与删除同样走面板而非 `confirm()`：
 * 这一步会把对方当前的登录立刻踢掉，说清「只踢下线、不禁登录」比一个系统弹窗有用。
 */
const pendingRevoke = ref<AdminUser | null>(null)
const revoking = ref(false)
// 重置密码：密码不落在列表状态里，只在弹窗里活到提交那一刻
const newPassword = ref('')

// 额度编辑：记住的是「被编辑那一行的原始覆盖值」，而不是生效值——
// 两者不同（继承全局的 10 与单独设的 10 数字一样，含义不同）。
const quotaForm = ref<UserQuotaPatch>({
  daily_task_limit: -1,
  daily_token_limit: -1,
  max_running: -1,
})
const savingQuota = ref(false)

/** 关掉当前弹窗并清掉临时输入：任何一条出路（取消 / 成功 / 点遮罩）都走它 */
function closePanel() {
  panel.value = null
  activeUser.value = null
  pendingDelete.value = null
  pendingRevoke.value = null
  deleteConfirmText.value = ''
  newPassword.value = ''
  preview.value = null
}

function openCreate() {
  form.value = { username: '', password: '', is_admin: false }
  panel.value = 'create'
}

/** 打开某一行的操作清单 */
function openActions(user: AdminUser) {
  activeUser.value = user
  panel.value = 'actions'
}

function openPassword(user: AdminUser) {
  activeUser.value = user
  newPassword.value = ''
  panel.value = 'password'
}

function openRevoke(user: AdminUser) {
  activeUser.value = user
  pendingRevoke.value = user
  panel.value = 'revoke'
}

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
  activeUser.value = user
  quotaForm.value = {
    daily_task_limit: rawOf(user.daily_task_limit, user.daily_task_source),
    daily_token_limit: rawOf(user.daily_token_limit, user.daily_token_source),
    max_running: rawOf(user.max_running, user.max_running_source),
  }
  panel.value = 'quota'
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
  closePanel()
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
  closePanel()
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
  closePanel()
  sayOk(`${user.username} 的密码已重置`)
}

/**
 * 打开删除面板：先拉预览。
 *
 * 预览拉不到（网络抖动 / 后端报错）**不拦着删除**——真删不动的时候后端会给出理由，
 * 而「因为预览挂了所以不能删」会让管理员卡在一个没有出口的界面里。
 * 这里只把数字留空，并在面板里说明「明细暂时取不到」。
 */
async function openDelete(user: AdminUser) {
  pendingDelete.value = user
  activeUser.value = user
  panel.value = 'delete'
  preview.value = null
  deleteConfirmText.value = ''
  previewLoading.value = true
  const result = await toResult(() => fetchDeletionPreview(user.id))
  previewLoading.value = false
  // 期间可能已经关掉或换了另一行，别把旧结果写进去
  if (pendingDelete.value?.id !== user.id) return
  preview.value = result.ok ? result.value : null
  if (!result.ok) error.value = result.message
}

/** 手打用户名与目标一致才允许删：防「手滑点两下」这类最常见的误删 */
const deleteAllowed = computed(
  () =>
    !!pendingDelete.value &&
    deleteConfirmText.value.trim() === pendingDelete.value.username &&
    !deleting.value,
)

async function onDelete(user: AdminUser) {
  if (!deleteAllowed.value) return
  deleting.value = true
  const result = await toResult(() => deleteUser(user.id))
  deleting.value = false
  closePanel()
  if (!result.ok) {
    sayFail(result.message)
    return
  }
  sayOk(`已删除 ${user.username}，其方案、文档与任务已一并清除`)
  await load()
}

/**
 * 导出账号数据（ZIP）。
 *
 * 失败理由要原样显示：后端会区分「账号不存在」与「数据超过导出上限」，
 * 两者都远好过一句「导出失败」。下载本身由 service 层触发（见 `exportUserData`）。
 */
async function onExport(user: AdminUser) {
  exportingId.value = user.id
  const result = await toResult(() => exportUserData(user))
  exportingId.value = null
  if (!result.ok) {
    sayFail(result.message)
    return
  }
  sayOk(`已导出 ${user.username} 的数据`)
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
  closePanel()
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
  <div class="space-y-5">
    <!-- 页头 -->
    <PageHeader kicker="Users / 账号" title="用户管理">
      <template #actions>
        <span class="brut-tag brut-tag-sky">{{ users.length }} 个账号</span>
        <button type="button" class="brut-btn brut-btn-xs brut-btn-primary" @click="openCreate">
          + 新建账号
        </button>
      </template>
      <template #desc>
        <p>
          停用只是禁止登录（可恢复）；删除会连同方案、文档与任务一并清除，不可恢复。
          操作被后端拒绝时，这里会原样显示它给出的理由——规则只在服务端写一份。
        </p>
      </template>
    </PageHeader>

    <p v-if="notice" class="brut-note brut-note-ok">{{ notice }}</p>
    <p v-if="error" class="brut-note brut-note-fail">{{ error }}</p>

    <!-- 列表。行内不展开任何东西：所有输入都进弹窗，行高因此恒定 -->
    <section class="brut-card-lg overflow-hidden">
      <div v-if="loading" class="px-4 py-8 text-sm text-subtle">加载中…</div>

      <div v-else class="overflow-x-auto">
        <table class="brut-table min-w-[860px]">
          <thead>
            <tr>
              <th>用户</th>
              <th>注册时间</th>
              <th>任务数</th>
              <th>Token 合计</th>
              <th>今日额度</th>
              <th>状态</th>
              <th class="w-[104px]">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="user in users" :key="user.id">
              <td>
                <div class="flex flex-wrap items-center gap-1.5">
                  <span class="font-bold text-ink">{{ user.username }}</span>
                  <span v-if="user.is_admin" class="brut-tag brut-tag-sky">管理员</span>
                  <span v-if="user.id === currentUserId" class="brut-tag brut-tag-slab">本人</span>
                </div>
              </td>
              <td class="tnum text-xs">{{ formatDate(user.created_at) }}</td>
              <td class="tnum">{{ user.task_count }}</td>
              <td class="tnum">{{ totalTokens(user).toLocaleString() }}</td>
              <td>
                <span
                  :class="quotaExhausted(user) ? 'text-danger-fg' : 'text-muted'"
                  class="tnum text-xs font-bold"
                  :title="`次数 ${user.daily_task_limit || '不限'} · Token ${user.daily_token_limit || '不限'} · 并发 ${user.max_running || '不限'}`"
                >
                  {{ quotaLabel(user) }}
                </span>
              </td>
              <td>
                <span
                  class="brut-tag"
                  :class="user.is_active ? 'brut-tag-ok' : 'brut-tag-warn'"
                >
                  {{ user.is_active ? '启用' : '已停用' }}
                </span>
              </td>
              <td>
                <!-- 七个动作收进一个弹窗列表：平铺成小按钮会把这一列撑到 320px，
                     整张表被挤得没有呼吸；列表里还能给每个动作配一句说明。 -->
                <button
                  type="button"
                  class="brut-btn brut-btn-xs"
                  :aria-label="`${user.username} 的操作`"
                  @click="openActions(user)"
                >
                  操作<span class="ml-1 text-[10px] text-subtle" aria-hidden="true">▾</span>
                </button>
              </td>
            </tr>
            <tr v-if="!users.length">
              <td colspan="7" class="px-4 py-10 text-center text-sm text-subtle">
                还没有任何用户
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- 新建账号 -->
    <AdminModal
      :open="panel === 'create'"
      kicker="Create"
      title="新建账号"
      size="sm"
      @close="closePanel"
    >
      <form class="space-y-3" @submit.prevent="onCreate">
        <label class="block">
          <span class="brut-label">用户名</span>
          <input
            v-model="form.username"
            type="text"
            required
            minlength="3"
            maxlength="32"
            class="brut-field"
            placeholder="3-32 个字符"
          />
        </label>
        <label class="block">
          <span class="brut-label">密码</span>
          <input
            v-model="form.password"
            type="password"
            required
            minlength="8"
            class="brut-field"
            placeholder="至少 8 位"
          />
        </label>
        <label
          class="flex items-center gap-2 border-2 border-brut bg-field px-3 py-2 text-sm font-bold text-ink"
        >
          <input v-model="form.is_admin" type="checkbox" class="brut-check" />
          设为管理员
        </label>

        <div class="flex items-center gap-2 pt-1">
          <button type="submit" :disabled="creating" class="brut-btn brut-btn-primary">
            {{ creating ? '创建中…' : '新建账号' }}
          </button>
          <button type="button" class="brut-btn" @click="closePanel">取消</button>
        </div>
      </form>
    </AdminModal>

    <!-- 操作清单：竖排可选列表，危险动作也在这里（染色，但不抢主位） -->
    <AdminModal
      :open="panel === 'actions'"
      kicker="Actions"
      :title="`操作 · ${activeUser?.username ?? ''}`"
      size="sm"
      @close="closePanel"
    >
      <div v-if="activeUser" class="action-list">
        <button type="button" class="action-list-item" @click="openQuota(activeUser)">
          <span class="action-list-name">编辑额度</span>
          <span class="action-list-hint">次数 / Token / 并发</span>
        </button>
        <button type="button" class="action-list-item" @click="openPassword(activeUser)">
          <span class="action-list-name">重置密码</span>
          <span class="action-list-hint">下一次登录起生效</span>
        </button>
        <button type="button" class="action-list-item" @click="onToggleAdmin(activeUser); closePanel()">
          <span class="action-list-name">
            {{ activeUser.is_admin ? '撤销管理员' : '设为管理员' }}
          </span>
          <span class="action-list-hint">至少保留一位</span>
        </button>
        <button type="button" class="action-list-item" @click="onToggleActive(activeUser); closePanel()">
          <span class="action-list-name">{{ activeUser.is_active ? '停用账号' : '启用账号' }}</span>
          <span class="action-list-hint">
            {{ activeUser.is_active ? '禁止登录，可恢复' : '恢复登录' }}
          </span>
        </button>
        <button
          type="button"
          class="action-list-item"
          :disabled="!activeUser.is_active"
          @click="openRevoke(activeUser)"
        >
          <span class="action-list-name">强制下线</span>
          <span class="action-list-hint">
            {{ activeUser.is_active ? '只吊销会话，不改密码' : '账号已停用，本就无法登录' }}
          </span>
        </button>
        <button
          type="button"
          class="action-list-item"
          :disabled="exportingId === activeUser.id"
          @click="onExport(activeUser); closePanel()"
        >
          <span class="action-list-name">
            {{ exportingId === activeUser.id ? '导出中…' : '导出账号数据' }}
          </span>
          <span class="action-list-hint">方案 / 文档 / 对话 / 任务的 ZIP</span>
        </button>
        <button
          type="button"
          class="action-list-item is-danger"
          @click="openDelete(activeUser)"
        >
          <span class="action-list-name">删除账号</span>
          <span class="action-list-hint">连同数据一并清除，不可恢复</span>
        </button>
      </div>
    </AdminModal>

    <!-- 额度：三个输入都是三态（-1 跟全局 / 0 不限 / 正数上限） -->
    <AdminModal
      :open="panel === 'quota'"
      kicker="Quota"
      :title="`编辑额度 · ${activeUser?.username ?? ''}`"
      size="sm"
      @close="closePanel"
    >
      <p class="kicker">-1 跟随全局 · 0 不限 · 正数为上限</p>
      <div v-if="activeUser" class="mt-3 grid gap-3">
        <label class="block">
          <span class="brut-label">每日生成次数</span>
          <input
            v-model.number="quotaForm.daily_task_limit"
            type="number"
            min="-1"
            class="brut-field"
          />
        </label>
        <label class="block">
          <span class="brut-label">每日 Token</span>
          <input
            v-model.number="quotaForm.daily_token_limit"
            type="number"
            min="-1"
            step="1000"
            class="brut-field"
          />
        </label>
        <label class="block">
          <span class="brut-label">同时运行任务</span>
          <input
            v-model.number="quotaForm.max_running"
            type="number"
            min="-1"
            class="brut-field"
          />
        </label>
      </div>
      <p v-if="activeUser" class="tnum mt-3 text-[11px] text-subtle">
        今日已用 {{ activeUser.usage_tasks_today }} 次 /
        {{ activeUser.usage_tokens_today.toLocaleString() }} Token
      </p>

      <template #footer>
        <button
          type="button"
          class="brut-btn brut-btn-primary"
          :disabled="savingQuota || !activeUser"
          @click="activeUser && onSaveQuota(activeUser)"
        >
          {{ savingQuota ? '保存中…' : '保存' }}
        </button>
        <button type="button" class="brut-btn" @click="closePanel">取消</button>
      </template>
    </AdminModal>

    <!-- 重置密码 -->
    <AdminModal
      :open="panel === 'password'"
      kicker="Password"
      :title="`重置密码 · ${activeUser?.username ?? ''}`"
      size="sm"
      @close="closePanel"
    >
      <label class="block">
        <span class="brut-label">新密码</span>
        <input
          v-model="newPassword"
          type="password"
          minlength="8"
          placeholder="至少 8 位"
          class="brut-field"
        />
      </label>
      <p class="mt-2 text-xs text-subtle">
        密码只在提交时经过一次内存，后端只记「已重置」，不记明文。
      </p>

      <template #footer>
        <button
          type="button"
          class="brut-btn brut-btn-primary"
          :disabled="!activeUser"
          @click="activeUser && onResetPassword(activeUser)"
        >
          确定
        </button>
        <button type="button" class="brut-btn" @click="closePanel">取消</button>
      </template>
    </AdminModal>

    <!-- 强制下线确认。说清「只是踢下线、不改密码、不禁登录」——三者的区别是后台最常见的误解 -->
    <AdminModal
      :open="panel === 'revoke'"
      kicker="Revoke"
      :title="`让「${pendingRevoke?.username ?? ''}」立刻下线？`"
      size="sm"
      @close="closePanel"
    >
      <p class="text-sm text-body">
        只吊销他当前的登录会话，<strong class="text-ink">不改密码、不禁登录</strong>——
        他下次请求会被要求重新登录，但用原密码立刻就能登回来。
        正在跑的任务不受影响（要停任务请去任务监控页）。
      </p>

      <template #footer>
        <button
          type="button"
          class="brut-btn brut-btn-primary"
          :disabled="revoking || !pendingRevoke"
          @click="pendingRevoke && onRevokeSessions(pendingRevoke)"
        >
          {{ revoking ? '处理中…' : '确认下线' }}
        </button>
        <button type="button" class="brut-btn" @click="closePanel">取消</button>
      </template>
    </AdminModal>

    <!-- 删除确认。用弹窗而不是 confirm()：后者在部分环境被禁用，且没法摆出明细 -->
    <AdminModal
      :open="panel === 'delete'"
      kicker="Delete"
      :title="`删除「${pendingDelete?.username ?? ''}」`"
      size="md"
      tone="danger"
      @close="closePanel"
    >
      <p class="text-sm font-bold text-danger-fg">
        其方案、文档与任务会一并清除，且无法恢复。
      </p>

      <!-- 明细：删之前先让人看清「删掉多少、留下什么」 -->
      <div v-if="previewLoading" class="mt-2 text-xs text-danger-fg">正在统计将删除的数据…</div>
      <div v-else-if="preview" class="mt-2 space-y-1 text-xs text-danger-fg">
        <p>
          将删除：方案 {{ preview.solutions }} 份 · 文档 {{ preview.documents }} 篇（含
          {{ preview.document_versions }} 个历史版本）· 对话 {{ preview.chat_turns }} 条 ·
          任务 {{ preview.jobs }} 个
          <span v-if="preview.running_jobs > 0">
            （其中 {{ preview.running_jobs }} 个正在运行，会先被取消）
          </span>
        </p>
        <p v-if="preview.sessions > 0">将同时吊销他当前的 {{ preview.sessions }} 条登录会话。</p>
        <p>
          会保留：操作审计 {{ preview.retained_audit }} 条、平台日志
          {{ preview.retained_logs }} 条 —— 这两类按设计不随账号删除。
        </p>
        <p class="font-bold">
          注意：任务记录会被一并删除，因此平台的总任务数与总 Token、以及历史图表都会随之减少。
        </p>
      </div>
      <p v-else class="mt-2 text-xs text-danger-fg">
        明细暂时取不到（不影响删除，但建议先刷新再操作）。
      </p>

      <p class="mt-2 text-xs text-danger-fg">如果只是想禁止他登录，请改用「停用账号」。</p>

      <label class="mt-3 block text-xs font-bold text-danger-fg">
        请输入「{{ pendingDelete?.username }}」以确认：
        <input
          v-model="deleteConfirmText"
          data-testid="delete-confirm-input"
          type="text"
          class="brut-field mt-1 border-danger-line bg-card text-sm text-ink"
          :placeholder="pendingDelete?.username"
          autocomplete="off"
        />
      </label>

      <template #footer>
        <button
          type="button"
          class="brut-btn brut-btn-danger"
          :disabled="!deleteAllowed || !pendingDelete"
          @click="pendingDelete && onDelete(pendingDelete)"
        >
          {{ deleting ? '删除中…' : '确认删除' }}
        </button>
        <button type="button" class="brut-btn" @click="closePanel">取消</button>
      </template>
    </AdminModal>
  </div>
</template>
