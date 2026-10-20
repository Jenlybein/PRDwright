import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import DocPhaseProgress from '../components/ui/DocPhaseProgress.vue'

/**
 * 下游文档（接口文档 / 提示词）生成中的阶段条。
 *
 * 这里守一条容易漏的口径：**快捷流程不审不改，阶段条上就不能有「审核 / 改写」两格**。
 * 那两格永远走不到，画在地图上的路走不通，用户会读成「还没审到」而不是「这次不审」。
 */

/** 胶囊上的文字（每个 `li` 里的第一个 span） */
function chips(wrapper: ReturnType<typeof mount>): string[] {
  return wrapper.findAll('ol li').map((li) => li.find('span').text().trim())
}

describe('DocPhaseProgress：整篇生成的阶段胶囊', () => {
  it('默认四格：生成 → 审核 → 改写 → 完成', () => {
    const wrapper = mount(DocPhaseProgress, { props: { phase: 'writing' } })

    expect(chips(wrapper)).toEqual(['生成', '审核', '改写', '完成'])
  })

  it('不复核时只剩「生成 → 完成」（快捷流程）', () => {
    const wrapper = mount(DocPhaseProgress, {
      props: { phase: 'writing', withReview: false },
    })

    expect(chips(wrapper)).toEqual(['生成', '完成'])
  })

  it('不复核时不会把「完成」提前点亮（防御：真收到审核阶段也当成还在生成）', () => {
    const wrapper = mount(DocPhaseProgress, {
      props: { phase: 'reviewing', withReview: false },
    })

    const [generating, done] = wrapper.findAll('ol li').map((li) => li.find('span').classes())
    // 当前高亮（ring）落在第一格，而不是把「完成」点亮——那是假信号
    expect(generating).toContain('ring-primary-100')
    expect(done).not.toContain('ring-primary-100')
  })
})
