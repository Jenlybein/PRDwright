/**
 * 文档 store：三份产物正文在前端的**工作副本**。
 *
 * ## 它是只读镜像，不是真源
 *
 * 「文档所有权在后端」落地后，`documents` 表是唯一真源。本 store 存在的理由只有两个：
 *
 * 1. **读**：打开方案 / 生成收尾后按方案 id 拉一份回来，供界面渲染；
 * 2. **提交用户手改**：审阅页里改的字要送到后端去。
 *
 * 因此这里**没有任何「生成结果写回」的路径**——生成结束时后端已经写好，前端只把
 * `done.reply` 当次展示一下，再重新拉一次确认（两者逐字一致，重新拉是为了顺带
 * 拿到正文之外的清单与回执）。
 *
 * ## 为什么手改要防抖 + 单飞
 *
 * 审阅页的编辑器是「边打边存」的：每敲一个字都发一次请求既浪费又会让最后一次
 * 覆盖前一次。防抖把连续输入合并成一次；单飞保证任一时刻至多一个 PUT 在途，
 * 避免同一份文档被交错写入。这两点在 `stores/solutions.ts` 上已经有成熟写法
 * （`scheduleSave` / `flushPendingSave` / `changeEpoch`），这里沿用同一套。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { DocumentsNotFoundError, fetchDocuments, saveDocument } from '@/services/documentService'
import type { DocumentKind, PendingItem, RunSummary } from '@/types'

/** 手改的防抖时长：比方案快照短一些，让「保存」这件事显得更跟手 */
export const DOCUMENT_SAVE_DEBOUNCE_MS = 800

/** 三种产物各自的空值 */
function emptyContents(): Record<DocumentKind, string> {
  return { prd: '', 'api-docs': '', prompts: '' }
}

function emptySummaries(): Record<DocumentKind, RunSummary | null> {
  return { prd: null, 'api-docs': null, prompts: null }
}

/** 三种产物的待确认清单各自的空值 */
function emptyPending(): Record<DocumentKind, PendingItem[]> {
  return { prd: [], 'api-docs': [], prompts: [] }
}

