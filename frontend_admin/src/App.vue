<script setup lang="ts">
/**
 * 管理端外壳：左侧导航 + 顶部信息条 + 路由出口。
 *
 * 两条结构上的取舍
 * ----------------
 * 1. **导航从顶栏搬到左侧**。后台的页面有七个且都是「切来切去」的用法，
 *    横向排一行的结果是每加一页就更挤一分，最后动作用户认不全；
 *    竖排有固定宽度，加页不用重排。
 * 2. **只有后台页面包外壳**。登录页与无权限页整屏自成一格——
 *    给一个还没登录的人看「概览 / 任务 / 用户」的导航，点哪个都会被弹回来。
 *
 * 账号块放在侧栏底部而不是顶栏：顶栏留给「我在哪一页」，
 * 侧栏留给「我是谁、怎么退出」，两件事不抢同一个位置。
 */
import { computed } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'

import ThemeSwitch from '@/components/ThemeSwitch.vue'
import { useSessionStore } from '@/stores/session'

const session = useSessionStore()
const route = useRoute()
const router = useRouter()

/** 侧栏导航。新增页面只改这一处，不用去每个页面里放返回链接 */
const NAV = [
  {
    key: 'ops',
    label: '运营',
    code: 'OPS',
    items: [
      { name: 'dashboard', label: '概览', code: 'OVERVIEW' },
      { name: 'jobs', label: '任务监控', code: 'JOBS' },
      { name: 'users', label: '用户', code: 'USERS' },
      { name: 'invites', label: '邀请码', code: 'INVITES' },
    ],
  },
  {
    key: 'sys',
    label: '系统',
    code: 'SYS',
    items: [
      { name: 'logs', label: '系统日志', code: 'LOGS' },
      { name: 'config', label: '配置', code: 'CONFIG' },
      { name: 'audit', label: '操作记录', code: 'AUDIT' },
    ],
  },
]

/** 扁平化一份，供「当前页面是什么」与移动端横排导航共用 */
const flatNav = NAV.flatMap((group) => group.items)

/**
 * 是否处于「后台外壳」里。
 *
 * 判据是路由的 `requiresAdmin`，不是「名字不等于 login」：
 * 后者会把无权限页也塞进外壳，而那一页恰恰要让人只看见「你没权限、换个账号」。
 */
const isConsole = computed(() => Boolean(route.meta.requiresAdmin))

const current = computed(() => flatNav.find((item) => item.name === route.name))

