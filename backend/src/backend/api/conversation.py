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
from backend.api.sse import (  # noqa: F401 - SSE_DONE/SSE_HEADERS/make_sse_generator 仅作再导出
    SSE_DONE,
    SSE_HEADERS,
    make_sse_generator,
    sse_response,
)
from backend.core.errors import AppError, ErrorCode
from backend.services import conversation_service, document_service

router = APIRouter(prefix="/api/conversation", tags=["conversation"])

# 问卷配置文件：backend/src/backend/core/question_config.json
QUESTIONS_CONFIG_PATH = Path(__file__).resolve().parents[1] / "core" / "question_config.json"

# SSE 的分帧与响应头已抽到 `backend/api/sse.py`（`/api/jobs/{id}/stream` 也用它），
# 路由统一改用 `sse_response(events)`，分帧行为逐字节不变。


def _message_to_history_item(message: schemas.MessageItem) -> dict[str, Any]:
    """把前端回传的历史消息转成服务层的 `FrontendMessage` 形状。

    工具调用链字段只在真有值时带上：`_build_lc_messages` 按 `.get()` 取可选字段，
    空壳键只会让历史变脏。`tool_result` 是 Pydantic 模型，要摊成 dict。
    """
    item: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        item["tool_calls"] = list(message.tool_calls)
    if message.tool_call_id:
        item["tool_call_id"] = message.tool_call_id
    if message.tool_result is not None:
        item["tool_result"] = message.tool_result.model_dump()
    return item


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
# 结构化摘要回填（一次性 JSON 接口，非流式）
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


@router.post("/start-stream", summary="提交问卷并开启会话（SSE 流式返回）")
async def start_conversation_stream(
    payload: schemas.ConversationCreateRequest,
) -> StreamingResponse:
    """提交问卷答案、创建会话，并以 SSE 流式返回模型的第一轮回复。

    事件序列：`status`（`data` 里带本次的 `conversation_id`，前端需立即记录）
    → 若干 `chunk`（增量文本）→ `done`（带完整 `reply`）；出错时为 `error`。

    请求里没有提示词参数：本轮用哪套人设与开场白，由后端按对话档案决定
    （`services/prompts.py` 的 `ConversationProfile`）。`profile_key` 只用于**点名场景**
    （留空取默认的「需求澄清」），未知取值直接 400——不静默回退默认档案。
    """
    # 先同步校验档案：错误发生在流开始之前，才可能是一个真正的 400
    # （进了 SSE 之后状态码恒为 200，前端只能靠错误码猜）
    conversation_service.check_profile_key(payload.profile_key)
    events = conversation_service.start_stream(
        form_data=payload.answers,
        title=payload.title,
        profile_key=payload.profile_key,
    )
    return sse_response(events)


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
    # 空列表视为「未传」，交给服务层回退到服务端历史。
    # 工具调用链字段（tool_calls / tool_call_id / tool_result）要原样带过去：
    # 作答轮要把答案对回发起提问的那次调用，裁掉就只剩悬空的 tool_call。
    history = [_message_to_history_item(m) for m in payload.history] or None
    # 同 `/start-stream`：档案标识在进流之前校验
    conversation_service.check_profile_key(payload.profile_key)
    events = conversation_service.continue_stream(
        user_message=payload.content,
        conversation_id=payload.conversation_id or None,
        history=history,
        # 服务重启后会话记录已丢，靠它把「点的是哪个场景」带过来，避免人设悄悄换回默认
        profile_key=payload.profile_key,
        # 结构化作答回灌：带上时本轮按 ToolMessage 应答工具调用，不重复投喂人话
        tool_result=(
            payload.tool_result.model_dump() if payload.tool_result is not None else None
        ),
    )
    return sse_response(events)


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
    return sse_response(events)


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
    return sse_response(events)


@router.post("/generate-api-docs-stream", summary="根据 PRD 生成接口文档（SSE 流式返回）")
async def generate_api_docs_stream(
    payload: schemas.GenerateApiDocsStreamRequest,
) -> StreamingResponse:
    """根据 PRD 正文流式返回接口文档。

    团队接口规范（统一响应结构、错误码、分页、鉴权等）由 `api-docs-generator` 技能包
    整段注入提示词，要求模型按团队约定对齐——不再做「按 PRD 关键词检索挑几条」的 RAG。
    """
    events = document_service.generate_api_docs_stream(
        prd_content=payload.prd_content,
    )
    return sse_response(events)


@router.post("/generate-prompts-stream", summary="根据 PRD 生成提示词套件（SSE 流式返回）")
async def generate_prompts_stream(
    payload: schemas.GeneratePromptsStreamRequest,
) -> StreamingResponse:
    """根据 PRD 正文流式返回提示词套件。"""
    events = document_service.generate_prompts_stream(
        prd_content=payload.prd_content,
    )
    return sse_response(events)


@router.post("/optimize-document-stream", summary="优化文档（SSE 流式返回）")
async def optimize_document_stream(
    payload: schemas.OptimizeDocumentStreamRequest,
) -> StreamingResponse:
    """按用户指令流式返回优化后的文档。"""
    events = document_service.optimize_document_stream(
        document=payload.document,
        instruction=payload.instruction,
    )
    return sse_response(events)

