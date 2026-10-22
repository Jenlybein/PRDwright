<script setup lang="ts">
/**
 * 概览页：总量数字 + 按日趋势 + 用户排行。
 *
 * 这一页回答两个不同的问题
 * ------------------------
 * - **卡片**回答「一共多少」：从开天辟地到此刻的累计。
 * - **趋势图**回答「在变好还是变坏」：每天多少、失败率涨没涨。
 *
 * 只有卡片时，你知道一共跑了 10 万次，但看不出「昨天突然掉了」——
 * 而后者才是需要立刻知道的事。
 *
 * 三个刻意的取舍
 * --------------
 * 1. **时区偏移由浏览器传**。后端存 UTC，按 UTC 分组会让东八区的「昨晚」出现在
 *    「今天」那一格。服务端不猜客户端时区（猜错会整体错位，且错得很隐蔽）。
 * 2. **失败率的分母是「已收尾」任务**，且排除主动取消 / 被顶替——它们同样是
 *    `failed` 状态，但成因不是「模型坏了」。这个口径在后端算，前端只显示。
 * 3. **空数据不画空图**。全员为 0 时给一句明确文案：一张什么都没有的图
 *    看起来像「页面坏了」，而不是「这段时间确实没人用」。
 */
import { computed, onMounted, ref, watch } from 'vue'

import {
  fetchDailyStats,
  fetchStats,
  fetchTopUsers,
  messageOf,
  type AdminDailyStat,
  type AdminStats,
  type AdminTopUser,
} from '@/services/adminService'

const stats = ref<AdminStats | null>(null)
const daily = ref<AdminDailyStat[]>([])
/** 区间汇总由后端给：失败率若在前端再算一遍，就成了同一口径的两套实现 */
const rangeSummary = ref<AdminDailyStat | null>(null)
const topUsers = ref<AdminTopUser[]>([])
const loading = ref(true)
const error = ref('')

/** 趋势的天数窗口 */
const days = ref(7)
/** 当前看的指标：生成数 / Token / 失败率 */
const metric = ref<'tasks' | 'tokens' | 'failure'>('tasks')

const METRICS = [
  { key: 'tasks' as const, label: '生成数' },
  { key: 'tokens' as const, label: 'Token' },
  { key: 'failure' as const, label: '失败率' },
]

const DAY_OPTIONS = [7, 14, 30]

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [platform, dailyRows, ranking] = await Promise.all([
      fetchStats(),
      fetchDailyStats(days.value),
      fetchTopUsers(10),
    ])
    stats.value = platform
    daily.value = dailyRows.items
    rangeSummary.value = dailyRows.summary
    topUsers.value = ranking
  } catch (err) {
    error.value = messageOf(err)
  } finally {
    loading.value = false
  }
}

/**
 * 只换天数时**只重拉趋势**：卡片与排行与天数无关，一起拉等于白白多发两个请求，
 * 而用户连点「7 天 / 30 天」时会把这个浪费放大好几倍。
 */
watch(days, async () => {
  try {
    const rows = await fetchDailyStats(days.value)
    daily.value = rows.items
    rangeSummary.value = rows.summary
  } catch (err) {
    error.value = messageOf(err)
  }
})

/** 数字卡片。总数与成功数放在一起，成功率才不用心算 */
const cards = [
  { key: 'user_count' as const, label: '注册用户', hint: '含已停用' },
  { key: 'admin_count' as const, label: '管理员', hint: '可进后台' },
  { key: 'task_count' as const, label: '生成任务', hint: '含失败' },
  { key: 'completed_count' as const, label: '成功完成', hint: '任务已收尾' },
  { key: 'prompt_tokens' as const, label: '输入 Token', hint: '累计' },
  { key: 'completion_tokens' as const, label: '输出 Token', hint: '累计' },
]

/** 这段时间内一条业务数据都没有——用于决定显示空态而不是空图 */
const isEmpty = computed(() =>
  daily.value.every((row) => row.task_count === 0 && row.prompt_tokens === 0),
)