/** 顶栏日期。用本地时区，和日志页的显示口径一致 */
const today = computed(() => {
  const d = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  const week = ['日', '一', '二', '三', '四', '五', '六'][d.getDay()]
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} 周${week}`
})

async function onLogout() {
  await session.logout()
  await router.replace({ name: 'login' })
}
</script>

<template>
  <div class="min-h-screen">
    <!-- 后台外壳 -->
    <div
      v-if="isConsole"
      class="mx-auto flex min-h-screen w-full max-w-[1720px] flex-col lg:flex-row"
    >
      <!-- 侧栏：天蓝底 + 硬边，是这套视觉里唯一的大面积色块 -->
      <aside
        class="hidden shrink-0 flex-col gap-5 border-brut bg-side px-4 py-4 lg:sticky lg:top-0 lg:flex lg:h-screen lg:w-[264px] lg:border-b-0 lg:border-r-[3px]"
      >
        <!-- 品牌牌：白底黑边，把「这是后台」在一眼之内说清 -->
        <div class="brut-card flex items-center gap-2.5 px-3 py-2.5">
          <span
            class="flex h-8 w-8 items-center justify-center border-2 border-brut bg-sky text-sm font-black text-on-sky"
          >
            P
          </span>
          <span class="min-w-0">
            <span class="block text-sm leading-tight font-extrabold tracking-tight text-ink">
              PRDwright
            </span>
            <span class="kicker mt-0.5 block">Admin Console</span>
          </span>
        </div>

        <nav class="flex flex-col gap-1" aria-label="后台导航">
          <template v-for="group in NAV" :key="group.key">
            <p class="kicker mt-2 mb-1 px-1">{{ group.label }} / {{ group.code }}</p>
            <RouterLink
              v-for="item in group.items"
              :key="item.name"
              :to="{ name: item.name }"
              class="group flex items-center justify-between gap-2 border-2 px-2.5 py-2 text-sm font-bold transition-colors"
              :class="
                route.name === item.name
                  ? 'border-brut bg-ink text-card shadow-[4px_4px_0_0_var(--ui-card)]'
                  : 'border-transparent text-ink hover:border-brut hover:bg-card/55'
              "
            >
              <span>{{ item.label }}</span>
              <span
                class="font-mono text-[10px] tracking-widest"
                :class="route.name === item.name ? 'text-sky' : 'text-ink/45'"
              >
                {{ item.code }}
              </span>
            </RouterLink>
          </template>
        </nav>

        <!-- 账号块：桌面端固定在侧栏底部 -->
        <div class="mt-auto hidden lg:block">
          <div class="border-2 border-brut bg-card/70 px-3 py-2.5">
            <p class="kicker">Signed in</p>
            <p class="mt-1 truncate text-sm font-extrabold text-ink">
              {{ session.username || '—' }}
            </p>
          </div>
          <button type="button" class="brut-btn brut-btn-xs mt-2 w-full" @click="onLogout">
            退出登录
          </button>
        </div>
      </aside>

      <div class="flex min-w-0 flex-1 flex-col">
        <!-- 顶栏：只回答「我在哪」与「现在几点」，操作永远留给页面自己 -->
        <header
          class="sticky top-0 z-20 border-brut border-b-[3px] bg-card px-4 py-2 sm:px-6"
        >
          <div class="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
            <div class="flex min-w-0 items-center gap-2">
              <!-- 窄屏没有侧栏，牌子挪到顶栏来，否则第一屏看不出这是哪个后台 -->
              <span class="flex items-center gap-1.5 lg:hidden">
                <span
                  class="flex h-6 w-6 items-center justify-center border-2 border-brut bg-sky text-[11px] font-black text-on-sky"
                >
                  P
                </span>
                <span class="text-sm font-extrabold tracking-tight text-ink">PRDwright</span>
                <span class="text-brut/40">/</span>
              </span>
              <span class="kicker hidden lg:inline">PRDwright Admin</span>
              <span class="hidden text-brut/40 lg:inline">/</span>
              <span class="truncate text-sm font-extrabold text-ink">
                {{ current?.label ?? '管理后台' }}
              </span>
              <span
                v-if="current"
                class="brut-tag brut-tag-slab hidden md:inline-flex"
              >
                {{ current.code }}
              </span>
            </div>

            <div class="flex items-center gap-2.5">
              <span class="kicker hidden md:inline">{{ today }}</span>
              <span class="hidden items-center gap-1.5 sm:flex">
                <span class="h-2 w-2 border border-brut bg-[var(--ui-sky)]"></span>
                <span class="kicker">Online</span>
              </span>
              <!-- 主题切换放在顶栏而不是某个设置页：它要随手可及，且切换立即生效 -->
              <ThemeSwitch />
              <button
                type="button"
                class="brut-btn brut-btn-xs lg:hidden"
                @click="onLogout"
              >
                退出
              </button>
            </div>
          </div>

          <!-- 移动端：导航横排，省掉一整个侧栏 -->
          <nav
            class="-mx-1 mt-2 flex gap-1.5 overflow-x-auto pb-0.5 lg:hidden"
            aria-label="后台导航（窄屏）"
          >
            <RouterLink
              v-for="item in flatNav"
              :key="item.name"
              :to="{ name: item.name }"
              class="brut-btn brut-btn-xs shrink-0"
              :class="route.name === item.name ? 'brut-btn-ink' : ''"
            >
              {{ item.label }}
            </RouterLink>
          </nav>
        </header>

        <main class="flex-1 px-4 py-5 sm:px-6 sm:py-6">
          <div class="mx-auto w-full max-w-[1180px]">
            <RouterView />
          </div>
        </main>

        <footer
          class="brut-hatch mt-4 border-t-2 border-brut px-4 py-3 sm:px-6"
        >
          <div
            class="mx-auto flex w-full max-w-[1180px] flex-wrap items-center justify-between gap-2"
          >
            <span class="kicker">PRDwright · 管理后台</span>
            <span class="kicker">
              权限由服务端 require_admin 判定，本页面只是入口
            </span>
          </div>
        </footer>
      </div>
    </div>

    <!-- 登录 / 无权限：整屏自成一格，不套后台外壳 -->
    <RouterView v-else />
  </div>
</template>
