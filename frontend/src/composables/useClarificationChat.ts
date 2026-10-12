import { computed, ref } from 'vue'
import { storeToRefs } from 'pinia'

import { continueConversationStream, startConversationStream } from '@/services/api'
import { useWorkflowStore } from '@/stores/workflow'
import type { ChatMessage, RequirementsSummary } from '@/types'
import { buildAnswers } from '@/utils/requirements'

/**
 * 澄清对话专用系统提示词。
 *
 * 通过 `system_prompt_override` 下发给后端，替换服务默认人设：只做澄清 / 补充 /
 * 确认 / 纠偏，一次只问 1-3 个关键问题，绝不代写 PRD 或任何完整文档。
 * 后端会把它存进会话记录，后续接续对话自动沿用；无状态重试时由前端重新下发。
 */
export const CLARIFICATION_SYSTEM_PROMPT = `你是一位资深产品经理，专注把用户填写的结构化需求摘要补齐、确认。

## 任务边界
- 只做四件事：澄清（问清没说清的关键点）、补充（引导用户给出缺失信息）、确认（复述你的理解并与用户对齐）、纠偏（指出与已有摘要矛盾或明显不合理的地方）。
- 绝不生成 PRD、接口文档、提示词或任何完整文档；不要输出文档章节标题，也不要写长段正文。
- 用户明确要求「直接出文档」时，也只用一句话说明需要先补齐哪些信息。

## 提问规则
- 每次只提 1-3 个关键问题，按对 PRD 质量的影响从高到低排序，不要贪多。
- 问题要具体、可直接回答；避免「请详细描述一下」这类空泛提问。
- 不要重复已经问过并被回答过的问题。

## 信息足够时
- 停止追问，只回一句话：「信息已经足够，可以点击生成 PRD」。不要再展开新的问题或补充长篇总结。

全程使用中文回答，语气专业、简洁。`

/** 对话流程需要向外部借用的能力 */
export interface ChatHooks {
  /** 展示一条短暂提示 */
  onNotice: (text: string) => void
  /** 把阶段切到对话页 */
  onEnterChat: () => void
}

/**
 * 澄清对话流程。
 *
 * 收敛「流式接续对话」这一摊状态与副作用：流式缓冲、错误提示，以及把增量
 * 固化成消息的收尾逻辑。会话数据本身仍在 Pinia store 里，这里只做编排，
 * 因此组件侧不需要再持有任何流程状态。
 */
export function useClarificationChat(hooks: ChatHooks) {
  const store = useWorkflowStore()
  const { summary, messages, conversationId, chatStreaming } = storeToRefs(store)

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
   * 后端会话存在内存里，服务重启后 ID 会失效；收到「会话不存在」时清掉 ID，
   * 改用前端回传的完整历史重试一次（无状态模式）。无状态模式下后端拿不到会话记录里
   * 保存的人设，因此每次都要把澄清专用提示词重新下发一遍。
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
            systemPrompt: CLARIFICATION_SYSTEM_PROMPT,
          },
          { onChunk: handleChunk },
        )
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error)
        if (!conversationId.value || !message.includes('会话不存在')) throw error
        // 会话已失效：丢掉旧 ID 与半截输出，退回无状态模式重试
        conversationId.value = ''
        streamingContent.value = ''
        return continueConversationStream(
          { content: text, history, systemPrompt: CLARIFICATION_SYSTEM_PROMPT },
          { onChunk: handleChunk },
        )
      }
    })
  }

  /**
   * 提交结构化需求并开启会话：切到对话阶段 → 调流式接口 → 实时展示 AI 的第一轮追问。
   *
   * 表单内部已完成校验，这里只负责推进流程；`answers` 由结构化摘要映射到后端
   * 已有题目的字段名（见 `utils/requirements.ts`）。同时下发澄清对话专用的系统提示词，
   * 让这一轮（以及后续接续对话）都只做澄清与追问，不代写文档。
   */
  async function startConversation(value: RequirementsSummary): Promise<void> {
    summary.value = value

    // 先切阶段再发请求：用户立刻看到「思考中」，而不是停在表单上干等
    messages.value = []
    conversationId.value = ''
    chatError.value = ''
    hooks.onEnterChat()
    hooks.onNotice('需求已提交，正在生成回复')

    await runStream(() =>
      startConversationStream(
        { answers: buildAnswers(value), systemPrompt: CLARIFICATION_SYSTEM_PROMPT },
        { onChunk: handleChunk },
      ),
    )
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
    /** 提交需求并开启会话 */
    startConversation,
    /** 清空临时状态 */
    reset,
  }
}
