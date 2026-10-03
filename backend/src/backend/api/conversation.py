"""会话相关路由。

**本层只做参数解析与响应封装，不写业务逻辑**，具体实现委托给
`conversation_service` / `document_service`。
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


def _make_sse_generator(events: AsyncIterator[schemas.StreamEvent]) -> AsyncIterator[str]:
    """把服务层产出的事件流包装成 SSE（text/event-stream）文本。

    Args:
        events: 服务层返回的 `StreamEvent` 异步迭代器。

    Yields:
        符合 SSE 规范的字符串片段，事件体为 JSON。
    """
    # 事件流的转码在生成器内部完成，避免路由层掺入序列化逻辑
    async def generator() -> AsyncIterator[str]:
        async for event in events:
            yield f"data: {event.model_dump_json()}\n\n"
        yield f"data: {SSE_DONE}\n\n"

    return generator()


# ============================================================================
# 已实现
# ============================================================================


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


# ============================================================================
# 空壳路由：仅解析参数并转发给服务层，业务逻辑待实现
# ============================================================================


@router.post(
    "",
    response_model=schemas.ConversationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="创建会话",
)
async def create_conversation(
    payload: schemas.ConversationCreateRequest,
) -> schemas.ConversationResponse:
    """创建会话（空壳）。"""
    return await conversation_service.create_conversation(
        title=payload.title,
        answers=payload.answers,
    )


@router.get("", response_model=schemas.ConversationListResponse, summary="获取会话列表")
async def list_conversations() -> schemas.ConversationListResponse:
    """获取会话列表（空壳）。"""
    items = await conversation_service.list_conversations()
    return schemas.ConversationListResponse(items=items, total=len(items))


@router.get(
    "/{conversation_id}",
    response_model=schemas.ConversationResponse,
    summary="获取会话详情",
)
async def get_conversation(conversation_id: str) -> schemas.ConversationResponse:
    """获取会话详情（空壳）。"""
    return await conversation_service.get_conversation(conversation_id)


@router.post("/{conversation_id}/messages", summary="发送消息（SSE 流式返回）")
async def send_message(
    conversation_id: str,
    payload: schemas.SendMessageRequest,
) -> StreamingResponse:
    """发送消息并以 SSE 流式返回回复（空壳）。"""
    events = conversation_service.stream_reply(
        conversation_id=conversation_id,
        content=payload.content,
    )
    return StreamingResponse(
        _make_sse_generator(events),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁用 Nginx 缓冲，保证流式实时输出
        },
    )


@router.post(
    "/{conversation_id}/document",
    response_model=schemas.DocumentResponse,
    summary="生成文档",
)
async def generate_document(
    conversation_id: str,
    payload: schemas.DocumentGenerateRequest,
) -> schemas.DocumentResponse:
    """基于会话上下文生成文档（空壳）。"""
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
    """读取会话下的文档（空壳）。"""
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
    """更新文档内容（空壳）。"""
    return await document_service.update_document(
        conversation_id=conversation_id,
        doc_type=doc_type,
        content=payload.content,
    )
