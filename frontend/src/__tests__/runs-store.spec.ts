import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'

import { useRunsStore } from '@/stores/runs'
import type { JobSnapshot } from '@/types'

/**
 * 在途任务运行态测试。
 *
 * 这一层是「生成中」从**全局布尔**变成**带身份的一支任务**的落点，因此要钉住的不是
 * 「值有没有变」，而是几条结构性保证——它们一旦破掉，症状都是「不报错但串稿」：
 *
 * 1. 身份按 `jobId` 比对：迟到的收尾清不掉新任务的指针（这一条从前要靠调用方自觉）；
 * 2. 迟到的快照 / 计时写不进别的任务（`applySnapshot` / `setElapsed` 都按 jobId 守卫）；
 * 3. `detach` 之后一切清空，还在道上飞的事件一个字节也写不进来；
 * 4. `begin` / `restore` 返回的是**响应式代理**——拿裸对象去写计时器，界面永远收不到更新。
 */

/** 造一份任务快照 */
function snapshot(overrides: Partial<JobSnapshot> = {}): JobSnapshot {
  return {
    jobId: 12,
    kind: 'prd',
    docKind: 'prd',
    status: 'running',
    phase: 'writing',
    finalizedText: '',
    liveBuffer: '',
    liveBySection: [],
    stageText: '',
    completedSections: [],
    solutionId: 1,
    errorCode: '',
    errorMessage: '',
    elapsedMs: 0,
    pendingItems: [],
    partial: false,
    failedSections: [],
    ...overrides,
  }
}

/** 开一支任务 */
function begin(runs: ReturnType<typeof useRunsStore>, jobId: number, docKind: 'prd' | 'api-docs' = 'prd') {
  return runs.begin({ jobId, solutionId: 1, jobKind: docKind, docKind })
}

beforeEach(() => {
  setActivePinia(createPinia())
})

describe('身份与指针', () => {
  it('begin 之后有指针、算「在跑」；settle 之后指针失效', () => {
    const runs = useRunsStore()

    expect(runs.hasRunning).toBe(false)
    expect(runs.pointer).toBeNull()

    begin(runs, 12)
    expect(runs.hasRunning).toBe(true)
    // 指针里还有「这轮回复记到哪条线程」那一项：文档任务没有线程，恒为 null
    expect(runs.pointer).toEqual({ jobId: 12, jobKind: 'prd', docKind: 'prd', chatStage: null })

    expect(runs.settle(12)).toBe(true)
    // 收尾后不再给指针：凭一个已经结束的 jobId 去重连，页面会卡在永远不动地生成态
    expect(runs.pointer).toBeNull()
    expect(runs.hasRunning).toBe(false)
    // 但身份还在（`settle` 只改状态不删对象），所以迟到的收尾仍可被识别为「同一支」
    expect(runs.holds(12)).toBe(true)
  })

  it('迟到的收尾清不掉新任务的指针（A 收尾时 B 已接手）', () => {
    const runs = useRunsStore()
    begin(runs, 12)
    begin(runs, 13)

    // A 的收尾迟到：只收同一支，所以什么都没发生
    expect(runs.settle(12)).toBe(false)
    expect(runs.holds(13)).toBe(true)
    expect(runs.pointer?.jobId).toBe(13)
    expect(runs.hasRunning).toBe(true)
  })

  it('按产物判断「哪一份在写」——同一份才算忙', () => {
    const runs = useRunsStore()
    begin(runs, 12, 'api-docs')

    expect(runs.isRunningFor('api-docs')).toBe(true)
    // 正在生成接口文档时，PRD 这一份并不忙：「生成 PRD」不该被一起灰掉
    expect(runs.isRunningFor('prd')).toBe(false)
    expect(runs.runOf('api-docs')?.jobId).toBe(12)
    expect(runs.runOf('prd')).toBeNull()
  })
})

