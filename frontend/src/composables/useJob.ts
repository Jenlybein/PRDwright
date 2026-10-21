/**
 * 生成任务生命周期编排。
 *
 * 把「一次生成」从「一次浏览器请求」提升为「一个后端任务」：`start` 创建任务并订阅，
 * `reconnect` 在刷新后凭任务指针**静默重连**（后端先发 `snapshot` 补全已产出正文，
 * 再接 live），`detach` 只退订、**不取消服务端任务**——这正是「生成与连接解耦」的落点。
 *
 * ## 运行态不住在这里
 *
 * 「谁在跑、跑的是哪一份、跑了多久」全部写进 `stores/runs.ts` 里那一支带身份的
 * `ActiveRun`，本文件只负责**发起与收尾**。这样换来两个结构性的保证：
 *
 * - 迟到的收尾清不掉新任务的状态（`runs.settle(jobId)` 只收同一支）；
 * - 切方案时谁都能 `runs.detach()`（不需要摸到本 composable 的实例变量）。
 *
 * \`start\` / \`reconnect\` 都把**本次任务那一支运行态**随结果一起返回：调用方必须拿它去比对，
 * 而不是读「当前那一支」——两者在后一个任务启动之后就不再是同一个了。
 *
 * **预览的单一出口仍是 `useDocumentPipeline`**：本 composable 只负责生命周期与运行态记账，
 * 不直接写预览。它把 `onChunk` / `onPart` / `onStage` / `onSnapshot` 回调原样透传给
 * 调用方提供的 handlers，由流水线决定这些事件怎么落到预览上。
 *
 * ## 已用时（`elapsedMs`）
 *
 * 「这次生成跑了多久」由本 composable 统一维护，而不是让展示组件各起一个计时器：
 * 生成中要一秒一跳，收尾后要停在权威总耗时上，重连后还要把客户端归零的部分补回来。
 * 三种情形共用一处状态，展示组件只管读。
 *
 * 计时不依赖任何一次请求：**每次 `snapshot` 都用服务端算好的 `elapsedMs` 重新锚定**，
 * 因此刷新页面、换设备重连，显示的仍是任务的真实已用时，而不是「本地计时器启动后过了多久」。
 *
 * 计时器按 `jobId` 登记（不再是单槽）：同一页面里先后两支任务交接时，前一支的表必须
 * 能被干净地停掉，否则它会继续往新任务的显示值里写。
 */

import { computed, getCurrentScope, onScopeDispose, ref } from 'vue'

import {
  createJob,
  readJobStream,
  type CreateJobPayload,
  type StreamHandlers,
} from '@/services/api'
import { ApiError } from '@/services/errors'
import {
  isSubscribed,
  register as registerSubscription,
  release as releaseSubscription,
  detachAll as detachAllSubscriptions,
} from '@/services/runSubscriptions'
import { useRunsStore, type ActiveRun } from '@/stores/runs'
import { useSolutionsStore } from '@/stores/solutions'
import { useWorkflowStore } from '@/stores/workflow'
import type { DocKind, DocumentKind, JobSnapshot, RunSummary, ViewState } from '@/types'
import { jobDocumentKind } from '@/utils/workflow'

/**
 * 文档种类 → 对应的「生成中」阶段。
 *
 * `optimize` 与 `clarify` 都是**就地**动某一篇已有文档：不切阶段，用户留在原来的界面上
 * （优化留在审阅页、澄清留在澄清页），由那一页自己表现「正在干活」。因此为 null。
 *
 * `auto-clarify` 也返回 null，但**调用方会自己先切到 `generating-<kind>`**：
 * 它要跑几十秒且输出整篇正文，留在审阅页只能干等，因此复用生成态的流式预览
 * （见 `useDocumentPipeline.autoClarify`）。切阶段放在调用方，是因为它知道目标产物是谁。
 */
const GENERATING_STATE: Record<DocKind, ViewState | null> = {
  prd: 'generating-prd',
  'api-docs': 'generating-api-docs',
  prompts: 'generating-prompts',
  optimize: null,
  clarify: null,
  'auto-clarify': null,
  // 对话不切阶段：用户本来就停在对话页，切走反而看不到正在流的内容。
  // 它是「就地生成」，和 optimize / clarify 同一种形态。
  chat: null,
}

