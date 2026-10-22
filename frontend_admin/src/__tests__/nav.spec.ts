/**
 * 后台导航（含配置页分区）的用例。
 *
 * 这个文件的重心是**两处一致性**：
 *
 * 1. 侧栏子菜单与配置页的分区是同一份数据——改一边忘了另一边，
 *    症状是「菜单里叫模型生成配置、点进去标题叫别的」；
 * 2. 后端每一个配置分组都必须落进某个分区。漏一个不会报错，
 *    只会让那一组配置**从子菜单里消失**（只有直接改 URL 才看得到）。
 *
 * ⚠️ 下面这份 `BACKEND_GROUPS` 是后端 `admin_config_service._GROUPS` 的键。
 * 后端新增分组时这条会红——那是提醒你去 `nav.ts` 里给它找个归宿，不是误报。
 */

import { describe, expect, it } from 'vitest'

import { CONFIG_SECTIONS, DEFAULT_SECTION, NAV, flatNav, sectionOf } from '@/nav'

/** 与后端 admin_config_service._GROUPS 对齐 */
const BACKEND_GROUPS = ['prd', 'review', 'llm', 'quota', 'auth']

describe('后台导航', () => {
  it('「配置」下面有四个子菜单项，且没有「全部」', () => {
    const config = NAV.flatMap((g) => g.items).find((i) => i.name === 'config')
    expect(config?.children?.map((c) => c.label)).toEqual([
      '模型配置',
      '模型生成配置',
      '预算与额度',
      '安全与登录',
    ])
    // 「全部」会让四摊参数重新挤回一屏，那就等于没分
    expect(config?.children?.some((c) => c.label === '全部')).toBe(false)
  })

  it('子菜单项与配置页的分区是同一份数据（标签一一对应）', () => {
    const children = NAV.flatMap((g) => g.items).find((i) => i.name === 'config')?.children ?? []
    expect(children.map((c) => c.label)).toEqual(CONFIG_SECTIONS.map((s) => s.label))
    expect(children.map((c) => c.sec)).toEqual(CONFIG_SECTIONS.map((s) => s.key))
  })

  it('后端每个配置分组都落进了某个分区', () => {
    const covered = CONFIG_SECTIONS.flatMap((s) => s.groups)
    for (const key of BACKEND_GROUPS) {
      expect(covered).toContain(key)
    }
  })

  it('「配置」本身没有去处：点它只展开，不跳页', () => {
    // 判据是它没有出现在任何「可跳转」的扁平列表里
    const jumpable = flatNav.filter((i) => i.name === 'config')
    expect(jumpable.length).toBe(CONFIG_SECTIONS.length)
    // 每一项都带 sec —— 没有 sec 的那一项就是「配置」自己，它不该在这
    expect(jumpable.every((i) => Boolean(i.sec))).toBe(true)
  })

  it('不认识的 ?sec= 回落到兜底分区，不会白屏', () => {
    expect(sectionOf('nope').key).toBe(DEFAULT_SECTION.key)
    expect(sectionOf('').key).toBe(DEFAULT_SECTION.key)
    expect(sectionOf(undefined).key).toBe(DEFAULT_SECTION.key)
    expect(DEFAULT_SECTION.key).toBe('model')
  })

  it('每个页面的路由名都在路由表里存在（扁平导航里没有野指针）', () => {
    // 这里只查「名字不为空、且带 sec 的必然是 config」——
    // 真正的路由存在性由 guard.spec.ts 覆盖，本文件不重复。
    for (const item of flatNav) {
      expect(item.name.length).toBeGreaterThan(0)
      if (item.sec) expect(item.name).toBe('config')
    }
  })
})
