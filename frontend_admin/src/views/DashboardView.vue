<script setup lang="ts">
/**
 * 概览页（占位）。
 *
 * 本阶段的目标只是「管理员能进来」，所以这里先放一张说明卡，
 * 把后续几块（用户 / 邀请码 / 统计 / 配置 / 日志）的进度讲清楚，
 * 而不是做一个没有数据支撑的空壳仪表盘——空图表比没有图表更容易误导。
 */
import { useSessionStore } from '@/stores/session'

const session = useSessionStore()

/** 后续阶段要接入的模块。T1–T4 逐个替换成真实页面 */
const roadmapItems = [
  { stage: 'T1', name: '用户管理', desc: '账号增删、启停、重置密码' },
  { stage: 'T1', name: '邀请码管理', desc: '多个邀请码的发放、停用与用量' },
  { stage: 'T2', name: '平台统计', desc: 'PRD 生成总量与用户用量排行' },
  { stage: 'T3', name: '系统配置', desc: '配额、超时重试、模型供应商切换' },
  { stage: 'T4', name: '日志查看', desc: '按级别 / 时间 / 请求号检索与实时尾随' },
]
</script>

<template>
  <div class="space-y-4">
    <div class="rounded-card border border-line bg-surface p-5 shadow-card">
      <h1 class="text-base font-semibold text-fg">平台概览</h1>
      <p class="mt-1 text-sm text-muted">
        地基已就绪：管理员身份与服务端守卫生效，日志已开始记录。
        下列模块将在后续阶段接入。
      </p>
      <p v-if="session.username" class="mt-2 text-sm text-body">
        当前管理员：<span class="font-medium text-fg">{{ session.username }}</span>
      </p>
    </div>

    <ul class="grid gap-3 sm:grid-cols-2">
      <li
        v-for="item in roadmapItems"
        :key="item.name"
        class="rounded-card border border-line bg-surface p-4"
      >
        <div class="flex items-baseline gap-2">
          <span class="text-sm font-medium text-fg">{{ item.name }}</span>
          <span class="rounded bg-surface-2 px-1.5 py-0.5 text-xs text-subtle">{{ item.stage }}</span>
        </div>
        <p class="mt-1 text-sm text-muted">{{ item.desc }}</p>
      </li>
    </ul>
  </div>
</template>
