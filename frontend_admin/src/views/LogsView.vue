<script setup lang="ts">
/**
 * 日志页：把 T0 起就在攒的日志变成可用的排障工具。
 *
 * 三条设计取舍
 * ------------
 * 1. **默认只看最近 24 小时**。日志表只增不减，一进来就拉全量等于把整张表搬过来；
 *    而人打开这个页面，九成是想看「刚才那次怎么了」。
 * 2. **级别用颜色区分，且筛选是「及以上」**。选「警告」时 ERROR 也要出来——
 *    人说的「看警告」几乎总是「看有哪些异常」，把 ERROR 挡掉正好挡在最该看的地方。
 * 3. **`request_id` 可点**。排障的主路径是「从别处粘来一个请求号，拉出这次请求的
 *    全部日志」，所以它必须能一键变成筛选条件，而不是让人手抄到输入框里。
 *
 * 详情展开为什么默认折叠
 * ----------------------
 * `extra` 与 `exc` 都很长（traceback 动辄几十行）。全部展开会让一屏只剩两三条，
 * 反而看不清「一共有哪些日志」——先给全貌，想看细节再点开。
 */
import { computed, onMounted, ref } from 'vue'

import AdminModal from '@/components/AdminModal.vue'
import PageHeader from '@/components/PageHeader.vue'

import { fetchLogs, messageOf, type AdminLog, type LogQuery } from '@/services/adminService'

/** 页面上的筛选条件（时间用 `datetime-local` 的原生值，形如 `2026-10-22T10:30`） */
const filters = ref({
  level: '',
  keyword: '',
  loggerName: '',
  event: '',
  requestId: '',
  userId: '',
  /** 时间范围：默认最近 24 小时 */
  since: defaultSince(),
  until: '',
  /** 只看业务事件（默认开）：排掉访问日志，否则一屏全是「请求完成」 */
  businessOnly: true,
})

const items = ref<AdminLog[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(100)
const loading = ref(false)
const error = ref('')
/** 已展开详情的日志 id（只允许一条：同时展开多条会刷屏） */
/** 正在查看那条日志。详情走弹窗，不再往表格里插一行 */
const openedLog = ref<AdminLog | null>(null)

/** 默认时间范围的起点：24 小时前，转成 `datetime-local` 要的本地时间格式 */
function defaultSince(): string {
  const d = new Date(Date.now() - 24 * 60 * 60 * 1000)
  return toLocalInput(d)
}

/** `Date` → `datetime-local` 的 `YYYY-MM-DDTHH:mm`（本地时区，不是 UTC） */
function toLocalInput(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/**
 * `datetime-local` 的值是**本地时间**，而后端存的是 UTC。
 * 不转换的话，东八区用户的「最近 24 小时」会整体偏移 8 小时——
 * 表现为「明明刚发生的日志却查不到」，最难查的一类问题。
 */
function toUtcIso(localValue: string): string {
  if (!localValue) return ''
  const d = new Date(localValue)
  return Number.isNaN(d.getTime()) ? '' : d.toISOString()
}

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)))
const hasPrev = computed(() => page.value > 1)
const hasNext = computed(() => page.value < totalPages.value)

/** 级别 → 文字颜色。未知级别用中性色，不吞掉它（新级别不该在页面上变成空白） */
function levelClass(level: string): string {
  switch (level.toUpperCase()) {
    case 'ERROR':
    case 'CRITICAL':
      return 'text-danger-fg'
    case 'WARNING':
      return 'text-warning-fg'
    case 'DEBUG':
      return 'text-subtle'
    default:
      return 'text-muted'
  }
}

/** 级别 → 行底色。整行着色比只染一个字更容易在一片文字里扫出问题行 */
function rowClass(level: string): string {
  switch (level.toUpperCase()) {
    case 'ERROR':
    case 'CRITICAL':
      return 'bg-danger-surface'
    case 'WARNING':
      return 'bg-warning-surface'
    default:
      return ''
  }
}

/** 级别 → 方块色，配合文字色一起用（只靠文字色在红底行上会糊） */
function dotClass(level: string): string {
  switch (level.toUpperCase()) {
    case 'ERROR':
    case 'CRITICAL':
      return 'bg-danger'
    case 'WARNING':
      return 'bg-[var(--ui-warning-line)]'
    default:
      return 'bg-muted'
  }
}

