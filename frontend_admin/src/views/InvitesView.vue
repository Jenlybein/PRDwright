<script setup lang="ts">
/**
 * 邀请码管理：多码并存，可设次数上限与过期时间。
 *
 * 两处刻意的设计：
 * 1. **码不转大写**。已发出的码是什么样就存什么样——中途改大小写会让
 *    「用户照着旧邮件复制的码」突然失效，而这种失效后台没有任何日志可查。
 * 2. **删除只提示「后面的人不能再用了」**。已经注册的用户与码无关，
 *    如果在文案上写成「删除邀请码会移除相关用户」，管理员会被吓得不敢清理。
 */
import { onMounted, ref } from 'vue'

import {
  createInvite,
  deleteInvite,
  fetchInvites,
  messageOf,
  toResult,
  type AdminInvite,
} from '@/services/adminService'

const invites = ref<AdminInvite[]>([])
const loading = ref(true)
const error = ref('')
const notice = ref('')

const form = ref({ code: '', max_uses: 0, expires_at: '', note: '' })
const creating = ref(false)
const pendingDelete = ref<AdminInvite | null>(null)

async function load() {
  loading.value = true
  error.value = ''
  try {
    invites.value = await fetchInvites()
  } catch (err) {
    error.value = messageOf(err)
  } finally {
    loading.value = false
  }
}

onMounted(load)

async function onCreate() {
  creating.value = true
  const result = await toResult(() =>
    createInvite({
      code: form.value.code.trim(),
      max_uses: Number(form.value.max_uses) || 0,
      // 空串表示永不过期；后端接受 ISO 8601
      expires_at: form.value.expires_at ? new Date(form.value.expires_at).toISOString() : null,
      note: form.value.note.trim(),
    }),
  )
  creating.value = false

  if (!result.ok) {
    error.value = result.message
    notice.value = ''
    return
  }
  form.value = { code: '', max_uses: 0, expires_at: '', note: '' }
  notice.value = '邀请码已创建'
  error.value = ''
  await load()
}

async function onDelete(invite: AdminInvite) {
  const result = await toResult(() => deleteInvite(invite.code))
  pendingDelete.value = null
  if (!result.ok) {
    error.value = result.message
    notice.value = ''
    return
  }
  notice.value = `已删除 ${invite.code}`
  error.value = ''
  await load()
}

/** 用尽 / 过期一眼可见，不必心算 */
function isExhausted(invite: AdminInvite): boolean {
  if (invite.max_uses > 0 && invite.used_count >= invite.max_uses) return true
  if (invite.expires_at && invite.expires_at < new Date().toISOString()) return true
  return false
}

function usageText(invite: AdminInvite): string {
  return invite.max_uses === 0 ? `${invite.used_count} / 不限` : `${invite.used_count} / ${invite.max_uses}`
}

function formatDate(value: string | null): string {
  return value ? value.slice(0, 10) : '长期有效'
}
</script>

<template>
  <div class="space-y-4">
    <div class="rounded-card border border-line bg-surface p-5 shadow-card">
      <h1 class="text-base font-semibold text-fg">邀请码管理</h1>
      <p class="mt-1 text-sm text-muted">
        注册需要邀请码。删除一个码只影响之后的注册，已经注册的用户不受影响。
      </p>
    </div>

    <p v-if="notice" class="rounded-card border border-success-line bg-success-surface px-4 py-3 text-sm text-success-fg">
      {{ notice }}
    </p>
    <p v-if="error" class="rounded-card border border-danger-line bg-danger-surface px-4 py-3 text-sm text-danger-fg">
      {{ error }}
    </p>

    <form class="rounded-card border border-line bg-surface p-4" @submit.prevent="onCreate">
      <div class="grid gap-3 sm:grid-cols-[1fr_auto_1fr_1fr_auto] sm:items-end">
        <label class="block">
          <span class="text-xs text-subtle">邀请码</span>
          <input
            v-model="form.code"
            type="text"
            required
            minlength="4"
            maxlength="32"
            class="mt-1 w-full rounded-control border border-line bg-field px-3 py-2 text-sm text-fg"
            placeholder="如 ALPHA2"
          />
        </label>
        <label class="block">
          <span class="text-xs text-subtle">次数上限</span>
          <input
            v-model.number="form.max_uses"
            type="number"
            min="0"
            class="mt-1 w-28 rounded-control border border-line bg-field px-3 py-2 text-sm text-fg"
          />
          <span class="mt-1 block text-xs text-subtle">0 = 不限</span>
        </label>
        <label class="block">
          <span class="text-xs text-subtle">过期时间</span>
          <input
            v-model="form.expires_at"
            type="date"
            class="mt-1 w-full rounded-control border border-line bg-field px-3 py-2 text-sm text-fg"
          />
        </label>
        <label class="block">
          <span class="text-xs text-subtle">备注</span>
          <input
            v-model="form.note"
            type="text"
            maxlength="64"
            class="mt-1 w-full rounded-control border border-line bg-field px-3 py-2 text-sm text-fg"
            placeholder="给谁用"
          />
        </label>
        <button
          type="submit"
          :disabled="creating"
          class="rounded-control bg-accent px-4 py-2 text-sm font-medium text-on-accent disabled:opacity-60"
        >
          {{ creating ? '创建中…' : '新建邀请码' }}
        </button>
      </div>
      <p class="mt-2 text-xs text-subtle">
        只允许大写字母与数字，且不含 O/0、I/1 等易混字符——码是要被人手抄的。
      </p>
    </form>

    <div class="rounded-card border border-line bg-surface">
      <div v-if="loading" class="px-4 py-6 text-sm text-subtle">加载中…</div>

      <table v-else class="w-full text-sm">
        <thead class="border-b border-line text-left text-xs text-subtle">
          <tr>
            <th class="px-4 py-2 font-medium">邀请码</th>
            <th class="px-4 py-2 font-medium">已用 / 上限</th>
            <th class="px-4 py-2 font-medium">过期时间</th>
            <th class="px-4 py-2 font-medium">备注</th>
            <th class="px-4 py-2 font-medium">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="invite in invites"
            :key="invite.code"
            class="border-b border-line last:border-0"
          >
            <td class="px-4 py-3 font-medium text-fg">{{ invite.code }}</td>
            <td class="px-4 py-3 text-muted">
              {{ usageText(invite) }}
              <span v-if="isExhausted(invite)" class="ml-1 text-xs text-warning-fg">已失效</span>
            </td>
            <td class="px-4 py-3 text-muted">{{ formatDate(invite.expires_at) }}</td>
            <td class="px-4 py-3 text-muted">{{ invite.note || '—' }}</td>
            <td class="px-4 py-3">
              <button
                type="button"
                class="rounded-control border border-danger-line px-2.5 py-1 text-xs text-danger-fg"
                @click="pendingDelete = invite"
              >
                删除
              </button>
            </td>
          </tr>
          <tr v-if="!invites.length">
            <td colspan="5" class="px-4 py-6 text-center text-sm text-subtle">
              还没有任何邀请码
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div
      v-if="pendingDelete"
      class="rounded-card border border-danger-line bg-danger-surface p-4"
    >
      <p class="text-sm text-danger-fg">
        确定删除邀请码「{{ pendingDelete.code }}」？之后将无法用它注册。
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
