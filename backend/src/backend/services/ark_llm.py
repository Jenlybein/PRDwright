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


class VolcanoArkChatModel:
    """基于官方 arkruntime 的异步流式聊天模型。"""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str | None = None,
        temperature: float = 0.7,
        *,
        thinking_enabled: bool = False,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.temperature = temperature
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
            }
            # 仅对自带思考的方舟模型下发；默认关闭，直接回答。
            if not self.thinking_enabled:
                kwargs["thinking"] = {"type": "disabled"}

            stream = await client.chat.completions.create(**kwargs)
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                text = getattr(delta, "content", None)
                if text:
                    yield AIMessageChunk(content=text)
        finally:
            await client.close()
