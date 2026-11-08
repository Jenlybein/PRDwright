import { describe, expect, it } from 'vitest'

import * as api from '@/services/api'
import { JOB_KIND_NAVIGATES_TO_RUN_VIEW } from '@/utils/workflow'
import apiSource from '../services/api.ts?raw'
import chatSource from '../composables/useClarificationChat.ts?raw'
import pipelineSource from '../composables/useDocumentPipeline.ts?raw'
import typesSource from '../types/index.ts?raw'

/**
 * 死代码清理守卫（issue #2 的三条判据）。
 *
 * 判据只有一条：没有代码读它。grep 是一次性证据，这里把结论钉成用例——
 * 已判定零引用的组件、资源与导出不得回流；被旧文档误判成「零外部引用」、
 * 实际仍被审核页 import 的 `AppDialog` 必须留下。
 */
describe('死代码清理', () => {
  it('ui 基础件不再保留零引用组件，被审核页引用的 AppDialog 仍在', () => {
    const uiModules = import.meta.glob('../components/ui/*.vue')
    const names = Object.keys(uiModules).map((path) => path.split('/').pop())

    // 反例：docs/architecture/ARCHITECTURE.md §5.5 写它「零外部引用」，ReviewStage.vue 却在用
    expect(names).toContain('AppDialog.vue')
    expect(names).not.toContain('AppToast.vue')
    expect(names).not.toContain('AppEmpty.vue')
    expect(names).not.toContain('AppInput.vue')
  })

  it('被删的三个孤儿资源不得回流', () => {
    // 只钉住本轮删掉的那三个：原断言是「assets 目录为空」，任何新增资源都会红，
    // 那是把「清理结论」写成了「目录永远不许有新文件」。
    const assets = Object.keys(import.meta.glob('../assets/*')).map((path) => path.split('/').pop())
    expect(assets).not.toContain('main.css')
    expect(assets).not.toContain('base.css')
    expect(assets).not.toContain('logo.svg')
  })

  it('API 客户端不再导出零引用的函数', () => {
    expect('getQuestions' in api).toBe(false)
    expect('generatePrdStream' in api).toBe(false)
  })

  it('旧直连生成通道的封装随路由一并退役（#61）', () => {
    // 三份产物的生成只剩任务通道（`createJob` + `readJobStream`）。这层封装留着
    // 就会有人接着用它——而后端那四条路由已经没有了，症状是线上 404 而不是编译错误。
    expect('generatePrdFromSummaryStream' in api).toBe(false)
    expect('generateApiDocsStream' in api).toBe(false)
    expect('generatePromptsStream' in api).toBe(false)
    // 非任务通道只剩就地优化这一条（它递的是「要改哪一份」，不是上游）
    expect('optimizeDocumentStream' in api).toBe(true)
  })

  it('快照类型只留仍在使用的 SessionDataV2', () => {
    expect(typesSource).not.toMatch(/export interface SessionData\s*\{/)
    expect(typesSource).toMatch(/export interface SessionDataV2\s*\{/)
  })

  // 级联死：这两个导出唯一的消费者是上面已被删掉的 getQuestions / generatePrdStream。
  // 它们是 interface（运行时被擦除），只能对源码做断言。
  it('问卷配置类型随 getQuestions 一并消失，QuestionConfig 不再单独残留', () => {
    expect(typesSource).not.toMatch(/export interface QuestionsConfig\s*\{/)
    expect(typesSource).not.toMatch(/export interface QuestionConfig\s*\{/)
  })

  it('API 客户端不再残留只服务于已删函数的请求体类型', () => {
    expect(apiSource).not.toMatch(/export interface GeneratePrdStreamPayload\s*\{/)
  })

  /**
   * 运行视图的接管清单与**真实发起任务的地方**必须对得上（#68）。
   *
   * 这条守卫来自一次真实事故：兜底清单只手写了「修复」与「重审」，于是
   * **一键更新**（走生成任务）与**按澄清改写 / AI 自行澄清**在途时切走再切回来就失联了。
   * 判据只有一条——「这个动作发起时界面会不会切进运行视图」——所以清单必须覆盖
   * 每一处真正 `job.start('<kind>')` 的调用，且**每一项的分类都要写在这里**：
   * 新增一个任务种类却没碰清单，这条用例就会红。
   */
  it('运行视图接管清单覆盖每一处 job.start 的任务种类（#68 防再漏）', () => {
    const kindsStartedInSource = new Set<string>()
    for (const source of [pipelineSource, chatSource]) {
      for (const match of source.matchAll(/job\.start\(\s*'([^']+)'/g)) {
        kindsStartedInSource.add(match[1] ?? '')
      }
    }
    // 源码里确实扫到了东西（防止正则失效后这条用例变成永远为真）
    expect(kindsStartedInSource.size).toBeGreaterThanOrEqual(8)

    for (const kind of kindsStartedInSource) {
      expect(
        Object.prototype.hasOwnProperty.call(JOB_KIND_NAVIGATES_TO_RUN_VIEW, kind),
        `任务种类「${kind}」没有登记是否接管运行视图：它的发起处会切阶段，漏登记就会「切走再切回来看不见」`,
      ).toBe(true)
    }

    // 分类本身也是结论，改动必须是有意的
    expect(JOB_KIND_NAVIGATES_TO_RUN_VIEW).toEqual({
      prd: true,
      'api-docs': true,
      prompts: true,
      clarify: true,
      'auto-clarify': true,
      review: true,
      'review-fix': true,
      optimize: false,
      chat: false,
    })
  })
})
