import { computed, ref, type Ref } from 'vue'
import { storeToRefs } from 'pinia'

import { continueConversationStream, startConversationStream } from '@/services/api'
import { isApiError } from '@/services/errors'
import { useWorkflowStore } from '@/stores/workflow'
import type { ChatMessage, RequirementAnswers, RequirementsSummary } from '@/types'
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
 * 这套骨架服务的会话落在哪里、说的是哪一套人设。
 *
 * 需求澄清与文档澄清共用同一套流式逻辑（缓冲、固化、断线重试），差异只有三点：聊天记录
 * 放在哪两个字段里、后端用哪个档案。把差异做成参数而不是复制一份 composable，
 * 是为了让「断线重试」「错误保留半截内容」这些细节只维护一处。
 */
export interface ClarificationChatOptions {
  /** 对话档案标识：后端据此选人设与开场白（如 `doc-clarification`） */
  profileKey?: string
  /** 聊天记录的存放位置；默认是需求澄清的 `messages` */
  messages?: Ref<ChatMessage[]>
  /** 会话 id 的存放位置；默认是需求澄清的 `conversationId` */
  conversationId?: Ref<string>
}

/**
 * 澄清对话流程。
 *
 * 收敛「流式接续对话」这一摊状态与副作用：流式缓冲、错误提示，以及把增量
 * 固化成消息的收尾逻辑。会话数据本身仍在 Pinia store 里，这里只做编排，
 * 因此组件侧不需要再持有任何流程状态。
 *
 * @param hooks 需要向外部借用的能力（提示、切阶段）。
 * @param options 会话落点与档案标识；不传即需求澄清。
 */
export function useClarificationChat(hooks: ChatHooks, options: ClarificationChatOptions = {}) {
  const store = useWorkflowStore()
  const state = storeToRefs(store)
  const { summary, chatStreaming } = state
  const messages = options.messages ?? state.messages
  const conversationId = options.conversationId ?? state.conversationId
  const profileKey = options.profileKey

  /** 正在流式生成的回复文本，作为列表末尾那条气泡的实时内容 */
  const streamingContent = ref('')

  /** 对话过程中的错误提示 */
  const chatError = ref('')

  /**
   * 交给消息列表渲染的内容。
   * 流式生成期间在末尾挂一条 assistant 气泡：还没收到增量内容时 content 为空，
   * 由列表渲染「思考中」动画指示；首字到达后自动切换为 Markdown 实时渲染。
   */
  const displayMessages = computed<ChatMessage[]>(() => {
    if (!chatStreaming.value) return messages.value
    return [
      ...messages.value,
      {
        role: 'assistant',
        content: streamingContent.value,
        streaming: true,
      },
    ]
  })

  /** 把增量文本追加到流式缓冲，由消息列表实时渲染 */
  function handleChunk(chunk: string): void {
    streamingContent.value += chunk
  }

  /**
   * 执行一次流式请求并收尾。
   *
   * 增量文本先落到 `streamingContent` 实时展示，请求结束后再固化成消息列表里的一条
   * assistant 消息；中途出错时保留已经流出的内容，避免整段凭空消失。
   *
   * @param request 实际发起请求的函数；接续对话需要在内部做重试，因此以闭包传入。
   */
  async function runStream(
    request: () => Promise<{ conversationId: string; reply: string }>,
  ): Promise<void> {
    chatStreaming.value = true
    streamingContent.value = ''
    chatError.value = ''

    let reply = ''
    try {
      const result = await request()
      if (result.conversationId) conversationId.value = result.conversationId
      reply = result.reply
    } catch (error) {
      chatError.value = error instanceof Error ? error.message : String(error)
      // 报错时把已经流出的半截内容保留下来
      reply = streamingContent.value
    } finally {
      // 先结束流式状态再固化消息，否则列表里会短暂多出一条「思考中」气泡
      chatStreaming.value = false
      const content = reply.trim()
      if (content) messages.value.push({ role: 'assistant', content })
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
   */
  async function sendMessage(content: string): Promise<void> {
    const text = content.trim()
    if (!text || chatStreaming.value) return

    messages.value.push({ role: 'user', content: text })
    chatError.value = ''

    // 历史只取本轮之前的消息，本轮输入单独通过 content 传递
    const history = messages.value
      .slice(0, -1)
      .map((message) => ({ role: message.role, content: message.content }))

    await runStream(async () => {
      try {
        return await continueConversationStream(
          {
            content: text,
            conversationId: conversationId.value || undefined,
            history,
            profileKey,
          },
          { onChunk: handleChunk },
        )
      } catch (error) {
        if (!conversationId.value || !isApiError(error, 'SESSION_NOT_FOUND')) throw error
        // 会话已失效：丢掉旧 ID 与半截输出，退回无状态模式重试。
        // 重试时仍要带上档案标识——否则文档澄清的人设会在这里悄悄换回默认的那套。
        conversationId.value = ''
        streamingContent.value = ''
        return continueConversationStream(
          { content: text, history, profileKey },
          { onChunk: handleChunk },
        )
      }
    })
  }

  /**
   * 用一组现成的表单数据开启会话：切到对话阶段 → 调流式接口 → 实时展示 AI 的第一轮追问。
   *
   * 两条链路共用：需求澄清提交结构化摘要（由 `buildAnswers` 映射到后端题目的字段名），
   * 文档澄清提交 `{document, pending_items}`。人设与首轮开场白由后端的对话档案决定，
   * 前端只提交内容，不参与措辞。
   */
  async function startConversationWithAnswers(
    answers: RequirementAnswers,
    notice = '正在生成回复',
  ): Promise<void> {
    // 先切阶段再发请求：用户立刻看到「思考中」，而不是停在原页面干等
    messages.value = []
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
    /** 是否正在流式接收回复 */
    streaming: chatStreaming,
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
