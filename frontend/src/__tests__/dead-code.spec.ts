import { describe, expect, it } from 'vitest'

import * as api from '@/services/api'
import apiSource from '../services/api.ts?raw'
import typesSource from '../types/index.ts?raw'

/**
 * 死代码清理守卫（issue #2 / ADR-099 的三条判据）。
 *
 * 判据只有一条：没有代码读它。grep 是一次性证据，这里把结论钉成用例——
 * 已判定零引用的组件、资源与导出不得回流；被旧文档误判成「零外部引用」、
 * 实际仍被审核页 import 的 `AppDialog` 必须留下。
 */
describe('死代码清理', () => {
  it('ui 基础件不再保留零引用组件，被审核页引用的 AppDialog 仍在', () => {
    const uiModules = import.meta.glob('../components/ui/*.vue')
    const names = Object.keys(uiModules).map((path) => path.split('/').pop())

    // 反例：ADR-015 与 v2/ARCHITECTURE.md 写它「零外部引用」，ReviewStage.vue 却在用
    expect(names).toContain('AppDialog.vue')
    expect(names).not.toContain('AppToast.vue')
    expect(names).not.toContain('AppEmpty.vue')
    expect(names).not.toContain('AppInput.vue')
  })

  it('assets 下不再有孤儿资源', () => {
    expect(Object.keys(import.meta.glob('../assets/*'))).toEqual([])
  })

  it('API 客户端不再导出零引用的函数', () => {
    expect('getQuestions' in api).toBe(false)
    expect('generatePrdStream' in api).toBe(false)
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
})
