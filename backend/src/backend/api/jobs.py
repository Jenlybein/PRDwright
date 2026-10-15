"""生成任务（jobs）路由：创建 / 查询 / 兜底列表 / 订阅流。

本层只做参数校验、归属校验的**结果翻译**与响应封装，业务编排在
`services/job_service.py`，数据隔离在 `repositories/generation_job_repo.py`。

**越权一律 404。** 「任务不存在」与「存在但不属于你」对外的表现完全一致，
统一用 `ErrorCode.SOLUTION_NOT_FOUND`（detail 为「任务不存在」）——不新增错误码，
前端码表也不用动。Repository 在 SQL 层面就不返回别人的任务，这里只把 None 翻译成 404。

**归属校验发生在进入流之前**：`/{job_id}/stream` 先 `get_job` 判归属（None → 真 404），
再把它包成 SSE。这样「越权 / 不存在」是一个明确的 HTTP 404，而不是藏在 200 流里的一条
`error` 事件——后者前端拿不到状态码，会把「没这个任务」误当成「生成失败」。

当前用户经 `acting_user` 获取：`AUTH_ENABLED=false` 的开发模式下会回落到内置 dev 用户，
因此 `user_id` 恒非空。

`/api/jobs/*` **不在**认证白名单里，由 `AuthMiddleware` 默认拒绝、统一保护。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, status

from backend.api import schemas
from backend.api.deps import acting_user
from backend.api.sse import sse_response
from backend.core.errors import AppError, ErrorCode
from backend.services import job_service
from backend.services.auth_service import User
from backend.services.job_fingerprint import compute_input_fingerprint

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

# 任务不存在、或存在但不属于当前用户，都回这一句。
_NOT_FOUND_DETAIL = "任务不存在"


def _validate_payload(payload: schemas.JobCreateRequest) -> None:
    """按 `kind` 校验必填输入；不合法时抛 `INVALID_REQUEST`（HTTP 400）。

    与 `document_service` 的入口校验口径一致——字段格式没问题、只是「这个类型缺输入」，
    因此用 400 而不是 422。
    """
    if payload.kind == "prd":
        has_summary = bool(payload.summary)
        has_requirements = bool(payload.requirements.strip())
        if not has_summary and not has_requirements:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                "summary 与 requirements 至少需要提供一个",
            )
    elif payload.kind in ("api_docs", "prompts"):
        if not payload.prd_content.strip():
            raise AppError(ErrorCode.INVALID_REQUEST, "prd_content 不能为空")
    elif payload.kind == "optimize":
        if not payload.document.strip():
            raise AppError(ErrorCode.INVALID_REQUEST, "document 不能为空")
        if not payload.instruction.strip():
            raise AppError(ErrorCode.INVALID_REQUEST, "instruction 不能为空")


def _requirements_text(payload: schemas.JobCreateRequest) -> str:
    """取用于计算指纹的需求文本：优先自由文本，其次结构化摘要的 JSON。"""
    if payload.requirements.strip():
        return payload.requirements
    if payload.summary:
        return json.dumps(payload.summary, ensure_ascii=False)
    return ""


def _build_request(payload: schemas.JobCreateRequest) -> dict[str, Any]:
    """把请求体摊平成 `job_service` 需要的生成参数（history 为空时置 None）。"""
    return {
        "summary": payload.summary,
        "requirements": payload.requirements,
        "product_name": payload.product_name,
        "history": [message.model_dump() for message in payload.history] or None,
        "prd_content": payload.prd_content,
        "document": payload.document,
        "instruction": payload.instruction,
        # 落库用：`optimize` 靠它知道把结果写进哪一份产物
        "doc_kind": payload.doc_kind,
    }


def _to_status(row: dict[str, Any]) -> schemas.JobStatus:
    """把 DB 行（含未对外暴露的列）收敛成 `JobStatus`（不含 draft_content）。"""
    usage = job_service.parse_usage(row.get("usage"))
    return schemas.JobStatus(
        id=int(row.get("id", 0)),
        kind=str(row.get("kind", "")),
        status=str(row.get("status", "")),
        phase=str(row.get("phase", "") or ""),
        solution_id=row.get("solution_id"),
        error_code=str(row.get("error_code", "") or ""),
        error_message=str(row.get("error_message", "") or ""),
        created_at=str(row.get("created_at", "")),
        updated_at=str(row.get("updated_at", "")),
        usage=usage,
    )


@router.post(
    "",
    response_model=schemas.JobCreated,
    status_code=status.HTTP_201_CREATED,
    summary="创建生成任务并启动后台生成",
)
async def create_job(
    payload: schemas.JobCreateRequest,
    user: User = Depends(acting_user),
) -> schemas.JobCreated:
    """创建一次生成任务并立即启动后台生成，返回任务 id。

    创建即返回——生成在后台进行，与浏览器连接解耦；前端随后订阅
    `/{job_id}/stream` 拿 `snapshot` + live 事件。刷新 / 断线只断订阅，不影响任务。
    """
    _validate_payload(payload)

    fingerprint = None
    if payload.kind == "prd":
        fingerprint = compute_input_fingerprint(_requirements_text(payload))

    job_id = job_service.create_job(
        owner_id=user.id,
        solution_id=payload.solution_id,
        kind=payload.kind,
        input_fingerprint=fingerprint,
    )
    job_service.dispatch(
        job_id,
        owner_id=user.id,
        solution_id=payload.solution_id,
        kind=payload.kind,
        request=_build_request(payload),
    )
    return schemas.JobCreated(job_id=job_id, status="running")


@router.get("", response_model=schemas.JobListResponse, summary="列出我的生成任务")
async def list_jobs(
    solution_id: int | None = None,
    kind: str | None = None,
    status: str | None = None,
    user: User = Depends(acting_user),
) -> schemas.JobListResponse:
    """列出当前用户的任务，可选按 `solution_id` / `kind` / `status` 过滤。

    典型用途是**刷新后的兜底找回**：快照里没记住 `activeJobId`，但用
    `?solution_id=..&kind=..&status=running` 仍能找到那个还在跑的任务并重连。
    响应不含 `draft_content`（轻量），正文只从 stream 的 snapshot 取。
    """
    rows = job_service.list_jobs(user.id, solution_id=solution_id, kind=kind, status=status)
    return schemas.JobListResponse(jobs=[_to_status(row) for row in rows])


@router.get("/{job_id}", response_model=schemas.JobStatus, summary="读取生成任务状态")
async def get_job(
    job_id: int,
    user: User = Depends(acting_user),
) -> schemas.JobStatus:
    """读取任务状态；不存在或非本人返回 404。响应不含 `draft_content`。"""
    row = job_service.get_job(user.id, job_id)
    if row is None:
        raise AppError(ErrorCode.SOLUTION_NOT_FOUND, _NOT_FOUND_DETAIL)
    return _to_status(row)


@router.get("/{job_id}/stream", summary="订阅生成任务的事件流（SSE）")
async def stream_job(
    job_id: int,
    user: User = Depends(acting_user),
):
    """订阅任务事件流：先 `snapshot` 补全已产出正文，再接 live 事件。

    **先做归属校验**（None → 真 404，HTTP 层面明确），再把它包成 SSE。
    """
    if job_service.get_job(user.id, job_id) is None:
        raise AppError(ErrorCode.SOLUTION_NOT_FOUND, _NOT_FOUND_DETAIL)
    return sse_response(job_service.open_stream(user.id, job_id))