export const useDocumentsStore = defineStore('documents', () => {
  /** 三份正文的工作副本 */
  const contents = ref<Record<DocumentKind, string>>(emptyContents())

  /** 三份文档各自的「最后一次生成」回执；随正文一起从后端取回 */
  const runSummaries = ref<Record<DocumentKind, RunSummary | null>>(emptySummaries())

  /** 三份文档各自的待确认清单；随正文一起从后端取回，前端只读、不修改 */
  const pendingItems = ref<Record<DocumentKind, PendingItem[]>>(emptyPending())

  /** 这批内容属于哪份方案；`null` 表示还没有归属（新建中） */
  const solutionId = ref<number | null>(null)

  /** 是否正在从后端拉取 */
  const loading = ref(false)

  /** 最近一次读取 / 保存失败的原因；成功时清空 */
  const saveError = ref('')

  /**
   * **内容写入**计数：每成功写入一次正文就自增。
   *
   * 三份产物的正文住在后端 `documents` / `document_versions` 里，**不经过会话快照**，
   * 因此 `workflow.changeTick` / `contentTick` 完全看不到它们。而「列表要显示的内容时间」
   * 必须把「生成了一份 PRD」「手改了一段」「恢复了某一版」都算进去，否则会出现
   * 「刚生成完 PRD，列表时间还停在填表单那一刻」。
   *
   * 由 `stores/solutions.ts` 消费（它同时依赖本 store 与 `workflow`，天然是两路计数的
   * 汇合点）。放在这里而不是 `workflow`，是因为反向依赖会成环——`workflow` 已经依赖本 store。
   *
   * **只在写成功之后自增**：失败不记，否则一次网络抖动会被当成「内容变了」写进时间轴。
   */
  const contentWriteTick = ref(0)

  // ------------------------------------------------------------------
  // 内部：防抖与并发控制
  // ------------------------------------------------------------------

  let timer: number | undefined

  /**
   * 待写入后端的改动：`种类 -> 最新正文`。
   *
   * 用「最新值」而不是「所有版本排队」：同一份文档连改三遍，只有最后一遍值得发出去，
   * 中间那两遍没有任何意义。这也让「保存失败后重试」天然正确——重试的是当前内容。
   */
  const pending = new Map<DocumentKind, string>()

  /** 是否有待写入的改动（保存失败时保持置位，等下一次变更或 `flushPendingSave` 再试） */
  let dirty = false

  /**
   * 改动版本号：每标记一次「有改动待落库」自增。
   *
   * 用于回答「在途的那次保存，是否覆盖了它启动之后才发生的改动」。与 `solutions.ts`
   * 同款：用版本号而不是布尔位，是为了让**保存失败**（版本没变）不会立刻触发重试。
   */
  let changeEpoch = 0

  /** 在途的保存请求；`flushPendingSave` 靠它等到保存链排空 */
  let inFlight: Promise<void> | null = null

  /**
   * 加载请求的序号：只有最新一次加载的结果才允许落地。
   *
   * 切换方案时可能有两个 `loadAll` 在飞（打开 A 之后马上切到 B），先发的那个若后到，
   * 会把 B 的正文盖成 A 的。序号让过期结果自己作废，不必去猜网络顺序。
   */
  let loadToken = 0

  function clearTimer(): void {
    if (timer !== undefined) {
      window.clearTimeout(timer)
      timer = undefined
    }
  }

  // ------------------------------------------------------------------
  // 读
  // ------------------------------------------------------------------

  /**
   * 拉取某方案下的全部文档，覆盖工作副本。
   *
   * **未保存的改动会盖回结果之上**：用户刚敲的字还没发出去，此时若因为切阶段触发了
   * 一次重新加载，不能把那些字抹掉——它们才是更新的那一版。
   *
   * 方案不可见（404）时清空内容并记下错误：这时界面不该继续显示上一份方案的正文。
   */
  async function loadAll(id: number): Promise<void> {
    const token = ++loadToken
    solutionId.value = id
    loading.value = true
    saveError.value = ''
    try {
      const records = await fetchDocuments(id)
      if (token !== loadToken) return
      const nextContents = emptyContents()
      const nextSummaries = emptySummaries()
      const nextPending = emptyPending()
      for (const record of records) {
        nextContents[record.kind] = record.content
        nextSummaries[record.kind] = record.runSummary
        nextPending[record.kind] = record.pendingItems
      }
      // 尚未落库的手改优先：它们在时间上更新于这次响应
      for (const [kind, content] of pending) nextContents[kind] = content
      contents.value = nextContents
      runSummaries.value = nextSummaries
      pendingItems.value = nextPending
    } catch (error) {
      if (token !== loadToken) return
      if (error instanceof DocumentsNotFoundError) {
        contents.value = emptyContents()
        runSummaries.value = emptySummaries()
        pendingItems.value = emptyPending()
      }
      saveError.value = error instanceof Error ? error.message : '无法读取文档'
    } finally {
      if (token === loadToken) loading.value = false
    }
  }

  /**
   * 用**已登记**的方案 id 重新拉取。
   *
   * 生成收尾时调用：那一刻后端刚写完 `documents`，拉一次即与它对齐。
   * 没有归属（方案还没建出来）时什么都不做。
   */
  async function reload(): Promise<void> {
    const id = solutionId.value
    if (id === null) return
    await loadAll(id)
  }

  /**
   * 登记这批内容属于哪份方案，**不发起请求**。
   *
   * 用在「方案刚被创建」的时刻：那时后端还没有任何文档（正文还在生成中），
   * 拉一次只会白跑一趟请求；等生成收尾时自然会重新拉。
   */
  function attach(id: number | null): void {
    solutionId.value = id
  }

  // ------------------------------------------------------------------
  // 写（用户手改）
  // ------------------------------------------------------------------

  /** 只改本地工作副本，不落库（生成收尾时的即时展示用） */
  function setLocal(kind: DocumentKind, content: string): void {
    contents.value[kind] = content
  }

  /**
   * 报告「刚刚写入了一份内容」。
   *
   * 给**不经本 store 的写入**用：生成收尾（后端在 `done` 之前就已落稿）、版本操作
   * （checkpoint / 恢复 / 删历史版本）都改的是后端，本 store 只是在旁边看着。经办方在
   * **写成功之后**调用——失败不报，否则失败的生成也会被记成「内容变了」。
   */
  function markContentWritten(): void {
    contentWriteTick.value += 1
  }

  /**
   * 提交用户手改：先更新本地（界面立刻可见），再把落库排进防抖队列。
   *
   * 方案还没有 id 时不排队——没有方案可挂，请求发出去只会 404。这种情况只可能
   * 发生在「方案尚未创建」时，而那时也不会有文档可改。
   */
  function save(kind: DocumentKind, content: string): void {
    contents.value[kind] = content
    if (solutionId.value === null) return
    pending.set(kind, content)
    dirty = true
    changeEpoch += 1
    clearTimer()
    timer = window.setTimeout(() => {
      timer = undefined
      scheduleSave()
    }, DOCUMENT_SAVE_DEBOUNCE_MS)
  }

  /**
   * 发起一次保存——**所有落库动作的唯一入口**，保证任一时刻至多一个请求在途。
   *
   * 在途时直接返回：期间发生的改动会体现在 `changeEpoch` 上，由收尾逻辑决定要不要补存。
   */
  function scheduleSave(): void {
    if (inFlight) return
    clearTimer()
    const epochAtStart = changeEpoch
    inFlight = saveNow().finally(() => {
      inFlight = null
      if (changeEpoch !== epochAtStart) scheduleSave()
    })
  }

  /**
   * 把待写队列里的内容逐个发给后端。
   *
   * 一次只发一条（单飞）：三份文档同属一份方案，并发写没有收益，反而让失败重试的
   * 记账变复杂。失败的那条放回队列——**内容还在本地，下一次变更或 flush 会再试**，
   * 用户不会因为一次网络抖动就丢掉刚敲的字。
   */
  async function saveNow(): Promise<void> {
    const batch = [...pending.entries()]
    for (const [kind, content] of batch) {
      const id = solutionId.value
      if (id === null) return
      pending.delete(kind)
      try {
        await saveDocument(id, kind, content)
        saveError.value = ''
        // 这一份真的写进库了——报告一次内容变更，让方案列表的时间与排序跟着走
        markContentWritten()
      } catch (error) {
        if (error instanceof DocumentsNotFoundError) {
          // 方案没了：再重试也没有意义，记下原因让用户知道
          saveError.value = error.message
          continue
        }
        pending.set(kind, content)
        saveError.value = error instanceof Error ? error.message : '保存文档失败'
      }
    }
    // 只要队列空了就认为「本轮改动已处理完」；失败的那几条已放回队列，
    // 由 `dirty` 保持置位（不清），留给下一次变更或 flush 重试。
    dirty = pending.size > 0
  }

  /**
   * 立即保存（若有待保存的改动），并等待保存链彻底排空。
   *
   * 用在「即将切换方案 / 离开页面」这类时机：防抖窗口里可能还压着一次未发出的保存，
   * 不等它就走，最后一次改动就丢了。
   */
  async function flushPendingSave(): Promise<void> {
    clearTimer()
    if (dirty || pending.size > 0) scheduleSave()
    while (inFlight) await inFlight
  }

  /** 供测试与调试：是否有尚未落库的改动 */
  function hasPendingSave(): boolean {
    return dirty || pending.size > 0
  }

  /** 清空工作副本并复位队列（「重新开始」/「删除方案」时用） */
  function clear(): void {
    clearTimer()
    pending.clear()
    dirty = false
    changeEpoch += 1
    contents.value = emptyContents()
    runSummaries.value = emptySummaries()
    pendingItems.value = emptyPending()
    solutionId.value = null
    saveError.value = ''
    // 让在途的加载结果作废：它的响应属于上一份方案，落地会把空内容又填回去
    loadToken += 1
  }

  // ------------------------------------------------------------------
  // 派生
  // ------------------------------------------------------------------

  function contentOf(kind: DocumentKind): string {
    return contents.value[kind]
  }

  function runSummaryOf(kind: DocumentKind): RunSummary | null {
    return runSummaries.value[kind]
  }

  /** 某份文档的待确认清单（只读，由后端随正文落库） */
  function pendingItemsOf(kind: DocumentKind): PendingItem[] {
    return pendingItems.value[kind]
  }

  const hasPrd = computed(() => contents.value.prd.trim() !== '')
  const hasApiDocs = computed(() => contents.value['api-docs'].trim() !== '')
  const hasPrompts = computed(() => contents.value.prompts.trim() !== '')

  /** 该方案下是否已有任何一份文档（决定「产物」相关入口是否出现） */
  const hasAny = computed(() => hasPrd.value || hasApiDocs.value || hasPrompts.value)

  return {
    // 状态
    contents,
    runSummaries,
    pendingItems,
    solutionId,
    loading,
    saveError,
    contentWriteTick,
    hasPrd,
    hasApiDocs,
    hasPrompts,
    hasAny,
    // 动作
    loadAll,
    reload,
    attach,
    setLocal,
    save,
    markContentWritten,
    flushPendingSave,
    hasPendingSave,
    clear,
    // 派生
    contentOf,
    runSummaryOf,
    pendingItemsOf,
  }
})
