/**
 * 页面级路由。
 *
 * **只有两条路由**，而且它们描述的是「正在编辑哪一份方案」，不是「走到流程的哪一步」：
 *
 * - `/v2`：新建。还没有 id，首次保存成功后会 `replace` 到 `/v2/{id}`；
 * - `/v2/:id`：打开某一份已有方案。
 *
 * 九个阶段的流转**仍然由状态机（`stores/workflow.ts`）驱动，不映射到 URL**——
 * 这样既避免了「直接访问中间步骤但数据不存在」这类非法态，也让刷新后的落点
 * 由「后端恢复了什么」而不是「地址栏写了什么」决定（见 ADR-006 修订）。
 *
 * history 模式而非 hash：地址更干净，且开发时由 Vite 的 SPA fallback 处理刷新。
 */

import { createRouter, createWebHistory } from 'vue-router'

import AppV2 from '@/AppV2.vue'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/v2' },
    { path: '/v2', name: 'new', component: AppV2, props: { solutionId: null } },
    { path: '/v2/:id', name: 'solution', component: AppV2, props: true },
    // 未匹配的地址一律回到新建页，避免刷新时报 404 白屏
    { path: '/:pathMatch(.*)*', redirect: '/v2' },
  ],
})
