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

from typing import Any

from fastapi import APIRouter, Depends, status

from backend.api import schemas
from backend.api.deps import acting_user
from backend.api.sse import sse_response
from backend.core.chat_kinds import CHAT_STAGES, is_valid_stage
from backend.core.errors import AppError, ErrorCode
from backend.services import job_inputs, job_service, job_ticket, quota_service
from backend.services.auth_service import User
from backend.services.job_drain import drain_controller
from backend.services.job_fingerprint import compute_input_fingerprint
from backend.services.job_snapshot import parse_usage

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

# 任务不存在、或存在但不属于当前用户，都回这一句。
_NOT_FOUND_DETAIL = "任务不存在"


def _validate_payload(payload: schemas.JobCreateRequest) -> None:
    """按 `kind` 校验必填输入；不合法时抛 `INVALID_REQUEST`（HTTP 400）。

    与 `document_service` 的入口校验口径一致——字段格式没问题、只是「这个类型缺输入」，
    因此用 400 而不是 422。

    **文档类任务必须带方案**（spec #51 / 票 #54）：输入已经从请求体搬到了服务端，
    没有方案就没有上游可查，缺它直接 400，而不是建一个注定空跑的任务。
    材料本身有没有（库内当前版本 / 方案上的录入文本）由 `job_inputs.resolve_runtime_inputs`
    在下面查——那是同一条「说清缺什么」的提示。
    """
    if payload.kind != "chat" and payload.solution_id is None:
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            "文档类任务必须关联一份方案（solution_id）：输入由服务端按方案取，缺方案就没有上游可查",
        )
    if payload.kind == "prd":
        # 需求摘要 / 自由文本 / 澄清历史全部由服务端从方案上取，请求体里没有它们的字段
        return
    if payload.kind in ("api_docs", "prompts"):
        # 上游 PRD（以及可选的接口文档）同样由服务端取；缺材料在解析时给出明确 400
        return
    if payload.kind == "optimize":
        if not payload.document.strip():
            raise AppError(ErrorCode.INVALID_REQUEST, "document 不能为空")
        if not payload.instruction.strip():
            raise AppError(ErrorCode.INVALID_REQUEST, "instruction 不能为空")
    elif payload.kind == "clarify":
        if not payload.document.strip():
            raise AppError(ErrorCode.INVALID_REQUEST, "document 不能为空")
        # `doc_kind` 决定两件事：注入哪一套产出规范、改写结果写回哪一份文档。
        # 缺了它就只能「不落库」，那这次澄清等于白做——因此在门口直接拒掉。
        if payload.doc_kind is None:
            raise AppError(ErrorCode.INVALID_REQUEST, "clarify 必须指定 doc_kind")
    elif payload.kind == "auto_clarify":
        if not payload.document.strip():
            raise AppError(ErrorCode.INVALID_REQUEST, "document 不能为空")
        # 与 clarify 同理：缺 `doc_kind` 就不知道规范从哪来、结果写回哪一份
        if payload.doc_kind is None:
            raise AppError(ErrorCode.INVALID_REQUEST, "auto_clarify 必须指定 doc_kind")
        # 没有清单就没有可拍板的东西，跑一次只会把正文原样吐回来（还烧一次 token）
        if not payload.pending_items:
            raise AppError(ErrorCode.INVALID_REQUEST, "auto_clarify 需要提供待确认清单（pending_items）")
    elif payload.kind == "review":
        # 重审**既不收正文也不收上游**：要审的那一份、以及它比对的材料都由服务端从库里取
        # （C-R3）。因此这里只校验「哪一份」——「在哪」已由开头那条统一校验挡掉了
        # （文档类任务必须带方案），分支里再判一次 `solution_id is None` 是死代码。
        if payload.doc_kind is None:
            raise AppError(ErrorCode.INVALID_REQUEST, "review 必须指定 doc_kind")
    elif payload.kind == "review_fix":
        # 修复同理：被修的正文、那条结论、上游材料全由服务端取（C-R3）；
        # `doc_kind` 还决定注入哪份上游全文，缺了既不指导哪一份、也拼不出上下文。
        if payload.doc_kind is None:
            raise AppError(ErrorCode.INVALID_REQUEST, "review_fix 必须指定 doc_kind")
        # 结论是不是「有问题」由 `job_inputs._fix_inputs` 在建任务时查库判定：
        # 那一步本来就要读当前版本，再读一次结论不会多一次往返。
    elif payload.kind == "chat":
        # 线程标识决定「这轮回复记到谁头上」，猜错等于把 A 线程的对话记到 B 线程；
        # 四条线程的人设与上下文完全不同，因此不给默认值，缺了就 400。
        if not is_valid_stage(payload.stage):
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                f"chat 需要合法的 stage（可选：{'、'.join(CHAT_STAGES)}）",
            )
        if payload.solution_id is None:
            raise AppError(ErrorCode.INVALID_REQUEST, "chat 必须关联一份方案")
        if payload.chat_mode not in ("start", "continue"):
            raise AppError(ErrorCode.INVALID_REQUEST, "chat_mode 只能是 start 或 continue")
        if payload.chat_mode == "start" and not payload.form_data:
            raise AppError(ErrorCode.INVALID_REQUEST, "开新会话需要提供 form_data")
        if payload.chat_mode == "continue" and not (
            payload.content.strip() or payload.tool_result is not None
        ):
            raise AppError(ErrorCode.INVALID_REQUEST, "续聊需要提供 content 或 tool_result")


