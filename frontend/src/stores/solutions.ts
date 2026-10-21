/**
 * 方案 store：把会话状态持久化到后端，并维护左侧列表。
 *
 * ## 为什么保存分「创建」和「更新」两条路径
 *
 * 方案是**有内容了才建**的（见下方 `shouldCreate`）。在创建之前，地址是 `/v2`（没有 id），
 * 因此第一次保存必须 `POST`，拿到 id 后 `router.replace` 到 `/v2/{id}`，此后一律 `PUT`。
 * 这样处理的好处是：用户只是点进入口看一眼就退回，**不会**在数据库里留下一堆空方案。
 *
 * ## 防抖与并发
 *
 * 状态变化 → 防抖 1.5s → 保存。生成期间**也**保存：快照里只有任务指针
 * （`activeJobId` / `activeJobKind`），半截正文不进快照（见 `shouldPersistStage`）。
 *
 * 落库是**单飞**的：所有保存都经 `scheduleSave` 发起，保证任一时刻至多一个请求在途。
 * 这在创建路径上是硬要求——`createSolution` 返回之前 `currentId` 仍是 `null`，
 * 两个并发请求会各自 `POST` 一次，库里就多出一份一模一样的方案（列表里两条同名记录）。
 * 保存期间若有新的变化进来，只记「还得再存一次」，等当前这次回来后自动补存，
 * 避免同一份方案被交错写入。
 *
 * ## 标题
 *
 * 自动取产品名，取不到则用带日期的默认标题。**每次保存都重算**——用户往往是先填了
 * 一堆功能描述，最后才回头补产品名。
 */

import { defineStore } from 'pinia'
import { computed, nextTick, ref, watch } from 'vue'

import {
  createSolution,
  deleteSolution,
  fetchSolution,
  fetchSolutions,
  fetchTurns,
  clearTurns,
  updateSolution,
} from '@/services/solutionService'
import { SolutionNotFoundError } from '@/services/solutionService'
import { detachAll as detachAllSubscriptions } from '@/services/runSubscriptions'
import { useDocumentsStore } from '@/stores/documents'
import { useRunsStore } from '@/stores/runs'
import { useWorkflowStore } from '@/stores/workflow'
import type {
  ChatStage,
  EntryMode,
  SessionDataV2,
  SolutionSavePayload,
  SolutionStatePayload,
  SolutionSummary,
} from '@/types'
import { fromStatePayload, normalizeSessionSnapshot } from '@/utils/workflow'

/** 防抖时长：既能挡住连续输入，又不会让用户觉得「怎么还没存上」 */
export const SAVE_DEBOUNCE_MS = 1500

/** 入口模式 → 中文名，用于列表展示 */
const MODE_LABELS: Record<EntryMode, string> = {
  structured: '完整流程',
  'prd-quick': '快捷流程',
  'prompt-debug': '导入 PRD',
  'prd-and-api-paste': '导入 PRD + 接口文档',
}

/** 阶段 → 中文名：列表里显示「进行到哪一步」 */
const STAGE_LABELS: Record<string, string> = {
  entry: '未开始',
  form: '填写需求',
  chatting: 'AI 对话',
  'summary-diff': '确认摘要',
  'generating-prd': '生成 PRD',
  'review-prd': '已出 PRD',
  'generating-api-docs': '生成接口文档',
  'review-api-docs': '已出接口文档',
  'generating-prompts': '生成提示词',
  'review-prompts': '已出提示词',
  'prd-quick': '描述需求',
  'prompt-debug': '粘贴 PRD',
  'prd-and-api-paste': '粘贴 PRD 与接口文档',
  done: '已完成',
}