/** `start` 的输入：任务种类 + 各类型各自的字段（`solutionId` 由本 composable 补） */
export type JobStartInput = Omit<CreateJobPayload, 'kind' | 'solutionId'>

/** 一次任务订阅的结果 */
export interface JobResult {
  /** 完整回复正文（无正文时为空串） */
  reply: string
  /** 是否成功（未被中止、未收到 error 事件） */
  ok: boolean
  /** 失败时的机器可读错误码 */
  errorCode?: string
  /**
   * 本次任务对应的运行态；`null` 表示任务压根没建起来（创建请求就失败了）。
   *
   * 调用方**必须**用它来判断「这次结果还是不是我的」：读 store 里「当前那一支」
   * 拿到的是后一个任务，A 的迟到结果就会以为自己没过期。
   */
  run: ActiveRun | null
}

/**
 * 任务种类 → 这次要写进哪一份产物。
 *
 * 三种**整篇生成**的种类就是产物；`optimize` / `clarify` / `auto-clarify` 是动作，
 * 目标由请求体里的 `docKind` 显式给出（后端 `_target_product_kind` 同口径）。
 * 都推不出来就留 `null`——**不猜**默认值，猜错等于拿 A 的结果覆盖 B。
 */
function targetDocKindOf(kind: DocKind, input: JobStartInput): DocumentKind | null {
  return jobDocumentKind(kind) ?? input.docKind ?? null
}

/**
 * 生成任务编排 composable。
 *
 * 运行态（`snapshot` / 摘要 / 已用时 / 阶段）都是**只读视图**，映射到 `runs` 里那一支任务；
 * 错误原因与「是否有订阅在途」因为是「本实例这一路的事」而留在这里。
 */
