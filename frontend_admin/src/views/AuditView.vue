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

import PageHeader from '@/components/PageHeader.vue'

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

/** 危险动作用红标签；其余用中性标签。扫一眼就知道「昨天那次删除」在哪一行 */
function isDestructive(action: string): boolean {
  return action === 'user.delete' || action === 'invite.delete'
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
  <div class="space-y-5">
    <PageHeader kicker="Audit / 留痕" title="操作记录">
      <template #actions>
        <span class="brut-tag brut-tag-slab">{{ items.length }} 条</span>
      </template>
      <template #desc>
        <p>
          管理员的每一次写操作都会留痕。账号被删之后，记录仍在，操作者名字也保留。
        </p>
      </template>
    </PageHeader>

    <p v-if="error" class="brut-note brut-note-fail">{{ error }}</p>

    <section class="brut-card-lg overflow-hidden">
      <div v-if="loading" class="px-4 py-8 text-sm text-subtle">加载中…</div>

      <div v-else class="overflow-x-auto">
        <table class="brut-table min-w-[680px]">
          <thead>
            <tr>
              <th class="w-52">时间</th>
              <th>操作者</th>
              <th>动作</th>
              <th>对象</th>
              <th>说明</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in items" :key="item.id">
              <td class="tnum text-xs">{{ formatTime(item.ts) }}</td>
              <td class="font-bold text-ink">{{ item.actor_name }}</td>
              <td>
                <span
                  class="brut-tag"
                  :class="isDestructive(item.action) ? 'brut-tag-danger' : 'brut-tag-slab'"
                >
                  {{ actionText(item.action) }}
                </span>
              </td>
              <td class="font-mono text-xs">{{ item.target || '—' }}</td>
              <td class="text-xs">{{ item.detail || '—' }}</td>
            </tr>
            <tr v-if="!items.length">
              <td colspan="5" class="px-4 py-10 text-center text-sm text-subtle">
                还没有任何操作记录
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>
