import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import DocPhaseProgress from '@/components/ui/DocPhaseProgress.vue'
import GeneratingStage from '@/components/stages/GeneratingStage.vue'
import { useRunsStore } from '@/stores/runs'
import type { StreamStageEvent } from '@/types'

/**
 * 分批续写的前端口径（票 #93）。
 *
 * 后端撞上模型输出上限时会自动接着写，每起一批补发一条 `stage(writing)` +
 * `data.continuation`。前端的三条硬要求都在这一个接缝上：
 *
 * 1. **不渲染成新章节**：正文只有一条缓冲，续写批次只是让它继续长；
 * 2. **进度不重置**：阶段仍是「生成」，进度条不回大纲、也不提前点亮「完成」；
 * 3. **拼接处不重复**：重复由后端接缝去重承担（`stream_reply._SeamGuard`），
 *    前端一个字节都不许自己剪，否则「服务端正文」与「界面预览」会变成两份。
 *
 * 判据都是**渲染结果**，不碰组件内部实现：`continuation` 只是被展示，不参与任何进程判断。
 */

/** 续写事件（后端 `stage` + `data.continuation`）在类型上必须有的形状 */
const CONTINUATION_EVENT: StreamStageEvent = {
  stage: 'writing',
  note: '',
  issues: [],
  continuation: 2,
}

function mountStage(props: Record<string, unknown> = {}) {
  return mount(GeneratingStage, {
    props: {
      title: '产品需求文档',
      streamingContent: '# 标题\n\n第一段已经写完。',
      generating: true,
      statusLabel: '生成中',
      docPhase: 'writing',
      ...props,
    },
  })
}

describe('分期续写：生成的解释，不是新章节', () => {
  it('续写事件仍是「撰写」阶段，阶段条一格都不多、也不回到大纲', () => {
    const wrapper = mountStage({ continuation: CONTINUATION_EVENT.continuation })

    const chips = wrapper.findAll('ol li').map((li) => li.find('span').text().trim())
    // 五格仍在，续写没有插新的一格
    expect(chips).toEqual(['大纲', '生成', '审核', '改写', '完成'])
    // 高亮仍在「生成」（第 2 格）：没有回到大纲，也没有提前点亮完成
    const classes = wrapper.findAll('ol li').map((li) => li.find('span').classes())
    expect(classes[1]).toContain('ring-primary-100')
    expect(classes[0]).not.toContain('ring-primary-100')
    expect(classes[4]).not.toContain('ring-primary-100')
  })

  it('续写期间正文照旧展示在同一块预览里（不是多出来的一块）', () => {
    const wrapper = mountStage({ continuation: 3 })

    // 正文只出现一次：续写批次不会被拼成第二段正文
    const occurrences = wrapper.text().split('第一段已经写完。').length - 1
    expect(occurrences).toBe(1)
  })

  it('真的续写过才解释一句「自动续写到第 N 段」', () => {
    expect(mountStage({ continuation: 2 }).text()).toContain('已自动续写到第 2 段')
    // 没续写（0 / 1）时不许出现这句话：那是「正常生成」的解释，不该凭空冒出来
    for (const continuation of [undefined, 0, 1]) {
      expect(mountStage({ continuation }).text()).not.toContain('已自动续写')
    }
  })

  it('审核阶段不再挂续写提示：那句话只解释「正文还在长」', () => {
    const wrapper = mountStage({ docPhase: 'reviewing', continuation: 3 })

    expect(wrapper.text()).not.toContain('已自动续写')
  })

  it('续写不是失败：不出现错误提示', () => {
    const wrapper = mountStage({ continuation: 2 })

    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  })
})

describe('续写批次序号的状态归属', () => {
  it('开新任务（resetPreview）把序号清回 0：上一份的续写不该算到下一份头上', () => {
    setActivePinia(createPinia())
    const runs = useRunsStore()

    runs.preview.continuation = 3
    runs.resetPreview()

    expect(runs.preview.continuation).toBe(0)
  })
})
