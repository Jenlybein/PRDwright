"""会话相关路由。

**本层只做参数解析与响应封装，不写业务逻辑**，具体实现委托给
`conversation_service` 与 `document_service`。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from backend.api import schemas
from backend.core.errors import AppError, ErrorCode
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
                    code=ErrorCode.INTERNAL_ERROR.value,
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
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            f"问卷配置文件不存在：{QUESTIONS_CONFIG_PATH}",
        ) from exc

    try:
        config: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            f"问卷配置文件不是合法 JSON：{exc}",
        ) from exc

    return schemas.QuestionsConfig.model_validate(config)


# ============================================================================
# 结构化摘要回填 & 接口规范检索（一次性 JSON 接口，非流式）
# ============================================================================


@router.post(
    "/sync-summary-from-conversation",
    response_model=schemas.SummarySyncResponse,
    summary="从对话历史回填结构化需求摘要",
)
async def sync_summary_from_conversation(
    payload: schemas.SummarySyncRequest,
) -> schemas.SummarySyncResponse:
    """从对话历史里抽取用户**明确补充或修正**的字段，回填成一份完整摘要。

    返回的是完整摘要（含未改动字段），前端据此与本地摘要做 Diff，弹面板让用户
    确认后才进入 PRD 生成流程。回填规则（保持原值 / 不新增字段 / 返回完整结构）
    由 `document_service` 的提示词与本地合并逻辑共同保证。
    """
    if not payload.summary:
        return schemas.SummarySyncResponse(summary={}, changed=False)

    # 空列表视为「无对话」，交给服务层按无补充对话处理
    history = [{"role": m.role, "content": m.content} for m in payload.history] or None
    try:
        updated = await document_service.sync_summary_from_conversation(
            summary=payload.summary,
            history=history,
        )
    except Exception as exc:  # noqa: BLE001 - 模型调用失败统一转成 502
        raise AppError(
            ErrorCode.LLM_UPSTREAM_ERROR,
            f"需求摘要回填失败：{exc}",
        ) from exc

    return schemas.SummarySyncResponse(summary=updated, changed=updated != payload.summary)


@router.post(
    "/retrieve-api-docs-rag",
    response_model=schemas.ApiDocsRagResponse,
    summary="检索与 PRD 相关的接口规范与历史示例（RAG）",
)
async def retrieve_api_docs_rag(
    payload: schemas.ApiDocsRagRequest,
) -> schemas.ApiDocsRagResponse:
    """在团队接口规范库里检索与当前 PRD 相关的规范条文与历史接口示例。

    前端拿到命中列表后展示给用户确认，确认的条目再随
    `/generate-api-docs-stream` 下发，作为生成接口文档时的对齐依据。
    """
    history = [{"role": m.role, "content": m.content} for m in payload.history] or None
    try:
        hits = await document_service.retrieve_api_docs_rag_hits(
            prd_content=payload.prd_content,
            history=history,
            limit=payload.limit,
        )
    except Exception as exc:  # noqa: BLE001 - 检索异常统一转成 500
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            f"接口规范检索失败：{exc}",
        ) from exc

    return schemas.ApiDocsRagResponse(
        hits=[schemas.RagHit.model_validate(hit) for hit in hits]
    )


@router.post("/start-stream", summary="提交问卷并开启会话（SSE 流式返回）")
async def start_conversation_stream(
    payload: schemas.ConversationCreateRequest,
) -> StreamingResponse:
    """提交问卷答案、创建会话，并以 SSE 流式返回模型的第一轮回复。

    事件序列：`status`（`data` 里带本次的 `conversation_id`，前端需立即记录）
    → 若干 `chunk`（增量文本）→ `done`（带完整 `reply`）；出错时为 `error`。

    请求里没有提示词参数：本轮用哪套人设与开场白，由后端按默认对话档案决定
    （`services/prompts.py` 的 `ConversationProfile`）。
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

    人设由会话记录里的档案标识实时解析；无状态模式（服务端会话已丢失）下回退到
    默认档案，前端不需要也没有通道重新下发提示词。
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
# 文档流式生成（document_service 的 *_stream 方法，SSE 流式返回）
# ============================================================================


