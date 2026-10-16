import { mount, type VueWrapper } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import VersionDrawer from '../components/ui/VersionDrawer.vue'
import type { DocumentVersionDetail, DocumentVersionSummary } from '../types'

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
  it('点「恢复此版本」先出确认，确认了才抛出该版本的 id', async () => {
    const wrapper = mountDrawer({ versions: [version({ versionId: 11, versionNo: 2 })] })

    await buttonByText(wrapper, '恢复此版本').trigger('click')
    expect(wrapper.emitted('restore')).toBeUndefined()
    expect(wrapper.text()).toContain('恢复这一版？')

    await buttonByText(wrapper, '确认恢复').trigger('click')

    expect(wrapper.emitted('restore')).toEqual([[11]])
  })

  it('恢复的确认可以取消（点「取消」后不抛事件，按钮回到原位）', async () => {
    const wrapper = mountDrawer({ versions: [version({ versionId: 11, versionNo: 2 })] })
    await buttonByText(wrapper, '恢复此版本').trigger('click')

    await buttonByText(wrapper, '取消').trigger('click')

    expect(wrapper.emitted('restore')).toBeUndefined()
    expect(buttonByText(wrapper, '恢复此版本')).toBeTruthy()
  })

  it('恢复与删除的确认互斥：开了一个，另一个自动收掉', async () => {
    const wrapper = mountDrawer({ versions: [version({ versionId: 11, versionNo: 2 })] })

    await buttonByText(wrapper, '删除').trigger('click')
    expect(wrapper.text()).toContain('删除这一版？')

    await buttonByText(wrapper, '取消').trigger('click')
    await buttonByText(wrapper, '恢复此版本').trigger('click')

    expect(wrapper.text()).toContain('恢复这一版？')
    expect(wrapper.text()).not.toContain('删除这一版？')
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

// ---------------------------------------------------------------------------
// 预览态：点某一版，抽屉加宽显示那一版的只读正文
// ---------------------------------------------------------------------------

/** 造一份版本详情（摘要 + 正文 + 可选备注 / 清单） */
function detail(overrides: Partial<DocumentVersionDetail> = {}): DocumentVersionDetail {
  return {
    versionId: 11,
    versionNo: 2,
    source: 'checkpoint',
    note: '',
    createdAt: '2026-10-16T02:00:00',
    isCurrent: false,
    content: '# 第二版正文\n\n这是内容。',
    pendingItems: [],
    runSummary: null,
    updatedAt: '2026-10-16T02:00:00',
    ...overrides,
  }
}

describe('VersionDrawer 的预览态', () => {
  it('点条目主体抛出 preview 事件（进预览）', async () => {
    const wrapper = mountDrawer({ versions: [version({ versionId: 11, versionNo: 2 })] })

    await wrapper.find('button[aria-label="预览 v2"]').trigger('click')

    expect(wrapper.emitted('preview')).toEqual([[11]])
  })

  it('预览态：抽屉加宽到 640px，标题变「版本预览」', () => {
    const wrapper = mountDrawer({ versions: [version()], preview: detail() })

    expect(wrapper.find('aside').classes()).toContain('w-[640px]')
    expect(wrapper.text()).toContain('版本预览')
    expect(wrapper.text()).not.toContain('版本历史')
  })

  it('列表态：抽屉是 360px 窄栏（预览才加宽）', () => {
    const wrapper = mountDrawer({ versions: [version()] })

    expect(wrapper.find('aside').classes()).toContain('w-[360px]')
  })

  it('预览正文渲染成 Markdown（不是把源码当纯文本摆出来）', () => {
    const wrapper = mountDrawer({ versions: [version()], preview: detail() })

    const body = wrapper.find('.prose')
    expect(body.exists()).toBe(true)
    expect(body.find('h1').text()).toBe('第二版正文')
    expect(body.find('p').text()).toBe('这是内容。')
  })

  it('预览态给出该版的元信息：第几版 / 来源 / 字数 / 备注', () => {
    const wrapper = mountDrawer({
      versions: [version()],
      preview: detail({ versionNo: 3, source: 'optimize', note: '定稿', content: '12345' }),
    })

    expect(wrapper.text()).toContain('v3')
    expect(wrapper.text()).toContain('AI 优化')
    expect(wrapper.text()).toContain('约 5 字')
    expect(wrapper.text()).toContain('定稿')
  })

  it('载入中显示读取提示，而不是空白或「还没有版本」', () => {
    const wrapper = mountDrawer({ versions: [version()], previewLoading: true })

    expect(wrapper.text()).toContain('正在读取该版本')
    expect(wrapper.find('.prose').exists()).toBe(false)
  })

  it('预览失败显示原因', () => {
    const wrapper = mountDrawer({ versions: [version()], previewError: '版本不存在' })

    expect(wrapper.text()).toContain('版本不存在')
  })

  it('「返回列表」抛出 closePreview', async () => {
    const wrapper = mountDrawer({ versions: [version()], preview: detail() })

    await buttonByText(wrapper, '返回列表').trigger('click')

    expect(wrapper.emitted('closePreview')).toHaveLength(1)
  })

  it('预览态按 Esc 先退回列表，而不是直接关抽屉', async () => {
    const wrapper = mountDrawer({ versions: [version()], preview: detail() })

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await wrapper.vm.$nextTick()

    expect(wrapper.emitted('closePreview')).toHaveLength(1)
    expect(wrapper.emitted('close')).toBeUndefined()
  })

  it('预览非当前版本时给「恢复此版本」，且同样要二次确认', async () => {
    const wrapper = mountDrawer({ versions: [version()], preview: detail({ versionId: 11 }) })

    await buttonByText(wrapper, '恢复此版本').trigger('click')
    expect(wrapper.emitted('restore')).toBeUndefined()

    await buttonByText(wrapper, '确认恢复').trigger('click')

    expect(wrapper.emitted('restore')).toEqual([[11]])
  })

  it('预览当前版本时不给「恢复此版本」（点了也是原地不动）', () => {
    const wrapper = mountDrawer({ versions: [version()], preview: detail({ isCurrent: true }) })

    expect(wrapper.findAll('button').filter((b) => b.text() === '恢复此版本')).toHaveLength(0)
  })

  it('预览态不展示「存为新版本」表单（那一栏属于列表）', () => {
    const wrapper = mountDrawer({ versions: [version()], preview: detail() })

    expect(wrapper.find('input').exists()).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// 删除：只删历史版本，且必须先确认
// ---------------------------------------------------------------------------

describe('VersionDrawer 的删除', () => {
  it('点「删除」不直接抛事件，先出确认', async () => {
    const wrapper = mountDrawer({ versions: [version({ versionId: 11 })] })

    await buttonByText(wrapper, '删除').trigger('click')

    expect(wrapper.emitted('remove')).toBeUndefined()
    expect(wrapper.text()).toContain('删除这一版？')
  })

  it('确认后才抛 remove（带该版本的 id）', async () => {
    const wrapper = mountDrawer({ versions: [version({ versionId: 11 })] })
    await buttonByText(wrapper, '删除').trigger('click')

    await buttonByText(wrapper, '确认删除').trigger('click')

    expect(wrapper.emitted('remove')).toEqual([[11]])
  })

  it('点「取消」撤掉确认，不抛 remove', async () => {
    const wrapper = mountDrawer({ versions: [version({ versionId: 11 })] })
    await buttonByText(wrapper, '删除').trigger('click')

    await buttonByText(wrapper, '取消').trigger('click')

    expect(wrapper.emitted('remove')).toBeUndefined()
    expect(buttonByText(wrapper, '删除')).toBeTruthy()
  })

  it('当前版本没有「删除」按钮，并说明原因', () => {
    const wrapper = mountDrawer({ versions: [version({ versionId: 12, isCurrent: true })] })

    expect(wrapper.findAll('button').filter((b) => b.text() === '删除')).toHaveLength(0)
    expect(wrapper.text()).toContain('当前版本不能删除或覆盖')
  })

  it('上层忙时「删除」与「确认删除」都禁用', async () => {
    const busyWrapper = mountDrawer({ busy: true, versions: [version({ versionId: 11 })] })
    expect(buttonByText(busyWrapper, '删除').attributes('disabled')).toBeDefined()

    const wrapper = mountDrawer({ versions: [version({ versionId: 11 })] })
    await buttonByText(wrapper, '删除').trigger('click')
    expect(buttonByText(wrapper, '确认删除').attributes('disabled')).toBeUndefined()
  })
})

// ---------------------------------------------------------------------------
// 预览热区：整张卡片都能点，不再只有标题那一行
// ---------------------------------------------------------------------------

describe('VersionDrawer 的预览热区', () => {
  it('热区铺满整张卡片（不再只有标题那一行可点）', () => {
    const wrapper = mountDrawer({ versions: [version({ versionNo: 2 })] })

    const hot = wrapper.find('button[aria-label="预览 v2"]')
    expect(hot.exists()).toBe(true)
    expect(hot.classes()).toContain('absolute')
    expect(hot.classes()).toContain('inset-0')
  })

  it('内容层让点击穿透到热区，动作按钮自己收回来', () => {
    const wrapper = mountDrawer({ versions: [version()] })

    // 内容层 pointer-events-none：点文字、点空白都落到下面的热区
    expect(wrapper.find('li > div').classes()).toContain('pointer-events-none')
    // 动作组 pointer-events-auto：两个按钮照旧可点
    const actions = wrapper.findAll('div').find((item) => item.classes().includes('pointer-events-auto'))
    expect(actions).toBeTruthy()
  })

  it('当前版本没有热区，内容层也不需要穿透', () => {
    const wrapper = mountDrawer({ versions: [version({ versionNo: 3, isCurrent: true })] })

    expect(wrapper.find('button[aria-label="预览 v3"]').exists()).toBe(false)
    expect(wrapper.find('li > div').classes()).not.toContain('pointer-events-none')
  })

  it('只有历史版本才有热区：一列表里当前版本与历史版本混排也分得清', () => {
    const wrapper = mountDrawer({
      versions: [version({ versionId: 12, versionNo: 3, isCurrent: true }), version({ versionId: 11, versionNo: 2 })],
    })

    const hotZones = wrapper
      .findAll('button')
      .filter((item) => (item.attributes('aria-label') ?? '').startsWith('预览 '))
    expect(hotZones.map((item) => item.attributes('aria-label'))).toEqual(['预览 v2'])
  })
})

describe('VersionDrawer 的 Esc 逐层退出', () => {
  it('有待确认的动作时，第一次 Esc 只撤掉确认', async () => {
    const wrapper = mountDrawer({ versions: [version({ versionId: 11 })] })
    await buttonByText(wrapper, '删除').trigger('click')
    expect(wrapper.text()).toContain('删除这一版？')

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await wrapper.vm.$nextTick()

    expect(wrapper.text()).not.toContain('删除这一版？')
    expect(wrapper.emitted('remove')).toBeUndefined()
    expect(wrapper.emitted('close')).toBeUndefined()
    expect(wrapper.emitted('closePreview')).toBeUndefined()
  })

  it('没有待确认动作时，Esc 才往上走：预览态退回列表、列表态关抽屉', async () => {
    const listWrapper = mountDrawer({ versions: [version()] })
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await listWrapper.vm.$nextTick()
    expect(listWrapper.emitted('close')).toHaveLength(1)
  })
})
