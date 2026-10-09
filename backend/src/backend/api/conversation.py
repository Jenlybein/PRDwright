"""会话相关路由。

**本层只做参数解析与响应封装，不写业务逻辑**，具体实现委托给
`conversation_service` 与 `document_service`。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse

from backend.api import schemas
from backend.services import conversation_service, document_service

router = APIRouter(prefix="/api/conversation", tags=["conversation"])

# 问卷配置文件：backend/src/backend/core/question_config.json
QUESTIONS_CONFIG_PATH = Path(__file__).resolve().parents[1] / "core" / "question_config.json"

# SSE 结束标记，前端收到后主动关闭连接
SSE_DONE = "[DONE]"

# SSE 响应头：禁用缓存与 Nginx 缓冲，保证流式实时输出
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _format_sse_block(event: schemas.StreamEvent) -> str:
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


def _make_sse_generator(events: AsyncIterator[schemas.StreamEvent]) -> AsyncIterator[str]:
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
                yield _format_sse_block(event)
        except Exception as exc:  # noqa: BLE001 - SSE 流里必须把异常转成事件
            yield _format_sse_block(
                schemas.StreamEvent(
                    type="error",
                    content=f"生成失败：{exc}",
                    data={"error_type": type(exc).__name__},
                )
            )
        yield f"data: {SSE_DONE}\n\n"

    return generator()


@router.get("/questions", response_model=schemas.QuestionsConfig, summary="获取需求澄清问卷")
async def get_questions() -> schemas.QuestionsConfig:
    """读取问卷配置并返回，前端据此渲染表单。"""
    try:
        raw = QUESTIONS_CONFIG_PATH.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"问卷配置文件不存在：{QUESTIONS_CONFIG_PATH}",
        ) from exc

    try:
        config: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"问卷配置文件不是合法 JSON：{exc}",
        ) from exc

    return schemas.QuestionsConfig.model_validate(config)


@router.post("/start-stream", summary="提交问卷并开启会话（SSE 流式返回）")
async def start_conversation_stream(
    payload: schemas.ConversationCreateRequest,
) -> StreamingResponse:
    """提交问卷答案、创建会话，并以 SSE 流式返回模型的第一轮回复。

    事件序列：`status`（`data` 里带本次的 `conversation_id`，前端需立即记录）
    → 若干 `chunk`（增量文本）→ `done`（带完整 `reply`）；出错时为 `error`。
    """
    events = conversation_service.start_stream(
        form_data=payload.answers,
        title=payload.title,
    )
    return StreamingResponse(
        _make_sse_generator(events),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post("/continue-stream", summary="接续对话（SSE 流式返回）")
async def continue_conversation_stream(
    payload: schemas.ConversationContinueRequest,
) -> StreamingResponse:
    """在已有会话上继续对话，以 SSE 流式返回模型回复。

    `conversation_id` 与 `history` 至少提供一种：给出 ID 时用服务端保存的
    历史；不给出则由前端回传完整历史走无状态模式。两者都给时以 `history` 为准。
    """
    # 空列表视为「未传」，交给服务层回退到服务端历史
    history = [{"role": m.role, "content": m.content} for m in payload.history] or None
    events = conversation_service.continue_stream(
        user_message=payload.content,
        conversation_id=payload.conversation_id or None,
        history=history,
    )
    return StreamingResponse(
        _make_sse_generator(events),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


# ============================================================================
# 文档（document）：基于会话上下文生成 / 读取 / 更新文档
# 当前接口已接线，但 `document_service` 尚未实现，调用会返回 500
# ============================================================================


@router.post(
    "/{conversation_id}/document",
    response_model=schemas.DocumentResponse,
    summary="生成文档",
)
async def generate_document(
    conversation_id: str,
    payload: schemas.DocumentGenerateRequest,
) -> schemas.DocumentResponse:
    """基于会话上下文生成文档（尚未实现）。"""
    return await document_service.generate_document(
        conversation_id=conversation_id,
        doc_type=payload.doc_type,
    )


@router.get(
    "/{conversation_id}/document",
    response_model=schemas.DocumentResponse,
    summary="获取文档",
)
async def get_document(
    conversation_id: str,
    doc_type: schemas.DocumentType = "prd",
) -> schemas.DocumentResponse:
    """读取会话下的文档（尚未实现）。"""
    return await document_service.get_document(
        conversation_id=conversation_id,
        doc_type=doc_type,
    )


@router.put(
    "/{conversation_id}/document",
    response_model=schemas.DocumentResponse,
    summary="更新文档",
)
async def update_document(
    conversation_id: str,
    payload: schemas.DocumentUpdateRequest,
    doc_type: schemas.DocumentType = "prd",
) -> schemas.DocumentResponse:
    """更新文档内容（尚未实现）。"""
    return await document_service.update_document(
        conversation_id=conversation_id,
        doc_type=doc_type,
        content=payload.content,
    )
