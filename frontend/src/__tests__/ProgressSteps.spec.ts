import { mount, type VueWrapper } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ProgressSteps from '../components/stages/ProgressSteps.vue'
import { stepsForMode } from '../utils/workflow'

/**
 * 步骤进度条的呈现：**「AI 对话」被摘到最左边**，与其余四个里程碑用一根竖线隔开。
 *
 * ```text
 * [AI 对话] │ [1 描述产品] —— [2 PRD] —— [3 接口文档] —— [4 提示词]
 * ```
 *
 * 这里守两类东西，都很容易被后来的重构弄坏：
 *
 * 1. **呈现**：对话那一段排在里程碑之前、**不占序号**（用气泡图标），里程碑的序号是
 *    画面顺序的 1~4（不是原始下标留下的 1/3/4/5 —— 后者看起来像进度条算错了）。
 * 2. **口径**：`currentIndex` / `reachedIndex` 一律是**原始下标**。把对话摘到最左之后，
 *    画面位置与原始下标的对应关系整个错开一格（对话：原始 1 / 画面 0；描述产品：原始 0 /
 *    画面 1），所以「当前高亮落在谁身上」「谁能点」必须仍按原始下标算。用画面位置算的实现
 *    会在「用户正停在对话页」时去高亮「描述产品」——差一格，肉眼很像、但不是。
 */

/** 完整流程的真实步骤口径（直接从状态机取，避免用例里手抄一份对不上的列表） */
const STRUCTURED = stepsForMode('structured')

function mountSteps(overrides: Partial<{ currentIndex: number; reachedIndex: number }> = {}) {
  return mount(ProgressSteps, {
    props: {
      steps: STRUCTURED,
      currentIndex: 0,
      reachedIndex: 0,
      ...overrides,
    },
  })
}

/** 按文案找那一步（进度条上的步骤是带 @click 的 div，不是 button） */
function stepItem(wrapper: VueWrapper, label: string) {
  const item = wrapper.findAll('nav li').find((li) => li.text().includes(label))
  if (!item) throw new Error(`未找到进度步骤：${label}`)
  return item
}

/** 点击某一步 */
async function clickStep(wrapper: VueWrapper, label: string): Promise<void> {
  await stepItem(wrapper, label).find('div').trigger('click')
}

/** 各步徽章上的文字：里程碑是序号，对话那一段是图标（因此为空串） */
function badgeTexts(wrapper: VueWrapper): string[] {
  return wrapper.findAll('nav li').map((li) => li.findAll('span')[0]?.text().trim() ?? '')
}

describe('ProgressSteps：把「AI 对话」摘到最左边', () => {
  it('对话那一段排在四个里程碑之前，且不占序号', () => {
    const wrapper = mountSteps()

    // 六步一个不少（数量不变，只是排布变了）
    expect(wrapper.findAll('nav li')).toHaveLength(6)
    // 画面顺序：对话在最左，其余保持执行顺序
    expect(wrapper.findAll('nav li').map((li) => li.text().split('\n')[0]?.trim())).toEqual([
      'AI 对话',
      '1描述产品',
      '2PRD',
      '3接口文档',
      '4提示词',
      '5交付',
    ])
  })

  it('序号按画面顺序重数（1~5），对话那一段没有数字', () => {
    const wrapper = mountSteps()

    // 空串 = 那一步的徽章里只有图标、没有序号
    expect(badgeTexts(wrapper)).toEqual(['', '1', '2', '3', '4', '5'])
  })

  it('对话与里程碑之间隔了一根竖线，里程碑之间没有', () => {
    const wrapper = mountSteps()

    const items = wrapper.findAll('nav li')
    expect(items[0]?.classes()).toContain('border-r')
    for (const item of items.slice(1)) {
      expect(item.classes()).not.toContain('border-r')
    }
  })

  it('对话段落与里程碑之间不画连接线（那根竖线就是分隔）', () => {
    const wrapper = mountSteps()

    // 连接线只在里程碑之间：五步 → 4 条
    const connectors = wrapper.findAll('nav ol > li > div.mx-1')
    expect(connectors).toHaveLength(4)
    // 对话那一条（第一项）里没有连接线
    expect(wrapper.findAll('nav li')[0]?.findAll('div.mx-1')).toHaveLength(0)
  })

  it('快捷流程也有「AI 对话」那一格（它承载文档澄清），步数与完整流程一致', () => {
    const quick = mount(ProgressSteps, {
      props: { steps: stepsForMode('prd-quick'), currentIndex: 0, reachedIndex: 0 },
    })
    // 六步齐全：对话段在最左（不占序号），里程碑 1~5
    expect(badgeTexts(quick)).toEqual(['', '1', '2', '3', '4', '5'])
    expect(quick.findAll('nav li').map((li) => li.text().split('\n')[0]?.trim())).toEqual([
      'AI 对话',
      '1描述产品',
      '2PRD',
      '3接口文档',
      '4提示词',
      '5交付',
    ])
    // 有对话段就有那条分隔线（分隔线挂在对话段右侧）
    expect(quick.findAll('nav li')[0]?.classes()).toContain('border-r')

    // 导入入口现在是「提示词 → 交付」两步：仍然渲染，但只有两个序号
    const imported = mount(ProgressSteps, {
      props: { steps: stepsForMode('prompt-debug'), currentIndex: 0, reachedIndex: 0 },
    })
    expect(imported.find('nav').exists()).toBe(true)
    expect(badgeTexts(imported)).toEqual(['1', '2'])
  })
})

describe('ProgressSteps：高亮与可点范围仍按原始下标（摘出去之后没有错格）', () => {
  it('停在对话页时高亮的是对话那一段，而不是画面上的第二个', () => {
    // 「AI 对话」的原始下标是 1；若实现误用画面位置，会高亮到「描述产品」
    const wrapper = mountSteps({ currentIndex: 1, reachedIndex: 1 })

    expect(stepItem(wrapper, 'AI 对话').find('span').classes()).toContain('ring-accent-soft')
    expect(stepItem(wrapper, '描述产品').find('span').classes()).not.toContain('ring-accent-soft')
  })

  it('已到达的对话段可以点回去，未到达时点了没有任何反应', async () => {
    const reachable = mountSteps({ currentIndex: 3, reachedIndex: 3 })
    await clickStep(reachable, 'AI 对话')
    expect(reachable.emitted('select')).toEqual([['chatting']])

    // 还没聊过（reachedIndex 停在 0）时对话不可点——与改动前的口径一致
    const unreachable = mountSteps({ currentIndex: 0, reachedIndex: 0 })
    await clickStep(unreachable, 'AI 对话')
    expect(unreachable.emitted('select')).toBeUndefined()
  })

  it('可点范围按原始下标算：到达 PRD 时，接口文档与提示词都还点不动', async () => {
    const wrapper = mountSteps({ currentIndex: 2, reachedIndex: 2 })

    await clickStep(wrapper, '描述产品')
    await clickStep(wrapper, 'AI 对话')
    await clickStep(wrapper, 'PRD')
    await clickStep(wrapper, '接口文档')
    await clickStep(wrapper, '提示词')

    expect(wrapper.emitted('select')).toEqual([['form'], ['chatting'], ['review-prd']])
  })
})
