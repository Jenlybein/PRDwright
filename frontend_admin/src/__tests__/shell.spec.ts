/**
 * 外壳（侧栏 + 顶栏）的用例。
 *
 * 这个文件盯的是**子菜单的开合**：
 *
 * - 「配置」点了只开合，**不跳页**——它没有自己的一屏，四个分区才是；
 * - 展开之后**不因换页而自动收起**。早先的版本跟路由联动（离开配置页就合上），
 *   结果点开配置再点「概览」，菜单当着面自己合上了：展开是人说「我要看这摊」，
 *   只有他再点一次才该收。
 */

import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'

import App from '@/App.vue'
import { flatNav } from '@/nav'

const Blank = { template: '<div />' }

/**
 * 路由表按 `NAV` 里的名字生成。
 *
 * ⚠️ 不能只造两三条：`<RouterLink :to="{ name: 'jobs' }">` 在解析时就会去查路由表，
 * 查不到直接抛——少一条路由，整棵侧栏都渲染不出来（而不是那一格空着）。
 */
const routes = [...new Set(flatNav.map((i) => i.name))].map((name) => ({
  path: `/${name}`,
  name,
  component: Blank,
  meta: { requiresAdmin: true },
}))

async function shellAt(path: string) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const router = createRouter({ history: createMemoryHistory(), routes })
  await router.push(path)
  await router.isReady()
  const wrapper = mount(App, { global: { plugins: [pinia, router] } })
  await wrapper.vm.$nextTick()
  return { wrapper, router }
}

/** 侧栏里的子菜单链接 */
function subLinks(wrapper: ReturnType<typeof mount>) {
  return wrapper.findAll('.brut-nav-sub a')
}

/** 「配置」那一行（它是按钮，不是链接） */
function configRow(wrapper: ReturnType<typeof mount>) {
  return wrapper.findAll('button.brut-nav-row').find((b) => b.text().includes('配置'))!
}

beforeEach(() => {
  setActivePinia(createPinia())
})

describe('后台外壳 · 侧栏子菜单', () => {
  it('配置下面挂着四个分区', async () => {
    const { wrapper } = await shellAt('/config?sec=model')
    expect(subLinks(wrapper).map((a) => a.text().replace(/\s+/g, ' ').trim())).toEqual([
      '模型配置MODEL',
      '模型生成配置GEN',
      '预算与额度QUOTA',
      '安全与登录AUTH',
    ])
  })

  it('点「配置」只开合，不跳页', async () => {
    const { wrapper, router } = await shellAt('/config?sec=model')
    const before = router.currentRoute.value.fullPath

    await configRow(wrapper).trigger('click')
    await wrapper.vm.$nextTick()

    // 收起了，但地址没动
    expect(subLinks(wrapper)).toHaveLength(0)
    expect(router.currentRoute.value.fullPath).toBe(before)

    await configRow(wrapper).trigger('click')
    await wrapper.vm.$nextTick()
    expect(subLinks(wrapper)).toHaveLength(4)
  })

  it('切到别的页面后，展开状态不变（不会自己收起）', async () => {
    const { wrapper, router } = await shellAt('/config?sec=model')
    expect(subLinks(wrapper)).toHaveLength(4)

    await router.push('/dashboard')
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()

    // 人没点过「配置」，它就得一直开着
    expect(subLinks(wrapper)).toHaveLength(4)
  })

  it('当前所在的分区会被标成选中，父项只标成分支', async () => {
    const { wrapper, router } = await shellAt('/config?sec=security')
    await wrapper.vm.$nextTick()

    const active = wrapper.findAll('.brut-nav-row.is-active')
    expect(active).toHaveLength(1)
    expect(active[0]!.text()).toContain('安全与登录')
    // 父项「配置」是分支态，不是选中态——两组「选中」并排会让人以为选了两个页面
    expect(configRow(wrapper).classes()).toContain('is-branch')
    expect(configRow(wrapper).classes()).not.toContain('is-active')

    await router.push('/dashboard')
    await wrapper.vm.$nextTick()
    const nowActive = wrapper.findAll('.brut-nav-row.is-active')
    expect(nowActive).toHaveLength(1)
    expect(nowActive[0]!.text()).toContain('概览')
  })
})
