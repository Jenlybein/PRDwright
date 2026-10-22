<script setup lang="ts">
/**
 * 管理端外壳：左侧导航 + 顶部信息条 + 路由出口。
 *
 * 两条结构上的取舍
 * ----------------
 * 1. **导航从顶栏搬到左侧**。后台的页面有七个且都是「切来切去」的用法，
 *    横向排一行的结果是每加一页就更挤一分，最后动作用户认不全；
 *    竖排有固定宽度，加页不用重排。
 * 2. **只有后台页面包外壳**。登录页整屏自成一格——
 *    给一个还没登录的人看「概览 / 任务 / 用户」的导航，点哪个都会被弹回来。
 *
 * 账号块放在侧栏底部而不是顶栏：顶栏留给「我在哪一页」，
 * 侧栏留给「我是谁、怎么退出」，两件事不抢同一个位置。
 */
import { computed, ref } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'

import ThemeSwitch from '@/components/ThemeSwitch.vue'
import { NAV, flatNav, type NavChild, type NavItem } from '@/nav'
import { useSessionStore } from '@/stores/session'

const session = useSessionStore()
const route = useRoute()
const router = useRouter()

/**
 * 是否处于「后台外壳」里。
 *
 * 判据是路由的 `requiresAdmin`，不是「名字不等于 login」：
 * 后者会在将来加别的公共页（例如重置密码）时悄悄给它套上后台导航，
 * 也会把「不是管理员」与「不在后台里」这两件事混成一件。
 */
const isConsole = computed(() => Boolean(route.meta.requiresAdmin))

/** 当前 URL 上的分区（`?sec=`）。侧栏高亮与页面内容读的是同一个值 */
const currentSec = computed(() => String(route.query.sec ?? ''))

/**
 * 某一项是否就是当前这一屏。
 *
 * 判据是「路由名 **且** 分区」两条都对上：只比路由名的话，
 * 「配置」和它下面的四个子项会同时高亮，看上去像选了两个。
 */
function isActive(item: NavItem | NavChild): boolean {
  return route.name === item.name && currentSec.value === (item.sec ?? '')
}

/** 有子项、且当前页就在这组里（选中的可能是组里的某一项） */
const branch = computed(() => NAV.flatMap((g) => g.items).find((i) => i.children?.length && i.name === route.name))

/**
 * 子菜单的展开状态。
 *
 * ⚠️ **只由点击决定，不跟着路由变**。早先的版本是「人在配置页就展开、
 * 离开就收起」，结果点开配置再点别的页，菜单当着面自己合上了——
 * 展开是一个人说「我要看这摊」的动作，只有他自己再点一次才该收。
 *
 * 默认展开：子菜单存在的意义就是被看见，藏起来只是多一次点击。
 */
const openGroups = ref<Record<string, boolean>>({})

function isOpen(item: NavItem): boolean {
  return openGroups.value[item.name] ?? true
}

function toggleGroup(item: NavItem) {
  openGroups.value = { ...openGroups.value, [item.name]: !isOpen(item) }
}

/** 跳转目标：有 `sec` 就带在 query 上（配置页的子项），没有就是裸链接 */
function toOf(item: NavChild) {
  return { name: item.name, query: item.sec ? { sec: item.sec } : {} }
}

/** 精确命中的那一项（可能是子项） */
const current = computed(
  () => flatNav.find((item) => route.name === item.name && (item.sec ?? '') === currentSec.value) ?? null,
)

/** 顶栏标题：在子菜单里时写成「配置 · 模型配置」，让人知道自己在哪一摊 */
const heading = computed(() => {
  const item = current.value
  if (!item) return '管理后台'
  return branch.value && branch.value !== item ? `${branch.value.label} · ${item.label}` : item.label
})

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

        <!-- 导航。样式（含子菜单的缩进与竖线）都在 index.css 的 .brut-nav-* 里，
             层次只靠缩进 + 竖线表达，不靠颜色深浅——暗色下深浅差不可靠。 -->
        <nav class="brut-nav" aria-label="后台导航">
          <template v-for="group in NAV" :key="group.key">
            <p class="kicker mt-2 mb-1 px-1">{{ group.label }} / {{ group.code }}</p>

            <template v-for="item in group.items" :key="item.name">
              <!-- 有子项：整行就是一个「展开/收起」按钮，点了**不跳转**。
                   配置页被切成四个分区，「配置」本身不再是一屏，
                   点它跳进某一屏只会让人以为那是「配置的概览」。 -->
              <template v-if="item.children?.length">
                <button
                  type="button"
                  class="brut-nav-row"
                  :class="route.name === item.name ? 'is-branch' : ''"
                  :aria-expanded="isOpen(item)"
                  @click="toggleGroup(item)"
                >
                  <span class="min-w-0 flex-1 truncate">{{ item.label }}</span>
                  <span class="brut-nav-caret" :class="isOpen(item) ? '' : 'is-closed'">▾</span>
                </button>

                <div v-if="isOpen(item)" class="brut-nav-sub">
                  <RouterLink
                    v-for="child in item.children"
                    :key="child.sec ?? child.name"
                    :to="toOf(child)"
                    class="brut-nav-row"
                    :class="isActive(child) ? 'is-active' : ''"
                  >
                    <span class="truncate">{{ child.label }}</span>
                    <span class="brut-nav-code">{{ child.code }}</span>
                  </RouterLink>
                </div>
              </template>

              <!-- 无子项：整行就是一个链接 -->
              <RouterLink
                v-else
                :to="toOf(item)"
                class="brut-nav-row"
                :class="isActive(item) ? 'is-active' : ''"
              >
                <span class="truncate">{{ item.label }}</span>
                <span class="brut-nav-code">{{ item.code }}</span>
              </RouterLink>
            </template>
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
              <span class="truncate text-sm font-extrabold text-ink">{{ heading }}</span>
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
              :key="`${item.name}-${item.sec ?? ''}`"
              :to="toOf(item)"
              class="brut-btn brut-btn-xs shrink-0"
              :class="isActive(item) ? 'brut-btn-ink' : ''"
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
