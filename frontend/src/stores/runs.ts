/**
 * 在途任务的运行态（本标签页的**唯一一支**）。
 *
 * ## 为什么要有这个 store
 *
 * 从前「有没有任务在跑」是一个全局布尔 `docGenerating`，它同时被当作三件不相干的事用：
 * 渲染归属（这块界面是不是正被写）、重入守卫（能不能再发一次）、**导航闸门**
 * （忙的时候不许切页面）。第三种用法把整个界面锁死了——进度条、底栏「返回」全都点不动，
 * 因为所有跳转都收口在 `workflow.goTo()` 的 `if (busy) return false` 里。
 *
 * 根因是那个布尔**没有归属**：它不说是谁在跑、跑的是哪一份、属于哪个方案。于是「忙」只能
 * 被理解成「整个应用忙」，没法回答「忙的是不是我现在看的这一份」。
 *
 * 这里把它换成一支带身份的运行态：
 *
 * - `jobId` —— 哪一次任务（后端主键，收尾时按它比对，迟到的收尾清不掉新任务的指针）；
 * - `docKind` —— 写哪一份产物（渲染归属与重入守卫都按它判）；
 * - `solutionId` —— 属于哪个方案（切方案时整体退订，回来再重连）。
 *
 * ## 为什么单独开一个 store
 *
 * `stores/solutions.ts` 在切方案时必须先退订、清预览（否则旧任务的流会写进新方案），
 * 但它不能 import `useDocumentPipeline`（`pipeline → solutions` 已经存在，反过来就成环）。
 * 本 store 只依赖 `types` 与 `services/runSubscriptions`，谁都能单向依赖它。
 *
 * ## 为什么 `preview` 不挂在 `activeRun` 里
 *
 * 预览是「界面此刻在显示什么」，任务一旦收尾，它还要继续显示最终结果；而 `activeRun`
 * 收尾后就该清掉身份。两者生命周期不同，硬塞进一个对象反而要在收尾时小心别把正文一起
 * 清掉。因此 `preview` 与 `activeRun` 并列，由同一组动作（`detach` / `clear` /
 * `resetPreview`）负责清理——**归属靠同一个 store，不靠同一个对象**。
 */

import { computed, reactive, ref } from 'vue'
import { defineStore } from 'pinia'

import type {
  ChatStage,
  DocKind,
  DocPhase,
  DocumentKind,
  JobSnapshot,
  PrdBoardState,
  ReviewConclusion,
  RunSummary,
} from '@/types'

/** 一次任务的预览态（正文 / 看板 / 阶段 / 审核结论 / 错误），整体随运行态清理 */
export interface RunPreview {
  /** 流式正文（生成中逐字追加，收尾后即最终正文） */
  content: string
  /** PRD 分章节看板；其它产物为 null */
  prdBoard: PrdBoardState | null
  /** 当前阶段（loading 条） */
  docPhase: DocPhase
  /** 当前阶段模型的原始输出（审核意见 / 改写稿），只给阶段面板看 */
  stageText: string
  /** 审核结论；不持久化，刷新即失 */
  review: ReviewConclusion | null
  /** 生成失败原因（成功时为空串） */
  error: string
}

/** 快照里的持久指针：刷新后据此重连 */
export interface RunPointer {
  jobId: number
  jobKind: DocKind
  docKind: DocumentKind | null
  /**
   * 对话任务属于哪条线程；文档任务为 null。
   *
   * `jobKind='chat'` 只说「这是对话」：四条线程的人设与上下文完全不同，
   * 没有这一项，重连后就分不清「这轮回复该记到谁头上」。
   */
  chatStage?: ChatStage | null
}

/** 一支在途（或刚收尾）的任务 */
export interface ActiveRun extends RunPointer {
  /** 任务创建那一刻的方案 id；`null` 表示当时还没有方案（快捷入口） */
  solutionId: number | null
  status: 'running' | 'settled' | 'detached'
  startedAt: number
  /** 已用时（毫秒）：生成中每秒自增，重连后按服务端值锚定 */
  elapsedMs: number
  /** 任务收尾摘要（用量汇总） */
  summary: RunSummary | null
  /** 最近一次收到的快照（供上层判断任务终态） */
  snapshot: JobSnapshot | null
}

function emptyPreview(): RunPreview {
  return { content: '', prdBoard: null, docPhase: 'idle', stageText: '', review: null, error: '' }
}

