import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import DoneStage from '../components/stages/DoneStage.vue'
import GeneratingStage from '../components/stages/GeneratingStage.vue'

/**
 * 「返回」按钮的适用范围。
 *
 * 两个**从零创建**的入口（完整流程 / 快捷流程）录入页就在进度条第 1 步上，点一下就能回去，
 * 底栏再放一个「返回对话 / 返回描述」是纯重复；两个**导入**入口的录入页不在进度条上，
 * 必须留着，否则用户改不了贴进来的材料。
 *
 * 判据由上层算好（`inputPageOnBar`）后经 `hideBack` 传下来——这三个阶段视图各自守一条：
 * 生成中（整条栏都不渲染）、交付页（只藏按钮，「重新开始」还在）。
 */
describe('GeneratingStage / DoneStage 的「返回」按钮', () => {
  const generatingProps = { title: '产品需求文档', streamingContent: '正文…', generating: true }

  it('生成中：hideBack 时整条底栏都不渲染（只藏按钮会留下一条空栏）', () => {
    const shown = mount(GeneratingStage, { props: generatingProps })
    expect(shown.find('.shell-action-bar').exists()).toBe(true)
    expect(shown.text()).toContain('返回对话')

    const hidden = mount(GeneratingStage, { props: { ...generatingProps, hideBack: true } })
    expect(hidden.find('.shell-action-bar').exists()).toBe(false)
    expect(hidden.text()).not.toContain('返回对话')
  })

  it('交付页：hideBack 只藏「返回」，重新开始与下载交付包都还在', () => {
    const props = { prdContent: 'a', apiDocsContent: 'b', promptsContent: 'c' }

    const shown = mount(DoneStage, { props })
    expect(shown.text()).toContain('返回对话')

    const hidden = mount(DoneStage, {
      props: { ...props, hideBack: true, backLabel: '返回素材' },
    })
    expect(hidden.text()).not.toContain('返回素材')
    // 交付页的主操作与「重新开始」不受影响：藏掉返回不等于把出口都拿掉
    expect(hidden.text()).toContain('重新开始')
    expect(hidden.text()).toContain('下载交付包')
  })
})
