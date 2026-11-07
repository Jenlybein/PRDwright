"""会话相关路由。

**本层只做参数解析与响应封装，不写业务逻辑**，具体实现委托给
`conversation_service` 与 `document_service`。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from backend.api import schemas
from backend.api.deps import acting_user
from backend.api.sse import (  # noqa: F401 - SSE_DONE/SSE_HEADERS/make_sse_generator 仅作再导出
    SSE_DONE,
    SSE_HEADERS,
    make_sse_generator,
    sse_response,
)
from backend.core.errors import AppError, ErrorCode
from backend.services import conversation_service, document_service, job_inputs
from backend.services.auth_service import User
from backend.services.prd_skill import format_requirements_text

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


@router.post("/generate-prd-stream", summary="按方案生成 PRD（整篇，SSE 流式返回）")
async def generate_prd_stream(
    payload: schemas.GeneratePrdStreamRequest,
    user: User = Depends(acting_user),
) -> StreamingResponse:
    """按方案生成 PRD（**整篇**那条旧链路），流式返回 PRD 正文。

    与另三条旧通道同形（spec #51 / C-R3）：**只收方案**。它从前收的是前端已经拼好的
    需求文本（`requirements` + `history`），于是「需求正文」有了两个来源；现在需求输入
    （结构化摘要 / 快捷入口那段自由文本 / 需求澄清线程）由 `job_inputs` 按方案从库里
    解析，请求体里一个字段都不带——与 `/generate-prd-from-summary-stream` 用的是同一份
    上游，差别只在服务方法（这条走整篇生成，那条走分章节 + 审核）。

    事件序列：`status` → 若干 `chunk` → `done`（`data` 带完整 `reply` 与
    `doc_type=prd`）；出错时为 `error`。
    """
    # 解析在**构造 SSE 之前**：进了流之后状态码恒为 200，缺需求就只能读成「生成失败」
    inputs = job_inputs.resolve_runtime_inputs("prd", user.id, payload.solution_id)
    events = document_service.generate_prd_stream(
        requirements=_prd_requirements_text(inputs),
        history=inputs.get("history") or None,
    )
    return sse_response(events)


def _prd_requirements_text(inputs: dict[str, Any]) -> str:
    """把服务端解析出的 PRD 输入折成「整篇生成」那条链路要的一段需求文本。

    两条 PRD 入口的差别只在输入怎么来，而**整篇**链路的提示词只认一段 `requirements`
    文本（分章节链路才有结构化摘要与两条线程的插槽）。折叠规则只有一处：
    `prd_skill.format_requirements_text`（结构化摘要优先，其次自由文本，两处都先渲染）。
    从前这里自己写了一份「摘要优先、否则原文」，与分章链路那份（会渲染自由文本）和
    输入指纹那份（自由文本优先）三份各不相同——同一次生成，三处读到的「需求」不是一个东西。
    线程原样交给 `history`。
    """
    return format_requirements_text(
        inputs.get("summary"),
        str(inputs.get("requirements") or ""),
        str(inputs.get("product_name") or ""),
    )


@router.post(
    "/generate-prd-from-summary-stream",
    summary="按方案生成 PRD（SSE 流式返回）",
)
async def generate_prd_from_summary_stream(
    payload: schemas.GeneratePrdFromSummaryStreamRequest,
    user: User = Depends(acting_user),
) -> StreamingResponse:
    """按方案生成 PRD（需求输入由服务端取），流式返回 PRD 正文。

    与接口文档 / 提示词两条旧通道同形（spec #51 / C-R3）：**只收方案**。需求输入
    （结构化摘要 / 快捷入口那段自由文本 / 需求澄清线程）由 `job_inputs` 按方案从库里
    解析，请求体里一个字段都不带。因此它和任务通道（`kind=prd`）走的是同一份上游，
    只是少了任务框架——两条路不会因为「谁递的需求」而写出不同的 PRD。

    事件序列：`status` → `chunk`（文档头）→ 若干 `stage`/`chunk`（每章）→
    `stage: done` → `done`（`data` 带完整 `reply` 与 `doc_type=prd`）；出错时为 `error`。
    """
    # 解析在**构造 SSE 之前**：进了流之后状态码恒为 200，缺需求就只能读成「生成失败」
    inputs = job_inputs.resolve_runtime_inputs("prd", user.id, payload.solution_id)
    events = document_service.generate_prd_from_summary_stream(
        summary=inputs.get("summary"),
        requirements=str(inputs.get("requirements") or ""),
        product_name=str(inputs.get("product_name") or ""),
        history=inputs.get("history") or None,
    )
    return sse_response(events)


@router.post("/generate-api-docs-stream", summary="根据 PRD 生成接口文档（SSE 流式返回）")
async def generate_api_docs_stream(
    payload: schemas.GenerateApiDocsStreamRequest,
    user: User = Depends(acting_user),
) -> StreamingResponse:
    """按方案生成接口文档正文（上游 PRD 由服务端取）。

    团队接口规范（统一响应结构、错误码、分页、鉴权等）由 `api-docs-generator` 技能包
    整段注入提示词，要求模型按团队约定对齐——不再做「按 PRD 关键词检索挑几条」的 RAG。

    **只收方案**（spec #51 / 票 #54）：请求体里不再有 `prd_content`。上游缺失时
    `resolve_runtime_inputs` 在**构造 SSE 之前**抛 400——进了流之后状态码恒为 200，
    前端就只能把「还没有 PRD」读成「生成失败」。
    """
    inputs = job_inputs.resolve_runtime_inputs("api_docs", user.id, payload.solution_id)
    events = document_service.generate_api_docs_stream(
        prd_content=str(inputs.get("prd_content") or ""),
    )
    return sse_response(events)


@router.post("/generate-prompts-stream", summary="根据 PRD 生成提示词套件（SSE 流式返回）")
async def generate_prompts_stream(
    payload: schemas.GeneratePromptsStreamRequest,
    user: User = Depends(acting_user),
) -> StreamingResponse:
    """按方案生成提示词套件（PRD 与可选的接口文档都由服务端取）。"""
    inputs = job_inputs.resolve_runtime_inputs("prompts", user.id, payload.solution_id)
    events = document_service.generate_prompts_stream(
        prd_content=str(inputs.get("prd_content") or ""),
        api_docs_content=str(inputs.get("api_docs_content") or ""),
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

