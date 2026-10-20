import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import PrdProgress from '../components/stages/PrdProgress.vue'
import type { PrdBoardState, PrdSectionCard, PrdSectionPhase } from '../types'

function card(index: number, phase: PrdSectionPhase, failed = false): PrdSectionCard {
  return {
    sectionId: `s${index}`,
    sectionTitle: `章节 ${index + 1}`,
    sectionHeading: `## ${index + 1}`,
    index,
    phase,
    failed,
    text: '',
    note: '',
    issues: [],
  }
}

function board(overrides: Partial<PrdBoardState> = {}): PrdBoardState {
  return {
    total: 6,
    headerReady: true,
    headerText: '# 文档头\n\n',
    sections: [],
    review: null,
    partial: false,
    failedSections: [],
    ...overrides,
  }
}

describe('PrdProgress：4 列 × 2 行管道看板', () => {
  it('只有两行：4 个车道头 + 4 个章节号格', () => {
    const wrapper = mount(PrdProgress, {
      props: {
        board: board({
          sections: [
            card(3, 'writing'),
            card(1, 'reviewing'),
            card(2, 'reviewing'),
            card(0, 'rewriting'),
          ],
        }),
      },
    })

    const grid = wrapper.find('.grid')
    expect(grid.classes()).toContain('grid-cols-4')
    // 4 个头 + 4 个身体格 = 8，正好两行
    expect(grid.element.children).toHaveLength(8)

    const text = wrapper.text()
    expect(text).toContain('生成')
    expect(text).toContain('审核')
    expect(text).toContain('改写')
    expect(text).toContain('定稿')
    // 章节号按车道归位
    expect(text).toContain('④')
    expect(text).toContain('②')
    expect(text).toContain('③')
    expect(text).toContain('①')
    expect(text).toContain('0/6')
  })

  it('空车道显示 —，失败章节停在车道里并带 !', () => {
    const wrapper = mount(PrdProgress, {
      props: {
        board: board({
          sections: [card(0, 'writing', true), card(1, 'done')],
        }),
      },
    })

    const text = wrapper.text()
    // 审核 / 改写两个空车道
    expect(text.split('—').length - 1).toBeGreaterThanOrEqual(2)
    expect(text).toContain('①!')
    expect(text).toContain('1/6')
  })

  it('不复核时只剩「生成 / 定稿」两条车道（快捷流程：不审不改）', () => {
    const wrapper = mount(PrdProgress, {
      props: {
        board: board({ sections: [card(0, 'writing'), card(1, 'done')] }),
        withReview: false,
      },
    })

    const grid = wrapper.find('.grid')
    expect(grid.classes()).toContain('grid-cols-2')
    // 2 个头 + 2 个身体格 = 4
    expect(grid.element.children).toHaveLength(4)
    // 那两条车道不是「还没跑到」，而是这次根本不跑——画出来是假地图
    expect(wrapper.text()).not.toContain('审核')
    expect(wrapper.text()).not.toContain('改写')
    expect(wrapper.text()).toContain('生成')
    expect(wrapper.text()).toContain('定稿')
  })

  it('全部定稿时定稿计数为 6/6，其余车道为空', () => {
    const wrapper = mount(PrdProgress, {
      props: {
        board: board({
          sections: Array.from({ length: 6 }, (_, index) => card(index, 'done')),
        }),
      },
    })

    expect(wrapper.text()).toContain('6/6')
    expect(wrapper.text()).toContain('—')
  })
})