@router.post("/generate-prd-stream", summary="根据对话生成 PRD（SSE 流式返回）")
async def generate_prd_stream(
    payload: schemas.GeneratePrdStreamRequest,
) -> StreamingResponse:
    """根据需求澄清结果与补充对话，流式返回 PRD 正文。

    事件序列：`status` → 若干 `chunk` → `done`（`data` 带完整 `reply` 与
    `doc_type=prd`）；出错时为 `error`。
    """
    # 空列表视为「无补充对话」
    history = [{"role": m.role, "content": m.content} for m in payload.history] or None
    events = document_service.generate_prd_stream(
        requirements=payload.requirements,
        history=history,
    )
    return StreamingResponse(
        _make_sse_generator(events),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post(
    "/generate-prd-from-summary-stream",
    summary="根据需求（结构化摘要或自由文本）生成 PRD（SSE 流式返回）",
)
async def generate_prd_from_summary_stream(
    payload: schemas.GeneratePrdFromSummaryStreamRequest,
) -> StreamingResponse:
    """根据结构化需求摘要**或**自由文本需求，流式返回 PRD 正文。

    相比 `/generate-prd-stream`（入参是已整理好的需求文本，走整篇生成），这里直接吃
    与 `field-schema.json` 对齐的摘要字典或快捷入口的一段话，服务层按输入形态选格式化
    函数后统一走「分章节撰写 → 审核 → 改写」链路——「需求 → PRD」在后端是一个自洽的
    能力，可被多个入口复用，前端不必自己拼需求文本。

    事件序列：`status` → `chunk`（文档头）→ 若干 `stage`/`chunk`（每章）→
    `stage: done` → `done`（`data` 带完整 `reply` 与 `doc_type=prd`）；出错时为 `error`。
    """
    if not payload.summary and not payload.requirements.strip():
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            "summary 与 requirements 至少需要提供一个",
        )

    # 空列表视为「无补充对话」
    history = [{"role": m.role, "content": m.content} for m in payload.history] or None
    events = document_service.generate_prd_from_summary_stream(
        summary=payload.summary,
        requirements=payload.requirements,
        product_name=payload.product_name,
        history=history,
    )
    return StreamingResponse(
        _make_sse_generator(events),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post("/generate-api-docs-stream", summary="根据 PRD 生成接口文档（SSE 流式返回）")
async def generate_api_docs_stream(
    payload: schemas.GenerateApiDocsStreamRequest,
) -> StreamingResponse:
    """根据 PRD 正文流式返回接口文档。

    `rag_hits` 为用户在前端确认过的检索命中（团队接口规范 / 历史接口示例），
    会作为「参考规范」拼进提示词，要求模型对齐团队既有约定。
    """
    events = document_service.generate_api_docs_stream(
        prd_content=payload.prd_content,
        rag_hits=[hit.model_dump() for hit in payload.rag_hits] or None,
    )
    return StreamingResponse(
        _make_sse_generator(events),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post("/generate-prompts-stream", summary="根据 PRD 生成提示词套件（SSE 流式返回）")
async def generate_prompts_stream(
    payload: schemas.GeneratePromptsStreamRequest,
) -> StreamingResponse:
    """根据 PRD 正文流式返回提示词套件。"""
    events = document_service.generate_prompts_stream(
        prd_content=payload.prd_content,
    )
    return StreamingResponse(
        _make_sse_generator(events),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post("/optimize-document-stream", summary="优化文档（SSE 流式返回）")
async def optimize_document_stream(
    payload: schemas.OptimizeDocumentStreamRequest,
) -> StreamingResponse:
    """按用户指令流式返回优化后的文档。"""
    events = document_service.optimize_document_stream(
        document=payload.document,
        instruction=payload.instruction,
    )
    return StreamingResponse(
        _make_sse_generator(events),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )

