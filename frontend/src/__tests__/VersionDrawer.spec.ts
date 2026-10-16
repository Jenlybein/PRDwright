import { mount, type VueWrapper } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import VersionDrawer from '../components/ui/VersionDrawer.vue'
import type { DocumentVersionSummary } from '../types'

/**
 * 版本抽屉测试。
 *
 * 它是**纯展示**组件：不 import store，列表 / 加载态 / 错误都由 props 进来，
 * 动作以事件出去。因此这里只钉三件事：
 *
 * 1. 收起态不可交互（`translate-x-full` 滑出屏幕之外，且标了 `aria-hidden`）；
 * 2. 列表把「第几版 · 怎么来的 · 当前是哪个」讲清楚，且**当前版本不给「恢复」按钮**
 *    （点了也是原地不动，给按钮只会让人以为能回到过去）；
 * 3. 两个动作各自把载荷抛出去——尤其是「存为新版本」带不带备注都要能提交。
 */

function version(overrides: Partial<DocumentVersionSummary> = {}): DocumentVersionSummary {
  return {
    versionId: 11,
    versionNo: 2,
    source: 'prd',
    note: '',
    contentSha: 'sha',
    contentLength: 128,
    createdAt: '2026-10-16T02:00:00',
    isCurrent: false,
    ...overrides,
  }
}

function mountDrawer(props: Record<string, unknown> = {}): VueWrapper {
  return mount(VersionDrawer, {
    props: {
      open: true,
      title: 'PRD 文档',
      versions: [],
      ...props,
    },
  })
}

/** 按文案找按钮 */
function buttonByText(wrapper: VueWrapper, text: string) {
  const button = wrapper.findAll('button').find((item) => item.text() === text)
  if (!button) throw new Error(`未找到按钮：${text}`)
  return button
}

describe('VersionDrawer 的展开与收起', () => {
  it('展开时滑入，收起时滑出并标为不可见', () => {
    const open = mountDrawer({ open: true })
    expect(open.find('aside').classes()).toContain('translate-x-0')
    expect(open.find('aside').attributes('aria-hidden')).toBe('false')

    const closed = mountDrawer({ open: false })
    expect(closed.find('aside').classes()).toContain('translate-x-full')
    expect(closed.find('aside').attributes('aria-hidden')).toBe('true')
  })

  it('点关闭按钮抛 close 事件', async () => {
    const wrapper = mountDrawer()

    await wrapper.find('button[aria-label="关闭版本历史"]').trigger('click')

    expect(wrapper.emitted('close')).toHaveLength(1)
  })

  it('按下 Esc 也关闭（抽屉浮在正文之上，键盘要能退出这一层）', async () => {
    const wrapper = mountDrawer()
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await wrapper.vm.$nextTick()

    expect(wrapper.emitted('close')).toHaveLength(1)
  })

  it('收起状态下按 Esc 不抛事件（没展开就无所谓关闭）', async () => {
    const wrapper = mountDrawer({ open: false })
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await wrapper.vm.$nextTick()

    expect(wrapper.emitted('close')).toBeUndefined()
  })
})

describe('VersionDrawer 的列表', () => {
  it('加载中显示读取提示，而不是「还没有任何版本」', () => {
    const wrapper = mountDrawer({ loading: true, versions: [] })

    expect(wrapper.text()).toContain('正在读取版本历史')
    expect(wrapper.text()).not.toContain('还没有任何版本')
  })

  it('没有版本时给出空态文案', () => {
    const wrapper = mountDrawer({ versions: [] })

    expect(wrapper.text()).toContain('还没有任何版本')
  })

  it('每一版给出序号、来源与备注；当前版本标「当前」', () => {
    const wrapper = mountDrawer({
      versions: [
        version({ versionId: 12, versionNo: 3, source: 'checkpoint', note: '定稿', isCurrent: true }),
        version({ versionId: 11, versionNo: 2, source: 'optimize' }),
      ],
    })

    expect(wrapper.text()).toContain('v3')
    expect(wrapper.text()).toContain('存为新版本')
    expect(wrapper.text()).toContain('定稿')
    expect(wrapper.text()).toContain('v2')
    expect(wrapper.text()).toContain('AI 优化')
    expect(wrapper.text()).toContain('当前')
  })

  it('当前版本不给「恢复」按钮，历史版本才给', () => {
    const wrapper = mountDrawer({
      versions: [
        version({ versionId: 12, versionNo: 3, isCurrent: true }),
        version({ versionId: 11, versionNo: 2 }),
      ],
    })

    const restoreButtons = wrapper.findAll('button').filter((b) => b.text() === '恢复此版本')
    expect(restoreButtons).toHaveLength(1)
  })

  it('未知来源原样显示（后端加了新取值也不至于看不见）', () => {
    const wrapper = mountDrawer({ versions: [version({ source: 'future-thing' })] })

    expect(wrapper.text()).toContain('future-thing')
  })

  it('错误态显示原因', () => {
    const wrapper = mountDrawer({ error: '无法读取版本历史' })

    expect(wrapper.text()).toContain('无法读取版本历史')
  })
})

describe('VersionDrawer 的两个动作', () => {
  it('点「恢复此版本」抛出该版本的 id', async () => {
    const wrapper = mountDrawer({ versions: [version({ versionId: 11, versionNo: 2 })] })

    await buttonByText(wrapper, '恢复此版本').trigger('click')

    expect(wrapper.emitted('restore')).toEqual([[11]])
  })

  it('「存为新版本」把备注抛出去', async () => {
    const wrapper = mountDrawer()
    await wrapper.find('input').setValue('  定稿  ')

    await buttonByText(wrapper, '存为新版本').trigger('click')

    // 备注两端空白被裁掉（后端也存的是裁过的）
    expect(wrapper.emitted('checkpoint')).toEqual([['定稿']])
  })

  it('备注留空也能提交（备注是可有可无的）', async () => {
    const wrapper = mountDrawer()

    await buttonByText(wrapper, '存为新版本').trigger('click')

    expect(wrapper.emitted('checkpoint')).toEqual([['']])
  })

  it('上层忙（生成中）时两个动作都禁用', () => {
    const wrapper = mountDrawer({
      busy: true,
      versions: [version({ versionId: 11, versionNo: 2 })],
    })

    expect(buttonByText(wrapper, '存为新版本').attributes('disabled')).toBeDefined()
    expect(buttonByText(wrapper, '恢复此版本').attributes('disabled')).toBeDefined()
  })

  it('重新打开时清空上一次填的备注', async () => {
    const wrapper = mountDrawer({ open: true })
    await wrapper.find('input').setValue('上一版的备注')

    await wrapper.setProps({ open: false })
    await wrapper.setProps({ open: true })

    expect((wrapper.find('input').element as HTMLInputElement).value).toBe('')
  })
})