/** 取当前 UTC 日期，`MM-DD HH:mm` 形式，用于默认标题 */
function nowLabel(): string {
  const d = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

export const useSolutionsStore = defineStore('solutions', () => {
  const workflow = useWorkflowStore()
  // 产物正文的持有者。方案切走 / 换掉时，内存里那份正文也要跟着走，
  // 否则界面会继续显示上一份方案的文档。
  const documents = useDocumentsStore()
  /**
   * 在途任务的运行态。
   *
   * 本 store 是「整份会话被换掉」的发起方（打开别的方案 / 开新方案 / 删掉当前方案），
   * 因此它必须在换人的那一刻把本地订阅与运行态一起摘掉——否则上一个方案还在跑的流
   * 会继续把事件写进新方案的预览与文档。
   *
   * 只依赖 `runs`（它只依赖 types）与订阅登记表（零依赖），不 import
   * `useDocumentPipeline`：`pipeline → solutions` 已经存在，反过来就成环。
   */
  const runs = useRunsStore()

  /** 左侧列表（只含摘要，不含快照正文） */
  const items = ref<SolutionSummary[]>([])

  /** 当前打开的方案 id；`null` 表示「新建中」，尚无 id */
  const currentId = ref<number | null>(null)

  /**
   * 会话代次：**每次「整份会话被换掉」就自增**（打开别的方案 / 开新方案 / 删掉当前方案）。
   *
   * 用途只有一个：给还在飞的异步回调一个判断依据——「我还是不是当初那个会话」。
   * 生成任务的流回调、摘要回填、文档加载都可能在用户已经切走之后才回来；它们若照常
   * 写回 store，就会把刚清空的会话重新填满（表现为删掉方案后列表里冒出一份「未命名方案」）。
   *
   * 为什么用递增计数而不是 `currentId`：开新方案与「删掉后回到新建态」都会让
   * `currentId` 变成 `null`，与「本来就是新建态」无法区分；代次每次都会变，天然可辨。
   */
  const sessionEpoch = ref(0)

  /** 列表是否已加载过（用于避免重复请求） */
  const loaded = ref(false)

  /** 保存 / 加载状态 */
  const saving = ref(false)
  const loading = ref(false)
  /** 最近一次保存失败的原因；成功或开始新一轮保存时清空 */
  const saveError = ref('')

  // ------------------------------------------------------------------
  // 内部：防抖与并发控制
  // ------------------------------------------------------------------

  let timer: number | undefined

  /**
   * 有改动尚未落库。
   *
   * 不能只看 `timer`：`watch` 的回调是**异步**刷新的，改完状态到定时器被排上之间
   * 有一个微任务窗口；而 `flushPendingSave` 恰恰常在这个窗口里被调用
   * （「点了跳转 → 同步 flush」）。只看定时器会让那次 flush 静默丢掉。
   *
   * 另：它只在**保存成功**（或方案已被删除）时才被清掉，失败时保持置位，
   * 这样 `flushPendingSave` 还知道该再试一次。
   */
  let dirty = false

  /**
   * 改动版本号：每标记一次「有改动待落库」自增。
   *
   * 用来回答一个问题——**在途的那次保存，是否覆盖了它启动之后才发生的改动**。
   * `scheduleSave` 启动时记下版本，收尾时版本变了就补存一次；版本没变就不再发请求。
   * 用版本号而不是「在途期间又置过位」的布尔标记，是为了让**保存失败**（版本没变）
   * 不会立刻触发重试：失败后 `dirty` 仍为真，重试交给下一次变更或 `flushPendingSave`。
   */
  let changeEpoch = 0

  /** 在途的保存请求；`flushPendingSave` 靠它等到保存链排空 */
  let inFlight: Promise<void> | null = null

  /** 取消尚未触发的防抖定时器 */
  function clearTimer(): void {
    if (timer !== undefined) {
      window.clearTimeout(timer)
      timer = undefined
    }
  }

  /**
   * 发起一次保存——**所有落库动作的唯一入口**，保证任一时刻至多一个请求在途。
   *
   * 为什么必须单飞：创建路径上 `createSolution` 返回之前 `currentId` 一直是 `null`，
   * 若防抖定时器与 `flushPendingSave` 各自并发跑一次 `saveNow`，两边都会走 `POST`，
   * 数据库里就留下两份一模一样的方案。
   *
   * 在途时直接返回，不再做别的：期间发生的改动会体现在 `changeEpoch` 上，
   * 由收尾逻辑统一决定要不要补存一次。
   *
   * 一开始就 `clearTimer()`：既然现在就存，那个防抖定时器已无意义，
   * 留着它只会在补存之后再多发一个请求。
   */
  function scheduleSave(): void {
    if (inFlight) return
    clearTimer()
    const epochAtStart = changeEpoch
    saving.value = true
    inFlight = saveNow()
      .then(() => undefined)
      .finally(() => {
        saving.value = false
        inFlight = null
        // 保存期间又有改动：补存一次（此时通常已经拿到 id，走 PUT）
        if (changeEpoch !== epochAtStart) scheduleSave()
      })
  }

  /**
   * 生成期间也保存——**去掉了旧的「`generating-*` 不保存」短路**。
   *
   * 任务与浏览器连接解耦后，快照里只保留「任务指针」（`activeJobId` / `activeJobKind`），
   * 半截正文天然在 `useDocumentPipeline` 的临时 ref、不进快照，因此生成态保存是安全的；
   * 而且**正因为要保存这个指针，刷新后才有东西可用来静默重连**。
   * （旧的「等生成结束再落库」反而是这个特性缺失的根因。）
   */
  function shouldPersistStage(): boolean {
    return true
  }

  /** 快照里有没有值得保存的内容（判定在 workflow store 里，避免两处口径不一致） */
  const hasContent = computed(() => workflow.hasContent)

  /** 是否已有 id（即已经创建过） */
  const isPersisted = computed(() => currentId.value !== null)

  /** 计算标题：优先产品名，其次所贴 PRD 的一级标题，最后给个带时间的默认名 */
  function computeTitle(): string {
    const fromSummary = workflow.summary?.product_name?.trim()
    if (fromSummary) return fromSummary

    const fromQuick = workflow.quickInput.productName.trim()
    if (fromQuick) return fromQuick

    // 调试入口贴入的 PRD：取首个一级标题，比「未命名」更有辨识度
    const heading = workflow.debugPrdText.match(/^#\s+(.+)$/m)
    if (heading?.[1]?.trim()) return heading[1].trim()

    // 双粘贴入口同理：两份材料里 PRD 的标题就是这份方案的名字（接口文档是附属材料，
    // 它的标题多半是「XX 接口文档」，拿来当方案名反而更差）
    const pastedHeading = workflow.pastePairInput.prd.match(/^#\s+(.+)$/m)
    if (pastedHeading?.[1]?.trim()) return pastedHeading[1].trim()

    return `未命名方案 · ${nowLabel()}`
  }

  /**
   * 上次**落库时**看到的两个内容变更计数。
   *
   * 内容有两个来源，各在自己那一层记数（分开记是为了不成环，见两处 docstring）：
   *
   * 1. `workflow.contentTick`：快照里的内容字段——需求摘要、对话、粘贴的两份材料；
   * 2. `documents.contentWriteTick`：三份产物的正文写入——生成、AI 优化、手改、版本操作。
   *    正文住在后端的 `documents` 表里，不经过快照，第 1 个计数完全看不到它们。
   *
   * 任一个与基准不等，就说明这批改动里有「作品真的变了」，该推进 `content_updated_at`。
   * 这个判断只能在前端做：后端不解析快照（见 `solution_repo` 的约定），分不出
   * 「翻了一页」与「改了内容」。
   */
  let lastSavedContentTick = workflow.contentTick
  let lastSavedDocWriteTick = documents.contentWriteTick

  /**
   * 把前端状态转成提交形状（camelCase → snake_case）。
   *
   * 转换放在这一处，而不是让每个调用方各拼一遍：漏一个字段就是「保存了但刷新就丢」，
   * 而那种 bug 不会报错，只会让人怀疑浏览器缓存。
   */
  function toStatePayload(session: SessionDataV2): SolutionStatePayload {
    return {
      view_state: session.viewState,
      entry_mode: session.entryMode ?? '',
      chat_tab: session.chatTab,
      summary: session.summary,
      quick_product_name: session.quickInput.productName,
      quick_requirements: session.quickInput.requirements,
      draft_prd_text: session.debugPrdText,
      paste_prd_text: session.pastePairInput.prd,
      paste_api_docs_text: session.pastePairInput.apiDocs,
      active_job_id: session.activeJobId,
      active_job_kind: session.activeJobKind ?? '',
      active_job_doc_kind: session.activeJobDocKind ?? '',
    }
  }

  /** 组装保存载荷 */
  function buildPayload(): SolutionSavePayload {
    return {
      title: computeTitle(),
      mode: workflow.entryMode ?? '',
      // 列表显示的是「这份**做到哪了**」，因此取最远里程碑而非当前所在阶段：
      // 取 `viewState` 会让「点回 PRD 回看」把进度倒退成「已出 PRD」。见 `progressStage`。
      stage: workflow.progressStage,
      // 相位与草稿**逐项**提交（不再是一整段 JSON）：后端因此能查、能按列覆盖，
      // 也不会因为某一段几十 KB 的粘贴稿把整次保存撑大。聊天记录不在这里——
      // 它由服务端写进 `chat_turns`，前端提交等于又造第二个真源。
      state: toStatePayload(workflow.snapshot()),
      // 相对**上次落库**的基准来比，而不是相对上一次调用：保存会因失败而重试，基准若
      // 跟着每次调用走，一次失败的保存就把「内容变了」这件事吃掉了。
      content_changed:
        workflow.contentTick !== lastSavedContentTick ||
        documents.contentWriteTick !== lastSavedDocWriteTick,
    }
  }

  /**
   * 执行一次保存。
   *
   * 分两条路径：
   * - 尚无 id → `POST` 创建，成功后把新 id 登记下来并通知路由（`onCreated` 回调）；
   * - 已有 id → `PUT` 整份覆盖；若返回「已不存在」（被别人删了），
   *   停掉自动保存并清空 id，避免对着一个不存在的 id 无限重试。
   */
  async function saveNow(): Promise<void> {
    // 没内容就不建：这是「有内容了才建」的落点
    if (!hasContent.value) {
      dirty = false
      return
    }

    const payload = buildPayload()
    // 记下这次请求带出去的两个内容计数：成功后才把它们设为新基准。要在 `await` **之前**取，
    // 请求在途期间用户还能继续改，那些改动不能被这一次的回应误当成「已落库」。
    const contentTickAtSend = workflow.contentTick
    const docWriteTickAtSend = documents.contentWriteTick

    try {
      if (currentId.value === null) {
        // 空壳方案的最后一道闸：一份**正常**方案不可能没有入口模式——用户必然先选入口
        // 才会产生内容，而 `reset()` 一定会把 `entryMode` 清成 `null`。
        // 因此走到这里还空着，说明这次保存来自一个已经作废的会话（最典型的是：方案被删、
        // `reset()` 刚清完，迟到的生成回调又把 `messages` 填了回来，`hasContent` 于是为真）。
        // 真正该做的是丢弃这次保存，而不是替它新建一份「未命名方案 · MM-DD HH:mm」。
        if (workflow.entryMode === null) {
          dirty = false
          return
        }
        const created = await createSolution(payload)
        currentId.value = created.id
        // 新建才提到最前；后续的自动保存都是「原位刷新」，不再动排序
        mergeIntoList(created, true)
        // 把「这批产物属于哪份方案」告诉文档 store：此后审阅页的手改才有地方可写。
        // 这里只登记归属、不拉取——方案刚建出来，后端还没有任何文档。
        documents.attach(created.id)
        onCreated?.(created.id)
      } else {
        const targetId = currentId.value
        const updated = await updateSolution(targetId, payload)
        if (updated === null) {
          // 方案在别处被删了：清掉 id，让它以后重新创建，而不是每 1.5 秒撞一次 404
          currentId.value = null
          removeFromList(targetId)
          saveError.value = '该方案已被删除，后续内容将另存为新方案'
          dirty = false
          return
        }
        // 请求在途期间方案被删 / 被切走：这份响应属于**上一份**方案。
        // 此时**不要**把它并回列表、也不要登记文档归属——删掉的那份会被凭空插回列表，
        // 变成一个点开就是 404 的幽灵条目。
        if (currentId.value !== targetId) return
        mergeIntoList(updated)
        // 幂等登记：打开方案时已经登记过，这里兜住「方案是本会话新建、后来才落库」的路径
        documents.attach(targetId)
      }
      saveError.value = ''
      // 内容变更这下真的进库了，推进基准。**只在成功路径上推进**：失败时若也推进，
      // 那次变更会被算作「已经记过」，下次保存报 `content_changed=false`——
      // `content_updated_at` 就永久丢了一次内容变更（而且是静默的）。
      lastSavedContentTick = contentTickAtSend
      lastSavedDocWriteTick = docWriteTickAtSend
      dirty = false
    } catch (error) {
      // 保存失败不阻断使用：内容还在内存里，下一次变更会再试。
      // 这里**不**清 `dirty`：这次没存上，`flushPendingSave` 还该再试一次。
      saveError.value = error instanceof Error ? error.message : '保存失败'
    }
  }

  /** 创建成功后的回调，由 AppV2 注入（用于 `router.replace` 到 `/v2/{id}`） */
  let onCreated: ((id: number) => void) | null = null

  /** 登记创建回调 */
  function setCreatedHandler(handler: ((id: number) => void) | null): void {
    onCreated = handler
  }

  // ------------------------------------------------------------------
  // 列表维护
  // ------------------------------------------------------------------

  /**
   * 把一份摘要并入列表。
   *
   * - **更新**（默认）：**原位替换**——自动保存每 1.5 秒就可能来一次（切一下阶段也算一次
   *   改动），若每次 `unshift`，当前方案会不停往列表顶部蹦：用户看到「侧栏自己跳了一下」，
   *   却看不到任何进度变化。
   * - **新建**（`toTop = true`）：插到最前，符合「最近的在上面」。
   */
  function mergeIntoList(summary: SolutionSummary, toTop = false): void {
    const index = items.value.findIndex((item) => item.id === summary.id)
    if (index < 0) {
      items.value.unshift(summary)
      return
    }
    if (!toTop) {
      items.value.splice(index, 1, summary)
      return
    }
    // 新建时列表里已有同 id（打开方案后本会话又新建、或列表拉取与创建竞态）：摘掉再插到最前
    items.value.splice(index, 1)
    items.value.unshift(summary)
  }

  /** 从列表移除 */
  function removeFromList(id: number): void {
    items.value = items.value.filter((item) => item.id !== id)
  }

  /** 拉取方案列表 */
  async function loadList(): Promise<void> {
    loading.value = true
    try {
      items.value = await fetchSolutions()
      loaded.value = true
    } catch (error) {
      saveError.value = error instanceof Error ? error.message : '无法获取方案列表'
    } finally {
      loading.value = false
    }
  }

  // ------------------------------------------------------------------
  // 对外动作
  // ------------------------------------------------------------------

  /**
   * 摘掉本地的任务订阅并清空运行态（**不取消服务端任务**）。
   *
   * 三处「整份会话换掉」都要先走这一步：切方案 / 开新方案 / 删掉当前方案。
   *
   * 为什么必须做：运行态与预览都是**单槽**的，上一个方案的任务一旦还在订阅，
   * 它的事件就会在切换之后照常写进预览（用户在新方案里看到上一份的正文），
   * 收尾时还会去清别人的任务指针。而「不取消服务端任务」是刻意的——任务与连接解耦，
   * 回到原方案时凭快照里的指针重连即可（见 `AppV2.resumeAfterRestore`）。
   *
   * 代次（`sessionEpoch`）管的是**回调**层面的事；这一层管的是**订阅**层面，
   * 两者互补：代次拦住已经进队列的事件，退订让队列不再产生新事件。
   */
  function detachActiveRun(): void {
    detachAllSubscriptions()
    runs.detach()
  }

  /**
   * 重新拉取聊天记录（服务端是唯一写者，本地只是镜像）。
   *
   * 调用点有两处：对话任务收尾之后（服务端此时已写好这一轮）、以及线程被作废之后。
   * 不用「把流内容自己拼成一条消息」：那会立刻造出第二个真源，而两份一旦不同步，
   * 表现是「刷新前后消息条数不一样」——最难解释的一类现象。
   */
  async function refreshTurns(): Promise<void> {
    const id = currentId.value
    if (id === null) return
    try {
      workflow.setTurns(await fetchTurns(id))
    } catch {
      // 拉取失败不清空既有镜像：少一条新消息比整段对话突然消失好得多
    }
  }

  /**
   * 作废一条对话线程（服务端 + 本地镜像）。
   *
   * 用于「整篇重新生成」之后：旧线程聊的是上一版的待确认项，留着它会让
   * 「按澄清结果改写」把针对旧文档的结论写进新文档。只清本地会在刷新后长回来，
   * 因此必须打服务端。
   */
  async function clearChatThread(stage: ChatStage): Promise<void> {
    const id = currentId.value
    workflow.dropChatThread(stage)
    if (id === null) return
    try {
      await clearTurns(id, stage)
    } catch {
      // 服务端没删成功时以它为准把镜像拉回来：界面与真源保持一致，
      // 而不是「看起来删了、其实还在」
      await refreshTurns()
    }
  }

  /**
   * 打开一份已有方案：拉详情、校验快照、回填 workflow。
   *
   * 快照可能来自旧版本前端，或落在 `generating-*` 这类刷新后无法继续的中间态，
   * 因此这里走 `parseSessionSnapshot` 做校验与降级，而不是直接赋值。
   *
   * 打开前先把当前压着的改动落库：马上 `applySnapshot` 会把内存整个盖掉，
   * 不先存就等于用户上一份方案的最后几笔编辑被无声丢弃。
   *
   * **文档队列也要冲**，而且必须在 `documents.loadAll` **之前**：`loadAll` 会把未落库的
   * `pending` 盖在新方案的响应之上（`documents.ts:137`），随后防抖定时器把旧稿 PUT 到
   * **新** solutionId —— 800ms 窗口内切方案就会串稿。两处 flush 都放在这里，用的是
   * 此刻仍是旧值的 `solutionId`。
   */
  async function openSolution(id: number): Promise<boolean> {
    await flushPendingSave()
    await documents.flushPendingSave()
    clearTimer()
    loading.value = true
    saveError.value = ''
    try {
      const detail = await fetchSolution(id)
      // 两步：先按传输形状搬运，再做校验与降级。搬运层不认识业务规则，
      // 校验层不关心字段怎么拼——「哪些值可信」因此只有一个判断点。
      const parsed = normalizeSessionSnapshot(fromStatePayload(detail.state))

      currentId.value = detail.id
      if (parsed) {
        // 会话换人了：先退订本地的流、清掉运行态与预览，再自增代次。
        // 顺序不能反——`applySnapshot` 会据新快照重建运行态，退订若放在它后面，
        // 就会把刚重建的那一支也摘掉（表现是「切回来以后再也不重连」）。
        detachActiveRun()
        sessionEpoch.value += 1
        workflow.applySnapshot(parsed.session, parsed.downgrade, detail.id)
      } else {
        // 快照坏掉（非法 JSON / 结构不符）时不清空当前内容，只标记错误让用户知道
        saveError.value = '这份方案的记录已损坏，无法恢复'
        return false
      }
      // 聊天记录单独取：它的真源是后端 `chat_turns`，状态列里没有它。
      // 取的是**全部线程**（一次往返），本地仍是合并数组、按 stage 过滤渲染。
      // 放在 `applySnapshot` 之后、`onOpened` 之前：前者刚把镜像清空，
      // 后者要让「打开完成」的回调看到一个内容齐备的状态。
      workflow.setTurns(await fetchTurns(detail.id))
      // 产物正文同样单独取：它不属于方案状态，随方案存在后端的 `documents` 表里。
      await documents.loadAll(detail.id)
      onOpened?.(detail.id)
      return true
    } catch (error) {
      if (error instanceof SolutionNotFoundError) {
        currentId.value = null
        saveError.value = error.message
        onOpened?.(null)
        return false
      }
      saveError.value = error instanceof Error ? error.message : '无法读取该方案'
      return false
    } finally {
      loading.value = false
    }
  }

  /** 打开完成后的回调，由 AppV2 注入（用于列表高亮等） */
  let onOpened: ((id: number | null) => void) | null = null

  /** 登记打开回调 */
  function setOpenedHandler(handler: ((id: number | null) => void) | null): void {
    onOpened = handler
  }

  /**
   * 开一份新方案：清空会话内容并把 id 归零。
   *
   * **不删除**原来那份——用户的意图是「再写一份」，而不是「把上一份扔掉」。
   * 旧方案仍留在侧边栏里。
   *
   * 清空之前先把压着的改动落库，理由同 `openSolution`：`reset()` 会把内容抹掉。
   * 两条流水线各有一份待落库的改动（会话快照 + 文档正文），都要先冲干净。
   */
  async function startNew(): Promise<void> {
    await flushPendingSave()
    await documents.flushPendingSave()
    clearTimer()
    // 会话换人了：先退订本地流、清运行态与预览（服务端任务照跑，回到原方案可重连），
    // 再自增代次让还在飞的回调作废
    detachActiveRun()
    sessionEpoch.value += 1
    currentId.value = null
    saveError.value = ''
    // `workflow.reset()` 内部会一并清掉文档工作副本
    workflow.reset()
    onOpened?.(null)
  }

  /**
   * 删除一份方案；若删的正是当前打开的，则回到新建态。
   *
   * 「回到新建态」不只是清空界面：**还要让在途的生成回调作废**（`sessionEpoch` 自增）。
   * 否则调用结束后那些迟到的回调会把刚清空的 `messages` 重新填满，自动保存据此
   * 判定「有内容」，在库里新建出一份「未命名方案 · MM-DD HH:mm」。
   * 后端那边也有一半责任（删除时取消挂在方案上的任务），但前端不能指望它：
   * 回调可能已经在本地队列里排着了。
   *
   * 本地订阅同样要摘（`detachActiveRun`）：后端 `cancel_for_solution` 会在收尾时关掉
   * 总线，但那是**异步**的，而前端这一刻已经开始渲染新方案的空会话了。留着订阅，
   * 关总线之前那几帧事件就会写进新方案的预览。
   */
  async function remove(id: number): Promise<void> {
    try {
      await deleteSolution(id)
      removeFromList(id)
      if (currentId.value === id) {
        clearTimer()
        // 先退订、再换代次：迟到的回调与在途的流都从此刻起写不进任何地方
        detachActiveRun()
        sessionEpoch.value += 1
        // 方案已经删了，压着的改动没有再存的意义
        dirty = false
        currentId.value = null
        workflow.reset()
        onOpened?.(null)
      }
    } catch (error) {
      // 删除失败时**不要**自增代次：方案还在、任务还在跑，界面必须继续接收流事件，
      // 否则用户看到的是「删不掉，而且生成卡住了」。
      saveError.value = error instanceof Error ? error.message : '删除方案失败'
    }
  }

  /**
   * 立即保存（若有待保存的变更），并等待保存链彻底排空。
   *
   * 用在「即将离开当前方案 / 页面」这类时机：防抖窗口里可能还压着一次未发出的保存，
   * 不等它就跳转，最后一次改动就丢了。
   *
   * 注意这里是**发起保存，而不是直接调 `saveNow`**：若已有请求在途（例如防抖定时器
   * 刚把创建请求发出去），再直接跑一次 `saveNow` 就是并发创建，库里会出现两份方案。
   * 交给 `scheduleSave` 就只会在收尾时按需补存。
   */
  async function flushPendingSave(): Promise<void> {
    // 先等一次微任务，让 watch 的回调有机会跑完并排上定时器：
    // 调用方常常是「同步改完状态紧接着 flush」，那时 watch 还没执行。
    await nextTick()

    // 既然现在就存，防抖定时器已无意义
    clearTimer()
    if (dirty) scheduleSave()

    // 等到保存链排空——包括在途任务结束时自动补存的那一次，
    // 否则调用方可能在最后一次改动还没写库时就跳转。
    while (inFlight) await inFlight
  }

  /** 供测试与调试：是否有尚未落库的变更 */
  function hasPendingSave(): boolean {
    return dirty
  }

  // ------------------------------------------------------------------
  // 自动保存：订阅 workflow 的变更计数
  // ------------------------------------------------------------------

  /** 上次自动保存看到过的 `restoreTick`，用来识别「这次变更是加载造成的」 */
  let lastSeenRestoreTick = workflow.restoreTick

  /**
   * 自动保存：订阅三个变更计数。
   *
   * - `changeTick`：快照里**任何**会入库的字段变了（含 `viewState` 这类相位）；
   * - `contentTick`：快照里的**内容**变了（列表时间看它）；
   * - `contentWriteTick`：三份产物的正文写成功了。它单独在这里是因为正文不经过快照——
   *   没有这条订阅，「生成完 PRD，列表时间却没变」就会成为常态。
   *
   * 同一次刷新里多个来源一起变化只会触发一次回调：Vue 把同一次刷新的变更合批。即使真的
   * 进来两次也无害——都只是置位 `dirty` 并重置防抖定时器。
   */
  watch(
    [
      () => workflow.changeTick,
      () => workflow.contentTick,
      () => documents.contentWriteTick,
    ],
    () => {
      // 这次变更来自「整份快照被换上」（打开已有方案），不是用户输入。
      // 跳过保存：刚读回来的内容原样存回去纯属白跑一趟请求，而且会顺带
      // 把 `updated_at` 刷新成现在——侧边栏的排序会因此乱掉。
      if (workflow.restoreTick !== lastSeenRestoreTick) {
        lastSeenRestoreTick = workflow.restoreTick
        // 恢复回来的内容不是「刚刚发生的内容变更」，两个基准都要跟着走：否则读完一份旧
        // 方案，之后任何一次保存都会声称「内容变了」，把 `content_updated_at` 顶到现在——
        // 那正好是「打开方案不该改内容时间」的反面。
        lastSavedContentTick = workflow.contentTick
        lastSavedDocWriteTick = documents.contentWriteTick
        dirty = false
        clearTimer()
        return
      }

      // 生成态也保存（见 `shouldPersistStage` 的说明）：保存的是任务指针，
      // 半截正文不进快照，因此这里不再按阶段短路。
      if (!shouldPersistStage()) return
      if (!hasContent.value && currentId.value === null) return

      // 记下「有未落库的改动」，并把改动版本推一格：
      // 即使后面因为页面跳转没能走到定时器回调，`flushPendingSave` 也还知道该补一次；
      // 而在途保存正是靠这个版本号判断「启动之后是否又有改动」。
      dirty = true
      changeEpoch += 1
      clearTimer()
      timer = window.setTimeout(() => {
        timer = undefined
        // 单飞入口：在途时它自己会记账，不会并发再发一个请求
        scheduleSave()
      }, SAVE_DEBOUNCE_MS)
    },
  )

  /** 列表里展示的入口模式名 */
  function modeLabel(mode: string): string {
    return MODE_LABELS[mode as EntryMode] ?? '完整流程'
  }

  /** 列表里展示的阶段名 */
  function stageLabel(stage: string): string {
    return STAGE_LABELS[stage] ?? '进行中'
  }

  return {
    // 状态
    items,
    currentId,
    sessionEpoch,
    loaded,
    loading,
    saving,
    saveError,
    hasContent,
    refreshTurns,
    clearChatThread,
    isPersisted,
    // 动作
    loadList,
    openSolution,
    startNew,
    remove,
    flushPendingSave,
    hasPendingSave,
    setCreatedHandler,
    setOpenedHandler,
    // 展示辅助
    modeLabel,
    stageLabel,
  }
})
