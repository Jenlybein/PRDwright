<script setup lang="ts">
/**
 * 审计记录：管理员做过的每一件事。
 *
 * 这个页面的价值在**出事后**才体现出来——「这个账号是谁建的」「那条邀请码是谁发的」
 * 「昨天那次删除是谁点的」。所以它显示的是**不可篡改的既成事实**：
 *
 * 1. **操作者名字是冗余保存的**。账号被删之后这条记录仍在，且仍显示当时的名字。
 *    如果这里改成「实时查用户表」，删号一执行，这条记录就变成空白——
 *    而「谁删的」恰恰是最该被记住的那条。
 * 2. **密码不写进来**。审计表是给人翻的，不是保险箱；后端在记的时候就已经
 *    只记「已重置」而不记新密码。
 */
import { onMounted, ref } from 'vue'

import { fetchAudit, messageOf, type AdminAudit } from '@/services/adminService'

const items = ref<AdminAudit[]>([])
const loading = ref(true)
const error = ref('')

/** 动作码 → 人话。未知动作原样显示，不吞掉——新加的动作不该在页面上变成空白 */
const ACTION_LABELS: Record<string, string> = {
  'user.create': '新建账号',
  'user.active': '启停账号',
  'user.admin': '管理员身份变更',
  'user.password': '重置密码',
  'user.delete': '删除账号',
  'invite.create': '新建邀请码',
  'invite.delete': '删除邀请码',
}

function actionText(action: string): string {
  return ACTION_LABELS[action] ?? action
}

/** 时间显示到秒：审计要能对上具体是哪一刻的操作 */
function formatTime(value: string): string {
  return value ? value.replace('T', ' ').slice(0, 19) : '—'
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    items.value = await fetchAudit()
  } catch (err) {
    error.value = messageOf(err)
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="space-y-4">
    <div class="rounded-card border border-line bg-surface p-5 shadow-card">
      <h1 class="text-base font-semibold text-fg">操作记录</h1>
      <p class="mt-1 text-sm text-muted">
        管理员的每一次写操作都会留痕。账号被删之后，记录仍在，操作者名字也保留。
      </p>
    </div>

    <p v-if="error" class="rounded-card border border-danger-line bg-danger-surface px-4 py-3 text-sm text-danger-fg">
      {{ error }}
    </p>

    <div class="rounded-card border border-line bg-surface">
      <div v-if="loading" class="px-4 py-6 text-sm text-subtle">加载中…</div>

      <table v-else class="w-full text-sm">
        <thead class="border-b border-line text-left text-xs text-subtle">
          <tr>
            <th class="px-4 py-2 font-medium">时间</th>
            <th class="px-4 py-2 font-medium">操作者</th>
            <th class="px-4 py-2 font-medium">动作</th>
            <th class="px-4 py-2 font-medium">对象</th>
            <th class="px-4 py-2 font-medium">说明</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="item in items"
            :key="item.id"
            class="border-b border-line last:border-0"
          >
            <td class="px-4 py-3 text-muted">{{ formatTime(item.ts) }}</td>
            <td class="px-4 py-3 font-medium text-fg">{{ item.actor_name }}</td>
            <td class="px-4 py-3 text-muted">{{ actionText(item.action) }}</td>
            <td class="px-4 py-3 text-muted">{{ item.target || '—' }}</td>
            <td class="px-4 py-3 text-muted">{{ item.detail || '—' }}</td>
          </tr>
          <tr v-if="!items.length">
            <td colspan="5" class="px-4 py-6 text-center text-sm text-subtle">
              还没有任何操作记录
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
