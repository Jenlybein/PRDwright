<script setup lang="ts">
import { onMounted, ref, computed } from 'vue'
import { useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'
import { useSolutionsStore } from '@/stores/solutions'
import type { SolutionSummary } from '@/types'
import { relativeTime } from '@/utils/time'
import AppButton from '@/components/ui/AppButton.vue'
import ThemeMenu from '@/components/ui/ThemeMenu.vue'
import UserMenu from '@/components/UserMenu.vue'

/**
 * 左侧方案侧边栏（常驻 chrome，而非独立列表页）。
 *
 * 这里是整个工作台的固定外壳：顶端放品牌 + 主题切换，底部放登录入口，
 * 中间是「新建任务」+ 方案列表。列表是工作台的一部分，用户写方案时随时要切到
 * 另一份、看一下最近几条，做成侧栏比独立列表页少两次来回导航。
 *
 * lg 以上常驻左侧且 `sticky`，不随右侧内容滚动；窄屏（< lg）下默认收起为抽屉，
 * 用悬浮按钮唤出。本组件只负责展示与派发事件，数据与动作都在 `stores/solutions.ts`。
 *
 * 颜色全部走语义令牌（bg-surface / text-fg / text-subtle / bg-accent-soft …）。
 * 这里曾是暗色可读性最差的一屏：方案标题写的是 `text-slate-800` 却没有暗色变体，
 * 在深底上对比度只有 1.22:1（等于看不见）；选中态用的 primary-950/40 与底色
 * 也只差 1.14:1。现在标题走 --ui-fg、次要信息走 --ui-subtle、选中态由
 * 「浅色底 + 描边 + 左侧色条」三重提示共同表达。
 */

const solutions = useSolutionsStore()
const auth = useAuthStore()
const router = useRouter()

/** 窄屏抽屉是否展开 */
const drawerOpen = ref(false)

/** 正在确认删除的方案 id；null 表示没有待确认的删除 */
const pendingDeleteId = ref<number | null>(null)

onMounted(() => {
  // 未登录不拉列表：那只会换来一次 401 与无意义的弹窗
  if (auth.isLoggedIn && !solutions.loaded) void solutions.loadList()
})

/** 当前打开的方案在列表里的位置，用于高亮 */
const activeId = computed(() => solutions.currentId)

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
  <!--
    窄屏唤出按钮：lg 以上不需要。
    `shell-fab` 供 index.css 里的 :has() 规则识别——当前这屏存在吸底操作栏时按钮上移，
    否则会被固定在底部的操作栏盖住（见 index.css 尾部的规则）。
  -->
  <button
    type="button"
    class="shell-fab fixed bottom-4 right-4 z-30 flex h-12 w-12 items-center justify-center rounded-full bg-primary-500 text-on-accent shadow-lg shadow-primary-500/30 lg:hidden"
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
    class="fixed inset-0 z-30 bg-slate-950/40 lg:hidden"
    @click="drawerOpen = false"
  />

  <aside
    :class="[
      'flex flex-col border-line bg-surface/80 backdrop-blur',
      // 窄屏：抽屉；lg 以上：常驻左侧且 sticky，不随右侧内容滚动。
      // 宽度走 --shell-sidebar-w（index.css）：底部操作栏吸底时的左偏移引用同一个值。
      'fixed inset-y-0 left-0 z-40 w-[var(--shell-sidebar-w)] border-r transition-transform lg:sticky lg:top-0 lg:bottom-auto lg:h-screen lg:self-start lg:z-auto lg:translate-x-0',
      drawerOpen ? 'translate-x-0' : '-translate-x-full',
    ]"
    aria-label="方案列表"
  >
    <!-- 顶端：品牌 + 主题切换（亮 / 暗 / 跟随系统） -->
    <div class="flex items-center justify-between gap-2 border-b border-line px-4 py-3">
      <div class="flex min-w-0 items-center gap-2.5">
        <div
          class="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-primary-500 to-primary-600 text-sm font-bold text-white shadow-md shadow-primary-500/30"
        >
          H
        </div>
        <div class="min-w-0 leading-tight">
          <div class="truncate text-sm font-bold text-fg">PRDwright</div>
          <div class="truncate text-[11px] text-subtle">需求 → PRD / 接口 / 提示词</div>
        </div>
      </div>

      <ThemeMenu />
    </div>

    <!-- 新建任务（仅登录后） -->
    <div v-if="auth.isLoggedIn" class="px-3 pt-4">
      <AppButton variant="primary" block @click="openNew">
        <svg class="h-4 w-4" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
          <path
            d="M10.75 4.75a.75.75 0 0 0-1.5 0v4.5h-4.5a.75.75 0 0 0 0 1.5h4.5v4.5a.75.75 0 0 0 1.5 0v-4.5h4.5a.75.75 0 0 0 0-1.5h-4.5v-4.5Z"
          />
        </svg>
        新建任务
      </AppButton>
    </div>

    <!-- 方案列表（仅登录后；列表区独立滚动，不影响侧栏固定） -->
    <div v-if="auth.isLoggedIn" class="mt-4 flex-1 overflow-y-auto px-3 pb-4">
      <div class="mb-2 flex items-center justify-between px-2">
        <span class="text-xs font-medium text-muted">我的方案</span>
        <!-- 窄屏抽屉的收起按钮；lg 以上侧栏常驻，不需要 -->
        <button
          type="button"
          class="rounded-md p-1 text-subtle transition hover:bg-surface-2 hover:text-fg lg:hidden"
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

      <!-- 加载中 -->
      <p
        v-if="solutions.loading && solutions.items.length === 0"
        class="px-2 py-6 text-center text-xs text-subtle"
      >
        加载中…
      </p>

      <!-- 空态 -->
      <p
        v-else-if="solutions.items.length === 0"
        class="px-2 py-6 text-center text-xs leading-relaxed text-subtle"
      >
        还没有方案<br />写下第一句话就会自动创建
      </p>

      <ul v-else class="space-y-1">
        <li v-for="item in solutions.items" :key="item.id">
          <div
            :class="[
              'group relative rounded-lg border-l-2 py-2 pl-2 pr-2.5 transition',
              item.id === activeId
                ? 'border-primary-500 bg-accent-soft ring-1 ring-accent-soft-line'
                : 'border-transparent hover:bg-surface-2',
            ]"
          >
            <button
              type="button"
              class="block w-full text-left"
              :aria-current="item.id === activeId ? 'true' : undefined"
              @click="open(item)"
            >
              <span class="block truncate text-sm font-medium text-fg">{{ item.title }}</span>
              <span class="mt-0.5 flex items-center gap-1.5 text-xs text-subtle">
                <span>{{ solutions.stageLabel(item.stage) }}</span>
                <span aria-hidden="true">·</span>
                <span>{{ relativeTime(item.updated_at) }}</span>
              </span>
            </button>

            <!-- 删除按钮：hover 或当前项时出现，避免列表看起来全是叉号 -->
            <button
              v-if="pendingDeleteId !== item.id"
              type="button"
              class="absolute right-1.5 top-1.5 rounded-md p-1 text-subtle opacity-0 transition hover:bg-danger-surface hover:text-danger focus-visible:opacity-100 group-hover:opacity-100"
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
              class="absolute inset-0 flex items-center justify-between gap-2 rounded-lg bg-danger-surface px-2.5 py-2 ring-1 ring-danger-line"
            >
              <span class="truncate text-xs text-danger-fg">删除「{{ item.title }}」？</span>
              <span class="flex shrink-0 gap-1">
                <button
                  type="button"
                  class="rounded-md bg-danger px-2 py-0.5 text-xs font-medium text-on-accent transition hover:bg-danger/90"
                  @click.stop="doDelete"
                >
                  删除
                </button>
                <button
                  type="button"
                  class="rounded-md px-2 py-0.5 text-xs text-body transition hover:bg-surface"
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

    <!-- 未登录占位：登录后才看得到方案列表 -->
    <div v-else class="mt-4 flex-1 overflow-y-auto px-3 pb-4">
      <p class="px-2 py-6 text-center text-xs leading-relaxed text-subtle">
        登录后查看<br />你的方案列表
      </p>
    </div>

    <!-- 底端：登录入口；未登录显示「登录」，已登录显示昵称并可退出 -->
    <div class="border-t border-line px-3 py-3">
      <UserMenu />
    </div>
  </aside>
</template>
