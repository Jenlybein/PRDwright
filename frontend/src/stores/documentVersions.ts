/**
 * 文档版本 store：某份文档的版本历史 + 两个写动作（存为新版本 / 恢复）。
 *
 * ## 为什么另开一个 store，而不是挂在 `documents` 上
 *
 * `stores/documents.ts` 已经背了「800ms 防抖 + 单飞 + `changeEpoch` 记账」这一套
 * 手改落库的并发控制。把版本列表、加载态、错误态再叠进去，两套状态会互相干扰
 * （最典型：刷新版本列表时 `loading` 一翻，手改的 UI 跟着变灰）。分开之后，
 * 两边各管一摊：`documents` 管「当前正文的工作副本」，本 store 管「版本链」。
 *
 * ## 归属从哪来
 *
 * 版本属于某份方案里的某一份产物，归属信息本 store 不另存一份——直接读
 * `documents.solutionId`。两份 id 迟早会漂移，而它们本来就该是同一个。
 *
 * ## 写动作之后必须重新拉文档
 *
 * 「存为新版本」与「恢复」都会改变**当前文档**（前者把指针移到新版本，后者把旧版本
 * 搬成新的当前版本）。不重新拉一次，编辑器里的正文还是旧的，用户会以为「点了没反应」。
 */

import { defineStore } from 'pinia'
import { ref } from 'vue'

import {
  checkpointDocument,
  fetchVersions,
  restoreVersion,
} from '@/services/documentService'
import { useDocumentsStore } from '@/stores/documents'
import type { DocumentKind, DocumentVersionSummary } from '@/types'

/** 三份产物各自的版本列表初值 */
function emptyVersions(): Record<DocumentKind, DocumentVersionSummary[]> {
  return { prd: [], 'api-docs': [], prompts: [] }
}

export const useDocumentVersionsStore = defineStore('documentVersions', () => {
  const documents = useDocumentsStore()

  /** 三份文档各自的版本摘要列表（新→旧） */
  const versionsByKind = ref<Record<DocumentKind, DocumentVersionSummary[]>>(emptyVersions())

  /** 是否正在拉取版本列表（抽屉打开时拉一次） */
  const loading = ref(false)

  /** 最近一次读 / 写版本失败的原因；成功时清空 */
  const error = ref('')

  /**
   * 加载请求序号：只有最新一次的结果允许落地。
   *
   * 关掉抽屉再打开另一份文档、或快速切方案时会有两次加载在飞，先发的后到就会把
   * 另一份文档的版本列表填进来。序号让过期结果自己作废。
   */
  let loadToken = 0

  /** 拉取某份文档的版本列表（新→旧）。方案还不存在时什么都不做。 */
  async function load(kind: DocumentKind): Promise<void> {
    const id = documents.solutionId
    if (id === null) return
    const token = ++loadToken
    loading.value = true
    error.value = ''
    try {
      const list = await fetchVersions(id, kind)
      if (token !== loadToken) return
      versionsByKind.value[kind] = list
    } catch (e) {
      if (token !== loadToken) return
      versionsByKind.value[kind] = []
      error.value = e instanceof Error ? e.message : '无法读取版本历史'
    } finally {
      if (token === loadToken) loading.value = false
    }
  }

  /**
   * 跑一次「写版本」的动作，成功后把当前文档与版本列表都对齐。
   *
   * 顺序有讲究：**先把在途的手改冲掉**。`documents.loadAll` 会把「尚未落库的手改」
   * 盖在响应之上（那是正确的——刚敲的字比库里的新），但在这里它会盖住恢复结果，
   * 变成「恢复完看起来没变化」。冲干净之后再写版本，回落的数据才是权威的。
   */
  async function runMutation(
    id: number,
    kind: DocumentKind,
    action: () => Promise<unknown>,
  ): Promise<boolean> {
    error.value = ''
    try {
      await documents.flushPendingSave()
      await action()
    } catch (e) {
      error.value = e instanceof Error ? e.message : '操作失败'
      return false
    }
    await documents.reload()
    await load(kind)
    return true
  }

  /**
   * 把当前正文存为一个新版本（打里程碑）。
   *
   * `content` 由调用方给出——取用户眼前这一版（`documents` 的工作副本在每次输入时
   * 就已同步，比后端的库新）。
   */
  async function checkpoint(kind: DocumentKind, content: string, note = ''): Promise<boolean> {
    const id = documents.solutionId
    if (id === null) return false
    return runMutation(id, kind, () => checkpointDocument(id, kind, content, note))
  }

  /** 把某个历史版本恢复为当前版本（后端实现为「追加一条新版本」） */
  async function restore(kind: DocumentKind, versionId: number): Promise<boolean> {
    const id = documents.solutionId
    if (id === null) return false
    return runMutation(id, kind, () => restoreVersion(id, kind, versionId))
  }

  /** 清空并复位（切换方案 / 重新开始时用）；顺带作废在途的加载结果 */
  function clear(): void {
    loadToken += 1
    versionsByKind.value = emptyVersions()
    loading.value = false
    error.value = ''
  }

  return {
    // 状态
    versionsByKind,
    loading,
    error,
    // 动作
    load,
    checkpoint,
    restore,
    clear,
  }
})