export function useJob() {
  const workflow = useWorkflowStore()
  const solutions = useSolutionsStore()
  const runs = useRunsStore()

  /**
   * 在途订阅数。
   *
   * 用计数而不是布尔：`start` 与 `reconnect` 完全可能交叠（A 还在收尾、B 已发起），
   * 布尔会被先结束的那一路提前归零。
   */
  const pendingCalls = ref(0)

  /** 是否有任务订阅进行中 */
  const running = computed(() => pendingCalls.value > 0)

  /** 最近一次失败原因；新一轮开始时清空 */
  const lastError = ref('')
  const error = computed(() => lastError.value)

  /** 本实例这一轮实际持有的那一支运行态（用于只读派生；写入一律走 `runs`） */
  const currentRun = computed<ActiveRun | null>(() => runs.activeRun)

  /** 最近的阶段（来自 stage 事件或 snapshot） */
  const phase = computed(() => currentRun.value?.snapshot?.phase ?? '')
  /** 任务收尾摘要（完整的用量汇总；展示由上层决定） */
  const runSummary = computed<RunSummary | null>(() => currentRun.value?.summary ?? null)
  /** 最近一次收到的快照（供上层判断任务是否已终态） */
  const snapshot = computed<JobSnapshot | null>(() => currentRun.value?.snapshot ?? null)
  /** 任务已用时（毫秒）：生成中每秒自增，重连时按服务端值锚定，收尾后停在总耗时 */
  const elapsedMs = computed(() => currentRun.value?.elapsedMs ?? 0)

  // ---- 已用时计时器 ----------------------------------------------------------
  // 每支任务一个：`baseMs` 是锚定时任务已经跑过的毫秒数（来自服务端），`anchorMs` 是本地
  // 锚点时刻。显示值 = baseMs + (now - anchorMs)，因此「重新锚定」是幂等的——给同一次任务
  // 锚两次，只是把基准换成服务端那个更准的值，不会跳变或翻倍。
  interface ElapsedTimer {
    handle: ReturnType<typeof setInterval>
    baseMs: number
    anchorMs: number
  }
  const timers = new Map<number, ElapsedTimer>()

  /** 停掉某支任务的表，**保留它的显示值**（收尾 / 退订后停表用） */
  function stopTimer(jobId: number): void {
    const timer = timers.get(jobId)
    if (!timer) return
    clearInterval(timer.handle)
    timers.delete(jobId)
  }

  /** 停掉全部计时器（组件卸载 / 整体退订） */
  function stopAllTimers(): void {
    for (const timer of timers.values()) clearInterval(timer.handle)
    timers.clear()
  }

  /** 开始计时（`baseMs` 为服务端已用时打底） */
  function startTimer(jobId: number, baseMs = 0): void {
    stopTimer(jobId)
    const timer: ElapsedTimer = {
      handle: setInterval(() => {
        const live = timers.get(jobId)
        if (!live) return
        runs.setElapsed(jobId, live.baseMs + (Date.now() - live.anchorMs))
      }, 1000),
      baseMs: Math.max(0, baseMs),
      anchorMs: Date.now(),
    }
    timers.set(jobId, timer)
    runs.setElapsed(jobId, timer.baseMs)
  }

  /** 用服务端权威耗时重新锚定（每次 `snapshot` 都做，幂等） */
  function anchorTimer(jobId: number, serverMs: number): void {
    const timer = timers.get(jobId)
    if (!timer) {
      // 没有在跑的表（刚重连就被快照锚定 / 已经停表）：只把显示值对齐，不起表
      runs.setElapsed(jobId, serverMs)
      return
    }
    timer.baseMs = Math.max(0, serverMs)
    timer.anchorMs = Date.now()
    runs.setElapsed(jobId, timer.baseMs)
  }

  /** 收尾：停表并停在权威总耗时上（没有总耗时就停在当前值） */
  function settleTimer(jobId: number, totalMs?: number): void {
    stopTimer(jobId)
    if (typeof totalMs === 'number') runs.setElapsed(jobId, totalMs)
  }

  // 组件卸载时停表。非组件环境（纯逻辑测试）下不注册，避免无谓告警。
  if (getCurrentScope()) onScopeDispose(stopAllTimers)

  /** 包装 handlers：在透传的同时把 snapshot / 摘要 / 已用时记进那一支运行态 */
  function wrapHandlers(jobId: number, handlers: StreamHandlers): StreamHandlers {
    return {
      ...handlers,
      onSnapshot: (snap) => {
        runs.applySnapshot(jobId, snap)
        // 快照是订阅开始时的权威进度：用它锚定已用时，客户端从零开始也不影响连续性
        anchorTimer(jobId, snap.elapsedMs)
        handlers.onSnapshot?.(snap)
      },
      onRunSummary: (summary) => {
        runs.setSummary(jobId, summary)
        // 收尾摘要里的总耗时是权威值，停表并停在它上面
        settleTimer(jobId, summary.durationMs)
        handlers.onRunSummary?.(summary)
      },
    }
  }

  /**
   * 开一次生成任务：确保方案存在 → 创建任务 → 记下运行态 + 切生成态 → 订阅。
   *
   * 两次 `flushPendingSave` 是刻意的：第一次保证「有内容的方案」先落库、拿到 id，
   * 任务创建时才能带上 `solution_id`；第二次在写入运行态后强制落库，
   * 确保任务指针进了后端快照——**刷新后能重连，全靠这一步**。
   *
   * 切阶段的顺序与从前一致（先 `goTo` 再登记运行态）：`goTo` 此刻仍会拦「有任务在跑」，
   * 反过来写就会把自己拦在门外。
   */
  async function start(
    kind: DocKind,
    input: JobStartInput,
    handlers: StreamHandlers,
  ): Promise<JobResult> {
    pendingCalls.value += 1
    lastError.value = ''
    let run: ActiveRun | null = null

    try {
      // 1. 先落一次：把「已有内容」的方案建出来，任务才有 solution_id 可挂
      await solutions.flushPendingSave()

      // 2. 创建任务
      const created = await createJob({ kind, solutionId: solutions.currentId, ...input })

      // 3. 切到生成态（`goTo` 必须在登记运行态之前：登记之后界面就是「忙」的了）
      const target = GENERATING_STATE[kind]
      if (target) workflow.goTo(target)

      // 4. 登记运行态：此后 `activeJobId` / `hasRunning` 都从这一支读
      run = runs.begin({
        jobId: created.jobId,
        solutionId: solutions.currentId,
        jobKind: kind,
        docKind: targetDocKindOf(kind, input),
        // 对话没有产物，身份由「哪条线程」表达。
        // ⚠️ 这一行漏了就会出现「对话不流式」：`isRunningForChat(stage)` 恒为假，
        // 渲染层不认为这一轮在跑，流式气泡压根不出现，用户只在收尾刷新后
        // 整段看到回复——看起来像「流式坏了」，实际是身份没登记。
        chatStage: input.chatStage ?? null,
      })

      // 5. 再次强制落库：把任务指针写进快照
      await solutions.flushPendingSave()

      // 6. 订阅事件流
      const controller = new AbortController()
      registerSubscription(created.jobId, controller)
      startTimer(created.jobId)
      try {
        const result = await readJobStream(
          created.jobId,
          wrapHandlers(created.jobId, handlers),
          controller.signal,
        )
        return { reply: result.reply, ok: true, run }
      } finally {
        releaseSubscription(created.jobId, controller)
        // 正常收尾时运行摘要已把耗时定死；中止 / 失败则停在当前值，不让计时器空转
        settleTimer(created.jobId)
      }
    } catch (caught) {
      return { ...fail(caught), run }
    } finally {
      pendingCalls.value -= 1
      // 订阅结束即这一支任务不再「在跑」：指针随之失效，界面不再被它挡住
      if (run) runs.settle(run.jobId)
    }
  }

  /**
   * 静默重连一个已有任务：先收 `snapshot` 补全正文，再接 live。
   *
   * 与 `start` 的区别是不创建任务、不改阶段（阶段已由快照恢复保留为 `generating-*`）。
   * 已用时同样从零起算、随即被快照里的服务端值锚定，所以显示的仍是任务的真实已用时。
   *
   * ⚠️ 调用方必须**先**把这一支运行态登记好（`runs.restore`）：重连不知道 `kind`，
   * 而且要凭 `run` 来判断「这次结果还是不是我的」。
   */
  async function reconnect(jobId: number, handlers: StreamHandlers): Promise<JobResult> {
    pendingCalls.value += 1
    lastError.value = ''
    // 没有登记就退化成「纯订阅」：定时与记账全部按 jobId 守卫，写不进去也不会错。
    // 调用方正常都会先登记（见 `useDocumentPipeline.reconnectAndSettle`）。
    const run = runs.holds(jobId) ? runs.activeRun : null

    try {
      if (run) runs.resume(jobId)
      const controller = new AbortController()
      registerSubscription(jobId, controller)
      startTimer(jobId)
      try {
        const result = await readJobStream(jobId, wrapHandlers(jobId, handlers), controller.signal)
        return { reply: result.reply, ok: true, run }
      } finally {
        releaseSubscription(jobId, controller)
        // 已用时由快照 / 收尾摘要锚定过；没锚到就停在当前值，不让表空转
        settleTimer(jobId)
      }
    } catch (caught) {
      return { ...fail(caught), run }
    } finally {
      pendingCalls.value -= 1
      runs.settle(jobId)
    }
  }

  /**
   * 只退订：中止本地读取，**不取消服务端任务**。
   *
   * 这是「生成与连接解耦」的直接体现——离开页面 / 切换方案只影响这些订阅，
   * 后端任务照跑；下次回来凭快照指针重连即可。
   *
   * 退订的范围是**这个标签页里的全部在途订阅**（登记表按 `jobId` 存的），
   * 同时把运行态与预览清空：清完之后，还在道上飞的事件一个字节也写不进来。
   */
  function detach(): void {
    detachAllSubscriptions()
    stopAllTimers()
    runs.detach()
  }

  /** 统一处理订阅失败：中止不算错误，其余记入 `error` 并取回错误码 */
  function fail(caught: unknown): JobResult {
    if (caught instanceof Error && caught.name === 'AbortError') {
      return { reply: '', ok: false, run: null }
    }
    lastError.value = caught instanceof Error ? caught.message : String(caught)
    const errorCode = caught instanceof ApiError ? caught.code : undefined
    return { reply: '', ok: false, errorCode, run: null }
  }

  return {
    // 运行态（只读视图，真实数据住在 stores/runs）
    running,
    error,
    phase,
    runSummary,
    snapshot,
    elapsedMs,
    // 动作
    start,
    reconnect,
    detach,
    // 便于上层做「这次任务还是不是我的」判断
    isSubscribed,
  }
}
