<script setup lang="ts">
/**
 * 概览页：平台总量。
 *
 * 这里只放**后端已经算得出来**的数字。空有图表的仪表盘比没有图表更误导——
 * 在 T2（平台统计）之前，趋势图没有数据源，所以这一版只有总量卡片。
 */
import { onMounted, ref } from 'vue'

import { fetchStats, messageOf, type AdminStats } from '@/services/adminService'

const stats = ref<AdminStats | null>(null)
const loading = ref(true)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    stats.value = await fetchStats()
  } catch (err) {
    error.value = messageOf(err)
  } finally {
    loading.value = false
  }
}

onMounted(load)

/** 数字卡片。总数与成功数放在一起，成功率才不用心算 */
const cards = [
  { key: 'user_count' as const, label: '注册用户', hint: '含已停用' },
  { key: 'admin_count' as const, label: '管理员', hint: '可进后台' },
  { key: 'task_count' as const, label: '生成任务', hint: '含失败' },
  { key: 'completed_count' as const, label: '成功完成', hint: '任务已收尾' },
  { key: 'prompt_tokens' as const, label: '输入 Token', hint: '累计' },
  { key: 'completion_tokens' as const, label: '输出 Token', hint: '累计' },
]
</script>

<template>
  <div class="space-y-4">
    <div class="rounded-card border border-line bg-surface p-5 shadow-card">
      <h1 class="text-base font-semibold text-fg">平台概览</h1>
      <p class="mt-1 text-sm text-muted">
        Token 统计包含失败的任务——重试恰恰是最烧钱的部分，剔掉它们会让成本看起来偏低。
      </p>
    </div>

    <p v-if="error" class="rounded-card border border-danger-line bg-danger-surface px-4 py-3 text-sm text-danger-fg">
      {{ error }}
    </p>

    <div v-else-if="loading" class="text-sm text-subtle">加载中…</div>

    <div v-else-if="stats" class="grid gap-3 sm:grid-cols-3">
      <div
        v-for="card in cards"
        :key="card.key"
        class="rounded-card border border-line bg-surface p-4"
      >
        <div class="text-xs text-subtle">{{ card.label }}</div>
        <div class="mt-1 text-2xl font-semibold text-fg">
          {{ stats[card.key].toLocaleString() }}
        </div>
        <div class="mt-0.5 text-xs text-subtle">{{ card.hint }}</div>
      </div>
    </div>
  </div>
</template>