def _build_request(payload: schemas.JobCreateRequest) -> dict[str, Any]:
    """把请求体摊平成 `job_service` 需要的生成参数。

    **只含「做什么、对哪一份」**：上游正文与需求正文由 `job_inputs` 在 `create_job` 里
    解析后随任务单走（不落进 `request_params`），这里一个都不出现。
    """
    return {
        "document": payload.document,
        "instruction": payload.instruction,
        # 落库用：`optimize` 靠它知道把结果写进哪一份产物
        "doc_kind": payload.doc_kind,
        # 仅 auto_clarify：交给后端拼提示词的**结构化数据**，不是前端拼好的文本
        "pending_items": payload.pending_items,
        # 仅 prd：`None` 时由服务层取配置，显式 `false` 才关掉逐章审核
        "review_enabled": payload.review_enabled,
        # --- 仅 chat ---
        "stage": payload.stage,
        "chat_mode": payload.chat_mode,
        "form_data": payload.form_data,
        "content": payload.content,
        "tool_result": payload.tool_result,
        "profile_key": payload.profile_key,
    }


def _to_status(row: dict[str, Any]) -> schemas.JobStatus:
    """把 DB 行（含未对外暴露的列）收敛成 `JobStatus`（不含 draft_content）。"""
    usage = parse_usage(row.get("usage"))
    return schemas.JobStatus(
        id=int(row.get("id", 0)),
        kind=str(row.get("kind", "")),
        # 空串（旧行）退化成 None：宁可不报，也不猜一份产物出来。
        doc_kind=str(row.get("doc_kind") or "") or None,
        # 对话线程同理：旧行没有这一列，空串退化成 None，调用方不得猜
        chat_stage=str(row.get("chat_stage") or "") or None,
        status=str(row.get("status", "")),
        phase=str(row.get("phase", "") or ""),
        solution_id=row.get("solution_id"),
        error_code=str(row.get("error_code", "") or ""),
        error_message=str(row.get("error_message", "") or ""),
        created_at=str(row.get("created_at", "")),
        updated_at=str(row.get("updated_at", "")),
        usage=usage,
        partial=bool(usage.get("partial")) if isinstance(usage, dict) else False,
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
    # 排空期间不收新活。放在最前面（连参数校验之前）：此刻「请求长什么样」已经无关
    # 紧要，服务端就是不接。塞进队列的代价是「任务起来了、进程随后死了、下次启动才
    # 告诉用户失败」——用户等三分钟换来一句生成失败，而重试用时只要几秒。
    #
    # 503 用既有的 INTERNAL_ERROR 承载，不新增错误码：错误码是前后端共享契约
    # （前端有一份逐字镜像的枚举），加一个就得多改一处前端，而这一版不碰前端。
    if drain_controller.is_draining():
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            "服务正在重启（排空），暂不接收新的生成任务，请稍后重试",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    _validate_payload(payload)

    # 额度检查必须在这里——**校验之后、建任务之前**。
    # 放在 `create_job` 之后的话，被拒的请求会先留下一行 failed，污染失败率、
    # 也会出现在用户的任务列表里（「我什么都没生成，却多了一条失败记录」）。
    # `kind=chat` 不受配额管（对话是生成的前置，限了连「为什么被拒」都问不了）。
    quota_service.check(user, payload.kind)

    # 输入一律由服务端按方案取（spec #51 / 契约 C-R3）：在**建任务之前**解析一次。
    # 放在这里而不是开流时，是为了让「缺方案 / 缺材料」成为一次货真价实的 400——
    # 而不是「任务建起来了、随后在流里失败」（那会在任务列表里留一条失败记录）。
    # 解析结果随任务单定格，派发时合进运行时请求参数，落库的 `request_params`
    # 仍然只记「调用方发了什么」。
    resolved = job_inputs.resolve_runtime_inputs(
        payload.kind, user.id, payload.solution_id, payload.doc_kind
    )

    fingerprint = None
    if payload.kind == "prd":
        # 指纹与真正喂给模型的需求同源：都取自刚刚解析出来的那一份
        fingerprint = compute_input_fingerprint(job_inputs.prd_requirement_text(resolved))

    # 摊平参数只算一次，并且**只交给造单这一个入口**：单①把请求参数、配置/供应商
    # 快照与技能包身份一次定格成不可变数据，登记产出单②，派发只认单②。旧形状把同一份
    # 可变 dict 同时递给 `create_job` 与 `dispatch`（chat 还会原地改写它），从此不再发生。
    request = _build_request(payload)
    ticket = job_ticket.build_job_ticket(
        owner_id=user.id,
        solution_id=payload.solution_id,
        kind=payload.kind,
        request=request,
        resolved_inputs=resolved,
        input_fingerprint=fingerprint,
    )
    registered = job_ticket.register_job_ticket(ticket)
    job_ticket.start_registered_job(registered)
    return schemas.JobCreated(job_id=registered.job_id, status="running")


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


@router.post(
    "/{job_id}/cancel",
    response_model=schemas.JobCancelResponse,
    summary="停止一次任务",
)
async def cancel_job(
    job_id: int,
    user: User = Depends(acting_user),
) -> schemas.JobCancelResponse:
    """停止某一次任务（用户按「停止生成」）。

    与「退订」严格分开
    ----------------
    - 退订（切方案 / 刷新 / 关页面）= 我先不看，任务继续跑，回来还能接上；
    - 停止（这个接口）= 别做了。

    两者混在一起，就会得到这个项目最早那个 bug 的反面：只是切走一次，
    正在生成的内容就白等了。

    **任务已经结束不算失败**：`cancelled=false` 表示「你点的时候它刚好说完了」。
    用 409 报错会让前端把一次无害的时序竞争当成故障弹提示。

    越权与不存在仍然 404（与其它任务接口同一口径）。
    """
    if job_service.get_job(user.id, job_id) is None:
        raise AppError(ErrorCode.SOLUTION_NOT_FOUND, _NOT_FOUND_DETAIL)
    cancelled = job_service.cancel_job(user.id, job_id)
    return schemas.JobCancelResponse(ok=True, cancelled=cancelled)


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
