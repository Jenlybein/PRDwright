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

import AdminModal from '@/components/AdminModal.vue'
import PageHeader from '@/components/PageHeader.vue'

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
/** 新建弹窗是否打开。与「提交中」分开：提交时弹窗要留着，成功后才收 */
const formOpen = ref(false)
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

function openCreate() {
  form.value = { code: '', max_uses: 0, expires_at: '', note: '' }
  formOpen.value = true
}

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
  formOpen.value = false
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
  <div class="space-y-5">
    <PageHeader kicker="Invites / 准入门票" title="邀请码管理">
      <template #actions>
        <span class="brut-tag brut-tag-sky">{{ invites.length }} 个码</span>
        <button type="button" class="brut-btn brut-btn-xs brut-btn-primary" @click="openCreate">
          + 新建邀请码
        </button>
      </template>
      <template #desc>
        <p>注册需要邀请码。删除一个码只影响之后的注册，已经注册的用户不受影响。</p>
      </template>
    </PageHeader>

    <p v-if="notice" class="brut-note brut-note-ok">{{ notice }}</p>
    <p v-if="error" class="brut-note brut-note-fail">{{ error }}</p>

    <!-- 新建邀请码：弹窗而不是常驻卡片。
         常驻表单会一直占着垂直空间，而建码是低频动作——多数时候它只是把列表往下推。 -->
    <AdminModal
      :open="formOpen"
      kicker="Create"
      title="新建邀请码"
      size="sm"
      @close="formOpen = false"
    >
      <form class="space-y-3" @submit.prevent="onCreate">
        <label class="block">
          <span class="brut-label">邀请码</span>
          <input
            v-model="form.code"
            type="text"
            required
            minlength="4"
            maxlength="32"
            class="brut-field font-mono tracking-widest uppercase"
            placeholder="如 ALPHA2"
          />
          <span class="mt-1 block text-xs text-subtle">
            只允许大写字母与数字，且不含 O/0、I/1 等易混字符——码是要被人手抄的。
          </span>
        </label>
        <label class="block">
          <span class="brut-label">次数上限（0 = 不限）</span>
          <input v-model.number="form.max_uses" type="number" min="0" class="brut-field" />
        </label>
        <label class="block">
          <span class="brut-label">过期时间</span>
          <input v-model="form.expires_at" type="date" class="brut-field" />
        </label>
        <label class="block">
          <span class="brut-label">备注</span>
          <input
            v-model="form.note"
            type="text"
            maxlength="64"
            class="brut-field"
            placeholder="给谁用"
          />
        </label>

        <div class="flex items-center gap-2 pt-1">
          <button type="submit" :disabled="creating" class="brut-btn brut-btn-primary">
            {{ creating ? '创建中…' : '新建邀请码' }}
          </button>
          <button type="button" class="brut-btn" @click="formOpen = false">取消</button>
        </div>
      </form>
    </AdminModal>

    <!-- 列表 -->
    <section class="brut-card-lg overflow-hidden">
      <div v-if="loading" class="px-4 py-8 text-sm text-subtle">加载中…</div>

      <div v-else class="overflow-x-auto">
        <table class="brut-table min-w-[560px]">
          <thead>
            <tr>
              <th>邀请码</th>
              <th>已用 / 上限</th>
              <th>过期时间</th>
              <th>备注</th>
              <th>状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="invite in invites" :key="invite.code">
              <td>
                <span class="font-mono text-sm font-bold tracking-widest text-ink">
                  {{ invite.code }}
                </span>
              </td>
              <td class="tnum">{{ usageText(invite) }}</td>
              <td class="tnum text-xs">{{ formatDate(invite.expires_at) }}</td>
              <td>{{ invite.note || '—' }}</td>
              <td>
                <span
                  class="brut-tag"
                  :class="isExhausted(invite) ? 'brut-tag-warn' : 'brut-tag-ok'"
                >
                  {{ isExhausted(invite) ? '已失效' : '可用' }}
                </span>
              </td>
              <td>
                <button
                  type="button"
                  class="brut-btn brut-btn-xs border-danger-line text-danger-fg"
                  @click="pendingDelete = invite"
                >
                  删除
                </button>
              </td>
            </tr>
            <tr v-if="!invites.length">
              <td colspan="6" class="px-4 py-10 text-center text-sm text-subtle">
                还没有任何邀请码
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- 删除确认：弹窗。之前是页面底部一块内联提示，展开会把列表往下顶 -->
    <AdminModal
      :open="Boolean(pendingDelete)"
      kicker="Delete"
      :title="`删除邀请码「${pendingDelete?.code ?? ''}」`"
      size="sm"
      tone="danger"
      @close="pendingDelete = null"
    >
      <p class="text-sm text-body">
        之后将无法用它注册。已经用它注册的用户不受影响——他们与码之间没有关联。
      </p>

      <template #footer>
        <button
          type="button"
          class="brut-btn brut-btn-danger"
          :disabled="!pendingDelete"
          @click="pendingDelete && onDelete(pendingDelete)"
        >
          确认删除
        </button>
        <button type="button" class="brut-btn" @click="pendingDelete = null">取消</button>
      </template>
    </AdminModal>
  </div>
</template>