describe('迟到的写入一律按 jobId 丢弃', () => {
  it('快照只认同一支任务，并用它的 docKind 校正目标产物', () => {
    const runs = useRunsStore()
    begin(runs, 12, 'prd')

    runs.applySnapshot(99, snapshot({ jobId: 99, docKind: 'api-docs' }))
    expect(runs.activeRun?.snapshot).toBeNull()
    expect(runs.activeRun?.docKind).toBe('prd')

    runs.applySnapshot(12, snapshot({ docKind: 'api-docs' }))
    expect(runs.activeRun?.snapshot?.jobId).toBe(12)
    expect(runs.activeRun?.docKind).toBe('api-docs')
  })

  it('快照里的 docKind 为空时不吃掉已推出的猜测', () => {
    const runs = useRunsStore()
    begin(runs, 12, 'api-docs')

    runs.applySnapshot(12, snapshot({ docKind: null }))
    expect(runs.activeRun?.docKind).toBe('api-docs')
  })

  it('计时与摘要只写同一支任务', () => {
    const runs = useRunsStore()
    begin(runs, 12)
    runs.setElapsed(99, 5_000)
    expect(runs.activeRun?.elapsedMs).toBe(0)

    runs.setElapsed(12, 5_000)
    expect(runs.activeRun?.elapsedMs).toBe(5_000)
  })
})

describe('响应式：写进去的值必须能被界面看见', () => {
  it('begin 返回的是代理，改它就等于改 store', () => {
    const runs = useRunsStore()
    const run = begin(runs, 12)

    // 计时器就是拿着这个引用每秒写一次；若返回的是构造时的裸对象，
    // 写进去的值界面永远看不到（表现是「进度条上的秒数不动」）
    run.elapsedMs = 3_000
    expect(runs.activeRun?.elapsedMs).toBe(3_000)
  })

  it('restore 同理', () => {
    const runs = useRunsStore()
    const run = runs.restore({ jobId: 7, solutionId: 1, jobKind: 'prd', docKind: 'prd' })

    run.elapsedMs = 1_200
    expect(runs.activeRun?.elapsedMs).toBe(1_200)
  })

  /**
   * 开新任务时**不清预览**。
   *
   * 预览只有 `useDocumentPipeline` 一个写入方，它在发起任务之前就按产物种类摆好了现场：
   * PRD 要先把空看板建出来，否则首批 `part` 事件无处可落。`begin` 若顺手清一次，
   * 表现就是「生成中一片空白、章节全丢」——这一条正是回归用例跑出来的真实症状。
   */
  it('begin 不清预览（调用方先摆好现场，别把它抹掉）', () => {
    const runs = useRunsStore()
    runs.preview.prdBoard = {
      total: 3,
      headerReady: false,
      headerText: '',
      sections: [],
      review: null,
      partial: false,
      failedSections: [],
    }

    begin(runs, 12)

    expect(runs.preview.prdBoard?.total).toBe(3)
  })
})

describe('重新接流与退订', () => {
  it('resume 清掉上一轮的用量与已用时，把状态拉回运行中', () => {
    const runs = useRunsStore()
    begin(runs, 12)
    runs.setSummary(12, { kind: 'prd' } as never)
    runs.setElapsed(12, 9_000)
    runs.settle(12)

    expect(runs.resume(12)).toBe(true)
    expect(runs.hasRunning).toBe(true)
    expect(runs.activeRun?.summary).toBeNull()
    expect(runs.activeRun?.elapsedMs).toBe(0)
  })

  it('detach 清身份与预览；之后迟到的写入全部落空', () => {
    const runs = useRunsStore()
    begin(runs, 12)
    runs.preview.content = '半截正文'
    runs.setElapsed(12, 4_000)

    runs.detach()

    expect(runs.activeRun).toBeNull()
    expect(runs.pointer).toBeNull()
    expect(runs.preview.content).toBe('')

    runs.setElapsed(12, 99_000)
    runs.applySnapshot(12, snapshot())
    expect(runs.activeRun).toBeNull()
    expect(runs.preview.content).toBe('')
  })

  it('restore 不伪造正文：切回原方案后预览仍是空的，等首个快照来补', () => {
    const runs = useRunsStore()
    begin(runs, 12)
    runs.preview.content = '上一份方案的正文'
    runs.detach()

    runs.restore({ jobId: 13, solutionId: 2, jobKind: 'prd', docKind: 'prd' })

    expect(runs.preview.content).toBe('')
    expect(runs.activeRun?.elapsedMs).toBe(0)
    expect(runs.activeRun?.snapshot).toBeNull()
  })

  it('clear 连身份一起清（reset / 换方案用）', () => {
    const runs = useRunsStore()
    begin(runs, 12)

    runs.clear()

    expect(runs.activeRun).toBeNull()
    expect(runs.hasRunning).toBe(false)
  })
})
