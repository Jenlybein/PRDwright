/**
 * 管理端路由与「门禁」。
 *
 * 守卫只有一条规则：标了 `requiresAdmin` 的页面，必须先在服务端确认过
 * 「已登录 **且** 是管理员」才放行。三种结果对应三个去处：
 *
 * - 未登录 → 登录页（并记下原本想去哪，登录完送回去）
 * - 已登录但不是管理员 → 无权限页
 * - 是管理员 → 放行
 *
 * ⚠️ 这个守卫**不是安全边界**。它只是让正常用户少点一次、少看一个空白页；
 * 真正的拦截在后端 `require_admin`（页面藏起来挡不住直接敲接口的人）。
 * 所以这里判断的每一条，后端都必须能独立再判一次。
 */

import { createRouter, createWebHistory } from 'vue-router'

import { useSessionStore } from '@/stores/session'

// 让 `meta.requiresAdmin` 有类型：不写这段，TS 会把 meta 当成任意字段，
// 拼错了也是静默生效（守卫永远读不到，页面等于没上锁）。
declare module 'vue-router' {
  interface RouteMeta {
    /** 是否需要管理员身份才能进入 */
    requiresAdmin?: boolean
  }
}

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/dashboard' },
    {
      path: '/login',
      name: 'login',
      // 懒加载：未登录的用户不该为了看登录页而下载整个后台的代码
      component: () => import('@/views/LoginView.vue'),
    },
    {
      path: '/forbidden',
      name: 'forbidden',
      component: () => import('@/views/ForbiddenView.vue'),
    },
    {
      path: '/dashboard',
      name: 'dashboard',
      component: () => import('@/views/DashboardView.vue'),
      meta: { requiresAdmin: true },
    },
    {
      path: '/users',
      name: 'users',
      component: () => import('@/views/UsersView.vue'),
      meta: { requiresAdmin: true },
    },
    {
      path: '/invites',
      name: 'invites',
      component: () => import('@/views/InvitesView.vue'),
      meta: { requiresAdmin: true },
    },
    {
      path: '/audit',
      name: 'audit',
      component: () => import('@/views/AuditView.vue'),
      meta: { requiresAdmin: true },
    },
    // 未匹配的地址一律回概览，避免刷新时白屏
    { path: '/:pathMatch(.*)*', redirect: '/dashboard' },
  ],
})

router.beforeEach(async (to) => {
  if (!to.meta.requiresAdmin) return true

  const session = useSessionStore()
  // 首屏必须先问后端：在拿到答案之前，`phase` 是 unknown，任何判断都是猜的。
  await session.ensureLoaded()

  if (!session.isLoggedIn) {
    return {
      name: 'login',
      // 带上原本要去哪，登录成功后送回去；首页本身不必带，免得出现 redirect=/
      query: to.fullPath === '/' ? {} : { redirect: to.fullPath },
    }
  }
  if (!session.isAdmin) return { name: 'forbidden' }
  return true
})
