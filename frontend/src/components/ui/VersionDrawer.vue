<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import AppButton from '@/components/ui/AppButton.vue'
import AppBadge from '@/components/ui/AppBadge.vue'
import type { DocumentVersionDetail, DocumentVersionSummary } from '@/types'
import { renderMarkdown } from '@/utils/markdown'
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
 * ## 两态：列表 / 预览
 *
 * 列表是 360px 窄栏，只给「第几版 · 怎么来的 · 什么时候」这类概览；点某一版进入**预览**，
 * 抽屉临时加宽到 640px 显示该版的只读 Markdown。加宽而不是切到中间正文区，是为了**不动
 * 状态机**——主区域由 `ViewState` 驱动，为「看一版历史」新增一个阶段，要把阶段表、进度条、
 * 底栏都跟着改一遍，而这件事并不需要它们参与。窄栏读长文要频繁滚动的代价，由「加宽」抵掉。
 *
 * ## 它是「内容区的一部分」，不是第二处流程落点
 *
 * 全站唯一操作落点仍是底栏（ADR-015）。本抽屉里只有**内容操作**（存为新版本 / 恢复某一版 /
 * 删除某一版 / 预览），永远不出现流程动作（生成下一份 / 完成 / 返回）；进入抽屉的入口也在
 * 底栏的内容行。它以 `--shell-action-bar-h` 作为下边距，因此**不会遮住底栏**（见 ADR-030）。
 *
 * ## 本组件是纯展示
 *
 * 不 import 任何 store：版本列表、预览详情、加载态、错误都由上层传入，动作以事件抛出去。
 * 「要存的内容是哪一版」也由上层提供（取自 `documents` 的工作副本），组件只把备注收上来。
 */
const props = defineProps<{
  /** 是否展开 */
  open: boolean
  /** 文档展示名，用于标题与无障碍标注 */
  title: string
  /** 版本摘要列表（新→旧），由上层拉取后传入 */
  versions: DocumentVersionSummary[]
  /** 是否正在拉取列表 */
  loading?: boolean
  /** 拉取 / 写入列表失败的原因 */
  error?: string
  /** 上层是否有进行中的动作（生成 / 优化中），置禁按钮 */
  busy?: boolean
  /** 正在预览的版本详情；`null` 表示停在列表上 */
  preview?: DocumentVersionDetail | null
  /** 是否正在拉取预览内容 */
  previewLoading?: boolean
  /** 预览失败的原因 */
  previewError?: string
}>()

const emit = defineEmits<{
  /** 关闭抽屉 */
  close: []
  /** 把当前正文存为新版本；载荷是用户填的备注（可为空） */
  checkpoint: [note: string]
  /** 恢复某个历史版本 */
  restore: [versionId: number]
  /** 预览某个版本 */
  preview: [versionId: number]
  /** 从预览回到列表 */
  closePreview: []
  /** 删除某个历史版本 */
  remove: [versionId: number]
}>()

/** 「存为新版本」的备注输入 */
const note = ref('')

/** 正在二次确认删除的版本 id；`null` 表示没有待确认的删除 */
const pendingRemoveId = ref<number | null>(null)

/** 正在二次确认恢复的版本 id；`null` 表示没有待确认的恢复 */
const pendingRestoreId = ref<number | null>(null)

/** 撤掉所有待确认的动作（两个都清：任何时刻至多一个确认开着） */
function clearPending(): void {
  pendingRemoveId.value = null
  pendingRestoreId.value = null
}

// 每次打开都清空备注与待确认的动作：上一次填/点的内容不该顺延到下一次
watch(
  () => props.open,
  (open) => {
    if (open) {
      note.value = ''
      clearPending()
    }
  },
)

/** 是否处于预览态（正在载入、已载入、或载入失败都算——三种都要离开列表） */
const previewing = computed(
  () => Boolean(props.preview) || Boolean(props.previewLoading) || Boolean(props.previewError),
)

// 进出预览时撤掉待确认的动作：那个确认属于列表里某一行，行都不在了再留着只会误触
watch(previewing, () => {
  clearPending()
})

/** 预览正文的渲染结果（只在详情变化时重算） */
const previewHtml = computed(() => (props.preview ? renderMarkdown(props.preview.content) : ''))

/** 「存为新版本」是否可点：上层忙的时候不许重复提交 */
const canCheckpoint = computed(() => !props.busy)

/** 提交存档：备注可选，正文由上层提供 */
function submitCheckpoint(): void {
  if (!canCheckpoint.value) return
  emit('checkpoint', note.value.trim())
}

/**
 * 点「恢复此版本」：先记下待确认的那一条，**不直接恢复**。
 *
 * 恢复会把当前正文换成这一版（虽然是追加、原文不丢，但用户眼前的稿子会变），
 * 属于值得拦一下的动作——与删除同一个口径：**先问一句再动手**。
 */
function askRestore(versionId: number): void {
  clearPending()
  pendingRestoreId.value = versionId
}

/** 点「删除」：先记下待确认的那一条，**不直接删** */
function askRemove(versionId: number): void {
  clearPending()
  pendingRemoveId.value = versionId
}

