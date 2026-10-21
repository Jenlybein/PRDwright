"""方案（solutions）路由：列表 / 创建 / 读取 / 更新 / 删除。

本层只做参数解析、归属校验的**结果翻译**与响应封装，业务判断在
`services/solution_service.py`，数据隔离在 `repositories/solution_repo.py`。

**越权一律返回 404 而非 403。** 用 403 等于告诉请求方「这个 id 确实存在，只是不属于你」，
攻击者可以借此枚举出系统里有哪些方案；统一 404 则不泄露任何存在性信息。
Repository 已经保证「不属于你的」在 SQL 层面就查不到，这里只是把 None 翻译成 404。

当前用户经 `acting_user` 获取：`AUTH_ENABLED=false` 的开发模式下它会回落到内置 dev 用户，
因此 `user_id` 恒非空，数据模型不必为开发模式开一个 NULL 分支。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from backend.api import schemas
from backend.api.deps import acting_user
from backend.core.chat_kinds import CHAT_STAGES, is_valid_stage
from backend.core.errors import AppError, ErrorCode
from backend.repositories.chat_session_repo import ChatSessionRepository
from backend.repositories.chat_turn_repo import ChatTurnRepository, to_display_turn
from backend.services import job_service, solution_service
from backend.services.auth_service import User

router = APIRouter(prefix="/api/solutions", tags=["solutions"])

# 方案不存在、或存在但不属于当前用户，都回这一句。
_NOT_FOUND_DETAIL = "方案不存在"


@router.get("", response_model=schemas.SolutionListResponse, summary="列出我的方案")
async def list_solutions(user: User = Depends(acting_user)) -> schemas.SolutionListResponse:
    """列出当前用户的方案摘要，最近更新在前。不返回状态列与草稿。"""
    rows = solution_service.list_solutions(user.id)
    return schemas.SolutionListResponse(
        solutions=[schemas.SolutionSummary(**row) for row in rows]
    )


@router.post(
    "",
    response_model=schemas.SolutionSummary,
    status_code=status.HTTP_201_CREATED,
    summary="创建方案",
)
async def create_solution(
    payload: schemas.SolutionSaveRequest,
    user: User = Depends(acting_user),
) -> schemas.SolutionSummary:
    """创建一份方案。

    前端在**首次出现非空内容**时调用；此后同一份方案走 `PUT` 更新。
    """
    row = solution_service.create_solution(
        owner_id=user.id,
        title=payload.title,
        mode=payload.mode,
        stage=payload.stage,
        state=payload.state.model_dump(),
    )
    return schemas.SolutionSummary(**row)


@router.get("/{solution_id}", response_model=schemas.SolutionDetail, summary="读取方案详情")
async def get_solution(
    solution_id: int,
    user: User = Depends(acting_user),
) -> schemas.SolutionDetail:
    """读取方案详情（相位与草稿）；不存在或非本人返回 404。

    聊天记录**不在**这个响应里：它按线程单独取（下一个接口）。两者分开的理由是
    「打开方案」与「翻到另一条线程」的取数粒度不同——后者不该顺手把草稿拖着走。
    """
    row = solution_service.get_solution(user.id, solution_id)
    if row is None:
        raise AppError(ErrorCode.SOLUTION_NOT_FOUND, _NOT_FOUND_DETAIL)
    return schemas.SolutionDetail(
        **solution_service._to_summary(row),  # noqa: SLF001 - 同模块内的裁剪函数
        state=schemas.SolutionStatePayload(**solution_service.state_of(row)),
    )


@router.put("/{solution_id}", response_model=schemas.SolutionDetail, summary="更新方案")
async def update_solution(
    solution_id: int,
    payload: schemas.SolutionSaveRequest,
    user: User = Depends(acting_user),
) -> schemas.SolutionDetail:
    """整份覆盖方案状态；不存在或非本人返回 404。

    刻意不做「字段级局部更新」：状态是**整份**语义，局部更新会引入「两次保存交错时
    谁的字段生效」这类难以推理的问题，而整份覆盖的语义是确定且幂等的。
    """
    row = solution_service.update_solution(
        owner_id=user.id,
        solution_id=solution_id,
        title=payload.title,
        mode=payload.mode,
        stage=payload.stage,
        state=payload.state.model_dump(),
        content_changed=payload.content_changed,
    )
    if row is None:
        raise AppError(ErrorCode.SOLUTION_NOT_FOUND, _NOT_FOUND_DETAIL)
    return schemas.SolutionDetail(
        **solution_service._to_summary(row),  # noqa: SLF001 - 同模块内的裁剪函数
        state=schemas.SolutionStatePayload(**solution_service.state_of(row)),
    )


@router.get(
    "/{solution_id}/turns",
    response_model=schemas.ChatTurnListResponse,
    summary="读取对话轮次",
)
async def list_turns(
    solution_id: int,
    stage: str | None = None,
    user: User = Depends(acting_user),
) -> schemas.ChatTurnListResponse:
    """读取对话轮次，按发生顺序排列。

    **不传 `stage` 时返回全部线程**：前端把四条线程合并成一个展示数组（按 `stage`
    过滤渲染），打开方案只需要一次往返。传了就只要那一条——「翻到某条线程」是另一种粒度。

    `stage` 传了就必须合法：四条线程的人设与上下文完全不同，**不给默认值**——
    默认成需求澄清会让「翻到 PRD 澄清」看到别人的对话，不报错但答非所问。

    越权与不存在的方案都返回 404（不是空列表）：空列表是「这条线程还没聊过」，
    与「你打不开这份方案」是两件不同的事。
    """
    if stage is not None and not is_valid_stage(stage):
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            f"未知的对话线程：{stage}（可选：{'、'.join(CHAT_STAGES)}）",
        )
    if not solution_service.solution_exists(user.id, solution_id):
        raise AppError(ErrorCode.SOLUTION_NOT_FOUND, _NOT_FOUND_DETAIL)
    rows = (
        ChatTurnRepository.list_thread(user.id, solution_id, stage)
        if stage is not None
        else ChatTurnRepository.list_all(user.id, solution_id)
    )
    return schemas.ChatTurnListResponse(
        turns=[schemas.ChatTurn(**to_display_turn(row)) for row in rows]
    )


@router.delete(
    "/{solution_id}/turns",
    response_model=schemas.SolutionDeleteResponse,
    summary="清空某条对话线程",
)
async def clear_turns(
    solution_id: int,
    stage: str,
    user: User = Depends(acting_user),
) -> schemas.SolutionDeleteResponse:
    """删掉某条线程的全部轮次与会话 id。

    唯一触发点是「整篇重新生成那份产物」：旧线程聊的是**上一版**的待确认项，
    留着它，用户点「按澄清结果改写」就会把针对旧文档的结论写进新文档——
    一次静默的错写，而且看起来还挺合理。

    清完轮次还要**清掉会话 id**：那一头的服务端会话记着旧上下文，只删本地轮次
    等于把上下文从中间截断，模型会在一个「记得旧文档」的会话上接着聊。

    走服务端而不是前端本地清：消息的真源在这里，只清本地镜像会在刷新后
    「自己长回来」——那种现象比不删更难解释。
    """
    if not is_valid_stage(stage):
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            f"未知的对话线程：{stage}（可选：{'、'.join(CHAT_STAGES)}）",
        )
    if not solution_service.solution_exists(user.id, solution_id):
        raise AppError(ErrorCode.SOLUTION_NOT_FOUND, _NOT_FOUND_DETAIL)
    ChatTurnRepository.delete_thread(user.id, solution_id, stage)
    ChatSessionRepository.set(user.id, solution_id, stage, "")
    return schemas.SolutionDeleteResponse(ok=True)


@router.delete(
    "/{solution_id}",
    response_model=schemas.SolutionDeleteResponse,
    summary="删除方案",
)
async def delete_solution(
    solution_id: int,
    user: User = Depends(acting_user),
) -> schemas.SolutionDeleteResponse:
    """删除方案；不存在或非本人返回 404。

    同样返回 404 而不是「静默成功」：否则删除别人的方案会与删除自己的返回相同结果，
    调用方无法察觉自己的归属判断写错了。

    **删除成功后要取消挂在这份方案上正在跑的任务**：方案没了，任务再跑完也没地方落正文，
    继续跑只是白烧 token，收尾时还会去写一份注定被丢弃的文档。

    ⚠️ 这段编排**放在 api 层而不是 service 层**：`job_service` 已经依赖
    `solution_service`（收尾前要确认方案还在），若让 `solution_service` 反向调用
    `job_service`，两个服务就互相 import 成环。路由层本来就知道「删方案」这件事的
    完整语义，放在这里既不引入环，也让两条服务各自保持单向依赖。
    """
    if not solution_service.delete_solution(user.id, solution_id):
        raise AppError(ErrorCode.SOLUTION_NOT_FOUND, _NOT_FOUND_DETAIL)
    job_service.cancel_for_solution(user.id, solution_id)
    return schemas.SolutionDeleteResponse(ok=True)