/** 柱状图的一根柱 */
interface Bar {
  day: string
  /** 横轴标签，30 天时只显示一部分，否则会挤成一团 */
  label: string
  /** 柱身总高百分比 0-100 */
  height: number
  /** 柱内分段（自下而上渲染）：用来表达「其中多少是失败」 */
  segments: { cls: string; height: number }[]
  /** 柱顶/悬停的说明 */
  caption: string
  /** 失败率这类可能没有值的指标，用空柱表示 */
  empty: boolean
}

/** 当前指标下每根柱的画法 */
const bars = computed<Bar[]>(() => {
  const rows = daily.value
  // 30 天时每 6 天显示一个标签，避免横轴糊成一片
  const labelStep = rows.length > 14 ? 6 : 1

  if (metric.value === 'tasks') {
    const max = Math.max(1, ...rows.map((r) => r.task_count))
    return rows.map((row, index) => {
      const other = Math.max(
        0,
        row.task_count - row.completed_count - row.failed_count,
      )
      const total = row.task_count
      return {
        day: row.day,
        label: index % labelStep === 0 ? row.day.slice(5) : '',
        height: (total / max) * 100,
        // 顺序即渲染顺序（自上而下）：失败置顶、成功垫底。
        // 成功在底、失败在上，是柱状图的默认读法——「坏的那部分长在最上面」一眼可见。
        segments: total
          ? [
              { cls: 'bg-danger', height: (row.failed_count / total) * 100 },
              {
                cls: 'bg-muted',
                height: (other / total) * 100,
              },
              { cls: 'bg-sky', height: (row.completed_count / total) * 100 },
            ].filter((seg) => seg.height > 0)
          : [],
        caption: `${row.day}：${total} 个（成功 ${row.completed_count}、失败 ${row.failed_count}）`,
        empty: total === 0,
      }
    })
  }

  if (metric.value === 'tokens') {
    const totals = rows.map((r) => r.prompt_tokens + r.completion_tokens)
    const max = Math.max(1, ...totals)
    return rows.map((row, index) => {
      const total = row.prompt_tokens + row.completion_tokens
      return {
        day: row.day,
        label: index % labelStep === 0 ? row.day.slice(5) : '',
        height: (total / max) * 100,
        segments: total ? [{ cls: 'bg-sky', height: 100 }] : [],
        caption: `${row.day}：${total.toLocaleString()} Token`,
        empty: total === 0,
      }
    })
  }

  // 失败率：柱子高度即百分比（0-100 固定刻度，不按最大值缩放）
  return rows.map((row, index) => {
    const rate = row.failure_rate
    return {
      day: row.day,
      label: index % labelStep === 0 ? row.day.slice(5) : '',
      height: rate === null ? 0 : rate * 100,
      segments: rate === null ? [] : [{ cls: 'bg-danger', height: 100 }],
      caption:
        rate === null
          ? `${row.day}：没有任务`
          : `${row.day}：${(rate * 100).toFixed(1)}% 失败（${row.failed_count} / ${row.completed_count + row.failed_count}）`,
      empty: rate === null,
    }
  })
})

/** 区间汇总：直接读后端给的那份，前端不再自己加总 */
const summary = computed(() => {
  const row = rangeSummary.value
  return {
    tasks: row?.task_count ?? 0,
    failed: row?.failed_count ?? 0,
    tokens: (row?.prompt_tokens ?? 0) + (row?.completion_tokens ?? 0),
    // 后端已按「已收尾」为分母算好；null 表示这段区间没有已收尾任务
    rate: row?.failure_rate === null || row?.failure_rate === undefined
      ? null
      : row.failure_rate * 100,
  }
})

/** 排行表里「总 Token」列 */
function totalTokens(row: AdminTopUser): number {
  return row.prompt_tokens + row.completion_tokens
}

function seq(index: number): string {
  return String(index + 1).padStart(2, '0')
}

onMounted(load)
</script>

