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

import {
  DocumentVersionConflictError,
  DocumentsNotFoundError,
  fetchDocuments,
  saveDocument,
} from '@/services/documentService'
import type {
  DerivedFrom,
  DocumentKind,
  PendingItem,
  ReviewConclusion,
  RunSummary,
} from '@/types'

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

/** 三种产物「是否基于旧版上游」的空值 */
function emptyStale(): Record<DocumentKind, boolean> {
  return { prd: false, 'api-docs': false, prompts: false }
}

/** 三种产物上游锚点的空值 */
function emptyDerived(): Record<DocumentKind, DerivedFrom | null> {
  return { prd: null, 'api-docs': null, prompts: null }
}

/** 三种产物「当前版本的审核记录」的空值 */
function emptyReviewSnapshots(): Record<DocumentKind, ReviewSnapshot> {
  return {
    prd: { review: null, reviewFailedAt: null, versionNo: 0, partial: false },
    'api-docs': { review: null, reviewFailedAt: null, versionNo: 0, partial: false },
    prompts: { review: null, reviewFailedAt: null, versionNo: 0, partial: false },
  }
}

/**
 * 一份文档「当前版本的审核记录」。
 *
 * 三格同源：都描述**同一个版本行**，随同一次 `GET .../documents` 回来。从前它们是三张
 * 平行的 `Record<DocumentKind, …>`，五处接线各写一遍（读、读失败、清空、setReview、
 * setReviewFailed）——加一格就要记得改五处，漏一处只表现为「刷新后某个状态不对」。
 * 收成一条记录之后，「加一格」只剩类型上的一处。
 */
export interface ReviewSnapshot {
  /** 当前版本那一条结论；`null` = 这一版没审过或没审成（**不等于通过**） */
  review: ReviewConclusion | null
  /**
   * 「这一版上一次审核试过但没跑成」的时间；`null` = 没有失败记录。
   *
   * 它是**落库事实**（后端 `document_versions.review_failed_at`），因此刷新、换设备后
   * 仍读得到——「审核失败」不再只活在一次会话里。
   */
  reviewFailedAt: string | null
  /** 当前版本号（0 = 后端没给，界面不显示） */
  versionNo: number
  /**
   * 当前版本是不是「部分完成」（票 #85）：正文可用但不完整。
   *
   * 它改变的是**审核区的读法**：这一版没有结论、也没有「审核失败」可讲（旧结论属于
   * 别的正文）。界面据此提示「已生成的部分可以先用」，并让审核面板显示「未审核」。
   */
  partial: boolean
}

