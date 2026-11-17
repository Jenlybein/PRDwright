/**
 * 任务七态的**唯一文案与视觉档位表**（票 #84）。
 *
 * 后端把 `status` 从三个值扩成七个值（`pending` / `running` / `cancelling` /
 * `succeeded` / `failed` / `superseded` / `cancelled`）。前端不再自己按错误码猜
 * 「是不是被取消」，也不再各处各写一份文案——**要显示状态就查这张表**。
 *
 * 为什么「被取代」与「已取消」必须分开
 * ------------------------------------
 * 两者看起来一样（都没有结果），含义却相反：前者是系统让位（同一份文档有更新的任务
 * 接手），后者是用户的明确意图（按了停止 / 删了方案）。合并成一句「失败」之后，
 * 用户会以为自己的稿子出了问题，排查时也永远分不清发生了什么。
 *
 * 视觉档位只有三类：
 * - `active`：还没收尾（排队中 / 执行中 / 取消中）；
 * - `done`：正常结局（已定稿）；
 * - `warning`：用户/系统主动让位（已取消 / 被取代）——**不是红色失败**；
 * - `failure`：真失败。
 */

import type { JobState } from '@/types'

/** 七态各自的中文文案（用户端与管理端共用同一套话，不另造词）。 */
export const JOB_STATE_LABELS: Record<JobState, string> = {
  pending: '排队中',
  running: '执行中',
  cancelling: '取消中',
  succeeded: '已定稿',
  failed: '失败',
  superseded: '被取代',
  cancelled: '已取消',
}

/** 视觉档位（决定徽标颜色 / 是否红色）。 */
export type JobStateTone = 'active' | 'done' | 'warning' | 'failure'

export const JOB_STATE_TONES: Record<JobState, JobStateTone> = {
  pending: 'active',
  running: 'active',
  cancelling: 'active',
  succeeded: 'done',
  failed: 'failure',
  superseded: 'warning',
  cancelled: 'warning',
}

/** 还没收尾的三个状态（「在途」的唯一定义）。 */
export const ACTIVE_JOB_STATES: JobState[] = ['pending', 'running', 'cancelling']

/** 后端 `?status=` 接受逗号分隔多值：查询「在途」时传这一个常量，不各自拼一遍。 */
export const ACTIVE_JOB_STATE_QUERY = ACTIVE_JOB_STATES.join(',')

export function isActiveJobState(state: string): state is JobState {
  return (ACTIVE_JOB_STATES as string[]).includes(state)
}

/** 终态：不会再变（已定稿 / 失败 / 被取代 / 已取消）。 */
export function isTerminalJobState(state: string): state is JobState {
  return (
    state === 'succeeded' ||
    state === 'failed' ||
    state === 'superseded' ||
    state === 'cancelled'
  )
}

/** 按状态取文案；未知取值（旧后端 / 坏数据）按「执行中」兜底，不抛错。 */
export function jobStateLabel(state: string): string {
  return JOB_STATE_LABELS[state as JobState] ?? JOB_STATE_LABELS.running
}

/** 按状态取视觉档位；未知取值按「进行类」兜底。 */
export function jobStateTone(state: string): JobStateTone {
  return JOB_STATE_TONES[state as JobState] ?? 'active'
}

/**
 * 用户在界面上看到的「运行视图徽标」文案。
 *
 * 与 `JOB_STATE_LABELS` 的区别只在**这层界面还知道任务干了什么**（修复 / 重审 /
 * 优化 / 改写），所以运行中要按动作细分；而排队中 / 取消中 / 终态不需要再细分动作。
 */
export function runBadgeLabel(
  state: string,
  action: 'fix' | 'review' | 'optimize' | 'rewrite' | 'generate',
  opts: { waitingRetry?: boolean; cancelling?: boolean } = {},
): string {
  if (state === 'cancelling' || opts.cancelling) return JOB_STATE_LABELS.cancelling
  if (state === 'pending' || opts.waitingRetry) {
    return opts.waitingRetry ? '等待重试' : JOB_STATE_LABELS.pending
  }
  if (isTerminalJobState(state)) return jobStateLabel(state)
  const runningByAction: Record<typeof action, string> = {
    fix: '修复中',
    review: '重审中',
    optimize: '优化中',
    rewrite: '改写中',
    generate: '生成中',
  }
  return runningByAction[action]
}
