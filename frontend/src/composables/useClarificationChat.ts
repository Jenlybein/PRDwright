import { computed } from 'vue'
import { storeToRefs } from 'pinia'

import { useJob } from '@/composables/useJob'
import type { StreamHandlers } from '@/services/api'
import { isSubscribed } from '@/services/runSubscriptions'
import { useRunsStore } from '@/stores/runs'
import { useSolutionsStore } from '@/stores/solutions'
import { useWorkflowStore } from '@/stores/workflow'
import type {
  ChatMessage,
  ChatStage,
  ClarificationAnswerItem,
  RequirementAnswers,
  RequirementsSummary,
  ToolResultPayload,
} from '@/types'
import { normalizeChatStage } from '@/utils/workflow'
import { buildAnswers } from '@/utils/requirements'

/**
 * 澄清对话的提示词（系统人设 + 首轮开场白）**全部由后端持有**，见
 * `backend/src/backend/services/prompts.py` 的对话档案注册表。
 *
 * 前端不传、也没有通道传任何提示词：`system_prompt_override` 已从接口契约里删除，
 * 免得再出现「前端人设写 1-3 个、后端开场白写 3-5 个」这类跨端口径漂移。
 * 前端能做的只有**点名场景**（`profileKey`）。
 */

/** 对话流程需要向外部借用的能力 */
export interface ChatHooks {
  /** 展示一条短暂提示 */
  onNotice: (text: string) => void
  /** 把阶段切到对话页 */
  onEnterChat: () => void
}

/**
 * 这套骨架服务的线程是哪一条、说的是哪一套人设。
 *
 * 四条澄清线程（需求 / PRD / 接口 / 提示词）共用同一套逻辑，差异只有两点：
 * **消息归属哪条线程**（`stage`，任务创建时随请求提交、由服务端记在轮次上）与
 * **后端用哪个档案**（`profileKey`）。
 */
export interface ClarificationChatOptions {
  /** 对话档案标识：后端据此选人设与开场白（如 `doc-clarification`）；不传即后端的默认档案 */
  profileKey?: string
  /**
   * 这条线程的标识。它决定三件事：
   *
   * 1. 这一轮的任务属于哪条线程（服务端据此写轮次、拼历史）；
   * 2. 渲染时从哪里过滤消息（`messages` 是四条线程的合并镜像）；
   * 3. 「重开这条线程」时清哪一段。
   *
   * 是必填的：四条线程的人设与上下文完全不同，没有它就只能靠猜。
   */
  stage: ChatStage
  /**
   * 开新会话时是否清掉**本线程**已有的聊天记录；默认 `true`。
   *
   * 文档澄清要传 `false`——那是「在同一条线程上继续」，把前面聊过的清掉正好就是用户
   * 最反感的体验（他会以为之前的交流丢了）。
   */
  clearHistory?: boolean
}

/**
 * 澄清对话流程。
 *
 * ## 与上一版的根本差别：对话现在是一个**任务**
 *
 * 从前它是一条裸的 `POST /continue-stream`：切走方案、刷新页面、换设备，这一轮就没了
 * （前端只能靠会话代次把它整份丢弃，看起来像「生成被中止」）。现在它走与文档生成
 * **完全相同的通道**：`createJob` → SSE `snapshot` + `chunk` + `done` → 收尾写轮次。
 * 于是「切走不断」「刷新接回」「换设备能看」三件事一次到位，一行新机制都没写。
 *
 * ## 三层归属，各自有判据
 *
 * - **哪一次**：`jobId`（`stores/runs`）；
 * - **哪条线程**：`stage`（随任务提交、由服务端落在轮次上）；
 * - **消息归谁**：服务端 `chat_turns`——前端只有一份只读镜像（`workflow.messages`）。
 *
 * 因此这个文件里**没有**「把增量拼成消息 push 进数组」的代码：那一步现在由服务端做，
 * 前端只负责在收尾后把轮次重新拉一遍。前端不再持有真源，也就没有第二个写者。
 *
 * @param hooks 需要向外部借用的能力（提示、切阶段）。
 * @param options 线程标识与档案标识。
 */
