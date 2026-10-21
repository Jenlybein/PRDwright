/**
 * 测试辅助：把前端状态（camelCase）转成提交给后端的形状（snake_case）。
 *
 * 为什么要这一层：方案的保存载荷从「一整段 JSON」变成了结构化字段，
 * 用例里那些 `data: JSON.stringify(snapshot)` 的写法要逐个改成 `state: toState(snapshot)`。
 * 把这个映射收在一处，用例仍然只描述「我这份状态长什么样」，
 * 不必在每个 it 里重抄一遍 12 个字段的拼写。
 */

/** 任意形状的状态对象（用例里常写成半份） */
import type { SolutionStatePayload } from '../../types'

export type LooseState = Record<string, unknown>

export function toState(snapshot: LooseState = {}): SolutionStatePayload {
  const quick = (snapshot.quickInput ?? {}) as Record<string, unknown>
  const paste = (snapshot.pastePairInput ?? {}) as Record<string, unknown>
  return {
    view_state: typeof snapshot.viewState === 'string' ? snapshot.viewState : '',
    entry_mode: typeof snapshot.entryMode === 'string' ? snapshot.entryMode : '',
    chat_tab: typeof snapshot.chatTab === 'string' ? snapshot.chatTab : '',
    summary: snapshot.summary ?? null,
    quick_product_name: typeof quick.productName === 'string' ? quick.productName : '',
    quick_requirements: typeof quick.requirements === 'string' ? quick.requirements : '',
    draft_prd_text: typeof snapshot.debugPrdText === 'string' ? snapshot.debugPrdText : '',
    paste_prd_text: typeof paste.prd === 'string' ? paste.prd : '',
    paste_api_docs_text: typeof paste.apiDocs === 'string' ? paste.apiDocs : '',
    active_job_id: typeof snapshot.activeJobId === 'number' ? snapshot.activeJobId : null,
    active_job_kind: typeof snapshot.activeJobKind === 'string' ? snapshot.activeJobKind : '',
    active_job_doc_kind:
      typeof snapshot.activeJobDocKind === 'string' ? snapshot.activeJobDocKind : '',
  } as SolutionStatePayload
}
