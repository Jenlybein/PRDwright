<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import AppButton from '@/components/ui/AppButton.vue'
import AppBadge from '@/components/ui/AppBadge.vue'
import type { DocumentVersionSummary } from '@/types'
import { relativeTime } from '@/utils/time'
import { versionSourceLabel } from '@/utils/versions'

/**
 * 文档版本抽屉（从右侧滑出，默认收起）。
 *
 * ## 为什么是抽屉而不是常驻窄栏
 *
 * 中部的正文是固定 `768px` 居中、底部还有一条横跨整窗的吸底操作栏。右侧插一条常驻窄栏
 * 会让正文居中失衡、底栏横向铺法也要跟着改。抽屉默认收起，打开时只是**浮在正文之上的一层**，
 * 对现有布局零打扰。
 *
 * ## 它是「内容区的一部分」，不是第二处流程落点
 *
 * 全站唯一操作落点仍是底栏（ADR-015）。本抽屉里只有**内容操作**（存为新版本 / 恢复某一版），
 * 永远不出现流程动作（生成下一份 / 完成 / 返回）；进入抽屉的入口也在底栏的内容行。
 * 它以 `--shell-action-bar-h` 作为下边距，因此**不会遮住底栏**（见 ADR-030）。
 *
 * ## 本组件是纯展示
 *
 * 不 import 任何 store：版本列表、加载态、错误都由上层传入，动作以事件抛出去。
 * 「要存的内容是哪一版」也由上层提供（取自 `documents` 的工作副本），
 * 组件只负责把备注收上来。
 */
const props = defineProps<{
  /** 是否展开 */
  open: boolean
  /** 文档展示名，用于标题与无障碍标注 */
  title: string
  /** 版本摘要列表（新→旧），由上层拉取后传入 */
  versions: DocumentVersionSummary[]
  /** 是否正在拉取 */
  loading?: boolean
  /** 拉取 / 写入失败的原因 */
  error?: string
  /** 上层是否有进行中的动作（生成 / 优化中），置禁按钮 */
  busy?: boolean
}>()

const emit = defineEmits<{
  /** 关闭抽屉 */
  close: []
  /** 把当前正文存为新版本；载荷是用户填的备注（可为空） */
  checkpoint: [note: string]
  /** 恢复某个历史版本 */
  restore: [versionId: number]
}>()

/** 「存为新版本」的备注输入 */
const note = ref('')

// 每次打开都清空备注：上一次填的内容不该顺延到下一次存档
watch(
  () => props.open,
  (open) => {
    if (open) note.value = ''
  },
)

/** 「存为新版本」是否可点：有备注或没备注都能存，但不能在上层忙的时候重复点 */
const canCheckpoint = computed(() => !props.busy)

/** 提交存档：备注可选，正文由上层提供 */
function submitCheckpoint(): void {
  if (!canCheckpoint.value) return
  emit('checkpoint', note.value.trim())
}

/** Esc 关闭：抽屉浮在正文之上，键盘也该能退出这一层 */
function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape' && props.open) emit('close')
}

onMounted(() => window.addEventListener('keydown', onKeydown))
onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))
</script>

