<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import type { RagHit } from '@/types'
import AppActionBar from '@/components/ui/AppActionBar.vue'
import AppButton from '@/components/ui/AppButton.vue'
import AppSpinner from '@/components/ui/AppSpinner.vue'
import AppBadge from '@/components/ui/AppBadge.vue'

/**
 * 接口规范检索结果确认面板（RAG）。
 *
 * 生成接口文档前，先把从团队规范库里检索到的「接口规范条文」与「历史接口示例」摆出来
 * 让用户过目：默认全选，用户可取消勾选不相关的条目，确认后才带着勾选的条目去生成；
 * 直接取消则不调用生成接口。
 *
 * 面板只做展示与选择，不发起请求；`confirm` 抛出勾选后的命中列表，`cancel` 抛空。
 */
const props = withDefaults(
  defineProps<{
    /** 检索命中列表 */
    hits?: RagHit[]
    /** 是否正在检索中 */
    loading?: boolean
    /** 检索失败原因 */
    error?: string
  }>(),
  {
    hits: () => [],
    loading: false,
    error: '',
  },
)

const emit = defineEmits<{
  /** 确认：载荷为勾选后的命中列表（可能为空） */
  confirm: [hits: RagHit[]]
  /** 取消：不生成接口文档 */
  cancel: []
}>()

/** 取条目的稳定标识：优先 id，缺省时退回标题 */
function keyOf(hit: RagHit): string {
  return hit.id || hit.title || hit.content.slice(0, 24)
}

/** 当前勾选的条目标识集合 */
const selected = ref<Set<string>>(new Set())

// 命中列表变化（含首次拿到结果）时默认全选，避免用户面对一个空的选择状态
watch(
  () => props.hits,
  (hits) => {
    selected.value = new Set(hits.map((hit) => keyOf(hit)))
  },
  { immediate: true },
)

/** 勾选后的命中列表 */
const selectedHits = computed(() => props.hits.filter((hit) => selected.value.has(keyOf(hit))))

/** 是否全部勾选 */
const allSelected = computed(
  () => props.hits.length > 0 && selected.value.size === props.hits.length,
)

/** 按来源分组，便于区分「团队接口规范」与「历史接口示例」 */
const groups = computed(() => {
  const result: { source: string; items: RagHit[] }[] = []
  for (const hit of props.hits) {
    const source = hit.source || '规范库'
    const group = result.find((item) => item.source === source)
    if (group) group.items.push(hit)
    else result.push({ source, items: [hit] })
  }
  return result
})

