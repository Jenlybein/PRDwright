"""SSE（Server-Sent Events）编码。

把服务层产出的 `StreamEvent` 事件流转成 `text/event-stream` 文本，并补上 `[DONE]`
结束标记。**这是全项目唯一一份 SSE 分帧实现**——历史上它长在 `api/conversation.py` 里，
本期因为 `/api/jobs/{id}/stream` 也要用同一套编码而抽出来，分帧逻辑逐字节不变。

职责边界：本层只做「事件对象 → SSE 文本」的转码与响应封装，不含任何业务判断。
「事件从哪来」由调用方（各路由）决定。
"""

from __future__ import annotations

from typing import AsyncIterator

from fastapi.responses import StreamingResponse

from backend.api import schemas
from backend.core.errors import ErrorCode

# SSE 结束标记，前端收到后主动关闭连接
SSE_DONE = "[DONE]"

# SSE 响应头：禁用缓存与 Nginx 缓冲，保证流式实时输出
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def format_sse_block(event: schemas.StreamEvent) -> str:
    """把单个事件格式化成标准 SSE 块。

    同时输出 `event:` 与 `data:` 两行：前端既可以用
    `addEventListener(event.type, ...)` 分类型监听，也可以只监听默认消息、
    再解析 `data` 里的 `type` 字段。事件体统一为 JSON，`content` 里的换行
    会被 JSON 转义，因此 `data:` 始终保持单行，不会破坏 SSE 帧边界。

    Args:
        event: 待发送的事件。

    Returns:
        形如 `event: chunk\\ndata: {...}\\n\\n` 的字符串。
    """
    return f"event: {event.type}\ndata: {event.model_dump_json()}\n\n"


def make_sse_generator(events: AsyncIterator[schemas.StreamEvent]) -> AsyncIterator[str]:
    """把服务层产出的事件流包装成 SSE（text/event-stream）文本。

    服务层自身已把异常转成 `error` 事件，这里再兜一层：万一生成器抛出未捕获
    异常，也补发一个 `error` 块，避免连接直接断开而前端收不到任何提示。
    无论正常结束还是出错，最后都会补发 `[DONE]` 标记。

    Args:
        events: 服务层返回的 `StreamEvent` 异步迭代器。

    Yields:
        符合 SSE 规范的字符串片段，每个事件为 `event:` + `data:` 组成的块。
    """

    # 事件流的转码在生成器内部完成，避免路由层掺入序列化逻辑
    async def generator() -> AsyncIterator[str]:
        try:
            async for event in events:
                yield format_sse_block(event)
        except Exception as exc:  # noqa: BLE001 - SSE 流里必须把异常转成事件
            yield format_sse_block(
                schemas.StreamEvent(
                    type="error",
                    content=f"生成失败：{exc}",
                    data={"error_type": type(exc).__name__},
                    code=ErrorCode.INTERNAL_ERROR.value,
                )
            )
        yield f"data: {SSE_DONE}\n\n"

    return generator()


def sse_response(events: AsyncIterator[schemas.StreamEvent]) -> StreamingResponse:
    """把事件流包装成 `StreamingResponse`（统一 media type 与响应头）。

    Args:
        events: 服务层的 `StreamEvent` 异步迭代器。

    Returns:
        可直接从路由返回的 `StreamingResponse`。
    """
    return StreamingResponse(
        make_sse_generator(events),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
