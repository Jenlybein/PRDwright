<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { useSolutionsStore } from '@/stores/solutions'
import type { SolutionSummary } from '@/types'

/**
 * 左侧方案侧边栏。
 *
 * 做成侧边栏而不是独立列表页：列表是**工作台的一部分**，用户写方案时随时要切到
 * 另一份、看一下最近几条，若每次都得「离开当前页面 → 看列表 → 再点回来」，
 * 来回两次导航的代价远大于它带来的空间收益。
 *
 * 窄屏（< lg）下默认收起，用顶栏的按钮唤出抽屉——手机上常驻侧栏会把内容挤没。
 *
 * 本组件只负责展示与派发事件，数据与动作都在 `stores/solutions.ts`。
 */

const solutions = useSolutionsStore()
const router = useRouter()

/** 窄屏抽屉是否展开 */
const drawerOpen = ref(false)

/** 正在确认删除的方案 id；null 表示没有待确认的删除 */
const pendingDeleteId = ref<number | null>(null)

onMounted(() => {
  if (!solutions.loaded) void solutions.loadList()
})

/** 当前打开的方案在列表里的位置，用于高亮 */
const activeId = computed(() => solutions.currentId)

/** 把 UTC 时间转成「刚刚 / 3 分钟前 / 10-13 15:20」这种相对说法 */
function relativeTime(iso: string): string {
  if (!iso) return ''
  // 后端存的是不带时区后缀的 UTC 字符串，直接 new Date 会被当成本地时间，
  // 因此这里补上 Z 再解析，否则在东八区会显示成「8 小时前」这样的错值。
  const parsed = new Date(iso.endsWith('Z') ? iso : `${iso}Z`)
  if (Number.isNaN(parsed.getTime())) return ''

  const diffMs = Date.now() - parsed.getTime()
  const minutes = Math.floor(diffMs / 60_000)
  if (minutes < 1) return '刚刚'
  if (minutes < 60) return `${minutes} 分钟前`

  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`

  const d = parsed
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function confirmDelete(item: SolutionSummary): void {
  pendingDeleteId.value = item.id
}

async function doDelete(): Promise<void> {
  const id = pendingDeleteId.value
  pendingDeleteId.value = null
  if (id !== null) await solutions.remove(id)
}

function cancelDelete(): void {
  pendingDeleteId.value = null
}

function openNew(): void {
  drawerOpen.value = false
  // 同 `open()`：地址要跟着走，否则「新建」之后地址还停在上一份方案的 id 上
  if (router.currentRoute.value.path !== '/v2') void router.push('/v2')
}

function open(item: SolutionSummary): void {
  drawerOpen.value = false
  if (item.id === activeId.value) return
  // 走路由而不是直接 `openSolution`：地址是「当前打开哪一份」的权威来源，
  // 只加载内容不改地址，会让后续的自动保存分不清「更新这一份」还是「新建一份」，
  // 结果就是在列表里多出一份重复方案。改地址后由 AppV2 的 watch 负责加载。
  void router.push(`/v2/${item.id}`)
}
</script>

<template>
  <!-- 窄屏唤出按钮：lg 以上不需要 -->
  <button
    type="button"
    class="fixed bottom-4 right-4 z-30 flex h-12 w-12 items-center justify-center rounded-full bg-primary-500 text-white shadow-lg shadow-sky-500/30 lg:hidden"
    aria-label="打开方案列表"
    @click="drawerOpen = true"
  >
    <svg class="h-5 w-5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path
        fill-rule="evenodd"
        d="M2 5.75A.75.75 0 0 1 2.75 5h14.5a.75.75 0 0 1 0 1.5H2.75A.75.75 0 0 1 2 5.75Zm0 4.25a.75.75 0 0 1 .75-.75h14.5a.75.75 0 0 1 0 1.5H2.75A.75.75 0 0 1 2 10Zm.75 3.5a.75.75 0 0 0 0 1.5h14.5a.75.75 0 0 0 0-1.5H2.75Z"
        clip-rule="evenodd"
      />
    </svg>
  </button>

  <!-- 窄屏抽屉遮罩 -->
  <div
    v-if="drawerOpen"
    class="fixed inset-0 z-30 bg-slate-900/30 lg:hidden"
    @click="drawerOpen = false"
  />

  <aside
    :class="[
      'flex flex-col border-slate-200/70 bg-white/70 backdrop-blur',
      // 窄屏：抽屉；lg 以上：常驻左侧栏
      'fixed inset-y-0 left-0 z-40 w-64 border-r transition-transform lg:static lg:z-auto lg:translate-x-0',
      drawerOpen ? 'translate-x-0' : '-translate-x-full',
    ]"
    aria-label="方案列表"
  >
    <div class="flex items-center justify-between px-4 py-3 lg:hidden">
      <span class="text-sm font-medium text-slate-700">我的方案</span>
      <button
        type="button"
        class="rounded-md p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-600"
        aria-label="收起方案列表"
        @click="drawerOpen = false"
      >
        <svg class="h-4 w-4" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
          <path
            d="M6.28 5.22a.75.75 0 0 0-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 1 0 1.06 1.06L10 11.06l3.72 3.72a.75.75 0 1 0 1.06-1.06L11.06 10l3.72-3.72a.75.75 0 0 0-1.06-1.06L10 8.94 6.28 5.22Z"
          />
        </svg>
      </button>
    </div>

    <div class="px-3 pt-4 lg:pt-5">
      <button
        type="button"
        class="flex w-full items-center justify-center gap-2 rounded-xl bg-primary-500 px-3 py-2.5 text-sm font-medium text-white shadow-sm transition hover:bg-primary-600"
        @click="openNew"
      >
        <svg class="h-4 w-4" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
          <path
            d="M10.75 4.75a.75.75 0 0 0-1.5 0v4.5h-4.5a.75.75 0 0 0 0 1.5h4.5v4.5a.75.75 0 0 0 1.5 0v-4.5h4.5a.75.75 0 0 0 0-1.5h-4.5v-4.5Z"
          />
        </svg>
        新建任务
      </button>
    </div>

    <div class="mt-4 flex-1 overflow-y-auto px-3 pb-4">
      <div class="mb-2 px-2 text-xs font-medium text-slate-400">我的方案</div>

      <!-- 加载中 -->
      <p v-if="solutions.loading && solutions.items.length === 0" class="px-2 py-6 text-center text-xs text-slate-400">
        加载中…
      </p>

      <!-- 空态 -->
      <p
        v-else-if="solutions.items.length === 0"
        class="px-2 py-6 text-center text-xs leading-relaxed text-slate-400"
      >
        还没有方案<br />写下第一句话就会自动创建
      </p>

      <ul v-else class="space-y-1">
        <li v-for="item in solutions.items" :key="item.id">
          <div
            :class="[
              'group relative rounded-lg px-2.5 py-2 transition',
              item.id === activeId
                ? 'bg-sky-50 ring-1 ring-sky-200'
                : 'hover:bg-slate-100/80',
            ]"
          >
            <button
              type="button"
              class="block w-full text-left"
              :aria-current="item.id === activeId ? 'true' : undefined"
              @click="open(item)"
            >
              <span class="block truncate text-sm text-slate-800">{{ item.title }}</span>
              <span class="mt-0.5 flex items-center gap-1.5 text-xs text-slate-400">
                <span>{{ solutions.stageLabel(item.stage) }}</span>
                <span aria-hidden="true">·</span>
                <span>{{ relativeTime(item.updated_at) }}</span>
              </span>
            </button>

            <!-- 删除按钮：hover 或当前项时出现，避免列表看起来全是叉号 -->
            <button
              v-if="pendingDeleteId !== item.id"
              type="button"
              class="absolute right-1.5 top-1.5 rounded-md p-1 text-slate-300 opacity-0 transition hover:bg-red-50 hover:text-red-500 focus-visible:opacity-100 group-hover:opacity-100"
              :aria-label="`删除方案 ${item.title}`"
              @click.stop="confirmDelete(item)"
            >
              <svg class="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                <path
                  fill-rule="evenodd"
                  d="M8.75 1a1 1 0 0 0-.95.68L7.42 2.5H4.25a.75.75 0 0 0 0 1.5h11.5a.75.75 0 0 0 0-1.5h-3.17l-.38-.82A1 1 0 0 0 11.25 1h-2.5ZM5.5 5.5l.6 9.06A1.75 1.75 0 0 0 7.84 16.2h4.32a1.75 1.75 0 0 0 1.74-1.64l.6-9.06H5.5Zm3 2.25a.75.75 0 0 1 .75.75v5a.75.75 0 0 1-1.5 0v-5a.75.75 0 0 1 .75-.75Zm3 0a.75.75 0 0 1 .75.75v5a.75.75 0 0 1-1.5 0v-5a.75.75 0 0 1 .75-.75Z"
                  clip-rule="evenodd"
                />
              </svg>
            </button>

            <!-- 行内二次确认：删除是不可逆动作，不该点一下就没了 -->
            <div
              v-else
              class="absolute inset-0 flex items-center justify-between gap-2 rounded-lg bg-red-50 px-2.5 py-2 ring-1 ring-red-100"
            >
              <span class="truncate text-xs text-red-700">删除「{{ item.title }}」？</span>
              <span class="flex shrink-0 gap-1">
                <button
                  type="button"
                  class="rounded-md bg-red-500 px-2 py-0.5 text-xs font-medium text-white hover:bg-red-600"
                  @click.stop="doDelete"
                >
                  删除
                </button>
                <button
                  type="button"
                  class="rounded-md px-2 py-0.5 text-xs text-slate-500 hover:bg-white"
                  @click.stop="cancelDelete"
                >
                  取消
                </button>
              </span>
            </div>
          </div>
        </li>
      </ul>
    </div>
  </aside>
</template>