<template>
  <!--
    抽屉本体：`fixed` 脱离文档流（不挤中部布局），右侧贴边、上下留出底栏空间。
    下边距取 `--shell-action-bar-h`（底栏实时高度），因此它**永远不遮底栏**。
    z 序与底栏同档但彼此不重叠，视觉上互不干扰。
  -->
  <aside
    class="shell-version-drawer fixed right-0 top-0 z-30 flex w-[360px] max-w-[85vw] flex-col border-l border-line bg-surface shadow-xl transition-transform duration-300 ease-out"
    style="bottom: var(--shell-action-bar-h, 0px)"
    :class="open ? 'translate-x-0' : 'translate-x-full'"
    :aria-hidden="!open"
    :aria-label="`${title} 的版本历史`"
  >
    <!-- 头部：标题 + 关闭 -->
    <header class="flex items-center justify-between gap-2 border-b border-line px-4 py-3">
      <div class="min-w-0">
        <h2 class="truncate text-sm font-semibold text-fg">版本历史</h2>
        <p class="truncate text-[11px] text-subtle">{{ title }}</p>
      </div>
      <button
        type="button"
        class="shrink-0 rounded-control p-1 text-subtle transition-colors hover:bg-surface-2 hover:text-muted"
        aria-label="关闭版本历史"
        @click="emit('close')"
      >
        <svg class="h-4 w-4" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
          <path
            d="M6.28 5.22a.75.75 0 0 0-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 1 0 1.06 1.06L10 11.06l3.72 3.72a.75.75 0 1 0 1.06-1.06L11.06 10l3.72-3.72a.75.75 0 0 0-1.06-1.06L10 8.94 6.28 5.22Z"
          />
        </svg>
      </button>
    </header>

    <!-- 存为新版本：把用户眼前的正文留档，备注可有可无 -->
    <div class="space-y-2 border-b border-line bg-surface-2/60 px-4 py-3">
      <p class="text-[11px] text-subtle">
        把当前正文存为一个新版本留档；之后可以随时恢复回这一版。
      </p>
      <div class="flex items-center gap-2">
        <input
          v-model="note"
          type="text"
          maxlength="200"
          class="min-w-0 flex-1 rounded-control border border-line-strong bg-field px-2.5 py-1.5 text-xs text-fg outline-none transition-colors placeholder:text-subtle focus:border-accent focus:ring-2 focus:ring-accent-soft-line"
          placeholder="备注（可选）"
          @keydown.enter.prevent="submitCheckpoint"
        />
        <AppButton variant="primary" size="sm" :disabled="!canCheckpoint" @click="submitCheckpoint">
          存为新版本
        </AppButton>
      </div>
    </div>

    <!-- 列表：新→旧。每项给出「第几版 · 怎么来的 · 什么时候」，当前版本不可恢复（点了也是原地不动） -->
    <div class="flex-1 overflow-y-auto px-4 py-3">
      <p v-if="loading" class="py-6 text-center text-xs text-subtle">正在读取版本历史…</p>

      <p v-else-if="error" class="rounded-control bg-danger-surface px-3 py-2 text-xs text-danger-fg">
        {{ error }}
      </p>

      <p v-else-if="versions.length === 0" class="py-6 text-center text-xs text-subtle">
        还没有任何版本。生成或保存一次就会产生第一版。
      </p>

      <ul v-else class="space-y-2">
        <li
          v-for="version in versions"
          :key="version.versionId"
          class="rounded-card border border-line bg-surface p-3"
          :class="version.isCurrent ? 'ring-1 ring-accent-soft-line' : ''"
        >
          <div class="flex items-center justify-between gap-2">
            <div class="flex min-w-0 flex-wrap items-center gap-1.5">
              <span class="text-xs font-semibold text-fg">v{{ version.versionNo }}</span>
              <AppBadge :variant="version.isCurrent ? 'primary' : 'neutral'">
                {{ versionSourceLabel(version.source) }}
              </AppBadge>
              <AppBadge v-if="version.isCurrent" variant="success">当前</AppBadge>
            </div>
            <span class="shrink-0 text-[11px] text-subtle">{{ relativeTime(version.createdAt) }}</span>
          </div>

          <p v-if="version.note" class="mt-1.5 text-[11px] text-muted">{{ version.note }}</p>

          <div class="mt-2 flex items-center justify-between gap-2">
            <span class="text-[11px] text-subtle">约 {{ version.contentLength }} 字</span>
            <AppButton
              v-if="!version.isCurrent"
              variant="secondary"
              size="sm"
              :disabled="busy"
              @click="emit('restore', version.versionId)"
            >
              恢复此版本
            </AppButton>
          </div>
        </li>
      </ul>
    </div>
  </aside>
</template>
