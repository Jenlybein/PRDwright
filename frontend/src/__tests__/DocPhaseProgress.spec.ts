import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import DocPhaseProgress from '../components/ui/DocPhaseProgress.vue'

/**
 * 文档生成中的阶段条（三份产物共用）。
 *
 * 守两条容易漏的口径：
 * 1. **三份产物的阶段顺序一致**：大纲 → 生成 →（审核 → 改写）→ 完成（票 #91）；
 * 2. **快捷流程不审不改，阶段条上就不能有「审核 / 改写」两格**。那两格永远走不到，
 *    画在地图上的路走不通，用户会读成「还没审到」而不是「这次不审」。
 */

/** 胶囊上的文字（每个 `li` 里的第一个 span） */
function chips(wrapper: ReturnType<typeof mount>): string[] {
  return wrapper.findAll('ol li').map((li) => li.find('span').text().trim())
}

/** 每格 span 的 class（判断哪一格在点亮） */
function chipClasses(wrapper: ReturnType<typeof mount>): string[][] {
  return wrapper.findAll('ol li').map((li) => li.find('span').classes())
}

describe('DocPhaseProgress：整篇生成的阶段胶囊', () => {
  it('默认五格：大纲 → 生成 → 审核 → 改写 → 完成', () => {
    const wrapper = mount(DocPhaseProgress, { props: { phase: 'outlining' } })

    expect(chips(wrapper)).toEqual(['大纲', '生成', '审核', '改写', '完成'])
  })

  it('大纲阶段点亮第一格，不是「生成」', () => {
    const wrapper = mount(DocPhaseProgress, { props: { phase: 'outlining' } })

    const [outlining, writing] = chipClasses(wrapper)
    expect(outlining).toContain('ring-primary-100')
    expect(writing).not.toContain('ring-primary-100')
  })

  it('不复核时只剩「大纲 → 生成 → 完成」（快捷流程）', () => {
    const wrapper = mount(DocPhaseProgress, {
      props: { phase: 'writing', withReview: false },
    })

    expect(chips(wrapper)).toEqual(['大纲', '生成', '完成'])
  })

  it('不复核时不会把「完成」提前点亮（防御：真收到审核阶段也当成还在生成）', () => {
    const wrapper = mount(DocPhaseProgress, {
      props: { phase: 'reviewing', withReview: false },
    })

    const [outlining, generating, done] = chipClasses(wrapper)
    // 当前高亮（ring）落在「生成」这一格，而不是把「完成」点亮——那是假信号
    expect(outlining).not.toContain('ring-primary-100')
    expect(generating).toContain('ring-primary-100')
    expect(done).not.toContain('ring-primary-100')
  })

  it('大纲降级后直接进撰写：阶段条把「生成」点亮，不报错、也不倒回大纲', () => {
    const wrapper = mount(DocPhaseProgress, { props: { phase: 'writing' } })

    const [outlining, writing] = chipClasses(wrapper)
    // 大纲与生成都算「走过」（淡色完成态），当前高亮在「生成」
    expect(outlining).not.toContain('ring-primary-100')
    expect(writing).toContain('ring-primary-100')
    // 没有出现错误提示类的元素
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  })
})