export function useClarificationChat(hooks: ChatHooks, options: ClarificationChatOptions) {
  const store = useWorkflowStore()
  const solutions = useSolutionsStore()
  const runs = useRunsStore()
  const job = useJob()
  const state = storeToRefs(store)
  const { summary, messages } = state
  const { stage, profileKey } = options

  /**
   * 本线程是否正在生成——判据是「那一支任务属于这条线程」。
   *
   * ⚠️ 从前这里是全局布尔 `chatStreaming`：任何一条线程在跑，四条线程的输入框全都灰掉，
   * 而且 A 的 `finally` 会把 B 的忙碌标志抹平。改成按线程判之后，
   * 「A 在生成时能去 B 提问」与「A 的收尾不影响 B」同时成立。
   */
  /**
   * 本线程是否正在生成。
   *
   * 判据以「那一支任务属于这条线程」为主。第二个分支是**兜底**：运行态在、确实是一个
   * 对话任务，但它的线程没登记下来（旧指针没存 `activeChatStage`、或兜底查询拿不到
   * 线程）时，只要用户看的就是当前标签，就按「在跑」处理。
   *
   * 没有这个兜底，症状是「刷新后流式完全看不见，等结束才整段出现」——
   * 内容其实一直在 `runs.preview` 里，只是渲染层认为没有任何一轮在跑。
   */
  const streaming = computed(
    () =>
      runs.isRunningForChat(stage) ||
      (runs.hasRunning &&
        runs.activeRun?.jobKind === 'chat' &&
        runs.activeRun.chatStage == null &&
        store.chatTab === stage),
  )

  /** 正在流式生成的正文（预览）；不在生成时为空串 */
  const streamingContent = computed(() => (streaming.value ? runs.preview.content : ''))

  /**
   * 对话过程中的错误提示。
   *
   * ⚠️ **不能只在生成中才显示**：任务可能压根没发起（方案还没 id、后端 400、
   * 网络失败）。那种情况下 `streaming` 从头到尾都是 false，若把错误也挂在它下面，
   * 用户看到的就只是「进了对话页，一片空白，没有任何提示」——最难自查的一种故障。
   * 错误属于「上一轮的结果」，与「此刻是否在跑」无关；新一轮开始时由
   * `runTurn` 的 `resetPreview()` 清掉。
   */
  const chatError = computed(() => runs.preview.error)

  /** 这条消息属于哪条线程；缺省视为需求澄清（旧数据没有这个字段） */
  function stageOf(message: ChatMessage): ChatStage {
    return normalizeChatStage(message.stage)
  }

  /**
   * 只属于本线程的消息，按发生顺序排列。
   *
   * 镜像里是四条线程的合并数组，而渲染与判断都只该看自己那条：把别线程的对话混进来，
   * 用户会在「PRD 澄清」里看到需求澄清的内容。
   */
  function history(): ChatMessage[] {
    return messages.value.filter((message) => stageOf(message) === stage)
  }

  /**
   * 交给消息列表渲染的内容：**只含本线程**，外加流式生成中的那一条。
   *
   * 流式气泡挂在末尾（还没收到增量时 content 为空，由列表渲染「思考中」动画；
   * 首字到达后自动切成 Markdown 实时渲染）。收尾后服务端已写好轮次，
   * `solutions.refreshTurns()` 会把镜像整体换新，气泡随之消失。
   */
  const displayMessages = computed<ChatMessage[]>(() => {
    const thread = history()
    if (!streaming.value) return thread
    return [
      ...thread,
      {
        role: 'assistant',
        content: streamingContent.value,
        streaming: true,
      },
    ]
  })

  /**
   * 订阅回调：增量只写进**预览**。
   *
   * 预览与运行态同 store 不同对象（见 `stores/runs.ts`）：任务收尾后身份该清，
   * 正文却还要继续显示到服务端轮次到达为止。
   *
   * `onSnapshot` 是重连的关键：后端先把「已经生成到哪儿」发过来，界面立刻有半截内容，
   * 再接着收 live 增量——否则刷新后要盯着空白等到下一个字。
   *
   * ⚠️ **代次守卫**：预览是单槽的，会话换人（切方案 / 开新方案 / 删方案）之后，
   * 旧订阅可能还有几个已入队的事件在飞。没有这道闸，它们会写进新会话的对话气泡里
   * （表现为「新任务里冒出上一轮的字」）。与文档流水线 `guardJobHandlers` 同一口径：
   * 丢弃必须静默——用户已经离开那个会话，这不是错误。
   */
  function streamHandlers(epoch: number): StreamHandlers {
    const live = () => solutions.sessionEpoch === epoch
    return {
      onChunk: (chunk) => {
        if (live()) runs.preview.content += chunk.text
      },
      onSnapshot: (snap) => {
        if (live()) runs.preview.content = snap.finalizedText + snap.liveBuffer
      },
    }
  }

  /**
   * 发起一轮对话并等它收尾。
   *
   * 收尾后**必须重新拉轮次**：这一轮的正文由服务端在 `run_summary` 之前写进
   * `chat_turns`，前端只负责把它取回来。不在这里自己拼一条消息——那会立刻造出
   * 第二个真源，两份一旦不同步，表现为「刷新前后消息条数不一样」。
   */
  async function runTurn(
    mode: 'start' | 'continue',
    payload: {
      formData?: Record<string, unknown> | null
      content?: string
      toolResult?: ToolResultPayload
    },
  ): Promise<void> {
    runs.resetPreview()
    // 代次在**发起之前**定格：这一轮结果只属于这一份会话
    const epoch = solutions.sessionEpoch
    const result = await job.start(
      'chat',
      {
        chatStage: stage,
        chatMode: mode,
        formData: payload.formData ?? null,
        content: payload.content ?? '',
        // 工具结果的形状由后端定义（`{tool_call_id, answers}`），前端只搬运不解释
        toolResult: (payload.toolResult ?? null) as Record<string, unknown> | null,
        profileKey,
      },
      streamHandlers(epoch),
    )
    // 订阅期间会话已经换人（用户开了新任务）：这次结果与界面无关，一个字都不要写。
    // 预览已被 `solutions.startNew()` 清掉，此时再补一句「生成失败」只会诬告用户
    // 正在看的那一轮——这正是「新任务流式正常、却顶着一条红色失败」的成因。
    if (solutions.sessionEpoch !== epoch) return
    // 先按服务端拉一遍：成功时有这一轮，失败时也可能有半截（服务端会把已产出的部分落库）
    await solutions.refreshTurns()
    if (result.ok) {
      runs.preview.content = ''
      return
    }
    // 主动退订（切走 / 开新任务）不是失败：没有错误可报，也不抹掉已经接到的半截
    if (result.aborted) return
    // 同一份会话里这一支已被更晚的任务顶替：结论属于上一轮，不能写到新一轮的界面上
    if (result.run !== null && !runs.holds(result.run.jobId)) return
    // 保留失败原因，但清掉预览正文——它已经在轮次里了，留着会与列表里的那条重复
    runs.preview.content = ''
    runs.preview.error = job.error.value || '生成失败'
  }

  /**
   * 发送一条消息并接续对话。
   *
   * 服务端在**创建任务时**就落一条输入轮次（拿到 id 之后，历史可以按「它之前」精确取），
   * 因此这里会先乐观地插一条用户气泡，等收尾时被服务端的整体替换掉。乐观插入只为了
   * 「我发出去的话立刻能看见」——它不是真源，也不参与任何写回。
   *
   * @param content 本轮输入。选项卡提交时它是拼好的展示文本：**照旧发给后端**——
   *   降级（拿不到 `tool_call_id`）时模型唯一能读到的就是它。
   * @param toolResult 结构化作答（**给模型**的回灌）。带上时后端按工具结果回给
   *   当初提问的那次工具调用；缺 `tool_call_id` 时不带，后端按纯文本处理。
   * @param answers 逐题作答（**给界面**的回执）。点选项卡提交就有，与
   *   `toolResult` 能否发出**无关**；不挂时这条消息渲染成普通气泡。
   */
  async function sendMessage(
    content: string,
    toolResult?: ToolResultPayload,
    answers?: ClarificationAnswerItem[],
  ): Promise<void> {
    const text = content.trim()
    if (!text || streaming.value) return

    // 同 `asks` 的口径：只在真有值时挂键，别挂空数组
    messages.value.push({
      role: 'user',
      content: text,
      stage,
      ...(answers?.length ? { answers } : {}),
    })

    await runTurn('continue', { content: text, toolResult })
  }

  /**
   * 用一组现成的表单数据开启会话：切到对话页 → 建任务 → 实时展示 AI 的第一轮追问。
   *
   * 四条线程共用：需求澄清提交结构化摘要（由 `buildAnswers` 映射到后端题目的字段名），
   * 三条文档澄清提交 `{document, pending_items}`。人设与首轮开场白由后端的对话档案决定，
   * 前端只提交内容，不参与措辞。
   */
  async function startConversationWithAnswers(
    answers: RequirementAnswers,
    notice = '正在生成回复',
  ): Promise<void> {
    // 默认清空本线程的历史：需求澄清是一次全新的开始。
    // ⚠️ **只清本线程**——四条线程共用一个数组，整数组清空会连带抹掉别的线程。
    // 清的是**服务端**那一份（`clearChatThread`），否则刷新一次旧对话又回来了。
    if (options.clearHistory !== false) {
      await solutions.clearChatThread(stage)
    }
    hooks.onEnterChat()
    hooks.onNotice(notice)

    await runTurn('start', { formData: answers })
  }

  /**
   * 提交结构化需求并开启会话（需求澄清入口）。
   *
   * 表单内部已完成校验，这里只负责把摘要映射成后端认识的题目字段并推进流程。
   */
  async function startConversation(value: RequirementsSummary): Promise<void> {
    summary.value = value
    await startConversationWithAnswers(buildAnswers(value), '需求已提交，正在生成回复')
  }

  /**
   * 刷新 / 换设备后，接回**正在进行的那一轮**对话。
   *
   * 为什么必须走对话自己这一条，而不是复用文档流水线的重连
   * ------------------------------------------------------
   * 文档流水线的 handlers 把增量写进 `docStreamingContent`，收尾走 `finishDoc`——
   * 那两件事对对话都不适用：对话的进度在 `runs.preview.content`，产物是一条轮次。
   * 之前对话被塞进那条重连链，结果就是「刷新后流式全丢、过一会儿才整段出现」：
   * 事件确实到了，只是全写进了另一个预览。
   *
   * 收尾后**必须重拉轮次**：服务端在广播 `run_summary` 之前已经写好这一轮，
   * 这一步把「流式预览」换成「服务端权威的那条消息」，界面不会因为预览清空而闪空。
   */
  async function reconnect(jobId: number): Promise<void> {
    // **幂等**：同一个任务绝不允许两路订阅并行。判据是订阅登记表（按 jobId 记），
    // 与 `useDocumentPipeline.reconnectJob` 同一口径。
    //
    // 为什么必须有：`register()` 会中止同一 jobId 的旧订阅。刷新后「恢复快照」有两条
    // 触发路径（路由解析、登录态探测），它们各自 `await` 一串请求后都可能走到这里；
    // 没有这道闸，后一路会把前一路掐掉，前一路以 `AbortError` 收场——而它其实已经把
    // 半截内容接回来了。用户看到的就是「内容好好的，却报『接回上一轮对话失败』」。
    if (isSubscribed(jobId)) return
    // 有反馈才可诊断：接回是一段看不见的动作，静默失败与「没接回」在界面上长得一样
    hooks.onNotice('正在接回上一轮的回复…')
    // 代次同样在发起之前定格：重连可能跑很久，期间用户完全可能开新任务
    const epoch = solutions.sessionEpoch
    const result = await job.reconnect(jobId, streamHandlers(epoch))
    // 会话已经换人：这一轮接回来的内容属于上一份会话，界面此刻显示的是别的东西
    if (solutions.sessionEpoch !== epoch) return
    await solutions.refreshTurns()
    if (result.ok) {
      // 成功：预览让位给服务端那条权威轮次，避免同一句话在列表里出现两次
      runs.preview.content = ''
      return
    }
    // 主动退订（切方案 / 开新任务）不是失败：**保留**预览里已经接到的半截内容。
    // 丢掉它等于把用户已经看到的字抹掉，而 refreshTurns 又拿不到这一轮（服务端还没收尾），
    // 界面会变成一片空白。
    if (result.aborted) return
    // 只是「登记过」不够：这一支可能已经被更晚的任务顶替，结论不该写到新任务脸上
    if (!runs.holds(jobId)) return
    runs.preview.error = job.error.value || '接回上一轮对话失败'
  }

  /** 清空对话相关的临时状态（含错误提示） */
  function reset(): void {
    runs.resetPreview()
  }

  return {
    /** 本线程是否正在生成（渲染用；导航不再看它） */
    streaming,
    /** 对话错误提示 */
    chatError,
    /** 交给消息列表渲染的消息（含流式中的那一条） */
    displayMessages,
    /** 发送一条消息 */
    sendMessage,
    /** 提交需求并开启会话（需求澄清） */
    startConversation,
    /** 用一组现成的表单数据开启会话（文档澄清等） */
    startConversationWithAnswers,
    /** 刷新 / 换设备后接回正在进行的那一轮 */
    reconnect,
    /** 清空临时状态 */
    reset,
  }
}