/**
 * 时间显示到毫秒，且**转成本地时区**。
 *
 * ⚠️ 后端存的是 UTC（`...Z`），直接截字符串会让东八区用户看到 8 小时前的时刻——
 * 表现为「明明刚发生的日志，时间却是昨天下午」，而这正是排查时最先看的一列。
 * 所以必须 `new Date()` 解析后按本地时区重排，不能对 ISO 串做字符串裁剪。
 */
function formatTime(value: string): string {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  const pad = (n: number) => String(n).padStart(2, '0')
  const date = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  // 毫秒单独补：同一秒内先后发生的两条日志，靠它才分得清顺序
  return `${date} ${time}.${String(d.getMilliseconds()).padStart(3, '0')}`
}

function buildQuery(): LogQuery {
  return {
    level: filters.value.level,
    keyword: filters.value.keyword,
    logger_name: filters.value.loggerName,
    event: filters.value.event,
    request_id: filters.value.requestId,
    user_id: filters.value.userId ? Number(filters.value.userId) : undefined,
    since: toUtcIso(filters.value.since),
    until: toUtcIso(filters.value.until),
    // 只看业务事件时排掉访问日志：它每个请求一条，会把真正的生成日志挤出视野
    exclude_event: filters.value.businessOnly ? 'request.completed' : '',
    page: page.value,
    page_size: pageSize.value,
  }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await fetchLogs(buildQuery())
    items.value = data.items
    total.value = data.total
    page.value = data.page
    pageSize.value = data.page_size
  } catch (err) {
    error.value = messageOf(err)
    items.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

/** 改条件后回到第 1 页：停在第 3 页却换了筛选条件，常常直接落到空页 */
function applyFilters() {
  page.value = 1
  openedLog.value = null
  load()
}

/** 清空全部条件（含时间），回到「默认最近 24 小时」 */
function resetFilters() {
  filters.value = {
    level: '',
    keyword: '',
    loggerName: '',
    event: '',
    requestId: '',
    userId: '',
    since: defaultSince(),
    until: '',
    businessOnly: true,
  }
  applyFilters()
}

/**
 * 一键按某个 `request_id` 反查——排障主路径。
 * 同时清掉其它条件：从「某次请求」出发时，别处残留的级别/关键词会悄悄把行筛掉，
 * 表现为「这个请求只查到一条」，而实际有很多条。
 */
function drillIntoRequest(requestId: string) {
  filters.value = {
    level: '',
    keyword: '',
    loggerName: '',
    event: '',
    requestId,
    userId: '',
    since: '',
    until: '',
    // 按请求号反查时**不排**访问日志：那一条正是「这个请求有没有到过后端」的证据
    businessOnly: false,
  }
  applyFilters()
}

/** `extra` 转成可读的多行文本；没有内容时给空串，模板里据此隐藏该块 */
function extraText(extra: Record<string, unknown>): string {
  const keys = Object.keys(extra)
  if (!keys.length) return ''
  return keys.map((key) => `${key}: ${JSON.stringify(extra[key])}`).join('\n')
}

function goto(next: number) {
  const target = Math.min(Math.max(1, next), totalPages.value)
  if (target === page.value) return
  page.value = target
  openedLog.value = null
  load()
}

onMounted(load)
</script>

<template>
  <div class="space-y-5">
    <!-- 说明条：告诉人这个页面和「操作记录」不是一回事 -->
    <PageHeader kicker="Logs / 排障" title="系统日志">
      <template #actions>
        <span class="brut-tag brut-tag-sky">共 {{ total }} 条</span>
      </template>
      <template #desc>
        <p>
          系统在说什么（排障用）。人为操作留下的痕迹在「操作记录」页，两者刻意分开——
          混在一起时，排查故障会被大量人为操作淹没。
        </p>
      </template>
    </PageHeader>

    <!-- 过滤条 -->
    <section class="brut-card">
      <header class="flex items-center justify-between border-b-2 border-brut px-4 py-2">
        <h2 class="text-sm font-extrabold tracking-tight text-ink">筛选</h2>
        <span class="kicker">默认最近 24 小时</span>
      </header>

      <div class="grid grid-cols-1 gap-3 px-4 py-3 sm:grid-cols-2 lg:grid-cols-4">
        <label class="block">
          <span class="brut-label">级别（含更高级）</span>
          <select v-model="filters.level" class="brut-field" @change="applyFilters">
            <option value="">全部</option>
            <option value="DEBUG">DEBUG</option>
            <option value="INFO">INFO</option>
            <option value="WARNING">WARNING</option>
            <option value="ERROR">ERROR</option>
            <option value="CRITICAL">CRITICAL</option>
          </select>
        </label>

        <label class="block">
          <span class="brut-label">关键词（消息）</span>
          <input
            v-model="filters.keyword"
            type="text"
            placeholder="如：生成失败"
            class="brut-field"
            @keyup.enter="applyFilters"
          />
        </label>

        <label class="block">
          <span class="brut-label">事件名</span>
          <input
            v-model="filters.event"
            type="text"
            placeholder="如：job.failed"
            class="brut-field font-mono"
            @keyup.enter="applyFilters"
          />
        </label>

        <label class="block">
          <span class="brut-label">来源 logger（前缀）</span>
          <input
            v-model="filters.loggerName"
            type="text"
            placeholder="如：backend.services"
            class="brut-field font-mono"
            @keyup.enter="applyFilters"
          />
        </label>

        <label class="block">
          <span class="brut-label">请求号（排障主路径）</span>
          <input
            v-model="filters.requestId"
            type="text"
            placeholder="粘贴一个 request_id"
            class="brut-field font-mono"
            @keyup.enter="applyFilters"
          />
        </label>

        <label class="block">
          <span class="brut-label">用户 ID</span>
          <input
            v-model="filters.userId"
            type="number"
            placeholder="如：3"
            class="brut-field"
            @keyup.enter="applyFilters"
          />
        </label>

        <label class="block">
          <span class="brut-label">起始时间（本地时区）</span>
          <input v-model="filters.since" type="datetime-local" class="brut-field" />
        </label>

        <label class="block">
          <span class="brut-label">结束时间（留空 = 不限）</span>
          <input v-model="filters.until" type="datetime-local" class="brut-field" />
        </label>
      </div>

      <div class="flex flex-wrap items-center gap-1.5 border-t-2 border-brut px-4 py-2.5">
        <button type="button" class="brut-btn brut-btn-primary brut-btn-xs" @click="applyFilters">
          查询
        </button>
        <button type="button" class="brut-btn brut-btn-xs" @click="resetFilters">重置</button>
        <!-- 默认开：访问日志每个请求一条，不排掉的话一屏全是「请求完成」 -->
        <label
          class="ml-1 flex items-center gap-1.5 border-2 border-brut bg-field px-2 py-1 text-xs font-bold text-ink"
        >
          <input
            v-model="filters.businessOnly"
            type="checkbox"
            class="brut-check"
            @change="applyFilters"
          />
          只看业务事件
        </label>
        <span class="ml-auto text-xs text-subtle">点任意一行查看结构化字段与堆栈</span>
      </div>
    </section>

    <p v-if="error" class="brut-note brut-note-fail">{{ error }}</p>

    <!-- 结果表 -->
    <section class="brut-card-lg overflow-hidden">
      <div v-if="loading" class="px-4 py-8 text-sm text-subtle">加载中…</div>

      <template v-else>
        <div class="overflow-x-auto">
          <table class="brut-table min-w-[820px]">
            <thead>
              <tr>
                <th class="w-52">时间</th>
                <th class="w-24">级别</th>
                <th>消息</th>
                <th class="w-36">事件</th>
                <th class="w-32">请求号</th>
              </tr>
            </thead>
            <tbody>
              <template v-for="item in items" :key="item.id">
                <tr
                  class="cursor-pointer"
                  :class="rowClass(item.level)"
                  @click="openedLog = item"
                >
                  <td class="tnum text-xs">{{ formatTime(item.ts) }}</td>
                  <td>
                    <span class="flex items-center gap-1.5">
                      <span
                        class="inline-block h-2 w-2 border border-brut"
                        :class="dotClass(item.level)"
                      ></span>
                      <span class="font-mono text-[11px] font-bold" :class="levelClass(item.level)">
                        {{ item.level }}
                      </span>
                    </span>
                  </td>
                  <td class="font-medium text-ink">{{ item.msg }}</td>
                  <td class="font-mono text-xs">{{ item.event || '—' }}</td>
                  <td class="font-mono text-xs">
                    <!-- 点它 = 按这个请求号反查，是最常用的一步，所以做成可点的 -->
                    <button
                      v-if="item.request_id"
                      type="button"
                      class="underline decoration-2 underline-offset-2 hover:text-sky-deep"
                      @click.stop="drillIntoRequest(item.request_id)"
                    >
                      {{ item.request_id.slice(0, 8) }}…
                    </button>
                    <span v-else class="text-subtle">—</span>
                  </td>
                </tr>

              </template>

              <tr v-if="!items.length">
                <td colspan="5" class="px-4 py-12 text-center text-sm text-subtle">
                  这段时间内没有匹配的日志。试试放宽时间范围或清掉筛选条件。
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- 分页 -->
        <div
          v-if="total > pageSize"
          class="flex items-center justify-between border-t-2 border-brut px-4 py-2.5 text-sm"
        >
          <span class="tnum text-xs text-muted">第 {{ page }} / {{ totalPages }} 页</span>
          <div class="flex gap-2">
            <button
              type="button"
              class="brut-btn brut-btn-xs"
              :disabled="!hasPrev"
              @click="goto(page - 1)"
            >
              上一页
            </button>
            <button
              type="button"
              class="brut-btn brut-btn-xs"
              :disabled="!hasNext"
              @click="goto(page + 1)"
            >
              下一页
            </button>
          </div>
        </div>
      </template>
    </section>

    <!-- 详情：结构化字段与 traceback。弹窗承载，不插行——日志行本来就密，
         中间插一行十几行的详情会把「相邻几条日志」彻底打散。 -->
    <AdminModal
      :open="Boolean(openedLog)"
      kicker="Log Detail"
      :title="openedLog?.msg ?? ''"
      size="md"
      @close="openedLog = null"
    >
      <template #subtitle>
        <p v-if="openedLog" class="mt-1 flex flex-wrap items-center gap-2 text-xs">
          <span class="brut-tag" :class="openedLog.level === 'ERROR' ? 'brut-tag-danger' : 'brut-tag-slab'">
            {{ openedLog.level }}
          </span>
          <span class="tnum text-subtle">{{ formatTime(openedLog.ts) }}</span>
        </p>
      </template>

      <dl v-if="openedLog" class="grid gap-3 sm:grid-cols-2">
        <div>
          <dt class="kicker">来源</dt>
          <dd class="mt-1 font-mono text-xs text-ink">{{ openedLog.logger }}</dd>
        </div>
        <div>
          <dt class="kicker">完整请求号</dt>
          <dd class="mt-1 font-mono text-xs text-ink">
            {{ openedLog.request_id || '—（无请求来源）' }}
          </dd>
        </div>
        <div>
          <dt class="kicker">归属用户</dt>
          <dd class="mt-1 font-mono text-xs text-ink">{{ openedLog.user_id ?? '—' }}</dd>
        </div>
        <div>
          <dt class="kicker">事件</dt>
          <dd class="mt-1 font-mono text-xs text-ink">{{ openedLog.event || '—' }}</dd>
        </div>
        <div v-if="extraText(openedLog.extra)" class="sm:col-span-2">
          <dt class="kicker">业务字段</dt>
          <dd>
            <pre
              class="mt-1 overflow-x-auto border-2 border-brut bg-field p-2 font-mono text-xs text-body"
            >{{ extraText(openedLog.extra) }}</pre>
          </dd>
        </div>
        <div v-if="openedLog.exc" class="sm:col-span-2">
          <dt class="kicker">异常堆栈</dt>
          <dd>
            <pre
              class="mt-1 max-h-72 overflow-auto border-2 border-danger-line bg-field p-2 font-mono text-xs text-danger-fg"
            >{{ openedLog.exc }}</pre>
          </dd>
        </div>
      </dl>

      <template #footer>
        <button
          v-if="openedLog?.request_id"
          type="button"
          class="brut-btn brut-btn-xs"
          @click="openedLog && drillIntoRequest(openedLog.request_id); openedLog = null"
        >
          按此请求号反查
        </button>
        <button type="button" class="brut-btn" @click="openedLog = null">关闭</button>
      </template>
    </AdminModal>
  </div>
</template>
