import { computed, ref } from 'vue'
import { storeToRefs } from 'pinia'

import { continueConversationStream, startConversationStream } from '@/services/api'
import { isApiError } from '@/services/errors'
import { useSolutionsStore } from '@/stores/solutions'
import { useWorkflowStore } from '@/stores/workflow'
import type { StreamResult } from '@/services/api'
import type {
  ChatMessage,
  ChatStage,
  ClarificationAnswerItem,
  ClarificationAsk,
  RequirementAnswers,
  RequirementsSummary,
  ToolCallRef,
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
 * 四条澄清线程（需求 / PRD / 接口 / 提示词）共用同一套流式逻辑（缓冲、固化、断线重试），
 * 差异只有两点：**消息归属哪条线程**（`stage`，消息上带着它、会话 id 也按它取）与
 * **后端用哪个档案**（`profileKey`）。把差异做成参数而不是复制四份 composable，
 * 是为了让「断线重试」「错误保留半截内容」这些细节只维护一处。
 */
export interface ClarificationChatOptions {
  /** 对话档案标识：后端据此选人设与开场白（如 `doc-clarification`）；不传即后端的默认档案 */
  profileKey?: string
  /**
   * 这条线程的标识。它决定三件事：
   *
   * 1. 新产生的消息打哪个 `stage`（对话页据此过滤显示，摘要回填据此只取需求那一条）；
   * 2. 从哪里读这条线程的后端会话 id（`conversationIds[stage]`）；
   * 3. 「清空历史」时该清掉哪一段。
   *
   * 是必填的：四条线程共用一个消息数组，没有它就只能靠猜。
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
 * 收敛「流式接续对话」这一摊状态与副作用：流式缓冲、错误提示，以及把增量
 * 固化成消息的收尾逻辑。会话数据本身仍在 Pinia store 里，这里只做编排，
 * 因此组件侧不需要再持有任何流程状态。
 *
 * **每条线程一个实例**：四条线程同时存在，各自持有自己的流式缓冲与错误提示，
 * 因此 A 线程出错不会在 B 线程上冒出来。
 *
 * @param hooks 需要向外部借用的能力（提示、切阶段）。
 * @param options 线程标识与档案标识。
 */
export function useClarificationChat(hooks: ChatHooks, options: ClarificationChatOptions) {
  const store = useWorkflowStore()
  // 会话代次的持有者：迟到的回复必须按它判断「我这一轮还在不在」
  const solutions = useSolutionsStore()
  const state = storeToRefs(store)
  const { summary, messages, chatStreaming } = state
  const { stage, profileKey } = options

  /**
   * 本次请求开始那一刻的会话代次（`solutions.sessionEpoch` 的快照）。
   *
   * 对话请求与生成任务一样，发出之后没法撤回。用户在流式回复途中删掉当前方案、
   * 甚至已经重新选了入口（此时 `entryMode` 非空，`solutions.saveNow` 那道
   * 「空壳方案」闸门拦不住），一条迟到的澄清回复就会 push 进 `messages`——
   * `hasContent` 变真 + 入口模式非空，自动保存据此 POST 出一份「未命名方案 · MM-DD HH:mm」。
   *
   * 代次只在「整份会话被换掉」时变（打开别的方案 / 开新方案 / 删掉当前方案），
   * 因此正常聊天过程里它是不变的，不会误伤正在进行的对话。
   */
  let chatEpoch = 0

  /** 当前会话是否已经被整份换掉（删除 / 新建 / 打开别的方案） */
  function chatExpired(): boolean {
    return solutions.sessionEpoch !== chatEpoch
  }

  /** 本线程（也是本实例）是否正在流式接收回复 */
  const streaming = ref(false)

  /**
   * 本线程的后端会话 id。
   *
   * 四条线程各有一个会话，所以不能拿 store 里某个字符串字段当引用——用一层可写 computed
   * 接上去：读的时候按 `stage` 取，写的时候只动自己那一个键。整体换一个新对象，让 store
   * 的深 watch 稳稳看到变化（就地改也可，但换对象更不容易踩到「同一个引用没被察觉」）。
   */
  const conversationId = computed<string>({
    get: () => store.conversationIds[stage],
    set: (value) => {
      store.conversationIds = { ...store.conversationIds, [stage]: value }
    },
  })

  /** 正在流式生成的回复文本，作为列表末尾那条气泡的实时内容 */
  const streamingContent = ref('')

  /** 对话过程中的错误提示 */
  const chatError = ref('')

  /** 这条消息属于哪条线程；缺省视为需求澄清（旧快照的消息都没有这个字段） */
  function stageOf(message: ChatMessage): ChatStage {
    return normalizeChatStage(message.stage)
  }

  /**
   * 只属于本线程的消息，按发生顺序排列。
   *
   * 消息列表是四条线程共用的一条，而喂给后端的必须是**本轮**的内容：把别线程的对话混进去，
   * 模型会以为那些也是本轮上下文（人设还是另一套）。
   */
  function history(): ChatMessage[] {
    return messages.value.filter((message) => stageOf(message) === stage)
  }

  /**
   * 交给消息列表渲染的内容：**只含本线程**，外加流式生成中的那一条。
   *
   * 流式气泡挂在末尾（还没收到增量时 content 为空，由列表渲染「思考中」动画；
   * 首字到达后自动切成 Markdown 实时渲染）。
   *
   * ⚠️ 判据用的是**本实例**的 `streaming`，不是 store 里全局的 `chatStreaming`：
   * 后者只要有任何一条线程在跑就是 true，而 `streamingContent` 是本实例私有的——
   * 用全局量判断，切到别的线程就会拿它那口空缓冲渲染出一条假的「思考中」。
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
   * 把增量文本追加到流式缓冲，由消息列表实时渲染。
   *
   * 会话已被换掉时直接丢弃：那段文本属于上一份会话，写进缓冲就会在新会话里
   * 渲染出一条莫名其妙的「思考中」气泡。
   */
  function handleChunk(chunk: string): void {
    if (chatExpired()) return
    streamingContent.value += chunk
  }

  /**
   * 执行一次流式请求并收尾。
   *
   * 增量文本先落到 `streamingContent` 实时展示，请求结束后再固化成消息列表里的一条
   * assistant 消息；中途出错时保留已经流出的内容，避免整段凭空消失。
   *
   * **会话代次在这里定格**：`sendMessage` / `startConversation` /
   * `startConversationWithAnswers` 三个发起入口最终都收口到本函数（见各自的实现），
   * 因此只要在这里记一次快照，三条路径就都被覆盖了；收尾处再统一比对一次。
   *
   * @param request 实际发起请求的函数；接续对话需要在内部做重试，因此以闭包传入。
   */
  async function runStream(request: () => Promise<StreamResult>): Promise<void> {
    // 定格本次请求所属的会话；此后所有回调与收尾都拿它比对
    chatEpoch = solutions.sessionEpoch
    // 两个状态各有各的读者：`streaming` 服务本线程的渲染，`chatStreaming` 是全局的
    // 「有请求在跑」，导航守卫（`store.busy`）靠它拦住跳转与切标签。
    streaming.value = true
    chatStreaming.value = true
    streamingContent.value = ''
    chatError.value = ''

    let reply = ''
    // 这一轮提问附带的结构化选择题（后端从工具调用参数里取出来的）
    let asks: ClarificationAsk[] = []
    // 本轮助手发起的工具调用；回传历史时不能裁（见 `tool_calls` 的说明）
    let toolCalls: ToolCallRef[] = []
    try {
      const result = await request()
      // 会话已被整份换掉：会话 id 也不能写——它会成为新会话里「接错线」的起点
      if (result.conversationId && !chatExpired()) conversationId.value = result.conversationId
      reply = result.reply
      asks = result.asks
      toolCalls = result.toolCalls ?? []
    } catch (error) {
      // 迟到的失败同样不该污染新会话：既不留错误提示，也不把半截内容固化下去
      if (!chatExpired()) {
        chatError.value = error instanceof Error ? error.message : String(error)
      }
      // 报错时把已经流出的半截内容保留下来；半截输出里没有可靠的选题，故 `asks` 留空
      // 工具调用同理：没走完 `done` 就无从得知这次调用的 id
      reply = streamingContent.value
    } finally {
      // 先结束流式状态再固化消息，否则列表里会短暂多出一条「思考中」气泡。
      // 这两个标志是**渲染开关**而不是会话数据：不管请求是否过期都要归位，
      // 否则删掉方案后 `busy` 会永远为真，新会话连入口都点不动。
      streaming.value = false
      chatStreaming.value = false
      const content = reply.trim()
      // 会话已被换掉 → 整路丢弃：不 push 消息、不带 asks / tool_calls
      if (content && !chatExpired()) {
        messages.value.push({
          role: 'assistant',
          content,
          // 带上线程标记：这条消息属于哪条线程，靠它在共享的消息列表里分清归属
          stage,
          // ⚠️ 只在**有选项时**才带这个字段：几条既有用例按 `{role, content, stage}` 精确
          // 断言消息对象，恒带一个空数组会把它们弄红，而那种红会让人误以为「不带也行」
          ...(asks.length ? { asks } : {}),
          // 同 `asks` 的口径：只在真有值时挂上去。它要跟着消息进历史回传给后端，
          // 丢了就会留下「调了工具却没人应答」的悬空调用。
          ...(toolCalls.length ? { tool_calls: toolCalls } : {}),
        })
      }
      streamingContent.value = ''
    }
  }

  /**
   * 发送一条消息并接续对话。
   *
   * 后端会话存在内存里，服务重启后 ID 会失效；收到 `SESSION_NOT_FOUND` 时清掉 ID，
   * 改用前端回传的完整历史重试一次（无状态模式）。人设由后端按默认档案解析，
   * 无状态模式下同样生效，前端不需要补发任何提示词。
   *
   * 判据是**错误码**而不是文案：这个错误发生在 SSE 流内（HTTP 状态码仍是 200），
   * 只能靠事件里的 `code` 识别；早先这里匹配的是「会话不存在」这句中文，
   * 后端改一个字降级逻辑就静默失效——那正是本次改造要消灭的耦合。
   *
   * @param content 本轮输入。选项卡提交时它是拼好的展示文本：**照旧发给后端**——
   *   降级（拿不到 `tool_call_id`）时模型唯一能读到的就是它，所以这里
   *   刻意保留完整题干与选项文本，不为了界面好看而改短。
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
    if (!text || chatStreaming.value) return

    // `answers` 只由「点选项卡提交」产生（自由文本作答不传），于是它天然就是
    // 「这条消息是选出来的」的标记。同 `asks` 的口径：只在真有值时挂键，别挂空数组。
    messages.value.push({
      role: 'user',
      content: text,
      stage,
      ...(answers?.length ? { answers } : {}),
    })
    chatError.value = ''

    // 历史只取本阶段、本轮之前的消息，本轮输入单独通过 content 传递。
    // 先按阶段过滤再去掉末尾刚推入的那一条（它被塞在上一步，一定在末尾）。
    //
    // ⚠️ `tool_calls` 不能裁：它是后端把随后的用户文本包成工具应答、完成配对的
    // 依据。裁掉会让历史里留下悬空的 tool_call，上游 API 直接报错。
    // 同 `asks` 的口径，只在真有值时带上。
    const prior = history()
      .slice(0, -1)
      .map(({ role, content, tool_calls }) => ({
        role,
        content,
        ...(tool_calls?.length ? { tool_calls } : {}),
      }))

    await runStream(async () => {
      try {
        return await continueConversationStream(
          {
            content: text,
            conversationId: conversationId.value || undefined,
            history: prior,
            profileKey,
            toolResult,
          },
          { onChunk: handleChunk },
        )
      } catch (error) {
        // 会话已被整份换掉：不重试——重试只会对着一个已经不存在的会话再发一次请求。
        // 抛出后由 `runStream` 的收尾按代次整路丢弃。
        if (chatExpired()) throw error
        if (!conversationId.value || !isApiError(error, 'SESSION_NOT_FOUND')) throw error
        // 会话已失效：丢掉旧 ID 与半截输出，退回无状态模式重试。
        // 重试时仍要带上档案标识——否则文档澄清的人设会在这里悄悄换回默认的那套。
        conversationId.value = ''
        streamingContent.value = ''
        return continueConversationStream(
          { content: text, history: prior, profileKey, toolResult },
          { onChunk: handleChunk },
        )
      }
    })
  }

  /**
   * 用一组现成的表单数据开启会话：切到对话阶段 → 调流式接口 → 实时展示 AI 的第一轮追问。
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
    // ⚠️ **只清本线程**——四条线程共用一个 `messages` 数组，整数组清空会连带抹掉别的线程
    // （那正是「回到某个标签发现自己聊过的内容没了」这类最难解释的 bug）。
    // 文档澄清传 `clearHistory: false`：它要在原有对话后面接下去。
    if (options.clearHistory !== false) {
      messages.value = messages.value.filter((message) => stageOf(message) !== stage)
    }
    conversationId.value = ''
    chatError.value = ''
    hooks.onEnterChat()
    hooks.onNotice(notice)

    await runStream(() =>
      startConversationStream({ answers, profileKey }, { onChunk: handleChunk }),
    )
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

  /** 清空对话相关的临时状态（含正在流式接收的半截内容） */
  function reset(): void {
    streamingContent.value = ''
    chatError.value = ''
  }

  return {
    /** 本线程是否正在流式接收回复（渲染用；导航守卫看的是 store 的 `busy`） */
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
    /** 清空临时状态 */
    reset,
  }
}