<template>
  <div class="space-y-5">
    <!-- 页头 -->
    <header class="brut-card-lg p-5">
      <div class="flex flex-wrap items-start justify-between gap-4">
        <div class="min-w-0">
          <p class="kicker">Overview / 平台</p>
          <h1 class="page-title mt-2">平台概览</h1>
        </div>
        <div class="flex flex-wrap items-center gap-2">
          <span class="brut-tag brut-tag-sky">累计口径</span>
          <button type="button" class="brut-btn brut-btn-xs" @click="load()">刷新</button>
        </div>
      </div>

      <div class="mt-4 grid gap-2 border-t-2 border-line pt-3 text-xs text-body sm:grid-cols-2">
        <p>
          Token 统计<b class="text-ink">包含失败的任务</b>——重试恰恰是最烧钱的部分，
          剔掉它们会让成本看起来偏低。
        </p>
        <p>
          失败率<b class="text-ink">不含主动取消与被新任务顶替</b>的那些：它们在库里同样是
          failed，但成因不是模型坏了。
        </p>
      </div>
    </header>

    <p v-if="error" class="brut-note brut-note-fail">{{ error }}</p>

    <div v-else-if="loading" class="brut-card px-4 py-6 text-sm text-subtle">加载中…</div>

    <template v-else>
      <!-- 总量卡片：六格等宽，序号当装饰，数字用等宽体便于竖向比对 -->
      <div v-if="stats" class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        <div
          v-for="(card, index) in cards"
          :key="card.key"
          class="brut-card relative overflow-hidden p-4"
        >
          <span
            class="absolute inset-x-0 top-0 h-1.5"
            :class="index % 3 === 1 ? 'bg-ink' : 'bg-sky'"
          ></span>
          <div class="flex items-baseline justify-between gap-2">
            <p class="kicker">{{ card.label }}</p>
            <span class="tnum text-[10px] text-subtle">{{ seq(index) }}</span>
          </div>
          <p class="tnum mt-2 text-3xl font-black leading-none text-ink">
            {{ stats[card.key].toLocaleString() }}
          </p>
          <p class="mt-1.5 text-xs text-subtle">{{ card.hint }}</p>
        </div>
      </div>

      <!-- 趋势 -->
      <section class="brut-card-lg">
        <header
          class="flex flex-wrap items-center justify-between gap-3 border-b-2 border-brut px-4 py-3"
        >
          <div>
            <p class="kicker">Trend</p>
            <h2 class="mt-1 text-base font-extrabold tracking-tight text-ink">按日趋势</h2>
          </div>

          <div class="flex flex-wrap items-center gap-2">
            <!-- 指标切换 -->
            <div class="brut-seg">
              <button
                v-for="item in METRICS"
                :key="item.key"
                type="button"
                :aria-pressed="metric === item.key"
                @click="metric = item.key"
              >
                {{ item.label }}
              </button>
            </div>

            <!-- 天数切换 -->
            <div class="brut-seg">
              <button
                v-for="option in DAY_OPTIONS"
                :key="option"
                type="button"
                :aria-pressed="days === option"
                @click="days = option"
              >
                {{ option }} 天
              </button>
            </div>
          </div>
        </header>

        <!-- 空数据：给文案，不画一张什么都没有的图 -->
        <div v-if="isEmpty" class="px-4 py-14 text-center">
          <p class="text-sm font-bold text-ink">这段时间还没有生成记录</p>
          <p class="mt-1 text-xs text-subtle">等有人开始用，这里会出现每天的趋势。</p>
        </div>

        <template v-else>
          <!-- 汇总：图看趋势，这一排看总量 -->
          <div class="grid grid-cols-2 gap-px border-b-2 border-line bg-line sm:grid-cols-4">
            <div class="bg-card px-4 py-3">
              <p class="kicker">生成</p>
              <p class="tnum mt-1 text-xl font-black text-ink">
                {{ summary.tasks.toLocaleString() }}
              </p>
            </div>
            <div class="bg-card px-4 py-3">
              <p class="kicker">失败</p>
              <p class="tnum mt-1 text-xl font-black text-ink">
                {{ summary.failed.toLocaleString() }}
              </p>
            </div>
            <div class="bg-card px-4 py-3">
              <p class="kicker">失败率</p>
              <p class="tnum mt-1 text-xl font-black text-ink">
                {{ summary.rate === null ? '—' : summary.rate.toFixed(1) + '%' }}
              </p>
            </div>
            <div class="bg-card px-4 py-3">
              <p class="kicker">Token</p>
              <p class="tnum mt-1 text-xl font-black text-ink">
                {{ summary.tokens.toLocaleString() }}
              </p>
            </div>
          </div>

          <!-- 柱状图：纯 CSS 高度百分比，天然自适应，不必算坐标 -->
          <div class="px-4 pt-5">
            <div
              class="flex h-48 items-end border-b-2 border-brut"
              :class="days > 14 ? 'gap-px' : 'gap-1.5'"
            >
              <div
                v-for="bar in bars"
                :key="bar.day"
                class="group flex h-full flex-1 flex-col justify-end"
                :title="bar.caption"
              >
                <div
                  v-if="!bar.empty"
                  class="flex w-full flex-col justify-end border-2 border-b-0 border-brut"
                  :style="{ height: Math.max(bar.height, 1.5) + '%' }"
                >
                  <!-- 分段自上而下（见 `bars` 里的顺序说明）：失败在顶、成功垫底 -->
                  <div
                    v-for="(seg, index) in bar.segments"
                    :key="index"
                    :class="seg.cls"
                    :style="{ height: seg.height + '%' }"
                  ></div>
                </div>
                <!-- 空柱：画一条 2px 的浅色底，表示「这天有格子但值是 0 / 无」 -->
                <div v-else class="w-full bg-line" style="height: 2px"></div>
              </div>
            </div>

            <!-- 横轴 -->
            <div class="mt-1.5 flex" :class="days > 14 ? 'gap-px' : 'gap-1.5'">
              <div
                v-for="bar in bars"
                :key="bar.day"
                class="flex-1 whitespace-nowrap text-center font-mono text-[10px] text-subtle"
              >
                {{ bar.label }}
              </div>
            </div>
          </div>

          <!-- 图例：不解释颜色，柱子里红蓝各是什么就得靠猜 -->
          <div class="flex flex-wrap gap-4 px-4 py-3">
            <span class="flex items-center gap-1.5 text-[11px] text-muted">
              <span class="h-2.5 w-2.5 border border-brut bg-sky"></span>成功
            </span>
            <span class="flex items-center gap-1.5 text-[11px] text-muted">
              <span class="h-2.5 w-2.5 border border-brut bg-danger"></span>失败
            </span>
            <span class="flex items-center gap-1.5 text-[11px] text-muted">
              <span class="h-2.5 w-2.5 border border-brut bg-muted"></span>未收尾
            </span>
          </div>
        </template>
      </section>

      <!-- 用户排行 -->
      <section class="brut-card-lg">
        <header class="flex flex-wrap items-end justify-between gap-2 border-b-2 border-brut px-4 py-3">
          <div>
            <p class="kicker">Ranking</p>
            <h2 class="mt-1 text-base font-extrabold tracking-tight text-ink">用户用量排行</h2>
          </div>
          <p class="max-w-md text-xs text-subtle">
            只列生成过任务的账号。口径与「用户」页同源——两处各算一遍，迟早会给出两个数。
          </p>
        </header>

        <div class="overflow-x-auto">
          <table class="brut-table min-w-[560px]">
            <thead>
              <tr>
                <th class="w-12">#</th>
                <th>用户</th>
                <th>任务数</th>
                <th>总 Token</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(row, index) in topUsers" :key="row.id">
                <td class="tnum text-xs text-subtle">{{ seq(index) }}</td>
                <td class="font-bold text-ink">{{ row.username }}</td>
                <td class="tnum">{{ row.task_count.toLocaleString() }}</td>
                <td class="tnum">{{ totalTokens(row).toLocaleString() }}</td>
                <td>
                  <span
                    class="brut-tag"
                    :class="row.is_active ? 'brut-tag-ok' : 'brut-tag-danger'"
                  >
                    {{ row.is_active ? '正常' : '已停用' }}
                  </span>
                </td>
              </tr>
              <tr v-if="!topUsers.length">
                <td colspan="5" class="px-4 py-10 text-center text-sm text-subtle">
                  还没有任何用户生成过任务。
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>
  </div>
</template>