export const useDocumentsStore = defineStore('documents', () => {
  /** 三份正文的工作副本 */
  const contents = ref<Record<DocumentKind, string>>(emptyContents())

  /** 三份文档各自的「最后一次生成」回执；随正文一起从后端取回 */
  const runSummaries = ref<Record<DocumentKind, RunSummary | null>>(emptySummaries())

  /** 三份文档各自的待确认清单；随正文一起从后端取回，前端只读、不修改 */
  const pendingItems = ref<Record<DocumentKind, PendingItem[]>>(emptyPending())

  /**
   * 三份文档各自的「是否基于旧版上游」。
   *
   * 由后端按 `metadata.derived_from` 记录的上游内容指纹与上游**当前**指纹比较得出；
   * 前端只负责展示黄条，不自己算——比较要用上游的 `content_sha`，那是后端才有的。
   */
  const stale = ref<Record<DocumentKind, boolean>>(emptyStale())

  /** 三份文档的上游锚点（用于提示条上说明「基于哪一版」） */
  const derivedFrom = ref<Record<DocumentKind, DerivedFrom | null>>(emptyDerived())

  /**
   * 三份文档**当前版本**的审核记录（结论 / 失败时间 / 版本号），一张表。
   *
   * 它是审阅页结论面板的**唯一真源**：随 `GET .../documents` 返回，因此刷新、换设备、
   * 重连之后读到的仍是同一条。订阅期间收到的 `review` 事件只做**即时更新**
   * （`setReview` / `setReviewFailed`）——落库发生在任务收尾，晚一步才出现在下一次
   * `loadAll` 里。
   */
  const reviewSnapshots = ref<Record<DocumentKind, ReviewSnapshot>>(emptyReviewSnapshots())

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
   * 本轮保存链里有没有失败（网络 / 版本冲突都算）。
   *
   * `flushPendingSave` 的返回值要回答「保存链是不是干净地排空了」：`stores/documentVersions.ts`
   * 在「存为新版本 / 恢复」之前会先冲掉手改，冲不干净就不该继续——否则会在「内容没落库」
   * 的前提下弹出「已存为新版本」的成功提示（保存失败不能显示成功）。
   */
  let saveFailed = false

  /**
   * 版本锁拒绝了写入的种类（票 #81）。
   *
   * 它在 `reload` / `loadAll` 之后清空：这时本地看到的才是库里的新版本。在此之前
   * **不再把手改排进队列**——带着旧版本号重试只会被后端一次次拒绝，而万一后端放行就是
   * 把别人刚写好的正文覆盖掉。用户的改动仍留在工作副本里（不静默丢），提示让他刷新。
   */
  const conflicted = new Set<DocumentKind>()

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
      const nextStale = emptyStale()
      const nextDerived = emptyDerived()
      const nextReviews = emptyReviewSnapshots()
      for (const record of records) {
        const local = reviewSnapshots.value[record.kind]
        nextContents[record.kind] = record.content
        nextSummaries[record.kind] = record.runSummary
        nextPending[record.kind] = record.pendingItems
        nextStale[record.kind] = record.stale
        nextDerived[record.kind] = record.derivedFrom
        nextReviews[record.kind] = {
          review: record.review,
          // 失败是**落库事实**，服务端给了就用服务端的；服务端没给、而这一版又确实没有
          // 结论时，保留本会话刚看到的那次失败——生成链路的审核降级不落库（它不是
          // 「重审失败」），本地这条只活到本会话结束。拿到真结论就一并清掉。
          //
          // **部分完成版本例外**（票 #85）：那一版的审核区必须是空的。把本会话里上一版
          // 的失败时间留在这里，会让用户读到「这份没审成的正文失败于某时」——而它
          // 根本没被审过，旧时间戳属于别的正文。
          reviewFailedAt: record.partial
            ? null
            : (record.reviewFailedAt ?? (record.review === null ? local.reviewFailedAt : null)),
          versionNo: record.versionNo,
          partial: record.partial,
        }
      }
      // 尚未落库的手改优先：它们在时间上更新于这次响应
      for (const [kind, content] of pending) nextContents[kind] = content
      contents.value = nextContents
      runSummaries.value = nextSummaries
      pendingItems.value = nextPending
      stale.value = nextStale
      derivedFrom.value = nextDerived
      reviewSnapshots.value = nextReviews
      // 拉回库里的版本之后，本地这个「以为的版本」才重新可信：解除版本冲突的封锁，
      // 之后的手改重新排队（此前一直不排队，避免带旧版本号覆盖已更新的正文）。
      conflicted.clear()
      saveFailed = false
    } catch (error) {
      if (token !== loadToken) return
      if (error instanceof DocumentsNotFoundError) {
        contents.value = emptyContents()
        runSummaries.value = emptySummaries()
        pendingItems.value = emptyPending()
        stale.value = emptyStale()
        derivedFrom.value = emptyDerived()
        reviewSnapshots.value = emptyReviewSnapshots()
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

  // ------------------------------------------------------------------
  // 审核结论（订阅期间的即时更新；落库那条以 `loadAll` 为准）
  // ------------------------------------------------------------------

  /**
   * 收到一条**真正跑出来的**结论（`pass` / `revise`）：立刻显示，并清掉「上次没跑成」。
   *
   * 它是 `review` 事件的落点——落库发生在任务收尾，比这条事件晚一步；不即时更新的话，
   * 用户要等一次 `reload` 往返才看得到结论（表现是「审核完了，面板还是空的」）。
   * 版本号不动：这次审核并不产生新版本。
   */
  function setReview(kind: DocumentKind, review: ReviewConclusion): void {
    const current = reviewSnapshots.value[kind]
    reviewSnapshots.value[kind] = { ...current, review, reviewFailedAt: null }
  }

  /**
   * 记一次**没跑成**的审核（`review` 事件判定为空串，或重审任务失败）。
   *
   * **不写结论**：没有结论 ≠ 通过（失败不写占位）。失败时间戳由后端持久化在
   * `document_versions.review_failed_at` 上，刷新后 `loadAll` 会把它读回来；
   * 这里传 `at` 只是为了订阅期间立刻见到「审核失败（时间）」。
   *
   * `at` 缺省取当前时刻：即时展示不必等一次往返，下一次 `loadAll` 以后端的值为准。
   */
  function setReviewFailed(kind: DocumentKind, at: string = new Date().toISOString()): void {
    const current = reviewSnapshots.value[kind]
    reviewSnapshots.value[kind] = { ...current, reviewFailedAt: at }
  }

  /**
   * 提交用户手改：先更新本地（界面立刻可见），再把落库排进防抖队列。
   *
   * 方案还没有 id 时不排队——没有方案可挂，请求发出去只会 404。这种情况只可能
   * 发生在「方案尚未创建」时，而那时也不会有文档可改。
   *
   * **版本冲突未解除时只更新本地、不排队**（票 #81）：带着旧版本号重试只会被后端拒绝，
   * 而这个「拒绝」正是保护「不把旧内容覆盖到刚生成的新正文上」的那道锁。用户的字仍在
   * 工作副本里，提示他刷新后重试。
   */
  function save(kind: DocumentKind, content: string): void {
    contents.value[kind] = content
    if (solutionId.value === null) return
    if (conflicted.has(kind)) return
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
   *
   * **版本冲突是例外**（票 #81）：那条**不放回队列**。冲突的含义是「库里已经有了更新
   * 的一版」，再试一次就是拿旧内容覆盖新正文——所以只提示「请刷新后重试」，并把这种类
   * 封锁到下一次 `loadAll`（拿到库里的新版本号）为止。本地内容不回滚：不静默丢改动。
   *
   * 每次保存都带 `expectedVersionNo`（本 store 以为的当前版本号）：手改与生成任务在同一
   * 份文档上赛跑时，由后端那道版本锁决定谁有资格写，而不是「后到的覆盖先到的」。
   */
  async function saveNow(): Promise<void> {
    saveFailed = false
    const batch = [...pending.entries()]
    let savedPrd = false
    for (const [kind, content] of batch) {
      const id = solutionId.value
      if (id === null) return
      pending.delete(kind)
      try {
        const record = await saveDocument(
          id,
          kind,
          content,
          reviewSnapshots.value[kind].versionNo,
        )
        saveError.value = ''
        // 当前版本号以写入结果为准：首次手改会在后端建出版本 1，下一笔保存必须带 1
        // 而不是还拿 0 去比对（否则自己把自己锁在冲突里）。
        reviewSnapshots.value[kind] = {
          ...reviewSnapshots.value[kind],
          versionNo: record.versionNo,
          // 手改不会让「这一版不完整」这件事消失（部分完成的正文仍是那一份），
          // 因此标记以写入结果为准，与版本号一起对齐。
          partial: record.partial,
        }
        // 这一份真的写进库了——报告一次内容变更，让方案列表的时间与排序跟着走
        markContentWritten()
        if (kind === 'prd') savedPrd = true
      } catch (error) {
        saveFailed = true
        if (error instanceof DocumentsNotFoundError) {
          // 方案没了：再重试也没有意义，记下原因让用户知道
          saveError.value = error.message
          continue
        }
        if (error instanceof DocumentVersionConflictError) {
          // 正文已被更新：**不覆盖、不重试**。本地改动保留，等用户刷新后重来。
          conflicted.add(kind)
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
    // PRD 正文变了 → 下游是否过期要重算。拉一次拿后端算好的 stale 标记；
    // 不 await，避免把「保存」这个动作拖成两次往返。
    if (savedPrd) void reload()
  }

  /**
   * 立即保存（若有待保存的改动），并等待保存链彻底排空。
   *
   * 用在「即将切换方案 / 离开页面」这类时机：防抖窗口里可能还压着一次未发出的保存，
   * 不等它就走，最后一次改动就丢了。
   *
   * Returns:
   *   `true` = 队列清空且本轮没有失败；`false` = 至少一条没写成功（网络 / 版本冲突）。
   *   调用方（版本抽屉的「存为新版本 / 恢复」）据此决定要不要继续——
   *   **保存失败不能显示成功**。
   */
  async function flushPendingSave(): Promise<boolean> {
    clearTimer()
    if (dirty || pending.size > 0) scheduleSave()
    while (inFlight) await inFlight
    return pending.size === 0 && !saveFailed
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
    conflicted.clear()
    saveFailed = false
    changeEpoch += 1
    contents.value = emptyContents()
    runSummaries.value = emptySummaries()
    pendingItems.value = emptyPending()
    stale.value = emptyStale()
    derivedFrom.value = emptyDerived()
    reviewSnapshots.value = emptyReviewSnapshots()
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

  /** 某份文档是否基于旧版上游（后端算好，前端只读） */
  function staleOf(kind: DocumentKind): boolean {
    return stale.value[kind]
  }

  /** 某份文档的上游锚点（提示条用；没有则为 null） */
  function derivedFromOf(kind: DocumentKind): DerivedFrom | null {
    return derivedFrom.value[kind]
  }

  /** 某份文档当前版本的审核结论；`null` 表示这一版没审过（不等于通过） */
  function reviewOf(kind: DocumentKind): ReviewConclusion | null {
    return reviewSnapshots.value[kind].review
  }

  /** 这份文档当前版本上一次重审没跑成的时间；`null` = 没有失败记录 */
  function reviewFailedAtOf(kind: DocumentKind): string | null {
    return reviewSnapshots.value[kind].reviewFailedAt
  }

  /** 这份文档的当前版本号（0 = 后端没给，界面不显示） */
  function versionNoOf(kind: DocumentKind): number {
    return reviewSnapshots.value[kind].versionNo
  }

  /**
   * 这份正文是不是**可用但不完整**（票 #85）。
   *
   * 读的是当前版本那个落库标记（流式期间不会变）：失败收尾把半截正文升格成版本时，
   * `loadAll` 之后它为 true。界面据此提示「已生成的部分可以先用」，
   * 并把用户的落点带到审阅页——那里能下载、能修、能重生成。
   */
  function partialOf(kind: DocumentKind): boolean {
    return reviewSnapshots.value[kind].partial
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
    stale,
    derivedFrom,
    reviewSnapshots,
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
    setReview,
    setReviewFailed,
    flushPendingSave,
    hasPendingSave,
    clear,
    // 派生
    contentOf,
    runSummaryOf,
    pendingItemsOf,
    staleOf,
    derivedFromOf,
    reviewOf,
    reviewFailedAtOf,
    versionNoOf,
    partialOf,
  }
})
