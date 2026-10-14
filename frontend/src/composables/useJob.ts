/**
 * 生成任务生命周期编排。
 *
 * 把「一次生成」从「一次浏览器请求」提升为「一个后端任务」：`start` 创建任务并订阅，
 * `reconnect` 在刷新后凭任务指针**静默重连**（后端先发 `snapshot` 补全已产出正文，
 * 再接 live），`detach` 只退订、**不取消服务端任务**——这正是「生成与连接解耦」的落点。
 *
 * **预览的单一出口仍是 `useDocumentPipeline`**：本 composable 只负责任务生命周期与
 * 状态记录，不直接写预览。它把 `onChunk` / `onPart` / `onStage` / `onSnapshot` 回调
 * 原样透传给调用方提供的 handlers，由流水线决定这些事件怎么落到预览上。
 *
 * `runSummary` 只存进 ref、**不做任何 UI**（token 计量留待后续阶段）。
 */

import { ref } from 'vue'

import {
  createJob,
  readJobStream,
  type CreateJobPayload,
  type StreamHandlers,
} from '@/services/api'
import { ApiError } from '@/services/errors'
import { useSolutionsStore } from '@/stores/solutions'
import { useWorkflowStore } from '@/stores/workflow'
import type { DocKind, JobSnapshot, RunSummary, ViewState } from '@/types'

/** 文档种类 → 对应的「生成中」阶段；`optimize` 就地流式、不改变阶段，故为 null */
const GENERATING_STATE: Record<DocKind, ViewState | null> = {
  prd: 'generating-prd',
  'api-docs': 'generating-api-docs',
  prompts: 'generating-prompts',
  optimize: null,
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
}

/**
 * 生成任务编排 composable。
 *
 * 内部持有 `running` / `error` / `phase` / `runSummary` 四个运行态 ref，
 * 以及最近一次 `snapshot`（供上层判断任务终态）。
 */
export function useJob() {
  const workflow = useWorkflowStore()
  const solutions = useSolutionsStore()

  /** 是否有任务订阅进行中 */
  const running = ref(false)
  /** 最近一次失败原因；新一轮开始时清空 */
  const error = ref('')
  /** 最近的阶段（来自 stage 事件或 snapshot） */
  const phase = ref('')
  /** 任务收尾摘要（**只记录，不做 UI**） */
  const runSummary = ref<RunSummary | null>(null)
  /** 最近一次收到的快照（供上层判断任务是否已终态） */
  const snapshot = ref<JobSnapshot | null>(null)
  /** `detach()` 用的控制器：退订而不取消服务端任务 */
  let controller: AbortController | null = null

  /** 包装 handlers：在透传的同时记录 phase / runSummary / snapshot */
  function wrapHandlers(handlers: StreamHandlers): StreamHandlers {
    return {
      ...handlers,
      onSnapshot: (snap) => {
        snapshot.value = snap
        phase.value = snap.phase
        handlers.onSnapshot?.(snap)
      },
      onRunSummary: (summary) => {
        runSummary.value = summary
        handlers.onRunSummary?.(summary)
      },
    }
  }

  /**
   * 开一次生成任务：确保方案存在 → 创建任务 → 记下指针 + 切生成态 → 订阅。
   *
   * 两次 `flushPendingSave` 是刻意的：第一次保证「有内容的方案」先落库、拿到 id，
   * 任务创建时才能带上 `solution_id`；第二次在写入 `activeJobId` 后强制落库，
   * 确保任务指针进了后端快照——**刷新后能重连，全靠这一步**。
   */
  async function start(
    kind: DocKind,
    input: JobStartInput,
    handlers: StreamHandlers,
  ): Promise<JobResult> {
    running.value = true
    error.value = ''
    phase.value = ''
    runSummary.value = null
    snapshot.value = null
    controller = new AbortController()

    try {
      // 1. 先落一次：把「已有内容」的方案建出来，任务才有 solution_id 可挂
      await solutions.flushPendingSave()

      // 2. 创建任务
      const created = await createJob({ kind, solutionId: solutions.currentId, ...input })

      // 3. 记下任务指针并切到生成态（goTo 在 busy 置位之前调用，才不会被守卫拦下）
      workflow.activeJobId = created.jobId
      workflow.activeJobKind = kind
      const target = GENERATING_STATE[kind]
      if (target) workflow.goTo(target)
      workflow.docGenerating = true

      // 4. 再次强制落库：把任务指针写进快照
      await solutions.flushPendingSave()

      // 5. 订阅事件流
      const result = await readJobStream(created.jobId, wrapHandlers(handlers), controller.signal)
      return { reply: result.reply, ok: true }
    } catch (caught) {
      return _fail(caught)
    } finally {
      running.value = false
      workflow.docGenerating = false
      controller = null
    }
  }

  /**
   * 静默重连一个已有任务：先收 `snapshot` 补全正文，再接 live。
   *
   * 与 `start` 的区别是不创建任务、不改阶段（阶段已由快照恢复保留为 `generating-*`）。
   */
  async function reconnect(jobId: number, handlers: StreamHandlers): Promise<JobResult> {
    running.value = true
    error.value = ''
    snapshot.value = null
    controller = new AbortController()

    try {
      workflow.docGenerating = true
      const result = await readJobStream(jobId, wrapHandlers(handlers), controller.signal)
      return { reply: result.reply, ok: true }
    } catch (caught) {
      return _fail(caught)
    } finally {
      running.value = false
      workflow.docGenerating = false
      controller = null
    }
  }

  /**
   * 只退订：中止本地读取，**不取消服务端任务**。
   *
   * 这是本期「生成与连接解耦」的直接体现——离开页面 / 刷新只影响这一次订阅，
   * 后端任务照跑；下次回来 `reconnect` 即可续上。
   */
  function detach(): void {
    controller?.abort()
    controller = null
    running.value = false
    workflow.docGenerating = false
  }

  /** 统一处理订阅失败：中止不算错误，其余记入 `error` 并取回错误码 */
  function _fail(caught: unknown): JobResult {
    if (caught instanceof Error && caught.name === 'AbortError') {
      return { reply: '', ok: false }
    }
    error.value = caught instanceof Error ? caught.message : String(caught)
    const errorCode = caught instanceof ApiError ? caught.code : undefined
    return { reply: '', ok: false, errorCode }
  }

  return {
    // 运行态
    running,
    error,
    phase,
    runSummary,
    snapshot,
    // 动作
    start,
    reconnect,
    detach,
  }
}
