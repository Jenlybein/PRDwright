<script setup lang="ts">
/**
 * 接口文档页：把两个**管理员专属**的后端渲染页面摆成可打开的入口。
 *
 * 为什么这里只是「一页 + 两个按钮」，而不是侧栏直接放两个外链
 * ----------------------------------------------------------
 * `/api/admin/docs`（Swagger UI）与 `/api/admin/redoc`（ReDoc）不是后台的路由，
 * 它们是**后端直接渲染的 HTML**，靠同源 Cookie 认身份。直接把外链塞进侧栏会有两个问题：
 *
 * 1. 地址跳出了 `/admin/` 前缀，看起来像离开了后台；
 * 2. 「为什么公网打不开、谁能看」这件事没有地方解释，而它恰恰是最容易让人误解的
 *    （有人会以为后台还有一份公开文档）。
 *
 * 所以用这一页把三件事说清楚：入口在哪、为什么只有管理员能进、JSON 原文在哪。
 *
 * 为什么新标签页打开
 * ------------------
 * 这两个页面是独立的 HTML 文档（带自己的脚本与样式），在当前标签里打开会离开后台
 * 这个 SPA，浏览器后退回来还要重新拉一次后台的全部资源。新标签页既保留现场，
 * 也让「这是另一个页面」这件事在交互上自明。
 *
 * ⚠️ 这里**不做**任何权限判断：页面能不能打开由后端 `require_admin` 决定。
 * 前端藏按钮只是体验（门禁是体验，真边界在后端）。
 */
import PageHeader from '@/components/PageHeader.vue'

interface DocPage {
  key: string
  name: string
  code: string
  href: string
  summary: string
  note: string
}

const DOC_PAGES: DocPage[] = [
  {
    key: 'swagger',
    name: 'Swagger UI',
    code: 'SWAGGER',
    href: '/api/admin/docs',
    summary: '可展开接口参数与响应，并能直接发请求。',
    note: '试调会真的调用接口，包括写操作，别拿生产数据练手。',
  },
  {
    key: 'redoc',
    name: 'ReDoc',
    code: 'REDOC',
    href: '/api/admin/redoc',
    summary: '只读文档，按标签分组，没有试调按钮。',
    note: '静态渲染，不发业务请求。',
  },
]

/** 新标签页打开；`noopener` 让新页面拿不到 `window.opener`（防反向跳转劫持） */
function open(href: string): void {
  window.open(href, '_blank', 'noopener')
}
</script>

<template>
  <div class="space-y-5">
    <PageHeader kicker="API / 开发者" title="接口文档">
      <template #actions>
        <span class="brut-tag brut-tag-sky">仅管理员</span>
      </template>
      <template #desc>
        <p>使用当前管理员会话打开；未登录或非管理员无法访问。</p>
      </template>
    </PageHeader>

    <div class="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <section v-for="page in DOC_PAGES" :key="page.key" class="brut-card flex flex-col">
        <header class="flex items-center justify-between border-b-2 border-brut px-4 py-2">
          <h2 class="text-sm font-extrabold tracking-tight text-ink">{{ page.name }}</h2>
          <span class="kicker">{{ page.code }}</span>
        </header>

        <div class="flex flex-1 flex-col gap-3 px-4 py-3">
          <p class="text-sm leading-relaxed text-body">{{ page.summary }}</p>
          <p class="text-xs leading-relaxed text-muted">{{ page.note }}</p>

          <code class="block truncate border border-line bg-field px-2 py-1 font-mono text-xs text-muted">
            {{ page.href }}
          </code>

          <div class="mt-auto flex items-center justify-end pt-1">
            <button type="button" class="brut-btn brut-btn-primary" @click="open(page.href)">
              打开
            </button>
          </div>
        </div>
      </section>
    </div>

    <section class="brut-card">
      <header class="flex items-center justify-between border-b-2 border-brut px-4 py-2">
        <h2 class="text-sm font-extrabold tracking-tight text-ink">原始定义（JSON）</h2>
        <span class="kicker">OPENAPI 3</span>
      </header>
      <div class="space-y-2 px-4 py-3">
        <p class="text-sm leading-relaxed text-body">
          两个页面读同一份 schema；要接第三方工具，直接取这份 JSON。
        </p>
        <code class="block truncate border border-line bg-field px-2 py-1 font-mono text-xs text-muted">
          GET /api/admin/openapi.json
        </code>
        <p class="text-xs leading-relaxed text-muted">
          同样需要管理员身份；公开的 <code class="font-mono">/openapi.json</code> 已关闭。
        </p>
      </div>
    </section>
  </div>
</template>
