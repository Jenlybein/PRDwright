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
  updateSolution,
} from '@/services/solutionService'
import { SolutionNotFoundError } from '@/services/solutionService'
import { useWorkflowStore } from '@/stores/workflow'
import type { EntryMode, SolutionSavePayload, SolutionSummary } from '@/types'
import { parseSessionSnapshot } from '@/utils/workflow'

/** 防抖时长：既能挡住连续输入，又不会让用户觉得「怎么还没存上」 */
export const SAVE_DEBOUNCE_MS = 1500

/** 入口模式 → 中文名，用于列表展示 */
const MODE_LABELS: Record<EntryMode, string> = {
  structured: '完整流程',
  'prd-quick': 'PRD 快捷',
  'prompt-debug': '提示词调试',
}

/** 阶段 → 中文名：列表里显示「进行到哪一步」 */
const STAGE_LABELS: Record<string, string> = {
  entry: '未开始',
  form: '填写需求',
  chatting: 'AI 对话',
  'summary-diff': '确认摘要',
  'generating-prd': '生成 PRD',
  'review-prd': '已出 PRD',
  'rag-preview': '确认规范',
  'generating-api-docs': '生成接口文档',
  'review-api-docs': '已出接口文档',
  'generating-prompts': '生成提示词',
  'review-prompts': '已出提示词',
  'prd-quick': '描述需求',
  'prompt-debug': '粘贴 PRD',
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

  /** 左侧列表（只含摘要，不含快照正文） */
  const items = ref<SolutionSummary[]>([])

  /** 当前打开的方案 id；`null` 表示「新建中」，尚无 id */
  const currentId = ref<number | null>(null)

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

    return `未命名方案 · ${nowLabel()}`
  }

  /** 组装保存载荷 */
  function buildPayload(): SolutionSavePayload {
    return {
      title: computeTitle(),
      mode: workflow.entryMode ?? '',
      stage: workflow.viewState,
      data: JSON.stringify(workflow.snapshot()),
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

    try {
      if (currentId.value === null) {
        const created = await createSolution(payload)
        currentId.value = created.id
        mergeIntoList(created)
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
        mergeIntoList(updated)
      }
      saveError.value = ''
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

  /** 把一份摘要并入列表（新建时插到最前，更新时原位刷新并提到最前） */
  function mergeIntoList(summary: SolutionSummary): void {
    const index = items.value.findIndex((item) => item.id === summary.id)
    if (index >= 0) items.value.splice(index, 1)
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
   * 打开一份已有方案：拉详情、校验快照、回填 workflow。
   *
   * 快照可能来自旧版本前端，或落在 `generating-*` 这类刷新后无法继续的中间态，
   * 因此这里走 `parseSessionSnapshot` 做校验与降级，而不是直接赋值。
   *
   * 打开前先把当前压着的改动落库：马上 `applySnapshot` 会把内存整个盖掉，
   * 不先存就等于用户上一份方案的最后几笔编辑被无声丢弃。
   */
  async function openSolution(id: number): Promise<boolean> {
    await flushPendingSave()
    clearTimer()
    loading.value = true
    saveError.value = ''
    try {
      const detail = await fetchSolution(id)
      const parsed = parseSessionSnapshot(detail.data)

      currentId.value = detail.id
      if (parsed) {
        workflow.applySnapshot(parsed.session, parsed.downgrade)
      } else {
        // 快照坏掉（非法 JSON / 结构不符）时不清空当前内容，只标记错误让用户知道
        saveError.value = '这份方案的记录已损坏，无法恢复'
        return false
      }
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
   */
  async function startNew(): Promise<void> {
    await flushPendingSave()
    clearTimer()
    currentId.value = null
    saveError.value = ''
    workflow.reset()
    onOpened?.(null)
  }

  /** 删除一份方案；若删的正是当前打开的，则回到新建态 */
  async function remove(id: number): Promise<void> {
    try {
      await deleteSolution(id)
      removeFromList(id)
      if (currentId.value === id) {
        clearTimer()
        // 方案已经删了，压着的改动没有再存的意义
        dirty = false
        currentId.value = null
        workflow.reset()
        onOpened?.(null)
      }
    } catch (error) {
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

  watch(
    () => workflow.changeTick,
    () => {
      // 这次变更来自「整份快照被换上」（打开已有方案），不是用户输入。
      // 跳过保存：刚读回来的内容原样存回去纯属白跑一趟请求，而且会顺带
      // 把 `updated_at` 刷新成现在——侧边栏的排序会因此乱掉。
      if (workflow.restoreTick !== lastSeenRestoreTick) {
        lastSeenRestoreTick = workflow.restoreTick
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
    loaded,
    loading,
    saving,
    saveError,
    hasContent,
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
