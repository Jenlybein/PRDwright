import { mount, type VueWrapper } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ChatThreadTabs from '../components/ui/ChatThreadTabs.vue'
import { CHAT_STAGES, type ChatStage } from '../types'

/**
 * 对话页的子标签栏。
 *
 * 它本身很薄（只负责画与报点），但有一处**设计取舍必须钉住**：标签是**常驻**的——
 * 不可用的置灰、给出理由，而不是消失。这样用户才知道「原来接口文档也能这样聊」。
 * 若哪天有人为了「界面清爽」把不可用的标签过滤掉，这条用例会亮红灯。
 *
 * 另一处是**当前标签的实心态**：请求进行中全部禁用时，仍要能看出「我现在在哪一条」，
 * 否则用户会怀疑自己是不是掉出了当前线程。
 */

const ALL = [...CHAT_STAGES]

function mountTabs(props: Record<string, unknown> = {}): VueWrapper {
  return mount(ChatThreadTabs, {
    props: { tabs: ALL, active: 'requirement', available: ALL, ...props },
  })
}

/** 按文案取标签按钮 */
function tabOf(wrapper: VueWrapper, label: string) {
  const tab = wrapper.findAll('[role="tab"]').find((item) => item.text() === label)
  if (!tab) throw new Error(`未找到标签：${label}`)
  return tab
}

function tabTexts(wrapper: VueWrapper): string[] {
  return wrapper.findAll('[role="tab"]').map((item) => item.text())
}

describe('ChatThreadTabs', () => {
  it('四条线程常驻且顺序固定（需求 → PRD → 接口 → 提示词）', () => {
    const wrapper = mountTabs()
    expect(tabTexts(wrapper)).toEqual(['需求澄清', 'PRD 澄清', '接口澄清', '提示词澄清'])
    expect(wrapper.attributes('role')).toBe('tablist')
    expect(wrapper.attributes('aria-label')).toBe('澄清线程')
  })

  it('不可用的线程照样渲染，只是置灰并给出理由（标签就是地图）', () => {
    const wrapper = mountTabs({ available: ['requirement'] })

    // 关键：四条都在——过滤掉的话用户就不知道后面还有别的线程
    expect(tabTexts(wrapper)).toHaveLength(4)
    expect((tabOf(wrapper, '需求澄清').element as HTMLButtonElement).disabled).toBe(false)
    const prd = tabOf(wrapper, 'PRD 澄清')
    expect((prd.element as HTMLButtonElement).disabled).toBe(true)
    // 禁用时要说明为什么点不动，而不是让人猜
    expect(prd.attributes('title')).toBe('这条澄清线程还用不了')
  })

  it('点击只报出目标线程，不改自己的状态（纯展示组件）', async () => {
    const wrapper = mountTabs({ available: ALL })
    await tabOf(wrapper, '接口澄清').trigger('click')

    expect(wrapper.emitted('select')).toEqual([['api-docs-clarify']])
  })

  it('请求进行中全部禁用，连当前标签也点不动（但仍是实心态）', () => {
    const wrapper = mountTabs({ active: 'prompts-clarify', available: ALL, busy: true })

    for (const label of tabTexts(wrapper)) {
      expect((tabOf(wrapper, label).element as HTMLButtonElement).disabled, label).toBe(true)
      expect(tabOf(wrapper, label).attributes('title')).toBe('正在处理中，请稍候')
    }
    // 当前标签保持实心：它是「你现在在哪」，淡下去会让人怀疑自己掉出了当前线程
    expect(tabOf(wrapper, '提示词澄清').classes()).toContain('bg-surface')
  })

  it('只有当前标签带 aria-selected，其余都不带', () => {
    const wrapper = mountTabs({ active: 'api-docs-clarify' })
    for (const label of tabTexts(wrapper)) {
      const selected = tabOf(wrapper, label).attributes('aria-selected')
      expect(selected, label).toBe(label === '接口澄清' ? 'true' : 'false')
    }
  })

  it('只画出传进来的那些标签（标签集合由上层给）', () => {
    const subset: ChatStage[] = ['requirement', 'prd-clarify']
    const wrapper = mountTabs({ tabs: subset, available: subset })
    expect(tabTexts(wrapper)).toEqual(['需求澄清', 'PRD 澄清'])
  })
})
