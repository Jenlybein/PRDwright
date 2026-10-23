/**
 * 管理端路由与「门禁」。
 *
 * 守卫只有一条规则：标了 `requiresAdmin` 的页面，必须先在服务端确认过
 * 「是管理员」才放行。两种结果对应两个去处：
 *
 * - 不是管理员（未登录 / 账号不存在 / 会话失效）→ 登录页（并记下原本想去哪）
 * - 是管理员 → 放行
 *
 * ⚠️ 只有两种去处是**有意的**（ADR-049）：后台不再区分「你没登录」与「你这个账号
 * 不是管理员」。对非管理员一律表现为「账号不存在」（后端给文案），停在登录页上
 * 比停在一页「你没有权限」更接近事实，也不会顺带确认「这个账号确实存在」。
 *
 * ⚠️ 这个守卫**不是安全边界**。它只是让正常用户少点一次、少看一个空白页；
 * 真正的拦截在后端 `require_admin` 与 `POST /api/admin/login`
 * （页面藏起来挡不住直接敲接口的人）。
 */

import { createRouter, createWebHistory } from 'vue-router'

import { DEFAULT_SEC } from '@/nav'
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
  // ⚠️ 必须显式传 `BASE_URL`，不能靠默认值。
  //
  // Vue Router 的 `normalizeBase()` 在**没传参**时读的是 HTML 里的 `<base>` 标签，
  // 再兜底 `/`——它**不会**跟随 Vite 的 `base`。两个「base」是两件事：
  // Vite 的 `base` 决定产物引用前缀，Vue Router 的 base 决定「地址栏里哪一段不算路由」。
  // 不接上这一句，生产打开 `/admin/dashboard` 会把整段当路由 → 匹配不到 → 空白页。
  //
  // `BASE_URL` 由 Vite 的 `base` 注入（生产与 dev 都是 `/admin/`，见 vite.config.ts）。
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    { path: '/', redirect: '/dashboard' },
    {
      path: '/login',
      name: 'login',
      // 懒加载：未登录的用户不该为了看登录页而下载整个后台的代码
      component: () => import('@/views/LoginView.vue'),
    },
    {
      path: '/dashboard',
      name: 'dashboard',
      component: () => import('@/views/DashboardView.vue'),
      meta: { requiresAdmin: true },
    },
    {
      path: '/jobs',
      name: 'jobs',
      component: () => import('@/views/JobsView.vue'),
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
    {
      path: '/logs',
      name: 'logs',
      component: () => import('@/views/LogsView.vue'),
      meta: { requiresAdmin: true },
    },
    {
      path: '/api-docs',
      name: 'api-docs',
      component: () => import('@/views/ApiDocsView.vue'),
      meta: { requiresAdmin: true },
    },
    {
      path: '/config',
      name: 'config',
      component: () => import('@/views/ConfigView.vue'),
      meta: { requiresAdmin: true },
      /**
       * 裸 `/config` 自动补上默认分区。
       *
       * ⚠️ 配置页只按分区显示（没有「全部」这一屏），所以「没有分区」不是一个
       * 合法的落点：留在那里，侧栏四个子项没有一个能高亮，刷新后也不知道自己
       * 原本在哪一屏。补上 `sec` 之后 URL 就始终是完整的。
       */
      beforeEnter: (to) => (to.query.sec ? true : { name: 'config', query: { sec: DEFAULT_SEC } }),
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

  if (!session.isAdmin) {
    return {
      name: 'login',
      // 带上原本要去哪，登录成功后送回去；首页本身不必带，免得出现 redirect=/
      query: to.fullPath === '/' ? {} : { redirect: to.fullPath },
    }
  }
  return true
})
