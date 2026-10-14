"""火山方舟（Volcano Ark）聊天模型封装。

通过官方 `arkruntime` SDK 调用豆包等模型，对外提供与 LangChain 类似的
`astream(messages)` 异步流式接口，逐块产出 `AIMessageChunk`，
使上游 `conversation_service` 无需感知底层 SDK 的差异。

设计要点：
- 方舟的豆包系列是「混合思考模型」，默认会先吐一大段 `reasoning_content`
  再出正文，造成首字极慢、正文爆发的观感。这里默认下发
  `thinking={"type": "disabled"}` 关闭官方思考、直接回答；是否开启由
  `ark_thinking_enabled` 控制（默认关闭）。后续若接入自主可控的思考机制，
  与此开关无关。
- 仅对「自带官方思考」的方舟模型下发关闭思考参数；其他供应商（Anthropic /
  OpenAI）走各自的 LangChain 路径，不做该处理。
- 流式模式默认下发 `stream_options={"include_usage": True}`，把 token 用量放在
  **最后一个分块**的顶层 `usage` 上，再映射成 LangChain 的 `usage_metadata`
  （`input_tokens` / `output_tokens` / `total_tokens`），使上游无需区分供应商即可计量。
  注意该分块的 `choices` 为空，读取顺序不能颠倒（详见 `astream` 内的说明）。
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from arkruntime import AsyncArk
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

# 方舟接受的消息角色，按 LangChain 消息类型映射
_ROLE_MAP: dict[type[BaseMessage], str] = {
    SystemMessage: "system",
    HumanMessage: "user",
    AIMessage: "assistant",
    ToolMessage: "tool",
}


def _role_of(message: BaseMessage) -> str:
    """把 LangChain 消息映射到方舟角色；未知类型兜底为 user。"""
    for message_type, role in _ROLE_MAP.items():
        if isinstance(message, message_type):
            return role
    return "user"


def _content_of(message: BaseMessage) -> str:
    """提取消息正文文本，兼容字符串与多模态 `list[dict]` 两种格式。"""
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "".join(parts)
    return str(content)


def _usage_metadata(usage: Any) -> dict[str, Any] | None:
    """把方舟的 `CompletionUsage` 映射成 LangChain 的 `usage_metadata`。

    LangChain 的 openai / anthropic 集成统一用 `input_tokens` / `output_tokens` /
    `total_tokens` 这套键名，方舟侧转成同一种形状，上游（`stream_reply`）就只需认一种。
    字段缺失时返回 `None`——「没给用量」与「用量为零」是两回事，前者不该被记成一次零消耗。
    """
    if usage is None:
        return None
    prompt_tokens = getattr(usage, "prompt_tokens", None)
    completion_tokens = getattr(usage, "completion_tokens", None)
    if prompt_tokens is None and completion_tokens is None:
        return None
    input_tokens = int(prompt_tokens or 0)
    output_tokens = int(completion_tokens or 0)
    total_tokens = getattr(usage, "total_tokens", None)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": int(total_tokens) if total_tokens is not None else input_tokens + output_tokens,
    }


class VolcanoArkChatModel:
    """基于官方 arkruntime 的异步流式聊天模型。"""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        *,
        thinking_enabled: bool = False,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.thinking_enabled = thinking_enabled

    async def astream(self, messages: list[BaseMessage]) -> AsyncIterator[AIMessageChunk]:
        """异步流式产出模型回复，每块为 `AIMessageChunk`。

        官方 SDK 的 `create` 返回的是异步可迭代对象。这里每次调用新建一个
        客户端并在结束后关闭，避免长生命周期连接池在请求间泄漏。
        """
        client = (
            AsyncArk(api_key=self.api_key, base_url=self.base_url)
            if self.base_url
            else AsyncArk(api_key=self.api_key)
        )
        try:
            ark_messages = [
                {"role": _role_of(message), "content": _content_of(message)}
                for message in messages
            ]
            kwargs: dict[str, Any] = {
                "model": self.model,
                "messages": ark_messages,
                "stream": True,
                "temperature": self.temperature,
                # 索取用量：不加这一项时，方舟在流式模式下不回吐任何 token 计数
                # （`include_usage` 的定义是「把用量放进最后一个流式分块」）
                "stream_options": {"include_usage": True},
            }
            # 显式指定单次回复的 token 上限，避免长文档被默认上限截断
            if self.max_tokens is not None:
                kwargs["max_tokens"] = self.max_tokens
            # 仅对自带思考的方舟模型下发；默认关闭，直接回答。
            if not self.thinking_enabled:
                kwargs["thinking"] = {"type": "disabled"}

            stream = await client.chat.completions.create(**kwargs)
            # 跨块记住结束原因：开启用量后，**携带用量的那一帧 `choices` 为空**，
            # 它自己没有 finish_reason，必须在之前的正文帧上记下来再带到最后，
            # 否则上游的截断检测（读最后一块的 finish_reason）会永远判不出截断。
            last_finish_reason = ""
            async for chunk in stream:
                # 先取用量、再看 choices：这是与旧实现的**唯一实质差异**。旧写法
                # `if not chunk.choices: continue` 会把用量帧整帧丢掉，表现为
                # 「开了 include_usage 却永远拿不到 usage」。
                usage_metadata = _usage_metadata(getattr(chunk, "usage", None))
                choices = getattr(chunk, "choices", None) or []

                if choices:
                    choice = choices[0]
                    text = getattr(choice.delta, "content", None) or ""
                    finish_reason = getattr(choice, "finish_reason", None)
                    if finish_reason:
                        last_finish_reason = str(finish_reason)
                    if text or last_finish_reason:
                        yield AIMessageChunk(
                            content=text,
                            response_metadata=(
                                {"finish_reason": last_finish_reason} if last_finish_reason else {}
                            ),
                            usage_metadata=usage_metadata,
                        )
                elif usage_metadata is not None:
                    # 用量帧：没有正文，但要把结束原因一并透出，让最后一块自洽
                    yield AIMessageChunk(
                        content="",
                        response_metadata=(
                            {"finish_reason": last_finish_reason} if last_finish_reason else {}
                        ),
                        usage_metadata=usage_metadata,
                    )
        finally:
            await client.close()
