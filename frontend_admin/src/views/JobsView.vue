<script setup lang="ts">
/**
 * 任务监控（v4.5 T5-1）：看现场 + 能干预。
 *
 * 三条交互纪律：
 * 1. **「卡死」由后端判定**。前端只负责把它画红——浏览器与服务器时钟有偏差，
 *    自己算会让「列表说卡死」与「点取消时说还在跑」互相矛盾。
 * 2. **取消要二次确认**。它是不可逆的：文档任务不会保留半截正文。
 * 3. **`cancelled=false` 不是错误**。它表示任务在点击前刚好结束，
 *    页面要说清「不用取消了」，而不是弹一个红色失败。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'

import {
  cancelJob,
  cancelStuckJobs,
  fetchJobs,
  messageOf,
  toResult,
  type AdminJob,
} from '@/services/adminService'

type TabKey = 'running' | 'failed' | 'all'

const TABS: { key: TabKey; label: string }[] = [
  { key: 'running', label: '在飞任务' },
  { key: 'failed', label: '最近失败' },
  { key: 'all', label: '全部' },
]

const KIND_LABELS: Record<string, string> = {
  prd: 'PRD',
  api_docs: 'API 文档',
  prompts: '提示词',
  optimize: '优化',
  chat: '对话',
}

const KIND_OPTIONS = [
  { value: '', label: '全部类型' },
  { value: 'prd', label: 'PRD' },
  { value: 'api_docs', label: 'API 文档' },
  { value: 'prompts', label: '提示词' },
  { value: 'optimize', label: '优化' },
  { value: 'chat', label: '对话' },
]

const tab = ref<TabKey>('running')
const username = ref('')
const kind = ref('')

const jobs = ref<AdminJob[]>([])
const total = ref(0)
const loading = ref(true)
const error = ref('')
const notice = ref('')

/** 展开详情的任务 id（同一时间只展开一条，避免长表格滚不完） */
const expandedId = ref<number | null>(null)
/** 待确认取消的任务；用面板而不是 confirm()，后者没法说明后果 */
const pendingCancel = ref<AdminJob | null>(null)
const cancelling = ref(false)
const bulkCancelling = ref(false)

/** 自动刷新默认关：打开页面先拿一份快照，需要时再开「每 10 秒刷新」 */
const autoRefresh = ref(false)
let timer: ReturnType<typeof setInterval> | null = null

const stuckCount = computed(() => jobs.value.filter((job) => job.is_stuck).length)

const filtered = computed(() => Boolean(username.value.trim() || kind.value))

const emptyText = computed(() => {
  if (filtered.value) return '没有符合筛选条件的任务'
  if (tab.value === 'running') return '当前没有正在运行的任务'
  if (tab.value === 'failed') return '最近没有失败任务'
  return '还没有任何任务'
})

async function load(silent = false) {
  if (!silent) loading.value = true
  error.value = ''
  try {
    const page = await fetchJobs({
      status: tab.value === 'all' ? '' : tab.value,
      username: username.value.trim(),
      kind: kind.value,
      limit: 100,
    })
    jobs.value = page.items
    total.value = page.total
  } catch (err) {
    error.value = messageOf(err)
  } finally {
    if (!silent) loading.value = false
  }
}

onMounted(load)
onUnmounted(stopTimer)

function selectTab(next: TabKey) {
  if (tab.value === next) return
  tab.value = next
  expandedId.value = null
  pendingCancel.value = null
  load()
}

function applyFilters() {
  expandedId.value = null
  load()
}

function resetFilters() {
  username.value = ''
  kind.value = ''
  expandedId.value = null
  load()
}

function toggleDetail(job: AdminJob) {
  expandedId.value = expandedId.value === job.id ? null : job.id
}

function onAutoRefreshChange(event: Event) {
  const checked = (event.target as HTMLInputElement).checked
  autoRefresh.value = checked
  stopTimer()
  if (checked) timer = setInterval(() => void load(true), 10_000)
}

function stopTimer() {
  if (timer !== null) {
    clearInterval(timer)
    timer = null
  }
}

async function onConfirmCancel() {
  const job = pendingCancel.value
  if (!job || cancelling.value) return

  cancelling.value = true
  const result = await toResult(() => cancelJob(job.id))
  cancelling.value = false
  pendingCancel.value = null

  if (!result.ok) {
    sayFail(result.message)
    return
  }
  if (result.value.cancelled) sayOk(`已取消任务 #${job.id}（${job.owner_name}）`)
  else sayOk(`任务 #${job.id} 已经结束了，无需取消`)
  await load(true)
}

async function onCancelStuck() {
  if (bulkCancelling.value) return
  bulkCancelling.value = true
  const result = await toResult(() => cancelStuckJobs())
  bulkCancelling.value = false

  if (!result.ok) {
    sayFail(result.message)
    return
  }
  sayOk(
    result.value.cancelled
      ? `已取消 ${result.value.cancelled} 个卡死任务`
      : '当前没有卡死任务',
  )
  await load(true)
}

function sayOk(text: string) {
  notice.value = text
  error.value = ''
}

/** 显示一条**已经取好**的错误文案（不要再套 `messageOf`，见 UsersView 的同名说明） */
function sayFail(text: string) {
  error.value = text
  notice.value = ''
}

/**
 * 后端存的是**不带时区标记**的 UTC 串（`utc_now_text()`），
 * 直接 `new Date()` 会被浏览器当成本地时间——东八区会整体差 8 小时。
 */
function parseUtc(value: string): Date | null {
  if (!value) return null
  const date = new Date(value.endsWith('Z') ? value : `${value}Z`)
  return Number.isNaN(date.getTime()) ? null : date
}

function formatTime(value: string): string {
  const date = parseUtc(value)
  if (!date) return '—'
  const pad = (n: number) => String(n).padStart(2, '0')
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  )
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`
  const hours = Math.floor(minutes / 60)
  return `${hours}h ${minutes % 60}m`
}

function statusLabel(job: AdminJob): string {
  if (job.status === 'running') {
    return job.is_stuck ? `已卡死 ${job.stuck_minutes} 分钟` : '运行中'
  }
  if (job.status === 'completed') return '已完成'
  if (job.error_code === 'CANCELLED') return '已取消'
  if (job.error_code === 'SUPERSEDED') return '已被替代'
  return '失败'
}

function statusClass(job: AdminJob): string {
  if (job.status === 'running') return job.is_stuck ? 'text-danger-fg' : 'text-success-fg'
  if (job.status === 'completed') return 'text-muted'
  if (job.error_code === 'CANCELLED' || job.error_code === 'SUPERSEDED') return 'text-subtle'
  return 'text-danger-fg'
}

/** 状态小方块。方块比字符「●」更容易和这套硬边视觉对齐 */
function dotClass(job: AdminJob): string {
  if (job.status === 'running') return job.is_stuck ? 'bg-danger' : 'bg-[var(--ui-sky)]'
  if (job.status === 'completed') return 'bg-muted'
  if (job.error_code === 'CANCELLED' || job.error_code === 'SUPERSEDED') return 'bg-line'
  return 'bg-danger'
}

function kindLabel(value: string): string {
  return KIND_LABELS[value] ?? value
}

function totalTokens(job: AdminJob): string {
  return (job.prompt_tokens + job.completion_tokens).toLocaleString()
}
</script>

<template>
  <div class="space-y-5">
    <!-- 页头 + 全局动作 -->
    <header class="brut-card-lg p-5">
      <div class="flex flex-wrap items-start justify-between gap-4">
        <div class="min-w-0">
          <p class="kicker">Jobs / 现场</p>
          <h1 class="page-title mt-2">任务监控</h1>
          <p class="mt-3 max-w-2xl text-sm text-body">
            在飞任务超过 30 分钟没有更新会被标记为卡死。取消是协作式的：任务在下一个可中断点停下。
          </p>
        </div>

        <div class="flex flex-col items-start gap-2 sm:items-end">
          <div class="flex flex-wrap items-center gap-2">
            <button type="button" class="brut-btn brut-btn-xs" @click="load()">刷新</button>
            <label
              class="flex items-center gap-1.5 border-2 border-brut bg-field px-2 py-1 text-xs font-bold text-ink"
            >
              <input
                type="checkbox"
                class="brut-check"
                :checked="autoRefresh"
                @change="onAutoRefreshChange"
              />
              自动刷新（10 秒）
            </label>
            <!-- 只在「在飞」页显示：失败页没有 running 数据，按钮会一直是（0），
                 反而让人以为「没有卡死任务」，而真正该清理的可能在别处 -->
            <button
              v-if="tab === 'running'"
              type="button"
              class="brut-btn brut-btn-xs"
              :class="stuckCount > 0 ? 'brut-btn-danger' : ''"
              :disabled="bulkCancelling || stuckCount === 0"
              @click="onCancelStuck"
            >
              {{ bulkCancelling ? '清理中…' : `取消全部卡死（${stuckCount}）` }}
            </button>
          </div>
          <p class="kicker">卡死判定 = running 且 updated_at 超 30 分钟</p>
        </div>
      </div>
    </header>

    <p v-if="notice" class="brut-note brut-note-ok">{{ notice }}</p>
    <p v-if="error" class="brut-note brut-note-fail">{{ error }}</p>

    <!-- 筛选 + 标签页 -->
    <section class="brut-card">
      <form class="flex flex-wrap items-end gap-3 px-4 py-3" @submit.prevent="applyFilters">
        <label class="block w-40">
          <span class="brut-label">用户名</span>
          <input v-model="username" type="text" class="brut-field" placeholder="模糊匹配" />
        </label>
        <label class="block w-36">
          <span class="brut-label">类型</span>
          <select v-model="kind" class="brut-field">
            <option v-for="option in KIND_OPTIONS" :key="option.value" :value="option.value">
              {{ option.label }}
            </option>
          </select>
        </label>
        <button type="submit" class="brut-btn brut-btn-primary">查询</button>
        <button type="button" class="brut-btn" @click="resetFilters">重置</button>

        <span class="ml-auto self-center font-mono text-xs text-subtle">共 {{ total }} 条</span>
      </form>

      <div class="flex flex-wrap items-center gap-2 border-t-2 border-brut px-4 py-2.5">
        <div class="brut-seg">
          <button
            v-for="item in TABS"
            :key="item.key"
            type="button"
            :aria-pressed="tab === item.key"
            @click="selectTab(item.key)"
          >
            {{ item.label }}
          </button>
        </div>
        <span v-if="stuckCount > 0" class="brut-tag brut-tag-danger">
          {{ stuckCount }} 个卡死
        </span>
      </div>
    </section>

    <!-- 列表 -->
    <section class="brut-card-lg overflow-hidden">
      <div v-if="loading" class="px-4 py-8 text-sm text-subtle">加载中…</div>

      <div v-else class="overflow-x-auto">
        <table class="brut-table min-w-[860px]">
          <thead>
            <tr>
              <th>状态</th>
              <th>用户</th>
              <th>类型</th>
              <th>阶段</th>
              <th>耗时</th>
              <th>Token</th>
              <th>最后更新</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="job in jobs" :key="job.id">
              <tr :class="job.is_stuck ? 'bg-danger-surface' : ''">
                <td>
                  <span class="flex items-center gap-1.5">
                    <span class="inline-block h-2.5 w-2.5 border border-brut" :class="dotClass(job)"></span>
                    <span class="text-xs font-bold" :class="statusClass(job)">
                      {{ statusLabel(job) }}
                    </span>
                  </span>
                </td>
                <td class="font-bold text-ink">{{ job.owner_name }}</td>
                <td>
                  <span class="brut-tag brut-tag-slab">{{ kindLabel(job.kind) }}</span>
                </td>
                <td class="font-mono text-xs">{{ job.phase || '—' }}</td>
                <td class="tnum">{{ formatDuration(job.duration_seconds) }}</td>
                <td class="tnum">{{ totalTokens(job) }}</td>
                <td class="tnum text-xs">{{ formatTime(job.updated_at) }}</td>
                <td>
                  <div class="flex flex-wrap items-center gap-1.5">
                    <button
                      type="button"
                      class="brut-btn brut-btn-xs"
                      @click="toggleDetail(job)"
                    >
                      {{ expandedId === job.id ? '收起' : '详情' }}
                    </button>
                    <button
                      v-if="job.status === 'running'"
                      type="button"
                      class="brut-btn brut-btn-xs border-danger-line text-danger-fg"
                      @click="pendingCancel = job"
                    >
                      取消
                    </button>
                  </div>
                </td>
              </tr>

              <!-- 详情：错误原因 / 时间线 / Token 明细 -->
              <tr v-if="expandedId === job.id" class="bg-slab">
                <td colspan="8" class="px-4 py-3">
                  <dl class="grid gap-x-6 gap-y-2 sm:grid-cols-2">
                    <div class="flex gap-2">
                      <dt class="kicker w-24 shrink-0 pt-0.5">任务 ID</dt>
                      <dd class="tnum text-xs text-ink">#{{ job.id }}</dd>
                    </div>
                    <div class="flex gap-2">
                      <dt class="kicker w-24 shrink-0 pt-0.5">创建时间</dt>
                      <dd class="tnum text-xs text-ink">{{ formatTime(job.created_at) }}</dd>
                    </div>
                    <div class="flex gap-2">
                      <dt class="kicker w-24 shrink-0 pt-0.5">输入 Token</dt>
                      <dd class="tnum text-xs text-ink">{{ job.prompt_tokens.toLocaleString() }}</dd>
                    </div>
                    <div class="flex gap-2">
                      <dt class="kicker w-24 shrink-0 pt-0.5">输出 Token</dt>
                      <dd class="tnum text-xs text-ink">{{ job.completion_tokens.toLocaleString() }}</dd>
                    </div>
                    <div class="flex gap-2">
                      <dt class="kicker w-24 shrink-0 pt-0.5">错误码</dt>
                      <dd class="tnum text-xs text-ink">{{ job.error_code || '—' }}</dd>
                    </div>
                    <div class="flex gap-2">
                      <dt class="kicker w-24 shrink-0 pt-0.5">错误说明</dt>
                      <dd class="break-all text-xs text-ink">{{ job.error_message || '—' }}</dd>
                    </div>
                  </dl>
                </td>
              </tr>
            </template>

            <tr v-if="!jobs.length">
              <td colspan="8" class="px-4 py-10 text-center text-sm text-subtle">
                {{ emptyText }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- 取消确认。用面板而不是 confirm()：要说明「文档任务不保留半截正文」这个后果 -->
    <section v-if="pendingCancel" class="brut-note brut-note-fail border-2 border-danger-line">
      <p class="text-sm font-bold">
        确定取消任务 #{{ pendingCancel.id }}（{{ pendingCancel.owner_name }} ·
        {{ kindLabel(pendingCancel.kind) }}）？
      </p>
      <p class="mt-1 text-xs">
        任务会在下一个可中断点停下；文档任务不会保留半截正文，对话会保留已说出口的部分。
      </p>
      <div class="mt-3 flex items-center gap-2">
        <button
          type="button"
          class="brut-btn brut-btn-danger"
          :disabled="cancelling"
          @click="onConfirmCancel"
        >
          {{ cancelling ? '取消中…' : '确认取消' }}
        </button>
        <button type="button" class="brut-btn" @click="pendingCancel = null">返回</button>
      </div>
    </section>
  </div>
</template>