/** 切换单条勾选状态 */
function toggle(hit: RagHit): void {
  const key = keyOf(hit)
  const next = new Set(selected.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  selected.value = next
}

/** 全选 / 全不选 */
function toggleAll(): void {
  selected.value = allSelected.value ? new Set() : new Set(props.hits.map((hit) => keyOf(hit)))
}

/** 主按钮文案：没有命中时不叫「确认」 */
const confirmLabel = computed(() => {
  if (!props.hits.length) return '直接生成接口文档'
  return `确认并生成接口文档（已选 ${selected.value.size} 条）`
})
</script>

<template>
  <section
    class="overflow-hidden rounded-2xl border border-line bg-surface shadow-sm  "
    aria-label="接口规范检索结果"
  >
    <!-- 头部 -->
    <div class="border-b border-line px-4 py-3  sm:px-6">
      <div class="flex items-start gap-3">
        <div class="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-primary-50 dark:bg-primary-950/40">
          <svg class="h-5 w-5 text-primary-600" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
            <path
              fill-rule="evenodd"
              d="M9 3.5a5.5 5.5 0 1 0 0 11 5.5 5.5 0 0 0 0-11ZM2 9a7 7 0 1 1 12.452 4.391l3.328 3.329a.75.75 0 1 1-1.06 1.06l-3.329-3.328A7 7 0 0 1 2 9Z"
              clip-rule="evenodd"
            />
          </svg>
        </div>
        <div class="min-w-0 flex-1">
          <h2 class="text-sm font-semibold text-fg ">接口规范检索结果</h2>
          <p class="mt-0.5 text-xs text-subtle">
            生成前先从团队规范库检索相关规范与历史接口示例；勾选的条目会作为「参考规范」参与生成，
            让接口风格与团队既有约定对齐。不勾选的条目不会被使用。
          </p>
        </div>
        <AppButton
          v-if="hits.length && !loading"
          variant="ghost"
          size="sm"
          class="shrink-0"
          @click="toggleAll"
        >
          {{ allSelected ? '全不选' : '全选' }}
        </AppButton>
      </div>
    </div>

    <!-- 检索中 -->
    <div v-if="loading" class="flex items-center justify-center gap-2 bg-surface-2 px-4 py-12 text-sm text-subtle  ">
      <AppSpinner />
      正在检索团队接口规范…
    </div>

    <!-- 检索失败 -->
    <div v-else-if="error" class="bg-surface-2 px-4 py-6  sm:px-6">
      <p
        class="flex items-start gap-2 rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-700 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-300"
        role="alert"
      >
        <svg class="mt-0.5 h-4 w-4 shrink-0" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
          <path
            fill-rule="evenodd"
            d="M8.485 2.495c.673-1.167 2.357-1.167 3.03 0l6.28 10.875c.673 1.167-.17 2.625-1.516 2.625H3.72c-1.347 0-2.189-1.458-1.515-2.625L8.485 2.495ZM10 6a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5A.75.75 0 0 1 10 6Zm0 8a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z"
            clip-rule="evenodd"
          />
        </svg>
        <span>{{ error }}</span>
      </p>
      <p class="mt-2 text-xs text-subtle">检索失败不影响生成，你可以跳过检索直接生成接口文档。</p>
    </div>

    <!-- 无命中 -->
    <div v-else-if="!hits.length" class="bg-surface-2 px-4 py-10 text-center  sm:px-6">
      <p class="text-sm text-subtle">没有检索到相关规范，可以直接生成接口文档。</p>
    </div>

    <!-- 命中列表（按来源分组） -->
    <div v-else class="max-h-[min(56vh,520px)] space-y-4 overflow-y-auto bg-surface-2 p-4  sm:p-6">
      <div v-for="group in groups" :key="group.source">
        <div class="mb-2 flex items-center gap-2">
          <h3 class="text-xs font-semibold text-muted ">{{ group.source }}</h3>
          <AppBadge variant="neutral">{{ group.items.length }} 条</AppBadge>
        </div>

        <ul class="space-y-2">
          <li
            v-for="hit in group.items"
            :key="keyOf(hit)"
            class="rounded-xl border border-line bg-surface px-3 py-2.5 transition  "
            :class="selected.has(keyOf(hit)) ? 'ring-1 ring-primary-200 dark:ring-primary-800' : 'opacity-70'"
          >
            <label class="flex cursor-pointer items-start gap-2.5">
              <input
                type="checkbox"
                class="mt-0.5 h-4 w-4 shrink-0 rounded border-line-strong text-primary-600 focus:ring-primary-400 "
                :checked="selected.has(keyOf(hit))"
                :aria-label="hit.title || keyOf(hit)"
                @change="toggle(hit)"
              />
              <span class="min-w-0 flex-1">
                <span class="flex flex-wrap items-center gap-2">
                  <span class="text-sm font-medium text-fg ">{{ hit.title || '未命名条目' }}</span>
                  <span
                    v-if="hit.score"
                    class="rounded-full bg-surface-2 px-2 py-0.5 font-mono text-[11px] text-subtle  "
                  >
                    相关度 {{ hit.score.toFixed(2) }}
                  </span>
                </span>

                <!-- 命中关键词：解释「为什么检索到这条」 -->
                <span v-if="hit.matched_terms.length" class="mt-1 flex flex-wrap gap-1">
                  <span
                    v-for="term in hit.matched_terms"
                    :key="term"
                    class="rounded bg-primary-50 px-1.5 py-0.5 text-[11px] text-primary-600 dark:bg-primary-950/40 dark:text-primary-300"
                  >
                    {{ term }}
                  </span>
                </span>
              </span>
            </label>

            <pre
              class="mt-2 max-h-40 overflow-y-auto whitespace-pre-wrap break-words rounded-lg bg-surface-2 px-3 py-2 font-mono text-[11px] leading-relaxed text-muted  "
            >{{ hit.content }}</pre>
          </li>
        </ul>
      </div>
    </div>

    <!--
      面板底栏，与页面级操作栏同一套三段式（说明文字在左、主操作在最右）。
      `sticky=false`：本面板自身是 `overflow-hidden` 的卡片，吸底只会粘在卡片内部而不是视口上；
      而命中列表已限高内滚，底栏本来就常驻可见，不需要吸底。
    -->
    <AppActionBar :sticky="false">
      <template #secondary>
        <AppButton variant="secondary" size="md" :disabled="loading" @click="emit('cancel')">
          取消
        </AppButton>
      </template>
      <template #hint>
        <p class="text-xs text-subtle">取消则不会调用生成接口，接口文档保持原样。</p>
      </template>

      <AppButton
        variant="primary"
        size="md"
        :disabled="loading"
        @click="emit('confirm', selectedHits)"
      >
        {{ confirmLabel }}
      </AppButton>
    </AppActionBar>
  </section>
</template>