export const useRunsStore = defineStore('runs', () => {
  /**
   * 当前这一支运行态。
   *
   * 只留一支：前端正常路径上每个界面只有一个发起入口，同时开两支属于「跨标签页」场景，
   * 本期的取舍见计划文档「不做的事」——那种情况最坏只是看不到另一支的进度，不会串稿
   * （正文落哪一份由后端 `doc_kind` 决定）。
   */
  const activeRun = ref<ActiveRun | null>(null)

  /** 预览态：与运行态分开，收尾后正文还要继续显示 */
  const preview = reactive<RunPreview>(emptyPreview())

  /** 是否有任务在跑（信息性判据；**不再用于导航拦截**） */
  const hasRunning = computed(() => activeRun.value?.status === 'running')

  /**
   * 可持久化的任务指针：只在**运行中**才有值。
   *
   * 收尾后返回 null，是为了避免「刷新后凭一个已经结束的 jobId 去重连」——
   * 那会让页面卡在一个永远不会再出事件的生成态上。
   */
  const pointer = computed<RunPointer | null>(() => {
    const run = activeRun.value
    if (!run || run.status !== 'running') return null
    return {
      jobId: run.jobId,
      jobKind: run.jobKind,
      docKind: run.docKind,
      chatStage: run.chatStage ?? null,
    }
  })

  /** 这支 job 是否仍由本 store 持有（身份的**主判据**） */
  function holds(jobId: number): boolean {
    return activeRun.value?.jobId === jobId
  }

  /** 这支 job 是否仍在跑（迟到的收尾 / 事件一律按它判过期） */
  function isRunning(jobId: number): boolean {
    const run = activeRun.value
    return run?.jobId === jobId && run.status === 'running'
  }

  /** 当前跑的这支是否正在写 `kind` 这一份产物（渲染归属 / 重入守卫） */
  function isRunningFor(kind: DocumentKind): boolean {
    const run = activeRun.value
    return run?.status === 'running' && run.docKind === kind
  }

  /**
   * 当前跑的这支是否正在替某条**对话线程**生成回复。
   *
   * 对话没有产物，判据只能是「线程」：四条线程可以各跑各的，A 在生成不该把 B 的
   * 输入框也灰掉——这正是从前那个全局布尔 `chatStreaming` 造成的毛病。
   */
  function isRunningForChat(stage: ChatStage): boolean {
    const run = activeRun.value
    return run?.status === 'running' && run.jobKind === 'chat' && run.chatStage === stage
  }

  /** 某一份产物的运行态（取不到为 null） */
  function runOf(kind: DocumentKind): ActiveRun | null {
    const run = activeRun.value
    return run && run.docKind === kind ? run : null
  }

  /**
   * 新任务开始：登记身份。
   *
   * **不在这里清预览**：预览的唯一写入方是 `useDocumentPipeline`，它在发起任务**之前**
   * 就按产物种类摆好了现场（PRD 要先把空看板建出来，否则首批 `part` 事件无处可落）。
   * 这里再清一次会把刚建好的看板抹掉，表现是「生成中一片空白、章节全丢」。
   * 需要清的时候它自己调 `resetPreview()`。
   *
   * 若已有一支在跑（正常路径不该发生，重入守卫会挡），后者以新任务为准；
   * 旧链后续的收尾会因为 `isRunning(oldJobId)` 为假而自动作废——这正是身份判据的价值。
   */
  function begin(input: {
    jobId: number
    solutionId: number | null
    jobKind: DocKind
    docKind: DocumentKind | null
    chatStage?: ChatStage | null
  }): ActiveRun {
    activeRun.value = {
      ...input,
      chatStage: input.chatStage ?? null,
      status: 'running',
      startedAt: Date.now(),
      elapsedMs: 0,
      summary: null,
      snapshot: null,
    }
    // 返回**响应式代理**而不是刚构造的裸对象：计时器与事件回调都拿着这个引用写
    // `elapsedMs` / `snapshot`，写裸对象只会改到 `ref` 内部的目标，界面永远收不到更新。
    return activeRun.value
  }

  /**
   * 从快照恢复一支运行态（刷新 / 切回原方案）。
   *
   * 只重建身份，不伪造 `elapsedMs` 与 `snapshot`：那两项由随后的首个 `snapshot` 事件补齐。
   */
  function restore(pointerValue: RunPointer & { solutionId: number | null }): ActiveRun {
    activeRun.value = {
      jobId: pointerValue.jobId,
      solutionId: pointerValue.solutionId,
      jobKind: pointerValue.jobKind,
      docKind: pointerValue.docKind,
      chatStage: pointerValue.chatStage ?? null,
      status: 'running',
      startedAt: Date.now(),
      elapsedMs: 0,
      summary: null,
      snapshot: null,
    }
    // 同 `begin`：必须返回代理，否则后续写入不会触发更新
    return activeRun.value
  }

  /**
   * 用后端的 `doc_kind` 校正目标产物。
   *
   * 旧快照 / 旧后端没有这个字段时，`restore` 先按 kind 推一个（`prd`/`api-docs`/`prompts`
   * 能推出来，`optimize`/`clarify` 推不出来）；首个 `snapshot` 到达后以服务端为准补上。
   */
  function refineDocKind(jobId: number, docKind: DocumentKind | null): void {
    const run = activeRun.value
    if (!run || run.jobId !== jobId) return
    run.docKind = docKind
  }

  /**
   * 记下最近一次快照，并顺手用它的 `docKind` 校正目标产物。
   *
   * 只认**同一支**任务：迟到的快照属于已经收尾 / 被顶替的那一支，写进来会让界面
   * 拿旧任务的进度覆盖当前任务。
   */
  function applySnapshot(jobId: number, snap: JobSnapshot): void {
    const run = activeRun.value
    if (!run || run.jobId !== jobId) return
    run.snapshot = snap
    // 空值不覆盖：`restore` 按 kind 推出来的那个猜测在旧后端上就是最好的答案
    if (snap.docKind) run.docKind = snap.docKind
  }

  /** 写入已用时（计时器每秒一跳；快照 / 收尾摘要用它锚定到服务端的权威值） */
  function setElapsed(jobId: number, elapsedMs: number): void {
    const run = activeRun.value
    if (!run || run.jobId !== jobId) return
    run.elapsedMs = Math.max(0, elapsedMs)
  }

  /** 写入任务收尾摘要（用量汇总） */
  function setSummary(jobId: number, summary: RunSummary | null): void {
    const run = activeRun.value
    if (!run || run.jobId !== jobId) return
    run.summary = summary
  }

  /** 收尾：只收**同一支**任务；返回是否真的收了（迟到的收尾返回 false） */
  function settle(jobId: number): boolean {
    const run = activeRun.value
    if (!run || run.jobId !== jobId) return false
    run.status = 'settled'
    return true
  }

  /**
   * 重新接流：把这一支任务拉回「运行中」，并清掉上一轮订阅留下的进度与用量。
   *
   * 为什么必须清：用量回执与已用时都是**上一次订阅**看到的东西，留着会让用户以为
   * 那是本次的；已用时则以快照里的服务端值重新锚定，刷新后显示的是任务的真实耗时。
   */
  function resume(jobId: number): boolean {
    const run = activeRun.value
    if (!run || run.jobId !== jobId) return false
    run.status = 'running'
    run.summary = null
    run.snapshot = null
    run.elapsedMs = 0
    run.startedAt = Date.now()
    return true
  }

  /**
   * 退订一切：清身份与预览，**不动后端任务**。
   *
   * 切方案 / 新建方案时调用；回到原方案由它的快照指针重连（见 `AppV2.resumeAfterRestore`）。
   */
  function detach(): void {
    if (activeRun.value) activeRun.value.status = 'detached'
    activeRun.value = null
    resetPreview()
  }

  /** 连指针一起清（`workflow.reset()` 用） */
  function clear(): void {
    activeRun.value = null
    resetPreview()
  }

  /** 只清预览（开新任务 / 重新接流前调用） */
  function resetPreview(): void {
    Object.assign(preview, emptyPreview())
  }

  return {
    // 状态
    activeRun,
    preview,
    hasRunning,
    pointer,
    // 判据
    holds,
    isRunning,
    isRunningFor,
    isRunningForChat,
    runOf,
    // 动作
    begin,
    restore,
    refineDocKind,
    applySnapshot,
    setElapsed,
    setSummary,
    settle,
    resume,
    detach,
    clear,
    resetPreview,
  }
})
