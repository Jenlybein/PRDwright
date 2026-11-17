import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'

import GeneratingStage from '@/components/stages/GeneratingStage.vue'
import { useRunsStore } from '@/stores/runs'
import type { JobSnapshot } from '@/types'
import {
  ACTIVE_JOB_STATE_QUERY,
  ACTIVE_JOB_STATES,
  JOB_STATE_LABELS,
  isActiveJobState,
  isTerminalJobState,
  jobStateLabel,
  jobStateTone,
  runBadgeLabel,
} from '@/utils/jobState'

/**
 * 任务七态的界面口径（票 #84）。
 *
 * 这一层不测「值有没有变」，只钉住三条会被误读的结构性保证：
 *
 * 1. **七个状态各有自己的文案与视觉档位**——「已取消」与「被取代」绝不合并成「失败」；
 * 2. **取消中是第三种在途态**：按钮立刻显示「正在取消」并置灰，不等服务端往返；
 * 3. **「在途」只有一份定义**：`pending,running,cancelling` 这个查询常量只在这里拼。
 */

function snapshot(overrides: Partial<JobSnapshot> = {}): JobSnapshot {
  return {
    jobId: 7,
    kind: 'prd',
    docKind: 'prd',
    status: 'running',
    phase: '',
    finalizedText: '',
    liveBuffer: '',
    liveBySection: [],
    stageText: '',
    completedSections: [],
    solutionId: 1,
    errorCode: '',
    errorMessage: '',
    elapsedMs: 0,
    retryAt: null,
    pendingItems: [],
    partial: false,
    failedSections: [],
    ...overrides,
  }
}

describe('七态文案与视觉档位', () => {
  it('七个值各有中文文案，已取消与被取代是不同的词', () => {
    expect(Object.keys(JOB_STATE_LABELS)).toHaveLength(7)
    expect(jobStateLabel('cancelled')).toBe('已取消')
    expect(jobStateLabel('superseded')).toBe('被取代')
    expect(jobStateLabel('cancelled')).not.toBe(jobStateLabel('superseded'))
    expect(jobStateLabel('cancelled')).not.toBe(jobStateLabel('failed'))
  })

  it('取消 / 被取代不是红色失败档位，只有 failed 是', () => {
    expect(jobStateTone('failed')).toBe('failure')
    expect(jobStateTone('cancelled')).toBe('warning')
    expect(jobStateTone('superseded')).toBe('warning')
    expect(jobStateTone('succeeded')).toBe('done')
    expect(jobStateTone('running')).toBe('active')
    expect(jobStateTone('cancelling')).toBe('active')
  })

  it('「在途」恰好三个值，且只拼一份查询串', () => {
    expect(ACTIVE_JOB_STATES).toEqual(['pending', 'running', 'cancelling'])
    expect(ACTIVE_JOB_STATE_QUERY).toBe('pending,running,cancelling')
    expect(isActiveJobState('cancelling')).toBe(true)
    expect(isActiveJobState('failed')).toBe(false)
    expect(isTerminalJobState('cancelled')).toBe(true)
    expect(isTerminalJobState('cancelling')).toBe(false)
  })

  it('运行视图徽标：取消中优先于动作文案，终态直接用状态文案', () => {
    expect(runBadgeLabel('cancelling', 'fix', { cancelling: true })).toBe('取消中')
    expect(runBadgeLabel('cancelled', 'generate')).toBe('已取消')
    expect(runBadgeLabel('superseded', 'review')).toBe('被取代')
    expect(runBadgeLabel('pending', 'generate')).toBe('排队中')
    expect(runBadgeLabel('running', 'fix')).toBe('修复中')
  })
})

describe('运行态里的取消标记', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('按下停止立刻置「取消中」，收尾时清掉', () => {
    const runs = useRunsStore()
    runs.begin({ jobId: 7, solutionId: 1, jobKind: 'prd', docKind: 'prd' })
    expect(runs.cancelling).toBe(false)

    expect(runs.requestCancel(7)).toBe(true)
    expect(runs.cancelling).toBe(true)

    // 别的任务按取消：不认
    expect(runs.requestCancel(8)).toBe(false)

    runs.settle(7)
    expect(runs.cancelling).toBe(false)
  })

  it('服务端快照说 cancelling 时也算取消中（本地标记丢了也能恢复）', () => {
    const runs = useRunsStore()
    runs.begin({ jobId: 7, solutionId: 1, jobKind: 'prd', docKind: 'prd' })
    runs.applySnapshot(7, snapshot({ status: 'cancelling' }))
    expect(runs.cancelling).toBe(true)
  })
})

describe('生成页的取消按钮', () => {
  function mountWaiting(cancelling: boolean) {
    return mount(GeneratingStage, {
      props: {
        title: '生成 PRD',
        streamingContent: '',
        generating: true,
        retryAt: new Date(Date.now() + 30_000).toISOString(),
        cancelling,
      },
    })
  }

  it('默认是「停止生成」，可点击', () => {
    const wrapper = mountWaiting(false)
    const button = wrapper.findAll('button').find((item) => item.text().includes('停止生成'))
    expect(button).toBeTruthy()
    expect(button!.attributes('disabled')).toBeUndefined()
  })

  it('取消后立刻变「正在取消…」并置灰', async () => {
    const wrapper = mountWaiting(false)
    // 模拟用户点了停止：上层把 `cancelling` 置真
    await wrapper.setProps({ cancelling: true })
    const text = wrapper.text()
    expect(text).toContain('正在取消…')
    expect(text).not.toContain('停止生成')
    const button = wrapper.findAll('button').find((item) => item.text().includes('正在取消…'))
    expect(button!.attributes('disabled')).toBeDefined()
  })
})