/**
 * Esc 逐层退出：先撤掉待确认的动作，再退出预览，最后才关抽屉。
 *
 * 从里往外退符合直觉——用户刚点开的确认，第一次 Esc 就该只收掉它。
 */
function onKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Escape' || !props.open) return
  if (pendingRemoveId.value !== null || pendingRestoreId.value !== null) {
    clearPending()
    return
  }
  if (previewing.value) {
    emit('closePreview')
    return
  }
  emit('close')
}

onMounted(() => window.addEventListener('keydown', onKeydown))
onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))
</script>

<template>
  <!--
    抽屉本体：`fixed` 脱离文档流（不挤中部布局），右侧贴边、上下留出底栏空间。
    下边距取 `--shell-action-bar-h`（底栏实时高度），因此它**永远不遮底栏**。
    预览态临时加宽到 640px——窄栏读长文档要频繁滚动，而这一段恰好是要读的正文。
  -->
  <aside
    class="shell-version-drawer fixed right-0 top-0 z-30 flex flex-col border-l border-line bg-surface shadow-xl transition-[width,transform] duration-300 ease-out"
    :class="[
      open ? 'translate-x-0' : 'translate-x-full',
      previewing ? 'w-[640px] max-w-[92vw]' : 'w-[360px] max-w-[85vw]',
    ]"
    style="bottom: var(--shell-action-bar-h, 0px)"
    :aria-hidden="!open"
    :aria-label="`${title} 的版本历史`"
  >
    <!-- 头部：标题 + 关闭 -->
    <header class="flex items-center justify-between gap-2 border-b border-line px-4 py-3">
      <div class="min-w-0">
        <h2 class="truncate text-sm font-semibold text-fg">
          {{ previewing ? '版本预览' : '版本历史' }}
        </h2>
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

    <!-- ============ 预览态：某一版的只读正文 ============ -->
    <template v-if="previewing">
      <div class="flex items-center justify-between gap-2 border-b border-line px-4 py-2.5">
        <AppButton variant="ghost" size="sm" @click="emit('closePreview')">
          <svg class="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
            <path
              fill-rule="evenodd"
              d="M12.79 5.23a.75.75 0 0 1-.02 1.06L8.83 10l3.94 3.71a.75.75 0 1 1-1.04 1.08l-4.5-4.25a.75.75 0 0 1 0-1.08l4.5-4.25a.75.75 0 0 1 1.06.02Z"
              clip-rule="evenodd"
            />
          </svg>
          返回列表
        </AppButton>
        <!-- 恢复入口：预览之后最自然的下一步就是「我就要这一版」。同样要二次确认——
             它会换掉用户眼前的当前正文。当前版本不给（点了也是原地不动） -->
        <div v-if="preview && !preview.isCurrent" class="flex items-center gap-2">
          <template v-if="pendingRestoreId !== preview.versionId">
            <AppButton variant="secondary" size="sm" :disabled="busy" @click="askRestore(preview.versionId)">
              恢复此版本
            </AppButton>
          </template>
          <template v-else>
            <span class="text-[11px] text-muted">恢复这一版？</span>
            <AppButton
              variant="primary"
              size="sm"
              :disabled="busy"
              @click="emit('restore', preview.versionId)"
            >
              确认恢复
            </AppButton>
            <AppButton variant="ghost" size="sm" @click="clearPending()">取消</AppButton>
          </template>
        </div>
      </div>

      <!-- 版本元信息：第几版 · 怎么来的 · 什么时候 · 多长 · 备注 -->
      <div v-if="preview" class="space-y-1.5 border-b border-line bg-surface-2/60 px-4 py-2.5">
        <div class="flex flex-wrap items-center gap-1.5">
          <span class="text-xs font-semibold text-fg">v{{ preview.versionNo }}</span>
          <AppBadge :variant="preview.isCurrent ? 'primary' : 'neutral'">
            {{ versionSourceLabel(preview.source) }}
          </AppBadge>
          <AppBadge v-if="preview.isCurrent" variant="success">当前</AppBadge>
          <span class="text-[11px] text-subtle">{{ relativeTime(preview.createdAt) }}</span>
          <span class="text-[11px] text-subtle">· 约 {{ preview.content.length }} 字</span>
        </div>
        <p v-if="preview.note" class="text-[11px] text-muted">{{ preview.note }}</p>
      </div>

      <div class="flex-1 overflow-y-auto bg-surface-2/40 p-4 dark:bg-bg/40">
        <p v-if="previewLoading" class="py-8 text-center text-xs text-subtle">正在读取该版本…</p>

        <p
          v-else-if="previewError"
          class="rounded-control bg-danger-surface px-3 py-2 text-xs text-danger-fg"
        >
          {{ previewError }}
        </p>

        <!-- 只读正文：与审阅页的预览同一套排版（同一份 Markdown 渲染器） -->
        <div
          v-else-if="preview"
          class="prose prose-sm prose-slate max-w-none rounded-xl bg-surface px-5 py-4 shadow-sm ring-1 ring-line dark:prose-invert"
        >
          <!-- eslint-disable-next-line vue/no-v-html -- 内容经 markdown-it 渲染且已关闭原始 HTML -->
          <div v-html="previewHtml" />
        </div>
      </div>
    </template>

    <!-- ============ 列表态 ============ -->
    <template v-else>
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

      <!-- 列表：新→旧。点条目进预览；当前版本不可恢复也不可删除（它是这份文档正在用的正文） -->
      <div class="flex-1 overflow-y-auto px-4 py-3">
        <p v-if="loading" class="py-6 text-center text-xs text-subtle">正在读取版本历史…</p>

        <p
          v-else-if="error"
          class="rounded-control bg-danger-surface px-3 py-2 text-xs text-danger-fg"
        >
          {{ error }}
        </p>

        <p v-else-if="versions.length === 0" class="py-6 text-center text-xs text-subtle">
          还没有任何版本。生成或保存一次就会产生第一版。
        </p>

        <ul v-else class="space-y-2">
          <li
            v-for="version in versions"
            :key="version.versionId"
            class="relative rounded-card border border-line bg-surface"
            :class="version.isCurrent ? 'ring-1 ring-accent-soft-line' : ''"
          >
            <!--
              预览热区：一张**铺满整张卡片**的透明按钮，垫在内容层之下。
              为什么不直接给 `<li>` 挂 click：那要么丢掉 `ul > li` 的列表语义（改 role），
              要么与行内的动作按钮变成「嵌套交互元素」。铺一层热区两个问题都没有——
              它是动作按钮的**兄弟**而不是祖先，读屏看到的也仍是一个有名字的按钮。
              ⚠️ **当前版本不给热区**：它就是正文区里正在看的那一份，再预览一次没有新信息。
            -->
            <button
              v-if="!version.isCurrent"
              type="button"
              class="absolute inset-0 z-0 rounded-card transition-colors hover:bg-surface-2/60"
              :aria-label="`预览 v${version.versionNo}`"
              @click="emit('preview', version.versionId)"
            ></button>

            <!--
              内容层压在热区之上；`pointer-events-none` 让点击穿透到热区（文字本身不需要点击），
              动作按钮那一组再显式开回 `auto` —— 于是「整卡可点、按钮除外」成立。
              当前版本没有热区，内容层保持默认的 `auto`（文字仍可选中）。
            -->
            <div class="relative z-10 p-3" :class="version.isCurrent ? '' : 'pointer-events-none'">
              <div class="flex items-center justify-between gap-2">
                <div class="flex min-w-0 flex-wrap items-center gap-1.5">
                  <span class="text-xs font-semibold text-fg">v{{ version.versionNo }}</span>
                  <AppBadge :variant="version.isCurrent ? 'primary' : 'neutral'">
                    {{ versionSourceLabel(version.source) }}
                  </AppBadge>
                  <AppBadge v-if="version.isCurrent" variant="success">当前</AppBadge>
                </div>
                <span class="shrink-0 text-[11px] text-subtle">
                  {{ relativeTime(version.createdAt) }}
                </span>
              </div>

              <p v-if="version.note" class="mt-1.5 text-[11px] text-muted">{{ version.note }}</p>

              <div class="mt-2 flex items-center justify-between gap-2">
                <span class="text-[11px] text-subtle">约 {{ version.contentLength }} 字</span>

                <!-- 当前版本：只说一句为什么没有动作，不摆两个点了没用的按钮 -->
                <span v-if="version.isCurrent" class="text-[11px] text-subtle">
                  当前版本不能删除或覆盖
                </span>

                <!-- 历史版本：恢复 / 删除。两者都要先问一句，确认了才抛出去 -->
                <div
                  v-else
                  class="pointer-events-auto flex items-center gap-2"
                >
                  <template v-if="pendingRestoreId === version.versionId">
                    <span class="text-[11px] text-muted">恢复这一版？</span>
                    <AppButton
                      variant="primary"
                      size="sm"
                      :disabled="busy"
                      @click="emit('restore', version.versionId)"
                    >
                      确认恢复
                    </AppButton>
                    <AppButton variant="ghost" size="sm" @click="clearPending()">取消</AppButton>
                  </template>

                  <template v-else-if="pendingRemoveId === version.versionId">
                    <span class="text-[11px] text-muted">删除这一版？</span>
                    <AppButton
                      variant="danger"
                      size="sm"
                      :disabled="busy"
                      @click="emit('remove', version.versionId)"
                    >
                      确认删除
                    </AppButton>
                    <AppButton variant="ghost" size="sm" @click="clearPending()">取消</AppButton>
                  </template>

                  <template v-else>
                    <AppButton
                      variant="secondary"
                      size="sm"
                      :disabled="busy"
                      @click="askRestore(version.versionId)"
                    >
                      恢复此版本
                    </AppButton>
                    <AppButton
                      variant="danger"
                      size="sm"
                      :disabled="busy"
                      @click="askRemove(version.versionId)"
                    >
                      删除
                    </AppButton>
                  </template>
                </div>
              </div>
            </div>
          </li>
        </ul>
      </div>
    </template>
  </aside>
</template>
