"""会话业务服务（尚未实现）。

路由层只做参数解析，具体业务逻辑后续在这里实现。
目前所有函数都是占位，调用会抛出 NotImplementedError。
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from backend.api.schemas import ConversationResponse, StreamEvent


async def create_conversation(
    title: str = "",
    answers: dict[str, Any] | None = None,
) -> ConversationResponse:
    """创建会话并初始化上下文。"""
    raise NotImplementedError("conversation_service.create_conversation 尚未实现")


async def get_conversation(conversation_id: str) -> ConversationResponse:
    """按 ID 获取会话详情。"""
    raise NotImplementedError("conversation_service.get_conversation 尚未实现")


async def list_conversations() -> list[ConversationResponse]:
    """获取会话列表。"""
    raise NotImplementedError("conversation_service.list_conversations 尚未实现")


async def stream_reply(
    conversation_id: str,
    content: str,
) -> AsyncIterator[StreamEvent]:
    """处理用户消息，以事件流形式逐步产出回复。"""
    raise NotImplementedError("conversation_service.stream_reply 尚未实现")
    yield  # pragma: no cover  # 让上面的 raise 与返回类型共存
